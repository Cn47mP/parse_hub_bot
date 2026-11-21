from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class ParseMessageBatchDone(Base):
    """同一条消息内某链接已完成（用于多链接消息统一删除原消息的批次完成判定）。"""

    __tablename__ = "parse_message_batch_done"
    __table_args__ = (UniqueConstraint("msg_key", "url", name="uq_parse_message_batch_msg_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    msg_key: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )