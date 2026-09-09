"""Operator CLI.

Everything an operator needs during Phase 0: sync the board registry, probe a
board before curating it, run one source in the foreground, inspect corpus
health against the exit criteria.
"""

from __future__ import annotations

import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer
import yaml
from sqlalchemy import text

from app.adapters.http import HttpClient, RateLimitedError, SourceUnavailableError
from app.adapters.sources import build_adapter
from app.config import BASE_DIR, get_config, get_settings
from app.db.queue import TaskType, enqueue
from app.db.session import session_scope
from app.logging import configure_logging
from app.services.ingestion import IngestionService
from app.services.sources import corpus_stats, load_boards, source_health, sync_boards

app = typer.Typer(add_completion=False, help="CareerPilot operator CLI")
sources_app = typer.Typer(help="Source registry")
boards_app = typer.Typer(help="Curated board files")
app.add_typer(sources_app, name="sources")
app.add_typer(boards_app, name="boards")

BOARDS_DIR = BASE_DIR / "config" / "boards"


def _http() -> HttpClient:
    config = get_config()
    return HttpClient(
        user_agent=config.ingestion.user_agent,
        timeout_seconds=config.ingestion.request_timeout_seconds,
        max_retries=config.ingestion.max_retries,
        per_host_min_interval_seconds=config.verification.per_host_min_interval_seconds,
    )


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    settings = get_settings()
    configure_logging("DEBUG" if verbose else settings.log_level, json_output=False)


# ── sources ───────────────────────────────────────────────────────────


@sources_app.command("sync")
def sources_sync(
    directory: Path = typer.Option(BOARDS_DIR, "--dir", help="Board YAML directory"),
    prune: bool = typer.Option(True, help="Disable sources no longer present in YAML"),
) -> None:
    """Load config/boards/*.yaml into job_sources."""
    entries = load_boards(directory)
    with session_scope() as session:
        report = sync_boards(session, entries, prune=prune)
    typer.echo(
        f"synced {report.total} boards: {report.created} created, "
        f"{report.updated} updated, {report.disabled} disabled"
    )


@sources_app.command("list")
def sources_list(json_output: bool = typer.Option(False, "--json")) -> None:
    """Per-source health (§17.1)."""
    with session_scope() as session:
        rows = source_health(session)
    if json_output:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        typer.echo("no sources registered — run `careerpilot sources sync`")
        return
    typer.echo(f"{'source':38} {'adapter':16} {'open':>6} {'last':>6} {'med7d':>6}  status")
    for row in rows:
        median = row["median_7d"]
        typer.echo(
            f"{row['name'][:38]:38} {row['adapter']:16} {row['open_postings']:6} "
            f"{row['last_fetched'] or 0:6} {float(median) if median else 0:6.0f}  "
            f"{row['last_status'] or '-'}"
            + ("" if row["enabled"] else f"  DISABLED: {row['disabled_reason']}")
        )


@sources_app.command("run")
def sources_run(name: str) -> None:
    """Run one source in the foreground, end to end. Prints the run report."""
    with session_scope() as session:
        row = session.execute(
            text("SELECT id FROM job_sources WHERE name = :name"), {"name": name}
        ).first()
        if row is None:
            typer.echo(f"no such source: {name}", err=True)
            raise typer.Exit(code=1)
        source_id: uuid.UUID = row.id

    http = _http()
    try:
        with session_scope() as session:
            report = IngestionService(session, http, get_config()).run_source(source_id)
    finally:
        http.close()
    typer.echo(
        f"{report.source_name}: fetched={report.fetched} new={report.new} "
        f"updated={report.updated} errors={report.errors} status={report.status}"
    )


@app.command("skills-sync")
def skills_sync(
    path: Path = typer.Option(BASE_DIR / "config" / "skills.yaml", "--file"),
) -> None:
    """Load the seed skill taxonomy into the database (§10.2)."""
    from app.services.taxonomy import load_seed, sync_skills

    entries = load_seed(path)
    with session_scope() as session:
        report = sync_skills(session, entries)
    typer.echo(
        f"{len(entries)} skills: {report.skills_created} created, "
        f"{report.skills_existing} existing, {report.aliases_created} new aliases"
    )


@app.command("stats")
def stats() -> None:
    """Corpus statistics against the Phase 0 exit criteria."""
    with session_scope() as session:
        data = corpus_stats(session)
    age = data["newest_posting_age_hours"]
    typer.echo(
        json.dumps(
            {k: (float(v) if isinstance(v, Decimal) else v) for k, v in data.items()},
            indent=2,
            default=str,
        )
    )
    checks = [
        ("≥ 5,000 unique live postings", data["unique_postings"] >= 5000),
        ("every posting has an apply_url", data["missing_apply_url"] == 0),
        ("newest posting < 6h old", age is not None and float(age) < 6),
        ("no suspected false merges", data["suspected_false_merges"] == 0),
        ("no ambiguous apply URLs", data["ambiguous_apply_urls"] == 0),
    ]
    typer.echo("")
    for label, passed in checks:
        typer.echo(f"  [{'x' if passed else ' '}] {label}")


@sources_app.command("renormalize")
def sources_renormalize(
    name: str | None = typer.Option(None, "--source", help="One source; default is all"),
    batch: int = typer.Option(2000, help="Payloads to replay per source"),
) -> None:
    """Re-derive postings from stored raw payloads, fetching nothing.

    Run this after changing an adapter or a shared normaliser: the payloads were
    persisted verbatim precisely so that a normalisation bug costs a replay
    rather than a re-fetch.
    """
    with session_scope() as session:
        query = "SELECT id, name FROM job_sources"
        params: dict[str, object] = {}
        if name:
            query += " WHERE name = :name"
            params["name"] = name
        sources = session.execute(text(query), params).all()

    http = _http()
    replayed = updated = errors = 0
    try:
        for source in sources:
            with session_scope() as session:
                report = IngestionService(session, http, get_config()).renormalize_source(
                    source.id, batch=batch
                )
            replayed += report.fetched
            updated += report.updated
            errors += report.errors
            if report.fetched:
                typer.echo(f"  {source.name}: replayed {report.fetched}, errors {report.errors}")
    finally:
        http.close()
    typer.echo(
        f"replayed {replayed} payloads across {len(sources)} sources "
        f"({updated} postings updated, {errors} errors)"
    )


@app.command("redo-dedup")
def redo_dedup(
    batch: int = typer.Option(200, help="Postings per queued task"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Re-cluster the whole corpus after a change to the deduplication rules.

    Clears every group and queues deduplication for all open postings. Grouping
    is derived data — the postings themselves are untouched — but a rebuild is a
    lot of work, so it asks first.
    """
    with session_scope() as session:
        total = session.execute(
            text("SELECT count(*) FROM job_postings WHERE status = 'open'")
        ).scalar_one()
    if not yes and not typer.confirm(f"Re-cluster {total} open postings?"):
        raise typer.Abort()

    with session_scope() as session:
        session.execute(text("UPDATE job_postings SET job_group_id = NULL"))
        session.execute(text("DELETE FROM job_groups"))
        rows = (
            session.execute(
                text("SELECT id FROM job_postings WHERE status = 'open' ORDER BY company_id, id")
            )
            .scalars()
            .all()
        )
        queued = 0
        for start in range(0, len(rows), batch):
            chunk = [str(pid) for pid in rows[start : start + batch]]
            if enqueue(
                session,
                TaskType.DEDUPLICATE,
                {"posting_ids": chunk},
                priority=8,
                dedup_key=f"redo-dedup:{chunk[0]}",
            ):
                queued += 1
    typer.echo(f"queued {queued} deduplication task(s) for {len(rows)} postings")


@app.command("worker")
def worker() -> None:
    """Run the worker process (scheduler + queue consumers)."""
    from app.workers.runner import Worker

    Worker().start()


@app.command("work-once")
def work_once(limit: int = typer.Option(1, help="Maximum tasks to execute")) -> None:
    """Execute up to `limit` queued tasks, then exit. Useful in development and CI."""
    from app.workers.runner import Worker

    runner = Worker(concurrency=1)
    executed = 0
    try:
        while executed < limit and runner.run_once():
            executed += 1
    finally:
        runner.shutdown()
        runner.http.close()
    typer.echo(f"executed {executed} task(s)")


# ── boards ────────────────────────────────────────────────────────────


@boards_app.command("validate")
def boards_validate(
    directory: Path = typer.Option(BOARDS_DIR, "--dir"),
    write: Path | None = typer.Option(
        None, "--write", help="Write a report of live/dead boards to this JSON file"
    ),
    fail_on_dead: bool = typer.Option(False, "--fail-on-dead"),
) -> None:
    """Probe every curated board and report what it actually returns.

    A board token is a claim about the world, and claims decay: companies churn
    ATS vendors and rename boards. This is what keeps `config/boards/*.yaml`
    honest, and it is why no token in this repository was added without a live
    200 and a non-zero posting count behind it.
    """
    entries = load_boards(directory)
    http = _http()
    results: list[dict[str, Any]] = []
    live = dead = empty = 0

    try:
        for entry in entries:
            adapter = build_adapter(
                entry.adapter,
                name=entry.name,
                config=entry.config,
                http=http,
                rate_limit_rpm=entry.rate_limit_rpm,
            )
            health = adapter.health()
            count = 0
            if health.reachable and health.detail:
                count = int(health.detail.split()[0])
            state = "live" if count > 0 else ("empty" if health.reachable else "dead")
            live += state == "live"
            empty += state == "empty"
            dead += state == "dead"
            results.append(
                {
                    "name": entry.name,
                    "adapter": entry.adapter,
                    "region": entry.region,
                    "state": state,
                    "postings": count,
                    "detail": health.detail,
                }
            )
            typer.echo(f"{state:6} {count:5}  {entry.name}  {health.detail or ''}")
    finally:
        http.close()

    typer.echo(f"\n{live} live · {empty} reachable but empty · {dead} dead")
    if write:
        write.write_text(json.dumps(results, indent=2), encoding="utf-8")
        typer.echo(f"report written to {write}")
    if fail_on_dead and dead:
        raise typer.Exit(code=1)


@boards_app.command("probe")
def boards_probe(
    adapter: str = typer.Argument(
        ..., help="greenhouse | lever | ashby | workable | smartrecruiters | recruitee"
    ),
    token: str = typer.Argument(..., help="board token / company slug / org / subdomain"),
    show: int = typer.Option(3, help="How many normalised postings to print"),
) -> None:
    """Probe one candidate board before adding it to the registry."""
    key = {
        "greenhouse": "board_token",
        "lever": "company",
        "ashby": "org",
        "workable": "subdomain",
        "smartrecruiters": "company_id",
        "recruitee": "company",
    }.get(adapter)
    if key is None:
        typer.echo(f"unknown adapter: {adapter}", err=True)
        raise typer.Exit(code=1)

    http = _http()
    try:
        source = build_adapter(
            adapter, name=f"{adapter}:{token}", config={key: token}, http=http, rate_limit_rpm=30
        )
        raws = list(source.fetch())
        typer.echo(f"{len(raws)} postings")
        for raw in raws[:show]:
            job = source.normalize(raw)
            typer.echo(
                json.dumps(
                    {
                        "title": job.title,
                        "company": job.company.name,
                        "locations": [loc.raw for loc in job.locations],
                        "remote_type": job.remote_type,
                        "seniority": job.seniority_level,
                        "posted_at": job.posted_at,
                        "apply_url": job.apply_url,
                        "description_chars": len(job.description_text),
                    },
                    indent=2,
                    default=str,
                )
            )
    except (RateLimitedError, SourceUnavailableError) as exc:
        typer.echo(f"unreachable: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        http.close()


@boards_app.command("check-candidates")
def boards_check_candidates(
    path: Path = typer.Argument(..., help="YAML file of candidate boards to probe"),
    keep: Path | None = typer.Option(None, "--keep", help="Write live entries to this YAML file"),
) -> None:
    """Probe a candidate list and write out only the boards that actually resolve."""
    with path.open("r", encoding="utf-8") as handle:
        candidates = yaml.safe_load(handle) or []

    http = _http()
    kept: list[dict[str, Any]] = []
    try:
        for item in candidates:
            adapter = build_adapter(
                item["adapter"],
                name=item["name"],
                config=item.get("config") or {},
                http=http,
                rate_limit_rpm=int(item.get("rate_limit_rpm", 30)),
            )
            health = adapter.health()
            count = int(health.detail.split()[0]) if health.reachable and health.detail else 0
            typer.echo(f"{'live ' if count else 'dead '} {count:5}  {item['name']}")
            if count > 0:
                item.setdefault("rate_limit_rpm", 20)
                kept.append(item)
    finally:
        http.close()

    typer.echo(f"\n{len(kept)}/{len(candidates)} boards live")
    if keep:
        keep.write_text(yaml.safe_dump(kept, sort_keys=False, allow_unicode=True), encoding="utf-8")
        typer.echo(f"wrote {len(kept)} entries to {keep}")


if __name__ == "__main__":
    sys.exit(app())
