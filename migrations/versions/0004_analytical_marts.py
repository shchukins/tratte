"""Ordinary privacy-scoped PostgreSQL views; no catalog application/backfill."""

from alembic import op

from app.services.analytical_marts import sql_file

revision = "0004_analytical_marts"
down_revision = "0003_analytical_identity"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sql_file("views_v1.sql"))


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for name in ("quality", "reconciliation", "items", "receipts"):
            op.execute(f"DROP VIEW analytics_{name}_v1")
