"""Blocking (§11.3 stage 3).

Comparing every posting to every other posting is quadratic and, at 3,000+ new
rows a day, simply not run. Blocking restricts comparison to postings that
share an employer and the head of their normalised title. No stage of the
pipeline ever compares across blocking keys.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable

from app.domain.jobs.normalize import title_tokens


def blocking_key(company_key: str | None, title_normalized: str, *, title_tokens_n: int = 3) -> str:
    """`company|first-n-title-tokens`.

    A posting whose employer could not be resolved gets a company key of
    `?`, which confines it to a block of other unresolved postings rather than
    letting it merge into a real employer's cluster on title alone.
    """
    company = company_key or "?"
    head = " ".join(title_tokens(title_normalized, title_tokens_n))
    return f"{company}|{head}"


def block[T](
    items: Iterable[T],
    key_fn: Callable[[T], str],
) -> dict[str, list[T]]:
    """Group items by blocking key, preserving input order within each block."""
    buckets: dict[str, list[T]] = defaultdict(list)
    for item in items:
        buckets[key_fn(item)].append(item)
    return dict(buckets)
