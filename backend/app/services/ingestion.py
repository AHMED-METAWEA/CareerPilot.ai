"""Discovery run orchestration (§11.2 W2).

One run = one source. The sequence is fixed: claim, fetch, persist verbatim,
normalise, resolve the employer, upsert the posting, record the run, check the
run against the source's own history, and enqueue deduplication for whatever
changed.

Two properties matter more than throughput:

* **Replayability.** The raw payload is persisted before anything interprets
  it, so a normalisation bug is a re-run, not a lost day of data.
* **Loud failure.** A source that returns zero rows against a productive
  history raises an alert instead of quietly recording success (R2).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.adapters.http import HttpClient, RateLimitedError, SourceUnavailableError
from app.adapters.sources import build_adapter
from app.adapters.sources.base import BaseSourceAdapter
from app.config import AppConfig
from app.db.queue import TaskType, enqueue
from app.domain.models import NormalizedJob, RawJob
from app.services.companies import CompanyDirectory

log = structlog.get_logger(__name__)

CIRCUIT_BREAKER_FAILURES = 5
DETAIL_FETCH_LIMIT_PER_RUN = 500
DETAIL_BATCH = 25


@dataclass(slots=True)
class RunReport:
    source_id: uuid.UUID
    source_name: str
    run_id: uuid.UUID | None
    fetched: int = 0
    new: int = 0
    updated: int = 0
    errors: int = 0
    status: str = "running"
    detail: dict[str, Any] = field(default_factory=dict)
    changed_posting_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SourceRow:
    id: uuid.UUID
    adapter: str
    name: str
    config: dict[str, Any]
    rate_limit_rpm: int
    tier: int


class IngestionService:
    def __init__(self, session: Session, http: HttpClient, config: AppConfig) -> None:
        self.session = session
        self.http = http
        self.config = config

    # ── entry point ──────────────────────────────────────────────────

    def run_source(self, source_id: uuid.UUID) -> RunReport:
        source = self._load_source(source_id)
        report = RunReport(source_id=source.id, source_name=source.name, run_id=None)

        adapter = build_adapter(
            source.adapter,
            name=source.name,
            config=source.config,
            http=self.http,
            rate_limit_rpm=source.rate_limit_rpm,
        )
        report.run_id = self._start_run(source.id)
        directory = CompanyDirectory.load(self.session)

        try:
            self._ingest(adapter, source, report, directory)
        except (RateLimitedError, SourceUnavailableError) as exc:
            report.status = "failed"
            report.detail["error"] = str(exc)
            self._finish_run(report)
            self._record_failure(source.id, str(exc))
            raise
        except Exception as exc:  # schema drift, adapter bug — equally a failure
            report.status = "failed"
            report.detail["error"] = f"{type(exc).__name__}: {exc}"
            self._finish_run(report)
            self._record_failure(source.id, report.detail["error"])
            raise

        if adapter.supports_details:
            report.detail["details_queued"] = self._queue_detail_fetches(source, adapter)

        report.status = self._assess_run(source, report)
        self._finish_run(report)
        self._record_success(source.id)
        return report

    def renormalize_source(self, source_id: uuid.UUID, *, batch: int = 500) -> RunReport:
        """Re-run normalisation over stored payloads, without fetching anything.

        This is what persisting `raw_payloads` verbatim buys (§11.2 step 3): a
        normalisation bug is a re-run, not a lost day of data. Used when an
        adapter or a shared normaliser changes — as when HTML entity decoding
        was found to run after tag stripping, leaving markup in every Greenhouse
        description.
        """
        source = self._load_source(source_id)
        adapter = build_adapter(
            source.adapter,
            name=source.name,
            config=source.config,
            http=self.http,
            rate_limit_rpm=source.rate_limit_rpm,
        )
        report = RunReport(source_id=source.id, source_name=source.name, run_id=None)
        directory = CompanyDirectory.load(self.session)

        rows = self.session.execute(
            text(
                """
                SELECT DISTINCT ON (external_id) external_id, payload
                  FROM raw_payloads
                 WHERE source_id = :source_id
                 ORDER BY external_id, fetched_at DESC
                 LIMIT :limit
                """
            ),
            {"source_id": source.id, "limit": batch},
        ).all()

        for row in rows:
            report.fetched += 1
            raw = RawJob(
                source_name=source.name,
                external_id=row.external_id,
                payload=row.payload,
                fetched_at=datetime.now(UTC),
            )
            try:
                normalized = adapter.normalize(raw)
            except Exception as exc:
                report.errors += 1
                log.warning(
                    "renormalize.failed",
                    source=source.name,
                    external_id=row.external_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue

            company_id, _ = directory.resolve_or_create(
                normalized.company,
                source_id=source.id,
                fuzzy_threshold=self.config.dedup.company_fuzzy_threshold,
                review_threshold=self.config.dedup.company_review_threshold,
            )
            posting_id, inserted = self._upsert_posting(source.id, company_id, normalized)
            report.changed_posting_ids.append(posting_id)
            report.new += int(inserted)
            report.updated += int(not inserted)

        report.status = "ok"
        log.info(
            "renormalize.completed",
            source=source.name,
            replayed=report.fetched,
            updated=report.updated,
            errors=report.errors,
        )
        return report

    # ── the run itself ───────────────────────────────────────────────

    def _ingest(
        self,
        adapter: BaseSourceAdapter,
        source: SourceRow,
        report: RunReport,
        directory: CompanyDirectory,
    ) -> None:
        assert report.run_id is not None
        for raw in adapter.fetch():
            report.fetched += 1
            # Persisted verbatim before interpretation: a normalisation bug must
            # never cost us the payload (§11.2 step 3).
            self._store_raw(source.id, report.run_id, raw.external_id, raw.payload)
            try:
                normalized = adapter.normalize(raw)
            except Exception as exc:
                report.errors += 1
                log.warning(
                    "ingestion.normalize_failed",
                    source=source.name,
                    external_id=raw.external_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue

            company_id, _resolution = directory.resolve_or_create(
                normalized.company,
                source_id=source.id,
                fuzzy_threshold=self.config.dedup.company_fuzzy_threshold,
                review_threshold=self.config.dedup.company_review_threshold,
            )
            posting_id, inserted = self._upsert_posting(source.id, company_id, normalized)
            report.changed_posting_ids.append(posting_id)
            if inserted:
                report.new += 1
            else:
                report.updated += 1

    def _queue_detail_fetches(self, source: SourceRow, adapter: BaseSourceAdapter) -> int:
        """Queue bodies for postings that still lack one, oldest first.

        Bounded per run: a 4,800-posting board fills in over several runs rather
        than issuing 4,800 requests in one. Newest postings come first because
        they are the ones a user might be shown today.
        """
        rows = self.session.execute(
            text(
                """
                SELECT external_id FROM job_postings
                 WHERE source_id = :source_id
                   AND status = 'open'
                   AND description_text = ''
                 ORDER BY posted_at DESC NULLS LAST
                 LIMIT :limit
                """
            ),
            {"source_id": source.id, "limit": DETAIL_FETCH_LIMIT_PER_RUN},
        ).all()
        external_ids = [row.external_id for row in rows]
        for start in range(0, len(external_ids), DETAIL_BATCH):
            batch = external_ids[start : start + DETAIL_BATCH]
            enqueue(
                self.session,
                TaskType.FETCH_DETAILS,
                {"source_id": str(source.id), "external_ids": batch},
                priority=3,
                dedup_key=f"details:{source.id}:{batch[0]}",
            )
        return len(external_ids)

    # ── persistence ──────────────────────────────────────────────────

    def _store_raw(
        self, source_id: uuid.UUID, run_id: uuid.UUID, external_id: str, payload: dict[str, Any]
    ) -> None:
        self.session.execute(
            text(
                """
                INSERT INTO raw_payloads (source_id, run_id, external_id, payload)
                VALUES (:source_id, :run_id, :external_id, CAST(:payload AS jsonb))
                """
            ),
            {
                "source_id": source_id,
                "run_id": run_id,
                "external_id": external_id,
                "payload": json.dumps(payload, default=str),
            },
        )

    def _upsert_posting(
        self, source_id: uuid.UUID, company_id: uuid.UUID, job: NormalizedJob
    ) -> tuple[uuid.UUID, bool]:
        """Insert or update by (source_id, external_id) — dedup stage 1.

        `apply_url`, `source_url` and `canonical_url` are all written, and none
        is overwritten by a later resolution of a *different* kind: an update
        refreshes them from the same authoritative field it used before
        (Appendix D).

        `xmax = 0` distinguishes a genuine insert from an update in one
        round-trip, which is what the run counters need.
        """
        params = {
            "source_id": source_id,
            "external_id": job.external_id,
            "company_id": company_id,
            "company_name_raw": job.company.name,
            "title": job.title,
            "title_normalized": job.title_normalized,
            "description_text": job.description_text,
            "locations": json.dumps([loc.model_dump() for loc in job.locations]),
            "remote_type": job.remote_type.value if job.remote_type else None,
            "employment_type": job.employment_type.value if job.employment_type else None,
            "seniority_level": job.seniority_level.value if job.seniority_level else None,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "currency": job.currency,
            "posted_at": job.posted_at,
            "expires_at": job.expires_at,
            "ats_platform": job.ats_platform.value,
            "ats_confidence": job.ats_confidence,
            "detection_method": job.detection_method.value,
            "apply_url": job.apply_url,
            "apply_url_method": job.apply_url_method.value,
            "source_url": job.source_url,
            "canonical_url": job.canonical_url,
            "content_simhash": job.content_simhash,
            "language": job.language,
            "status": job.status.value,
        }
        row = self.session.execute(
            text(
                """
                INSERT INTO job_postings (
                    source_id, external_id, company_id, company_name_raw, title,
                    title_normalized, description_text, locations, remote_type,
                    employment_type, seniority_level, salary_min, salary_max, currency,
                    posted_at, expires_at, ats_platform, ats_confidence, detection_method,
                    apply_url, apply_url_method, source_url, canonical_url,
                    content_simhash, language, status
                ) VALUES (
                    :source_id, :external_id, :company_id, :company_name_raw, :title,
                    :title_normalized, :description_text, CAST(:locations AS jsonb), :remote_type,
                    :employment_type, :seniority_level, :salary_min, :salary_max, :currency,
                    :posted_at, :expires_at, :ats_platform, :ats_confidence, :detection_method,
                    :apply_url, :apply_url_method, :source_url, :canonical_url,
                    :content_simhash, :language, :status
                )
                ON CONFLICT (source_id, external_id) DO UPDATE SET
                    company_id       = EXCLUDED.company_id,
                    company_name_raw = EXCLUDED.company_name_raw,
                    title            = EXCLUDED.title,
                    title_normalized = EXCLUDED.title_normalized,
                    -- Never blank a body we already have: a list-only row from
                    -- a two-phase source must not erase a fetched description.
                    description_text = CASE
                        WHEN EXCLUDED.description_text = '' THEN job_postings.description_text
                        ELSE EXCLUDED.description_text END,
                    locations        = EXCLUDED.locations,
                    remote_type      = EXCLUDED.remote_type,
                    employment_type  = EXCLUDED.employment_type,
                    seniority_level  = EXCLUDED.seniority_level,
                    salary_min       = EXCLUDED.salary_min,
                    salary_max       = EXCLUDED.salary_max,
                    currency         = EXCLUDED.currency,
                    posted_at        = COALESCE(job_postings.posted_at, EXCLUDED.posted_at),
                    expires_at       = EXCLUDED.expires_at,
                    apply_url        = EXCLUDED.apply_url,
                    apply_url_method = EXCLUDED.apply_url_method,
                    source_url       = EXCLUDED.source_url,
                    canonical_url    = EXCLUDED.canonical_url,
                    content_simhash  = EXCLUDED.content_simhash,
                    language         = EXCLUDED.language,
                    status           = EXCLUDED.status,
                    last_seen_at     = now(),
                    updated_at       = now()
                RETURNING id, (xmax = 0) AS inserted
                """
            ),
            params,
        ).one()
        return row.id, bool(row.inserted)

    # ── run bookkeeping and health ───────────────────────────────────

    def _load_source(self, source_id: uuid.UUID) -> SourceRow:
        row = self.session.execute(
            text(
                """
                SELECT id, adapter, name, config, rate_limit_rpm, tier
                  FROM job_sources WHERE id = :id
                """
            ),
            {"id": source_id},
        ).first()
        if row is None:
            raise LookupError(f"job_sources row {source_id} not found")
        return SourceRow(
            id=row.id,
            adapter=row.adapter,
            name=row.name,
            config=row.config or {},
            rate_limit_rpm=row.rate_limit_rpm,
            tier=row.tier,
        )

    def _start_run(self, source_id: uuid.UUID) -> uuid.UUID:
        row = self.session.execute(
            text("INSERT INTO source_runs (source_id) VALUES (:source_id) RETURNING id"),
            {"source_id": source_id},
        ).one()
        return uuid.UUID(str(row.id))

    def _finish_run(self, report: RunReport) -> None:
        if report.run_id is None:
            return
        self.session.execute(
            text(
                """
                UPDATE source_runs
                   SET finished_at = now(), fetched = :fetched, new_count = :new,
                       updated_count = :updated, errors = :errors, status = :status,
                       detail = CAST(:detail AS jsonb)
                 WHERE id = :id
                """
            ),
            {
                "id": report.run_id,
                "fetched": report.fetched,
                "new": report.new,
                "updated": report.updated,
                "errors": report.errors,
                "status": report.status,
                "detail": json.dumps(report.detail, default=str),
            },
        )
        self.session.execute(
            text("UPDATE job_sources SET last_run_at = now() WHERE id = :id"),
            {"id": report.source_id},
        )

    def _assess_run(self, source: SourceRow, report: RunReport) -> str:
        """Compare this run against the source's own 7-day median (§17.1).

        A source that has been returning 300 rows a day and returns 0 today has
        broken, whatever HTTP said. `suspect` is a successful run that nobody
        should trust — it is the signal R2 is about.
        """
        row = self.session.execute(
            text(
                """
                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY fetched) AS median,
                       count(*) AS runs
                  FROM source_runs
                 WHERE source_id = :source_id
                   AND status IN ('ok', 'suspect')
                   AND started_at > now() - interval '7 days'
                """
            ),
            {"source_id": source.id},
        ).one()
        median = float(row.median) if row.median is not None else None
        if median is None or row.runs < 3 or median <= 0:
            return "ok"

        ratio = report.fetched / median
        if ratio < self.config.ingestion.zero_row_alert_ratio:
            report.detail.update({"median_7d": median, "ratio": round(ratio, 3)})
            log.error(
                "source.health_degraded",
                source=source.name,
                fetched=report.fetched,
                median_7d=median,
                ratio=round(ratio, 3),
            )
            return "suspect"
        return "ok"

    def _record_failure(self, source_id: uuid.UUID, error: str) -> None:
        """Open the circuit breaker after five consecutive failures (§13.2).

        Disabling beats retrying into a hard quota: the source stops, an alert
        is raised, and a human decides. Re-enabling is deliberate.
        """
        row = self.session.execute(
            text(
                """
                UPDATE job_sources
                   SET consecutive_failures = consecutive_failures + 1
                 WHERE id = :id
                RETURNING name, consecutive_failures
                """
            ),
            {"id": source_id},
        ).one()
        if row.consecutive_failures >= CIRCUIT_BREAKER_FAILURES:
            self.session.execute(
                text(
                    """
                    UPDATE job_sources
                       SET enabled = false, disabled_reason = :reason
                     WHERE id = :id
                    """
                ),
                {
                    "id": source_id,
                    "reason": f"circuit breaker after {row.consecutive_failures} failures: {error[:300]}",
                },
            )
            log.error(
                "source.circuit_breaker_open",
                source=row.name,
                consecutive_failures=row.consecutive_failures,
                error=error,
            )

    def _record_success(self, source_id: uuid.UUID) -> None:
        self.session.execute(
            text("UPDATE job_sources SET consecutive_failures = 0 WHERE id = :id"),
            {"id": source_id},
        )
