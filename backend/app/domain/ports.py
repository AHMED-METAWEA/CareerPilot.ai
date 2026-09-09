"""Protocols the domain defines and adapters implement (§4.4).

The domain never imports an adapter. Adding a job source is one file plus one
database row; changing LLM provider is one configuration value.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any, Protocol, runtime_checkable

from app.domain.models import Cursor, NormalizedJob, RawJob, SourceHealth


@runtime_checkable
class JobSourceAdapter(Protocol):
    """One ATS or aggregator feed.

    Each adapter owns its own container shape and field vocabulary. There is no
    shared response accessor — that is precisely how Lever's bare array returns
    zero rows for a week without anyone noticing (Appendix B, integration rule).
    """

    name: str
    adapter: str

    def health(self) -> SourceHealth: ...

    def fetch(self, cursor: Cursor | None = None) -> Iterator[RawJob]: ...

    def normalize(self, raw: RawJob) -> NormalizedJob: ...


class LLMProvider(Protocol):
    def complete(self, prompt: str, schema: type[Any], **kwargs: Any) -> Any: ...


class EmbeddingBackend(Protocol):
    dim: int
    model_id: str

    def encode(self, texts: Sequence[str]) -> Any: ...


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> str: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...
