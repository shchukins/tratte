"""PostgreSQL SQL consumer with a mandatory coherent mapping validation gate."""

from datetime import datetime
from hashlib import sha256
from importlib.resources import files

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from analytics.run_audit import parameters
from app.services.analytical_identity import validate_version


def sql_file(name: str) -> str:
    return files("analytics").joinpath("sql", name).read_text(encoding="utf-8")


def calculate(
    engine: Engine, *, as_of: datetime, base_month: str | None = None,
    current_month: str | None = None, mapping_version: str | None = None,
    store: str | None = None, category: str | None = None, decomposition: bool = False,
    dashboard: bool = False,
) -> str:
    """Return private JSON text, preserving PostgreSQL numeric precision.

    No mapping means descriptive-only data. An explicit stale/unknown mapping
    fails the whole calculation; it never silently degrades to unmatched.
    """
    if category is not None and not (decomposition or dashboard):
        raise ValueError("Category scope requires decomposition")
    params = parameters(as_of.isoformat(), "Europe/Moscow", base_month, current_month)
    if engine.dialect.name != "postgresql":
        raise ValueError("Analytical marts require PostgreSQL")
    query = sql_file("comparison_v1.sql")
    params.update(
        mapping_version=mapping_version, store=store,
        comparison_sha256=sha256(query.encode()).hexdigest(),
        views_sha256=sha256(sql_file("views_v1.sql").encode()).hexdigest(),
    )
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout = '30s'"))
            with Session(bind=connection) as session:
                if mapping_version is not None:
                    validate_version(session, mapping_version)
                document = session.scalar(text(query), params)
                if not (decomposition or dashboard):
                    return document
                factor_sql = sql_file("decomposition_v1.sql")
                factors = session.scalar(text(factor_sql), {
                    "document": document, "category": category,
                    "decomposition_sha256": sha256(factor_sql.encode()).hexdigest(),
                })
                if not dashboard:
                    return factors
                dashboard_sql = sql_file("dashboard_v1.sql")
                return session.scalar(text(dashboard_sql), {
                    "marts": document, "factors": factors, "category": category,
                    "dashboard_sha": sha256(dashboard_sql.encode()).hexdigest(),
                })
