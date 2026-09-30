from pathcrawl.graph import graph_from_store
from pathcrawl.store import LinkRecord, PageRecord, Store


def link(url, region="body", in_scope=True):
    return LinkRecord(href=url, url=url, text="t", region=region, in_scope=in_scope)


def test_queue_is_breadth_first_and_deduplicated(tmp_path):
    s = Store(tmp_path / "crawl.db")
    assert s.enqueue("a", 0, None)
    assert s.enqueue("c", 1, "a")
    assert s.enqueue("b", 0, None)
    assert not s.enqueue("a", 2, "c")  # already known
    order = []
    while (item := s.next_pending()) is not None:
        order.append((item.url, item.depth))
        s.mark_queue(item.url, "done")
    assert order == [("a", 0), ("b", 0), ("c", 1)]


def test_state_survives_reopening(tmp_path):
    s = Store(tmp_path / "crawl.db")
    s.set_meta(campaign_id="c1", status="running")
    s.enqueue("a", 0, None)
    s.enqueue("b", 1, "a")
    s.save_page(PageRecord(url="a", status="ok", depth=0), [link("b")], queue_url="a")
    s.close()

    s2 = Store(tmp_path / "crawl.db")
    assert s2.meta("campaign_id") == "c1"
    assert s2.next_pending().url == "b"
    assert s2.has_page("a")


def test_redirect_aliases_and_resolution(tmp_path):
    s = Store(tmp_path / "crawl.db")
    s.save_page(PageRecord(url="final", status="ok", requested_url="old", redirect_chain=["old", "mid", "final"]), [])
    assert s.resolve("old") == "final" and s.resolve("mid") == "final" and s.resolve("other") == "other"
    assert not s.enqueue("old", 1, None)  # known under its final URL


def test_graph_from_store(tmp_path):
    s = Store(tmp_path / "crawl.db")
    s.add_entry(0, "ad link", "https://x.test/start?utm=1", "start")
    s.save_page(PageRecord(url="start", status="ok"), [
        link("old"),                         # resolves to win
        link("gone"),                        # redirected off-site: dropped
        link("next", region="nav"),
        link("https://ads.test/", in_scope=False),
    ])
    s.save_page(PageRecord(url="win", status="ok", requested_url="old", redirect_chain=["old", "win"],
                           win=True, win_source="pattern"), [])
    s.save_page(PageRecord(url="gone", status="offsite"), [])
    s.save_page(PageRecord(url="wall", status="skipped"), [])
    s.add_operator_link("wall", "win")

    g, entries = graph_from_store(s)
    assert set(g) == {"start", "win", "next", "wall"}
    assert g.edges["start", "win"]["regions"] == {"body"}
    assert g.edges["start", "next"]["regions"] == {"nav"}
    assert g.nodes["next"]["explored"] is False and g.nodes["wall"]["explored"] is False
    assert g.edges["wall", "win"]["operator"] is True
    assert g.nodes["win"]["win"] is True
    assert [(e.label, e.url) for e in entries] == [("ad link", "start")]


def test_operator_actions_are_logged(tmp_path):
    s = Store(tmp_path / "crawl.db")
    s.log_action("u", "blocked", "skip", "HTTP 403")
    assert [(r["problem"], r["action"]) for r in s.operator_actions()] == [("blocked", "skip")]


def test_win_is_matched_by_url_whatever_the_crawl_status(tmp_path):
    """Regression: a win URL that robots.txt blocked, or that was only ever a
    link target, must still be a win node, and never "unknown"."""
    from pathcrawl.config import parse_config
    from pathcrawl.graph import EntryPoint, analyze

    win = parse_config({
        "client": {"name": "T", "slug": "t"},
        "scope": {"allowed_domains": ["x.test"]},
        "win": {"name": "W", "url_patterns": ["https://x.test/talk*", "https://x.test/contact"]},
        "campaigns": [{"id": "c", "name": "c", "platform": "p", "ad_copy": "a",
                       "entry_links": [{"label": "e", "url": "https://x.test/a"}]}],
    }).win
    s = Store(tmp_path / "crawl.db")
    s.add_entry(0, "a", "https://x.test/a", "https://x.test/a")
    s.add_entry(1, "b", "https://x.test/b", "https://x.test/b")
    s.save_page(PageRecord(url="https://x.test/a", status="ok"), [link("https://x.test/talk-v4.html")])
    s.save_page(PageRecord(url="https://x.test/b", status="ok"), [link("https://x.test/contact")])
    s.save_page(PageRecord(url="https://x.test/talk-v4.html", status="robots", error="disallowed by robots.txt"), [])
    # https://x.test/contact was never fetched at all (no pages row)

    g, entries = graph_from_store(s)  # without the win config: the old behaviour
    assert g.nodes["https://x.test/talk-v4.html"]["win"] is False

    g, entries = graph_from_store(s, win)
    for url, status in (("https://x.test/talk-v4.html", "robots"), ("https://x.test/contact", None)):
        assert g.nodes[url]["win"] is True and g.nodes[url]["win_source"] == "pattern"
        assert g.nodes[url]["explored"] is False and g.nodes[url]["status"] == status
    res = analyze(g, entries, max_depth=8).modes["content_only"]
    assert [e.shortest_clicks for e in res.entries] == [1, 1]
    assert res.dead_zones.unknown == [] and res.dead_zones.unexplored_reachable == []
    assert res.worst_case.distribution == {0: 2, 1: 2}


def test_loaded_win_without_required_form_is_not_a_win(tmp_path):
    from pathcrawl.config import parse_config

    win = parse_config({
        "client": {"name": "T", "slug": "t"},
        "scope": {"allowed_domains": ["x.test"]},
        "win": {"name": "W", "url_patterns": ["https://x.test/talk*"], "form_selector": "form", "require_form": True},
        "campaigns": [{"id": "c", "name": "c", "platform": "p", "ad_copy": "a",
                       "entry_links": [{"label": "e", "url": "https://x.test/a"}]}],
    }).win
    s = Store(tmp_path / "crawl.db")
    s.save_page(PageRecord(url="https://x.test/a", status="ok"),
                [link("https://x.test/talk-1"), link("https://x.test/talk-2")])
    s.save_page(PageRecord(url="https://x.test/talk-1", status="ok", form_present=False), [])
    g, _ = graph_from_store(s, win)
    assert g.nodes["https://x.test/talk-1"]["win"] is False  # loaded, form missing
    assert g.nodes["https://x.test/talk-2"]["win"] is True   # not loaded: matched by URL
