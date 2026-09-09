"""Queue backoff policy (§13.2). Pure arithmetic, no database."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.queue import backoff_delay

SCHEDULE = [1, 5, 25]


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        (1, timedelta(minutes=1)),
        (2, timedelta(minutes=5)),
        (3, timedelta(minutes=25)),
        (4, None),  # dead-letter
        (9, None),
    ],
)
def test_backoff_schedule(attempts: int, expected: timedelta | None) -> None:
    assert backoff_delay(attempts, SCHEDULE) == expected


def test_backoff_is_monotonic() -> None:
    delays = [backoff_delay(a, SCHEDULE) for a in (1, 2, 3)]
    assert all(d is not None for d in delays)
    assert delays == sorted(delays)  # type: ignore[type-var]
