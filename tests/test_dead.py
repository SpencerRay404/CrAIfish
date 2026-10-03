"""Change 5: dead pages (404, 410, soft 404) and the links still pointing at them."""

from __future__ import annotations

import csv
import json

import networkx as nx
import pytest
import yaml
from typer.testing import CliRunner

from pathcrawl.cli import app
from pathcrawl.config import parse_config
from pathcrawl.dead import backfill_dead, dead_annotations, dead_links
from pathcrawl.extract import detect_dead
from pathcrawl.store import LinkRecord, PageRecord, Store

A = "https://about.x.test/"
W = "https://www.x.test/"
LI = "https://www.linkedin.com/pulse/lean-inventory"


@pytest.mark.parametrize(("status", "title", "text", "reason"), [
    (404, "Anything", "", "HTTP 404"),
    (410, "Gone", "", "HTTP 410"),
    (200, "404 | About X", "", "title starts with 404"),
    (200, "Page Not Found | X", "", "title says 'page not found'"),
    (200, "Our story", "Sorry, this page no longer exists. Try search.", "page says 'this page no longer exists'"),
    (200, "Inventory", "How to manage inventory", None),
    (500, "Error", "", None),
])
def test_detect_dead(status, title, text, reason):
    assert detect_dead(status, title, text) == reason


def build_run(run_dir, with_dead_columns=True):
    cfg = parse_config({
        "client": {"name": "X", "slug": "xco"},
        "scope": {"allowed_domains": ["www.x.test", "about.x.test"]},
        "win": {"name": "form", "url_patterns": [W + "talk*"]},
        "campaigns": [{"id": "c", "name": "C", "platform": "LinkedIn", "ad_copy": "a",
                       "entry_links": [{"label": "home", "url": W + "home"}]}],
    })
    (run_dir / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
    s = Store(run_dir / "crawl.db")
    s.set_meta(status="complete", campaign_id="c")
    s.add_entry(0, "home", W + "home", W + "home")
    pages = [
        (W + "home", 200, "Home", "Welcome", [(A + "gone-story", "body", "ONLINE_X_1"), (A + "soft", "footer", None),
                                               (W + "talk", "body", None)]),
        (W + "news", 200, "News", "News", [(A + "gone-story", "body", None), (A + "gone-story", "nav", None)]),
        (A + "gone-story", 404, "404 | About X", "Sorry, this page no longer exists.", [(W + "home", "nav", None)]),
        (A + "soft", 200, "Page Not Found | X", "We could not find that.", []),
        (W + "orphan-gone", 410, "Gone", "", []),
    ]
    for url, status, title, text, links in pages:
        s.save_page(PageRecord(url=url, status="ok" if status < 400 else "http_error", http_status=status, title=title,
                               body_text=text, jsonld_types=[]),
                    [LinkRecord(t, t, "link", region, True, mc_id=tag) for t, region, tag in links])
    # a LinkedIn post whose landing page is gone
    s.save_page(PageRecord(url=LI, status="external", title="Lean inventory"),
                [LinkRecord("https://lnkd.in/z", A + "gone-story", "", "external", True,
                            mc_id="GM_PARTNER_CONTENT_InventoryEquation_2510")])
    if with_dead_columns:
        backfill_dead(s)
    s.close()
    return run_dir


def test_backfill_marks_dead_pages(tmp_path):
    build_run(tmp_path, with_dead_columns=False)
    s = Store(tmp_path / "crawl.db")
    assert s.db.execute("SELECT COUNT(*) FROM pages WHERE is_dead = 1").fetchone()[0] == 0
    assert backfill_dead(s) == 3
    reasons = dict(s.db.execute("SELECT url, dead_reason FROM pages WHERE is_dead = 1").fetchall())
    assert reasons == {A + "gone-story": "HTTP 404", A + "soft": "title says 'page not found'",
                       W + "orphan-gone": "HTTP 410"}
    assert s.db.execute("SELECT is_dead FROM pages WHERE url = ?", (W + "home",)).fetchone()[0] == 0
    assert s.db.execute("SELECT is_dead FROM pages WHERE url = ?", (LI,)).fetchone()[0] is None  # not loaded
    s.close()


def test_dead_links_and_annotations(tmp_path):
    build_run(tmp_path)
    s = Store(tmp_path / "crawl.db")
    links = dead_links(s)
    assert [(lk.src, lk.dead_url, lk.region, lk.mc_id) for lk in links] == [
        (LI, A + "gone-story", "external", "GM_PARTNER_CONTENT_InventoryEquation_2510"),
        (W + "home", A + "gone-story", "body", "ONLINE_X_1"),
        (W + "news", A + "gone-story", "body", ""),
        (W + "news", A + "gone-story", "nav", ""),
        (W + "home", A + "soft", "footer", ""),
    ]
    nodes, edges = dead_annotations(s)
    s.close()
    gone = nodes[A + "gone-story"]
    assert (gone["inbound_dead_links"], gone["dead_inbound_pages"], gone["dead_inbound_body_links"]) == (4, 3, 2)
    assert nodes[W + "orphan-gone"]["inbound_dead_links"] == 0
    assert edges[(LI, A + "gone-story")] == {"to_dead": True}
    assert (W + "home", W + "talk") not in edges


def test_report_dead_pages(tmp_path):
    build_run(tmp_path)
    result = CliRunner().invoke(app, ["report", "--run", str(tmp_path)])
    assert result.exit_code == 0, result.output
    md = (tmp_path / "report.md").read_text()
    section = md[md.index("## Dead pages"):]
    assert "**3 pages no longer exist** (2 on about.x.test, 1 on www.x.test). 5 links still point at them, from 3 " \
           "distinct pages: 2 in the page body, 2 in the nav, header or footer, 1 from external posts." in section
    assert "| about.x.test/gone-story | HTTP 404 |" in section and "| 2 / 1 | GM_PARTNER_CONTENT_InventoryEquation_2510, ONLINE_X_1 |" in section
    assert "| /orphan-gone | HTTP 410 | (nothing crawled links here) | 0 / 0 | - |" in section
    assert "**From external posts:** 1 post link lands on a dead page" in section
    with open(tmp_path / "xco_dead_pages.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 6 and rows[-1]["dead_url"] == W + "orphan-gone" and rows[-1]["src"] == ""
    data = json.loads((tmp_path / "report.json").read_text())
    assert data["dead_pages"]["count"] == 3
    assert data["nodes"][A + "gone-story"]["is_dead"] is True and data["nodes"][W + "home"]["is_dead"] is False
    g = nx.read_gexf(tmp_path / "graph.gexf")
    assert g.nodes[A + "gone-story"]["is_dead"] is True and g.nodes[A + "gone-story"]["inbound_dead_links"] == 4
    assert g.edges[W + "home", A + "gone-story"]["to_dead"] is True
    assert g.edges[W + "home", W + "talk"]["to_dead"] is False


def test_backfill_cli_fills_dead_pages(tmp_path):
    build_run(tmp_path, with_dead_columns=False)
    result = CliRunner().invoke(app, ["backfill-links", "--run", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Dead pages: 3." in result.output


def test_a_dead_landing_page_is_not_made_an_entry(tmp_path):
    from pathcrawl.seeds import plan

    build_run(tmp_path)
    cfg = parse_config(yaml.safe_load((tmp_path / "config.yaml").read_text()))
    rows = [{"seed_url": "https://www.linkedin.com/pulse/other/", "outbound_url_raw": "x",
             "outbound_resolved_url": A + "gone-story"}]
    p = plan(rows, cfg.scope, [], [W + "home"], cfg.win, dead={A + "gone-story"})
    assert p.new_entries == [] and p.seeds[0].links[0].target == A + "gone-story"



def test_links_from_dead_pages_are_not_counted(tmp_path):
    """A dead page's own menu linking to another dead page doesn't count."""
    build_run(tmp_path)
    s = Store(tmp_path / "crawl.db")
    # the dead story page links (nav) to the soft-404 page
    s.db.execute("INSERT INTO links(src, href, url, text, region, in_scope) VALUES (?, ?, ?, 'x', 'nav', 1)",
                 (A + "gone-story", A + "soft", A + "soft"))
    s.db.commit()
    nodes, _ = dead_annotations(s)
    assert nodes[A + "soft"]["inbound_dead_links"] == 1  # only home's footer link
    assert all(lk.src != A + "gone-story" for lk in dead_links(s))
    s.close()


def test_sections_are_stored_in_the_database(tmp_path):
    build_run(tmp_path)
    CliRunner().invoke(app, ["report", "--run", str(tmp_path)])
    s = Store(tmp_path / "crawl.db")
    row = s.db.execute("SELECT section, page_type FROM pages WHERE url = ?", (W + "news",)).fetchone()
    s.close()
    assert row["section"] == "news" and row["page_type"]
