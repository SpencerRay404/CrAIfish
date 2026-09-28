"""Report outputs for a run: report.md, report.json, graph.graphml, paths.mmd,
categories.csv.

Everything here is formatting. The numbers all come from ``graph.analyze`` and
``categorize.categorize``, so the report can never disagree with the analysis.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import networkx as nx

from pathcrawl.categorize import MODE_LABEL, categorize, summarize, write_csv
from pathcrawl.graph import ALL_LINKS, CONTENT_ONLY, MODES, Analysis, distances_to_win, edge_counts, mode_view

DEFINITIONS = {
    "click": "Following one link. A path of N clicks visits N+1 pages.",
    "win page": "A page whose URL matches win.url_patterns (and, if require_form is set, shows the win form), "
    "or a page the operator marked as a win.",
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


# --------------------------------------------------------------------------- markdown


def markdown_report(run, analysis: Analysis, summary: dict, mermaid: str, short) -> str:
    cfg, store = run.config, run.store
    campaign = store.meta("campaign_name") or ""
    win_name = cfg.win.name
    L: list[str] = []
    add = L.append

    add(f"# {cfg.client.name}: how reachable is the {win_name}?")
    add("")
    add(f"**Campaign:** {campaign}  ")
    add(f"**Crawl:** {store.meta('status')} · {store.explored_count()} pages loaded · max depth "
        f"{cfg.crawl.max_depth} · page budget {cfg.crawl.max_pages} · started {store.meta('started_at')}")
    add("")
    add(f"> {headline(analysis, win_name)}")
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

    add("## Metric definitions")
    add("")
    for term, text in DEFINITIONS.items():
        add(f"- **{term}**: {text}")
    add("")
    return "\n".join(L)


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
    }
    store = run.store
    statuses = dict(store.db.execute("SELECT status, COUNT(*) FROM pages GROUP BY status").fetchall())
    report = {
        "client": run.config.client.name,
        "campaign": {
            "id": store.meta("campaign_id"),
            "name": store.meta("campaign_name"),
            "ad_copy": store.meta("ad_copy"),
        },
        "win": {"name": run.config.win.name, "url_patterns": run.config.win.url_patterns},
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
    paths["report.json"].write_text(json.dumps(report, indent=2, default=lambda v: list(v)), encoding="utf-8")
    paths["report.md"].write_text(markdown_report(run, analysis, summary, mermaid, short), encoding="utf-8")
    paths["paths.mmd"].write_text(mermaid, encoding="utf-8")
    nx.write_graphml(graphml_graph(run.graph, categories), paths["graph.graphml"])
    write_csv(categories, paths["categories.csv"])
    return paths
