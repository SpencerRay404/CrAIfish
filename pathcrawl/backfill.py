"""Fill columns added after a run was crawled, from data already in its crawl.db.

No pages are fetched. ``pathcrawl backfill-links --run DIR`` runs every step:

- ``links.mc_id``: the campaign tag (``scope.capture_params``, e.g.
  ``WT.mc_id``) parsed from each link's raw ``href``.
- Pages stored twice under URLs that differ only by params the current
  config strips (e.g. ``?msockid=...``) are merged into one page under the
  clean URL. Link targets are renormalized the same way, and the old spellings
  become aliases, so nothing else in the run has to change.
- ``pages.is_dead`` / ``dead_reason``: 404, 410 and soft 404s, from the stored
  status, title and text (``pathcrawl.dead.backfill_dead``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import json

from pathcrawl.extract import h1_counts
from pathcrawl.normalize import captured_params


@dataclass
class LinkBackfill:
    links: int = 0
    links_tagged: int = 0
    distinct_tags: int = 0
    source_pages: int = 0
    duplicate_pages: list[list[str]] = field(default_factory=list)  # merged groups
    links_renormalized: int = 0
    dead_pages: int = 0
    page_tags: int = 0


def backfill_links(store, scope) -> LinkBackfill:
    """Set ``links.mc_id`` from raw hrefs, merge duplicate pages, mark dead pages."""
    out = LinkBackfill()
    out.duplicate_pages = duplicate_pages(store, scope)
    out.links_renormalized = merge_duplicate_pages(store, scope, out.duplicate_pages)
    if out.duplicate_pages:
        previous = store.meta("merged_pages", []) or []
        store.set_meta(merged_pages=previous + out.duplicate_pages)
    rows = store.db.execute("SELECT id, href FROM links").fetchall()
    first = scope.capture_params[0] if scope.capture_params else ""
    updates = []
    for r in rows:
        found = captured_params(r["href"], scope.capture_params)
        updates.append((found.get(first), json.dumps(found) if found else None, r["id"]))
    with store.db:
        store.db.executemany("UPDATE links SET mc_id = ?, params = ? WHERE id = ?", updates)
        # H1s with and without text
        for r in store.db.execute("SELECT url, headings FROM pages WHERE headings IS NOT NULL").fetchall():
            store.db.execute("UPDATE pages SET h1_count = ?, h1_empty_count = ? WHERE url = ?",
                             (*h1_counts(json.loads(r["headings"])), r["url"]))
        # canonical URLs are stored normalized, like every other URL
        for r in store.db.execute("SELECT url, canonical FROM pages WHERE canonical IS NOT NULL").fetchall():
            new = scope.normalize(r["canonical"])
            if new and new != r["canonical"]:
                store.db.execute("UPDATE pages SET canonical = ? WHERE url = ?", (new, r["url"]))
    out.links = len(rows)
    stats = store.db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT mc_id), COUNT(DISTINCT src) FROM links WHERE mc_id IS NOT NULL"
    ).fetchone()
    out.links_tagged, out.distinct_tags, out.source_pages = stats[0], stats[1], stats[2]
    from pathcrawl.dead import backfill_dead

    out.dead_pages = backfill_dead(store)
    out.page_tags = backfill_page_tags(store)
    return out


def backfill_page_tags(store) -> int:
    """page_tags rows for loaded pages crawled before page tags were recorded,
    rebuilt from what crawl.db holds: title, description, H1, path, structured
    data types and the page's own menu link labels (their group labels, the
    breadcrumb, Open Graph and keyword tags weren't stored, so they stay
    empty). Marked source = backfill."""
    from urllib.parse import urlsplit

    have = {r[0] for r in store.db.execute("SELECT url FROM page_tags")}
    rows = store.db.execute(
        """SELECT url, title, meta_description, headings, jsonld_types, microdata_types, rdfa_types
           FROM pages WHERE status IN ('ok', 'http_error')""").fetchall()
    n = 0
    with store.db:
        for r in rows:
            if r["url"] in have:
                continue
            headings = json.loads(r["headings"]) if r["headings"] else []
            types = set()
            for col in ("jsonld_types", "microdata_types", "rdfa_types"):
                types |= set(json.loads(r[col]) if r[col] else [])
            labels = []
            for lk in store.db.execute(
                    "SELECT DISTINCT text FROM links WHERE src = ? AND region IN ('nav', 'header', 'footer') "
                    "AND text != '' ORDER BY id", (r["url"],)):
                labels.append(["", lk["text"]])
            store.save_page_tags(r["url"], "backfill", r["title"], r["meta_description"], {
                "h1": next((t for level, t in headings if level == 1 and t.strip()), ""),
                "url_path_segments": [s for s in urlsplit(r["url"]).path.split("/") if s],
                "breadcrumb": [], "schema_types": sorted(types), "og_type": None, "article_tags": [],
                "meta_keywords": [], "service_entities": [], "nav_labels": labels[:300],
            })
            n += 1
    return n


def duplicate_pages(store, scope) -> list[list[str]]:
    """Stored page URLs the current normalization changes: groups that map to one
    URL, and single pages stored under a spelling that is no longer normal."""
    groups: dict[str, list[str]] = defaultdict(list)
    for row in store.db.execute("SELECT url FROM pages ORDER BY url"):
        groups[scope.normalize(row["url"]) or row["url"]].append(row["url"])
    return [urls for key, urls in sorted(groups.items()) if len(urls) > 1 or urls[0] != key]


STATUS_RANK = {"ok": 0, "http_error": 1}


def merge_duplicate_pages(store, scope, groups: list[list[str]]) -> int:
    """Merge each group into one page under its normalized URL. Returns the
    number of link targets rewritten to their current normalized form.

    The kept row is the best-loaded member (ok before http_error before the
    rest, then the one with most links). The other members' links move to it,
    exact duplicate links are dropped, and the old URLs become aliases.
    """
    db = store.db
    with db:
        renamed = 0
        for r in db.execute("SELECT id, url FROM links WHERE url IS NOT NULL").fetchall():
            new = scope.normalize(r["url"])
            if new and new != r["url"]:
                db.execute("UPDATE links SET url = ? WHERE id = ?", (new, r["id"]))
                renamed += 1
        for group in groups:
            key = scope.normalize(group[0]) or group[0]

            def rank(u: str) -> tuple:
                row = db.execute("SELECT status FROM pages WHERE url = ?", (u,)).fetchone()
                n = db.execute("SELECT COUNT(*) FROM links WHERE src = ?", (u,)).fetchone()[0]
                return (STATUS_RANK.get(row["status"] if row else "", 9), -n, u != key, u)

            keep, *others = sorted(group, key=rank)
            for u in others:
                db.execute("UPDATE links SET src = ? WHERE src = ?", (keep, u))
                db.execute("DELETE FROM pages WHERE url = ?", (u,))
            if keep != key:
                db.execute("UPDATE pages SET url = ? WHERE url = ?", (key, keep))
                db.execute("UPDATE links SET src = ? WHERE src = ?", (key, keep))
            for u in group:
                if u == key:
                    continue
                db.execute("INSERT OR REPLACE INTO aliases(url, final_url) VALUES (?, ?)", (u, key))
                db.execute("UPDATE aliases SET final_url = ? WHERE final_url = ?", (key, u))
                db.execute("UPDATE entries SET node_url = ? WHERE node_url = ?", (key, u))
                db.execute("DELETE FROM queue WHERE url = ?", (u,))
                db.execute("DELETE FROM page_entities WHERE url = ?", (u,))
                db.execute("UPDATE lead_attribution SET src = ? WHERE src = ?", (key, u))
            # the same link found on two spellings of one page is one link
            db.execute(
                """DELETE FROM links WHERE src = ? AND id NOT IN (
                       SELECT MIN(id) FROM links WHERE src = ?
                       GROUP BY href, url, text, region, operator, COALESCE(mc_id, ''))""",
                (key, key),
            )
        for r in db.execute("SELECT url, final_url FROM aliases").fetchall():
            new = scope.normalize(r["final_url"])
            if new and new != r["final_url"] and db.execute("SELECT 1 FROM pages WHERE url = ?", (new,)).fetchone():
                db.execute("UPDATE aliases SET final_url = ? WHERE url = ?", (new, r["url"]))
    return renamed
