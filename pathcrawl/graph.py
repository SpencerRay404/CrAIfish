"""Link graph and every path metric.

All metrics are deterministic graph computations: no sampling, no LLM. Wherever
a choice between equally good answers is needed, neighbours are visited in
sorted URL order so the same graph always produces the same report.

Terms used throughout (repeated in the report):

- **Click**: following one link. A path of N clicks visits N+1 pages.
- **Win page**: a page matching ``win.url_patterns`` or marked as a win by the
  operator. A URL that matches the patterns is a win whether or not the crawler
  loaded it (robots.txt may forbid it, or the crawl never fetched it): a link to
  it is all a journey needs. The win is the goal, so it is terminal: its own
  outbound links are never followed, crawled or counted.
- **Mode**: every metric is computed twice. ``all_links`` counts every link;
  ``content_only`` ignores links in the nav, header and footer, so a global
  "Contact us" link does not make every page look one click from the win.
- **Explored page**: a page the crawler loaded, so its outbound links are known.
  Pages that were discovered but never loaded (page budget, depth limit,
  skipped by the operator) are **unexplored**. Win pages are never counted as
  unexplored, because nothing past them matters.
- **Dead end**: an explored page that provably cannot reach a win page.
  Every page reachable from it is explored and none is a win. A page whose
  only hope runs through unexplored pages is **unknown**, not a dead end.
- **Trap loop**: a group of two or more dead-end pages that all link to each
  other (a strongly connected component), so a visitor can click around in
  circles without ever reaching the win.
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

import networkx as nx

from pathcrawl.extract import BODY, CHROME_REGIONS

ALL_LINKS = "all_links"
CONTENT_ONLY = "content_only"
MODES = (ALL_LINKS, CONTENT_ONLY)

DEFAULT_TIME_BUDGET_S = 5.0


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class Page:
    url: str
    explored: bool = True
    win: bool = False
    win_source: str | None = None  # "pattern" or "operator"


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    region: str = BODY
    operator: bool = False  # the operator supplied this jump; no real link exists for it


@dataclass(frozen=True)
class EntryPoint:
    label: str
    url: str  # the node the entry link landed on, after redirects and normalization


def build_graph(pages: Iterable[Page], edges: Iterable[Edge]) -> nx.DiGraph:
    """Directed graph of pages and links.

    Several links between the same two pages collapse into one edge that
    remembers every region it appeared in, plus whether an operator edge exists.
    Self-links are dropped, since clicking them goes nowhere. A link target that
    is not in ``pages`` is added as an unexplored page. Callers must pass only
    in-scope edges, because off-allowlist pages are never part of the graph.
    """
    g = nx.DiGraph()
    for p in pages:
        g.add_node(p.url, explored=p.explored, win=p.win, win_source=p.win_source)
    for e in edges:
        if e.src == e.dst:
            continue
        for n in (e.src, e.dst):
            if n not in g:
                g.add_node(n, explored=False, win=False, win_source=None)
        if not g.has_edge(e.src, e.dst):
            g.add_edge(e.src, e.dst, regions=set(), operator=False)
        data = g.edges[e.src, e.dst]
        if e.operator:
            data["operator"] = True
        else:
            data["regions"].add(e.region)
    return g


def edge_counts(data: dict, mode: str, include_operator: bool = True) -> bool:
    """Whether an edge exists in the given mode."""
    if include_operator and data["operator"]:
        return True
    regions = data["regions"]
    return bool(regions) if mode == ALL_LINKS else bool(regions - CHROME_REGIONS)


def mode_view(g: nx.DiGraph, mode: str, include_operator: bool = True) -> nx.DiGraph:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    # Win pages are terminal: the journey ends there, so their outbound links never count.
    return nx.subgraph_view(
        g,
        filter_edge=lambda u, v: not g.nodes[u]["win"] and edge_counts(g.edges[u, v], mode, include_operator),
    )


def _is_operator_only(g: nx.DiGraph, u: str, v: str, mode: str) -> bool:
    """True if this hop exists in ``mode`` only because of an operator edge."""
    return edge_counts(g.edges[u, v], mode, include_operator=True) and not edge_counts(
        g.edges[u, v], mode, include_operator=False
    )


# --------------------------------------------------------------------------- primitives


def win_nodes(g: nx.DiGraph) -> list[str]:
    return sorted(n for n, d in g.nodes(data=True) if d["win"])


def distances_to_win(h: nx.DiGraph) -> dict[str, int]:
    """Shortest click distance from each page to its nearest win (reverse BFS).

    Pages missing from the result have no path to any win.
    """
    dist = {w: 0 for w in win_nodes(h)}
    queue = deque(sorted(dist))
    while queue:
        node = queue.popleft()
        for pred in sorted(h.predecessors(node)):
            if pred not in dist:
                dist[pred] = dist[node] + 1
                queue.append(pred)
    return dist


def _reverse_reachable(h: nx.DiGraph, targets: Iterable[str]) -> set[str]:
    """All pages with a path to any of ``targets`` (targets included)."""
    seen = set(targets)
    queue = deque(seen)
    while queue:
        node = queue.popleft()
        for pred in h.predecessors(node):
            if pred not in seen:
                seen.add(pred)
                queue.append(pred)
    return seen


def _bfs(h: nx.DiGraph, src: str, stop) -> list[str] | None:
    """Shortest path from ``src`` to the first node satisfying ``stop``.

    Visits neighbours in sorted order, so ties break deterministically.
    """
    if stop(src):
        return [src]
    parent = {src: None}
    queue = deque([src])
    while queue:
        node = queue.popleft()
        for nxt in sorted(h.successors(node)):
            if nxt in parent:
                continue
            parent[nxt] = node
            if stop(nxt):
                path = [nxt]
                while parent[path[-1]] is not None:
                    path.append(parent[path[-1]])
                return path[::-1]
            queue.append(nxt)
    return None


def shortest_path_to_win(h: nx.DiGraph, src: str) -> list[str] | None:
    """BFS from ``src`` to the nearest win page; None if there is none."""
    return _bfs(h, src, lambda n: h.nodes[n]["win"])


class _OutOfTime(Exception):
    pass


def longest_simple_path_to_win(
    h: nx.DiGraph,
    src: str,
    max_depth: int,
    dist: dict[str, int] | None = None,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
) -> tuple[list[str] | None, bool]:
    """Longest simple (non-repeating) path from ``src`` to a win page within
    ``max_depth`` clicks.

    A path ends at the first win page it reaches: a visitor who has reached
    the win has converted, so routes that pass through a win don't count.
    Unbounded longest paths are undefined in graphs with loops, which is why
    this is limited to simple paths within ``max_depth``.

    Bounded DFS with pruning: a branch is abandoned when even its shortest
    route to a win would exceed ``max_depth``. Returns ``(path, exhaustive)``.
    If the time budget runs out, ``exhaustive`` is False and ``path`` is the
    longest found so far, so treat it as a lower bound.
    """
    if dist is None:
        dist = distances_to_win(h)
    if src not in dist or dist[src] > max_depth:
        return None, True

    deadline = time.monotonic() + time_budget_s
    best: list[str] | None = None
    path = [src]
    on_path = {src}

    def dfs(node: str) -> bool:
        """Returns True when the search can stop (a max-length path was found)."""
        nonlocal best
        if time.monotonic() > deadline:
            raise _OutOfTime
        if h.nodes[node]["win"]:
            if best is None or len(path) > len(best):
                best = list(path)
            return len(path) - 1 == max_depth
        clicks = len(path) - 1
        for nxt in sorted(h.successors(node)):
            if nxt in on_path or nxt not in dist or clicks + 1 + dist[nxt] > max_depth:
                continue
            path.append(nxt)
            on_path.add(nxt)
            done = dfs(nxt)
            path.pop()
            on_path.discard(nxt)
            if done:
                return True
        return False

    try:
        dfs(src)
    except _OutOfTime:
        return best, False
    return best, True


# --------------------------------------------------------------------------- results


@dataclass
class EntryResult:
    label: str
    url: str
    in_graph: bool
    # Metric 1: shortest path to a win (BFS).
    shortest_clicks: int | None = None
    shortest_path: list[str] | None = None
    win_reached: str | None = None
    win_marked_by_operator: bool = False
    # Metric 2: longest simple path to a win within max_depth.
    longest_clicks: int | None = None
    longest_path: list[str] | None = None
    longest_exhaustive: bool = True
    # Metric 4: first click at which this journey can enter a dead zone.
    dead_zone_click: int | None = None
    dead_zone_path: list[str] | None = None
    # Metric 6: operator dependency.
    shortest_uses_operator_edge: bool = False
    shortest_clicks_without_operator: int | None = None


@dataclass
class WorstCase:
    """Metric 3, over explored pages reachable from the entry links."""

    max_clicks: int | None
    pages_at_max: list[str]
    distribution: dict[int, int]  # clicks-to-win -> page count
    no_path_count: int  # dead ends + unknown


@dataclass
class DeadZones:
    """Metric 4."""

    dead_ends: list[str]
    dead_end_count: int
    crawled_pages: int  # every explored page in the graph (the denominator)
    dead_end_pct: float
    trap_loops: list[list[str]]
    unknown: list[str]  # no known path to win, but might reach one through unexplored pages
    unexplored_reachable: list[str]


@dataclass
class Convergence:
    """Metric 5."""

    all_reach_win: bool
    wins_reached: list[str]
    converged: bool  # every entry link reaches the win, and all reach the same win page
    common_pages: list[str]  # pages on every entry link's shortest path
    pairwise_overlap: list[dict]  # Jaccard overlap of the shortest paths' page sets


@dataclass
class ModeResult:
    mode: str
    reachable_pages: int
    entries: list[EntryResult]
    worst_case: WorstCase
    dead_zones: DeadZones
    convergence: Convergence
    # Labels that reach a win only thanks to the operator: no path without
    # operator edges, or the win itself was marked by the operator.
    operator_dependent_entries: list[str]


@dataclass
class Analysis:
    max_depth: int
    win_pages: list[str]
    operator_win_pages: list[str]
    operator_edges: list[tuple[str, str]]
    modes: dict[str, ModeResult] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- analysis


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def analyze_mode(
    g: nx.DiGraph,
    entries: list[EntryPoint],
    mode: str,
    max_depth: int,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
) -> ModeResult:
    h = mode_view(g, mode)
    h_no_op = mode_view(g, mode, include_operator=False)
    dist = distances_to_win(h)

    starts = sorted({e.url for e in entries if e.url in g})
    reachable = set(starts)
    for s in starts:
        reachable |= nx.descendants(h, s)

    # A win is the goal, never "unexplored", even when it was not loaded.
    unexplored = {n for n in g if not g.nodes[n]["explored"] and not g.nodes[n]["win"]}
    can_reach_unexplored = _reverse_reachable(h, unexplored)
    settled_reachable = sorted(n for n in reachable if g.nodes[n]["explored"] or g.nodes[n]["win"])
    dead = [n for n in settled_reachable if n not in dist and n not in can_reach_unexplored]
    unknown = [n for n in settled_reachable if n not in dist and n in can_reach_unexplored]
    dead_set = set(dead)

    # A dead page's successors are all dead too, so each SCC is all-dead or none.
    trap_loops = sorted(
        (sorted(c) for c in nx.strongly_connected_components(h.subgraph(dead)) if len(c) >= 2),
        key=lambda c: (-len(c), c[0]),
    )

    crawled = sum(1 for n in g if g.nodes[n]["explored"])
    dead_zones = DeadZones(
        dead_ends=dead,
        dead_end_count=len(dead),
        crawled_pages=crawled,
        dead_end_pct=round(100 * len(dead) / crawled, 1) if crawled else 0.0,
        trap_loops=trap_loops,
        unknown=unknown,
        unexplored_reachable=sorted(n for n in reachable if n in unexplored),
    )

    finite = {n: dist[n] for n in settled_reachable if n in dist}
    max_clicks = max(finite.values()) if finite else None
    worst_case = WorstCase(
        max_clicks=max_clicks,
        pages_at_max=sorted(n for n, d in finite.items() if d == max_clicks),
        distribution={d: sum(1 for v in finite.values() if v == d) for d in sorted(set(finite.values()))},
        no_path_count=len(dead) + len(unknown),
    )

    results = []
    for e in entries:
        r = EntryResult(label=e.label, url=e.url, in_graph=e.url in g)
        if r.in_graph:
            sp = shortest_path_to_win(h, e.url)
            if sp:
                r.shortest_path, r.shortest_clicks, r.win_reached = sp, len(sp) - 1, sp[-1]
                r.win_marked_by_operator = g.nodes[sp[-1]]["win_source"] == "operator"
                r.shortest_uses_operator_edge = any(_is_operator_only(g, u, v, mode) for u, v in itertools.pairwise(sp))
            sp_no_op = shortest_path_to_win(h_no_op, e.url)
            r.shortest_clicks_without_operator = len(sp_no_op) - 1 if sp_no_op else None

            lp, exhaustive = longest_simple_path_to_win(h, e.url, max_depth, dist, time_budget_s)
            r.longest_path, r.longest_exhaustive = lp, exhaustive
            r.longest_clicks = len(lp) - 1 if lp else None

            dz = _bfs(h, e.url, lambda n: n in dead_set)
            if dz:
                r.dead_zone_path, r.dead_zone_click = dz, len(dz) - 1
        results.append(r)

    paths = [(r.label, set(r.shortest_path)) for r in results if r.shortest_path]
    wins_reached = sorted({r.win_reached for r in results if r.win_reached})
    all_reach = bool(results) and all(r.shortest_path for r in results)
    convergence = Convergence(
        all_reach_win=all_reach,
        wins_reached=wins_reached,
        converged=all_reach and len(wins_reached) == 1,
        common_pages=sorted(set.intersection(*(p for _, p in paths))) if paths else [],
        pairwise_overlap=[
            {"a": la, "b": lb, "jaccard": round(_jaccard(pa, pb), 3), "shared_pages": sorted(pa & pb)}
            for (la, pa), (lb, pb) in itertools.combinations(paths, 2)
        ],
    )

    return ModeResult(
        mode=mode,
        reachable_pages=len(reachable),
        entries=results,
        worst_case=worst_case,
        dead_zones=dead_zones,
        convergence=convergence,
        operator_dependent_entries=[
            r.label
            for r in results
            if r.shortest_path and (r.shortest_clicks_without_operator is None or r.win_marked_by_operator)
        ],
    )


def analyze(
    g: nx.DiGraph,
    entries: list[EntryPoint],
    max_depth: int,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
) -> Analysis:
    """Run every metric in both modes."""
    analysis = Analysis(
        max_depth=max_depth,
        win_pages=win_nodes(g),
        operator_win_pages=sorted(n for n in win_nodes(g) if g.nodes[n]["win_source"] == "operator"),
        operator_edges=sorted((u, v) for u, v, d in g.edges(data=True) if d["operator"]),
    )
    for mode in MODES:
        analysis.modes[mode] = analyze_mode(g, entries, mode, max_depth, time_budget_s)
    return analysis


# --------------------------------------------------------------------------- from a crawl


def graph_from_store(store, win=None) -> tuple[nx.DiGraph, list[EntryPoint]]:
    """Build the graph and entry points from a crawl database (``pathcrawl.store.Store``).

    - Loaded pages (``ok``, ``http_error``) are explored nodes.
    - Pages the crawl could not or would not load (skipped, blocked by robots)
      are unexplored nodes, as are in-scope links the crawl never reached.
    - Pages that redirected off the allowlist are left out, along with links to them.
    - Link targets are resolved through redirects, so a link to an old URL
      points at the page it actually lands on.
    - With ``win`` (the config's ``WinConfig``), every node whose URL matches
      the win patterns is a win, whatever happened when crawling it: loaded,
      blocked by robots.txt, errored, or only ever seen as a link target. The
      one exception is a page that loaded without the form when the config
      says ``require_form: true``.
    - Every node gets ``status``: its stored crawl status, or ``None`` for a
      link target that was never fetched.
    """
    from pathcrawl.store import EXPLORED_STATUSES

    pages, offsite, statuses, form_missing = [], set(), {}, set()
    for row in store.pages():
        if row["status"] == "offsite":
            offsite.add(row["url"])
            continue
        statuses[row["url"]] = row["status"]
        if row["status"] in EXPLORED_STATUSES and row["form_present"] == 0:
            form_missing.add(row["url"])
        pages.append(
            Page(
                url=row["url"],
                explored=row["status"] in EXPLORED_STATUSES,
                win=bool(row["win"]),
                win_source=row["win_source"],
            )
        )
    edges = []
    for row in store.links():
        if not row["in_scope"] or not row["url"]:
            continue
        dst = store.resolve(row["url"])
        if dst in offsite:
            continue
        region = row["region"] if not row["operator"] else BODY
        edges.append(Edge(row["src"], dst, region, operator=bool(row["operator"])))
    entries = [EntryPoint(r["label"], r["node_url"]) for r in store.entries()]
    g = build_graph(pages, edges)
    for n in g:
        g.nodes[n]["status"] = statuses.get(n)
    if win is not None:
        mark_pattern_wins(g, win, form_missing)
    return g, entries


def mark_pattern_wins(g: nx.DiGraph, win, form_missing: Iterable[str] = ()) -> None:
    """Mark every node whose URL matches the win patterns as a win
    (``win_source`` "pattern"), except loaded pages that lack a required form."""
    form_missing = set(form_missing)
    for n, d in g.nodes(data=True):
        if d["win"] or not win.url_matches(n):
            continue
        if win.require_form and n in form_missing:
            continue
        d["win"], d["win_source"] = True, "pattern"
