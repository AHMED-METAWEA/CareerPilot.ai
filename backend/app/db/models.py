"""SQLAlchemy mapping of the schema in §6.

The migrations are the source of truth for DDL (they carry the partitioning,
the extensions and the partial indexes); this module is the runtime mapping.
`tests/integration/test_schema_parity.py` asserts the two agree.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INT4RANGE, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 384  # intfloat/multilingual-e5-small


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ── Identity ──────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    locale: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'en'"))
    created_at: Mapped[datetime] = _now()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RefreshToken(Base):
    """Rotating refresh tokens (§16.2).

    Stored hashed, single-use, and grouped into a `family` so that reuse of a
    rotated token can revoke the whole chain rather than one token — reuse means
    a copy exists, and one of the two holders is not the user.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user", "user_id"),
        Index("ix_refresh_tokens_family", "family"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    family: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class AuditLog(Base):
    """Every CV access, export and deletion (§16.2)."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


# ── Candidate ─────────────────────────────────────────────────────────


class CvDocument(Base):
    __tablename__ = "cv_documents"
    __table_args__ = (UniqueConstraint("user_id", "sha256"),)

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_at: Mapped[datetime] = _now()


class CvVersion(Base):
    __tablename__ = "cv_versions"

    id: Mapped[uuid.UUID] = _pk()
    cv_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cv_documents.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    parse_quality: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    parser_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _now()


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    cv_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cv_versions.id"), nullable=False
    )
    years_experience: Mapped[float | None] = mapped_column(Numeric(4, 1))
    seniority_level: Mapped[str | None] = mapped_column(Text)
    locations: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    work_auth: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    languages: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    summary: Mapped[str | None] = mapped_column(Text)
    extraction_confidence: Mapped[float | None] = mapped_column(Numeric(3, 2))
    extracted_at: Mapped[datetime] = _now()


class ProfileBullet(Base):
    """One CV bullet, with its span and its sentence-level vector (§7.2).

    Document-level similarity saturates: two engineering CVs look alike because
    both are engineering CVs. Requirement-level alignment needs the individual
    bullet, and the bullet that matched is also the evidence shown to the user.
    """

    __tablename__ = "profile_bullets"
    __table_args__ = (
        Index("ix_profile_bullets_profile", "profile_id"),
        Index(
            "profile_bullet_vec_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text_: Mapped[str] = mapped_column("text", Text, nullable=False)
    span: Mapped[Any | None] = mapped_column(INT4RANGE)
    section: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[Any | None] = mapped_column(HALFVEC(EMBEDDING_DIM))
    created_at: Mapped[datetime] = _now()


class ProfileEmbedding(Base):
    """Whole-profile vector, for stage 3 retrieval."""

    __tablename__ = "profile_embeddings"

    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[Any] = mapped_column(HALFVEC(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = _now()


class ProfileSkill(Base):
    __tablename__ = "profile_skills"

    id: Mapped[uuid.UUID] = _pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False
    )
    years: Mapped[float | None] = mapped_column(Numeric(4, 1))
    proficiency: Mapped[str | None] = mapped_column(Text)
    evidence_span: Mapped[Any] = mapped_column(INT4RANGE, nullable=False)
    """Character offsets into cv_versions.raw_text — verified, not asserted (§10.1)."""
    source: Mapped[str] = mapped_column(Text, nullable=False)


# ── Taxonomy ──────────────────────────────────────────────────────────


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = _pk()
    canonical_name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    esco_uri: Mapped[str | None] = mapped_column(Text)
    o_net_code: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)

    aliases: Mapped[list[SkillAlias]] = relationship(back_populates="skill")


class SkillAlias(Base):
    __tablename__ = "skill_aliases"
    __table_args__ = (UniqueConstraint("alias", "lang"),)

    id: Mapped[uuid.UUID] = _pk()
    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    lang: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'en'"))

    skill: Mapped[Skill] = relationship(back_populates="aliases")


class UnmappedSkill(Base):
    """Tokens that did not resolve to the taxonomy (§10.2). Never invented into a skill."""

    __tablename__ = "unmapped_skills"
    __table_args__ = (UniqueConstraint("token", "lang"),)

    id: Mapped[uuid.UUID] = _pk()
    token: Mapped[str] = mapped_column(Text, nullable=False)
    lang: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'en'"))
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    first_seen_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime] = _now()
    resolved_skill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id")
    )


# ── Employer ──────────────────────────────────────────────────────────


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = _pk()
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(Text, unique=True)
    hq_country: Mapped[str | None] = mapped_column(Text)
    size_bucket: Mapped[str | None] = mapped_column(Text)
    careers_url: Mapped[str | None] = mapped_column(Text)

    aliases: Mapped[list[CompanyAlias]] = relationship(back_populates="company")


class CompanyAlias(Base):
    __tablename__ = "company_aliases"
    __table_args__ = (
        UniqueConstraint("alias", "lang"),
        # Trigram index: the alias review queue searches by partial name.
        Index(
            "company_alias_trgm_idx",
            "alias",
            postgresql_using="gin",
            postgresql_ops={"alias": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    lang: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'en'"))

    company: Mapped[Company] = relationship(back_populates="aliases")


class CompanyReviewQueue(Base):
    """Employer references that were plausible but unproven (§11.3, R5)."""

    __tablename__ = "company_review_queue"
    __table_args__ = (UniqueConstraint("observed_name", "source_id"),)

    id: Mapped[uuid.UUID] = _pk()
    observed_name: Mapped[str] = mapped_column(Text, nullable=False)
    observed_domain: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_sources.id")
    )
    suggested_company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id")
    )
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    matched_alias: Mapped[str | None] = mapped_column(Text)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    created_at: Mapped[datetime] = _now()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Ingestion ─────────────────────────────────────────────────────────


class JobSource(Base):
    __tablename__ = "job_sources"

    id: Mapped[uuid.UUID] = _pk()
    adapter: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    robots_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("20"))
    tier: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    region: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    """Five in a row opens the circuit breaker and disables the source (§13.2)."""
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class SourceRun(Base):
    __tablename__ = "source_runs"

    id: Mapped[uuid.UUID] = _pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_sources.id"), nullable=False
    )
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    new_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    updated_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    errors: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class RawPayload(Base):
    """Verbatim source payloads, partitioned monthly and pruned after 90 days (§6.4)."""

    __tablename__ = "raw_payloads"
    __table_args__ = (
        Index("raw_payloads_source_external_idx", "source_id", "external_id"),
        {"postgresql_partition_by": "RANGE (fetched_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), primary_key=True
    )


# ── Postings ──────────────────────────────────────────────────────────


class JobGroup(Base):
    __tablename__ = "job_groups"

    id: Mapped[uuid.UUID] = _pk()
    canonical_posting_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    cluster_key: Mapped[str] = mapped_column(Text, nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    first_seen_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime] = _now()


class JobPosting(Base):
    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        Index("ix_job_postings_company_title", "company_id", "title_normalized"),
        Index(
            "ix_job_postings_posted_at_open",
            text("posted_at DESC"),
            postgresql_where=text("status = 'open'"),
        ),
        Index(
            "ix_job_postings_last_verified_open",
            "last_verified_at",
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_job_postings_canonical_url", "canonical_url"),
        Index("ix_job_postings_group", "job_group_id"),
        # Lexical half of hybrid retrieval (§6.3, §7.3). 'simple' rather than
        # 'english': the corpus is bilingual, and stemming one language while
        # mangling the other is worse than stemming neither.
        Index(
            "job_fts_idx",
            text("to_tsvector('simple', title || ' ' || description_text)"),
            postgresql_using="gin",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    job_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_groups.id")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_sources.id"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id")
    )
    company_name_raw: Mapped[str | None] = mapped_column(Text)
    """Kept verbatim so an unresolved employer can still be reviewed and re-linked."""
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    description_text: Mapped[str] = mapped_column(Text, nullable=False)
    locations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    remote_type: Mapped[str | None] = mapped_column(Text)
    employment_type: Mapped[str | None] = mapped_column(Text)
    seniority_level: Mapped[str | None] = mapped_column(Text)
    salary_min: Mapped[float | None] = mapped_column(Numeric)
    salary_max: Mapped[float | None] = mapped_column(Numeric)
    currency: Mapped[str | None] = mapped_column(String(3))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ats_platform: Mapped[str | None] = mapped_column(Text)
    ats_confidence: Mapped[float | None] = mapped_column(Numeric(3, 2))
    detection_method: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str] = mapped_column(Text, nullable=False)
    apply_url_method: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    url_status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'unknown'"))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    content_simhash: Mapped[int | None] = mapped_column(BigInteger)
    """SimHash-64 stored signed: Postgres has no unsigned bigint (§11.3 stage 4)."""
    language: Mapped[str | None] = mapped_column(String(2))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    first_seen_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class JobRequirement(Base):
    __tablename__ = "job_requirements"
    __table_args__ = (Index("ix_job_requirements_posting", "posting_id"),)

    id: Mapped[uuid.UUID] = _pk()
    posting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    text_: Mapped[str] = mapped_column("text", Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    is_must_have: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    skill_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("skills.id"))
    span: Mapped[Any] = mapped_column(INT4RANGE, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[Any | None] = mapped_column(HALFVEC(EMBEDDING_DIM))
    """Sentence-level vector for this requirement (§7.2), for per-requirement
    alignment. Nullable until the embed worker reaches it."""


class JobEmbedding(Base):
    __tablename__ = "job_embeddings"
    __table_args__ = (
        Index(
            "job_vec_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
    )

    posting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_postings.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    """Carried on every row so an embedding-model change is a dual-write, not a rebuild (R7)."""
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[Any] = mapped_column(HALFVEC(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = _now()


# ── Matching ──────────────────────────────────────────────────────────


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("profile_id", "posting_id", "model_version"),
        Index(
            "ix_matches_user_score",
            "user_id",
            text("total_score DESC"),
            postgresql_where=text("gate_passed"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidate_profiles.id"), nullable=False
    )
    posting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    total_score: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    percentile: Mapped[float | None] = mapped_column(Numeric(5, 2))
    gate_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    gate_failures: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    subscores: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text)
    """Prose assembled from the fact bundle, never from the model's own memory (§10.4)."""
    gaps: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    computed_at: Mapped[datetime] = _now()


class MatchEvidence(Base):
    __tablename__ = "match_evidence"

    id: Mapped[uuid.UUID] = _pk()
    match_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("matches.id", ondelete="CASCADE"), nullable=False
    )
    requirement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_requirements.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    cv_span: Mapped[Any | None] = mapped_column(INT4RANGE)
    similarity: Mapped[float | None] = mapped_column(Numeric(4, 3))
    note: Mapped[str | None] = mapped_column(Text)


# ── Engagement ────────────────────────────────────────────────────────


class UserJobEvent(Base):
    __tablename__ = "user_job_events"
    __table_args__ = (Index("ix_user_job_events_user_posting", "user_id", "posting_id"),)

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    posting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    event: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    """`clock_timestamp()`, not `now()`.

    Postgres' `now()` is the *transaction* start time, so a save and a dismiss
    recorded in one request share a timestamp and their order is undefined —
    which made "currently saved" return dismissed postings. An event log needs
    the instant the event happened."""


class DigestSend(Base):
    """One posting sent to one user in a digest (§11.7).

    The uniqueness constraint is what makes "new" mean new: a digest that
    repeats yesterday's list trains people to stop opening it.
    """

    __tablename__ = "digest_sends"
    __table_args__ = (
        UniqueConstraint("user_id", "posting_id"),
        Index("ix_digest_sends_user_sent", "user_id", "sent_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    posting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    match_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    message_id: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = _now()


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("user_id", "posting_id"),)

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    posting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_postings.id"), nullable=False
    )
    cv_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cv_versions.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'applied'"))
    applied_at: Mapped[datetime] = _now()


class ApplicationEvent(Base):
    __tablename__ = "application_events"

    id: Mapped[uuid.UUID] = _pk()
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = _now()
    note: Mapped[str | None] = mapped_column(Text)


# ── Evaluation ────────────────────────────────────────────────────────


class EvalLabel(Base):
    __tablename__ = "eval_labels"
    __table_args__ = (
        UniqueConstraint("profile_id", "posting_id", "labeler"),
        CheckConstraint("grade BETWEEN 0 AND 3", name="eval_labels_grade_check"),
    )

    id: Mapped[uuid.UUID] = _pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidate_profiles.id"), nullable=False
    )
    posting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_postings.id"), nullable=False
    )
    grade: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    labeler: Mapped[str] = mapped_column(Text, nullable=False)
    labeled_at: Mapped[datetime] = _now()


class ModelRun(Base):
    __tablename__ = "model_runs"

    id: Mapped[uuid.UUID] = _pk()
    component: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    run_at: Mapped[datetime] = _now()


# ── Worker queue (§13.1) ──────────────────────────────────────────────


class TaskQueue(Base):
    __tablename__ = "task_queue"
    __table_args__ = (
        Index(
            "ix_task_queue_claim",
            text("priority DESC"),
            "run_after",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ux_task_queue_dedup",
            "dedup_key",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    dedup_key: Mapped[str | None] = mapped_column(Text)
    """Keeps a task idempotent by construction: re-enqueuing a pending task is a no-op."""
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("4"))
    run_after: Mapped[datetime] = _now()
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class UserScoreWeights(Base):
    """Personalised scoring weights for one user (§11.8, Phase 6).

    One row per user, written only after a fit has been *validated* against
    that user's own labelled subset. The columns exist in the shape they do
    because §11.8 requires the adjustment to be defensible after the fact:
    `coefficients` is the interpretable model, `adjustments` is what it did to
    each weight, and the two NDCG figures are the evidence that it helped.

    `is_active` is separate from the row's existence on purpose. A fit that did
    not beat the defaults is kept rather than discarded — it is the record of
    having tried, and it stops the next run from re-deriving the same negative
    result and calling it new.
    """

    __tablename__ = "user_score_weights"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    weights: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    coefficients: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    adjustments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    events_used: Mapped[int] = mapped_column(Integer, nullable=False)
    ndcg_default: Mapped[float | None] = mapped_column(Numeric(6, 4))
    ndcg_personalised: Mapped[float | None] = mapped_column(Numeric(6, 4))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    """False where the fit did not beat the defaults. The weights are stored
    either way; only an active row is allowed to change a ranking."""
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    trained_at: Mapped[datetime] = _now()
