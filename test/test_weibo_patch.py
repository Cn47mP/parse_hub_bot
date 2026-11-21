import asyncio
from copy import deepcopy

import pytest
from parsehub import ParseHub
from parsehub.provider_api.weibo import MediaType, PageInfo, WeiboAPI
from parsehub.types import ImageParseResult

from utils import weibo_patch


@pytest.fixture
def patched(monkeypatch):
    # Restore the exact descriptor and flag even when a test fails.
    monkeypatch.setattr(PageInfo, "parse", PageInfo.__dict__["parse"])
    monkeypatch.setattr(weibo_patch, "_patched", False)
    weibo_patch.apply()


def test_unpatched_parsehub_reproduces_missing_object_type():
    with pytest.raises(KeyError, match="object_type"):
        PageInfo.parse({"type": 2, "page_title": "Linked status"})


@pytest.mark.parametrize("object_type", [None, "unknown", ""])
def test_non_media_card(patched, object_type):
    card = {"type": 2, "page_pic": "https://example.com/card.jpg", "short_url": "https://t.cn/card"}
    if object_type is not None:
        card["object_type"] = object_type
    before = deepcopy(card)

    result = PageInfo.parse(card)

    assert result.object_type is None
    assert result.media_info is None
    assert result.page_pic == card["page_pic"]
    assert result.short_url == card["short_url"]
    assert card == before


def test_article_still_uses_original_parser(patched):
    result = PageInfo.parse({"object_type": "article", "page_pic": "https://example.com/article.jpg"})
    assert result.object_type == MediaType.ARTICLE
    assert result.media_info is None


def test_video_still_uses_original_parser(patched):
    result = PageInfo.parse(
        {
            "object_type": "video",
            "media_info": {
                "format": "mp4",
                "duration": 10,
                "prefetch_size": 0,
                "playback_list": [
                    {
                        "play_info": {
                            "url": "https://example.com/video.mp4",
                            "width": 1920,
                            "height": 1080,
                            "duration": 10,
                        }
                    }
                ],
            },
        }
    )
    assert result.object_type == MediaType.VIDEO
    assert result.media_url == "https://example.com/video.mp4"
    assert (result.width, result.height, result.duration) == (1920, 1080, 10)


def test_unknown_card_with_media_is_not_silently_discarded(patched):
    with pytest.raises(ValueError):
        PageInfo.parse({"object_type": "unknown", "media_info": {"duration": 10}})


def test_malformed_video_is_not_silently_discarded(patched):
    with pytest.raises(KeyError):
        PageInfo.parse({"object_type": "video"})


def test_apply_is_idempotent(patched):
    descriptor = PageInfo.__dict__["parse"]
    weibo_patch.apply()
    assert PageInfo.__dict__["parse"] is descriptor


def test_status_with_card_preserves_all_four_images(patched, monkeypatch):
    # Minimal synthetic shape of the failing status; no cookies or user data.
    raw = {
        "id": "123",
        "text_raw": "Four images https://t.cn/card",
        "page_info": {"type": 2, "page_title": "Linked status", "short_url": "https://t.cn/card"},
        "pic_infos": {
            str(i): {
                "pic_id": str(i),
                "type": "pic",
                "thumbnail": {"url": f"https://example.com/thumb-{i}.jpg"},
                "largest": {"url": f"https://example.com/image-{i}.jpg", "width": 2048, "height": 2227},
            }
            for i in range(4)
        },
    }

    async def statuses_show(self, bid):
        return deepcopy(raw)

    monkeypatch.setattr(WeiboAPI, "statuses_show", statuses_show)
    result = asyncio.run(ParseHub().parse("https://weibo.com/1234567890/Abcdef123"))

    assert isinstance(result, ImageParseResult)
    assert len(result.media) == 4
    assert [image.url for image in result.media] == [f"https://example.com/image-{i}.jpg" for i in range(4)]
    assert all((image.width, image.height) == (2048, 2227) for image in result.media)
    assert "Four images" in result.content
    assert "https://t.cn/card" not in result.content
