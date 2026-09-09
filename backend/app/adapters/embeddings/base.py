"""Embedding backend protocol and vector helpers.

Vectors are plain `list[float]`: pgvector's SQLAlchemy type accepts them
directly, and it keeps numpy out of the core dependency set — it arrives with
the `[ml]` extra, alongside the models that actually need it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

Vector = list[float]


@runtime_checkable
class EmbeddingBackend(Protocol):
    dim: int
    model_id: str

    def encode(self, texts: Sequence[str]) -> list[Vector]: ...


def normalize(vector: Sequence[float]) -> Vector:
    """L2-normalise, so cosine similarity is a dot product."""
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0.0:
        return list(vector)
    return [value / magnitude for value in vector]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, clamped to [-1, 1] against floating-point drift."""
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    magnitude = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if magnitude == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / magnitude))


def to_pgvector(vector: Sequence[float]) -> str:
    """Render a vector for a halfvec column. pgvector accepts this literal form."""
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"


def parse_vector(value: object) -> Vector | None:
    """Read a halfvec column back into a list of floats.

    pgvector's typed psycopg adapter needs numpy, which is deliberately not a
    core dependency, so a `text()` query returns the wire format as a string.
    Both shapes are handled here rather than at every call site.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip().strip("[]")
        if not stripped:
            return []
        return [float(part) for part in stripped.split(",")]
    if isinstance(value, (list, tuple)):
        return [float(item) for item in value]
    if isinstance(value, Iterable):  # numpy array, when the ml extra is installed
        return [float(item) for item in value]
    raise TypeError(f"cannot read a vector from {type(value).__name__}")
