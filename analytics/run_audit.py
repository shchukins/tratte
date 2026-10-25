"""Read-only PostgreSQL audit. No application startup, credentials or file writes."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SQL_PATH = Path(__file__).with_name("audit.sql")


def parameters(as_of: str, timezone: str, base: str | None, current: str | None) -> dict:
    cutoff = datetime.fromisoformat(as_of)
    if cutoff.tzinfo is None:
        raise ValueError("as_of must include a UTC offset")
    this_month = cutoff.astimezone(ZoneInfo(timezone)).date().replace(day=1)
    current_month = date.fromisoformat(current) if current else (
        this_month - timedelta(days=1)
    ).replace(day=1)
    base_month = date.fromisoformat(base) if base else (
        current_month - timedelta(days=1)
    ).replace(day=1)
    if base_month.day != 1 or current_month.day != 1:
        raise ValueError("month parameters must be first-of-month dates")
    if base_month != (current_month - timedelta(days=1)).replace(day=1):
        raise ValueError("MVP requires adjacent months")
    if current_month >= this_month:
        raise ValueError("both months must be complete at as_of")
    return {
        "as_of": cutoff.isoformat(), "timezone": timezone,
        "base_month": base_month.isoformat(), "current_month": current_month.isoformat(),
    }


def transaction(query: str) -> str:
    return (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
        "SET LOCAL statement_timeout = '30s';\n"
        "SET LOCAL search_path = public, pg_catalog;\n"
        + query + "\nROLLBACK;\n"
    )


def command(params: dict, *, host: str | None, container: str | None,
            database: str, user: str) -> list[str]:
    cmd = ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", database]
    for key, value in params.items():
        cmd.extend(["-v", f"{key}={value}"])
    if container:
        cmd = ["docker", "exec", "-i", container, *cmd]
    if host:
        if host.startswith("-"):
            raise ValueError("SSH host must not be an option")
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, shlex.join(cmd)]
    return cmd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=datetime.now(UTC).isoformat())
    parser.add_argument("--timezone", default="Europe/Moscow")
    parser.add_argument("--base-month")
    parser.add_argument("--current-month")
    parser.add_argument("--ssh-host")
    parser.add_argument("--container")
    parser.add_argument("--database", default="tratte")
    parser.add_argument("--user", default="tratte")
    args = parser.parse_args()
    try:
        params = parameters(args.as_of, args.timezone, args.base_month, args.current_month)
        cmd = command(params, host=args.ssh_host, container=args.container,
                      database=args.database, user=args.user)
    except (ValueError, KeyError) as error:
        parser.error(str(error))
    result = subprocess.run(cmd, input=transaction(SQL_PATH.read_text()),
                            capture_output=True, text=True, timeout=60, check=True)
    # Validate the response, but retain PostgreSQL's decimal representation.
    json.loads(result.stdout)
    print(result.stdout.strip())


if __name__ == "__main__":
    main()
