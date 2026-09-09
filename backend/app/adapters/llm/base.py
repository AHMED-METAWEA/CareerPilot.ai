"""Schema-constrained completion (§7.4).

Language models are used for exactly three tasks here, and all three are
structured: extraction, requirement assessment, and phrasing. None of them is
allowed to produce a number that reaches a score.

The contract every provider implements:

* temperature 0 — the same input gives the same output, or as close as a hosted
  model allows;
* the response is parsed and validated against a Pydantic schema;
* exactly one repair retry, showing the model its own invalid output and the
  validation error;
* then hard failure. A third attempt is a worse use of a rate-limited budget
  than an honest error.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, ValidationError

log = structlog.get_logger(__name__)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.S)


class LLMError(RuntimeError):
    """The provider could not be reached, or returned an unusable response."""


class ProviderRateLimited(LLMError):
    """Rate limited. Carries `retry_after` so the caller can park rather than retry."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SchemaViolationError(LLMError):
    """The model's output did not validate, and the repair attempt also failed."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


@dataclass(frozen=True, slots=True)
class LLMResult:
    """One completion, with everything `model_runs` records (§17.2)."""

    content: str
    model: str
    provider: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    attempts: int = 1
    repaired: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class ChatProvider(Protocol):
    """What every provider adapter implements."""

    name: str

    def chat(
        self, *, system: str, user: str, model: str, max_tokens: int = 2048, json_mode: bool = True
    ) -> LLMResult: ...


REPAIR_INSTRUCTION = (
    "Your previous response did not satisfy the required JSON schema.\n"
    "Validation error:\n{error}\n\n"
    "Your previous response was:\n{previous}\n\n"
    "Return only corrected JSON matching the schema. No commentary, no markdown fence."
)


SCHEMA_INSTRUCTION = (
    "\n\nReturn a single JSON object matching this schema exactly. Use these "
    "field names verbatim, do not rename or abbreviate them, do not wrap the "
    "object inside another key, and do not return a bare array.\n\n{schema}"
)
"""Every prompt used to end with "Return only JSON matching the schema" without
ever showing the schema, which left the field names to the model's guess. That
worked for as long as the configured model happened to guess the same names the
Pydantic model used, and broke silently the day it changed: one provider
returned a top-level array of `{"requirement": ...}` where the schema wanted
`{"requirements": [{"text": ...}]}`, both attempts failed validation, and every
posting was scored with no requirements at all.

The schema is rendered from the Pydantic model itself, so it cannot drift from
what is actually validated."""


def _with_schema(system: str, schema: type[BaseModel]) -> str:
    return system + SCHEMA_INSTRUCTION.format(
        schema=json.dumps(schema.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    )


def complete_schema[T: BaseModel](
    provider: ChatProvider,
    *,
    system: str,
    user: str,
    model: str,
    schema: type[T],
    max_tokens: int = 2048,
) -> tuple[T, LLMResult]:
    """Complete, parse and validate — with one repair retry, then hard failure."""
    started = time.monotonic()
    system = _with_schema(system, schema)
    result = provider.chat(system=system, user=user, model=model, max_tokens=max_tokens)

    try:
        return _parse(result.content, schema), result
    except (ValidationError, ValueError) as exc:
        # Bound to an outer name: Python unbinds the `except` variable on exit,
        # and the repair prompt needs the message.
        validation_error = str(exc)
        log.info(
            "llm.schema_repair_attempt",
            provider=provider.name,
            model=model,
            error=validation_error[:300],
        )

    repair = REPAIR_INSTRUCTION.format(
        error=validation_error[:1500], previous=result.content[:4000]
    )
    repaired = provider.chat(
        system=system, user=f"{user}\n\n{repair}", model=model, max_tokens=max_tokens
    )
    combined = LLMResult(
        content=repaired.content,
        model=model,
        provider=provider.name,
        tokens_in=result.tokens_in + repaired.tokens_in,
        tokens_out=result.tokens_out + repaired.tokens_out,
        latency_ms=(time.monotonic() - started) * 1000,
        attempts=2,
        repaired=True,
    )
    try:
        return _parse(repaired.content, schema), combined
    except (ValidationError, ValueError) as second_error:
        raise SchemaViolationError(
            f"{provider.name}/{model} failed schema validation twice: {second_error}",
            raw=repaired.content,
        ) from second_error


def _parse[T: BaseModel](content: str, schema: type[T]) -> T:
    """Parse a model response into `schema`.

    Tolerates a markdown fence and leading prose, because providers add them
    even in JSON mode. Tolerates nothing else: a response that is not the
    requested object is a failure, not something to coerce.
    """
    text = content.strip()
    if not text:
        raise ValueError("empty response")

    fenced = _JSON_BLOCK.search(text)
    if fenced:
        text = fenced.group(1).strip()
    elif not text.startswith(("{", "[")):
        start = min(
            (index for index in (text.find("{"), text.find("[")) if index >= 0),
            default=-1,
        )
        if start < 0:
            raise ValueError(f"no JSON object in response: {text[:200]!r}")
        text = text[start:]

    data = json.loads(text)
    return schema.model_validate(data)
