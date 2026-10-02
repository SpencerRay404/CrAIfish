"""External entry points: posts collected by hand (e.g. LinkedIn, which sits
behind a login wall and is never crawled) and the client pages they link to.

Input is a CSV filled in by hand (``campaigns[].external_seeds``), one row per
outbound link found in a post:

- ``seed_url``: the post.
- ``post_title`` (optional): headline or first line.
- ``outbound_url_raw``: the link as it appears in the post (often a short link).
- ``anchor_text``, ``link_order`` (optional).
- ``outbound_resolved_url``: where the link lands, with its query string. The
  campaign tag (``scope.capture_params``) is read from here, else from the raw link.

Each post becomes a node with status ``external`` (``channel`` from the
campaign platform) and each outbound link a row in ``links``. Tags therefore
join to leads exactly like on-site links. A landing page inside the allowed
domains becomes an entry link and is queued at depth 0. A link that resolves
to another post becomes another seed.

Only the post URL, title, date and outbound links are kept. Nothing else in the
file (commenters, reactions, profiles) is read.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pathcrawl.normalize import captured_param

EXTERNAL = "external"
ACTIVITY_ID = re.compile(r"activity[-:](\d{19})")
POST_HOSTS = ("linkedin.com",)
COLUMNS = ("seed_url", "post_title", "outbound_url_raw", "anchor_text", "link_order", "outbound_resolved_url")
# Other names accepted for a column (header text lowercased, spaces as underscores).
COLUMN_ALIASES = {
    "outbound_resolved_url": ("resolved_url", "outbound_url_resolved", "landing_url", "landing_page",
                              "landing_page_url", "final_url", "destination_url", "resolved"),
    "outbound_url_raw": ("outbound_url", "link_url", "raw_url", "short_link", "post_link"),
    "seed_url": ("post_url", "linkedin_url", "seed"),
    "post_title": ("title", "headline"),
}
SHORTENER_HOSTS = ("lnkd.in", "spr.ly", "bit.ly", "ow.ly", "t.co", "buff.ly", "tinyurl.com")


class SeedFileError(Exception):
    pass


def normalize_seed_url(url: str | None) -> str | None:
    """Normalization used to compare posts and landing pages: lowercase scheme
    and host, no query (tracking IDs, utm_*, rcm), no fragment, no trailing slash."""
    url = (url or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return None
    path = parts.path.rstrip("/") or ""
    return urlunsplit((parts.scheme.lower() or "https", host, path, "", ""))


def is_post(url: str | None) -> bool:
    host = urlsplit(url or "").hostname or ""
    return any(host == h or host.endswith("." + h) for h in POST_HOSTS)


def post_date_derived(url: str | None) -> str | None:
    """Post date from a LinkedIn activity ID: its top bits (id >> 22) are
    milliseconds since the Unix epoch."""
    m = ACTIVITY_ID.search(url or "")
    if not m:
        return None
    ms = int(m.group(1)) >> 22
    return datetime.fromtimestamp(ms / 1000, tz=UTC).date().isoformat()


@dataclass
class SeedLink:
    seed: str
    raw: str
    resolved: str | None
    target: str | None  # normalized client page (scope.normalize), or the seed it resolves to
    kind: str  # page, seed, offsite or none (no landing page)
    in_scope: bool
    mc_id: str | None
    anchor_text: str = ""
    link_order: str = ""


@dataclass
class Seed:
    url: str
    title: str
    date: str | None
    links: list[SeedLink] = field(default_factory=list)


@dataclass
class SeedIngest:
    seeds: list[Seed]
    skipped_seeds: list[str]  # already an ad URL or entry link
    duplicate_rows: int
    new_entries: list[str]  # landing pages added as entry links
    existing_entries: list[str]  # landing pages that already were entry links
    # every outbound link that did not become an entry link, and why
    not_entries: list[tuple[str, str]] = field(default_factory=list)
    columns_used: dict[str, str] = field(default_factory=dict)


class SeedRows(list):
    """The rows of a seed file, plus which header fed each column."""

    columns: dict[str, str] = {}


def read_rows(path: Path) -> SeedRows:
    try:
        f = open(path, newline="", encoding="utf-8-sig")
    except OSError as e:
        raise SeedFileError(f"cannot read seed file {path}: {e}") from None
    with f:
        reader = csv.DictReader(f)
        mapping = column_mapping(reader.fieldnames or [])
        if "seed_url" not in mapping:
            raise SeedFileError(f"{path} has no seed_url column (found: {', '.join(reader.fieldnames or [])})")
        rows = SeedRows({col: (r.get(orig) or "").strip() for col, orig in mapping.items()} for r in reader)
    rows.columns = mapping
    return rows


def column_mapping(fieldnames: list[str]) -> dict[str, str]:
    """Our column name -> the header in the file, accepting the aliases above."""
    norm = {re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_"): h for h in fieldnames}
    out = {}
    for col in COLUMNS:
        for name in (col, *COLUMN_ALIASES.get(col, ())):
            if name in norm:
                out[col] = norm[name]
                break
    return out


def plan(rows: list[dict[str, str]], scope, ad_urls: list[str], entry_urls: list[str], win=None,
         dead: set[str] = frozenset()) -> SeedIngest:
    """Dedupe the rows and decide what each becomes (no database writes).

    A landing page that is a win keeps its edge but is not made an entry link:
    the journey is already complete there. Nor is a landing page known to be
    dead; its edge is kept and reported (to_dead)."""
    known = {normalize_seed_url(u) for u in ad_urls} | {normalize_seed_url(u) for u in entry_urls}
    entries = {scope.normalize(u) for u in entry_urls}
    seeds: dict[str, Seed] = {}
    skipped, dupes, pairs = [], 0, set()
    columns = getattr(rows, "columns", {})
    for r in rows:
        seed = normalize_seed_url(r.get("seed_url"))
        if not seed:
            continue
        if seed in known:
            if seed not in skipped:
                skipped.append(seed)
            continue
        s = seeds.setdefault(seed, Seed(seed, r.get("post_title", ""), post_date_derived(r.get("seed_url"))))
        if not s.title and r.get("post_title"):
            s.title = r["post_title"]
        raw = r.get("outbound_url_raw", "")
        resolved = r.get("outbound_resolved_url") or None
        key = normalize_seed_url(resolved or raw)
        if (seed, key) in pairs:
            dupes += 1
            continue
        pairs.add((seed, key))
        mc_id = captured_param(resolved, scope.capture_params) or captured_param(raw, scope.capture_params)
        if not (resolved or raw):
            link = SeedLink(seed, "", None, None, "none", False, None)
        elif is_post(resolved or raw):
            link = SeedLink(seed, raw, resolved, normalize_seed_url(resolved or raw), "seed", False, mc_id)
        else:
            target = scope.normalize(resolved or raw)
            in_scope = bool(target and scope.in_scope(target))
            link = SeedLink(seed, raw, resolved, target, "page" if in_scope else "offsite", in_scope, mc_id)
        link.anchor_text, link.link_order = r.get("anchor_text", ""), r.get("link_order", "")
        s.links.append(link)

    # a post that another post links to becomes a seed itself
    for s in list(seeds.values()):
        for link in s.links:
            if link.kind == "seed" and link.target not in seeds and link.target not in known:
                seeds[link.target] = Seed(link.target, "", post_date_derived(link.resolved or link.raw))

    landing, not_entries = set(), {}
    for s in seeds.values():
        for lk in s.links:
            target = lk.target or lk.raw or ""
            if lk.kind == "none":
                not_entries[f"{s.url} (no link)"] = "no landing page in the row"
            elif lk.kind == "seed":
                not_entries[target] = "another post (added as a seed)"
            elif lk.kind == "offsite":
                not_entries[target] = _offsite_reason(lk, scope)
            elif lk.target in dead:
                not_entries[target] = "dead page"
            elif win is not None and win.url_matches(lk.target):
                not_entries[target] = "win page"
            else:
                landing.add(lk.target)
    landing = sorted(landing)
    return SeedIngest(
        seeds=list(seeds.values()),
        skipped_seeds=skipped,
        duplicate_rows=dupes,
        new_entries=[u for u in landing if u not in entries],
        existing_entries=[u for u in landing if u in entries],
        not_entries=sorted(not_entries.items()),
        columns_used=dict(columns),
    )


def _offsite_reason(lk: SeedLink, scope) -> str:
    url = lk.target or lk.raw or ""
    host = urlsplit(url).hostname or ""
    if host in SHORTENER_HOSTS and not lk.resolved:
        return f"short link not resolved (outbound_resolved_url is empty; {host} is not on an allowed domain)"
    if not scope.domain_allowed(url):
        return f"host {host or '?'} is not in scope.allowed_domains"
    if not scope.locale_allowed(url):
        return "excluded by the locale filters"
    return "not in scope"


def ingest(store, config, campaign, path: Path) -> SeedIngest:
    """Write the seeds into the run: post nodes, their links, and new entry links."""
    from pathcrawl.store import LinkRecord, PageRecord

    entry_urls = [e["requested_url"] for e in store.entries()] or [e.url for e in campaign.entry_links]
    dead = {r["url"] for r in store.db.execute("SELECT url FROM pages WHERE is_dead = 1")}
    result = plan(read_rows(path), config.scope, campaign.ad_urls, entry_urls, config.win, dead)
    channel = campaign.platform
    for s in result.seeds:
        links = [
            LinkRecord(href=lk.raw or lk.resolved, url=lk.target if lk.kind != "none" else None,
                       text=lk.anchor_text, region=EXTERNAL, in_scope=lk.in_scope, mc_id=lk.mc_id)
            for lk in s.links if lk.kind != "none"
        ]
        store.save_page(PageRecord(url=s.url, status=EXTERNAL, title=s.title or None), links)
        store.set_external(s.url, channel, s.date)
    next_idx = (store.db.execute("SELECT COALESCE(MAX(idx), -1) + 1 FROM entries").fetchone()[0])
    titles = {lk.target: s.title for s in result.seeds for lk in s.links if lk.kind == "page"}
    for i, url in enumerate(result.new_entries):
        label = f"{channel}: {titles.get(url) or url}"
        store.add_entry(next_idx + i, label, url, url)
        store.enqueue(url, 0, None)
    store.set_meta(external_seeds={
        "file": str(path),
        "seeds": len(result.seeds),
        "skipped_seeds": result.skipped_seeds,
        "duplicate_rows": result.duplicate_rows,
        "new_entries": result.new_entries,
        "existing_entries": result.existing_entries,
    })
    return result
