"""Clustering stages 1–7 (§11.3)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.dedup.clustering import (
    DedupCandidate,
    UnionFind,
    canonical_pick,
    cluster_postings,
)
from app.domain.dedup.simhash import simhash64
from app.domain.models import ATSPlatform

LONG = (
    "We are looking for a senior backend engineer to join our payments team. "
    "You will design and operate distributed services in Python and Go, own "
    "reliability for a high-throughput ledger, and mentor engineers. "
) * 4


def candidate(
    id_: str,
    *,
    source: str = "greenhouse:acme",
    external_id: str | None = None,
    company: str | None = "acme",
    title: str = "senior backend engineer",
    url: str | None = None,
    text: str = LONG,
    native: bool = True,
    posted: datetime | None = None,
    location: str | None = "cairo",
) -> DedupCandidate:
    return DedupCandidate(
        id=id_,
        source_name=source,
        external_id=external_id or id_,
        company_key=company,
        title_normalized=title,
        canonical_url=url,
        simhash=simhash64(text),
        posted_at=posted or datetime(2026, 9, 1, tzinfo=UTC),
        description_length=len(text),
        ats_platform=ATSPlatform.GREENHOUSE if native else ATSPlatform.UNKNOWN,
        is_ats_native=native,
        location_key=location,
    )


def test_union_find_groups_transitively() -> None:
    uf = UnionFind(["a", "b", "c", "d"])
    uf.union("a", "b")
    uf.union("b", "c")
    groups = {frozenset(v) for v in uf.groups().values()}
    assert groups == {frozenset({"a", "b", "c"}), frozenset({"d"})}


def test_same_canonical_url_merges_across_sources() -> None:
    """Stage 2 is an exact lookup, so it merges even across blocks."""
    a = candidate("a", url="https://boards.greenhouse.io/acme/jobs/1")
    b = candidate(
        "b",
        source="remotive",
        company="acme",
        title="backend engineer senior",
        url="https://boards.greenhouse.io/acme/jobs/1",
        native=False,
    )
    clusters = cluster_postings([a, b])
    assert len(clusters) == 1
    assert clusters[0].reasons["b"] == "canonical_url"


def test_near_duplicate_descriptions_merge_within_a_block() -> None:
    a = candidate("a", url="https://x/1")
    b = candidate(
        "b", source="remotive", text=LONG + " No agencies please.", native=False, url="https://y/1"
    )
    clusters = cluster_postings([a, b])
    assert len(clusters) == 1
    assert clusters[0].reasons["b"] == "simhash"


def test_different_companies_never_compare() -> None:
    """Blocking confines comparison; identical text at two employers is two jobs."""
    a = candidate("a", company="acme", url="https://x/1")
    b = candidate("b", company="globex", url="https://y/1")
    assert len(cluster_postings([a, b])) == 2


def test_same_title_different_city_does_not_merge() -> None:
    """A city change is a few characters of text and a different job."""
    a = candidate("a", location="cairo", url="https://x/1")
    b = candidate("b", source="remotive", location="dubai", native=False, url="https://y/1")
    assert len(cluster_postings([a, b])) == 2


def test_unknown_location_does_not_block_a_merge() -> None:
    a = candidate("a", location=None, url="https://x/1")
    b = candidate(
        "b",
        source="remotive",
        location="cairo",
        native=False,
        text=LONG + " Apply today.",
        url="https://y/1",
    )
    assert len(cluster_postings([a, b])) == 1


def test_short_descriptions_fall_back_to_exact_title() -> None:
    """SimHash distance scales with the fraction of text that differs, so a stub
    posting cannot be judged at the same threshold as a full description. The
    fallback is the evidence that does not degrade with length."""
    stub = "Backend engineer wanted in Cairo. Apply now."
    a = candidate("a", text=stub, title="backend engineer", url="https://x/1")
    b = candidate(
        "b",
        source="remotive",
        native=False,
        text=stub + " Immediate start.",
        title="backend engineer ii",
        url="https://y/1",
    )
    assert len(cluster_postings([a, b])) == 2

    c = candidate(
        "c",
        source="remotive",
        native=False,
        text=stub + " Immediate start.",
        title="backend engineer",
        url="https://z/1",
    )
    clusters = cluster_postings([a, c])
    assert len(clusters) == 1
    # Recorded as what actually decided it, not as a SimHash match.
    assert clusters[0].reasons["c"] == "title_exact"


def test_canonical_pick_prefers_the_employers_own_ats() -> None:
    aggregator = candidate("agg", source="remotive", native=False, text=LONG + " extra text here")
    native = candidate("native", native=True)
    assert canonical_pick([aggregator, native]).id == "native"


def test_canonical_pick_breaks_ties_deterministically() -> None:
    short = candidate("a", text=LONG)
    full = candidate("b", text=LONG + " Additional responsibilities and benefits.")
    assert canonical_pick([short, full]).id == "b"  # longest description wins
    assert canonical_pick([full, short]).id == "b"  # order-independent


def test_exact_source_key_merges_duplicate_rows() -> None:
    a = candidate("a", external_id="42", url="https://x/1")
    b = candidate("b", external_id="42", url="https://y/1")
    clusters = cluster_postings([a, b])
    assert len(clusters) == 1 and clusters[0].reasons["b"] == "exact_key"


def test_empty_input() -> None:
    assert cluster_postings([]) == []


def test_a_url_shared_by_many_postings_never_merges_them() -> None:
    """A board-page URL identifies nothing.

    Some employers render their Greenhouse board on their own domain with the job
    id in a query parameter. If that parameter is lost anywhere upstream, every
    posting collapses to one URL — and a URL-equality merge would then fold the
    entire board into one group.
    """
    board_url = "https://acme.com/careers/job"
    crowd = [
        candidate(
            f"p{i}", external_id=f"e{i}", title=f"role {i}", url=board_url, text=LONG + f" {i}"
        )
        for i in range(12)
    ]
    clusters = cluster_postings(crowd, max_postings_per_url=8)
    assert len(clusters) == 12

    # Below the threshold, URL equality is still trusted.
    few = crowd[:3]
    assert len(cluster_postings(few, max_postings_per_url=8)) == 1


def test_blocking_groups_by_company_and_title_head() -> None:
    from app.domain.dedup.blocking import block, blocking_key

    items = [
        ("acme", "senior backend engineer payments"),
        ("acme", "senior backend engineer platform"),
        ("acme", "product designer"),
        ("globex", "senior backend engineer payments"),
        (None, "senior backend engineer payments"),
    ]
    buckets = block(items, lambda item: blocking_key(item[0], item[1]))

    # Same employer and same three-token head share a bucket; a different
    # employer never does, and an unresolved employer sits with other unknowns.
    assert len(buckets["acme|senior backend engineer"]) == 2
    assert len(buckets["globex|senior backend engineer"]) == 1
    assert len(buckets["?|senior backend engineer"]) == 1
    assert len(buckets["acme|product designer"]) == 1


def test_a_location_less_posting_cannot_bridge_two_cities() -> None:
    """The location guard has to hold for the cluster, not just the pair.

    Found in the real corpus: one posting of a role carried no location, and
    union-find used it as a bridge — 45 postings of the same title across 28 US
    cities ended up in a single group.
    """
    cairo = candidate("cairo", location="cairo", url="https://x/1")
    dubai = candidate("dubai", source="remotive", location="dubai", native=False, url="https://x/2")
    nowhere = candidate(
        "nowhere", source="arbeitnow", location=None, native=False, url="https://x/3"
    )

    clusters = cluster_postings([cairo, nowhere, dubai])

    # The location-less posting joins exactly one cluster; the two cities stay apart.
    assert len(clusters) == 2
    groups = {frozenset(c.member_ids) for c in clusters}
    assert frozenset({"cairo", "dubai"}) not in groups
    assert any("nowhere" in members for members in groups)


def test_two_location_less_postings_still_merge() -> None:
    a = candidate("a", location=None, url="https://x/1")
    b = candidate(
        "b",
        source="remotive",
        location=None,
        native=False,
        text=LONG + " Apply today.",
        url="https://x/2",
    )
    assert len(cluster_postings([a, b])) == 1


def test_two_postings_from_one_source_never_merge_on_text() -> None:
    """Text similarity exists to bridge sources, not to collapse a board.

    Found in the real corpus: an employer's templated ads for "Sr. FDE — Retail"
    and "Sr. FDE — Healthcare" differ by two words in 8,000 characters, so
    SimHash reads them as the same posting. They are two requisitions with two
    apply URLs, and stage 1 already settles same-source identity.
    """
    retail = candidate("retail", external_id="1", title="sr fde retail", text=LONG + " Retail.")
    health = candidate("health", external_id="2", title="sr fde healthcare", text=LONG + " Health.")
    assert len(cluster_postings([retail, health])) == 2

    # The same pair across two sources is exactly what dedup is for.
    syndicated = candidate(
        "syndicated",
        source="remotive",
        native=False,
        external_id="9",
        title="sr fde retail",
        text=LONG + " Retail.",
    )
    assert len(cluster_postings([retail, syndicated])) == 1
