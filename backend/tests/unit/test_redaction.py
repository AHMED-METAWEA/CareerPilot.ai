"""PII redaction before hosted inference (§11.1 step 6, §16.2)."""

from __future__ import annotations

import pytest

from app.domain.safety.redaction import assert_no_pii, redact

CV = """Ahmed Metawea
ahmed.metawea001@gmail.com | +20 100 123 4567
12 Tahrir Street, Cairo
linkedin.com/in/ahmedmetawea

Experience
- Data Engineer at Instabug: Python, Kafka, Airflow.
"""


def test_contact_details_are_removed() -> None:
    result = redact(CV)
    assert "ahmed.metawea001@gmail.com" not in result.text
    assert "+20 100 123 4567" not in result.text
    assert "Tahrir Street" not in result.text
    assert "linkedin.com/in/ahmedmetawea" not in result.text


def test_professional_content_survives() -> None:
    """Over-redaction costs extraction quality; the CV must still be a CV."""
    result = redact(CV)
    for kept in ("Data Engineer", "Instabug", "Python", "Kafka", "Airflow"):
        assert kept in result.text


def test_name_is_removed_everywhere_it_appears() -> None:
    text = "Ahmed Metawea\nReferences available. Contact Ahmed Metawea for details."
    result = redact(text)
    assert "Ahmed Metawea" not in result.text
    assert result.text.count("[[NAME_1]]") == 2


def test_mapping_restores_the_original() -> None:
    result = redact(CV)
    assert result.mapping.restore(result.text) == CV


def test_repeated_value_gets_one_placeholder() -> None:
    text = "a@b.com wrote to a@b.com"
    result = redact(text)
    assert result.text.count("[[EMAIL_1]]") == 2
    assert len(result.mapping) == 1


def test_guard_reports_what_redaction_missed() -> None:
    """A redaction that silently misses is worse than one that fails loudly."""
    assert assert_no_pii(redact(CV).text) == []
    assert "EMAIL" in assert_no_pii("reach me at ahmed@example.com")


@pytest.mark.parametrize(
    "phone",
    ["+20 100 123 4567", "(555) 010-9999", "0100-123-4567", "+44 20 7946 0958"],
)
def test_phone_formats(phone: str) -> None:
    result = redact(f"Ahmed Metawea\nCall {phone} any time.")
    assert phone not in result.text


def test_national_id_is_removed() -> None:
    result = redact("Ahmed Metawea\nNational ID: 29001011234567")
    assert "29001011234567" not in result.text


def test_arabic_name_line() -> None:
    result = redact("أحمد متاوع\nمهندس بيانات\nالبريد: ahmed@example.com")
    assert "ahmed@example.com" not in result.text
    assert "أحمد متاوع" not in result.text


def test_metrics_are_not_mistaken_for_phone_numbers() -> None:
    """An achievement figure is the substance of a CV bullet.

    `4000000 events per day` is a metric; `01001234567` is a mobile number. The
    separator, and the digit count, are what tell them apart.
    """
    result = redact("Ahmed Metawea\n- Processed 4000000 events per day in 2021-2024.")
    assert "4000000 events per day" in result.text

    mobile = redact("Ahmed Metawea\nMobile 01001234567")
    assert "01001234567" not in mobile.text
