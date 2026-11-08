"""parse_queue_done

Revision ID: e4b1c9d27a3f
Revises: 765f26cfd84a
Create Date: 2026-11-03 03:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4b1c9d27a3f"
down_revision: str | Sequence[str] | None = "765f26cfd84a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # init_db 已通过 create_all 建表；此时不要重复创建。
    if inspector.has_table("parse_queue_done"):
        return

    op.create_table(
        "parse_queue_done",
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("url"),
    )
    op.create_index(
        "ix_parse_queue_done_finished_at", "parse_queue_done", ["finished_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_parse_queue_done_finished_at", table_name="parse_queue_done")
    op.drop_table("parse_queue_done")
