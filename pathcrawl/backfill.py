"""Fill columns added after a run was crawled, from data already in its crawl.db.

No pages are fetched. ``pathcrawl backfill-links --run DIR`` runs every step:

- ``links.mc_id``: the campaign tag (``scope.capture_params``, e.g.
  ``WT.mc_id``) parsed from each link's raw ``href``.
- A check for pages stored twice under URLs that differ only by params the
  current config strips (e.g. ``?msockid=...``). They are reported, not merged.
- ``pages.is_dead`` / ``dead_reason``: 404, 410 and soft 404s, from the stored
  status, title and text (``pathcrawl.dead.backfill_dead``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from pathcrawl.normalize import captured_param


@dataclass
class LinkBackfill:
    links: int = 0
    links_tagged: int = 0
    distinct_tags: int = 0
    source_pages: int = 0
    duplicate_pages: list[list[str]] = field(default_factory=list)
    dead_pages: int = 0


def backfill_links(store, scope) -> LinkBackfill:
    """Set ``links.mc_id`` on every link from its raw href, replacing earlier values."""
    rows = store.db.execute("SELECT id, href FROM links").fetchall()
    updates = [(captured_param(r["href"], scope.capture_params), r["id"]) for r in rows]
    with store.db:
        store.db.executemany("UPDATE links SET mc_id = ? WHERE id = ?", updates)
    out = LinkBackfill(links=len(rows))
    stats = store.db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT mc_id), COUNT(DISTINCT src) FROM links WHERE mc_id IS NOT NULL"
    ).fetchone()
    out.links_tagged, out.distinct_tags, out.source_pages = stats[0], stats[1], stats[2]
    out.duplicate_pages = duplicate_pages(store, scope)
    from pathcrawl.dead import backfill_dead

    out.dead_pages = backfill_dead(store)
    return out


def duplicate_pages(store, scope) -> list[list[str]]:
    """Groups of stored page URLs that the current normalization maps to one URL."""
    groups: dict[str, list[str]] = defaultdict(list)
    for row in store.db.execute("SELECT url FROM pages ORDER BY url"):
        groups[scope.normalize(row["url"]) or row["url"]].append(row["url"])
    return [urls for _, urls in sorted(groups.items()) if len(urls) > 1]
