"""Categorization, report formatting, and run loading (no browser needed:
the run is built from the fixture site's HTML files)."""

import json

import networkx as nx
import pytest
import yaml

from pathcrawl.categorize import page_type_of, section_of
from pathcrawl.graph import analyze
from pathcrawl.report import headline, make_short, mermaid_paths, write_report
from pathcrawl.run import RunError, open_run
from pathcrawl.scoring import JourneyStep, NotConfiguredScorer
from pathcrawl.selftest import FIXTURE_BASE, FIXTURE_SITE, entry_points, fixture_config, graph_from_html_files
from pathcrawl.store import LinkRecord, PageRecord, Store


@pytest.mark.parametrize(
    ("url", "section"),
    [
        ("https://www.ups.com/us/en/shipping/services.page", "shipping"),
        ("https://www.ups.com/us/en/home", "(home)"),
        ("https://www.ups.com/us/en/", "(home)"),
        ("https://solutions.ups.com/manufacturing-ussp-page.html", "manufacturing-ussp-page"),
        ("https://x.test/Blog/Post-1", "blog"),
    ],
)
def test_section_of(url, section):
    assert section_of(url, ["/us/en/"]) == section


@pytest.mark.parametrize(
    ("url", "jsonld", "win", "expected"),
    [
        ("https://x.test/us/en/home", [], False, "home"),
        ("https://x.test/help-center/faq", [], False, "support"),
        ("https://x.test/track?id=1", [], False, "tool"),
        ("https://x.test/insights/supply-chain-risk", [], False, "content"),
        ("https://x.test/about/careers", [], False, "corporate"),
        ("https://x.test/business-solutions/manufacturing", [], False, "product/service"),
        ("https://x.test/login", [], False, "account"),
        ("https://x.test/p/12345", ["BlogPosting"], False, "content"),
        ("https://x.test/anything", [], True, "win"),
        ("https://x.test/p/12345", [], False, "other"),
    ],
)
def test_page_type_of(url, jsonld, win, expected):
    assert page_type_of(url, jsonld, win, section_of(url, ["/us/en/"])) == expected


def fixture_analysis():
    cfg = fixture_config()
    return analyze(graph_from_html_files(cfg), entry_points(cfg), max_depth=8)


def test_headline_reads_plainly():
    text = headline(fixture_analysis(), "Contact sales form")
    assert text.startswith("Following content links only, both entry links reach the Contact sales form in 1–3 clicks.")
    assert "Counting site navigation too, both entry links reach the Contact sales form in 1 click." in text
    assert "4 of 9 crawled pages (44.4%) are dead ends" in text and "1 trap loop." in text


def test_mermaid_marks_entries_wins_and_dead_zones():
    analysis = fixture_analysis()
    mmd = mermaid_paths(analysis, make_short([FIXTURE_BASE + "x.html"]))
    assert mmd.startswith("flowchart LR")
    assert '["/win.html"]' in mmd and '["/trap-a.html"]' in mmd
    assert mmd.count("class ") == 5  # 2 entries, 1 win, 2 dead-zone targets
    lines = [line.strip() for line in mmd.splitlines()]
    # a hop drawn on the main path is not drawn again as a dashed dead-zone hop
    for line in lines:
        if "-.->" in line:
            a, _, b = line.split()
            assert f"{a} --> {b}" not in lines


def fixture_run(tmp_path):
    """A run directory built from the fixture HTML (as if crawled), for report tests."""
    cfg = fixture_config()
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
    store = Store(tmp_path / "crawl.db")
    store.set_meta(client="Fixture", campaign_id="fixture", campaign_name="Fixture campaign", status="complete")
    for i, e in enumerate(cfg.campaigns[0].entry_links):
        store.add_entry(i, e.label, e.url, cfg.scope.normalize(e.url))
    g = graph_from_html_files(cfg)
    for n, d in g.nodes(data=True):
        links = [LinkRecord(v, v, "", r, True) for _, v, dd in g.out_edges(n, data=True) for r in dd["regions"]]
        status = "ok" if d["explored"] else "not_fetched"  # the win is recorded, not loaded
        store.save_page(PageRecord(url=n, status=status, depth=0, win=d["win"], win_source=d["win_source"],
                                   headings=[(1, "H")], jsonld_types=[]), links)
    store.close()
    return tmp_path


def test_write_report_files(tmp_path):
    run = open_run(fixture_run(tmp_path))
    paths = write_report(run)
    run.close()
    assert all(p.exists() for p in paths.values())

    data = json.loads(paths["report.json"].read_text())
    assert data["headline"].startswith("Following content links only, both entry links")
    assert data["categories"]["pages_loaded"] == 9
    assert data["win_pages"] == [{
        "url": FIXTURE_BASE + "win.html", "win_type": "Contact sales", "win_source": "pattern", "status": "not_fetched", "fetched": False,
        "not_fetched_reason": "the crawl stops at the win, so it is not loaded", "form_present": None,
        "linked_from": 8,
    }]
    assert data["win_near_misses"] == []
    assert data["categories"]["reach"]["content_only"]["trap loop"] == 3
    assert "dead end" in data["definitions"]

    md = paths["report.md"].read_text()
    for heading in ("## Entry links", "## Dead zones", "## Convergence", "## Operator dependency",
                    "## Site map: page categories", "## Metric definitions", "```mermaid"):
        assert heading in md
    assert "win page not fetched: the crawl stops at the win, so it is not loaded" in md
    assert "look like the win" not in md

    g = nx.read_graphml(paths["graph.graphml"])
    assert len(g) == 10
    win = g.nodes[FIXTURE_BASE + "win.html"]
    assert win["win"] is True and win["page_type"] == "win" and win["clicks_to_win_content_only"] == 0
    assert g.edges[FIXTURE_BASE + "trap-a.html", FIXTURE_BASE + "win.html"]["content_link"] is False

    csv_lines = paths["categories.csv"].read_text().splitlines()
    assert len(csv_lines) == 11 and csv_lines[0].startswith("url,host,section,page_type")

    full = nx.read_gexf(paths["graph.gexf"])
    assert len(full) == 10 and full.number_of_edges() == g.number_of_edges()
    roles = {n: d["role"] for n, d in full.nodes(data=True)}
    assert roles[FIXTURE_BASE + "win.html"] == "win" and roles[FIXTURE_BASE + "entry-near.html"] == "entry"
    assert roles[FIXTURE_BASE + "trap-a.html"] == "crawled"
    for _, d in full.nodes(data=True):
        assert {"position", "size", "color"} <= set(d["viz"])
    positions = {(d["viz"]["position"]["x"], d["viz"]["position"]["y"]) for _, d in full.nodes(data=True)}
    assert len(positions) == len(full)  # laid out, not stacked on one point
    # the most-linked page is drawn biggest: the win, which every page links to
    assert max(full.nodes, key=lambda n: full.nodes[n]["viz"]["size"]) == FIXTURE_BASE + "win.html"
    assert full.nodes[FIXTURE_BASE + "win.html"]["label"]  # labelled
    assert full.edges[FIXTURE_BASE + "trap-a.html", FIXTURE_BASE + "win.html"]["content_link"] is False
    assert full.edges[FIXTURE_BASE + "trap-a.html", FIXTURE_BASE + "win.html"]["region"] == "nav"

    content = nx.read_gexf(paths["graph_content_only.gexf"])
    assert all(d["content_link"] for _, _, d in content.edges(data=True))
    assert FIXTURE_BASE + "about.html" not in content  # only nav/footer links reach it
    assert content.number_of_edges() < full.number_of_edges()


def test_open_run_applies_saved_overrides(tmp_path):
    run_dir = fixture_run(tmp_path)
    s = Store(run_dir / "crawl.db")
    s.set_meta(crawl_overrides={"max_depth": 2})
    s.close()
    run = open_run(run_dir)
    assert run.config.crawl.max_depth == 2
    far = next(e for e in run.analyze().modes["content_only"].entries if e.label == "far")
    assert far.longest_path is None  # 3 clicks needed, only 2 allowed
    run.close()


def test_open_run_rejects_non_run_dirs(tmp_path):
    with pytest.raises(RunError):
        open_run(tmp_path)


def test_scoring_stub_explains_itself():
    step = JourneyStep(url="u", title=None, text="", screenshot_path=None)
    with pytest.raises(NotImplementedError, match="v2"):
        NotConfiguredScorer().score_relevance("ad", step)


def test_fixture_site_exists():
    assert (FIXTURE_SITE / "expected.yaml").exists()


def test_near_miss_win_urls_are_flagged(tmp_path):
    run_dir = fixture_run(tmp_path)
    s = Store(run_dir / "crawl.db")
    s.save_page(PageRecord(url=FIXTURE_BASE + "entry-far.html", status="ok", depth=0, headings=[(1, "H")], jsonld_types=[]),
                [LinkRecord("x", FIXTURE_BASE + "win-2023.html", "", "body", True)])
    s.close()
    run = open_run(run_dir)
    paths = write_report(run)
    run.close()
    data = json.loads(paths["report.json"].read_text())
    assert data["win_near_misses"] == [{"url": FIXTURE_BASE + "win-2023.html", "linked_from": 1}]
    md = paths["report.md"].read_text()
    assert "1 page looks like the win (URL contains 'win') but matches no win pattern" in md
    assert FIXTURE_BASE + "win-2023.html" in md
