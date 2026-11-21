from pyrogram.file_id import FileId, FileType
from pyrogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo, Message

from services import CacheMedia, CacheMediaType


def is_photo_file_id(file_id: str | None) -> bool:
    """判断 file_id 是否为 Photo 类型。

    video_cover= 只接受 Photo file_id, 传 Thumbnail 会报
    "Expected PHOTO, got THUMBNAIL file id instead"。历史缓存可能残留 Thumbnail,
    因此重发前必须校验; 解析失败按不可用处理(退回 TG 抽帧)。
    """
    if not file_id:
        return False
    try:
        return FileId.decode(file_id).file_type == FileType.PHOTO
    except Exception:
        return False


def safe_cover_file_id(file_id: str | None) -> str | None:
    """返回可安全用于 video_cover= 的 file_id, 不可用则 None。"""
    return file_id if is_photo_file_id(file_id) else None


def _cover_file_id(v) -> str | None:
    """取视频封面的 file_id —— 必须是 Photo, 否则返回 None。

    缓存重发走 video_cover=, pyrogram 内部用 FileType.PHOTO 解析该 file_id
    (send_video.py:348 / input_media_video.py:183), 传 Thumbnail 会抛
    "Expected PHOTO, got THUMBNAIL file id instead"。

    本项目上传封面用的是 thumb=, 落在 v.thumbs(Thumbnail 类型), 因此**不能**用它当封面;
    v.video_cover 才是真正的 InputPhoto 字段, 只有它可用。取不到就让 Telegram 自动抽首帧,
    与原设计「封面不可用时退回 TG 抽帧」一致。
    """
    photo = getattr(v, "video_cover", None)
    if photo and getattr(photo, "file_id", None):
        return photo.file_id
    return None


def cache_media_from_message(m: Message) -> CacheMedia | None:
    """从已发送的 Telegram Message 提取 CacheMedia。"""
    if m.photo:
        return CacheMedia(type=CacheMediaType.PHOTO, file_id=m.photo.file_id)
    if m.video:
        return CacheMedia(
            type=CacheMediaType.VIDEO,
            file_id=m.video.file_id,
            cover_file_id=_cover_file_id(m.video),
            width=m.video.width or 0,
            height=m.video.height or 0,
            duration=int(m.video.duration or 0),
        )
    if m.animation:
        return CacheMedia(type=CacheMediaType.ANIMATION, file_id=m.animation.file_id)
    if m.audio:
        return CacheMedia(type=CacheMediaType.AUDIO, file_id=m.audio.file_id)
    if m.document:
        return CacheMedia(type=CacheMediaType.DOCUMENT, file_id=m.document.file_id)
    return None


def cache_media_from_rich_message(m: Message) -> list[CacheMedia]:
    """从已发送的富文本 Message 的块中递归提取 CacheMedia。"""
    media: list[CacheMedia] = []

    def walk(blocks: list) -> None:
        for b in blocks or []:
            if getattr(b, "photo", None):
                media.append(CacheMedia(type=CacheMediaType.PHOTO, file_id=b.photo.file_id))
            elif getattr(b, "video", None):
                media.append(
                    CacheMedia(
                        type=CacheMediaType.VIDEO,
                        file_id=b.video.file_id,
                        cover_file_id=_cover_file_id(b.video),
                        width=b.video.width or 0,
                        height=b.video.height or 0,
                        duration=int(b.video.duration or 0),
                    )
                )
            elif getattr(b, "animation", None):
                media.append(CacheMedia(type=CacheMediaType.ANIMATION, file_id=b.animation.file_id))
            elif getattr(b, "document", None):
                media.append(CacheMedia(type=CacheMediaType.DOCUMENT, file_id=b.document.file_id))
            if getattr(b, "blocks", None):
                walk(b.blocks)

    if m.rich_message:
        walk(m.rich_message.blocks)
    return media


def build_cached_media_group(
    media: list[CacheMedia], *, video_cover: bool
) -> list[InputMediaPhoto | InputMediaVideo | InputMediaDocument]:
    """从 CacheMedia 列表构建 Telegram media group。"""
    group: list[InputMediaPhoto | InputMediaVideo | InputMediaDocument] = []
    for m in media:
        match m.type:
            case CacheMediaType.PHOTO:
                group.append(InputMediaPhoto(media=m.file_id))
            case CacheMediaType.VIDEO:
                dims = dict(
                    width=m.width or None,
                    height=m.height or None,
                    duration=m.duration or None,
                )
                cover = safe_cover_file_id(m.cover_file_id) if video_cover else None
                if cover:
                    group.append(
                        InputMediaVideo(
                            media=m.file_id,
                            supports_streaming=True,
                            video_cover=cover,
                            **dims,
                        )
                    )
                else:
                    group.append(
                        InputMediaVideo(media=m.file_id, supports_streaming=True, **dims)
                    )
            case CacheMediaType.DOCUMENT:
                group.append(InputMediaDocument(media=m.file_id))
    return group
