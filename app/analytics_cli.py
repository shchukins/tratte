"""Private, explicit review workflow for issue #3."""

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import typer
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal, engine
from app.models import AnalyticsMappingVersion
from app.services.analytical_identity import (
    Review,
    apply_review,
    candidates,
    export_review,
    validate_version,
)
from app.services.analytical_marts import calculate

cli = typer.Typer(no_args_is_help=True)


def write_private(path: Path, document):
    write_private_text(path, json.dumps(document, ensure_ascii=False, indent=2))


def write_private_text(path: Path, document: str):
    # Never overwrite an existing review or make private product rows world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(document)
        stream.write("\n")


@cli.command("candidates")
def list_candidates(base_month: str, current_month: str, output: Path):
    """Export exact audit-v1 candidates to a NEW private JSON file; no DB writes."""
    try:
        with SessionLocal() as session:
            document = candidates(
                session, date.fromisoformat(base_month), date.fromisoformat(current_month)
            )
        write_private(output, document)
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Candidate keys: {len(document)}. File contains private review data.")


@cli.command("export")
def export_items(output: Path, item_ids: str, scope: str):
    """Export selected comma-separated IDs as unresolved decisions for explicit review."""
    try:
        ids = [int(value.strip()) for value in item_ids.split(",")]
        with SessionLocal() as session:
            review = export_review(session, ids, scope)
        write_private(output, review.model_dump(mode="json"))
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Unresolved items: {len(review.decisions)}")


@cli.command("revise")
def revise(version: str, output: Path, add_item_ids: str = ""):
    """Copy a valid version for correction/revocation; editing it creates a NEW version."""
    try:
        with SessionLocal() as session:
            validate_version(session, version)
            review = Review.model_validate_json(
                session.get(AnalyticsMappingVersion, version).manifest
            )
            review.parent_version = version
            if add_item_ids:
                ids = [int(value.strip()) for value in add_item_ids.split(",")]
                if set(ids) & {d.item_id for d in review.decisions}:
                    raise ValueError("Added IDs already exist in this version")
                review.decisions.extend(export_review(session, ids, review.scope).decisions)
        write_private(output, review.model_dump(mode="json"))
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo("Review copied; change decisions and remove unreferenced products before applying.")


@cli.command("apply")
def apply(path: Path):
    """Explicitly approve this review as one immutable snapshot. No automatic name matching."""
    try:
        review = Review.model_validate_json(path.read_text(encoding="utf-8"))
        with SessionLocal() as session:
            version = apply_review(session, review)
            session.commit()
    except (ValueError, OSError) as exc:
        # Pydantic errors can echo private input values. Keep them out of logs/stdout.
        raise typer.BadParameter(
            "Review rejected; check schema, IDs, units and source freshness"
        ) from exc
    typer.echo(version)


@cli.command("validate")
def validate(version: str):
    """Gate a calculation/backfill on intact source rows; exit nonzero if stale."""
    try:
        with SessionLocal() as session:
            result = validate_version(session, version)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, sort_keys=True))


@cli.command("marts")
def marts(
    output: Path, mapping_version: str | None = None, as_of: str | None = None,
    base_month: str | None = None, current_month: str | None = None, store: str | None = None,
):
    """Export private SQL datasets; validate the explicit mapping in the same read-only snapshot."""
    try:
        document = calculate(
            engine, as_of=datetime.fromisoformat(as_of) if as_of else datetime.now(UTC),
            base_month=base_month, current_month=current_month,
            mapping_version=mapping_version, store=store,
        )
        write_private_text(output, document)
    except (ValueError, OSError, SQLAlchemyError) as exc:
        raise typer.BadParameter(
            "Marts rejected; check parameters, mapping and output path"
        ) from exc
    typer.echo("Private marts exported; monetary values are preserved as PostgreSQL numerics.")


@cli.command("decompose")
def decompose(
    output: Path, mapping_version: str | None = None, as_of: str | None = None,
    base_month: str | None = None, current_month: str | None = None,
    store: str | None = None, category: str | None = None,
):
    """Export an accounting bridge and previous-price scenario in one validated snapshot."""
    try:
        document = calculate(
            engine, as_of=datetime.fromisoformat(as_of) if as_of else datetime.now(UTC),
            base_month=base_month, current_month=current_month, mapping_version=mapping_version,
            store=store, category=category, decomposition=True,
        )
        write_private_text(output, document)
    except (ValueError, OSError, SQLAlchemyError) as exc:
        raise typer.BadParameter(
            "Decomposition rejected; check parameters, mapping and output path"
        ) from exc
    typer.echo("Private accounting decomposition exported.")
