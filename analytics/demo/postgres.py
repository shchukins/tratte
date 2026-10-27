"""Load only a new empty disposable database, publish and export the checked snapshot."""

import json
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from analytics.demo.load import seed
from analytics.demo.validate import check_report
from analytics.superset.bundle import build
from analytics.superset.demo import AS_OF
from app.config import get_settings
from app.services.analytical_dashboard import publish
from app.services.analytical_marts import calculate


def main():
    url = os.environ["TRATTE_DEMO_DATABASE_URL"]
    engine = create_engine(url)
    if engine.dialect.name != "postgresql":
        raise ValueError("Disposable PostgreSQL required")
    with engine.connect() as connection:
        if set(inspect(connection).get_schema_names()) - {"information_schema", "public"}:
            raise ValueError("Demo requires a new disposable database without custom schemas")
        if inspect(connection).get_table_names():
            raise ValueError("Demo requires a new empty disposable database")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    version = seed(engine)
    report = json.loads(calculate(engine, as_of=AS_OF, mapping_version=version, dashboard=True))
    check_report(report)
    with engine.begin() as connection:
        connection.execute(text(Path("analytics/superset/snapshot.sql").read_text()))
    snapshot = publish(engine, engine, mapping_version=version, as_of=AS_OF)
    out = Path("output/demo")
    out.mkdir(parents=True, exist_ok=True)
    build(out / "superset.zip", snapshot)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Synthetic snapshot: {snapshot}; output/demo (no credentials)")


if __name__ == "__main__":
    main()
