"""Source registry: YAML in Git, rows in Postgres (§5.7).

Every source is a database row, not code. The curated board files under
`config/boards/` are the reviewable, version-controlled truth; `sync` reconciles
them into `job_sources` without ever destroying operational state (health
counters, ETags, last-run timestamps).

A board removed from YAML is *disabled*, not deleted: its postings still
reference the source row, and a deletion would orphan them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.sources import ADAPTERS
from app.db.session import rows_affected

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BoardEntry:
    adapter: str
    name: str
    config: dict[str, Any]
    rate_limit_rpm: int = 20
    enabled: bool = True
    robots_ok: bool = True
    tier: int = 1
    region: str | None = None

    @staticmethod
    def from_dict(data: dict[str, Any], *, region: str | None = None) -> BoardEntry:
        adapter = data["adapter"]
        if adapter not in ADAPTERS:
            raise ValueError(f"unknown adapter '{adapter}' for board '{data.get('name')}'")
        return BoardEntry(
            adapter=adapter,
            name=data["name"],
            config=data.get("config") or {},
            rate_limit_rpm=int(data.get("rate_limit_rpm", 20)),
            enabled=bool(data.get("enabled", True)),
            robots_ok=bool(data.get("robots_ok", True)),
            tier=int(data.get("tier", 1)),
            region=data.get("region") or region,
        )


@dataclass(slots=True)
class SyncReport:
    created: int = 0
    updated: int = 0
    disabled: int = 0
    total: int = 0


def load_boards(directory: Path) -> list[BoardEntry]:
    """Read every `*.yaml` under `config/boards/`, tagging entries by filename."""
    entries: list[BoardEntry] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or []
        if not isinstance(data, list):
            raise ValueError(f"{path}: expected a list of board entries")
        for item in data:
            entry = BoardEntry.from_dict(item, region=path.stem)
            if entry.name in seen:
                raise ValueError(f"{path}: duplicate source name '{entry.name}'")
            seen.add(entry.name)
            entries.append(entry)
    return entries


def sync_boards(session: Session, entries: list[BoardEntry], *, prune: bool = True) -> SyncReport:
    """Reconcile YAML entries into `job_sources`.

    Config changes overwrite; operational columns (consecutive_failures, etag,
    last_run_at) are never touched, so a re-sync does not reset a circuit
    breaker or force a full re-fetch.
    """
    report = SyncReport(total=len(entries))
    for entry in entries:
        row = session.execute(
            text(
                """
                INSERT INTO job_sources (adapter, name, config, enabled, robots_ok,
                                         rate_limit_rpm, tier, region)
                VALUES (:adapter, :name, CAST(:config AS jsonb), :enabled, :robots_ok,
                        :rate_limit_rpm, :tier, :region)
                ON CONFLICT (name) DO UPDATE SET
                    adapter        = EXCLUDED.adapter,
                    config         = EXCLUDED.config,
                    enabled        = EXCLUDED.enabled,
                    robots_ok      = EXCLUDED.robots_ok,
                    rate_limit_rpm = EXCLUDED.rate_limit_rpm,
                    tier           = EXCLUDED.tier,
                    region         = EXCLUDED.region,
                    disabled_reason = CASE WHEN EXCLUDED.enabled THEN NULL
                                           ELSE job_sources.disabled_reason END
                RETURNING id, (xmax = 0) AS inserted
                """
            ),
            {
                "adapter": entry.adapter,
                "name": entry.name,
                "config": json.dumps(entry.config),
                "enabled": entry.enabled,
                "robots_ok": entry.robots_ok,
                "rate_limit_rpm": entry.rate_limit_rpm,
                "tier": entry.tier,
                "region": entry.region,
            },
        ).one()
        if row.inserted:
            report.created += 1
        else:
            report.updated += 1

    if prune and entries:
        result = session.execute(
            text(
                """
                UPDATE job_sources
                   SET enabled = false,
                       disabled_reason = 'removed from config/boards'
                 WHERE enabled
                   AND name <> ALL(:names)
                """
            ),
            {"names": [e.name for e in entries]},
        )
        report.disabled = rows_affected(result)

    log.info(
        "sources.synced",
        created=report.created,
        updated=report.updated,
        disabled=report.disabled,
    )
    return report


def source_health(session: Session) -> list[dict[str, Any]]:
    """Per-source health for `/admin/sources` and the CLI (§17.1).

    Reports the last run against the source's own 7-day median, because a
    source's normal volume is the only meaningful baseline — 12 postings is
    healthy for a 30-person company and catastrophic for a multinational.
    """
    rows = session.execute(
        text(
            """
            WITH recent AS (
                SELECT source_id,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY fetched) AS median_7d,
                       count(*) AS runs_7d,
                       sum(errors) AS errors_7d
                  FROM source_runs
                 WHERE started_at > now() - interval '7 days'
                 GROUP BY source_id
            ),
            last_run AS (
                SELECT DISTINCT ON (source_id)
                       source_id, started_at, finished_at, fetched, new_count,
                       updated_count, errors, status
                  FROM source_runs
                 ORDER BY source_id, started_at DESC
            )
            SELECT s.id, s.name, s.adapter, s.tier, s.region, s.enabled,
                   s.consecutive_failures, s.disabled_reason, s.rate_limit_rpm,
                   l.started_at AS last_started_at, l.finished_at AS last_finished_at,
                   l.fetched AS last_fetched, l.new_count AS last_new,
                   l.errors AS last_errors, l.status AS last_status,
                   r.median_7d, r.runs_7d, r.errors_7d,
                   (SELECT count(*) FROM job_postings p
                     WHERE p.source_id = s.id AND p.status = 'open') AS open_postings
              FROM job_sources s
              LEFT JOIN last_run l ON l.source_id = s.id
              LEFT JOIN recent r ON r.source_id = s.id
             ORDER BY s.enabled DESC, s.name
            """
        )
    ).all()
    return [dict(row._mapping) for row in rows]


def corpus_stats(session: Session) -> dict[str, Any]:
    """The Phase 0 exit criteria, as one query (§18)."""
    row = session.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM job_postings WHERE status = 'open') AS open_postings,
              (SELECT count(DISTINCT COALESCE(job_group_id, id)) FROM job_postings
                WHERE status = 'open') AS unique_postings,
              (SELECT count(*) FROM job_postings
                WHERE status = 'open' AND (apply_url IS NULL OR apply_url = '')) AS missing_apply_url,
              (SELECT count(*) FROM companies) AS companies,
              (SELECT count(*) FROM company_review_queue WHERE status = 'pending') AS companies_for_review,
              (SELECT count(*) FROM job_sources WHERE enabled) AS enabled_sources,
              (SELECT count(*) FROM job_sources WHERE NOT enabled) AS disabled_sources,
              (SELECT count(*) FROM job_groups WHERE member_count > 1) AS duplicate_groups,
              (SELECT extract(epoch FROM now() - max(posted_at)) / 3600.0 FROM job_postings
                WHERE status = 'open') AS newest_posting_age_hours,
              -- A company row carrying many distinct employer names is the
              -- signature of a false merge (R5). One query, and the failure
              -- stops being invisible.
              (SELECT count(*) FROM (
                 SELECT company_id
                   FROM job_postings
                  WHERE company_id IS NOT NULL AND company_name_raw IS NOT NULL
                  GROUP BY company_id
                 HAVING count(DISTINCT company_name_raw) > 3
               ) AS suspicious) AS suspected_false_merges,
              -- Postings whose apply URL is shared by many others: the URL has
              -- lost the job's identity, usually because a parameter carrying it
              -- was stripped somewhere upstream.
              (SELECT count(*) FROM (
                 SELECT canonical_url FROM job_postings
                  WHERE canonical_url IS NOT NULL AND canonical_url <> ''
                  GROUP BY canonical_url HAVING count(*) > 8
               ) AS shared) AS ambiguous_apply_urls
            """
        )
    ).one()
    return dict(row._mapping)
