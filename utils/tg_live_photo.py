"""Telegram 实况照片(live photo)发送。

pyrogram 2.x 自带的 InputMediaLivePhoto.write 在"本地文件"分支里把
InputMediaUploadedPhoto 的 video 字段传成了 InputFile（该字段类型是
InputDocument），服务器回 400 FILE_REFERENCE_COVER_EXPIRED。

这里按官方客户端的做法来：
1. messages.UploadMedia 先上传动的那段视频，拿到 MessageMediaDocument →
   转成 InputDocument（带 access_hash / file_reference）；
2. messages.SendMedia / SendMultiMedia 发送
   InputMediaUploadedPhoto(file=静态照片, video=该 InputDocument, live_photo=True)。

相册(multi)另有一条硬性要求：SendMultiMedia 里的 media 必须是**已上传的引用**，
直接塞 InputMediaUploadedPhoto(file=...) 会回 400 MEDIA_INVALID。所以多张时
照片也要先 UploadMedia 拿到 InputPhoto，再用 InputMediaPhoto(id=..., video=..., live_photo=True)。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pyrogram import Client, raw, utils

from log import logger

logger = logger.bind(name="LivePhoto")


async def _upload_photo(client: Client, peer: raw.base.InputPeer, path: str | Path) -> raw.types.InputPhoto:
    uploaded = await client.invoke(
        raw.functions.messages.UploadMedia(
            peer=peer,
            media=raw.types.InputMediaUploadedPhoto(file=await client.save_file(str(path))),
        )
    )
    photo = uploaded.photo
    return raw.types.InputPhoto(
        id=photo.id,
        access_hash=photo.access_hash,
        file_reference=photo.file_reference,
    )


async def _upload_video(
    client: Client,
    peer: raw.base.InputPeer,
    path: str | Path,
    *,
    width: int = 0,
    height: int = 0,
    duration: int = 3,
) -> raw.types.InputDocument:
    uploaded = await client.invoke(
        raw.functions.messages.UploadMedia(
            peer=peer,
            media=raw.types.InputMediaUploadedDocument(
                mime_type="video/mp4",
                file=await client.save_file(str(path)),
                attributes=[
                    raw.types.DocumentAttributeVideo(
                        duration=max(1, min(10, int(duration or 3))),
                        w=int(width or 0),
                        h=int(height or 0),
                        supports_streaming=True,
                    )
                ],
            ),
        )
    )
    doc = uploaded.document
    return raw.types.InputDocument(id=doc.id, access_hash=doc.access_hash, file_reference=doc.file_reference)


async def _build_media(
    client: Client,
    peer: raw.base.InputPeer,
    photo: str | Path,
    video: str | Path,
    *,
    width: int = 0,
    height: int = 0,
    duration: int = 3,
) -> raw.types.InputMediaUploadedPhoto:
    doc = await _upload_video(client, peer, video, width=width, height=height, duration=duration)
    return raw.types.InputMediaUploadedPhoto(
        file=await client.save_file(str(photo)),
        video=doc,
        live_photo=True,
    )


async def _build_album_media(
    client: Client,
    peer: raw.base.InputPeer,
    photo: str | Path,
    video: str | Path | None,
    *,
    width: int = 0,
    height: int = 0,
    duration: int = 3,
) -> raw.types.InputMediaPhoto:
    photo_ref = await _upload_photo(client, peer, photo)
    if not video:
        return raw.types.InputMediaPhoto(id=photo_ref)
    doc = await _upload_video(client, peer, video, width=width, height=height, duration=duration)
    return raw.types.InputMediaPhoto(id=photo_ref, video=doc, live_photo=True)


def _duration_seconds(duration_ms: int | None) -> int:
    if not duration_ms:
        return 3
    return max(1, min(10, round(int(duration_ms) / 1000)))


async def send_live_photos(
    client: Client,
    chat_id: int | str,
    items: Sequence[tuple[Path, Path | None, int, int, int]],
    *,
    caption: str = "",
) -> list[int]:
    """items: (静态图路径, 动图视频路径或 None, width, height, duration_ms)；返回消息 id 列表。"""
    peer = await client.resolve_peer(chat_id)
    # caption 是 build_caption 产出的 markdown/HTML 混排文本, 必须过一遍解析器
    # 换成纯文本 + entities, 否则频道里会直接显示 ** 和 <a href> 这些标记
    parsed = await utils.parse_text_entities(client, caption or "", None, None)
    text = parsed["message"]
    entities = parsed["entities"]

    if len(items) == 1:
        photo, video, width, height, duration = items[0]
        media = await _build_media(
            client, peer, photo, video, width=width, height=height, duration=_duration_seconds(duration)
        )
        result = await client.invoke(
            raw.functions.messages.SendMedia(
                peer=peer,
                media=media,
                message=text,
                entities=entities,
                random_id=client.rnd_id(),
            )
        )
    else:
        multi: list[raw.types.InputSingleMedia] = []
        for index, (photo, video, width, height, duration) in enumerate(items):
            media = await _build_album_media(
                client, peer, photo, video, width=width, height=height, duration=_duration_seconds(duration)
            )
            multi.append(
                raw.types.InputSingleMedia(
                    media=media,
                    random_id=client.rnd_id(),
                    message=text if index == 0 else "",
                    entities=entities if index == 0 else None,
                )
            )
        result = await client.invoke(
            raw.functions.messages.SendMultiMedia(peer=peer, multi_media=multi)
        )

    ids: list[int] = []
    for update in result.updates:
        message = getattr(update, "message", None)
        if message is not None and getattr(message, "id", None):
            ids.append(message.id)
    logger.info(f"实况照片发送完成: {len(ids)} 条")
    return ids
