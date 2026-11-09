"""parsehub 抖音图文/实况照片清晰度修复。

parsehub 2.3.17 的 DouyinApiResult._parse_images 在处理"实况照片"时,
把 LivePhotoRef.url 取成 video_info["thumb_url"]（即视频封面, 通常只有 540x720）,
而 API 返回的 image["url_list"] 里其实有原图(如 1440x1920 q88)。

参考 telegram-bili-feed-helper 的做法: 优先使用图片自身的 url_list（挑 jpeg 变体）,
封面仅作为兜底。
"""

from __future__ import annotations

from typing import Any

from parsehub.parsers.parser.douyin import DouyinApiResult, DouyinMediaType, parse_video_info
from parsehub.types import ImageRef, LivePhotoRef

from log import logger

logger = logger.bind(name="DouyinPatch")

_patched = False


def _pick_image_url(image: dict[str, Any]) -> str:
    """从 url_list 里挑最优图片地址, 优先 jpeg（部分作品首个是 heic）。"""
    urls = [u for u in (image.get("url_list") or []) if u]
    for u in urls:
        low = u.lower()
        if ".jpeg" in low or ".jpg" in low:
            return u
    return urls[-1] if urls else ""


def _parse_images_hires(cls: type[DouyinApiResult], images: list[dict], desc: str) -> DouyinApiResult:
    """同 _parse_images, 但实况照片使用原图地址与图片自身宽高。"""
    has_live_photos = any(img.get("video") for img in images)
    image_list: list[ImageRef | LivePhotoRef] = []

    if has_live_photos:
        for image in images:
            hi_res = _pick_image_url(image)
            if video := image.get("video"):
                video_info = parse_video_info(video)
                image_list.append(
                    LivePhotoRef(
                        url=hi_res or video_info["thumb_url"] or "",
                        video_url=video_info["video_url"],
                        width=int(image.get("width") or video_info["width"] or 0),
                        height=int(image.get("height") or video_info["height"] or 0),
                        duration=int(video_info["duration"]) or 3,
                    )
                )
            elif hi_res:
                image_list.append(
                    ImageRef(
                        url=hi_res,
                        height=image.get("height", 0),
                        width=image.get("width", 0),
                    )
                )
    else:
        image_list = [
            ImageRef(
                url=url,
                height=img.get("height", 0),
                width=img.get("width", 0),
            )
            for img in images
            if (url := _pick_image_url(img))
        ]

    return cls(type=DouyinMediaType.IMAGE, image_list=image_list, desc=desc)


def apply() -> None:
    """幂等: 替换 DouyinApiResult._parse_images。"""
    global _patched
    if _patched:
        return
    DouyinApiResult._parse_images = classmethod(_parse_images_hires)  # type: ignore[method-assign]
    _patched = True
    logger.info("已启用抖音图文清晰度修复 (实况照片使用原图 url_list)")
