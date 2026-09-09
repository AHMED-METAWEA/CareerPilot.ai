"""Requirement extraction from job descriptions (§11.4 stage 5)."""

from __future__ import annotations

from app.domain.jobs.requirements import (
    ExtractedRequirements,
    classify_kind,
    extract_requirements_heuristic,
    ground_requirements,
    sponsorship_stance,
    stated_min_years,
)

JD = """About the role
You will own our billing platform end to end.

Requirements
• 5+ years of experience building backend services in Python or Go
• Strong experience with PostgreSQL and distributed systems
• Fluent English is required for daily collaboration
• Bachelor's degree in Computer Science or equivalent experience

Nice to have
• Experience with Kubernetes
• Familiarity with dbt is a plus

We are unable to sponsor visas for this position.
"""


def test_requirements_are_separated_from_responsibilities() -> None:
    requirements = extract_requirements_heuristic(JD)
    texts = [requirement.text for requirement, _ in requirements]
    assert not any("own our billing platform" in text for text in texts)
    assert any("5+ years" in text for text in texts)


def test_section_context_decides_must_have() -> None:
    """A bullet under 'Nice to have' is a nice-to-have however it is worded."""
    requirements = {
        requirement.text: requirement.is_must_have
        for requirement, _ in extract_requirements_heuristic(JD)
    }
    assert requirements["5+ years of experience building backend services in Python or Go"]
    assert not requirements["Experience with Kubernetes"]


def test_trailing_prose_is_not_a_requirement() -> None:
    """Regression: 'We are unable to sponsor visas' was captured as a
    requirement because the section context outlived the bullet list."""
    texts = [requirement.text for requirement, _ in extract_requirements_heuristic(JD)]
    assert not any("sponsor visas" in text for text in texts)


def test_spans_point_into_the_description() -> None:
    for requirement, span in extract_requirements_heuristic(JD):
        assert span is not None
        assert JD[span.start : span.end].strip().startswith(requirement.text[:20])


def test_kind_classification() -> None:
    assert classify_kind("5+ years of Python") == "experience"
    assert classify_kind("Bachelor's degree in Computer Science") == "education"
    assert classify_kind("Fluent English required") == "language"
    assert classify_kind("Must have the right to work in Germany") == "auth"
    assert classify_kind("Experience with Kafka") == "skill"


def test_min_years_takes_the_floor_not_the_aspiration() -> None:
    """'3+ years, ideally 5' has a floor of three; the gate must not fire on
    the aspiration."""
    assert stated_min_years("3+ years required, ideally 5 years") == 3.0
    assert stated_min_years("No experience figure here") is None
    assert stated_min_years("40 years of company history") is None  # implausible as a floor


def test_sponsorship_stance_distinguishes_silence_from_refusal() -> None:
    """Gating on silence would hide most of the corpus from candidates who need
    a visa."""
    assert sponsorship_stance(JD) is False
    assert sponsorship_stance("Visa sponsorship is available for this role") is True
    assert sponsorship_stance("A normal job description saying nothing about it") is None


def test_model_extracted_requirements_must_be_quoted() -> None:
    extracted = ExtractedRequirements.model_validate(
        {
            "requirements": [
                {"text": "5 years of Python", "quote": "5+ years of experience building backend"},
                {"text": "PhD in astrophysics", "quote": "we require a doctorate in astrophysics"},
            ]
        }
    )
    grounded = ground_requirements(JD, extracted)
    assert [requirement.text for requirement, _ in grounded] == ["5 years of Python"]


def test_arabic_requirement_markers() -> None:
    arabic = """المتطلبات
• مطلوب خبرة 3 سنوات في بايثون
• يشترط إجادة اللغة الإنجليزية
"""
    requirements = extract_requirements_heuristic(arabic)
    assert len(requirements) == 2
    assert all(requirement.is_must_have for requirement, _ in requirements)
