"""Publish a coherent validated calculation to an isolated BI database, atomically."""

from hashlib import sha256

from sqlalchemy import Engine, text

from app.services.analytical_marts import calculate


def publish(source: Engine, destination: Engine, **parameters) -> str:
    # Calculate first: a stale mapping cannot replace the last published snapshot.
    document = calculate(source, dashboard=True, **parameters)
    snapshot_id = sha256(document.encode()).hexdigest()
    if destination.dialect.name != "postgresql":
        raise ValueError("Dashboard destination requires PostgreSQL")
    with destination.begin() as connection:
        connection.execute(text("SET LOCAL statement_timeout = '30s'"))
        connection.execute(text("""
            INSERT INTO tratte_bi.snapshot_v1(snapshot_id, document)
            VALUES (:snapshot_id, CAST(:document AS jsonb))
            ON CONFLICT (snapshot_id) DO NOTHING
        """), {"snapshot_id": snapshot_id, "document": document})
    return snapshot_id
