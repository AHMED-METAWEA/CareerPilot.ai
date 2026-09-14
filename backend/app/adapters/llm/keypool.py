"""Rotating a provider's API keys (§7.1, R1).

A rate limit is per key, so several keys are several quotas. The system's
throughput has been bounded by one free-tier ceiling — a matching run spent
minutes sleeping on `Retry-After` — and the cheapest way past that is to hold
more than one key.

Two things make this work rather than merely look like it works:

* **Selection is cooldown-aware, not round-robin.** Plain rotation still sends
  one request in N to a key that has just said 429, which wastes the request and
  re-arms the same limit. A key that reports a rate limit is parked for as long
  as it asked to be, and skipped until then.
* **Each key carries its own rate budget.** The HTTP client meters requests per
  `budget_key`; if every key shared one, four keys would queue behind a single
  25-per-minute allowance and buy nothing. `budget_key()` returns a distinct
  name per key.

Keys never appear in logs. A key is identified by its position and a short
fingerprint, which is enough to tell two keys apart in an incident and useless
to anyone reading the output.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

DEFAULT_COOLDOWN_SECONDS = 20.0
"""Used when a provider reports a rate limit without saying for how long.

Long enough that a key which has hit a per-minute ceiling is not retried into
the same wall, short enough that a burst does not idle the pool."""


class AllKeysExhausted(RuntimeError):
    """Every key is inside a cooldown.

    Raised rather than waited out: the caller sits behind a fallback chain, and
    another provider answering now beats this one answering in thirty seconds.
    """

    def __init__(self, keys: int, retry_after: float | None) -> None:
        super().__init__(
            f"all {keys} key(s) are rate limited"
            + (f"; soonest retry in {retry_after:.0f}s" if retry_after else "")
        )
        self.retry_after = retry_after


@dataclass(slots=True)
class _Key:
    value: str
    index: int
    available_at: float = 0.0
    uses: int = 0
    rate_limits: int = 0
    fingerprint: str = field(default="")

    def __post_init__(self) -> None:
        # Six hex characters of a digest: enough to distinguish keys in a log
        # line, not enough to be worth anything to a reader.
        self.fingerprint = hashlib.sha256(self.value.encode()).hexdigest()[:6]

    @property
    def label(self) -> str:
        return f"#{self.index + 1}:{self.fingerprint}"


class ApiKeyPool:
    """A provider's keys, handed out least-used-first among those available."""

    def __init__(
        self,
        keys: Sequence[str],
        *,
        provider: str,
        clock: Callable[[], float] = time.monotonic,
        default_cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        cleaned = [key.strip() for key in keys if key and key.strip()]
        # Duplicates would look like extra capacity and deliver none — the same
        # quota counted twice.
        unique = list(dict.fromkeys(cleaned))
        if not unique:
            raise ValueError(f"{provider}: no API keys supplied")
        if len(unique) < len(cleaned):
            log.warning(
                "llm.duplicate_keys_ignored",
                provider=provider,
                supplied=len(cleaned),
                distinct=len(unique),
            )

        self.provider = provider
        self._clock = clock
        self._default_cooldown = default_cooldown
        self._keys = [_Key(value=key, index=index) for index, key in enumerate(unique)]

    def __len__(self) -> int:
        return len(self._keys)

    def budget_key(self, key: str) -> str:
        """The HTTP client's rate-budget name for this key.

        Distinct per key, which is the whole point: a shared budget would meter
        four keys as though they were one.
        """
        for candidate in self._keys:
            if candidate.value == key:
                return f"llm:{self.provider}:{candidate.fingerprint}"
        return f"llm:{self.provider}"

    def acquire(self) -> str:
        """The least-used key that is not cooling down.

        Least-used rather than round-robin so that a key added mid-run, or one
        just released from a cooldown, is brought up to par instead of waiting
        its turn behind keys that have been carrying the load.
        """
        now = self._clock()
        available = [key for key in self._keys if key.available_at <= now]
        if not available:
            soonest = min(key.available_at for key in self._keys) - now
            raise AllKeysExhausted(len(self._keys), max(soonest, 0.0))

        chosen = min(available, key=lambda key: (key.uses, key.index))
        chosen.uses += 1
        return chosen.value

    def penalise(self, key: str, retry_after: float | None) -> None:
        """Park a key that has just reported a rate limit."""
        for candidate in self._keys:
            if candidate.value != key:
                continue
            cooldown = retry_after if retry_after and retry_after > 0 else self._default_cooldown
            candidate.available_at = self._clock() + cooldown
            candidate.rate_limits += 1
            log.info(
                "llm.key_parked",
                provider=self.provider,
                key=candidate.label,
                cooldown_seconds=round(cooldown, 1),
                available=sum(1 for k in self._keys if k.available_at <= self._clock()),
                of=len(self._keys),
            )
            return

    def stats(self) -> list[dict[str, object]]:
        """Per-key usage, for `models-check` and incident review. No secrets."""
        now = self._clock()
        return [
            {
                "key": key.label,
                "uses": key.uses,
                "rate_limits": key.rate_limits,
                "cooling_down_for": round(max(key.available_at - now, 0.0), 1),
            }
            for key in self._keys
        ]
