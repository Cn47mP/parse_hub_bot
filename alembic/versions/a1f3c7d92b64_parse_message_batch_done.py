"""parse_message_batch_done

Revision ID: a1f3c7d92b64
Revises: e4b1c9d27a3f
Create Date: 2026-11-14 01:50:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1f3c7d92b64"
down_revision: str | Sequence[str] | None = "e4b1c9d27a3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None



def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # init_db 已通过 create_all 建表；此时不要重复创建。
    if inspector.has_table("parse_message_batch_done"):
        return

    op.create_table(
        "parse_message_batch_done",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("msg_key", sa.String(length=128), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("msg_key", "url", name="uq_parse_message_batch_msg_url"),
    )
    op.create_index(
        "ix_parse_message_batch_done_msg_key", "parse_message_batch_done", ["msg_key"], unique=False
    )



def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_parse_message_batch_done_msg_key", table_name="parse_message_batch_done")
    op.drop_table("parse_message_batch_done")