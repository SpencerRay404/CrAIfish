"""Website health: dead pages, how easy content is to reach, and how
machine-readable each page is for search engines and AI agents (SEO / GEO).

Per page (``<client>_site_health.csv``), for every loaded page:

- ``clicks_from_home_all_links`` / ``clicks_from_home_body_links``: breadth-first
  search from ``health.home_url`` over every link, and over links in the page
  body only. Empty when the page can't be reached from home.
- ``inbound_links``: crawled pages linking to it. ``is_dead`` and
  ``dead_inbound_pages`` (see ``pathcrawl.dead``).
- ``has_title`` / ``title_duplicated`` and ``has_meta_description`` /
  ``meta_duplicated``: duplicate means the same lowercased text on more than
  one loaded page. ``h1_count`` (H1s with text; the check passes at exactly
  one) and ``h1_empty_count`` (empty H1s, reported on their own). ``canonical_self``: the canonical URL is the
  page itself (empty when there is no canonical).
- ``structured_data_types`` (JSON-LD), ``microdata_types``, ``rdfa_types``,
  ``og_properties``, ``hreflang`` and ``robots_meta``. The last five are empty
  for runs crawled before they were recorded.
- ``js_dependent``: the raw HTML has under half the rendered text.
  ``raw_text_share`` is raw over rendered text length.
- ``redirect_hops``. ``carries_lead_tags`` (a link on the page carries a
  campaign tag) and ``leads_allocated`` (``pathcrawl leads``).

Per host (``collect_site_signals``): robots.txt rules for named AI crawlers,
whether ``llms.txt`` and a sitemap exist, and how many crawled pages the
sitemap lists. These are fetched once per host, either at the end of a crawl
or with ``pathcrawl site-signals``.
"""

from __future__ import annotations

import csv
import gzip
import math
import json
import re
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

from pathcrawl.extract import BODY, h1_counts

AI_CRAWLERS = [
    "GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot", "Claude-User", "anthropic-ai",
    "PerplexityBot", "Perplexity-User", "Google-Extended", "Applebot-Extended", "CCBot", "Bytespider", "Meta-ExternalAgent",
]
MAX_SITEMAPS = 200
MAX_SITEMAP_URLS = 500_000


# --------------------------------------------------------------------------- per page


@dataclass
class PageHealth:
    url: str
    host: str
    section: str
    page_type: str
    status: str
    clicks_from_home_all_links: int | None
    clicks_from_home_body_links: int | None
    inbound_links: int
    is_dead: bool
    dead_inbound_pages: int
    has_title: bool
    title_duplicated: bool
    has_meta_description: bool
    meta_duplicated: bool
    h1_count: int  # H1s with text
    h1_empty_count: int  # H1s with no text (often a template's extra one)
    canonical_self: bool | None
    has_structured_data: bool
    structured_data_types: str
    microdata_types: str
    rdfa_types: str
    og_properties: str
    hreflang: str
    robots_meta: str
    js_dependent: bool | None
    raw_text_share: float | None
    redirect_hops: int
    carries_lead_tags: bool
    leads_allocated: int  # whole leads, rounded down (a lead is a whole record)
    carries_leads: bool  # the page has an allocated share, even if under one lead


def _bfs(g, start: str | None, edge_ok: Callable[[dict], bool]) -> dict[str, int]:
    if not start or start not in g:
        return {}
    dist = {start: 0}
    q = deque([start])
    while q:
        u = q.popleft()
        for v in sorted(g.successors(u)):
            if v not in dist and edge_ok(g.edges[u, v]):
                dist[v] = dist[u] + 1
                q.append(v)
    return dist


def click_depths(g, home: str | None) -> tuple[dict[str, int], dict[str, int]]:
    """Clicks from home over every link, and over body links only (operator jumps excluded)."""
    return (_bfs(g, home, lambda d: bool(d["regions"])),
            _bfs(g, home, lambda d: BODY in d["regions"]))


def _list(value: str | None) -> list[str] | None:
    return None if value is None else json.loads(value)


def page_health(run, categories, home: str | None) -> list[PageHealth]:
    store, g = run.store, run.graph
    loaded = [p for p in store.pages() if p["status"] in ("ok", "http_error")]
    by_url = {c.url: c for c in categories}
    titles = defaultdict(int)
    metas = defaultdict(int)
    for p in loaded:
        if p["title"]:
            titles[p["title"].strip().lower()] += 1
        if p["meta_description"]:
            metas[p["meta_description"].strip().lower()] += 1
    all_d, body_d = click_depths(g, home)
    dead_sources: dict[str, set[str]] = defaultdict(set)
    dead = {r["url"] for r in store.db.execute("SELECT url FROM pages WHERE is_dead = 1")}
    tagged = {r["src"] for r in store.db.execute("SELECT DISTINCT src FROM links WHERE mc_id IS NOT NULL")}
    leads = defaultdict(float)
    for r in store.lead_attribution():
        leads[r["src"]] += r["share"] if r["share"] is not None else r["leads_allocated"]
    for r in store.db.execute("SELECT src, url FROM links WHERE url IS NOT NULL"):
        t = store.resolve(r["url"])
        if t in dead and t != r["src"] and r["src"] not in dead:  # live linking pages only
            dead_sources[t].add(r["src"])

    out = []
    for p in loaded:
        url = p["url"]
        c = by_url.get(url)
        headings = json.loads(p["headings"]) if p["headings"] else []
        jsonld = json.loads(p["jsonld_types"]) if p["jsonld_types"] else []
        micro, rdfa = _list(p["microdata_types"]), _list(p["rdfa_types"])
        og, hreflang = _list(p["og_properties"]), _list(p["hreflang"])
        raw, rendered = p["raw_text_len"], p["rendered_text_len"]
        canonical = p["canonical"]
        chain = json.loads(p["redirect_chain"]) if p["redirect_chain"] else []
        out.append(PageHealth(
            url=url,
            host=urlsplit(url).hostname or "",
            section=c.section if c else "",
            page_type=c.page_type if c else "",
            status=p["status"],
            clicks_from_home_all_links=all_d.get(url),
            clicks_from_home_body_links=body_d.get(url),
            inbound_links=g.in_degree(url) if url in g else 0,
            is_dead=bool(p["is_dead"]),
            dead_inbound_pages=len(dead_sources.get(url, ())),
            has_title=bool(p["title"]),
            title_duplicated=bool(p["title"]) and titles[p["title"].strip().lower()] > 1,
            has_meta_description=bool(p["meta_description"]),
            meta_duplicated=bool(p["meta_description"]) and metas[p["meta_description"].strip().lower()] > 1,
            h1_count=h1_counts(headings)[0],
            h1_empty_count=h1_counts(headings)[1],
            canonical_self=None if not canonical else run.config.scope.normalize(canonical) == url,
            has_structured_data=bool(jsonld or micro or rdfa),
            structured_data_types=", ".join(jsonld),
            microdata_types="" if micro is None else ", ".join(micro),
            rdfa_types="" if rdfa is None else ", ".join(rdfa),
            og_properties="" if og is None else ", ".join(og),
            hreflang="" if hreflang is None else ", ".join(hreflang),
            robots_meta=p["robots_meta"] or "",
            js_dependent=None if raw is None or not rendered else raw < rendered / 2,
            raw_text_share=None if raw is None or not rendered else round(raw / rendered, 3),
            redirect_hops=max(0, len(chain) - 1),
            carries_lead_tags=url in tagged,
            leads_allocated=math.floor(leads.get(url, 0.0) + 1e-9),
            carries_leads=url in leads,
        ))
    return out


def write_csv(rows: list[PageHealth], path: Path) -> None:
    names = [f.name for f in fields(PageHealth)]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=names)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in asdict(r).items()})


def health_annotations(rows: list[PageHealth]) -> dict[str, dict]:
    return {r.url: {
        "has_structured_data": r.has_structured_data,
        "js_dependent": bool(r.js_dependent),
        "clicks_from_home_body_links": -1 if r.clicks_from_home_body_links is None else r.clicks_from_home_body_links,
        "clicks_from_home_all_links": -1 if r.clicks_from_home_all_links is None else r.clicks_from_home_all_links,
    } for r in rows}


# --------------------------------------------------------------------------- per host

# A fetch returns (HTTP status or None, body) or (status, body, error text).
Fetch = Callable[[str], tuple]

READ, UNREADABLE, ABSENT = "read", "unreadable", "absent"


def _get(fetch: Fetch, url: str) -> tuple[int | None, bytes, str]:
    r = fetch(url)
    status, body = r[0], r[1] or b""
    error = r[2] if len(r) > 2 and r[2] else ""
    return status, body, error


def _state(status: int | None, body: bytes, error: str) -> tuple[str, str]:
    """read / unreadable / absent, with the reason when not read. Only a clean
    404 or 410 means "absent"; anything else that isn't a usable 200 is
    "unreadable", so coverage can't be computed from it."""
    if status == 200 and body.strip():
        return READ, ""
    if status in (404, 410):
        return ABSENT, f"HTTP {status}"
    if status == 200:
        return UNREADABLE, "HTTP 200 but empty"
    if status is None:
        return UNREADABLE, error or "no response"
    return UNREADABLE, f"HTTP {status}"


def _text(body: bytes) -> str:
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except OSError:
            return ""
    return body.decode("utf-8", errors="replace")


def robots_ai_rules(robots_txt: str, base: str, ai_crawlers: list[str]) -> dict[str, dict]:
    """For each AI crawler: whether robots.txt names it, and whether it may fetch the home page."""
    named = {m.lower() for m in re.findall(r"(?im)^\s*user-agent\s*:\s*(\S+)", robots_txt)}
    rp = RobotFileParser()
    rp.parse(robots_txt.splitlines())
    out = {}
    for bot in ai_crawlers:
        out[bot] = {
            "named": bot.lower() in named,
            "allowed_home": rp.can_fetch(bot, base.rstrip("/") + "/"),
        }
    return out


def _sitemap_urls(fetch: Fetch, start: list[str], errors: list[str]) -> tuple[set[str], int, list[str]]:
    """URLs listed by the sitemaps (following indexes). Returns the URLs, the
    number of sitemaps fetched, and the state of each one."""
    seen, urls, queue, states = set(), set(), deque(start), []
    while queue and len(seen) < MAX_SITEMAPS and len(urls) < MAX_SITEMAP_URLS:
        sm = queue.popleft()
        if sm in seen:
            continue
        seen.add(sm)
        status, body, error = _get(fetch, sm)
        state, detail = _state(status, body, error)
        if state != READ:
            states.append(state)
            errors.append(f"{sm}: {detail}")
            continue
        try:
            root = ElementTree.fromstring(_text(body).encode())
        except ElementTree.ParseError:
            head = _text(body)[:200].lower()
            states.append(UNREADABLE)
            errors.append(f"{sm}: not XML" + (" (an HTML page, maybe a bot check)" if "<html" in head else ""))
            continue
        states.append(READ)
        tag = root.tag.rsplit("}", 1)[-1]
        locs = [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text]
        if tag == "sitemapindex":
            queue.extend(locs)
        else:
            urls.update(locs)
    return urls, len(seen), states


def site_bases(crawled_urls: list[str], allowed_domains: list[str]) -> list[str]:
    """``scheme://host[:port]`` of every allowed host with a crawled page."""
    bases = set()
    for u in crawled_urls:
        parts = urlsplit(u)
        if parts.hostname in allowed_domains:
            bases.add(f"{parts.scheme}://{parts.netloc}")
    return sorted(bases)


def collect_site_signals(bases: list[str], fetch: Fetch, crawled_urls: list[str], normalize,
                         ai_crawlers: list[str] | None = None, in_scope=None) -> dict[str, dict]:
    """robots.txt AI rules, llms.txt and sitemap coverage per site (``scheme://host[:port]``)."""
    ai_crawlers = ai_crawlers or AI_CRAWLERS
    out = {}
    for base in sorted(bases):
        host = urlsplit(base).netloc
        errors: list[str] = []
        status, body, error = _get(fetch, base + "/robots.txt")
        robots_state, robots_detail = _state(status, body, error)
        robots = _text(body) if robots_state == READ else ""
        sitemaps = re.findall(r"(?im)^\s*sitemap\s*:\s*(\S+)", robots) or [base + "/sitemap.xml"]
        llms_status, llms_body, llms_error = _get(fetch, base + "/llms.txt")
        llms_state, llms_detail = _state(llms_status, llms_body, llms_error)
        if llms_state == READ and llms_body.lstrip()[:1] == b"<":  # an HTML page served at /llms.txt
            llms_state, llms_detail = ABSENT, "an HTML page, not llms.txt"
        sm_urls, sm_count, sm_states = _sitemap_urls(fetch, sitemaps, errors)
        if READ in sm_states:
            # some listed sitemaps failed or were missing: read, but marked partial
            sitemap_state, sitemap_detail = READ, ("partly: " + errors[0]) if any(x != READ for x in sm_states) else ""
        elif sm_states and all(x == ABSENT for x in sm_states):
            sitemap_state, sitemap_detail = ABSENT, errors[0] if errors else ""
        else:
            sitemap_state, sitemap_detail = UNREADABLE, errors[0] if errors else "no sitemap could be read"
        sm_norm = {normalize(u) for u in sm_urls} - {None}
        on_host = [u for u in crawled_urls if urlsplit(u).netloc == host]
        out[host] = {
            "robots_state": robots_state,
            "robots_detail": robots_detail,
            "robots_txt": robots_state == READ,
            "robots_status": status,
            "ai_crawlers": robots_ai_rules(robots, base, ai_crawlers) if robots_state == READ else {},
            "llms_state": llms_state,
            "llms_detail": llms_detail,
            "llms_txt": llms_state == READ,
            "sitemaps_declared": sitemaps,
            "sitemaps_read": sm_count,
            "sitemap_state": sitemap_state,
            "sitemap_detail": sitemap_detail,
            "sitemap_urls": len(sm_urls) if sitemap_state == READ else None,
            "crawled_pages": len(on_host),
            # never a coverage figure from a sitemap that couldn't be read
            "crawled_pages_in_sitemap": sum(1 for u in on_host if u in sm_norm) if sitemap_state == READ else None,
            # in-scope URLs the sitemap lists that the link crawl never reached
            "sitemap_not_crawled": len(missed := sorted(
                u for u in sm_norm if urlsplit(u).netloc == host and u not in set(on_host)
                and (in_scope is None or in_scope(u)))) if sitemap_state == READ else None,
            "sitemap_not_crawled_sample": missed[:20] if sitemap_state == READ else [],
            "errors": errors[:10],
        }
    return out


def urllib_fetch(user_agent: str = "pathcrawl", timeout: float = 20) -> Fetch:
    import urllib.error
    import urllib.request

    def fetch(url: str) -> tuple[int | None, bytes, str]:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(20_000_000), ""
        except urllib.error.HTTPError as e:
            return e.code, b"", ""
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            return None, b"", f"{type(e).__name__}: {e}"[:200]

    return fetch


def playwright_fetch(request, timeout_ms: int = 20000) -> Fetch:
    """Fetch with the crawler's browser request context (same cookies and headers)."""

    def fetch(url: str) -> tuple[int | None, bytes, str]:
        try:
            r = request.get(url, timeout=timeout_ms)
            return r.status, r.body() if r.ok else b"", ""
        except Exception as e:
            return None, b"", (str(e).splitlines() or [type(e).__name__])[0][:200]

    return fetch


# --------------------------------------------------------------------------- report


def site_state(h: dict, kind: str) -> str:
    """read / unreadable / absent for robots, sitemap or llms (older runs: inferred)."""
    if f"{kind}_state" in h:
        return h[f"{kind}_state"]
    if kind == "robots":
        return READ if h.get("robots_txt") else ABSENT if h.get("robots_status") in (404, 410) else UNREADABLE
    if kind == "llms":
        return READ if h.get("llms_txt") else ABSENT
    return READ if h.get("sitemaps_read") and h.get("sitemap_urls") else UNREADABLE


def robots_cell(h: dict) -> str:
    state = site_state(h, "robots")
    if state == READ:
        return "read"
    if state == ABSENT:
        return "none (404)"
    return f"could not be read ({h.get('robots_detail') or 'HTTP ' + str(h.get('robots_status'))})"


def ai_cell(h: dict) -> str:
    if site_state(h, "robots") != READ:
        return "unknown"
    bots = h.get("ai_crawlers", {})
    named = [b for b, v in bots.items() if v["named"]]
    blocked = [b for b, v in bots.items() if not v["allowed_home"]]
    return f"{len(named)} / {len(blocked)}" + (f" (blocked: {', '.join(blocked[:6])})" if blocked else "")


def llms_cell(h: dict) -> str:
    state = site_state(h, "llms")
    return {"read": "yes", "absent": "no"}.get(state, f"could not be read ({h.get('llms_detail', '')})")


def sitemap_cell(h: dict) -> str:
    state = site_state(h, "sitemap")
    if state == READ:
        return f"{h['sitemap_urls']} URLs" + (" (partly read)" if h.get("sitemap_detail") else "")
    if state == ABSENT:
        return "none found"
    return f"could not be read ({h.get('sitemap_detail') or 'error'})"


def coverage_cell(h: dict) -> str:
    if site_state(h, "sitemap") != READ or h.get("crawled_pages_in_sitemap") is None:
        return "unknown (sitemap could not be read)" if site_state(h, "sitemap") == UNREADABLE else "-"
    return _pct(h["crawled_pages_in_sitemap"], h["crawled_pages"])


def _pct(n: int, d: int) -> str:
    return f"{n} ({round(100 * n / d)}%)" if d else "0"


def _summary(rows: list[PageHealth]) -> dict:
    n = len(rows)
    all_d = [r.clicks_from_home_all_links for r in rows if r.clicks_from_home_all_links is not None]
    body_d = [r.clicks_from_home_body_links for r in rows if r.clicks_from_home_body_links is not None]
    return {
        "pages": n,
        "dead": sum(r.is_dead for r in rows),
        "no_structured_data": sum(not r.has_structured_data for r in rows),
        "js_dependent": sum(bool(r.js_dependent) for r in rows),
        "missing_title": sum(not r.has_title for r in rows),
        "title_duplicated": sum(r.title_duplicated for r in rows),
        "missing_meta_description": sum(not r.has_meta_description for r in rows),
        "meta_duplicated": sum(r.meta_duplicated for r in rows),
        "h1_one": sum(r.h1_count == 1 for r in rows),
        "h1_missing": sum(r.h1_count == 0 for r in rows),
        "h1_multiple": sum(r.h1_count > 1 for r in rows),
        "h1_empty": sum(r.h1_empty_count > 0 for r in rows),
        "canonical_missing": sum(r.canonical_self is None for r in rows),
        "canonical_elsewhere": sum(r.canonical_self is False for r in rows),
        "redirected": sum(r.redirect_hops > 0 for r in rows),
        "with_microdata": sum(bool(r.microdata_types) for r in rows),
        "with_rdfa": sum(bool(r.rdfa_types) for r in rows),
        "with_open_graph": sum(bool(r.og_properties) for r in rows),
        "with_hreflang": sum(bool(r.hreflang) for r in rows),
        "noindex": sum("noindex" in r.robots_meta for r in rows),
        "unreachable_from_home_all": n - len(all_d),
        "unreachable_from_home_body": n - len(body_d),
        "median_clicks_from_home_all": median(all_d) if all_d else None,
        "median_clicks_from_home_body": median(body_d) if body_d else None,
    }


def _fmt(v) -> str:
    if v is None:
        return "-"
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def health_section(rows: list[PageHealth], home: str | None, site: dict, short,
                   extra_recorded: bool) -> tuple[list[str], dict]:
    s = _summary(rows)
    s["extra_signals_recorded"] = extra_recorded
    n = s["pages"]
    L = ["## Website health", ""]
    L.append(f"{n} pages loaded. Click depth is counted from {short(home) if home else '(no home page set)'} over "
             "every link and over body links only. Duplicate means the same title or description on more than one "
             "page. JavaScript-dependent means the HTML holds under half the text the browser shows. Full per-page "
             "list: the site_health CSV.")
    L.append("")
    L.append("| signal | pages |")
    L.append("|---|---|")
    rows_md = [
        ("dead (404, 410, soft 404)", s["dead"]),
        ("no structured data (JSON-LD, Microdata or RDFa)", s["no_structured_data"]),
        ("content depends on JavaScript", s["js_dependent"]),
        ("missing title", s["missing_title"]),
        ("duplicated title", s["title_duplicated"]),
        ("missing meta description", s["missing_meta_description"]),
        ("duplicated meta description", s["meta_duplicated"]),
        ("exactly one H1 with text (passes)", s["h1_one"]),
        ("no H1 with text", s["h1_missing"]),
        ("more than one H1 with text", s["h1_multiple"]),
        ("an empty H1 (alongside or instead of a real one)", s["h1_empty"]),
        ("no canonical URL", s["canonical_missing"]),
        ("canonical points elsewhere", s["canonical_elsewhere"]),
        ("reached through a redirect", s["redirected"]),
        ("not reachable from home (any link)", s["unreachable_from_home_all"]),
        ("not reachable from home (body links)", s["unreachable_from_home_body"]),
    ]
    if s["extra_signals_recorded"]:
        rows_md += [("Microdata", s["with_microdata"]), ("RDFa", s["with_rdfa"]),
                    ("Open Graph tags", s["with_open_graph"]), ("hreflang alternates", s["with_hreflang"]),
                    ("robots meta noindex", s["noindex"])]
    for label, v in rows_md:
        L.append(f"| {label} | {_pct(v, n)} |")
    L.append(f"| median clicks from home (any link / body links) | {_fmt(s['median_clicks_from_home_all'])} / "
             f"{_fmt(s['median_clicks_from_home_body'])} |")
    L.append("")
    if not s["extra_signals_recorded"]:
        L.append("Microdata, RDFa, Open Graph, hreflang and the robots meta tag were not recorded for this run "
                 "(crawled before they were added); a new crawl records them.")
        L.append("")

    lead_rows = [r for r in rows if r.carries_leads]
    groups = [("section", "By section"), ("page_type", "By page type")]
    for key, title in groups:
        L.append(f"### {title}")
        L.append("")
        L.append(f"| {key.replace('_', ' ')} | pages | dead | no structured data | JS-dependent | median clicks (body) |")
        L.append("|---|---|---|---|---|---|")
        buckets: dict[str, list[PageHealth]] = defaultdict(list)
        for r in rows:
            buckets[getattr(r, key) or "(none)"].append(r)
        items = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        shown = items[:20]
        if lead_rows:
            shown.append(("pages carrying leads", lead_rows))
        for name, rs in shown:
            b = _summary(rs)
            label = f"**{name}**" if name == "pages carrying leads" else name
            L.append(f"| {label} | {len(rs)} | {b['dead']} | {_pct(b['no_structured_data'], len(rs))} | "
                     f"{_pct(b['js_dependent'], len(rs))} | {_fmt(b['median_clicks_from_home_body'])} |")
        if len(items) > 20:
            L.append(f"| … {len(items) - 20} more | | | | | |")
        L.append("")

    if site:
        L.append("### Hosts: robots.txt, llms.txt and sitemaps")
        L.append("")
        L.append("| host | robots.txt | AI crawlers named / blocked from home | llms.txt | sitemap | crawled pages in sitemap |")
        L.append("|---|---|---|---|---|---|")
        for host, h in sorted(site.items()):
            L.append(f"| {host} | {robots_cell(h)} | {ai_cell(h)} | {llms_cell(h)} | {sitemap_cell(h)} | {coverage_cell(h)} |")
        L.append("")
        unreadable = [h for h, v in site.items() if site_state(v, "sitemap") == UNREADABLE
                      or site_state(v, "robots") == UNREADABLE]
        if unreadable:
            L.append("Unreadable means the file exists or should, but the request failed (an HTTP error, a block or "
                     "no response); what it would say is unknown. It is not the same as \"not there\".")
            L.append("")
    else:
        L.append("robots.txt, llms.txt and sitemap coverage have not been checked for this run: "
                 "run `pathcrawl site-signals --run <run dir>`.")
        L.append("")
    return L, s
