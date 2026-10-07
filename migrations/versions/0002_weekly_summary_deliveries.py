"""Track successful weekly Telegram summaries."""

import sqlalchemy as sa
from alembic import op

revision = "0002_weekly_summary_deliveries"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weekly_summary_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("telegram_user_id", "week_start", name="uq_weekly_summary_user_week"),
    )


def downgrade() -> None:
    op.drop_table("weekly_summary_deliveries")
