"""Title, seniority, date, HTML and location normalisation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.jobs.normalize import (
    detect_language,
    html_to_text,
    infer_remote_type,
    infer_seniority,
    normalize_arabic,
    normalize_title,
    parse_datetime,
    parse_employment_type,
    parse_location,
    title_tokens,
)
from app.domain.models import EmploymentType, RemoteType, Seniority


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Senior Backend Engineer (m/f/d)", "senior backend engineer"),
        ("Data Engineer - Remote [JR-10293]", "data engineer"),
        ("Software Engineer, Full-Time (Cairo)", "software engineer cairo"),
        ("C++ Developer", "c++ developer"),
        ("C# / .NET Engineer", "c# net engineer"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def test_normalize_title_keeps_language_tokens_distinct() -> None:
    """`C++` and `C#` are different jobs; punctuation stripping must not merge them."""
    assert normalize_title("C++ Engineer") != normalize_title("C# Engineer")


def test_title_tokens_gives_the_blocking_head() -> None:
    assert title_tokens(normalize_title("Senior Backend Engineer, Payments"), 3) == (
        "senior",
        "backend",
        "engineer",
    )


def test_arabic_normalisation_folds_orthographic_variants() -> None:
    assert normalize_arabic("مُهندس أول") == normalize_arabic("مهندس اول")


def test_detect_language() -> None:
    assert detect_language("مطلوب مهندس برمجيات للعمل في القاهرة") == "ar"
    assert detect_language("Backend engineer wanted in Cairo") == "en"
    # Mixed script with a Latin majority stays 'en': one Arabic company name
    # does not make an Arabic posting.
    assert detect_language("Backend Engineer at فودافون in Cairo, full stack role") == "en"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Data Engineer", Seniority.SENIOR),
        ("Staff Software Engineer", Seniority.STAFF),
        ("Principal Architect", Seniority.PRINCIPAL),
        ("Junior Developer", Seniority.JUNIOR),
        ("Software Engineering Intern", Seniority.INTERN),
        ("Software Engineer", None),
    ],
)
def test_infer_seniority(title: str, expected: Seniority | None) -> None:
    assert infer_seniority(title) == expected


def test_seniority_is_never_guessed() -> None:
    """An absent level is information the gates need; 'mid' must not be invented."""
    assert infer_seniority("Backend Developer", "We build payment systems.") is None


def test_hybrid_beats_remote() -> None:
    assert infer_remote_type("Remote-friendly hybrid role in Cairo") is RemoteType.HYBRID
    assert infer_remote_type("100% Remote") is RemoteType.REMOTE
    assert infer_remote_type("On-site in Berlin") is RemoteType.ONSITE
    assert infer_remote_type(None, "") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-01T10:00:00Z", datetime(2026, 9, 1, 10, tzinfo=UTC)),
        (1756720800, datetime(2025, 9, 1, 10, tzinfo=UTC)),
        (1756720800000, datetime(2025, 9, 1, 10, tzinfo=UTC)),
        ("", None),
        (None, None),
    ],
)
def test_parse_datetime(value: object, expected: datetime | None) -> None:
    assert parse_datetime(value) == expected


def test_epoch_milliseconds_are_not_read_as_seconds() -> None:
    """Lever emits milliseconds. Read as seconds, every posting looks decades old
    and the freshness gate silently discards the whole source."""
    parsed = parse_datetime(1756720800000)
    assert parsed is not None and parsed.year == 2025


def test_html_to_text_preserves_list_structure() -> None:
    text = html_to_text("<p>We need:</p><ul><li>Python</li><li>SQL</li></ul>")
    assert "• Python" in text and "• SQL" in text
    assert "<li>" not in text


def test_html_to_text_drops_scripts_and_unescapes() -> None:
    text = html_to_text("<script>alert('x')</script><p>R&amp;D team</p>")
    assert "alert" not in text
    assert "R&D team" in text


def test_parse_location() -> None:
    location = parse_location("Cairo, Egypt (Remote)")
    assert location is not None
    assert (location.city, location.country, location.is_remote) == ("Cairo", "EG", True)


def test_parse_location_invents_nothing() -> None:
    location = parse_location("Somewhere Nice")
    assert location is not None and location.country is None


def test_parse_employment_type() -> None:
    assert parse_employment_type("Full-time") is EmploymentType.FULL_TIME
    assert parse_employment_type("Internship") is EmploymentType.INTERNSHIP
    assert parse_employment_type(None) is None


def test_underscores_do_not_survive_normalisation() -> None:
    """`_` is a word character, so the punctuation pass keeps it — and it is a
    single-character wildcard in the SQL LIKE that loads dedup candidates."""
    assert normalize_title("Senior_Backend Engineer") == "senior backend engineer"
    assert "_" not in normalize_title("data_engineer_ii")
