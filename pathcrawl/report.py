"""Report outputs for a run: report.md, report.json, graph.graphml, paths.mmd,
categories.csv, and graph.gexf / graph_content_only.gexf for Gephi. When the
run has entity tags (``pathcrawl entities extract``): page_entities.csv,
entity_coverage.csv, bridge_links.csv and <client>_knowledge_graph.gexf.

Everything here is formatting. The numbers all come from ``graph.analyze`` and
``categorize.categorize``, so the report can never disagree with the analysis.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import networkx as nx

from pathcrawl.categorize import MODE_LABEL, categorize, summarize, write_csv
from pathcrawl.coverage import (
    BridgeLink,
    EntityCoverage,
    bridge_links,
    entity_coverage,
    knowledge_graph,
    mode_distances,
    recommendations_markdown,
    site_findings,
    write_dataclass_csv,
    write_page_entities_csv,
)
from pathcrawl.graph import ALL_LINKS, CONTENT_ONLY, MODES, Analysis, distances_to_win, edge_counts, mode_view
from pathcrawl.dead import dead_annotations, dead_links, dead_pages, dead_section
from pathcrawl.dead import write_csv as write_dead_csv
from pathcrawl.health import health_annotations, health_section, page_health
from pathcrawl.health import write_csv as write_health_csv
from pathcrawl.leads import lead_annotations, lead_section

DEFINITIONS = {
    "click": "Following one link. A path of N clicks visits N+1 pages.",
    "win page": "A page whose URL matches win.url_patterns (and, if require_form is set, shows the win form), "
    "or a page the operator marked as a win. A URL that matches is a win even if it was never loaded (for example "
    "because robots.txt forbids it): a link to it is enough. The win ends the journey, so its own links are not "
    "followed.",
    "all links": "Every link counts, including the site navigation, header and footer.",
    "content links only": "Links in the nav, header and footer are ignored. This shows whether the content itself "
    "guides people to the win, rather than a global menu link that makes every page look one click away.",
    "shortest path": "The fewest clicks from an entry link to any win page (breadth-first search), and the pages "
    "along the way.",
    "longest simple path": "The longest path from an entry link to a win page that never visits the same page "
    "twice and stays within max_depth clicks. A path ends at the first win it reaches. Unbounded longest paths "
    "are undefined in sites with loops, which is why it is limited this way. If the search ran out of time the "
    "value is a lower bound and is marked with +.",
    "worst-case distance": "For every page reachable from the entry links, its shortest distance to a win; the "
    "worst case is the maximum.",
    "dead end": "A crawled page that provably cannot reach a win page: every page reachable from it was crawled "
    "and none is a win.",
    "trap loop": "Two or more dead-end pages that link to each other in a cycle, so a visitor can click around "
    "without ever reaching the win.",
    "unknown": "No known path to a win, but the page leads to pages the crawl never loaded (page budget, depth "
    "limit, skipped), so it cannot be called a dead end.",
    "dead zone hit": "The first click at which an entry link's journey can land on a dead end.",
    "convergence": "Whether every entry link reaches the win, and the same win page; overlap is the Jaccard "
    "similarity of the pages on their shortest paths.",
    "operator edge": "A jump the operator entered during the crawl ([u]), not a link on the site. Journeys that "
    "need one are ones a real visitor probably could not complete.",
    "entity": "An industry, segment, service, topic or customer from the client's taxonomy file. A page is tagged "
    "when the entity's terms appear in its title, headings or body after boilerplate (text repeated across many "
    "pages, such as menus and cookie banners) is removed.",
    "bridge link": "A suggested content link from a page more than one click from the win to a page about the "
    "same entities that links to the win directly, ranked by shared entity score.",
}


# --------------------------------------------------------------------------- helpers


def _main_host(urls) -> str:
    hosts = Counter(urlsplit(u).hostname for u in urls)
    return hosts.most_common(1)[0][0] if hosts else ""


def make_short(urls):
    main = _main_host(urls)

    def short(url: str | None) -> str:
        if not url:
            return "-"
        parts = urlsplit(url)
        tail = parts.path + (f"?{parts.query}" if parts.query else "")
        return tail if parts.hostname == main else f"{parts.hostname}{tail}"

    return short


def _clicks(n: int | None, exhaustive: bool = True) -> str:
    if n is None:
        return "no path"
    return f"{n}{'' if exhaustive else '+'}"


def _pct(n: int, d: int) -> str:
    return f"{round(100 * n / d)}%" if d else "-"


# --------------------------------------------------------------------------- headline


def headline(analysis: Analysis, win_name: str) -> str:
    content, all_links = analysis.modes[CONTENT_ONLY], analysis.modes[ALL_LINKS]
    n = len(content.entries)

    def reach_sentence(mode_result, label: str) -> str:
        reached = [e for e in mode_result.entries if e.shortest_clicks is not None]
        if not reached:
            return f"{label}, {'the entry link does not' if n == 1 else f'none of the {n} entry links'} reach the {win_name}."
        clicks = sorted(e.shortest_clicks for e in reached)
        span = f"{clicks[0]} click{'s' if clicks[0] != 1 else ''}" if clicks[0] == clicks[-1] else f"{clicks[0]}–{clicks[-1]} clicks"
        if n == 1:
            who = "the entry link reaches"
        elif len(reached) == n:
            who = "both entry links reach" if n == 2 else f"all {n} entry links reach"
        else:
            who = f"{len(reached)} of {n} entry links reach"
        return f"{label}, {who} the {win_name} in {span}."

    dz = content.dead_zones
    parts = [
        reach_sentence(content, "Following content links only"),
        reach_sentence(all_links, "Counting site navigation too"),
        f"{dz.dead_end_count} of {dz.crawled_pages} crawled pages ({dz.dead_end_pct}%) are dead ends when "
        "navigation is ignored" + (f", including {len(dz.trap_loops)} trap loop{'s' if len(dz.trap_loops) != 1 else ''}." if dz.trap_loops else "."),
    ]
    dependent = content.operator_dependent_entries
    if dependent:
        parts.append(f"{len(dependent)} journey(s) only reach the win thanks to operator help: {', '.join(dependent)}.")
    return " ".join(parts)


# --------------------------------------------------------------------------- mermaid


def mermaid_paths(analysis: Analysis, short) -> str:
    """Shortest path per entry link (content links only, falling back to all
    links), with the branch into the nearest dead zone drawn dashed in red."""
    lines = [
        "flowchart LR",
        "  classDef entry fill:#1f4e8c,color:#fff,stroke:#1f4e8c",
        "  classDef win fill:#1b7f3b,color:#fff,stroke:#1b7f3b",
        "  classDef dead fill:#b42318,color:#fff,stroke:#b42318",
    ]
    ids: dict[str, str] = {}

    def node(url: str) -> str:
        if url not in ids:
            ids[url] = f"n{len(ids)}"
            label = short(url).replace('"', "#quot;")
            lines.append(f'  {ids[url]}["{label}"]')
        return ids[url]

    content = {e.label: e for e in analysis.modes[CONTENT_ONLY].entries}
    all_links = {e.label: e for e in analysis.modes[ALL_LINKS].entries}
    classes: dict[str, str] = {}
    edges: set[tuple[str, str, str]] = set()
    for label, e in content.items():
        path, note = e.shortest_path, "content links"
        if not path:
            path, note = all_links[label].shortest_path, "only via navigation"
        start = node(e.url)
        classes.setdefault(start, "entry")
        lines.append(f'  %% entry "{label}": {note if path else "no path to the win"}')
        if path:
            for a, b in zip(path, path[1:]):
                edges.add((node(a), node(b), "-->"))
            classes[node(path[-1])] = "win"
        else:
            nopath = f"x{len(ids)}"
            ids[f"__nopath_{label}"] = nopath
            lines.append(f'  {nopath}(["no path to the win"])')
            edges.add((start, nopath, "-.-"))
        if e.dead_zone_path:
            for a, b in zip(e.dead_zone_path, e.dead_zone_path[1:]):
                if (node(a), node(b), "-->") not in edges:  # don't redraw a hop already on the main path
                    edges.add((node(a), node(b), "-.->"))
            classes[node(e.dead_zone_path[-1])] = "dead"
    for a, b, arrow in sorted(edges):
        lines.append(f"  {a} {arrow} {b}")
    for nid, cls in sorted(classes.items()):
        lines.append(f"  class {nid} {cls}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- graphml


def graphml_graph(g: nx.DiGraph, categories) -> nx.DiGraph:
    """A copy of the graph with only GraphML-friendly attribute types."""
    by_url = {c.url: c for c in categories}
    dist = {mode: distances_to_win(mode_view(g, mode)) for mode in MODES}
    out = nx.DiGraph()
    for n, d in g.nodes(data=True):
        c = by_url.get(n)
        out.add_node(
            n,
            win=bool(d["win"]),
            win_source=d["win_source"] or "",
            explored=bool(d["explored"]),
            title=(c.title or "") if c else "",
            section=c.section if c else "",
            page_type=c.page_type if c else "",
            reach_content_only=c.reach_content_only if c else "",
            clicks_to_win_all_links=dist[ALL_LINKS].get(n, -1),
            clicks_to_win_content_only=dist[CONTENT_ONLY].get(n, -1),
        )
    for u, v, d in g.edges(data=True):
        out.add_edge(
            u, v,
            regions=",".join(sorted(d["regions"])),
            operator=bool(d["operator"]),
            content_link=edge_counts(d, CONTENT_ONLY, include_operator=False),
        )
    return out


# --------------------------------------------------------------------------- win pages


NOT_FETCHED_REASONS = {
    "robots": "blocked by robots.txt",
    "not_fetched": "the crawl stops at the win, so it is not loaded",
    None: "linked, but the crawl never reached it",
}


def win_pages(run) -> list[dict]:
    """Every win page in the graph, whether it was loaded and, if not, why."""
    rows = {r["url"]: r for r in run.store.pages()}
    out = []
    for n, d in sorted(run.graph.nodes(data=True)):
        if not d["win"]:
            continue
        row = rows.get(n)
        status = row["status"] if row else None
        fetched = bool(d["explored"])
        reason = None
        if not fetched:
            reason = NOT_FETCHED_REASONS.get(status) or (row["error"] if row and row["error"] else status)
            if status is None and run.graph.in_degree(n) == 0:
                reason = "listed in win.known_pages; no crawled page links to it"
        out.append({
            "url": n,
            "win_type": d.get("win_type"),
            "win_source": d["win_source"],
            "status": status,
            "fetched": fetched,
            "not_fetched_reason": reason,
            "form_present": None if not row or row["form_present"] is None else bool(row["form_present"]),
            "linked_from": run.graph.in_degree(n),
        })
    return out


def near_misses(run) -> list[dict]:
    """Pages that look like the win (a win keyword in the URL path) but match no
    win pattern: probably a variant the patterns should include."""
    win = run.config.win
    return [
        {"url": n, "linked_from": run.graph.in_degree(n)}
        for n in sorted(run.graph)
        if not run.graph.nodes[n]["win"] and win.near_miss(n)
    ]


# --------------------------------------------------------------------------- gephi

ROLE_COLORS = {
    "entry": (31, 119, 180),     # blue
    "win": (44, 160, 44),        # green
    "crawled": (150, 150, 150),  # grey
    "uncrawled": (255, 160, 60), # orange
    "external": (10, 102, 194),  # LinkedIn-ish blue: posts collected by hand
}
TOP_HUBS = 10


@dataclass
class Annotations:
    """Extra node and edge attributes (lead counts, dead pages, health) carried
    into the GEXF files and report.json. Every node or edge gets each attribute,
    using the default where nothing was recorded, so Gephi sees one type per column."""

    node_defaults: dict[str, object] = field(default_factory=dict)
    nodes: dict[str, dict[str, object]] = field(default_factory=dict)
    edge_defaults: dict[str, object] = field(default_factory=dict)
    edges: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)

    def add_nodes(self, defaults: dict[str, object], values: dict[str, dict[str, object]]) -> None:
        self.node_defaults.update(defaults)
        for n, d in values.items():
            self.nodes.setdefault(n, {}).update(d)

    def add_edges(self, defaults: dict[str, object], values: dict[tuple[str, str], dict[str, object]]) -> None:
        self.edge_defaults.update(defaults)
        for e, d in values.items():
            self.edges.setdefault(e, {}).update(d)

    def node(self, n: str) -> dict[str, object]:
        return {**self.node_defaults, **self.nodes.get(n, {})}

    def edge(self, u: str, v: str) -> dict[str, object]:
        return {**self.edge_defaults, **self.edges.get((u, v), {})}


def gexf_graph(g: nx.DiGraph, entries, categories, content_only: bool = False, seed: int = 42,
               annotations: Annotations | None = None) -> nx.DiGraph:
    """A copy of the graph laid out for Gephi.

    Positions come from a weighted spring layout (content links pull harder
    than nav/header/footer links), node size grows with in-degree, colour shows
    the role (entry, win, crawled, uncrawled), and only entries, wins and the
    top hubs are labelled. With ``content_only`` the nav/header/footer links
    are left out, and so are pages that are then unconnected (entries and wins
    always stay).
    """
    by_url = {c.url: c for c in categories}
    entry_urls = {e.url for e in entries}
    out = nx.DiGraph()
    for u, v, d in g.edges(data=True):
        content = edge_counts(d, CONTENT_ONLY, include_operator=False)
        if content_only and not (content or d["operator"]):
            continue
        out.add_edge(
            u, v,
            region=",".join(sorted(d["regions"])) or "operator",
            content_link=content,
            operator=bool(d["operator"]),
            weight=1.0 if content or d["operator"] else 0.2,
            **(annotations.edge(u, v) if annotations else {}),
        )
    keep = set(out) if content_only else set(g)
    keep |= {n for n in g if n in entry_urls or g.nodes[n]["win"]}
    out.add_nodes_from(sorted(keep))

    def role(n):
        d = g.nodes[n]
        if d.get("external"):
            return "external"
        if n in entry_urls:
            return "entry"
        if d["win"]:
            return "win"
        return "crawled" if d["explored"] else "uncrawled"

    indeg = dict(out.in_degree())
    max_in = max(indeg.values(), default=0) or 1
    hubs = set(sorted((n for n in out if role(n) in ("crawled", "uncrawled")), key=lambda n: (-indeg[n], n))[:TOP_HUBS])
    layout = nx.spring_layout(
        out.to_undirected(as_view=True), weight="weight", seed=seed,
        iterations=100 if len(out) <= 2000 else 50,
    ) if len(out) else {}
    for n in out:
        c, r = by_url.get(n), role(n)
        red, green, blue = ROLE_COLORS[r]
        x, y = (float(v) for v in layout[n])
        out.nodes[n].update(
            label=(c.title if c and c.title else n) if (r in ("entry", "win", "external") or n in hubs) else "",
            role=r,
            in_degree=indeg[n],
            title=(c.title or "") if c else "",
            section=c.section if c else "",
            page_type=c.page_type if c else "",
            status=g.nodes[n].get("status") or "",
            win_type=g.nodes[n].get("win_type") or "",
            external=bool(g.nodes[n].get("external")),
            **(annotations.node(n) if annotations else {}),
            viz={
                "color": {"r": red, "g": green, "b": blue, "a": 1.0},
                "size": round(4 + 36 * (indeg[n] / max_in) ** 0.5, 2),
                "position": {"x": round(x * 1000, 2), "y": round(y * 1000, 2), "z": 0.0},
            },
        )
    return out


# --------------------------------------------------------------------------- markdown


def markdown_report(run, analysis: Analysis, summary: dict, mermaid: str, short,
                    extra_sections: list[str] | None = None) -> str:
    cfg, store = run.config, run.store
    campaign = store.meta("campaign_name") or ""
    win_name = cfg.win.name
    L: list[str] = []
    add = L.append

    add(f"# {cfg.client.name}: how reachable is the {win_name}?")
    add("")
    add(f"**Campaign:** {campaign}  ")
    ad_urls = store.meta("ad_urls", []) or []
    if ad_urls:
        add(f"**Ads ({len(ad_urls)}, not crawled):** " + " · ".join(f"[{i}]({u})" for i, u in enumerate(ad_urls, 1)) + "  ")
    add(f"**Crawl:** {store.meta('status')} · {store.explored_count()} pages loaded · max depth "
        f"{cfg.crawl.max_depth} · page budget {cfg.crawl.max_pages} · started {store.meta('started_at')}")
    add("")
    add(f"> {headline(analysis, win_name)}")
    add("")
    def type_note(w) -> str:
        return f" ({w['win_type']})" if w.get("win_type") and w["win_type"] != win_name else ""

    for w in win_pages(run):
        if w["fetched"]:
            form = {True: "form found", False: "form not rendered", None: "form not checked"}[w["form_present"]]
            add(f"- Win page {w['url']}{type_note(w)}: loaded ({form}).")
        else:
            add(f"- Win page {w['url']}{type_note(w)}: win page not fetched: {w['not_fetched_reason']}. "
                f"It was matched by URL, "
                f"so the link to it ({w['linked_from']} page{'s' if w['linked_from'] != 1 else ''} link here) "
                "is confirmed, but the form itself was not checked.")
    misses = near_misses(run)
    if misses:
        add("")
        add(f"> ⚠ {len(misses)} page{'s look' if len(misses) != 1 else ' looks'} like the win "
            f"(URL contains {' / '.join(repr(k) for k in run.config.win.keywords())}) but "
            f"match{'es' if len(misses) == 1 else ''} no win pattern, so {'they are' if len(misses) != 1 else 'it is'} "
            "not counted as wins. Add a pattern to win.url_patterns if they should be:")
        for m in misses[:20]:
            add(f">  - {m['url']} (linked from {m['linked_from']} page{'s' if m['linked_from'] != 1 else ''})")
        if len(misses) > 20:
            add(f">  - … and {len(misses) - 20} more (see report.json)")
    add("")

    add("## Entry links")
    add("")
    add("| entry link | shortest (content) | shortest (all links) | longest simple (content) | longest simple (all links) | dead zone at click (content) |")
    add("|---|---|---|---|---|---|")
    content_entries = {e.label: e for e in analysis.modes[CONTENT_ONLY].entries}
    for e_all in analysis.modes[ALL_LINKS].entries:
        e_c = content_entries[e_all.label]
        flag = " ⚠ operator" if e_all.label in analysis.modes[CONTENT_ONLY].operator_dependent_entries else ""
        add(f"| {e_all.label}{flag} | {_clicks(e_c.shortest_clicks)} | {_clicks(e_all.shortest_clicks)} | "
            f"{_clicks(e_c.longest_clicks, e_c.longest_exhaustive)} | {_clicks(e_all.longest_clicks, e_all.longest_exhaustive)} | "
            f"{'-' if e_c.dead_zone_click is None else e_c.dead_zone_click} |")
    add("")
    add("### Shortest journeys")
    add("")
    for e_c in analysis.modes[CONTENT_ONLY].entries:
        e_all = next(x for x in analysis.modes[ALL_LINKS].entries if x.label == e_c.label)
        add(f"**{e_c.label}** ({short(e_c.url)})")
        for mode_label, e in (("content links", e_c), ("all links", e_all)):
            path = " → ".join(short(u) for u in e.shortest_path) if e.shortest_path else "no path to the win"
            add(f"- {mode_label}: {path}")
        add("")
    add("```mermaid")
    add(mermaid.rstrip())
    add("```")
    add("")
    add("Blue: entry link. Green: win. Red, dashed: the nearest branch into a dead zone.")
    add("")

    add("## Dead zones")
    add("")
    add("| | all links | content only |")
    add("|---|---|---|")
    dz = {m: analysis.modes[m].dead_zones for m in MODES}
    wc = {m: analysis.modes[m].worst_case for m in MODES}
    add(f"| pages reachable from the entry links | {analysis.modes[ALL_LINKS].reachable_pages} | {analysis.modes[CONTENT_ONLY].reachable_pages} |")
    add(f"| dead ends | {dz[ALL_LINKS].dead_end_count} ({dz[ALL_LINKS].dead_end_pct}%) | {dz[CONTENT_ONLY].dead_end_count} ({dz[CONTENT_ONLY].dead_end_pct}%) |")
    add(f"| trap loops | {len(dz[ALL_LINKS].trap_loops)} | {len(dz[CONTENT_ONLY].trap_loops)} |")
    add(f"| unknown (lead only to uncrawled pages) | {len(dz[ALL_LINKS].unknown)} | {len(dz[CONTENT_ONLY].unknown)} |")
    add(f"| worst-case clicks to win | {_clicks(wc[ALL_LINKS].max_clicks)} | {_clicks(wc[CONTENT_ONLY].max_clicks)} |")
    add("")
    for m in MODES:
        for i, loop in enumerate(dz[m].trap_loops, 1):
            add(f"- Trap loop {i} ({MODE_LABEL[m]}, {len(loop)} pages): " + ", ".join(short(u) for u in loop))
    if any(dz[m].trap_loops for m in MODES):
        add("")
    dead = dz[CONTENT_ONLY].dead_ends
    if dead:
        add(f"Dead ends with content links only ({len(dead)}): " + ", ".join(short(u) for u in dead[:30])
            + (f", … and {len(dead) - 30} more (see categories.csv)" if len(dead) > 30 else ""))
        add("")

    add("### Distance to the win")
    add("")
    add("| clicks to win | pages (all links) | pages (content only) |")
    add("|---|---|---|")
    keys = sorted(set(wc[ALL_LINKS].distribution) | set(wc[CONTENT_ONLY].distribution))
    for k in keys:
        add(f"| {k} | {wc[ALL_LINKS].distribution.get(k, 0)} | {wc[CONTENT_ONLY].distribution.get(k, 0)} |")
    add(f"| no path | {wc[ALL_LINKS].no_path_count} | {wc[CONTENT_ONLY].no_path_count} |")
    add("")

    add("## Convergence")
    add("")
    for m in MODES:
        c = analysis.modes[m].convergence
        wins = ", ".join(short(w) for w in c.wins_reached) or "none"
        add(f"- **{MODE_LABEL[m]}**: {'converge on one win' if c.converged else 'do not converge'} (wins reached: {wins}).")
        for pair in c.pairwise_overlap:
            add(f"  - {pair['a']} vs {pair['b']}: {round(100 * pair['jaccard'])}% of path pages shared")
    add("")

    add("## Operator dependency")
    add("")
    if analysis.operator_edges or analysis.operator_win_pages:
        for u, v in analysis.operator_edges:
            add(f"- Operator jump: {short(u)} → {short(v)}")
        for w in analysis.operator_win_pages:
            add(f"- Operator-marked win: {short(w)}")
        for m in MODES:
            dep = analysis.modes[m].operator_dependent_entries
            if dep:
                add(f"- {MODE_LABEL[m]}: only reachable with operator help: {', '.join(dep)}")
    else:
        add("None: every journey above uses real links on the site.")
    add("")

    add("## Site map: page categories")
    add("")
    add(f"{summary['pages_loaded']} pages loaded, categorized by section, page type and reachability "
        "(full list in `categories.csv`).")
    add("")
    for key, title in (("section", "By section"), ("page_type", "By page type")):
        add(f"### {title}")
        add("")
        add(f"| {key.replace('_', ' ')} | pages | reach win (all links) | reach win (content only) | median clicks (content) | dead or trapped (content) | JS-only content |")
        add("|---|---|---|---|---|---|---|")
        for row in summary[f"by_{key}"]:
            med = row["median_clicks_content_only"]
            med = "-" if med is None else (int(med) if float(med).is_integer() else med)
            add(f"| {row[key]} | {row['pages']} | {_pct(row['reach_win_all_links'], row['pages'])} | "
                f"{_pct(row['reach_win_content_only'], row['pages'])} | {med} | {row['dead_or_trap_content_only']} | {row['js_dependent']} |")
        add("")
    s = summary["signals"]
    n = summary["pages_loaded"]
    add("### Content signals (AI and agent readability)")
    add("")
    add(f"- Content only appears after JavaScript runs: {s['js_dependent']} of {n} pages")
    add(f"- No structured data (JSON-LD): {s['no_structured_data']}")
    add(f"- Missing H1: {s['missing_h1']} · more than one H1: {s['multiple_h1']}")
    add(f"- Missing meta description: {s['missing_meta_description']}")
    add(f"- Slower than 3 s to load: {s['slow_over_3s']}")
    if s["win_form_missing"]:
        add(f"- Win URL where the form did not render: {s['win_form_missing']}")
    add("")

    L.extend(extra_sections or [])

    add("## Metric definitions")
    add("")
    for term, text in DEFINITIONS.items():
        add(f"- **{term}**: {text}")
    add("")
    return "\n".join(L)


# --------------------------------------------------------------------------- health


def health_home(run) -> str | None:
    """The page click depth is counted from: health.home_url, else the first entry link."""
    if run.config.health.home_url:
        return run.store.resolve(run.config.scope.normalize(run.config.health.home_url) or run.config.health.home_url)
    return run.entries[0].url if run.entries else None


# --------------------------------------------------------------------------- external seeds


def external_seed_rows(run) -> list[dict]:
    store, g = run.store, run.graph
    rows = []
    for p in store.db.execute("SELECT url, title, channel, post_date_derived FROM pages WHERE status = 'external' ORDER BY url"):
        links = store.db.execute("SELECT href, url, mc_id, in_scope FROM links WHERE src = ? ORDER BY id", (p["url"],)).fetchall()
        rows.append({
            "url": p["url"], "title": p["title"], "channel": p["channel"], "post_date_derived": p["post_date_derived"],
            "links": [{
                "target": lk["url"], "mc_id": lk["mc_id"], "in_scope": bool(lk["in_scope"]),
                "target_is_win": bool(lk["url"] in g and g.nodes[lk["url"]]["win"]),
                "dead": bool(store.db.execute("SELECT 1 FROM pages WHERE url = ? AND is_dead = 1",
                                              (store.resolve(lk["url"]) if lk["url"] else "",)).fetchone()),
                "target_status": g.nodes[lk["url"]].get("status") if lk["url"] in g else None,
            } for lk in links],
        })
    return rows


def external_seed_section(run, short) -> list[str]:
    meta = run.store.meta("external_seeds") or {}
    rows = external_seed_rows(run)
    L = ["## External entry points", ""]
    L.append(f"{len(rows)} posts collected by hand ({Path(meta.get('file', '')).name}), never crawled. "
             f"{len(meta.get('skipped_seeds', []))} were skipped as already listed (ad URLs or entry links) and "
             f"{meta.get('duplicate_rows', 0)} duplicate rows dropped. {len(meta.get('new_entries', []))} landing "
             f"pages became entry links; {len(meta.get('existing_entries', []))} already were.")
    L.append("")
    L.append("| post | date | lands on | tag |")
    L.append("|---|---|---|---|")
    for r in rows:
        title = (r["title"] or r["url"]).replace("|", "/")[:70]
        if not r["links"]:
            L.append(f"| {title} | {r['post_date_derived'] or '-'} | (no landing page) | - |")
        for lk in r["links"]:
            note = (" (win)" if lk["target_is_win"] else " (dead page)" if lk["dead"]
                    else "" if lk["in_scope"] else " (outside the crawl)")
            L.append(f"| {title} | {r['post_date_derived'] or '-'} | {short(lk['target']) if lk['target'] else '-'}{note} "
                     f"| {lk['mc_id'] or '-'} |")
    L.append("")
    return L


# --------------------------------------------------------------------------- entities


def _fmt(v) -> str:
    return "-" if v is None else str(v)


def entity_sections(run, rows, categories, report: dict, paths: dict, out: Path, short) -> list[str]:
    """Write the entity files, add them to report.json, and return the report.md sections."""
    from pathcrawl.entities import Taxonomy, load_taxonomy

    meta = run.store.meta("entities", {}) or {}
    tax_path = meta.get("taxonomy")
    # the copy saved by the last extraction; it holds the coverage settings
    tax = load_taxonomy(run.dir / "entities.yaml") if (run.dir / "entities.yaml").exists() else Taxonomy()
    g = run.graph
    dist = mode_distances(g)
    cov = entity_coverage(g, rows, tax.coverage.flag_min_pages, dist)
    bridges = bridge_links(g, rows, tax.coverage.bridges_per_page, dist)

    slug = run.config.client.slug
    for name in ("page_entities.csv", "entity_coverage.csv", "bridge_links.csv", f"{slug}_knowledge_graph.gexf"):
        paths[name] = out / name
    write_page_entities_csv(rows, paths["page_entities.csv"])
    write_dataclass_csv(cov, EntityCoverage, paths["entity_coverage.csv"])
    write_dataclass_csv(bridges, BridgeLink, paths["bridge_links.csv"])
    nx.write_gexf(knowledge_graph(g, rows, run.entries, categories), paths[f"{slug}_knowledge_graph.gexf"])
    report["entities"] = {
        "extraction": meta,
        "coverage": [c.__dict__ for c in cov],
        "flagged": [c.__dict__ for c in cov if c.flag],
        "bridge_links": len(bridges),
    }

    tagged = len({r["url"] for r in rows})
    L = ["## Entities: what the content is about, and how far each topic is from the win", ""]
    L.append(f"{tagged} of {meta.get('pages', '?')} loaded pages carry at least one of {len(cov)} entities "
             f"({len(rows)} tags; taxonomy `{tax_path or 'entities.yaml'}`). Boilerplate removed first: "
             f"{meta.get('boilerplate_words_dropped', 0)} repeated words and "
             f"{meta.get('boilerplate_headings_dropped', 0)} repeated headings. Full lists: `page_entities.csv`, "
             "`entity_coverage.csv`.")
    L.append("")
    L.append("| entity | type | pages | link to win (content) | median clicks (content) | within 2 clicks (content) "
             "| median clicks (all) | within 2 clicks (all) | flag |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for c in cov[:40]:
        L.append(f"| {c.entity} | {c.entity_type} | {c.pages_tagged} | {c.pages_linking_win_content} | "
                 f"{_fmt(c.median_clicks_content)} | {c.pct_within_2_content}% | {_fmt(c.median_clicks_all)} | "
                 f"{c.pct_within_2_all}% | {'⚠ ' + c.flag if c.flag else ''} |")
    if len(cov) > 40:
        L.append(f"| … {len(cov) - 40} more in entity_coverage.csv | | | | | | | | |")
    L.append("")
    flagged = [c for c in cov if c.flag]
    if flagged:
        L.append(f"**Flagged** (at least {tax.coverage.flag_min_pages} pages, and none or most of them cannot reach "
                 "the win with content links): " + "; ".join(
                     f"{c.entity} ({c.entity_type}, {c.no_path_content} of {c.pages_tagged} pages with no path)"
                     for c in flagged) + ".")
        L.append("")

    L.append("### Bridge links")
    L.append("")
    if bridges:
        pages = len({b.page for b in bridges})
        L.append(f"{pages} pages are more than one content click from the win and share entities with a page that "
                 "links to it. Adding a content link to the suggested page brings each within two clicks. "
                 "Best suggestion per page, strongest first (all suggestions: `bridge_links.csv`):")
        L.append("")
        L.append("| page | clicks now (content) | link to | shared entities | score |")
        L.append("|---|---|---|---|---|")
        best = sorted((b for b in bridges if b.rank == 1), key=lambda b: (-b.shared_score, b.page))
        for b in best[:25]:
            names = b.shared_entities.split("; ")
            shared = "; ".join(names[:4]) + (f" (+{len(names) - 4} more)" if len(names) > 4 else "")
            L.append(f"| {short(b.page)} | {_fmt(b.page_clicks_to_win_content) if b.page_clicks_to_win_content is not None else 'no path'} "
                     f"| {short(b.bridge_page)} | {shared} | {b.shared_score} |")
        if len(best) > 25:
            L.append(f"| … {len(best) - 25} more pages in bridge_links.csv | | | | |")
    else:
        L.append("No suggestions: every tagged page is already within one content click of the win, or no page "
                 "linking to the win shares its entities.")
    L.append("")
    return L


# --------------------------------------------------------------------------- entry point


def write_report(run, out_dir: Path | None = None) -> dict[str, Path]:
    """Analyze the run and write every report file. Returns the paths written."""
    out = Path(out_dir or run.dir)
    analysis = run.analyze()
    categories = categorize(run.store, run.graph, analysis, run.config.scope.locale_include)
    summary = summarize(categories)
    short = make_short(list(run.graph))
    mermaid = mermaid_paths(analysis, short)

    paths = {
        "report.md": out / "report.md",
        "report.json": out / "report.json",
        "graph.graphml": out / "graph.graphml",
        "paths.mmd": out / "paths.mmd",
        "categories.csv": out / "categories.csv",
        "graph.gexf": out / "graph.gexf",
        "graph_content_only.gexf": out / "graph_content_only.gexf",
    }
    store = run.store
    statuses = dict(store.db.execute("SELECT status, COUNT(*) FROM pages GROUP BY status").fetchall())
    report = {
        "client": run.config.client.name,
        "campaign": {
            "id": store.meta("campaign_id"),
            "name": store.meta("campaign_name"),
            "ad_copy": store.meta("ad_copy"),
            "ad_urls": store.meta("ad_urls", []),
        },
        "win": {"name": run.config.win.name, "url_patterns": run.config.win.url_patterns},
        "win_pages": win_pages(run),
        "win_near_misses": near_misses(run),
        "crawl": {
            "status": store.meta("status"),
            "started_at": store.meta("started_at"),
            "finished_at": store.meta("finished_at"),
            "pages_loaded": store.explored_count(),
            "pages_by_status": statuses,
            "max_depth": run.config.crawl.max_depth,
            "max_pages": run.config.crawl.max_pages,
        },
        "entry_links": [dict(r) for r in store.entries()],
        "headline": headline(analysis, run.config.win.name),
        "analysis": analysis.to_dict(),
        "categories": summary,
        "operator_actions": [dict(r) for r in store.operator_actions()],
        "definitions": DEFINITIONS,
    }
    extra = []
    entity_rows = store.page_entities()
    if entity_rows:
        extra += entity_sections(run, entity_rows, categories, report, paths, out, short)
    annotations = Annotations()
    lead_nodes, lead_edges = lead_annotations(store, run.graph)
    if lead_nodes or store.meta("leads"):
        annotations.add_nodes({"leads_origin": 0.0, "leads_exact": 0.0, "leads_landed": 0.0}, lead_nodes)
        annotations.add_edges({"leads": 0.0}, {e: {"leads": v} for e, v in lead_edges.items()})
        extra += lead_section(store, run.graph, run.config.leads.min_cell, short)
        report["leads"] = {k: v for k, v in (store.meta("leads") or {}).items() if k not in ("tags", "conversion_pages")}
    dead_nodes, dead_edges = dead_annotations(store)
    annotations.add_nodes({"is_dead": False, "dead_reason": "", "inbound_dead_links": 0, "dead_inbound_pages": 0,
                           "dead_inbound_body_links": 0}, dead_nodes)
    annotations.add_edges({"to_dead": False}, dead_edges)
    extra += dead_section(store, short)
    paths[f"{run.config.client.slug}_dead_pages.csv"] = out / f"{run.config.client.slug}_dead_pages.csv"
    write_dead_csv(dead_links(store), dead_pages(store), paths[f"{run.config.client.slug}_dead_pages.csv"])
    report["dead_pages"] = {"count": len(dead_nodes), "pages": dead_nodes}
    home = health_home(run)
    health_rows = page_health(run, categories, home)
    extra_recorded = bool(store.db.execute("SELECT 1 FROM pages WHERE og_properties IS NOT NULL LIMIT 1").fetchone())
    site_signals = store.meta("site_signals") or {}
    health_md, health_summary = health_section(health_rows, home, site_signals, short, extra_recorded)
    paths[f"{run.config.client.slug}_site_health.csv"] = out / f"{run.config.client.slug}_site_health.csv"
    write_health_csv(health_rows, paths[f"{run.config.client.slug}_site_health.csv"])
    annotations.add_nodes({"has_structured_data": False, "js_dependent": False, "clicks_from_home_body_links": -1,
                           "clicks_from_home_all_links": -1}, health_annotations(health_rows))
    report["health"] = {"home_url": home, "summary": health_summary, "site_signals": site_signals}
    if store.meta("external_seeds"):
        extra += external_seed_section(run, short)
        report["external_seeds"] = external_seed_rows(run)
    findings = site_findings(store, categories, [w["url"] for w in report["win_pages"]],
                             [m["url"] for m in report["win_near_misses"]], entity_rows)
    report["site_findings"] = findings
    extra += recommendations_markdown(findings, run.config.win.name)
    extra += health_md
    if annotations.node_defaults:
        report["nodes"] = {n: annotations.node(n) for n in sorted(run.graph)}
    if annotations.edge_defaults:
        report["edges"] = [{"src": u, "dst": v, **d} for (u, v), d in sorted(annotations.edges.items())]
    paths["report.json"].write_text(json.dumps(report, indent=2, default=lambda v: list(v)), encoding="utf-8")
    paths["report.md"].write_text(markdown_report(run, analysis, summary, mermaid, short, extra), encoding="utf-8")
    paths["paths.mmd"].write_text(mermaid, encoding="utf-8")
    nx.write_graphml(graphml_graph(run.graph, categories), paths["graph.graphml"])
    write_csv(categories, paths["categories.csv"])
    for name, content_only in (("graph.gexf", False), ("graph_content_only.gexf", True)):
        nx.write_gexf(gexf_graph(run.graph, run.entries, categories, content_only, annotations=annotations), paths[name])
    return paths
