"""Entity coverage, bridge-link suggestions, the knowledge-graph export, and
site-side recommendations.

Everything is computed from the link graph and the ``page_entities`` table:

- **Coverage** per entity: pages tagged, how many of them link straight to the
  win, the median clicks to the win, and the share within 2 clicks, in both
  modes (all links, content links only). Entities with at least
  ``flag_min_pages`` pages are flagged when none of their pages can reach the
  win with content links ("no path"), or when most can't ("mostly no path").
- **Bridge links**: for every tagged page more than one content click from the
  win (or with no path at all), the pages about the same entities that link to
  the win directly, ranked by shared entity score (the sum, over shared
  entities, of the lower of the two pages' scores). Adding a content link to a
  bridge page puts the page two clicks from the win. Pages it already links to
  are left out.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from statistics import median

import networkx as nx

from pathcrawl.graph import ALL_LINKS, CONTENT_ONLY, MODES, distances_to_win, edge_counts, mode_view

ENTITY_COLORS = {
    "Industry": (148, 103, 189),  # purple
    "Segment": (214, 39, 40),     # red
    "Service": (23, 190, 207),    # teal
    "Topic": (188, 189, 34),      # olive
    "Customer": (227, 119, 194),  # pink
}
OTHER_ENTITY_COLOR = (127, 127, 127)


def tags_by_page(rows) -> dict[str, dict[tuple[str, str], float]]:
    out: dict[str, dict[tuple[str, str], float]] = defaultdict(dict)
    for r in rows:
        out[r["url"]][(r["entity_type"], r["entity"])] = r["score"]
    return out


def mode_distances(g: nx.DiGraph) -> dict[str, dict[str, int]]:
    return {mode: distances_to_win(mode_view(g, mode)) for mode in MODES}


# --------------------------------------------------------------------------- coverage


@dataclass
class EntityCoverage:
    entity_type: str
    entity: str
    pages_tagged: int
    pages_linking_win_content: int
    pages_linking_win_all: int
    median_clicks_content: float | None
    median_clicks_all: float | None
    pct_within_2_content: float
    pct_within_2_all: float
    no_path_content: int
    flag: str  # "", "no path" or "mostly no path"


def _median(values):
    if not values:
        return None
    m = median(values)
    return int(m) if float(m).is_integer() else m


def entity_coverage(g: nx.DiGraph, rows, flag_min_pages: int = 10,
                    dist: dict[str, dict[str, int]] | None = None) -> list[EntityCoverage]:
    """One row per entity, most-tagged first."""
    dist = dist or mode_distances(g)
    pages: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in rows:
        if r["url"] in g and not g.nodes[r["url"]]["win"]:
            pages[(r["entity_type"], r["entity"])].add(r["url"])
    out = []
    for (etype, name), urls in pages.items():
        n = len(urls)
        d = {m: [dist[m][u] for u in urls if u in dist[m]] for m in MODES}
        no_path = n - len(d[CONTENT_ONLY])
        flag = ""
        if n >= flag_min_pages:
            if no_path == n:
                flag = "no path"
            elif no_path * 2 > n:
                flag = "mostly no path"
        out.append(EntityCoverage(
            entity_type=etype,
            entity=name,
            pages_tagged=n,
            pages_linking_win_content=sum(1 for x in d[CONTENT_ONLY] if x == 1),
            pages_linking_win_all=sum(1 for x in d[ALL_LINKS] if x == 1),
            median_clicks_content=_median(d[CONTENT_ONLY]),
            median_clicks_all=_median(d[ALL_LINKS]),
            pct_within_2_content=round(100 * sum(1 for x in d[CONTENT_ONLY] if x <= 2) / n, 1),
            pct_within_2_all=round(100 * sum(1 for x in d[ALL_LINKS] if x <= 2) / n, 1),
            no_path_content=no_path,
            flag=flag,
        ))
    return sorted(out, key=lambda c: (-c.pages_tagged, c.entity_type, c.entity))


# --------------------------------------------------------------------------- bridge links


@dataclass
class BridgeLink:
    page: str
    page_clicks_to_win_content: int | None
    rank: int
    bridge_page: str
    shared_score: float
    shared_entities: str


def bridge_links(g: nx.DiGraph, rows, per_page: int = 3,
                 dist: dict[str, dict[str, int]] | None = None) -> list[BridgeLink]:
    dist = (dist or mode_distances(g))[CONTENT_ONLY]
    h = mode_view(g, CONTENT_ONLY)
    tags = tags_by_page(rows)
    bridges = sorted(u for u in tags if u in g and dist.get(u) == 1)
    out = []
    for page in sorted(tags):
        if page not in g or not g.nodes[page]["explored"] or g.nodes[page]["win"]:
            continue
        d = dist.get(page)
        if d is not None and d <= 1:
            continue
        scored = []
        for b in bridges:
            if b == page or h.has_edge(page, b):
                continue
            # strongest shared entity first
            shared = sorted(set(tags[page]) & set(tags[b]), key=lambda k: (-min(tags[page][k], tags[b][k]), k))
            if not shared:
                continue
            score = round(sum(min(tags[page][k], tags[b][k]) for k in shared), 2)
            scored.append((-score, b, shared))
        scored.sort()
        for rank, (neg, b, shared) in enumerate(scored[:per_page], 1):
            out.append(BridgeLink(page, d, rank, b, -neg, "; ".join(f"{t}: {e}" for t, e in shared)))
    return out


# --------------------------------------------------------------------------- files


def write_dataclass_csv(rows, cls, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[x.name for x in fields(cls)])
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in asdict(r).items()})


def write_page_entities_csv(rows, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["url", "entity_type", "entity", "score", "evidence"])
        for r in rows:
            w.writerow([r["url"], r["entity_type"], r["entity"], r["score"], r["evidence"]])


def knowledge_graph(g: nx.DiGraph, rows, entries, categories=(), seed: int = 42) -> nx.DiGraph:
    """Pages and entities in one graph, laid out for Gephi.

    Nodes: every loaded page and win (``node_type`` page) and every entity that
    tags at least one page (``node_type`` entity, id ``entity:<Type>:<name>``).
    Edges: ``mention`` (page -> entity, weight = score) and ``link`` (page ->
    page, content links only, so menus don't turn it into a hairball).
    ``clicks_to_win`` is the content-links distance; for an entity it is the
    median over its pages, rounded; -1 means no path.
    """
    from pathcrawl.report import ROLE_COLORS

    by_url = {c.url: c for c in categories}
    dist = distances_to_win(mode_view(g, CONTENT_ONLY))
    entry_urls = {e.url for e in entries}
    kg = nx.DiGraph()
    pages = sorted(n for n, d in g.nodes(data=True) if d["explored"] or d["win"])
    for n in pages:
        d = g.nodes[n]
        role = "entry" if n in entry_urls else "win" if d["win"] else "crawled"
        c = by_url.get(n)
        kg.add_node(n, node_type="page", role=role, entity_type="", title=(c.title or "") if c else "",
                    section=c.section if c else "", page_type=c.page_type if c else "",
                    clicks_to_win=dist.get(n, -1), pages_tagged=0)
    page_set = set(pages)
    for u, v, d in g.edges(data=True):
        if u in page_set and v in page_set and not g.nodes[u]["win"] and edge_counts(d, CONTENT_ONLY, include_operator=False):
            kg.add_edge(u, v, edge_type="link", weight=1.0, evidence="", region=",".join(sorted(d["regions"])))
    entity_pages: dict[str, list[str]] = defaultdict(list)
    max_score = max((r["score"] for r in rows), default=1) or 1
    for r in rows:
        if r["url"] not in page_set:
            continue
        eid = f"entity:{r['entity_type']}:{r['entity']}"
        if eid not in kg:
            kg.add_node(eid, node_type="entity", role="entity", entity_type=r["entity_type"], title=r["entity"],
                        section="", page_type="", clicks_to_win=-1, pages_tagged=0)
        entity_pages[eid].append(r["url"])
        kg.add_edge(r["url"], eid, edge_type="mention", weight=round(r["score"] / max_score, 3),
                    evidence=r["evidence"], region="")
    for eid, urls in entity_pages.items():
        clicks = [dist[u] for u in urls if u in dist]
        kg.nodes[eid]["pages_tagged"] = len(urls)
        kg.nodes[eid]["clicks_to_win"] = int(round(median(clicks))) if clicks else -1

    layout = nx.spring_layout(kg.to_undirected(as_view=True), weight="weight", seed=seed,
                              iterations=100 if len(kg) <= 2000 else 50) if len(kg) else {}
    max_tagged = max((len(v) for v in entity_pages.values()), default=1) or 1
    indeg = {n: sum(1 for _, _, d in kg.in_edges(n, data=True) if d["edge_type"] == "link") for n in kg}
    max_in = max(indeg.values(), default=1) or 1
    for n, d in kg.nodes(data=True):
        if d["node_type"] == "entity":
            rgb = ENTITY_COLORS.get(d["entity_type"], OTHER_ENTITY_COLOR)
            size = 8 + 42 * math.sqrt(d["pages_tagged"] / max_tagged)
            label = d["title"]
        else:
            rgb = ROLE_COLORS[d["role"]]
            size = 3 + 17 * math.sqrt(indeg[n] / max_in)
            label = (d["title"] or n) if d["role"] in ("entry", "win") else ""
        x, y = (float(v) for v in layout[n])
        d["label"] = label
        d["viz"] = {
            "color": {"r": rgb[0], "g": rgb[1], "b": rgb[2], "a": 1.0},
            "size": round(size, 2),
            "position": {"x": round(x * 1000, 2), "y": round(y * 1000, 2), "z": 0.0},
        }
    return kg


# --------------------------------------------------------------------------- site-side recommendations

ARTICLE_TYPES = {"Article", "NewsArticle", "BlogPosting", "TechArticle", "Report", "ScholarlyArticle"}


def site_findings(store, categories, win_urls: list[str], near_miss_urls: list[str], rows) -> dict:
    """The numbers behind the site-side recommendations."""
    loaded = [p for p in store.pages() if p["status"] == "ok"]
    types_by_url = {p["url"]: set(json.loads(p["jsonld_types"]) if p["jsonld_types"] else []) for p in loaded}
    type_counts: dict[str, int] = defaultdict(int)
    for ts in types_by_url.values():
        for t in ts:
            type_counts[t] += 1
    content_urls = [c.url for c in categories if c.page_type == "content" and c.url in types_by_url]
    tagged_evidence: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        tagged_evidence[r["url"]].add(r["evidence"])
    return {
        "pages_loaded": len(loaded),
        "pages_with_jsonld": sum(1 for ts in types_by_url.values() if ts),
        "jsonld_types": dict(sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "content_pages": len(content_urls),
        "content_pages_with_article": sum(1 for u in content_urls if types_by_url[u] & ARTICLE_TYPES),
        "pages_with_organization": type_counts.get("Organization", 0),
        "pages_with_breadcrumbs": type_counts.get("BreadcrumbList", 0),
        "js_dependent_pages": sum(1 for p in loaded if p["js_dependent"]),
        "conversion_urls": sorted(set(win_urls) | set(near_miss_urls)),
        "pages_tagged_from_body_only": sum(1 for ev in tagged_evidence.values() if ev == {"body"}),
        "pages_tagged": len(tagged_evidence),
    }


def recommendations_markdown(f: dict, win_name: str) -> list[str]:
    n = f["pages_loaded"] or 1

    def pct(k):
        return f"{round(100 * k / n)}%"

    types = ", ".join(f"{t} ({c})" for t, c in list(f["jsonld_types"].items())[:8]) or "none"
    if f["content_pages"]:
        article = (f"{f['content_pages'] - f['content_pages_with_article']} of {f['content_pages']} content pages have "
                   "no Article markup. ")
    else:
        article = ""
    L = [
        "## Site-side recommendations",
        "",
        "Changes to the site itself that would make its content machine-readable, so journeys and ads can be "
        "driven by typed entities instead of inferred ones. Numbers are from this crawl.",
        "",
        f"1. **Add schema.org JSON-LD.** {f['pages_with_jsonld']} of {f['pages_loaded']} pages "
        f"({pct(f['pages_with_jsonld'])}) have any structured data; types found: {types}. "
        f"{article}Add `Article` (headline, datePublished, author, about) to content pages, `Organization` "
        f"site-wide (on {f['pages_with_organization']} pages now), `Service` to service pages, and "
        f"`BreadcrumbList` everywhere (on {f['pages_with_breadcrumbs']} pages now).",
        f"2. **Render body copy on the server.** On {f['js_dependent_pages']} pages ({pct(f['js_dependent_pages'])}) "
        "less than half the text is in the HTML before JavaScript runs, so crawlers and AI agents that don't run "
        "scripts see a near-empty page.",
    ]
    urls = f["conversion_urls"]
    if len(urls) > 1:
        L.append(f"3. **Consolidate the {len(urls)} conversion URLs into one {win_name}.** Redirect the variants to a "
                 "single target so every journey, link and ad ends at the same page: " + ", ".join(urls) + ".")
    else:
        L.append(f"3. **Keep one conversion target.** Every journey here ends at a single {win_name} URL; keep new "
                 "campaign variants as tracking parameters rather than new pages.")
    L.append(f"4. **Add page metadata for industry, journey stage and persona** (for example `about` and `audience` in "
             f"JSON-LD, or meta tags). {f['pages_tagged_from_body_only']} of {f['pages_tagged']} tagged pages were "
             "tagged from body text alone, which is inference; declared metadata would replace it.")
    L.append("")
    return L
