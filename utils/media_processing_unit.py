"""媒体处理器 — 将图片/视频转换为 Telegram 兼容格式"""

import asyncio
import json
import math
import mimetypes
import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from haishoku.haishoku import Haishoku
from loguru import logger
from PIL import Image, ImageOps
from PIL.Image import Resampling

from utils.helpers import run_cmd

# ffmpeg/ffprobe 可执行文件路径。
# Windows 的 winloop 不解析 PATH 中的命令名（子进程报 WinError 3），必须给完整路径；
# Linux 的 uvloop 同样不解析 PATH，因此统一用 shutil.which 解析成绝对路径。
if sys.platform == "win32":
    FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
    FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe"
else:
    FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
    FFPROBE = shutil.which("ffprobe") or "ffprobe"


@dataclass
class MediaProcessResult:
    """统一处理结果"""

    output_paths: list[Path]
    temp_dir: Path | None = None


class MediaProcessingUnit:
    """媒体处理器，将媒体转换为 Telegram 兼容的格式

    Telegram 限制：
    - 图片宽高比 / 高宽比不能超过 20:1
    - 单次最多发送 10 张图片
    - 单个视频最大 2000 MB

    用法：
        mpu = MediaProcessingUnit(output_dir=Path("./output"))
        result = await mpu.process("media.mp4")
    """

    TG_MAX_VIDEO_SIZE = 2000 * 1000**2

    def __init__(
        self,
        output_dir: str | Path,
        segment_height: int = 1400,
        medium_threshold: int = 2,
        overlap: int = 100,
        logger: Callable = logger.info,
        autoplay_safe_transcode: bool = True,
        skip_max_duration: float = 600,
        skip_max_size: int = 500 * 1000**2,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.segment_height = segment_height
        self.medium_threshold = medium_threshold
        self.overlap = overlap
        self.logger = logger
        self.autoplay_safe_transcode = autoplay_safe_transcode
        self.skip_max_duration = skip_max_duration
        self.skip_max_size = skip_max_size

    # ------------------------------------------------------------------ #
    #  公共入口
    # ------------------------------------------------------------------ #

    async def process(self, file_path: str | Path) -> MediaProcessResult:
        media_type = await self.get_media_type_by_mime(file_path)
        self.logger(f"开始处理媒体: path={file_path}, type={media_type}")
        if media_type == "image":
            return await self.process_image(Path(file_path))
        elif media_type == "video":
            return await self.process_video(Path(file_path))
        elif media_type == "audio":
            self.logger("音频无需处理")
            return MediaProcessResult(output_paths=[Path(file_path)])
        else:
            raise ValueError(f"Unsupported media type: {file_path}")

    # ------------------------------------------------------------------ #
    #  图片处理
    # ------------------------------------------------------------------ #

    async def process_image(self, file_path: Path) -> MediaProcessResult:
        image_format, is_broken, image_mode = await self._detect_image_format(file_path)
        if image_format == "GIF":
            self.logger("图片为 GIF 格式，无需处理")
            return MediaProcessResult(output_paths=[file_path])
        needs_convert = image_format not in {"PNG", "JPEG"}
        needs_rgb = image_mode == "RGBA"
        source = file_path
        intermediates: list[Path] = []  # 统一收集中间文件

        try:
            if needs_convert or is_broken or needs_rgb:
                self.logger(f"图片需转换: format={image_format}, mode={image_mode}, broken={is_broken}")
                source = await self._img2jpg(file_path)
                intermediates.append(source)

            if result := await asyncio.to_thread(self._adapt_image, source):
                return result

            # _adapt_image 无需处理，尝试 downscale
            if downscaled := await asyncio.to_thread(self._downscale_image, source):
                intermediates.append(downscaled)
                source = downscaled

            intermediates = [p for p in intermediates if p != source]
            return MediaProcessResult(output_paths=[source])
        finally:
            for p in intermediates:
                if p.exists():
                    self.logger(f"删除中间文件: {p}")
                    os.remove(p)

    @staticmethod
    async def _detect_image_format(file_path: Path) -> tuple[str, bool, str]:
        try:
            with Image.open(file_path) as img:
                return (img.format or "").upper(), False, img.mode
        except OSError:
            return await MediaProcessingUnit._probe_image_format(file_path), True, ""

    @staticmethod
    async def _probe_image_format(file_path: Path) -> str:
        codec = await run_cmd(
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        )
        return {"mjpeg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}.get(
            codec.strip().lower(), codec.strip().upper()
        )

    def _adapt_image(self, file_path: Path) -> MediaProcessResult | None:
        """分析图片尺寸并做填充 / 切割，返回 None 表示无需处理"""
        with Image.open(file_path) as img:
            w, h = img.width, img.height

        wh_ratio = w / h
        hw_ratio = h / w
        self.logger(f"图片尺寸: {w}x{h}, wh_ratio={wh_ratio:.2f}, hw_ratio={hw_ratio:.2f}")

        if w >= h:
            # 横图
            if wh_ratio <= 20:
                self.logger("横图比例正常，跳过处理")
                return None
            self.logger("横图比例超限，需要填充")
            padding = self._calc_padding_horizontal(w, h)
            with Image.open(file_path) as img:
                return self._pad_image(file_path, img, padding)
        else:
            # 竖图
            if hw_ratio <= 5 or (w < 200 and hw_ratio < 20):
                self.logger("竖图比例正常，跳过处理")
                return None
            if w < 200 and hw_ratio > 20:
                self.logger("窄竖图比例超限，需要填充")
                padding = self._calc_padding_vertical(w, h)
                with Image.open(file_path) as img:
                    return self._pad_image(file_path, img, padding)
            # 长图切割
            segments = h // self.segment_height
            seg_h = h // 2 if segments < self.medium_threshold else self.segment_height
            self.logger(f"长图切割: segments={segments}, seg_h={seg_h}")
            return self._split_image(file_path, seg_h)

    async def _img2jpg(self, file_path: Path) -> Path:
        output = self.output_dir / file_path.with_suffix(".jpg").name
        try:
            with Image.open(file_path) as pil_img:
                img = pil_img.convert("RGB") if pil_img.mode != "RGB" else pil_img
                img.save(output, format="JPEG")
        except OSError as e:
            self.logger(f"Pillow 转换失败，尝试 ffmpeg: {e}")
            await run_cmd(
                FFMPEG,
                "-v",
                "error",
                "-i",
                str(file_path),
                "-frames:v",
                "1",
                "-q:v",
                "1",
                "-y",
                str(output),
                timeout=60,
            )
            if not output.exists() or output.stat().st_size == 0:
                raise
        self.logger(f"图片转换完成: {output}")
        return output

    def _downscale_image(self, file_path: Path, max_side: int = 2560) -> Path | None:
        """若图片任一边超过 max_side，等比缩放至长边为 max_side，返回新文件路径；无需缩放返回 None"""
        with Image.open(file_path) as img:
            w, h = img.size
            if max(w, h) <= max_side:
                return None
            scale = max_side / max(w, h)
            new_w, new_h = int(w * scale), int(h * scale)
            self.logger(f"图片长边超限({max(w, h)}px > {max_side}px)，缩放: {w}x{h} -> {new_w}x{new_h}")
            resized = img.resize((new_w, new_h), Resampling.LANCZOS)
            ext = file_path.suffix
            out_path = self.output_dir / f"{file_path.stem}_downscaled{ext}"
            resized.save(out_path)
        return out_path

    # -- 图片辅助 --------------------------------------------------------- #

    @staticmethod
    def _calc_padding_horizontal(w: int, h: int) -> tuple[int, int, int, int]:
        h_padding = w // 20 - h // 2
        return 0, h_padding, 0, h_padding

    @staticmethod
    def _calc_padding_vertical(w: int, h: int) -> tuple[int, int, int, int]:
        w_padding = h // 20 - w // 2
        return w_padding, 0, w_padding, 0

    @staticmethod
    def _get_dominant_color(file_path: Path) -> tuple[int, ...]:
        haishoku = Haishoku.loadHaishoku(str(file_path))
        return tuple(int(v * 0.8) for v in haishoku.palette[0][1])

    def _pad_image(
        self,
        file_path: Path,
        img: Image.Image,
        padding: tuple[int, int, int, int],
    ) -> MediaProcessResult:
        fill_color = self._get_dominant_color(file_path)
        padded = ImageOps.expand(img, padding, fill=fill_color)
        out_path = self.output_dir / f"padded_{time.time_ns()}.png"
        padded.save(out_path)
        self.logger(f"填充完成: padding={padding}, color={fill_color}, output={out_path}")
        return MediaProcessResult(output_paths=[out_path])

    def _split_image(self, file_path: Path, segment_height: int) -> MediaProcessResult:
        temp_dir = self.output_dir / f"split_{time.time_ns()}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        segments = self._do_split(file_path, temp_dir, segment_height)
        self.logger(f"图片切割完成: {len(segments)} 段, output_dir={temp_dir}")
        return MediaProcessResult(output_paths=segments, temp_dir=temp_dir)

    def _do_split(
        self,
        input_path: Path,
        output_dir: Path,
        segment_height: int,
    ) -> list[Path]:
        with Image.open(input_path) as img:
            width, height = img.size
            num_segments = math.ceil(height / segment_height)
            self.logger(f"切割参数: size={width}x{height}, segment_h={segment_height}, num={num_segments}")
            result: list[Path] = []
            for i in range(num_segments):
                top = i * segment_height - (self.overlap if i != 0 else 0)
                bottom = min((i + 1) * segment_height, height)
                segment = img.crop((0, top, width, bottom))
                out_path = output_dir / f"segment_{i + 1:03d}.png"
                segment.save(out_path)
                result.append(out_path)
        return result

    # ------------------------------------------------------------------ #
    #  视频处理
    # ------------------------------------------------------------------ #

    async def process_video(self, file_path: Path) -> MediaProcessResult:
        """处理视频，保证产物"可流式"（H.264+AAC+MP4+faststart）。

        决策顺序：
        1. 视频编码≠h264 → 走 H.264 转码（含硬件优先/兜底阈值），产物为 faststart mp4；
        2. 视频已是 h264 但（容器非 mp4 或 音频不兼容 mp4 或 非 faststart）→ remux 到 mp4
           （-c:v copy，音频不兼容时 -c:a aac），加 +faststart；
        3. 全部满足 → 原样直通，不产生新文件。
        """
        # autoplay_safe_transcode 关闭时完全退回旧行为：仅容器非 mp4 时 remux 换壳
        if not self.autoplay_safe_transcode:
            self.logger("autoplay_safe_transcode 已关闭，退回旧行为（仅 remux 换壳）")
            container = await self.get_container(file_path)
            if "mp4" not in container:
                converted = await self.remux_to_mp4(file_path)
                source = converted if converted != file_path else file_path
            else:
                source = file_path
            return await self._finalize_video(source)

        container = await self.get_container(file_path)
        vcodec = await self.get_video_codec(file_path)
        acodec = await self.get_audio_codec(file_path)
        self.logger(f"视频信息: container={container}, vcodec={vcodec}, acodec={acodec!r}, path={file_path}")

        intermediates: list[Path] = []
        source: Path = file_path

        try:
            if vcodec != "h264":
                # 视频编码不是 H.264 → 转码
                # 兜底：无硬件编码且超阈值的长/大视频，跳过转码直发原文件
                if not await self.has_hw_encoder() and await self._exceeds_skip_threshold(file_path):
                    self.logger(
                        "无硬件编码且视频超阈值（长/大），跳过转码直发原文件，"
                        "该视频可能无法自动播放（性能与体验取舍）"
                    )
                    return await self._finalize_video(file_path)
                self.logger(f"视频编码 {vcodec} != h264，走转码")
                converted = await self.ensure_h264(file_path)
                if converted != file_path:
                    source = converted
                    intermediates.append(converted)
                else:
                    # 转码失败回退原文件
                    return await self._finalize_video(file_path)
            else:
                # 已是 h264：容器非 mp4 / 音频不兼容 / 非 faststart → remux
                audio_incompatible = acodec in {"opus", "vorbis", "flac"}
                need_remux = "mp4" not in container or audio_incompatible or not self.is_faststart(file_path)
                if need_remux:
                    self.logger(
                        f"h264 但需要 remux: container={container}, acodec={acodec!r}, "
                        f"faststart={self.is_faststart(file_path)}"
                    )
                    converted = await self.remux_to_mp4(file_path)
                    if converted != file_path:
                        source = converted
                        intermediates.append(converted)
                else:
                    self.logger("h264+AAC+mp4+faststart 全部满足，原样直通")
                    return await self._finalize_video(file_path)

            result = await self._finalize_video(source)
            return result
        finally:
            # 清理中间产物：删除不在最终 output_paths 里的（分割后源不再被发送）
            final_paths = set(locals().get("result").output_paths) if "result" in locals() and locals().get("result") else set()
            for p in intermediates:
                if p not in final_paths and p.exists():
                    self.logger(f"删除中间文件: {p}")
                    os.remove(p)

    async def _exceeds_skip_threshold(self, file_path: Path) -> bool:
        """无硬件编码时的兜底阈值判断：时长 > max_duration 或 体积 > max_size"""
        try:
            duration = await self.get_duration(file_path)
            size = file_path.stat().st_size
        except Exception:
            return False
        if duration > self.skip_max_duration:
            return True
        if size > self.skip_max_size:
            return True
        return False

    async def _finalize_video(self, source: Path) -> MediaProcessResult:
        """分割超限视频或直接返回，兼容现有 output_paths/temp_dir 语义"""
        try:
            video_size = source.stat().st_size
        except OSError:
            video_size = 0
        self.logger(f"视频大小: {video_size / 1000 / 1000:.1f} MB ({video_size / 1024 / 1024:.1f} MiB)")
        if video_size > self.TG_MAX_VIDEO_SIZE:
            self.logger(f"视频超过 {self.TG_MAX_VIDEO_SIZE / 1000 / 1000:.0f} MB 限制，开始分割")
            output_paths, output_dir = await self.split_video(source, self.output_dir, self.TG_MAX_VIDEO_SIZE)
            return MediaProcessResult(output_paths=output_paths, temp_dir=output_dir)
        return MediaProcessResult(output_paths=[source])

    @staticmethod
    async def get_video_codec(file_path: Path) -> str:
        out = await run_cmd(
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        )
        return out.strip().lower() if out else ""

    @staticmethod
    async def get_audio_codec(file_path: Path) -> str:
        """探测音频编码名；无音轨返回空字符串"""
        out = await run_cmd(
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        )
        return out.strip().lower() if out else ""

    @staticmethod
    def is_faststart(file_path: Path) -> bool:
        """扫描 mp4 顶层 box，判断 moov 是否出现在 mdat 之前。

        解析 8 字节 box header（size+type）；size==1 时读 64 位扩展 size，
        size==0 表示该 box 延伸到文件尾。非 mp4/无法解析时返回 False。
        """
        try:
            with open(file_path, "rb") as f:
                seen_mdat = False
                while True:
                    header = f.read(8)
                    if len(header) < 8:
                        break
                    size = int.from_bytes(header[:4], "big")
                    box_type = header[4:8]
                    if size == 1:
                        ext = f.read(8)
                        if len(ext) < 8:
                            return False
                        size = int.from_bytes(ext, "big")
                    elif size == 0:
                        size = os.path.getsize(file_path) - f.tell() + 8
                    if box_type == b"mdat":
                        seen_mdat = True
                    elif box_type == b"moov":
                        return not seen_mdat
                    if size < 8:
                        return False
                    f.seek(size - 8, 1)
        except Exception:
            return False
        return False

    @staticmethod
    async def get_container(file_path: Path) -> str:
        result = await run_cmd(
            FFPROBE, "-v", "error", "-show_entries", "format=format_name", "-of", "json", str(file_path)
        )
        r: dict[str, dict[str, str]] = json.loads(result)
        if not (f := r.get("format", {})):
            raise ValueError(f"ffprobe failed to get container: {result}")
        return f["format_name"]

    @staticmethod
    async def _encoder_really_works(encoder: str) -> bool:
        """实际试跑一次极小编码，确认编码器在本机真的可用。

        `ffmpeg -encoders` 只说明这个 ffmpeg 构建**编进了**该编码器，不代表
        机器上有对应硬件。例如没有 NVIDIA 显卡的机器同样会列出 h264_nvenc,
        真跑时报 "Error while opening encoder" 失败。因此必须实测。
        """
        cmd = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=0.1:size=64x64:rate=10",
            "-frames:v",
            "1",
            "-c:v",
            encoder,
            "-pix_fmt",
            "yuv420p",
            "-f",
            "null",
            "-",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception:
            return False
        if proc.returncode != 0:
            logger.debug(
                f"硬件编码器 {encoder} 实测不可用(rc={proc.returncode}): "
                f"{stderr.decode(errors='replace')[-200:]}"
            )
            return False
        return True

    @staticmethod
    async def detect_hw_encoder() -> str | None:
        """探测本机**实际可用**的硬件 H.264 编码器，返回编码器名或 None。

        先确认 ffmpeg 编进了该编码器，再实际试跑一次确认硬件真的可用。
        优先级：nvenc > qsv > videotoolbox。
        """
        try:
            out = await run_cmd(FFMPEG, "-hide_banner", "-encoders", timeout=15)
        except Exception:
            return None

        candidates = ["h264_nvenc", "h264_qsv", "h264_videotoolbox"]
        for enc in candidates:
            if enc not in out:
                continue
            if await MediaProcessingUnit._encoder_really_works(enc):
                return enc
            logger.info(f"硬件编码器 {enc} 在 ffmpeg 中存在但本机不可用, 跳过")
        return None

    async def get_hw_encoder(self) -> str | None:
        """可用的硬件 H.264 编码器名, None 表示只能用软件编码(惰性缓存)"""
        if not hasattr(self, "_hw_encoder"):
            self._hw_encoder = await self.detect_hw_encoder()
        return self._hw_encoder

    async def has_hw_encoder(self) -> bool:
        """是否可用硬件 H.264 编码器(惰性缓存)"""
        return await self.get_hw_encoder() is not None

    @staticmethod
    async def get_duration(file_path: Path) -> float:
        out = await run_cmd(
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        )
        return float(out.strip()) if out else 0.0

    async def remux_to_mp4(self, file_path: Path) -> Path:
        """重封装到 faststart mp4（视频 -c:v copy 零重编码）。

        音频为 opus/vorbis/flac 等非 mp4 常规编码时改用 -c:a aac 转码音频；
        失败（返回码非 0 或产物为空）抛异常，不静默产出坏文件。
        """
        out = self.output_dir / (file_path.stem + "_remux" + ".mp4")
        acodec = await self.get_audio_codec(file_path)
        audio_args: list[str] = []
        if acodec in {"opus", "vorbis", "flac"}:
            self.logger(f"音频编码 {acodec} 不兼容 mp4，音频转 aac（视频仍 copy）")
            audio_args = ["-c:a", "aac"]
        cmd = [
            FFMPEG,
            "-i",
            str(file_path),
            "-c:v",
            "copy",
            *audio_args,
            "-movflags",
            "+faststart",
            "-y",
            str(out),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            self.logger(f"remux 失败(rc={proc.returncode}): {stderr.decode(errors='replace')[-300:]}")
            raise RuntimeError(f"remux to mp4 failed: {file_path}")
        return out

    async def ensure_h264(self, file_path: Path) -> Path:
        """确保视频为 H.264+AAC+faststart mp4；转码失败回退原文件。

        硬件编码优先（nvenc/qsv/videotoolbox），失败回退 libx264。
        输出强制 .mp4（file_path.stem + "_h264.mp4"）。
        """
        out = self.output_dir / (file_path.stem + "_h264.mp4")
        duration = await self.get_duration(file_path)
        height = await self._get_video_height(file_path)
        acodec = await self.get_audio_codec(file_path)
        has_audio = bool(acodec)

        hw = await self.get_hw_encoder()
        if hw:
            self.logger(f"h264 转码: {file_path.name} -> {out.name}, duration={duration:.0f}s, encoder=HW:{hw}")
            cmd = self._build_hw_transcode_cmd(file_path, out, duration, height, hw, has_audio)
            ok = await self._run_transcode(cmd, duration)
            if ok:
                self.logger(f"h264 硬件转码成功: {out}")
                return out
            self.logger(f"硬件转码失败，回退软件转码: {file_path.name}")

        self.logger(f"h264 转码: {file_path.name} -> {out.name}, duration={duration:.0f}s, encoder=SW:libx264")
        cmd = self._build_sw_transcode_cmd(file_path, out, duration, height, has_audio)
        ok = await self._run_transcode(cmd, duration)
        if ok:
            self.logger(f"h264 转码成功: size={out.stat().st_size / 1024 / 1024:.1f}MB")
            return out

        self.logger(f"转码失败，该视频可能无法自动播放，返回原文件: {file_path}")
        return file_path

    async def _run_transcode(self, cmd: list[str], duration: float) -> bool:
        """运行转码命令，按源时长估算超时；超时/失败视为失败（不卡死 bot）"""
        timeout = max(600, int(duration * 30))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            self.logger(f"转码超时（>{timeout}s），视为失败")
            return False
        if proc.returncode != 0:
            self.logger(f"转码返回码 {proc.returncode}: {stderr.decode(errors='replace')[-200:]}")
            return False
        return True

    def _build_hw_transcode_cmd(
        self, file_path: Path, out: Path, duration: float, height: int, encoder: str, has_audio: bool
    ) -> list[str]:
        """硬件编码命令（映射到等效 crf 档位）；失败回退 libx264 由调用方处理"""
        crf_map = {
            "h264_nvenc": {"short": "23", "long": "26"},
            "h264_qsv": {"short": "23", "long": "26"},
            "h264_videotoolbox": {"short": "23", "long": "26"},
        }
        crf = crf_map.get(encoder, {"short": "23", "long": "26"})["long" if duration > 600 else "short"]
        cmd = [
            FFMPEG,
            "-i",
            str(file_path),
            "-c:v",
            encoder,
            "-preset",
            "p5" if duration <= 600 else "p4",
            "-crf",
            crf,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
        if has_audio:
            cmd += ["-c:a", "aac"]
        cmd += ["-y", str(out)]
        return cmd

    @staticmethod
    async def _get_video_height(file_path: Path) -> int:
        out = await run_cmd(
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=height",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        )
        return int(out.strip()) if out and out.strip().isdigit() else 0

    def _build_sw_transcode_cmd(
        self, file_path: Path, out: Path, duration: float, height: int, has_audio: bool
    ) -> list[str]:
        if duration <= 30:
            preset, crf = "slow", "18"
        elif duration <= 60:
            preset, crf = "medium", "20"
        elif duration <= 600:
            preset, crf = "fast", "23"
        elif duration <= 1800:
            preset, crf = "veryfast", "26"
        else:
            preset, crf = "ultrafast", "28"

        scale = ["-vf", "scale=-2:720"] if duration > 1800 and height > 720 else []
        self.logger(f"SW 转码策略: preset={preset}, crf={crf}, scale={'720p' if scale else 'original'}")

        cmd = [
            FFMPEG,
            "-i",
            str(file_path),
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            crf,
            *scale,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
        if has_audio:
            cmd += ["-c:a", "aac"]
        cmd += ["-y", str(out)]
        return cmd

    async def split_video(
        self,
        file_path: Path,
        output_dir: Path,
        size_limit: int = TG_MAX_VIDEO_SIZE,
        ffmpeg_args: list[str] | None = None,
        keep_sec: float = 1.0,
    ) -> tuple[list[Path], Path]:
        if ffmpeg_args is None:
            ffmpeg_args = ["-c", "copy"]

        base = file_path.stem
        split_dir = output_dir / f"{base}_split"
        split_dir.mkdir(parents=True, exist_ok=True)
        # 分割产物统一为 .mp4（配合 faststart 保证流式），不沿用源后缀
        ext = "mp4"
        total_duration = int(await self.get_duration(file_path))
        self.logger(
            f"视频分割: duration={total_duration}s, "
            f"size_limit={size_limit / 1000 / 1000:.0f} MB ({size_limit / 1024 / 1024:.1f} MiB)"
        )

        cur, part, output_paths = 0, 1, []
        while cur < total_duration:
            out_file = split_dir / f"{base}_part_{part:03d}.{ext}"
            output_paths.append(out_file)
            cmd = [
                FFMPEG,
                "-ss",
                str(cur),
                "-i",
                str(file_path),
                "-fs",
                str(size_limit),
                *ffmpeg_args,
                "-y",
                str(out_file),
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

            new_dur = int(await self.get_duration(out_file))
            self.logger(f"分割 part {part}: offset={cur}s, duration={new_dur}s, file={out_file}")
            if new_dur <= 0:
                break
            cur += new_dur
            if cur < total_duration:
                cur = max(cur - int(keep_sec), 0)
            part += 1

        self.logger(f"视频分割完成: {len(output_paths)} 段")
        return output_paths, split_dir

    # ------------------------------------------------------------------ #
    #  工具方法
    # ------------------------------------------------------------------ #

    @staticmethod
    async def get_media_type_by_mime(file_path: str | Path) -> str:
        mime, _ = mimetypes.guess_type(str(file_path))
        if mime:
            if mime.startswith("image/"):
                return "image"
            if mime.startswith("video/"):
                return "video"
            if mime.startswith("audio/"):
                return "audio"
        # 扩展名未知（如 B 站 .unknown_video），用 ffprobe 内容嗅探兜底
        try:
            out = await run_cmd(
                FFPROBE,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(file_path),
            )
            if "video" in (out or ""):
                return "video"
        except Exception:
            pass
        return "unknown"


async def main() -> None:
    mpu = MediaProcessingUnit(
        output_dir=Path(r"/home/mio/Projects/personal/parse_hub_bot/downloads/与辉同行25年6月13日-一")
    )
    result = await mpu.process(
        r"/home/mio/Projects/personal/parse_hub_bot/downloads/与辉同行25年6月13日-一/与辉同行25年6月13日-一.mp4"
    )
    print(result.output_paths)


if __name__ == "__main__":
    asyncio.run(main())
