"""Configuration surface (Appendix E)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config import BASE_DIR, AppConfig, get_config


def test_shipped_config_is_valid() -> None:
    config = get_config()
    assert abs(config.scoring.weights.total() - 1.0) < 1e-9
    assert config.funnel.rerank_top_n < config.funnel.fusion_top_n < config.funnel.retrieval_top_k
    assert config.dedup.simhash_hamming_max >= 0


def test_model_identifiers_live_in_config_not_code() -> None:
    """§7.1: vendors deprecate model names frequently, so none is hardcoded."""
    config = get_config()
    assert config.models.embedding and config.models.reranker
    assert config.models.extraction.provider and config.models.extraction.model


def test_weights_that_do_not_sum_to_one_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent rescale of every score in the system is worth failing loudly for."""
    data = yaml.safe_load((BASE_DIR / "config" / "config.yaml").read_text())
    data["scoring"]["weights"]["freshness"] = 0.50
    broken = tmp_path / "config.yaml"
    broken.write_text(yaml.safe_dump(data))

    from app import config as config_module

    monkeypatch.setenv("CONFIG_PATH", str(broken))
    config_module.reset_config_cache()
    try:
        with pytest.raises(ValueError, match=r"must sum to 1\.0"):
            config_module.get_config()
    finally:
        monkeypatch.delenv("CONFIG_PATH", raising=False)
        config_module.reset_config_cache()


def test_config_model_rejects_unknown_shapes() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"scoring": {}})


# ── Structured prompts carry their schema (§7.4) ──────────────────────


def test_a_structured_prompt_shows_the_model_the_schema() -> None:
    """Prompts used to say "return JSON matching the schema" without ever
    showing one, which left the field names to the model's guess. That held only
    while the configured model happened to guess the names the Pydantic model
    used, and broke silently the day the provider retired that model."""
    from pydantic import BaseModel

    from app.adapters.llm.base import LLMResult, complete_schema

    class Item(BaseModel):
        text: str
        is_must_have: bool = False

    class Payload(BaseModel):
        requirements: list[Item] = []

    seen: dict[str, str] = {}

    class Recorder:
        name = "recorder"

        def chat(
            self,
            *,
            system: str,
            user: str,
            model: str,
            max_tokens: int = 2048,
            json_mode: bool = True,
        ) -> LLMResult:
            seen["system"] = system
            return LLMResult(
                content='{"requirements": [{"text": "5 years of Python", "is_must_have": true}]}',
                model=model,
                provider=self.name,
            )

    payload, result = complete_schema(
        Recorder(), system="Extract requirements.", user="a posting", model="m", schema=Payload
    )

    assert payload.requirements[0].text == "5 years of Python"
    assert result.attempts == 1, "a schema in the prompt should remove the need to repair"
    # The exact field names must reach the model, not a prose description.
    assert "requirements" in seen["system"]
    assert "is_must_have" in seen["system"]
    assert "do not return a bare array" in seen["system"]
