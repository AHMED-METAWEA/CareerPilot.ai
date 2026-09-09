"""Clustering postings into job_groups (§11.3 stages 1–7).

Ordered cheapest first. Every stage is deterministic and reproducible: no model
decides whether two postings are the same job. The semantic stage is a
tie-breaker only, and never merges on its own where SimHash disagreed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.domain.dedup.blocking import blocking_key
from app.domain.dedup.simhash import hamming
from app.domain.models import ATSPlatform

MIN_SIMHASH_CHARS = 400
"""Below this, SimHash is not trusted on its own.

Measured behaviour: on a full-length description (~350 words) a syndication
trailer moves the hash by 1–2 bits and a genuine rewrite by 9, so a threshold of
3 separates them cleanly. On a 60-word stub the same trailer moves it by 7 —
the distance scales with the *fraction* of the document that changed, not the
number of words. For short postings the pipeline therefore additionally requires
an exact normalised-title match, and relies on the exact-key and canonical-URL
stages, which do not degrade with length."""


@dataclass(frozen=True, slots=True)
class DedupCandidate:
    """The projection of a posting that deduplication actually needs."""

    id: str
    source_name: str
    external_id: str
    company_key: str | None
    title_normalized: str
    canonical_url: str | None
    simhash: int | None
    posted_at: datetime | None = None
    description_length: int = 0
    location_key: str | None = None
    """Normalised primary location, e.g. `eg|cairo`. None when unknown.

    Two postings that differ only in city are two jobs, not one — and that
    difference is a handful of characters, which SimHash cannot see."""
    ats_platform: ATSPlatform = ATSPlatform.UNKNOWN
    is_ats_native: bool = False
    """True when the posting came from the employer's own ATS feed, not an aggregator."""


@dataclass(slots=True)
class Cluster:
    cluster_key: str
    member_ids: list[str]
    canonical_id: str
    reasons: dict[str, str] = field(default_factory=dict)
    """member_id -> the stage that attached it to this cluster."""


class UnionFind:
    """Disjoint-set with path compression and union by size."""

    def __init__(self, items: Sequence[str]) -> None:
        self._parent: dict[str, str] = {i: i for i in items}
        self._size: dict[str, int] = dict.fromkeys(items, 1)

    def find(self, item: str) -> str:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self._size[ra] < self._size[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        self._size[ra] += self._size[rb]
        return True

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for item in self._parent:
            out.setdefault(self.find(item), []).append(item)
        return out


def _locations_compatible(a: str | None, b: str | None) -> bool:
    """False only when both locations are known and differ.

    An unknown location never blocks a merge: absence of evidence is not
    evidence of difference, and remote postings frequently carry none.
    """
    if not a or not b:
        return True
    return a == b


def _is_short_pair(a: DedupCandidate, b: DedupCandidate) -> bool:
    """True when either description is too short for SimHash to be meaningful."""
    return min(a.description_length, b.description_length) < MIN_SIMHASH_CHARS


def canonical_pick(members: Sequence[DedupCandidate]) -> DedupCandidate:
    """Choose the posting a user is sent to (§11.3 stage 7).

    A posting on the employer's own ATS wins outright: its apply URL is
    authoritative and its content is the employer's own. Aggregator copies stay
    in the group as corroborating evidence, never as the link we hand a user.
    Remaining ties break on description completeness, then earliest posting
    date, then id — so the choice is stable across re-runs.
    """
    return sorted(
        members,
        key=lambda c: (
            not c.is_ats_native,
            -c.description_length,
            c.posted_at.timestamp() if c.posted_at else float("inf"),
            c.id,
        ),
    )[0]


def cluster_postings(
    candidates: Sequence[DedupCandidate],
    *,
    simhash_hamming_max: int = 3,
    title_block_tokens: int = 3,
    semantic_threshold: float = 0.94,
    max_postings_per_url: int = 8,
    similarity_fn: Callable[[str, str], float | None] | None = None,
) -> list[Cluster]:
    """Run stages 1–7 and return one cluster per distinct job.

    `similarity_fn(id_a, id_b)` supplies stage 5's cosine when embeddings are
    available; returning None (or omitting the callable) simply skips the
    tie-break, which is the correct behaviour before the embedding worker has
    caught up with a new posting.
    """
    if not candidates:
        return []

    by_id = {c.id: c for c in candidates}
    uf = UnionFind(list(by_id))
    reasons: dict[str, str] = {}

    # The location a cluster has settled on, tracked per root. Comparing pairs
    # alone is not enough: with union-find, one posting that carries no location
    # bridges every city it is individually compatible with, and a role posted
    # in 28 cities collapses into one group. The invariant is enforced on the
    # cluster, not the pair — a cluster may hold at most one known location.
    cluster_location: dict[str, str | None] = {c.id: c.location_key for c in candidates}

    def merge(a: str, b: str, reason: str) -> None:
        if uf.union(a, b):
            reasons.setdefault(b, reason)
            reasons.setdefault(a, reason)
            root = uf.find(a)
            cluster_location[root] = cluster_location.get(a) or cluster_location.get(b)

    def text_merge_allowed(a: DedupCandidate, b: DedupCandidate) -> bool:
        """Location check for the text stages, applied at cluster level."""
        return _locations_compatible(
            cluster_location.get(uf.find(a.id)), cluster_location.get(uf.find(b.id))
        )

    # ── Stage 1: exact source key ────────────────────────────────────
    by_source_key: dict[tuple[str, str], list[str]] = {}
    for c in candidates:
        by_source_key.setdefault((c.source_name, c.external_id), []).append(c.id)
    for ids in by_source_key.values():
        for other in ids[1:]:
            merge(ids[0], other, "exact_key")

    # ── Stage 2: canonical URL (an exact lookup, not a comparison) ───
    by_url: dict[str, list[str]] = {}
    for c in candidates:
        if c.canonical_url:
            by_url.setdefault(c.canonical_url, []).append(c.id)
    for ids in by_url.values():
        # A URL shared by many postings is a board page, not a job page, and it
        # identifies nothing. Some employers render their board on their own
        # domain with the job id in a query parameter; lose that parameter and
        # every posting collapses to one URL. Trusting it merged 870 unrelated
        # jobs into one group before this guard existed.
        if len(ids) > max_postings_per_url:
            continue
        for other in ids[1:]:
            merge(ids[0], other, "canonical_url")

    # ── Stage 3: blocking ────────────────────────────────────────────
    buckets: dict[str, list[DedupCandidate]] = {}
    for c in candidates:
        key = blocking_key(c.company_key, c.title_normalized, title_tokens_n=title_block_tokens)
        buckets.setdefault(key, []).append(c)

    # ── Stages 4 and 5: near-duplicate, then semantic tie-break ──────
    for members in buckets.values():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if uf.find(a.id) == uf.find(b.id):
                    continue
                # Text similarity exists to bridge *sources*. Two rows from one
                # source with different external ids are two requisitions the
                # employer published separately — and templated job ads make
                # them nearly identical text: "Sr. FDE — Retail" and "Sr. FDE —
                # Healthcare" differ by two words in 8,000 characters. Stage 1
                # already settles same-source identity, and stage 2 still merges
                # them if they truly resolve to one page.
                if a.source_name == b.source_name:
                    continue
                # Both text stages are guarded by location: a city change is a
                # handful of characters and a different job.
                if not text_merge_allowed(a, b):
                    continue
                if _is_short_pair(a, b):
                    # SimHash cannot separate a truncated aggregator stub from a
                    # different short posting, so the decision falls back to the
                    # evidence that does not degrade with length: an exact
                    # normalised title, within an already company-blocked bucket.
                    # Recorded honestly as `title_exact`, not as `simhash`.
                    if a.title_normalized == b.title_normalized:
                        merge(a.id, b.id, "title_exact")
                    continue
                if (
                    a.simhash is not None
                    and b.simhash is not None
                    and hamming(a.simhash, b.simhash) <= simhash_hamming_max
                ):
                    merge(a.id, b.id, "simhash")
                    continue
                if similarity_fn is not None:
                    score = similarity_fn(a.id, b.id)
                    if score is not None and score > semantic_threshold:
                        merge(a.id, b.id, "semantic")

    # ── Stages 6 and 7: union-find groups, then canonical pick ───────
    clusters: list[Cluster] = []
    for member_ids in uf.groups().values():
        members = [by_id[m] for m in member_ids]
        canonical = canonical_pick(members)
        clusters.append(
            Cluster(
                cluster_key=cluster_key_for(canonical, title_block_tokens),
                member_ids=sorted(member_ids),
                canonical_id=canonical.id,
                reasons={m: reasons.get(m, "singleton") for m in sorted(member_ids)},
            )
        )
    clusters.sort(key=lambda c: c.canonical_id)
    return clusters


def cluster_key_for(canonical: DedupCandidate, title_block_tokens: int = 3) -> str:
    """A human-readable, stable key for the group — derived from its canonical member."""
    return blocking_key(
        canonical.company_key, canonical.title_normalized, title_tokens_n=title_block_tokens
    )
