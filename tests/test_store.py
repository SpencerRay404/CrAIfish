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
