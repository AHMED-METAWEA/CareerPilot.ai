"""Deduplication against the database (§11.3 W3).

The domain does the clustering; this module decides which postings are even
compared, and writes the result. Comparison scope is the point: a new posting
is only ever compared with postings in its own block, plus anything sharing its
canonical URL. That keeps the work linear in new rows rather than quadratic in
the corpus.

Groups are stable across runs. A cluster that touches an existing job_group
joins it rather than creating a rival, so a posting's group id does not churn
every time an aggregator re-syndicates it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import AppConfig
from app.domain.dedup.clustering import DedupCandidate, cluster_postings
from app.domain.dedup.simhash import from_signed
from app.domain.jobs.normalize import title_tokens
from app.domain.models import ATSPlatform

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class DedupReport:
    considered: int = 0
    compared: int = 0
    clusters: int = 0
    groups_created: int = 0
    groups_joined: int = 0
    merged_postings: int = 0
    """Postings that ended up in a group with at least one other posting."""
    ambiguous_urls: int = 0
    """Canonical URLs ignored because too many postings share them."""
    reasons: dict[str, int] = field(default_factory=dict)


class DedupService:
    def __init__(self, session: Session, config: AppConfig) -> None:
        self.session = session
        self.config = config

    def deduplicate(self, posting_ids: list[uuid.UUID]) -> DedupReport:
        report = DedupReport(considered=len(posting_ids))
        if not posting_ids:
            return report

        seeds = self._load(posting_ids)
        if not seeds:
            return report

        pool = self._load_comparison_pool(seeds)
        report.compared = len(pool)

        ambiguous = self._ambiguous_urls(
            sorted({c.canonical_url for c in pool.values() if c.canonical_url})
        )
        if ambiguous:
            report.ambiguous_urls = len(ambiguous)
            log.warning("dedup.ambiguous_urls_ignored", count=len(ambiguous))
            pool = {
                pid: (
                    replace(candidate, canonical_url=None)
                    if candidate.canonical_url in ambiguous
                    else candidate
                )
                for pid, candidate in pool.items()
            }

        clusters = cluster_postings(
            list(pool.values()),
            simhash_hamming_max=self.config.dedup.simhash_hamming_max,
            title_block_tokens=self.config.dedup.title_block_tokens,
            semantic_threshold=self.config.dedup.semantic_threshold,
            max_postings_per_url=self.config.dedup.max_postings_per_url,
        )
        report.clusters = len(clusters)

        seed_ids = {str(pid) for pid in posting_ids}
        for cluster in clusters:
            # Only write clusters this run actually touched; the pool contains
            # neighbours that are already grouped correctly.
            if not seed_ids.intersection(cluster.member_ids):
                continue
            joined = self._persist(cluster.cluster_key, cluster.member_ids, cluster.canonical_id)
            if joined:
                report.groups_joined += 1
            else:
                report.groups_created += 1
            if len(cluster.member_ids) > 1:
                report.merged_postings += len(cluster.member_ids)
            for reason in cluster.reasons.values():
                report.reasons[reason] = report.reasons.get(reason, 0) + 1

        log.info(
            "dedup.completed",
            considered=report.considered,
            compared=report.compared,
            clusters=report.clusters,
            merged=report.merged_postings,
            reasons=report.reasons,
        )
        return report

    # ── loading ──────────────────────────────────────────────────────

    _SELECT = """
        SELECT p.id, s.name AS source_name, p.external_id, p.company_id,
               p.title_normalized, p.canonical_url, p.content_simhash, p.posted_at,
               length(p.description_text) AS description_length,
               -- City first, falling back to country. The same city arrives
               -- with and without a parsed country depending on the ATS, and a
               -- key of `country|city` would then read 'Austin' and
               -- 'US, Austin' as two different places.
               lower(coalesce(nullif(p.locations->0->>'city', ''),
                              nullif(p.locations->0->>'country', ''), '')) AS location_key,
               p.ats_platform, p.detection_method, p.job_group_id
          FROM job_postings p
          JOIN job_sources s ON s.id = p.source_id
    """

    def _ambiguous_urls(self, urls: list[str]) -> set[str]:
        """URLs that too many postings share, corpus-wide.

        The in-pool guard only sees the pool; this catches a board-page URL whose
        members are spread across many batches.
        """
        if not urls:
            return set()
        rows = (
            self.session.execute(
                text(
                    """
                SELECT canonical_url
                  FROM job_postings
                 WHERE canonical_url = ANY(:urls)
                 GROUP BY canonical_url
                HAVING count(*) > :limit
                """
                ),
                {"urls": urls, "limit": self.config.dedup.max_postings_per_url},
            )
            .scalars()
            .all()
        )
        return set(rows)

    def _load(self, posting_ids: list[uuid.UUID]) -> dict[str, DedupCandidate]:
        rows = self.session.execute(
            text(self._SELECT + " WHERE p.id = ANY(:ids)"), {"ids": posting_ids}
        ).all()
        return {str(row.id): _to_candidate(row) for row in rows}

    def _load_comparison_pool(self, seeds: dict[str, DedupCandidate]) -> dict[str, DedupCandidate]:
        """Seeds plus every open posting that could plausibly be the same job.

        Two lookups, both index-backed: the blocking key (company + title head)
        and the canonical URL. Nothing else is loaded, and nothing outside this
        pool is ever compared.

        The company and prefix lists are matched independently, so a batch
        spanning several employers over-collects slightly. That is deliberate:
        clustering re-blocks on the exact (company, title head) pair anyway, so
        the only cost is a few extra rows, and the alternative is a per-pair
        query.
        """
        n = self.config.dedup.title_block_tokens
        heads = {
            (c.company_key, " ".join(title_tokens(c.title_normalized, n))) for c in seeds.values()
        }
        urls = [c.canonical_url for c in seeds.values() if c.canonical_url]

        pool = dict(seeds)
        company_ids = [uuid.UUID(k) for k, _ in heads if k]
        prefixes = [head for _, head in heads if head]
        if company_ids and prefixes:
            rows = self.session.execute(
                text(
                    self._SELECT
                    + """
                     WHERE p.status = 'open'
                       AND p.company_id = ANY(:company_ids)
                       AND EXISTS (
                             SELECT 1 FROM unnest(CAST(:prefixes AS text[])) AS prefix
                              WHERE p.title_normalized = prefix
                                 OR p.title_normalized LIKE prefix || ' %'
                           )
                    """
                ),
                {"company_ids": company_ids, "prefixes": prefixes},
            ).all()
            for row in rows:
                pool.setdefault(str(row.id), _to_candidate(row))

        if urls:
            rows = self.session.execute(
                text(self._SELECT + " WHERE p.canonical_url = ANY(:urls)"), {"urls": urls}
            ).all()
            for row in rows:
                pool.setdefault(str(row.id), _to_candidate(row))

        return pool

    # ── writing ──────────────────────────────────────────────────────

    def _persist(self, cluster_key: str, member_ids: list[str], canonical_id: str) -> bool:
        """Attach members to a group. Returns True if an existing group was joined."""
        ids = [uuid.UUID(m) for m in member_ids]
        existing = self.session.execute(
            text(
                """
                SELECT g.id
                  FROM job_groups g
                  JOIN job_postings p ON p.job_group_id = g.id
                 WHERE p.id = ANY(:ids)
                 GROUP BY g.id
                 ORDER BY min(g.first_seen_at)
                 LIMIT 1
                """
            ),
            {"ids": ids},
        ).first()

        if existing is not None:
            group_id = existing.id
            joined = True
            self.session.execute(
                text(
                    """
                    UPDATE job_groups
                       SET canonical_posting_id = :canonical,
                           cluster_key = :cluster_key,
                           member_count = :count,
                           last_seen_at = now()
                     WHERE id = :id
                    """
                ),
                {
                    "id": group_id,
                    "canonical": uuid.UUID(canonical_id),
                    "cluster_key": cluster_key,
                    "count": len(ids),
                },
            )
        else:
            joined = False
            group_id = (
                self.session.execute(
                    text(
                        """
                    INSERT INTO job_groups (cluster_key, canonical_posting_id, member_count)
                    VALUES (:cluster_key, :canonical, :count)
                    RETURNING id
                    """
                    ),
                    {
                        "cluster_key": cluster_key,
                        "canonical": uuid.UUID(canonical_id),
                        "count": len(ids),
                    },
                )
                .one()
                .id
            )

        self.session.execute(
            text("UPDATE job_postings SET job_group_id = :group_id WHERE id = ANY(:ids)"),
            {"group_id": group_id, "ids": ids},
        )
        return joined


def _to_candidate(row: object) -> DedupCandidate:
    return DedupCandidate(
        id=str(row.id),  # type: ignore[attr-defined]
        source_name=row.source_name,  # type: ignore[attr-defined]
        external_id=row.external_id,  # type: ignore[attr-defined]
        company_key=str(row.company_id) if row.company_id else None,  # type: ignore[attr-defined]
        title_normalized=row.title_normalized,  # type: ignore[attr-defined]
        canonical_url=row.canonical_url,  # type: ignore[attr-defined]
        # Stored signed (Postgres has no unsigned bigint); the domain works
        # in unsigned space. Masking makes the round trip explicit.
        simhash=from_signed(row.content_simhash)  # type: ignore[attr-defined]
        if row.content_simhash is not None  # type: ignore[attr-defined]
        else None,
        posted_at=row.posted_at,  # type: ignore[attr-defined]
        description_length=row.description_length or 0,  # type: ignore[attr-defined]
        location_key=row.location_key or None,  # type: ignore[attr-defined]
        ats_platform=ATSPlatform(row.ats_platform)  # type: ignore[attr-defined]
        if row.ats_platform  # type: ignore[attr-defined]
        else ATSPlatform.UNKNOWN,
        # 'construction' means the posting came from the employer's own ATS
        # feed — the property canonical_pick cares about (§11.3 stage 7).
        is_ats_native=row.detection_method == "construction",  # type: ignore[attr-defined]
    )
