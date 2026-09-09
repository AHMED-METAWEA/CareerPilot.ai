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


eval_app = typer.Typer(help="Evaluation harness (§9)")
app.add_typer(eval_app, name="eval")


@eval_app.command("rubric")
def eval_rubric() -> None:
    """Print the grading rubric annotators work from."""
    from app.eval.golden import RUBRIC

    typer.echo(RUBRIC)


@eval_app.command("sample")
def eval_sample(
    profile: str = typer.Option(..., "--profile", help="Profile id to sample for"),
    per_decile: int = typer.Option(6, help="Pairs per score decile"),
    out: Path = typer.Option(Path("golden-sample.csv"), "--out"),
) -> None:
    """Draw a stratified sample of pairs to label (§9.1).

    Stratified across score deciles on purpose: labelling only the top of the
    ranking measures how good the system is at cases it already likes.
    """
    import csv

    from app.eval.golden import sample_for_labelling

    with session_scope() as session:
        rows = sample_for_labelling(session, uuid.UUID(profile), per_decile=per_decile)

    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "profile_id",
                "posting_id",
                "decile",
                "score",
                "title",
                "company",
                "apply_url",
                "grade",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    profile,
                    row["posting_id"],
                    row["decile"],
                    round(float(str(row["total_score"] or 0)), 4),
                    row["title"],
                    row["company"],
                    row["apply_url"],
                    "",
                ]
            )
    typer.echo(f"wrote {len(rows)} pairs to {out} — fill in the `grade` column (0-3)")
    typer.echo("Rubric: careerpilot eval rubric")


@eval_app.command("load-labels")
def eval_load_labels(
    path: Path = typer.Argument(..., help="Filled-in CSV from `eval sample`"),
    labeller: str = typer.Option(..., "--labeller", help="Who graded these"),
) -> None:
    """Load one annotator's grades into `eval_labels`."""
    import csv

    from app.eval.golden import record_label

    loaded = 0
    with path.open("r", encoding="utf-8") as handle, session_scope() as session:
        for row in csv.DictReader(handle):
            grade = (row.get("grade") or "").strip()
            if grade == "":
                continue
            record_label(
                session,
                profile_id=uuid.UUID(row["profile_id"]),
                posting_id=uuid.UUID(row["posting_id"]),
                grade=int(grade),
                labeller=labeller,
            )
            loaded += 1
    typer.echo(f"loaded {loaded} labels from {labeller}")


@eval_app.command("agreement")
def eval_agreement() -> None:
    """Inter-annotator agreement and the κ ≥ 0.60 gate (§9.1)."""
    from app.eval.golden import KAPPA_GATE, agreement

    with session_scope() as session:
        stats = agreement(session)

    typer.echo(f"pairs      : {stats.pairs}")
    typer.echo(f"annotators : {stats.labellers}")
    typer.echo(f"grades     : {dict(sorted(stats.by_grade.items()))}")
    if stats.cohens_kappa is not None:
        typer.echo(f"Cohen's κ  : {stats.cohens_kappa}")
    if stats.fleiss_kappa is not None:
        typer.echo(f"Fleiss' κ  : {stats.fleiss_kappa}")
    if stats.pairs:
        verdict = "PASS" if stats.passes_gate else "FAIL"
        typer.echo(f"\n{verdict}: the gate is κ ≥ {KAPPA_GATE}.")
        if not stats.passes_gate:
            typer.echo("The rubric is defective. Revise it before trusting any metric (§9.1).")


@eval_app.command("run")
def eval_run(
    fail_on_regression: bool = typer.Option(False, "--check-regression"),
    max_drop: float = typer.Option(2.0, "--max-drop", help="Points of NDCG@10"),
) -> None:
    """Run the ablation and print the §9.3 table."""
    from app.adapters.embeddings import build_reranker
    from app.eval.harness import EvaluationHarness, check_regression

    config = get_config()
    with session_scope() as session:
        harness = EvaluationHarness(
            session, config, reranker=build_reranker(config.models.reranker)
        )
        previous = harness.previous_ndcg()
        report = harness.run()

    if not report.results:
        typer.echo("No labelled pairs yet. The golden set is human work (§9.1):")
        typer.echo("  careerpilot eval sample --profile <id>   # draw a stratified sample")
        typer.echo("  careerpilot eval load-labels <csv> --labeller <name>")
        raise typer.Exit(code=0)

    typer.echo(
        f"{report.labelled_pairs} labelled pairs across {report.labelled_profiles} profiles\n"
    )
    typer.echo(report.markdown_table())

    if report.cross_lingual is not None:
        typer.echo("\nBy language (§18, Phase 5):\n")
        typer.echo(report.cross_lingual.markdown_table())

    headline = report.headline
    if headline is not None and fail_on_regression:
        ok, message = check_regression(headline.metrics.ndcg_at_10, previous, tolerance=max_drop)
        typer.echo(f"\n{message}")
        if not ok:
            raise typer.Exit(code=1)


@eval_app.command("languages")
def eval_languages() -> None:
    """Report the language make-up of the corpus and the golden set (§18, Phase 5).

    Runs no matching, so it is the cheap way to see whether the Arabic split can
    be measured at all before spending an hour on the harness.
    """
    from sqlalchemy import text as sql

    from app.eval.golden import load_labels
    from app.eval.splits import (
        MIN_PAIRS_FOR_A_NUMBER,
        posting_languages,
        profile_languages,
    )

    with session_scope() as session:
        postings = posting_languages(session)
        profiles = profile_languages(session)
        labels = load_labels(session)
        stored: dict[str, int] = {
            str(row[0]): int(row[1])
            for row in session.execute(
                sql(
                    "SELECT COALESCE(language, 'unset'), count(*) FROM job_postings "
                    "GROUP BY 1 ORDER BY 2 DESC"
                )
            ).all()
        }

    typer.echo("Corpus")
    for language, count in sorted(stored.items(), key=lambda item: -item[1]):
        typer.echo(f"  {language:>5}: {count}")

    arabic_postings = sum(1 for language in postings.values() if language == "ar")
    typer.echo(f"  detected Arabic (including unset rows): {arabic_postings}")

    typer.echo("\nProfiles")
    for language in ("en", "ar"):
        typer.echo(f"  {language:>5}: {sum(1 for v in profiles.values() if v == language)}")

    typer.echo("\nLabelled pairs by cell")
    cells: dict[str, int] = {}
    for profile_id, pairs in labels.items():
        cv_language = profiles.get(profile_id, "en")
        for posting_id in pairs:
            cell = f"{cv_language}\u2192{postings.get(posting_id, 'en')}"
            cells[cell] = cells.get(cell, 0) + 1

    if not cells:
        typer.echo("  none — the golden set is human work (\u00a79.1)")
    for cell in ("en\u2192en", "en\u2192ar", "ar\u2192en", "ar\u2192ar"):
        count = cells.get(cell, 0)
        mark = "measurable" if count >= MIN_PAIRS_FOR_A_NUMBER else "not enough to measure"
        typer.echo(f"  {cell}: {count} ({mark})")


@eval_app.command("controls")
def eval_controls() -> None:
    """Run the negative controls (§9.4). These need no labels."""
    from app.eval.controls_runner import run_controls

    with session_scope() as session:
        summary = run_controls(session, get_config())

    for control in summary["controls"]:
        mark = "PASS" if control["passed"] else "FAIL"
        typer.echo(f"[{mark}] {control['name']}: {control['detail']}")
    if not summary["passed"]:
        typer.echo("\nA failed control means the score is not measuring fit (§9.4).")
        raise typer.Exit(code=1)


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
