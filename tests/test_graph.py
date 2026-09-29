"""Path metrics, proven against the fixture site (see tests/fixtures/site/README.md)
and a few small synthetic graphs for the edge cases the fixture doesn't cover."""

import time

import pytest

from pathcrawl.graph import (
    ALL_LINKS,
    CONTENT_ONLY,
    Edge,
    EntryPoint,
    Page,
    analyze,
    build_graph,
    longest_simple_path_to_win,
    mode_view,
)
from pathcrawl.selftest import FIXTURE_BASE as BASE
from pathcrawl.selftest import fixture_config, graph_from_html_files

FIXTURE_CONFIG = fixture_config()


def u(name: str) -> str:
    return BASE + name + ".html"


def fixture_graph():
    """Built exactly as the crawler will: extract links, keep in-scope, mark wins."""
    return graph_from_html_files(FIXTURE_CONFIG)


ENTRIES = [EntryPoint("near", u("entry-near")), EntryPoint("far", u("entry-far"))]


@pytest.fixture(scope="module")
def result():
    return analyze(fixture_graph(), ENTRIES, max_depth=8)


def entry(result, mode, label):
    return next(e for e in result.modes[mode].entries if e.label == label)


# ------------------------------------------------------------------ graph shape


def test_fixture_graph_shape():
    g = fixture_graph()
    assert len(g) == 10
    # every page is loaded except the win, which is the goal and never loaded
    assert [n for n, d in g.nodes(data=True) if not d["explored"]] == [u("win")]
    assert [n for n, d in g.nodes(data=True) if d["win"]] == [u("win")]
    assert g.out_degree(u("win")) == 0  # its links are not followed
    # the orphan has no outbound links; self-links were dropped
    assert g.out_degree(u("orphan")) == 0
    assert not g.has_edge(u("win"), u("win"))
    # a link present in both nav and body keeps both regions
    assert g.edges[u("entry-near"), u("win")]["regions"] == {"nav", "body"}
    assert g.edges[u("trap-a"), u("win")]["regions"] == {"nav"}


def test_content_view_drops_chrome_edges():
    g = fixture_graph()
    content = mode_view(g, CONTENT_ONLY)
    assert content.has_edge(u("entry-near"), u("win"))  # body link survives
    assert not content.has_edge(u("trap-a"), u("win"))  # nav-only link does not
    assert not content.has_edge(u("article-1"), u("about"))  # footer-only link does not


# ------------------------------------------------------------------ metric 1: shortest


def test_shortest_all_links(result):
    assert entry(result, ALL_LINKS, "near").shortest_path == [u("entry-near"), u("win")]
    far = entry(result, ALL_LINKS, "far")
    assert far.shortest_clicks == 1
    assert far.shortest_path == [u("entry-far"), u("win")]


def test_shortest_content_only(result):
    assert entry(result, CONTENT_ONLY, "near").shortest_clicks == 1
    far = entry(result, CONTENT_ONLY, "far")
    assert far.shortest_clicks == 3
    assert far.shortest_path == [u("entry-far"), u("article-1"), u("article-2"), u("win")]
    assert far.win_reached == u("win")


# ------------------------------------------------------------------ metric 2: longest simple


def test_longest_all_links(result):
    near = entry(result, ALL_LINKS, "near")
    assert near.longest_clicks == 2
    assert near.longest_path == [u("entry-near"), u("about"), u("win")]
    far = entry(result, ALL_LINKS, "far")
    assert far.longest_clicks == 6
    assert far.longest_path == [
        u("entry-far"), u("article-1"), u("trap-a"), u("trap-b"), u("trap-c"), u("about"), u("win"),
    ]
    assert far.longest_exhaustive


def test_longest_content_only(result):
    assert entry(result, CONTENT_ONLY, "near").longest_clicks == 1
    assert entry(result, CONTENT_ONLY, "far").longest_clicks == 3


def test_longest_respects_max_depth():
    small = analyze(fixture_graph(), ENTRIES, max_depth=4)
    far = entry(small, ALL_LINKS, "far")
    assert far.longest_clicks == 4
    assert far.longest_path == [u("entry-far"), u("article-1"), u("article-2"), u("about"), u("win")]
    # content-only far needs 3 clicks: fits in 3, not in 2
    assert entry(analyze(fixture_graph(), ENTRIES, max_depth=3), CONTENT_ONLY, "far").longest_clicks == 3
    assert entry(analyze(fixture_graph(), ENTRIES, max_depth=2), CONTENT_ONLY, "far").longest_path is None


# ------------------------------------------------------------------ metric 3: worst case


def test_worst_case_all_links(result):
    wc = result.modes[ALL_LINKS].worst_case
    assert wc.max_clicks == 1
    assert len(wc.pages_at_max) == 8
    assert wc.distribution == {0: 1, 1: 8}
    assert wc.no_path_count == 1


def test_worst_case_content_only(result):
    wc = result.modes[CONTENT_ONLY].worst_case
    assert wc.max_clicks == 3
    assert wc.pages_at_max == [u("entry-far")]
    assert wc.distribution == {0: 1, 1: 2, 2: 1, 3: 1}
    assert wc.no_path_count == 4


def test_reachable_counts(result):
    assert result.modes[ALL_LINKS].reachable_pages == 10
    assert result.modes[CONTENT_ONLY].reachable_pages == 9  # about.html is footer-only


# ------------------------------------------------------------------ metric 4: dead zones


def test_dead_zones_all_links(result):
    dz = result.modes[ALL_LINKS].dead_zones
    assert dz.dead_ends == [u("orphan")]
    assert dz.dead_end_count == 1
    assert dz.crawled_pages == 9
    assert dz.dead_end_pct == 11.1
    assert dz.trap_loops == []
    assert dz.unknown == [] and dz.unexplored_reachable == []


def test_dead_zones_content_only(result):
    dz = result.modes[CONTENT_ONLY].dead_zones
    assert dz.dead_ends == [u("orphan"), u("trap-a"), u("trap-b"), u("trap-c")]
    assert dz.dead_end_count == 4
    assert dz.dead_end_pct == 44.4
    assert dz.trap_loops == [[u("trap-a"), u("trap-b"), u("trap-c")]]


def test_entry_dead_zone_hits(result):
    assert entry(result, ALL_LINKS, "near").dead_zone_click == 1
    assert entry(result, ALL_LINKS, "near").dead_zone_path == [u("entry-near"), u("orphan")]
    assert entry(result, ALL_LINKS, "far").dead_zone_click is None
    assert entry(result, CONTENT_ONLY, "near").dead_zone_click == 1
    far = entry(result, CONTENT_ONLY, "far")
    assert far.dead_zone_click == 2
    assert far.dead_zone_path == [u("entry-far"), u("article-1"), u("trap-a")]


# ------------------------------------------------------------------ metric 5: convergence


def test_convergence(result):
    for mode, jaccard in ((ALL_LINKS, 0.333), (CONTENT_ONLY, 0.2)):
        c = result.modes[mode].convergence
        assert c.all_reach_win and c.converged
        assert c.wins_reached == [u("win")]
        assert c.common_pages == [u("win")]
        assert c.pairwise_overlap[0]["jaccard"] == jaccard


# ------------------------------------------------------------------ metric 6: operator dependency


def test_fixture_has_no_operator_dependency(result):
    assert result.operator_edges == [] and result.operator_win_pages == []
    for mode in (ALL_LINKS, CONTENT_ONLY):
        assert result.modes[mode].operator_dependent_entries == []


def test_operator_edge_rescues_trapped_journey():
    """The operator jumped from trap-c to the win. Content-only, the trap now
    has a way out, but only thanks to the operator."""
    g = fixture_graph()
    g2 = build_graph(
        [Page(n, **d) for n, d in g.nodes(data=True)],
        [Edge(a, b, r) for a, b, d in g.edges(data=True) for r in d["regions"]]
        + [Edge(u("trap-c"), u("win"), operator=True)],
    )
    res = analyze(g2, [EntryPoint("trapped", u("trap-a"))], max_depth=8)
    assert res.operator_edges == [(u("trap-c"), u("win"))]

    content = res.modes[CONTENT_ONLY]
    e = content.entries[0]
    assert e.shortest_path == [u("trap-a"), u("trap-b"), u("trap-c"), u("win")]
    assert e.shortest_uses_operator_edge
    assert e.shortest_clicks_without_operator is None
    assert content.operator_dependent_entries == ["trapped"]
    assert content.dead_zones.trap_loops == []

    # All links: nav already reaches the win, so the operator edge isn't needed.
    e_all = res.modes[ALL_LINKS].entries[0]
    assert e_all.shortest_clicks == 1 and not e_all.shortest_uses_operator_edge
    assert res.modes[ALL_LINKS].operator_dependent_entries == []


def test_operator_marked_win_is_flagged():
    g = build_graph(
        [Page("a"), Page("b", win=True, win_source="operator")],
        [Edge("a", "b")],
    )
    res = analyze(g, [EntryPoint("e", "a")], max_depth=5)
    assert res.operator_win_pages == ["b"]
    e = res.modes[CONTENT_ONLY].entries[0]
    assert e.win_marked_by_operator
    assert res.modes[CONTENT_ONLY].operator_dependent_entries == ["e"]


# ------------------------------------------------------------------ edge cases


def test_unexplored_pages_make_unknown_not_dead():
    """a -> b -> c where c was never loaded: b might still reach the win."""
    g = build_graph(
        [Page("a"), Page("b"), Page("c", explored=False), Page("w", win=True), Page("d")],
        [Edge("a", "b"), Edge("b", "c"), Edge("a", "d"), Edge("x", "w")],
    )
    dz = analyze(g, [EntryPoint("e", "a")], max_depth=5).modes[ALL_LINKS].dead_zones
    assert dz.dead_ends == ["d"]
    assert dz.unknown == ["a", "b"]
    assert dz.unexplored_reachable == ["c"]
    assert dz.crawled_pages == 4  # a, b, d, w; x was auto-added as unexplored


def test_link_target_missing_from_pages_is_unexplored():
    g = build_graph([Page("a")], [Edge("a", "new")])
    assert g.nodes["new"]["explored"] is False


def test_entry_that_is_a_win():
    g = build_graph([Page("w", win=True), Page("x")], [Edge("w", "x")])
    e = analyze(g, [EntryPoint("e", "w")], max_depth=3).modes[ALL_LINKS].entries[0]
    assert e.shortest_clicks == 0 and e.longest_clicks == 0


def test_entry_not_in_graph():
    g = build_graph([Page("w", win=True)], [])
    res = analyze(g, [EntryPoint("missing", "nope")], max_depth=3)
    e = res.modes[ALL_LINKS].entries[0]
    assert not e.in_graph and e.shortest_path is None
    assert not res.modes[ALL_LINKS].convergence.all_reach_win


def test_longest_path_stops_at_first_win():
    """s -> w1 -> x -> w2: reaching w1 ends the journey, so the answer is 1, not 3."""
    g = build_graph(
        [Page("s"), Page("w1", win=True), Page("x"), Page("w2", win=True)],
        [Edge("s", "w1"), Edge("w1", "x"), Edge("x", "w2")],
    )
    path, exhaustive = longest_simple_path_to_win(g, "s", max_depth=5)
    assert path == ["s", "w1"] and exhaustive


def test_convergence_detects_different_wins():
    g = build_graph(
        [Page("a"), Page("b"), Page("w1", win=True), Page("w2", win=True)],
        [Edge("a", "w1"), Edge("b", "w2")],
    )
    c = analyze(g, [EntryPoint("A", "a"), EntryPoint("B", "b")], max_depth=3).modes[ALL_LINKS].convergence
    assert c.all_reach_win and not c.converged
    assert c.wins_reached == ["w1", "w2"]
    assert c.common_pages == [] and c.pairwise_overlap[0]["jaccard"] == 0.0


def test_longest_path_time_budget_gives_lower_bound():
    """A dense graph makes simple-path search explode; the budget must hold."""
    n = 14
    nodes = [f"n{i:02d}" for i in range(n)]
    edges = [Edge(a, b) for a in nodes for b in nodes if a != b] + [Edge(a, "win") for a in nodes]
    g = build_graph([Page(x) for x in nodes] + [Page("win", win=True)], edges)
    start = time.monotonic()
    # max_depth above the longest possible path (n clicks), so the search can
    # never stop early on a max-length path and must run out of time instead.
    path, exhaustive = longest_simple_path_to_win(g, "n00", max_depth=n + 5, time_budget_s=0.2)
    assert time.monotonic() - start < 2.0
    assert not exhaustive
    assert path is not None and path[-1] == "win"


def test_analysis_is_deterministic():
    assert analyze(fixture_graph(), ENTRIES, 8).to_dict() == analyze(fixture_graph(), ENTRIES, 8).to_dict()


def test_win_is_terminal_and_never_unexplored():
    # w is a win that was never loaded; its outbound link must not count
    g = build_graph(
        [Page("a"), Page("w", explored=False, win=True), Page("after")],
        [Edge("a", "w"), Edge("w", "after"), Edge("w", "x")],
    )
    res = analyze(g, [EntryPoint("a", "a")], max_depth=8).modes["content_only"]
    assert res.entries[0].shortest_path == ["a", "w"]
    assert res.dead_zones.unknown == []
    assert res.dead_zones.unexplored_reachable == []  # x lies past the win
    assert res.reachable_pages == 2
