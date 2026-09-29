"""SQLite crawl storage.

One database per run (``<run_dir>/crawl.db``). Everything the crawler learns is
written as it goes, and each page is committed in one transaction together with
its queue update, so a quit or crash loses at most the page in flight. Opening
an existing database resumes the run.

Tables:
- ``meta``: key/value facts about the run (campaign, status, timestamps).
- ``entries``: the campaign's entry links and the page each one landed on.
- ``queue``: the BFS frontier. ``state`` is pending, done or skipped.
- ``pages``: one row per page, keyed by its normalized final URL.
- ``aliases``: every requested URL -> the final URL it resolved to (redirects).
- ``links``: every outbound link found on a page, in scope or not.
- ``operator_actions``: every decision the operator made, for the audit trail.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS entries (
    idx INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    requested_url TEXT NOT NULL,
    node_url TEXT NOT NULL,           -- normalized URL; the final URL once crawled
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS queue (
    url TEXT PRIMARY KEY,
    depth INTEGER NOT NULL,
    parent TEXT,
    seq INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    requested_url TEXT,
    status TEXT NOT NULL,             -- ok, http_error, skipped, robots, offsite, not_fetched
    depth INTEGER,
    http_status INTEGER,
    load_ms INTEGER,
    redirect_chain TEXT,              -- JSON list of URLs
    canonical TEXT,
    title TEXT,
    meta_description TEXT,
    headings TEXT,                    -- JSON list of [level, text]
    body_text TEXT,
    form_present INTEGER,
    jsonld_types TEXT,                -- JSON list; empty list = no structured data
    raw_text_len INTEGER,
    rendered_text_len INTEGER,
    js_dependent INTEGER,
    screenshot TEXT,
    win INTEGER NOT NULL DEFAULT 0,
    win_source TEXT,
    error TEXT,
    crawled_at TEXT
);
CREATE TABLE IF NOT EXISTS aliases (url TEXT PRIMARY KEY, final_url TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY,
    src TEXT NOT NULL,
    href TEXT,
    url TEXT,                         -- normalized; NULL if not crawlable (mailto: etc.)
    text TEXT,
    region TEXT,
    in_scope INTEGER NOT NULL,
    operator INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS links_src ON links(src);
CREATE TABLE IF NOT EXISTS operator_actions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    url TEXT,
    problem TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
"""

# Page statuses whose outbound links are known ("explored" in graph terms).
EXPLORED_STATUSES = ("ok", "http_error")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class PageRecord:
    url: str
    status: str
    requested_url: str | None = None
    depth: int | None = None
    http_status: int | None = None
    load_ms: int | None = None
    redirect_chain: list[str] = field(default_factory=list)
    canonical: str | None = None
    title: str | None = None
    meta_description: str | None = None
    headings: list[tuple[int, str]] = field(default_factory=list)
    body_text: str | None = None
    form_present: bool | None = None
    jsonld_types: list[str] | None = None
    raw_text_len: int | None = None
    rendered_text_len: int | None = None
    js_dependent: bool | None = None
    screenshot: str | None = None
    win: bool = False
    win_source: str | None = None
    error: str | None = None


@dataclass
class LinkRecord:
    href: str | None
    url: str | None
    text: str
    region: str
    in_scope: bool
    operator: bool = False


@dataclass
class QueueItem:
    url: str
    depth: int
    parent: str | None


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ meta

    def set_meta(self, **values: object) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                [(k, json.dumps(v)) for k, v in values.items()],
            )

    def meta(self, key: str, default: object = None) -> object:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # ------------------------------------------------------------------ entries

    def add_entry(self, idx: int, label: str, requested_url: str, node_url: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO entries(idx, label, requested_url, node_url) VALUES (?, ?, ?, ?)",
                (idx, label, requested_url, node_url),
            )

    def update_entry(self, node_url: str, *, new_node_url: str | None = None, status: str | None = None) -> None:
        with self.db:
            if new_node_url:
                self.db.execute("UPDATE entries SET node_url = ? WHERE node_url = ?", (new_node_url, node_url))
                node_url = new_node_url
            if status:
                self.db.execute("UPDATE entries SET status = ? WHERE node_url = ?", (status, node_url))

    def entries(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM entries ORDER BY idx").fetchall()

    def is_entry(self, url: str) -> bool:
        return self.db.execute("SELECT 1 FROM entries WHERE node_url = ?", (url,)).fetchone() is not None

    # ------------------------------------------------------------------ queue

    def enqueue(self, url: str, depth: int, parent: str | None) -> bool:
        """Add a URL to the frontier. Returns False if it was already known."""
        if self.resolve(url) != url or self.has_page(url):
            return False
        seq = self.db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM queue").fetchone()[0]
        cur = self.db.execute(
            "INSERT OR IGNORE INTO queue(url, depth, parent, seq) VALUES (?, ?, ?, ?)", (url, depth, parent, seq)
        )
        self.db.commit()
        return cur.rowcount == 1

    def next_pending(self) -> QueueItem | None:
        """Breadth-first: shallowest first, then discovery order."""
        row = self.db.execute(
            "SELECT url, depth, parent FROM queue WHERE state = 'pending' ORDER BY depth, seq LIMIT 1"
        ).fetchone()
        return QueueItem(row["url"], row["depth"], row["parent"]) if row else None

    def mark_queue(self, url: str, state: str) -> None:
        with self.db:
            self.db.execute("UPDATE queue SET state = ? WHERE url = ?", (state, url))

    def pending_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM queue WHERE state = 'pending'").fetchone()[0]

    # ------------------------------------------------------------------ pages

    def has_page(self, url: str) -> bool:
        return self.db.execute("SELECT 1 FROM pages WHERE url = ?", (url,)).fetchone() is not None

    def explored_count(self) -> int:
        q = f"SELECT COUNT(*) FROM pages WHERE status IN ({','.join('?' * len(EXPLORED_STATUSES))})"
        return self.db.execute(q, EXPLORED_STATUSES).fetchone()[0]

    def save_page(self, page: PageRecord, links: list[LinkRecord], queue_url: str | None = None) -> None:
        """Write a page, its links and its alias, and close its queue item, atomically."""
        with self.db:
            self.db.execute(
                """INSERT OR REPLACE INTO pages(url, requested_url, status, depth, http_status, load_ms,
                   redirect_chain, canonical, title, meta_description, headings, body_text, form_present,
                   jsonld_types, raw_text_len, rendered_text_len, js_dependent, screenshot, win, win_source,
                   error, crawled_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    page.url, page.requested_url, page.status, page.depth, page.http_status, page.load_ms,
                    json.dumps(page.redirect_chain), page.canonical, page.title, page.meta_description,
                    json.dumps(page.headings), page.body_text,
                    None if page.form_present is None else int(page.form_present),
                    None if page.jsonld_types is None else json.dumps(page.jsonld_types),
                    page.raw_text_len, page.rendered_text_len,
                    None if page.js_dependent is None else int(page.js_dependent),
                    page.screenshot, int(page.win), page.win_source, page.error, now(),
                ),
            )
            self.db.execute("DELETE FROM links WHERE src = ? AND operator = 0", (page.url,))
            self.db.executemany(
                "INSERT INTO links(src, href, url, text, region, in_scope, operator) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(page.url, lk.href, lk.url, lk.text, lk.region, int(lk.in_scope), int(lk.operator)) for lk in links],
            )
            for alias in {page.url, page.requested_url, *page.redirect_chain} - {None}:
                self.db.execute("INSERT OR REPLACE INTO aliases(url, final_url) VALUES (?, ?)", (alias, page.url))
            if queue_url:
                self.db.execute("UPDATE queue SET state = 'done' WHERE url = ?", (queue_url,))
            # The final URL may itself be queued (reached directly elsewhere); it is done now.
            self.db.execute("UPDATE queue SET state = 'done' WHERE url = ? AND state = 'pending'", (page.url,))

    def add_alias(self, url: str, final_url: str) -> None:
        """``url`` turned out to be another spelling of an already-crawled page."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO aliases(url, final_url) VALUES (?, ?)", (url, final_url))
            self.db.execute("UPDATE queue SET state = 'done' WHERE url = ?", (url,))

    def mark_win(self, url: str, source: str) -> None:
        with self.db:
            self.db.execute("UPDATE pages SET win = 1, win_source = ? WHERE url = ?", (source, url))

    def add_operator_link(self, src: str, url: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO links(src, href, url, text, region, in_scope, operator) VALUES (?, ?, ?, ?, ?, 1, 1)",
                (src, url, url, "(operator)", "operator"),
            )

    def pages(self) -> Iterator[sqlite3.Row]:
        return iter(self.db.execute("SELECT * FROM pages ORDER BY url").fetchall())

    def links(self) -> Iterator[sqlite3.Row]:
        return iter(self.db.execute("SELECT * FROM links ORDER BY id").fetchall())

    def resolve(self, url: str) -> str:
        """The final URL a requested URL is known to land on (itself if unknown)."""
        row = self.db.execute("SELECT final_url FROM aliases WHERE url = ?", (url,)).fetchone()
        return row["final_url"] if row else url

    # ------------------------------------------------------------------ operator audit

    def log_action(self, url: str | None, problem: str | None, action: str, detail: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO operator_actions(ts, url, problem, action, detail) VALUES (?, ?, ?, ?, ?)",
                (now(), url, problem, action, detail),
            )

    def operator_actions(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM operator_actions ORDER BY id").fetchall()
