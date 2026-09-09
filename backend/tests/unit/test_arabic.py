"""Arabic and mixed-script handling (§18, Phase 5).

Most of these are cases that were silently wrong before the module existed:
text that looked identical on screen and compared unequal, numbers that were
numbers to a human and not to `float()`, and skills that a conjunction hid.
"""

from __future__ import annotations

import pytest

from app.domain.safety.anti_invention import check
from app.domain.safety.taxonomy import normalize_token
from app.domain.text.arabic import (
    detect_employment_type,
    detect_language,
    detect_remote_type,
    detect_sections,
    detect_seniority,
    extract_years_of_experience,
    fold_digits,
    is_mixed_script,
    normalize_arabic,
    normalize_for_matching,
    script_shares,
    segment_scripts,
    strip_invisibles,
)

# ── Invisible characters ──────────────────────────────────────────────


def test_bidi_controls_are_removed() -> None:
    """Extractors emit these to preserve visual order; they break every boundary."""
    assert strip_invisibles("‫مهندس‬ بيانات‏") == "مهندس بيانات"


def test_an_invisible_character_cannot_hide_a_skill() -> None:
    """A RLM between the word and its neighbour used to defeat the lookaround."""
    assert "python" in normalize_for_matching("خبرة في ‏Python")


def test_zero_width_joiners_are_removed() -> None:
    assert normalize_arabic("مهند​س") == "مهندس"


# ── Digits ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("٠١٢٣٤٥٦٧٨٩", "0123456789"),  # Arabic-Indic
        ("۰۱۲۳۴۵۶۷۸۹", "0123456789"),  # Extended, used for Persian/Urdu
        ("٥ سنوات", "5 سنوات"),
        ("٢٠٢١", "2021"),
    ],
)
def test_digits_fold_to_ascii(written: str, expected: str) -> None:
    assert fold_digits(written) == expected


def test_a_folded_year_is_a_year() -> None:
    """`٢٠٢١` has to reach the date check as 2021 or it is never checked at all."""
    assert "2021" in normalize_arabic("تخرجت في ٢٠٢١")


def test_the_arabic_decimal_separator_becomes_a_full_stop() -> None:
    assert normalize_arabic("٣٫٥") == "3.5"


# ── Orthographic folding ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("أحمد", "احمد"),  # hamza on alef
        ("إبراهيم", "ابراهيم"),
        ("آلاء", "الاء"),
        ("القاهرة", "القاهره"),  # ta marbuta
        ("مصطفى", "مصطفي"),  # alef maqsura
        ("مُهَنْدِس", "مهندس"),  # tashkeel
        ("مـهـنـدس", "مهندس"),  # tatweel
    ],
)
def test_variants_fold_together(left: str, right: str) -> None:
    """These compare unequal unfolded, and are the same word to any reader."""
    assert normalize_arabic(left) == normalize_arabic(right)


def test_presentation_forms_survive_nfkc() -> None:
    """Older PDF producers emit one code point per glyph shape."""
    assert normalize_for_matching("ﻣﻬﻨﺪﺱ") == normalize_for_matching("مهندس")


# ── Mixed script ──────────────────────────────────────────────────────


def test_a_clitic_does_not_hide_a_latin_skill() -> None:
    """ "وKafka" is "and Kafka". No space, so a word-boundary match found nothing."""
    assert segment_scripts("Python وKafka") == "Python و Kafka"
    assert "kafka" in normalize_for_matching("خبرة في Python وKafka")


def test_the_diff_no_longer_refuses_a_truthful_arabic_sentence() -> None:
    """The failure this fixes was invisible and punished honesty (§10.3)."""
    cv = "خبرة في بناء خطوط المعالجة باستخدام Python وKafka"
    result = check(
        "استخدمت Kafka في بناء خطوط المعالجة.",
        cv=cv,
        known_skills=frozenset({"Kafka", "Python", "Kubernetes"}),
    )
    assert result.is_clean, result.summary()


def test_the_diff_still_catches_invention_in_arabic() -> None:
    """The fold opens boundaries; it does not lower the bar."""
    cv = "خبرة في بناء خطوط المعالجة باستخدام Python وKafka"
    result = check(
        "لدي خبرة واسعة في Kubernetes.",
        cv=cv,
        known_skills=frozenset({"Kafka", "Python", "Kubernetes"}),
    )
    assert not result.is_clean


def test_the_taxonomy_resolves_a_skill_a_conjunction_touches() -> None:
    assert normalize_token("وPython") == "و python"
    assert "python" in normalize_token("وPython").split()


@pytest.mark.parametrize(
    ("text", "mixed"),
    [
        ("مطلوب مهندس بيانات لديه خبرة في Python و Kafka", True),
        ("Senior Data Engineer, Cairo", False),
        ("مهندس برمجيات في القاهرة", False),
        # One Arabic company name in an English posting is not a bilingual ad.
        # Realistic length matters here: the floor is a *share*, so a three-word
        # string tests arithmetic rather than behaviour.
        (
            "Senior Data Engineer at شركة. You will build streaming pipelines, "
            "own the data platform end to end, and work with a distributed team "
            "across three time zones. Python and SQL are required.",
            False,
        ),
    ],
)
def test_mixed_script_detection(text: str, mixed: bool) -> None:
    assert is_mixed_script(text) is mixed


def test_script_shares_sum_to_one_when_there_are_letters() -> None:
    arabic, latin = script_shares("مهندس Data")
    assert arabic + latin == pytest.approx(1.0)
    assert script_shares("2024 — 2025") == (0.0, 0.0)


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("مطلوب مهندس بيانات للعمل في القاهرة", "ar"),
        ("Senior Data Engineer with Python experience", "en"),
        ("مهندس بيانات — Data Engineer — Python, Kafka, SQL", "ar"),
        ("", "en"),
    ],
)
def test_language_detection(text: str, language: str) -> None:
    assert detect_language(text) == language


# ── Vocabulary ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("خبرة ٥ سنوات في تحليل البيانات", 5.0),
        ("خبرة 7 سنوات", 7.0),
        ("خمس سنوات من الخبرة", 5.0),
        ("خبرة سنتين", 2.0),
        ("أكثر من ١٠ سنوات خبرة", 10.0),
        ("مهندس بيانات في القاهرة", None),  # nothing stated → None, never a guess
        ("تخرجت عام ٢٠٢١", None),  # a graduation year is not a duration
    ],
)
def test_years_of_experience(text: str, years: float | None) -> None:
    assert extract_years_of_experience(text) == years


def test_the_longest_experience_claim_wins() -> None:
    """Two figures in one CV describe one career, not two jobs."""
    assert extract_years_of_experience("خبرة ٣ سنوات ... خبرة ٨ سنوات") == 8.0


@pytest.mark.parametrize(
    ("title", "level"),
    [
        ("مهندس برمجيات أول", "senior"),
        ("مطور مبتدئ", "junior"),
        ("مدير تنفيذي", "principal"),  # contains "مدير"; longest phrase must win
        # A manager is a role, not a rung on this ladder — as in English, where
        # "Project Manager" also gets no level rather than a guessed one.
        ("مدير مشروع", None),
        ("قائد فريق البيانات", "staff"),
        ("متدرب تطوير برمجيات", "intern"),
        ("مهندس بيانات", None),  # unstated stays null (§7.1)
    ],
)
def test_seniority_from_an_arabic_title(title: str, level: str | None) -> None:
    assert detect_seniority(title) == level


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("العمل عن بعد", "remote"),
        ("نظام هجين", "hybrid"),
        ("العمل من المكتب", "onsite"),
        ("مهندس بيانات", None),
    ],
)
def test_remote_type_from_arabic(text: str, expected: str | None) -> None:
    assert detect_remote_type(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("دوام كامل", "full_time"),
        ("بدوام جزئي", "part_time"),
        ("عقد لمدة سنة", "contract"),
        ("برنامج تدريبي صيفي", "internship"),
    ],
)
def test_employment_type_from_arabic(text: str, expected: str | None) -> None:
    assert detect_employment_type(text) == expected


def test_arabic_cv_sections_are_detected() -> None:
    cv = """أحمد متاوع — مهندس بيانات

نبذة
مهندس بيانات لديه خبرة في بناء خطوط المعالجة.

الخبرة العملية
- مهندس بيانات، إنستاباج (٢٠٢١–٢٠٢٤)

التعليم
بكالوريوس هندسة الحاسبات، جامعة القاهرة

المهارات
Python، SQL، Apache Kafka

اللغات
العربية (اللغة الأم)، الإنجليزية
"""
    assert detect_sections(cv) == {
        "summary",
        "experience",
        "education",
        "skills",
        "languages",
    }


def test_a_heading_written_with_different_orthography_is_still_found() -> None:
    """ "الخبرة" and "الخبره" are the same heading to every reader."""
    assert "experience" in detect_sections("الخبره")
    assert "experience" in detect_sections("الخبرة")
