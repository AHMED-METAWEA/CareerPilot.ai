"""CV parse-quality scoring (§11.1 step 4).

The premise from §2.4: real ATS failures are parsing failures, not keyword
density. These tests pin the signals that decide whether a CV is readable at
all, and the findings a candidate can act on.
"""

from __future__ import annotations

from app.domain.profile.parseability import MIN_QUALITY, Severity, score_parseability

GOOD_CV = """Ahmed Metawea
Senior Data Engineer
ahmed@example.com | +20 100 123 4567

Summary
Data engineer with five years building streaming and batch platforms for mobile
analytics and telecom reporting, comfortable owning a pipeline end to end.

Experience
• Data Engineer, Instabug (2021-2024): built streaming pipelines in Python and
  Kafka processing four million crash events per day.
• Rebuilt the nightly reporting stack on Airflow, cutting failed runs from a
  dozen a week to fewer than one.
• Analyst, Vodafone Egypt (2019-2021): SQL reporting for network operations.

Education
BSc Computer Engineering, Cairo University (2019)

Skills
Python, SQL, Kafka, Airflow, PostgreSQL, Docker, Spark
"""


def test_well_formed_cv_passes() -> None:
    report = score_parseability(GOOD_CV)
    assert report.is_processable
    assert report.quality > MIN_QUALITY
    assert set(report.sections_found) >= {"experience", "education", "skills", "contact"}
    assert report.layout_damage == 0.0


def test_scanned_cv_is_blocked_with_an_actionable_finding() -> None:
    """A near-empty text layer means a scan or an image, and no amount of
    downstream cleverness recovers what was never extracted."""
    report = score_parseability("Ahmed Metawea", page_count=2)
    assert not report.is_processable
    blockers = [f for f in report.findings if f.severity is Severity.BLOCKER]
    assert any(f.code == "low_text_yield" for f in blockers)
    assert all(f.suggestion for f in report.findings), "every finding must be actionable"


def test_two_column_layout_is_detected() -> None:
    """Columns interleave when flattened, which destroys reading order."""
    two_column = "\n".join(
        f"Skills: Python        Experience: Engineer at Company {index}" for index in range(12)
    )
    report = score_parseability(two_column + "\n" + GOOD_CV)
    assert report.layout_damage > 0.25
    assert any(f.code == "multi_column_layout" for f in report.findings)


def test_missing_sections_are_reported_individually() -> None:
    report = score_parseability(
        "Ahmed Metawea\nahmed@example.com\n\nI have worked in data for five years. " * 30
    )
    codes = {finding.code for finding in report.findings}
    assert "missing_section_experience" in codes
    assert "missing_section_skills" in codes


def test_arabic_headings_are_recognised() -> None:
    """Bilingual by design: an Arabic CV must not read as an unstructured one."""
    arabic = """أحمد متاوع
مهندس بيانات

الخبرات
• مهندس بيانات في انستاباج: بناء خطوط معالجة البيانات باستخدام بايثون وكافكا.
• محلل بيانات في فودافون مصر: إعداد التقارير باستخدام إس كيو إل.

التعليم
بكالوريوس هندسة حاسبات، جامعة القاهرة
"""
    report = score_parseability(arabic * 4)
    assert "experience" in report.sections_found
    assert "education" in report.sections_found


def test_quality_is_bounded() -> None:
    assert 0.0 <= score_parseability("").quality <= 1.0
    assert 0.0 <= score_parseability(GOOD_CV * 10).quality <= 1.0


# ── Arabic and mixed-script CVs (§18, Phase 5) ────────────────────────

ARABIC_CV = """أحمد متاوع — مهندس بيانات أول
القاهرة، مصر · ahmed@example.com · +20 100 000 0000

نبذة
مهندس بيانات لديه خبرة ٥ سنوات في بناء خطوط معالجة البيانات وأنظمة التدفق،
مع تركيز على الجودة وقابلية التتبع في كل مرحلة من مراحل المعالجة اليومية.

الخبرة العملية
- مهندس بيانات، إنستاباج (٢٠٢١–٢٠٢٤): بناء خطوط المعالجة باستخدام Python وKafka،
  بمعالجة أربعة ملايين حدث يوميًا، وخفض زمن الإدخال من أربعين دقيقة إلى أقل من ثلاث.
- محلل بيانات، فودافون مصر (٢٠١٩–٢٠٢١): تقارير SQL ونماذج PostgreSQL وتحليل
  سلوك المستخدمين عبر لوحات متابعة أسبوعية للفرق التجارية.

التعليم
بكالوريوس هندسة الحاسبات، جامعة القاهرة (٢٠١٩).

المهارات
Python، SQL، Apache Kafka، Apache Airflow، PostgreSQL، Docker

اللغات
العربية (اللغة الأم)، الإنجليزية (متقدم)
"""


def test_an_arabic_cv_gets_credit_for_the_headings_it_has() -> None:
    """The Arabic skills heading was invisible before Phase 5, which cost this
    CV a quarter of its section score and pushed it toward the processability
    cliff for a fault it did not have."""
    report = score_parseability(ARABIC_CV)

    assert set(report.sections_found) == {"experience", "education", "skills", "contact"}
    assert report.sections_missing == ()
    assert report.is_processable


def test_the_script_of_a_cv_is_reported() -> None:
    report = score_parseability(ARABIC_CV)
    assert report.language == "ar"
    assert report.is_mixed_script, "Arabic prose naming Latin technologies is the normal case"


def test_invisible_characters_do_not_count_as_recovered_text() -> None:
    """An extractor emits hundreds of bidi marks into an Arabic CV. Counted as
    text, they make a document that recovered almost nothing look readable."""
    padded = ARABIC_CV + "‏" * 2000
    assert (
        score_parseability(padded).character_count == score_parseability(ARABIC_CV).character_count
    )
