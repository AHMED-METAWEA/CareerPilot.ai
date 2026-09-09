"""Skill taxonomy: YAML in Git, rows in Postgres (§10.2).

The same shape as the board registry: curated data, reviewed as data, synced
into tables. Aliases are additive — an alias a human added through the review
queue is never removed by a sync, because the sync file is a seed, not the
whole vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.safety.taxonomy import SkillEntry, SkillKind, SkillTaxonomy

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class TaxonomySyncReport:
    skills_created: int = 0
    skills_existing: int = 0
    aliases_created: int = 0


def load_seed(path: Path) -> list[SkillEntry]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or []
    entries: list[SkillEntry] = []
    seen: set[str] = set()
    for item in data:
        name = item["name"]
        if name in seen:
            raise ValueError(f"{path}: duplicate skill '{name}'")
        seen.add(name)
        entries.append(
            SkillEntry(
                canonical_name=name,
                kind=SkillKind(item["kind"]),
                aliases=tuple(item.get("aliases") or ()),
                esco_uri=item.get("esco_uri"),
            )
        )
    return entries


def sync_skills(session: Session, entries: list[SkillEntry]) -> TaxonomySyncReport:
    """Upsert skills and their aliases. Never deletes."""
    report = TaxonomySyncReport()
    for entry in entries:
        row = session.execute(
            text(
                """
                INSERT INTO skills (canonical_name, kind, esco_uri)
                VALUES (:name, :kind, :esco)
                ON CONFLICT (canonical_name) DO UPDATE SET kind = EXCLUDED.kind
                RETURNING id, (xmax = 0) AS created
                """
            ),
            {"name": entry.canonical_name, "kind": entry.kind.value, "esco": entry.esco_uri},
        ).one()
        if row.created:
            report.skills_created += 1
        else:
            report.skills_existing += 1

        for alias in entry.aliases:
            inserted = session.execute(
                text(
                    """
                    INSERT INTO skill_aliases (skill_id, alias, lang)
                    VALUES (:skill_id, :alias, :lang)
                    ON CONFLICT (alias, lang) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "skill_id": row.id,
                    "alias": alias,
                    # Arabic aliases are tagged so the review queue can be
                    # filtered by language; matching itself is language-agnostic.
                    "lang": "ar" if _is_arabic(alias) else "en",
                },
            ).first()
            if inserted:
                report.aliases_created += 1

    log.info(
        "taxonomy.synced",
        created=report.skills_created,
        existing=report.skills_existing,
        aliases=report.aliases_created,
    )
    return report


def load_taxonomy(session: Session, *, fuzzy_threshold: float = 0.90) -> SkillTaxonomy:
    """Build the in-memory index from the database."""
    rows = session.execute(
        text(
            """
            SELECT s.canonical_name, s.kind, s.esco_uri,
                   COALESCE(array_agg(a.alias) FILTER (WHERE a.alias IS NOT NULL), '{}') AS aliases
              FROM skills s
              LEFT JOIN skill_aliases a ON a.skill_id = s.id
             GROUP BY s.id, s.canonical_name, s.kind, s.esco_uri
            """
        )
    ).all()
    entries = [
        SkillEntry(
            canonical_name=row.canonical_name,
            kind=SkillKind(row.kind),
            aliases=tuple(row.aliases or ()),
            esco_uri=row.esco_uri,
        )
        for row in rows
    ]
    return SkillTaxonomy(entries, fuzzy_threshold=fuzzy_threshold)


def record_unmapped(session: Session, tokens: list[str]) -> int:
    """Queue tokens that did not resolve, for taxonomy review (§10.2).

    Counted rather than merely listed: a token seen two hundred times is a gap
    in the vocabulary, one seen once is probably a typo in someone's CV.
    """
    recorded = 0
    for token in tokens:
        if not token.strip():
            continue
        session.execute(
            text(
                """
                INSERT INTO unmapped_skills (token, lang)
                VALUES (:token, :lang)
                ON CONFLICT (token, lang) DO UPDATE
                   SET occurrences = unmapped_skills.occurrences + 1,
                       last_seen_at = now()
                """
            ),
            {"token": token.strip()[:200], "lang": "ar" if _is_arabic(token) else "en"},
        )
        recorded += 1
    return recorded


def _is_arabic(text_value: str) -> bool:
    return any("؀" <= char <= "ۿ" for char in text_value)
