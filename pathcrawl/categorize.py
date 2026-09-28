"""Categorize every crawled page.

Each page gets:
- **section**: the first path segment after the locale prefix
  (``www.ups.com/us/en/shipping/...`` -> ``shipping``), per host.
- **page type**: a deterministic guess from the URL and structured data
  (see ``PAGE_TYPE_RULES``). Win pages are always ``win``.
- **reach**: for each mode, where the page stands relative to the win:
  ``win``, ``reaches win`` (with the click count), ``dead end``, ``trap loop``
  (a dead end inside a loop), ``unknown`` (only unexplored pages could lead on)
  or ``not reached`` (the entry links never lead here in this mode).
- **content signals**: JavaScript-only content, structured data, missing or
  multiple H1s, missing meta description, load time, and a win URL whose form
  did not render.

No LLM is involved; the same crawl always gets the same categories.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit

import networkx as nx

from pathcrawl.graph import MODES, Analysis, distances_to_win, mode_view

# (type, URL path pattern). First match wins. Patterns are generic web
# conventions, not client-specific; they only look at the path.
PAGE_TYPE_RULES: list[tuple[str, str]] = [
    ("account", r"login|log-in|signin|sign-in|register|my-?account|profile"),
    ("support", r"support|help|faq|contact|customer-service|claims?"),
    ("tool", r"track|calculat|quote|rates?|locat|schedule|estimat|tool"),
    ("content", r"blog|insight|stor(y|ies)|news|article|resource|library|case-stud|white-?paper|guide|podcast|webinar|report"),
    ("corporate", r"about|careers?|investor|sustainab|press|leadership|legal|privacy|terms"),
    ("product/service", r"shipping|service|solution|product|business|industr|logistic|freight|supply-chain|pricing"),
]
ARTICLE_TYPES = {"Article", "BlogPosting", "NewsArticle", "Report", "TechArticle"}
MODE_LABEL = {"all_links": "all links", "content_only": "content only"}


@dataclass
class PageCategory:
    url: str
    host: str
    section: str
    page_type: str
    title: str | None
    status: str
    depth: int | None
    http_status: int | None
    load_ms: int | None
    reach_all_links: str
    clicks_to_win_all_links: int | None
    reach_content_only: str
    clicks_to_win_content_only: int | None
    js_dependent: bool | None
    structured_data: str  # comma-separated JSON-LD types, "" if none
    h1_count: int
    missing_meta_description: bool
    win_form_missing: bool
    operator_marked_win: bool


def _locale_prefix_len(path: str, locale_include: list[str]) -> int:
    lowered = path.lower()
    for loc in locale_include:
        i = lowered.find(loc.lower())
        if i == 0:
            return len(loc)
    return 0


def section_of(url: str, locale_include: list[str] | None = None) -> str:
    """First path segment after any locale prefix; ``(home)`` for the root."""
    path = urlsplit(url).path or "/"
    path = path[_locale_prefix_len(path, locale_include or []):]
    segment = next((s for s in path.split("/") if s), "")
    if not segment or segment.lower() in ("home", "home.page", "index.html"):
        return "(home)"
    return re.sub(r"\.(html?|page|aspx?|php)$", "", segment.lower())


def page_type_of(url: str, jsonld_types: list[str], is_win: bool, section: str) -> str:
    if is_win:
        return "win"
    path = urlsplit(url).path.lower()
    for page_type, pattern in PAGE_TYPE_RULES:
        if re.search(pattern, path):
            return page_type
    if ARTICLE_TYPES & set(jsonld_types):
        return "content"
    if section == "(home)":
        return "home"
    return "other"


def _reach_labels(g: nx.DiGraph, analysis: Analysis, mode: str) -> tuple[dict[str, str], dict[str, int]]:
    h = mode_view(g, mode)
    dist = distances_to_win(h)
    dz = analysis.modes[mode].dead_zones
    dead, unknown = set(dz.dead_ends), set(dz.unknown) | set(dz.unexplored_reachable)
    in_trap = {u for loop in dz.trap_loops for u in loop}
    starts = [e.url for e in analysis.modes[mode].entries if e.in_graph]
    reached = set(starts)
    for s in starts:
        reached |= nx.descendants(h, s)
    labels = {}
    for n in g:
        if g.nodes[n]["win"]:
            labels[n] = "win"
        elif n not in reached:
            labels[n] = "not reached"
        elif n in in_trap:
            labels[n] = "trap loop"
        elif n in dead:
            labels[n] = "dead end"
        elif n in dist:
            labels[n] = "reaches win"
        elif n in unknown:
            labels[n] = "unknown"
        else:
            labels[n] = "not reached"
    return labels, dist


def categorize(store, g: nx.DiGraph, analysis: Analysis, locale_include: list[str] | None = None) -> list[PageCategory]:
    """One category row per page the crawler saved (offsite redirects excluded)."""
    reach = {mode: _reach_labels(g, analysis, mode) for mode in MODES}
    rows = []
    for page in store.pages():
        url = page["url"]
        if url not in g:
            continue
        jsonld = json.loads(page["jsonld_types"]) if page["jsonld_types"] else []
        headings = json.loads(page["headings"]) if page["headings"] else []
        is_win = bool(page["win"])
        section = section_of(url, locale_include)
        explored = page["status"] in ("ok", "http_error")
        (labels_all, dist_all), (labels_content, dist_content) = reach["all_links"], reach["content_only"]
        rows.append(
            PageCategory(
                url=url,
                host=urlsplit(url).hostname or "",
                section=section,
                page_type=page_type_of(url, jsonld, is_win, section),
                title=page["title"],
                status=page["status"],
                depth=page["depth"],
                http_status=page["http_status"],
                load_ms=page["load_ms"],
                reach_all_links=labels_all[url],
                clicks_to_win_all_links=dist_all.get(url),
                reach_content_only=labels_content[url],
                clicks_to_win_content_only=dist_content.get(url),
                js_dependent=None if page["js_dependent"] is None else bool(page["js_dependent"]),
                structured_data=", ".join(jsonld),
                h1_count=sum(1 for level, _ in headings if level == 1),
                missing_meta_description=explored and not page["meta_description"],
                win_form_missing=is_win and page["form_present"] == 0,
                operator_marked_win=page["win_source"] == "operator",
            )
        )
    return rows


def write_csv(rows: list[PageCategory], path: Path) -> None:
    fields = list(PageCategory.__dataclass_fields__)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def summarize(rows: list[PageCategory]) -> dict:
    """Totals by section and page type, plus content-signal counts."""
    loaded = [r for r in rows if r.status in ("ok", "http_error")]

    def group(key: str) -> list[dict]:
        buckets: dict[str, list[PageCategory]] = defaultdict(list)
        for r in loaded:
            buckets[getattr(r, key)].append(r)
        out = []
        for name, rs in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            content_clicks = [r.clicks_to_win_content_only for r in rs if r.clicks_to_win_content_only is not None]
            out.append({
                key: name,
                "pages": len(rs),
                "reach_win_all_links": sum(r.reach_all_links in ("win", "reaches win") for r in rs),
                "reach_win_content_only": sum(r.reach_content_only in ("win", "reaches win") for r in rs),
                "median_clicks_content_only": median(content_clicks) if content_clicks else None,
                "dead_or_trap_content_only": sum(r.reach_content_only in ("dead end", "trap loop") for r in rs),
                "js_dependent": sum(bool(r.js_dependent) for r in rs),
            })
        return out

    return {
        "pages_categorized": len(rows),
        "pages_loaded": len(loaded),
        "by_section": group("section"),
        "by_page_type": group("page_type"),
        "reach": {
            mode: dict(Counter(getattr(r, f"reach_{mode}") for r in loaded).most_common()) for mode in MODES
        },
        "signals": {
            "js_dependent": sum(bool(r.js_dependent) for r in loaded),
            "no_structured_data": sum(not r.structured_data for r in loaded),
            "missing_h1": sum(r.h1_count == 0 for r in loaded),
            "multiple_h1": sum(r.h1_count > 1 for r in loaded),
            "missing_meta_description": sum(r.missing_meta_description for r in loaded),
            "slow_over_3s": sum((r.load_ms or 0) > 3000 for r in loaded),
            "win_form_missing": sum(r.win_form_missing for r in loaded),
        },
    }
