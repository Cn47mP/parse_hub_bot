"""parsehub B 站解析风控修复。

parsehub 2.3.17 / 2.4.0 的 BiliAPI.get_video_info 请求
https://api.bilibili.com/x/web-interface/view/detail 时未带 buvid3 cookie,
B 站对无匿名指纹的请求返回 HTTP 412 (安全风控), 导致 API 与 yt-dlp 两条回退全失败。

参考 telegram-bili-feed-helper (simonsmh) 的做法: 请求前现取一枚匿名 buvid3/buvid4
指纹并随请求带上, 即可正常通过。finger/spi 接口本身不需要 cookie。
"""

from __future__ import annotations

from typing import Any

from log import logger
from parsehub.provider_api.bilibili import BiliAPI

logger = logger.bind(name="BiliPatch")

_patched = False


async def _get_video_info_with_buvid(self: BiliAPI, url: str) -> dict[str, Any]:
    """带 buvid3/buvid4 指纹请求视频详情, 绕过 B 站 412 风控。"""
    bvid = self.get_bvid(url)
    if not bvid:
        raise ValueError(f"Invalid url: {url}")

    b3, b4 = await self.get_buvid()
    response = await self._get_client().get(
        "https://api.bilibili.com/x/web-interface/view/detail",
        params={"bvid": bvid},
        cookies={"buvid3": b3, "buvid4": b4},
    )
    if response.status_code == 412:
        raise Exception("由于触发哔哩哔哩安全风控策略，该次访问请求被拒绝。")
    response.raise_for_status()
    return response.json()


def apply() -> None:
    """幂等: 给 BiliAPI.get_video_info 注入 buvid3 指纹。"""
    global _patched
    if _patched:
        return
    BiliAPI.get_video_info = _get_video_info_with_buvid  # type: ignore[method-assign]
    _patched = True
    logger.info("已启用 B 站解析风控修复 (get_video_info 携带 buvid3 指纹)")
