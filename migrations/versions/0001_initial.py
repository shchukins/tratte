"""initial schema"""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), unique=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "gmail_integrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("encrypted_token", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gmail_message_id", sa.String(255), nullable=False),
        sa.Column("ofd_provider", sa.String(100)),
        sa.Column("seller", sa.String(255)),
        sa.Column("store", sa.String(100)),
        sa.Column("inn", sa.String(20)),
        sa.Column("location", sa.Text()),
        sa.Column("purchased_at", sa.DateTime(timezone=True)),
        sa.Column("operation_type", sa.String(40)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("total", sa.Numeric(12, 2)),
        sa.Column("payment_method", sa.String(100)),
        sa.Column("fiscal_drive_number", sa.String(64)),
        sa.Column("fiscal_document_number", sa.String(64)),
        sa.Column("fiscal_sign", sa.String(64)),
        sa.Column("fiscal_fingerprint", sa.String(220)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("parse_error", sa.Text()),
        sa.Column("source_sender", sa.String(320)),
        sa.Column("source_subject", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("fiscal_fingerprint", name="uq_receipt_fiscal"),
    )
    op.create_index("ix_receipts_gmail_message_id", "receipts", ["gmail_message_id"], unique=True)
    op.create_index("ix_receipts_purchased_at", "receipts", ["purchased_at"])
    op.create_index("ix_receipts_store", "receipts", ["store"])
    op.create_table(
        "receipt_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "receipt_id",
            sa.Integer(),
            sa.ForeignKey("receipts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit", sa.String(20)),
        sa.Column("total", sa.Numeric(12, 2), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("vat_rate", sa.String(30)),
    )
    op.create_index("ix_receipt_items_receipt_id", "receipt_items", ["receipt_id"])
    op.create_index("ix_receipt_items_normalized_name", "receipt_items", ["normalized_name"])
    op.create_table(
        "product_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alias", sa.Text(), nullable=False, unique=True),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("messages_seen", sa.Integer(), nullable=False),
        sa.Column("parsed", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
    )


def downgrade() -> None:
    op.drop_table("sync_runs")
    op.drop_table("product_aliases")
    op.drop_table("receipt_items")
    op.drop_table("receipts")
    op.drop_table("gmail_integrations")
    op.drop_table("users")
