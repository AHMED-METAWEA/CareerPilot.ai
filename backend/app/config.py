"""Configuration surface (Appendix E).

Two sources, deliberately separated:

* **Environment** — secrets, connection strings, per-deployment switches.
* **`config/config.yaml`** — every tunable: weights, thresholds, funnel sizes
  and model identifiers. Vendors deprecate model names frequently, so no model
  identifier appears anywhere in source code (§7.1).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Environment-injected settings. Secrets never enter the YAML file."""

    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env", BASE_DIR.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://careerpilot:careerpilot@localhost:5432/careerpilot"
    careerpilot_env: Literal["local", "staging", "production"] = "local"
    log_level: str = "INFO"
    config_path: str = "config/config.yaml"

    admin_token: str = "change-me-in-production"

    groq_api_key: str | None = None
    gemini_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"

    adzuna_app_id: str | None = None
    adzuna_app_key: str | None = None
    usajobs_api_key: str | None = None
    usajobs_user_agent: str | None = None

    careerpilot_test_database_url: str | None = None

    @property
    def resolved_config_path(self) -> Path:
        path = Path(self.config_path)
        return path if path.is_absolute() else BASE_DIR / path


# ── config.yaml schema ────────────────────────────────────────────────


class ScoringWeights(BaseModel):
    skill_coverage: float
    requirement_alignment: float
    seniority_fit: float
    semantic_similarity: float
    freshness: float

    def total(self) -> float:
        return (
            self.skill_coverage
            + self.requirement_alignment
            + self.seniority_fit
            + self.semantic_similarity
            + self.freshness
        )


class ScoringConfig(BaseModel):
    weights: ScoringWeights
    freshness_half_life_days: float
    skill_fuzzy_threshold: float


class GatesConfig(BaseModel):
    max_years_shortfall: float
    max_posting_age_days: int
    require_verified_url: bool


class FunnelConfig(BaseModel):
    retrieval_top_k: int
    rrf_k: int
    fusion_top_n: int
    rerank_top_n: int


class ProviderModel(BaseModel):
    provider: str
    model: str


class ModelsConfig(BaseModel):
    embedding: str
    reranker: str
    extraction: ProviderModel
    analysis: ProviderModel
    fallbacks: list[str] = Field(default_factory=list)


class DedupConfig(BaseModel):
    simhash_hamming_max: int
    semantic_threshold: float
    title_block_tokens: int
    max_postings_per_url: int = 8
    """Above this, a shared canonical URL is treated as a board page, not identity."""
    company_fuzzy_threshold: float = 0.90
    company_review_threshold: float = 0.75


class VerificationConfig(BaseModel):
    reverify_after_hours: int
    suppress_after_hours: int
    per_host_min_interval_seconds: float


class IngestionConfig(BaseModel):
    user_agent: str
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    default_rate_limit_rpm: int = 20
    raw_payload_retention_days: int = 90
    zero_row_alert_ratio: float = 0.20


class QueueConfig(BaseModel):
    backoff_minutes: list[int] = Field(default_factory=lambda: [1, 5, 25])
    claim_batch: int = 1
    lock_timeout_minutes: int = 30
    worker_concurrency: int = 4


class AppConfig(BaseModel):
    """The whole tunable surface, validated on load."""

    scoring: ScoringConfig
    gates: GatesConfig
    funnel: FunnelConfig
    models: ModelsConfig
    dedup: DedupConfig
    verification: VerificationConfig
    ingestion: IngestionConfig
    queue: QueueConfig


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Load and validate `config.yaml`.

    The weight-sum check is a guard against a silent scoring change: weights
    that no longer sum to 1.0 rescale every score in the system without any
    single number looking wrong.
    """
    path = get_settings().resolved_config_path
    with path.open("r", encoding="utf-8") as handle:
        data: dict[str, Any] = yaml.safe_load(handle)
    config = AppConfig.model_validate(data)
    total = config.scoring.weights.total()
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"scoring.weights must sum to 1.0, got {total:.6f} in {path}")
    return config


def reset_config_cache() -> None:
    """Test hook: drop cached settings and config."""
    get_settings.cache_clear()
    get_config.cache_clear()
