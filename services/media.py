import io
from dataclasses import dataclass
from pathlib import Path

import httpx
from easy_ai18n import PreLocaleSelector
from parsehub.types import AnyMediaFile, DownloadResult, ProgressUnit
from parsehub.utils.media_info import MediaInfoReader
from PIL import Image

from log import logger
from utils.helpers import to_list
from utils.media_processing_unit import MediaProcessingUnit
from core import bs


@dataclass
class ProcessedMedia:
    source: AnyMediaFile
    output_paths: list[Path] | None = None
    output_dir: Path | None = None


def crop_to_aspect(img: Image.Image, target_ratio: float) -> Image.Image:
    """按目标长宽比中心裁切(不缩放), 避免 TG 把封面拉伸铺满视频框。

    target_ratio = width / height。偏差小于 2% 视为一致, 原样返回。
    """
    if target_ratio <= 0:
        return img
    cur = img.width / img.height
    if abs(cur - target_ratio) / target_ratio < 0.02:
        return img
    if cur > target_ratio:
        # 太宽 -> 裁左右
        new_w = int(round(img.height * target_ratio))
        if new_w >= img.width:
            return img
        left = (img.width - new_w) // 2
        return img.crop((left, 0, left + new_w, img.height))
    # 太高 -> 裁上下
    new_h = int(round(img.width / target_ratio))
    if new_h >= img.height:
        return img
    top = (img.height - new_h) // 2
    return img.crop((0, top, img.width, top + new_h))


def make_thumb(
    src: bytes | str | Path,
    out_path: str | Path,
    *,
    aspect: float | None = None,
) -> str | None:
    """压到 TG thumb 规格(≤320px JPEG), 失败返回 None。

    aspect 给定时(w/h), 先按该比例中心裁切再缩放, 使封面与视频同比例。
    """
    try:
        img = Image.open(io.BytesIO(src) if isinstance(src, bytes) else src).convert("RGB")
        if aspect:
            img = crop_to_aspect(img, aspect)
        img.thumbnail((320, 320))
        img.save(out_path, "JPEG", quality=85)
        return str(out_path)
    except Exception as e:
        logger.debug(f"缩略图处理失败: {e}")
        return None


async def fetch_thumb(
    url: str,
    out_path: str | Path,
    *,
    aspect: float | None = None,
) -> str | None:
    """下载缩略图并压到 TG thumb 规格, 失败返回 None。aspect 同 make_thumb。"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        return make_thumb(resp.content, out_path, aspect=aspect)
    except Exception as e:
        logger.warning(f"缩略图获取失败, 跳过({url}): {e}")
        return None


def resolve_media_info(processed: ProcessedMedia, file_path: str) -> tuple[int, int, int]:
    """获取媒体的宽、高、时长。若经过转码则从文件读取，否则使用源信息。"""
    if processed.output_paths:
        info = MediaInfoReader.read(file_path)
        return info.width, info.height, info.duration
    return processed.source.width, processed.source.height, getattr(processed.source, "duration", 0)


def progress(current: int, total: int, unit: ProgressUnit, _t: PreLocaleSelector) -> str | None:
    if unit == "bytes":
        if total <= 0:
            return None

        text = _t(f"下 载 中... | {current * 100 / total:.0f}%")
        if round(current * 100 / total, 1) % 25 == 0:
            return str(text)
    else:
        text = _t(f"下 载 中... | {current}/{total}")
        if (current + 1) % 3 == 0 or (current + 1) == total:
            return str(text)
    return None


async def process_media_files(download_result: DownloadResult) -> list[ProcessedMedia]:
    """对下载结果中的媒体文件进行处理，返回 ProcessedMedia 列表"""
    processed_dir = download_result.output_dir.joinpath("processed")
    processor = MediaProcessingUnit(
        processed_dir,
        segment_height=1920,
        logger=logger.bind(name="MediaProcessor").info,
        autoplay_safe_transcode=bs.autoplay_safe_transcode,
        skip_max_duration=bs.autoplay_skip_transcode_max_duration,
        skip_max_size=bs.autoplay_skip_transcode_max_size,
    )
    media_files = to_list(download_result.media)
    logger.debug(f"开始媒体处理: 文件数={len(media_files)}, output_dir={processed_dir}")
    processed_list: list[ProcessedMedia] = []
    for media_file in media_files:
        # 对于实况图片只处理图片, 不处理视频
        logger.debug(f"处理文件: {media_file.path}")
        result = await processor.process(media_file.path)
        logger.debug(f"处理结果: output_paths={result.output_paths}")
        processed_list.append(ProcessedMedia(media_file, result.output_paths, result.temp_dir))
    logger.debug(f"媒体处理完成: 处理数={len(processed_list)}")
    return processed_list
