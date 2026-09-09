"""Evidence-span verification — the anti-fabrication mechanism (§10.1)."""

from __future__ import annotations

from app.domain.profile.spans import (
    EvidenceSpan,
    SpanStatus,
    find_span,
    hallucination_rate,
    verify_field,
    verify_span,
)

CV = """Ahmed Metawea
Experience
- Built streaming pipelines in Python and Kafka at Instabug.
- SQL reporting for network operations at Vodafone Egypt.
"""


def test_exact_span_verifies() -> None:
    quote = "Python and Kafka"
    start = CV.index(quote)
    assert verify_span(CV, EvidenceSpan(start, start + len(quote)), quote) is SpanStatus.EXACT


def test_wrong_offsets_are_corrected_not_rejected() -> None:
    """Models re-type quotes accurately and count characters badly."""
    result = verify_field(CV, "Python", "Python and Kafka", EvidenceSpan(0, 16))
    assert result.status is SpanStatus.RELOCATED
    assert result.span is not None
    assert result.span.slice(CV) == "Python and Kafka"


def test_whitespace_and_case_differences_are_tolerated() -> None:
    span = find_span(CV, "python   AND    kafka")
    assert span is not None and span.slice(CV) == "Python and Kafka"


def test_invented_quote_is_not_found() -> None:
    """The whole point: a fabricated claim cannot produce a real span."""
    result = verify_field(CV, "Kubernetes", "operated Kubernetes clusters at scale")
    assert result.status is SpanStatus.NOT_FOUND
    assert result.span is None
    assert not result.is_grounded


def test_empty_quote_is_never_grounded() -> None:
    assert verify_field(CV, "Python", "").status is SpanStatus.NOT_FOUND


def test_span_beyond_the_document_relocates_or_fails() -> None:
    assert verify_span(CV, EvidenceSpan(9000, 9100), "Python and Kafka") is SpanStatus.RELOCATED
    assert verify_span(CV, EvidenceSpan(9000, 9100), "Rust and Erlang") is SpanStatus.NOT_FOUND


def test_span_offsets_index_the_original_text() -> None:
    """Spans highlight the candidate's own CV, so they must index the original,
    not a normalised copy."""
    messy = "Experience\n\n   Built   streaming   pipelines   in   Python\n"
    span = find_span(messy, "Built streaming pipelines in Python")
    assert span is not None
    assert "Built" in messy[span.start : span.end]
    assert messy[span.start : span.end].strip().endswith("Python")


def test_hallucination_rate() -> None:
    fields = [
        verify_field(CV, "Python", "Python and Kafka"),
        verify_field(CV, "SQL", "SQL reporting"),
        verify_field(CV, "Rust", "wrote Rust in production"),
    ]
    assert hallucination_rate(fields) == 1 / 3
    assert hallucination_rate([]) == 0.0


def test_arabic_quotes_verify() -> None:
    arabic_cv = "الخبرات\n- بناء خطوط معالجة البيانات باستخدام بايثون وكافكا في انستاباج."
    assert find_span(arabic_cv, "بايثون وكافكا") is not None
    assert find_span(arabic_cv, "كوبرنيتس") is None
