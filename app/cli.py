from __future__ import annotations

import logging
from datetime import date
from typing import Annotated

import typer
import uvicorn
from sqlalchemy import select

from app.bot import run_bot
from app.config import get_settings
from app.db import SessionLocal
from app.models import Receipt
from app.scheduler import run_scheduler
from app.services.gmail import GmailIntegrationService
from app.services.normalization import ProductNormalizer
from app.services.secrets import FernetSecretStorage
from app.services.sync import retry_failed, sync_receipts

cli = typer.Typer(no_args_is_help=True)


def _logging() -> None:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # python-telegram-bot uses URLs containing the bot token. httpx logs the
    # complete request URL at INFO, so it must never inherit application INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@cli.command("generate-key")
def generate_key() -> None:
    """Generate a Fernet key for TOKEN_ENCRYPTION_KEY."""
    typer.echo(FernetSecretStorage.generate_key())


@cli.command("gmail-auth")
def gmail_auth(
    host: str = typer.Option("localhost"),
    bind_address: str | None = typer.Option(None),
    port: int = typer.Option(0),
    open_browser: bool = typer.Option(True, "--open-browser/--no-open-browser"),
) -> None:
    """Connect one Gmail account using the read-only OAuth scope."""
    settings = get_settings()
    with SessionLocal() as session:
        integration = GmailIntegrationService(
            session, settings, FernetSecretStorage(settings.token_encryption_key)
        ).authorize_interactively(
            host=host,
            bind_address=bind_address,
            port=port,
            open_browser=open_browser,
        )
    typer.echo(f"Gmail подключён: {integration.email}")


@cli.command("import-receipts")
def import_receipts(since: Annotated[str, typer.Option(help="Дата YYYY-MM-DD")]) -> None:
    """Initial import from the configured Gmail label/senders."""
    try:
        since_date = date.fromisoformat(since)
    except ValueError as exc:
        raise typer.BadParameter("Ожидается дата в формате YYYY-MM-DD") from exc
    run = sync_receipts(since_date)
    typer.echo(f"Разобрано: {run.parsed}; пропущено: {run.skipped}; ошибок: {run.failed}")


@cli.command("sync")
def sync() -> None:
    run = sync_receipts()
    typer.echo(f"Разобрано: {run.parsed}; пропущено: {run.skipped}; ошибок: {run.failed}")


@cli.command("retry-failed")
def retry() -> None:
    run = retry_failed()
    typer.echo(f"Разобрано: {run.parsed}; пропущено: {run.skipped}; ошибок: {run.failed}")


@cli.command("set-alias")
def set_alias(alias: str, name: str, category: str = "другое") -> None:
    """Manually set a normalized name and category."""
    with SessionLocal() as session:
        ProductNormalizer(session).set_alias(alias, name, category)
        session.commit()
    typer.echo("Алиас сохранён")


@cli.command("reprocess")
def reprocess(message_id: str) -> None:
    """Mark one parsed receipt for fetching and parsing again."""
    with SessionLocal() as session:
        receipt = session.scalar(select(Receipt).where(Receipt.gmail_message_id == message_id))
        if not receipt:
            raise typer.BadParameter("Gmail message ID не найден")
        receipt.status = "failed"
        receipt.parse_error = "Ручной повторный разбор"
        session.commit()
    retry()


@cli.command("bot")
def bot() -> None:
    run_bot()


@cli.command("scheduler")
def scheduler() -> None:
    run_scheduler()


@cli.command("serve")
def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    uvicorn.run("app.main:app", host=host, port=port)


def main() -> None:
    _logging()
    cli()


if __name__ == "__main__":
    main()
