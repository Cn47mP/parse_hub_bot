from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class ParseQueueDone(Base):
    """已完成解析的 URL 记录, 用于重启后断点续跑时跳过"""

    __tablename__ = "parse_queue_done"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    """用户原始分享链接(req.url), 不是 raw_url"""
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
