"""Adapters against recorded payloads (§14, tests/fixtures).

Each fixture is a real response captured from the live endpoint, trimmed to two
postings. The point of these tests is not that the code runs — it is that each
adapter reads *its own* container shape and *its own* field vocabulary, so a
vendor changing one cannot silently take the others down with it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from app.adapters.sources import build_adapter
from app.adapters.sources.base import BaseSourceAdapter
from app.domain.models import ATSPlatform, DetectionMethod
from tests.conftest import load_fixture

CASES = [
    (
        "greenhouse",
        {"board_token": "vercel", "company_name": "Vercel"},
        "https://boards-api.greenhouse.io/v1/boards/vercel/jobs",
        ("greenhouse", "board.json"),
    ),
    (
        "lever",
        {"company": "swile", "company_name": "Swile"},
        "https://api.lever.co/v0/postings/swile",
        ("lever", "board.json"),
    ),
    (
        "ashby",
        {"org": "linear", "company_name": "Linear"},
        "https://api.ashbyhq.com/posting-api/job-board/linear",
        ("ashby", "board.json"),
    ),
    (
        "workable",
        {"subdomain": "robusta", "company_name": "Robusta"},
        "https://apply.workable.com/api/v1/widget/accounts/robusta",
        ("workable", "board.json"),
    ),
    (
        "recruitee",
        {"company": "channable", "company_name": "Channable"},
        "https://channable.recruitee.com/api/offers/",
        ("recruitee", "board.json"),
    ),
    (
        "smartrecruiters",
        {"company_id": "McDonaldsCorporation", "company_name": "McDonald's"},
        "https://api.smartrecruiters.com/v1/companies/McDonaldsCorporation/postings",
        ("smartrecruiters", "board.json"),
    ),
]


def build(adapter: str, config: dict[str, Any], http: Any) -> BaseSourceAdapter:
    return build_adapter(
        adapter, name=f"{adapter}:test", config=config, http=http, rate_limit_rpm=600
    )


@pytest.mark.parametrize(("adapter", "config", "url", "fixture"), CASES)
@respx.mock
def test_adapter_normalises_recorded_payload(
    adapter: str, config: dict[str, Any], url: str, fixture: tuple[str, str], http_client: Any
) -> None:
    respx.get(url__startswith=url).mock(
        return_value=httpx.Response(200, json=load_fixture(*fixture))
    )
    source = build(adapter, config, http_client)

    raws = list(source.fetch())
    assert raws, f"{adapter}: fetched zero postings from a payload that has some"

    for raw in raws:
        job = source.normalize(raw)
        assert job.title.strip()
        assert job.external_id
        assert job.apply_url.startswith("http"), f"{adapter}: apply_url must be resolvable"
        assert job.canonical_url
        assert job.company.name
        assert job.title_normalized
        assert job.ats_platform is not ATSPlatform.UNKNOWN
        # Tier 1: the posting came from that ATS's own API (Appendix C, rule 1).
        assert job.ats_confidence == 1.00
        assert job.detection_method is DetectionMethod.CONSTRUCTION
        assert job.content_simhash is not None
        assert job.language in {"en", "ar"}


@respx.mock
def test_lever_returns_a_bare_array(http_client: Any) -> None:
    """Regression test named in the Phase 0 exit criteria.

    Lever answers with a JSON array, not an object. `payload.get("jobs", [])`
    against this endpoint yields zero rows, silently, for as long as nobody
    looks at the source's own history (§5.2).
    """
    payload = load_fixture("lever", "board.json")
    assert isinstance(payload, list), "fixture itself must be a bare array"

    respx.get(url__startswith="https://api.lever.co/v0/postings/swile").mock(
        return_value=httpx.Response(200, json=payload)
    )
    source = build("lever", {"company": "swile"}, http_client)
    assert len(list(source.fetch())) == len(payload)


@respx.mock
def test_lever_object_response_fails_loudly(http_client: Any) -> None:
    """If Lever ever wraps its results, the run must fail, not return nothing."""
    respx.get(url__startswith="https://api.lever.co/v0/postings/swile").mock(
        return_value=httpx.Response(200, json={"postings": []})
    )
    source = build("lever", {"company": "swile"}, http_client)
    with pytest.raises(ValueError, match="expected a JSON array"):
        list(source.fetch())


@respx.mock
def test_lever_reads_title_from_text_field(http_client: Any) -> None:
    """Lever's title field is `text`, not `title`."""
    payload = load_fixture("lever", "board.json")
    respx.get(url__startswith="https://api.lever.co").mock(
        return_value=httpx.Response(200, json=payload)
    )
    source = build("lever", {"company": "swile"}, http_client)
    job = source.normalize(next(iter(source.fetch())))
    assert job.title == payload[0]["text"]


@respx.mock
def test_workable_shape_change_fails_loudly(http_client: Any) -> None:
    """The Workable widget endpoint is undocumented and may change without
    notice (R3). A shape change must raise, so source health alerts instead of
    the run quietly recording zero rows."""
    respx.get(url__startswith="https://apply.workable.com").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    source = build("workable", {"subdomain": "robusta"}, http_client)
    with pytest.raises(ValueError, match="unexpected Workable widget payload"):
        list(source.fetch())


@respx.mock
def test_smartrecruiters_does_not_fetch_details_during_a_run(http_client: Any) -> None:
    """A 4,800-posting board must not become 4,800 requests in one run."""
    detail_route = respx.get(url__regex=r".*/postings/[\w-]+$").mock(
        return_value=httpx.Response(200, json=load_fixture("smartrecruiters", "detail.json"))
    )
    listing = respx.get(
        url__startswith="https://api.smartrecruiters.com/v1/companies/McDonaldsCorporation/postings"
    ).mock(return_value=httpx.Response(200, json=load_fixture("smartrecruiters", "board.json")))

    source = build("smartrecruiters", {"company_id": "McDonaldsCorporation"}, http_client)
    raws = list(source.fetch())

    assert raws
    assert listing.called
    # The property under test: no request per posting during a run.
    assert detail_route.call_count == 0
    assert all("_detail" not in raw.payload for raw in raws)


@respx.mock
def test_smartrecruiters_detail_phase_supplies_the_body(http_client: Any) -> None:
    detail = load_fixture("smartrecruiters", "detail.json")
    respx.get(url__regex=r".*/postings/[\w-]+$").mock(return_value=httpx.Response(200, json=detail))
    source = build("smartrecruiters", {"company_id": "McDonaldsCorporation"}, http_client)
    fetched = source.fetch_detail(detail["id"])  # type: ignore[attr-defined]

    assert fetched is not None
    body = source.describe(fetched)  # type: ignore[attr-defined]
    assert len(body) > 100
    assert "<" not in body  # HTML flattened, not passed through


@respx.mock
def test_ashby_reads_structured_compensation(http_client: Any) -> None:
    """Ashby is the one Tier 1 source publishing salary; it is read, not inferred."""
    respx.get(url__startswith="https://api.ashbyhq.com").mock(
        return_value=httpx.Response(200, json=load_fixture("ashby", "board.json"))
    )
    source = build("ashby", {"org": "linear"}, http_client)
    jobs = [source.normalize(raw) for raw in source.fetch()]
    priced = [j for j in jobs if j.salary_min is not None or j.salary_max is not None]
    for job in priced:
        assert job.currency, "a salary without a currency is not a salary"


@respx.mock
def test_304_not_modified_yields_no_postings(http_client: Any) -> None:
    """Conditional requests are used where the origin supports them (§5.6)."""
    respx.get(url__startswith="https://boards-api.greenhouse.io").mock(
        return_value=httpx.Response(304)
    )
    source = build("greenhouse", {"board_token": "vercel"}, http_client)
    assert list(source.fetch()) == []


@respx.mock
def test_health_reports_unreachable_without_raising(http_client: Any) -> None:
    respx.get(url__startswith="https://boards-api.greenhouse.io").mock(
        return_value=httpx.Response(500)
    )
    source = build("greenhouse", {"board_token": "nope"}, http_client)
    health = source.health()
    assert health.reachable is False and health.detail
