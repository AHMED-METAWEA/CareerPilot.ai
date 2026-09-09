"""Company entity resolution against the database (§11.2 step 5, §11.3).

The domain decides *whether* two employer references are the same; this module
supplies it with the known set and records the outcome — including the review
queue row that a plausible-but-unproven match generates. Resolution never
silently merges: an ambiguous reference gets its own company row plus a queue
entry, so no posting is left without an employer and no two employers are
fused on a guess.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.dedup.entities import KnownCompany, normalize_company_name, resolve_company
from app.domain.jobs.urls import company_domain, is_ats_host
from app.domain.models import CompanyRef, CompanyResolution

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class CompanyDirectory:
    """Known employers, loaded once per run and kept in memory.

    At Phase 0 scale (hundreds of employers) this is a few hundred kilobytes and
    removes a query per posting. When the directory outgrows memory the same
    interface can be backed by a blocked lookup without touching the caller.
    """

    session: Session
    companies: list[KnownCompany] = field(default_factory=list)
    _by_key: dict[str, uuid.UUID] = field(default_factory=dict)
    _cache: dict[str, tuple[uuid.UUID, CompanyResolution]] = field(default_factory=dict)
    """Resolutions already made in this run, keyed by normalised name + domain.

    A board is usually one employer repeated a few hundred times, and matching is
    a full scan of the directory, so without this the run is O(postings ×
    companies) fuzzy comparisons to answer the same question every time."""

    @classmethod
    def load(cls, session: Session) -> CompanyDirectory:
        rows = session.execute(
            text(
                """
                SELECT c.id, c.canonical_name, c.domain,
                       COALESCE(array_agg(a.alias) FILTER (WHERE a.alias IS NOT NULL), '{}') AS aliases
                  FROM companies c
                  LEFT JOIN company_aliases a ON a.company_id = c.id
                 GROUP BY c.id, c.canonical_name, c.domain
                """
            )
        ).all()
        directory = cls(session=session)
        for row in rows:
            key = str(row.id)
            directory.companies.append(
                KnownCompany(
                    key=key,
                    canonical_name=row.canonical_name,
                    domain=row.domain,
                    aliases=tuple(row.aliases or ()),
                )
            )
            directory._by_key[key] = row.id
        return directory

    # ── resolution ───────────────────────────────────────────────────

    def resolve_or_create(
        self,
        ref: CompanyRef,
        *,
        source_id: uuid.UUID | None,
        fuzzy_threshold: float,
        review_threshold: float,
    ) -> tuple[uuid.UUID, CompanyResolution]:
        cache_key = f"{normalize_company_name(ref.name)}|{ref.domain or ''}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        resolution = resolve_company(
            ref,
            self.companies,
            fuzzy_threshold=fuzzy_threshold,
            review_threshold=review_threshold,
        )

        if resolution.company_key is not None:
            company_id = self._by_key[resolution.company_key]
            self._cache[cache_key] = (company_id, resolution)
            return company_id, resolution

        company_id, created = self._create(ref)
        # Cached before the review row is written, so the same unresolved
        # employer seen again in this run reuses the row rather than creating a
        # second one and queueing a duplicate review.
        self._cache[cache_key] = (company_id, resolution)

        if resolution.needs_review or not created:
            # `not created` means an existing row already held this domain — a
            # merge decision that resolution did not make, so a human sees it.
            self._queue_review(ref, resolution, source_id=source_id, created_id=company_id)

        return company_id, resolution

    def _create(self, ref: CompanyRef) -> tuple[uuid.UUID, bool]:
        """Insert an employer row. Returns (id, created).

        `created=False` means another row already claimed this domain. That is a
        merge, and merges are never silent here — the caller queues a review.
        """
        domain = company_domain(ref.domain, ref.careers_url)
        careers_url = None if is_ats_host(ref.careers_url) else ref.careers_url
        row = self.session.execute(
            text(
                """
                INSERT INTO companies (canonical_name, domain, careers_url)
                VALUES (:name, :domain, :careers_url)
                ON CONFLICT (domain) DO UPDATE SET canonical_name = companies.canonical_name
                RETURNING id, (xmax = 0) AS created
                """
            ),
            {"name": ref.name.strip(), "domain": domain, "careers_url": careers_url},
        ).one()
        company_id: uuid.UUID = row.id
        key = str(company_id)
        # Register immediately so later postings in the same run resolve by alias.
        self.companies.append(
            KnownCompany(key=key, canonical_name=ref.name.strip(), domain=domain, aliases=())
        )
        self._by_key[key] = company_id
        return company_id, bool(row.created)

    def _queue_review(
        self,
        ref: CompanyRef,
        resolution: CompanyResolution,
        *,
        source_id: uuid.UUID | None,
        created_id: uuid.UUID,
    ) -> None:
        suggested = self._by_key.get(resolution.company_key) if resolution.company_key else None
        if suggested is None and resolution.matched_alias:
            suggested = self._lookup_by_alias(resolution.matched_alias)
        self.session.execute(
            text(
                """
                INSERT INTO company_review_queue
                    (observed_name, observed_domain, source_id, suggested_company_id,
                     confidence, matched_alias)
                VALUES (:name, :domain, :source_id, :suggested, :confidence, :alias)
                ON CONFLICT (observed_name, source_id)
                DO UPDATE SET occurrences = company_review_queue.occurrences + 1
                """
            ),
            {
                "name": ref.name.strip(),
                "domain": ref.domain,
                "source_id": source_id,
                "suggested": suggested,
                "confidence": round(resolution.confidence, 3),
                "alias": resolution.matched_alias,
            },
        )
        log.info(
            "company.review_queued",
            observed=ref.name,
            matched_alias=resolution.matched_alias,
            confidence=round(resolution.confidence, 3),
            created_company_id=str(created_id),
        )

    def _lookup_by_alias(self, alias: str) -> uuid.UUID | None:
        normalized = normalize_company_name(alias)
        for company in self.companies:
            for candidate in (company.canonical_name, *company.aliases):
                if normalize_company_name(candidate) == normalized:
                    return self._by_key[company.key]
        return None
