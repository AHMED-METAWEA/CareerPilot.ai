"""Grounding: extraction is not generation (§10.1, §10.2, §10.5).

The tests that matter here are the ones about what does *not* survive.
"""

from __future__ import annotations

import pytest

from app.domain.models import Seniority
from app.domain.profile.extraction import (
    INSUFFICIENT,
    ExtractedProfile,
    build_extraction_prompt,
    ground_profile,
)
from app.domain.safety.taxonomy import SkillEntry, SkillKind, SkillTaxonomy

CV = """Ahmed Metawea
Senior Data Engineer

Experience
- Data Engineer at Instabug (2021-2024): streaming pipelines in Python and Kafka.
- Analyst at Vodafone Egypt (2019-2021): SQL reporting.

Education
BSc Computer Engineering, Cairo University

Languages
Arabic (native), English (C1)
"""


@pytest.fixture
def taxonomy() -> SkillTaxonomy:
    return SkillTaxonomy(
        [
            SkillEntry("Python", SkillKind.LANGUAGE, ("python3",)),
            SkillEntry("Apache Kafka", SkillKind.TOOL, ("kafka",)),
            SkillEntry("SQL", SkillKind.LANGUAGE, ()),
        ]
    )


def profile(**overrides: object) -> ExtractedProfile:
    base: dict[str, object] = {
        "years_experience": 5,
        "years_experience_quote": "Data Engineer at Instabug (2021-2024)",
        "seniority_level": "senior",
        "seniority_quote": "Senior Data Engineer",
        "skills": [{"name": "Python", "quote": "Python and Kafka"}],
    }
    return ExtractedProfile.model_validate({**base, **overrides})


def test_grounded_fields_survive(taxonomy: SkillTaxonomy) -> None:
    grounded = ground_profile(CV, profile(), taxonomy)
    assert grounded.years_experience == 5
    assert grounded.seniority_level is Seniority.SENIOR
    assert [skill.canonical_name for skill in grounded.skills] == ["Python"]
    assert grounded.discarded == []


def test_an_invented_skill_is_discarded(taxonomy: SkillTaxonomy) -> None:
    """The model cannot invent offsets for a word that is not in the document."""
    extracted = profile(
        skills=[
            {"name": "Python", "quote": "Python and Kafka"},
            {"name": "Kubernetes", "quote": "operated Kubernetes clusters"},
        ]
    )
    grounded = ground_profile(CV, extracted, taxonomy)
    assert [skill.canonical_name for skill in grounded.skills] == ["Python"]
    assert "skill:Kubernetes" in grounded.discarded


def test_a_real_skill_outside_the_taxonomy_is_queued_not_invented(
    taxonomy: SkillTaxonomy,
) -> None:
    """§10.2: an unresolvable token is recorded, never promoted to canonical."""
    extracted = profile(skills=[{"name": "Airflow", "quote": "streaming pipelines in Python"}])
    grounded = ground_profile(CV, extracted, taxonomy)
    assert grounded.skills == []
    assert [match.token for match in grounded.unmapped_skills] == ["Airflow"]
    assert "skill:Airflow" not in grounded.discarded


def test_a_fabricated_work_authorisation_is_discarded(taxonomy: SkillTaxonomy) -> None:
    """Work authorisation drives a hard gate: an unverifiable claim here would
    silently include or exclude entire markets."""
    extracted = profile(
        work_authorization=[{"value": "EU: citizen", "quote": "holds an EU passport"}]
    )
    grounded = ground_profile(CV, extracted, taxonomy)
    assert grounded.work_authorization == []
    assert "work_auth:EU: citizen" in grounded.discarded


def test_abstention_is_a_valid_answer(taxonomy: SkillTaxonomy) -> None:
    """§10.5: a model that declines beats one that guesses."""
    extracted = profile(seniority_level=INSUFFICIENT, seniority_quote="", years_experience=None)
    grounded = ground_profile(CV, extracted, taxonomy)
    assert grounded.seniority_level is None
    assert grounded.years_experience is None
    # Declining is not a hallucination.
    assert grounded.discarded == []


def test_confidence_reflects_what_survived(taxonomy: SkillTaxonomy) -> None:
    extracted = profile(
        skills=[
            {"name": "Python", "quote": "Python and Kafka"},
            {"name": "Rust", "quote": "wrote Rust for three years"},
        ]
    )
    grounded = ground_profile(CV, extracted, taxonomy)
    assert grounded.fields_checked == 4
    assert grounded.hallucination_rate == pytest.approx(0.25)
    assert grounded.confidence == 0.75


def test_prompt_declares_truncation() -> None:
    """A model reading half a CV should know it is reading half a CV."""
    long_cv = "x" * 30000
    assert "truncated" in build_extraction_prompt(long_cv, max_chars=1000)
    assert "truncated" not in build_extraction_prompt("short cv")


def test_schema_ignores_unexpected_fields() -> None:
    """Models add fields. That is not a reason to fail the whole extraction."""
    extracted = ExtractedProfile.model_validate(
        {"years_experience": 3, "confidence_score": 0.9, "notes": "seems good"}
    )
    assert extracted.years_experience == 3
