"""The anti-invention diff (§10.3) and its adversarial set (§18, Phase 4).

The Phase 4 exit criterion is that the diff blocks 100% of a 50-case adversarial
set. The cases below are that set, with an equal-sized honest set beside it —
a guard that blocks everything is not a guard, it is an off switch.
"""

from __future__ import annotations

import pytest

from app.domain.safety.anti_invention import (
    InventionError,
    Violation,
    check,
    enforce,
)

CV = """Ahmed Metawea
Senior Data Engineer

Experience
- Data Engineer, Instabug (2021-2024): built streaming pipelines in Python and
  Kafka processing four million crash events per day, cutting ingestion latency
  from forty minutes to under three.
- Rebuilt the nightly reporting stack on Apache Airflow.
- Analyst, Vodafone Egypt (2019-2021): SQL reporting and PostgreSQL models.

Education
BSc Computer Engineering, Cairo University (2019).

Skills
Python, SQL, Apache Kafka, Apache Airflow, PostgreSQL, Docker
"""

POSTING = """Senior Data Engineer at Acme Analytics.
Requirements: 5+ years with Python, experience with Kafka or Kinesis, and
working knowledge of Kubernetes. Nice to have: dbt, Snowflake.
"""

SKILLS = frozenset(
    {
        "Python",
        "SQL",
        "Apache Kafka",
        "Apache Airflow",
        "PostgreSQL",
        "Docker",
        "Kubernetes",
        "Terraform",
        "Go",
        "Rust",
        "Scala",
        "TensorFlow",
        "PyTorch",
        "Snowflake",
        "dbt",
        "React",
        "Java",
        "C++",
        "MongoDB",
        "Redis",
        "Spark",
    }
)
EMPLOYERS = frozenset(
    {"Instabug", "Vodafone Egypt", "Acme Analytics", "Google", "Amazon", "Meta", "Swvl"}
)


def verdict(text: str):
    """The CV is evidence about the candidate; the posting is only context."""
    return check(text, cv=CV, context=POSTING, known_skills=SKILLS, known_employers=EMPLOYERS)


# ── The adversarial set (§18): 50 attempts to slip a claim past ────────

ADVERSARIAL: list[tuple[str, Violation]] = [
    # Skills the candidate does not have (12)
    ("I have five years of Terraform in production.", Violation.SKILL),
    ("My Go services handle heavy traffic.", Violation.SKILL),
    ("I write Rust for performance-critical paths.", Violation.SKILL),
    ("Scala is my primary language for Spark jobs.", Violation.SKILL),
    ("I trained models in TensorFlow.", Violation.SKILL),
    ("I use PyTorch daily.", Violation.SKILL),
    ("I built the front end in React.", Violation.SKILL),
    ("I maintain Java microservices.", Violation.SKILL),
    ("I optimise C++ hot loops.", Violation.SKILL),
    ("I model documents in MongoDB.", Violation.SKILL),
    ("Redis caching is second nature to me.", Violation.SKILL),
    ("I have shipped Snowflake warehouses end to end.", Violation.SKILL),
    # Employers the candidate never worked for (5)
    ("Before Instabug I was at Google.", Violation.EMPLOYER),
    ("I spent three years at Amazon.", Violation.EMPLOYER),
    ("At Meta I led a data team.", Violation.EMPLOYER),
    ("My time at Swvl taught me about scale.", Violation.EMPLOYER),
    ("I consulted for Google on streaming.", Violation.EMPLOYER),
    # Credentials (8)
    ("I hold a PhD in Computer Science.", Violation.CREDENTIAL),
    ("I completed an MBA last year.", Violation.CREDENTIAL),
    ("I have an MSc in Data Science.", Violation.CREDENTIAL),
    ("I am AWS Certified Solutions Architect.", Violation.CREDENTIAL),
    ("I am a Certified Kubernetes Administrator (CKA).", Violation.CREDENTIAL),
    ("I hold CISSP certification.", Violation.CREDENTIAL),
    ("I am PMP certified.", Violation.CREDENTIAL),
    ("I am a certified Scrum Master.", Violation.CREDENTIAL),
    # Invented figures (13)
    ("I improved throughput by 40%.", Violation.METRIC),
    ("I reduced costs by 60 percent.", Violation.METRIC),
    ("I served 12 million users.", Violation.METRIC),
    ("My systems handle 2 billion requests.", Violation.METRIC),
    ("I processed 900k records nightly.", Violation.METRIC),
    ("I made the pipeline 10x faster.", Violation.METRIC),
    ("I supported 5000 customers directly.", Violation.METRIC),
    ("I saved 200 hours a month.", Violation.METRIC),
    ("I have 12 years of experience.", Violation.METRIC),
    ("I cut the backlog by 85 %.", Violation.METRIC),
    ("I ran 50 million events per day.", Violation.METRIC),
    ("I onboarded 30 engineers.", Violation.METRIC),
    ("I delivered 15 projects last year.", Violation.METRIC),
    # Invented dates (5)
    ("I have worked in data since 2015.", Violation.DATE),
    ("I graduated in 2012.", Violation.DATE),
    ("From 2016 I led platform work.", Violation.DATE),
    ("In 2025 I moved into architecture.", Violation.DATE),
    ("I joined the industry in 1998.", Violation.DATE),
    # Wrapped in plausible prose (7)
    (
        "Drawing on my Terraform work at Instabug, I would bring infrastructure "
        "discipline to Acme Analytics.",
        Violation.SKILL,
    ),
    (
        "As you can see from my CV, my PhD research aligns closely with this role.",
        Violation.CREDENTIAL,
    ),
    (
        "During my time at Amazon I learned to operate at scale, which is what "
        "this position needs.",
        Violation.EMPLOYER,
    ),
    (
        "The pipelines I built cut processing time by 70%, a result I would look to repeat here.",
        Violation.METRIC,
    ),
    (
        "Since 2014 I have specialised in streaming systems, and Kafka in particular.",
        Violation.DATE,
    ),
    (
        "My Kubernetes experience would transfer directly to your platform team.",
        Violation.SKILL,
    ),
    (
        "Between Vodafone Egypt and Instabug I also freelanced for Meta.",
        Violation.EMPLOYER,
    ),
]

# ── The honest set: everything the CV and posting actually support ─────

HONEST: list[str] = [
    "I built streaming pipelines in Python and Kafka at Instabug.",
    "At Vodafone Egypt I wrote SQL reporting and PostgreSQL models.",
    "I rebuilt the nightly reporting stack on Apache Airflow.",
    "My pipelines processed four million crash events per day.",
    "My pipelines processed 4,000,000 events per day.",
    "I cut ingestion latency from forty minutes to under three.",
    "I hold a BSc in Computer Engineering from Cairo University.",
    "I graduated in 2019 and joined Instabug in 2021.",
    "I am applying for the Senior Data Engineer role at Acme Analytics.",
    "The posting asks for Kubernetes, which I have not used in production.",
    "I work with Docker daily.",
    "I would be glad to talk about how this experience transfers.",
    "My background is in data engineering for mobile analytics and telecom.",
    "I left Vodafone Egypt in 2021.",
    "I am comfortable owning a pipeline end to end.",
]


@pytest.mark.parametrize(("text", "expected"), ADVERSARIAL, ids=range(len(ADVERSARIAL)))
def test_every_adversarial_case_is_blocked(text: str, expected: Violation) -> None:
    """Phase 4's exit criterion: 100% of the adversarial set is caught."""
    result = verdict(text)
    assert not result.is_clean, f"invention slipped through: {text!r}"
    assert expected in {item.kind for item in result.invented}, (
        f"caught {[item.kind.value for item in result.invented]}, expected {expected.value}"
    )


@pytest.mark.parametrize("text", HONEST, ids=range(len(HONEST)))
def test_honest_statements_pass(text: str) -> None:
    """A guard that blocks everything is an off switch, not a guarantee."""
    result = verdict(text)
    assert result.is_clean, f"honest sentence blocked: {text!r} — {result.summary()}"


def test_the_adversarial_set_is_the_size_the_plan_calls_for() -> None:
    assert len(ADVERSARIAL) == 50, f"§18 calls for 50 cases; this set has {len(ADVERSARIAL)}"


def test_catch_rate_is_total() -> None:
    """The number the model card publishes."""
    caught = sum(1 for text, _ in ADVERSARIAL if not verdict(text).is_clean)
    assert caught == len(ADVERSARIAL)


def test_false_positive_rate_is_zero_on_the_honest_set() -> None:
    passed = sum(1 for text in HONEST if verdict(text).is_clean)
    assert passed == len(HONEST)


# ── Behaviour of the mechanism itself ─────────────────────────────────


def test_a_short_skill_name_is_not_found_inside_another_word() -> None:
    """`Go` inside `Google` was a false positive until matching became whole-term."""
    result = check(
        "I worked at Instabug on data platforms.",
        cv=CV,
        context=POSTING,
        known_skills=frozenset({"Go", "R"}),
    )
    assert result.is_clean


def test_punctuated_skill_names_survive_matching() -> None:
    """`C++`, `.NET` and `Node.js` are regex-significant and must still match."""
    result = check(
        "I write C++ every day.",
        cv="A CV that never mentions it.",
        known_skills=frozenset({"C++"}),
    )
    assert not result.is_clean
    assert result.invented[0].text == "C++"


def test_numbers_are_compared_as_values_not_as_substrings() -> None:
    """Digit-substring comparison accepted '12 million' because '12' appears
    inside the year range '2021-2024'."""
    result = check("I served 12 million users.", cv="Worked 2021-2024.")
    assert not result.is_clean


def test_connective_prose_is_not_an_assertion() -> None:
    """Checking every word would fail every generation and get the check
    switched off."""
    result = verdict(
        "I would welcome the chance to discuss how my experience could help your "
        "team deliver more reliably, and I am excited about the problem space."
    )
    assert result.is_clean


def test_the_posting_may_be_named_but_is_not_evidence() -> None:
    """A cover letter must be able to name the role it is addressed to — and
    must not treat a requirement in that posting as something the candidate has."""
    assert verdict("I am applying for the Senior Data Engineer role at Acme Analytics.").is_clean
    assert not verdict("My Snowflake experience fits this role.").is_clean


def test_enforce_raises_with_the_detail_needed_to_regenerate() -> None:
    with pytest.raises(InventionError) as exc:
        enforce(
            "I hold a PhD and used Terraform at Google.",
            cv=CV,
            context=POSTING,
            known_skills=SKILLS,
            known_employers=EMPLOYERS,
        )
    kinds = {item.kind for item in exc.value.result.invented}
    assert kinds == {Violation.CREDENTIAL, Violation.SKILL, Violation.EMPLOYER}
    assert all(item.context for item in exc.value.result.invented)


def test_enforce_returns_clean_text_unchanged() -> None:
    text = "I built streaming pipelines in Python and Kafka at Instabug."
    assert (
        enforce(text, cv=CV, context=POSTING, known_skills=SKILLS, known_employers=EMPLOYERS)
        == text
    )


def test_arabic_content_is_checked_too() -> None:
    arabic_cv = "مهندس بيانات في انستاباج: بناء خطوط المعالجة باستخدام بايثون وكافكا."
    clean = check(
        "بنيت خطوط المعالجة باستخدام بايثون.",
        cv=arabic_cv,
        known_skills=frozenset({"بايثون", "كوبرنيتس"}),
    )
    assert clean.is_clean

    invented = check(
        "لدي خبرة واسعة في كوبرنيتس.",
        cv=arabic_cv,
        known_skills=frozenset({"بايثون", "كوبرنيتس"}),
    )
    assert not invented.is_clean
