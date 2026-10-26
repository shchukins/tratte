"""Add isolated, immutable analytical identity snapshots.

Source rows may be deleted by reprocess: preserve historical IDs and null the live FK.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_analytical_identity"
down_revision = "0002_weekly_summary_deliveries"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analytics_products",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("specification", sa.Text(), nullable=False),
        sa.Column("comparison_unit", sa.String(10), nullable=False),
        sa.CheckConstraint("comparison_unit IN ('kg', 'l', 'piece')", name="ck_product_unit"),
    )
    op.create_table(
        "analytics_mapping_versions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("rules_version", sa.String(64), nullable=False),
        sa.Column("manifest", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "analytics_item_mappings",
        sa.Column(
            "mapping_version_id",
            sa.String(64),
            sa.ForeignKey("analytics_mapping_versions.id"),
            primary_key=True,
        ),
        sa.Column("item_id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_item_id", sa.Integer(), sa.ForeignKey("receipt_items.id", ondelete="SET NULL")
        ),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("product_id", sa.String(64), sa.ForeignKey("analytics_products.id")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("quantity_basis", sa.String(20)),
        sa.Column("factor", sa.Numeric(18, 9)),
        sa.CheckConstraint(
            "status IN ('confirmed', 'rejected', 'unresolved')", name="ck_mapping_status"
        ),
        sa.CheckConstraint(
            "(status = 'confirmed' AND product_id IS NOT NULL "
            "AND factor IS NOT NULL AND factor > 0 "
            "AND quantity_basis IS NOT NULL AND quantity_basis IN "
            "('source_weight', 'source_volume', 'package_count', 'piece_count')) "
            "OR (status != 'confirmed' AND product_id IS NULL AND factor IS NULL "
            "AND quantity_basis IS NULL)",
            name="ck_mapping_decision",
        ),
        sa.CheckConstraint(
            "source_item_id IS NULL OR source_item_id = item_id", name="ck_mapping_item"
        ),
    )
    op.create_index(
        "ix_analytics_item_mappings_source_item_id", "analytics_item_mappings", ["source_item_id"]
    )
    # Enforce immutable snapshots against accidental SQL writes too. A source FK
    # clearing during reprocess is the sole permitted update to a mapping.
    dialect = op.get_bind().dialect.name
    fields = (
        "mapping_version_id",
        "item_id",
        "source_fingerprint",
        "product_id",
        "status",
        "source",
        "note",
        "quantity_basis",
        "factor",
    )
    if dialect == "postgresql":
        same = " AND ".join(f"NEW.{f} IS NOT DISTINCT FROM OLD.{f}" for f in fields)
        op.execute(f"""
            CREATE FUNCTION analytics_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_TABLE_NAME = 'analytics_item_mappings' AND TG_OP = 'UPDATE' THEN
                    IF NEW.source_item_id IS NULL AND OLD.source_item_id IS NOT NULL
                       AND {same} THEN RETURN NEW; END IF;
                END IF;
                RAISE EXCEPTION 'Analytical snapshots are immutable; create a new version';
            END; $$
        """)
        for table in (
            "analytics_products",
            "analytics_mapping_versions",
            "analytics_item_mappings",
        ):
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION analytics_immutable()"
            )
    elif dialect == "sqlite":
        same = " AND ".join(f"NEW.{f} IS OLD.{f}" for f in fields)
        for table in (
            "analytics_products",
            "analytics_mapping_versions",
            "analytics_item_mappings",
        ):
            for action in ("UPDATE", "DELETE"):
                condition = ""
                if table == "analytics_item_mappings" and action == "UPDATE":
                    condition = (
                        "WHEN NOT (NEW.source_item_id IS NULL AND "
                        f"OLD.source_item_id IS NOT NULL AND {same})"
                    )
                op.execute(
                    f"CREATE TRIGGER {table}_{action.lower()} BEFORE {action} ON {table} "
                    f"{condition} BEGIN SELECT RAISE(ABORT, 'Immutable analytics'); END"
                )


def downgrade():
    for table in ("analytics_item_mappings", "analytics_mapping_versions", "analytics_products"):
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION analytics_immutable()")
