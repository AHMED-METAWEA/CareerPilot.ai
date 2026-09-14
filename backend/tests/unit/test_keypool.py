"""API key rotation (§7.1, R1).

Holding four keys is only worth anything if the pool actually behaves like four
quotas. Each test here pins one of the ways a naive implementation quietly
delivers the throughput of one.
"""

from __future__ import annotations

import pytest

from app.adapters.llm.keypool import (
    DEFAULT_COOLDOWN_SECONDS,
    AllKeysExhausted,
    ApiKeyPool,
)

KEYS = ["key-a", "key-b", "key-c", "key-d"]


def pool(keys: list[str] | None = None, *, now: list[float] | None = None) -> ApiKeyPool:
    clock = now if now is not None else [0.0]
    return ApiKeyPool(keys or KEYS, provider="groq", clock=lambda: clock[0])


def test_load_is_spread_across_every_key() -> None:
    """A pool that always hands out the first key is a pool of one."""
    keys = pool()
    handed = [keys.acquire() for _ in range(8)]

    assert set(handed) == set(KEYS)
    assert {handed.count(key) for key in KEYS} == {2}, "evenly, not merely eventually"


def test_each_key_gets_its_own_rate_budget() -> None:
    """The HTTP client meters per `budget_key`. Sharing one name across four
    keys would queue them behind a single per-minute allowance and buy nothing —
    the exact failure this whole mechanism exists to avoid."""
    keys = pool()
    budgets = {keys.budget_key(key) for key in KEYS}

    assert len(budgets) == len(KEYS)
    assert all(budget.startswith("llm:groq:") for budget in budgets)


def test_a_rate_limited_key_is_skipped_until_its_cooldown_expires() -> None:
    """Round-robin would keep sending one request in four back into the same
    429, wasting the request and re-arming the limit."""
    now = [0.0]
    keys = pool(now=now)

    keys.penalise("key-a", retry_after=30.0)
    handed = {keys.acquire() for _ in range(6)}
    assert "key-a" not in handed

    now[0] = 31.0
    assert "key-a" in {keys.acquire() for _ in range(6)}


def test_a_provider_that_does_not_say_how_long_still_gets_parked() -> None:
    now = [0.0]
    keys = pool(now=now)
    keys.penalise("key-a", retry_after=None)

    assert "key-a" not in {keys.acquire() for _ in range(6)}
    now[0] = DEFAULT_COOLDOWN_SECONDS + 0.1
    assert "key-a" in {keys.acquire() for _ in range(6)}


def test_all_keys_exhausted_raises_rather_than_sleeps() -> None:
    """The caller sits behind a fallback chain. Another provider answering now
    beats this one answering in thirty seconds."""
    now = [0.0]
    keys = pool(now=now)
    for key in KEYS:
        keys.penalise(key, retry_after=30.0)

    with pytest.raises(AllKeysExhausted) as exc:
        keys.acquire()
    assert exc.value.retry_after == pytest.approx(30.0)


def test_a_recovered_key_is_brought_back_up_to_par() -> None:
    """Least-used rather than round-robin, so a key released from a cooldown is
    not left idle waiting its turn behind keys carrying the load."""
    now = [0.0]
    keys = pool(["key-a", "key-b"], now=now)

    keys.penalise("key-a", retry_after=10.0)
    for _ in range(4):
        keys.acquire()  # all key-b

    now[0] = 11.0
    assert keys.acquire() == "key-a", "the idle key should be preferred once it is back"


def test_duplicate_keys_are_collapsed() -> None:
    """The same key twice is one quota, not two — counting it twice would
    promise capacity that does not exist."""
    keys = ApiKeyPool(["same", "same", "other"], provider="groq", clock=lambda: 0.0)
    assert len(keys) == 2


def test_blank_entries_are_ignored() -> None:
    """`GROQ_API_KEYS=a,,b,` is what a hand-edited .env actually looks like."""
    keys = ApiKeyPool(["a", "", "  ", "b"], provider="groq", clock=lambda: 0.0)
    assert len(keys) == 2


def test_an_empty_pool_is_refused() -> None:
    with pytest.raises(ValueError, match="no API keys"):
        ApiKeyPool([" ", ""], provider="groq")


def test_stats_never_contain_a_key() -> None:
    """These go to logs and to `models-check` output."""
    keys = pool()
    keys.acquire()
    keys.penalise("key-a", retry_after=5.0)

    rendered = repr(keys.stats())
    for key in KEYS:
        assert key not in rendered
    assert all("uses" in row and "rate_limits" in row for row in keys.stats())
