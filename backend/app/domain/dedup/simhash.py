"""SimHash-64 over word 5-gram shingles (§11.3 stage 4).

Near-duplicate detection at the text level. Two postings of the same role
syndicated through different sources differ in boilerplate but share nearly all
their shingles, so their hashes land within a Hamming distance of a few bits.

Postgres has no unsigned 64-bit integer, so hashes are stored signed. The
conversion is lossless and the Hamming distance is computed on the unsigned
representation — `to_signed`/`from_signed` exist so that never gets fumbled.

**Calibration** (measured on six real ATS descriptions, 5.3k–9.6k characters;
reproduced by `tests/integration/test_dedup_calibration.py`):

    same posting + syndication trailer      0–2 bits
    same posting, one paragraph dropped     0–4 bits
    same posting, whitespace normalised     0 bits
    same posting, 15% of the tail truncated 5–11 bits
    two different postings                  10–38 bits

The configured threshold of 3 (`dedup.simhash_hamming_max`) therefore catches
lightly-edited syndication and rejects unrelated postings with room to spare.
It deliberately does *not* stretch to cover truncated aggregator copies: at
5–11 bits those overlap the different-posting range, and a threshold that
caught them would start merging distinct jobs. Truncated copies are caught
earlier instead — by the exact-key, canonical-URL and title-blocking stages,
which do not degrade with length.
"""

from __future__ import annotations

import hashlib
import re

_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)

BITS = 64
_MASK = (1 << BITS) - 1
_SIGN_BIT = 1 << (BITS - 1)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, punctuation removed."""
    cleaned = _NON_WORD.sub(" ", text.casefold())
    return [t for t in _WS.split(cleaned) if t]


def shingles(tokens: list[str], size: int = 5) -> list[str]:
    """Overlapping word n-grams. Short texts yield a single whole-text shingle."""
    if len(tokens) < size:
        return [" ".join(tokens)] if tokens else []
    return [" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)]


def _hash64(value: str) -> int:
    return int.from_bytes(hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest(), "big")


def simhash64(text: str, *, shingle_size: int = 5) -> int:
    """Unsigned 64-bit SimHash of `text`. Empty input hashes to 0."""
    grams = shingles(tokenize(text), shingle_size)
    if not grams:
        return 0

    weights = [0] * BITS
    counts: dict[str, int] = {}
    for gram in grams:
        counts[gram] = counts.get(gram, 0) + 1

    for gram, weight in counts.items():
        h = _hash64(gram)
        for bit in range(BITS):
            if h >> bit & 1:
                weights[bit] += weight
            else:
                weights[bit] -= weight

    out = 0
    for bit in range(BITS):
        if weights[bit] > 0:
            out |= 1 << bit
    return out


def hamming(a: int, b: int) -> int:
    """Bit distance between two unsigned 64-bit hashes."""
    return ((a ^ b) & _MASK).bit_count()


def to_signed(value: int) -> int:
    """Unsigned 64-bit -> Postgres bigint."""
    value &= _MASK
    return value - (1 << BITS) if value & _SIGN_BIT else value


def from_signed(value: int) -> int:
    """Postgres bigint -> unsigned 64-bit."""
    return value & _MASK


def is_near_duplicate(a: int, b: int, *, max_distance: int = 3) -> bool:
    return hamming(a, b) <= max_distance
