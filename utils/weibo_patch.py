"""Compatibility for non-media Weibo page cards in parsehub 2.4.2.

A status can contain both pictures and a page_info link card without
object_type/media_info. ParseHub otherwise raises KeyError before it gets
as far as parsing the pictures. Leave actual media cards to the original parser.
"""

from functools import wraps
from typing import Any

from parsehub.provider_api.weibo import MediaType, PageInfo

_patched = False


def apply() -> None:
    """Idempotently allow non-media cards without changing media parsing."""
    global _patched
    if _patched:
        return

    original_parse = PageInfo.parse.__func__
    known_types = {item.value for item in MediaType}

    @wraps(original_parse)
    def parse(cls: type[PageInfo], data: dict[str, Any]) -> PageInfo:
        if data.get("object_type") not in known_types and not data.get("media_info"):
            return cls(page_pic=data.get("page_pic"), short_url=data.get("short_url"))
        return original_parse(cls, data)

    PageInfo.parse = classmethod(parse)
    _patched = True
