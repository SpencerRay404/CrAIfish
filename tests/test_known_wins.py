"""Change 3: win pages listed by URL (known_pages) with a conversion type,
exclusions, and case-insensitive matching."""

from __future__ import annotations

import json

import pytest
import yaml
from typer.testing import CliRunner

from pathcrawl.cli import app
from pathcrawl.config import ConfigError, config_warnings, load_config, parse_config
from pathcrawl.graph import analyze, graph_from_store
from pathcrawl.store import LinkRecord, PageRecord, Store

S = "https://solutions.x.test/"
B = "https://www.x.test/"


def win_config(**win):
    return parse_config({
        "client": {"name": "X", "slug": "xco"},
        "scope": {"allowed_domains": ["www.x.test", "solutions.x.test"]},
        "win": {
            "name": "consultation form",
            "url_patterns": [S + "virtual-consultation-us-en*"],
            "known_pages": [
                {"url": S + "virtual-consultation-us-en-v4.html", "type": "Virtual consultation"},
                {"url": S + "SBR-Signup-ussp-page.html", "type": "White papers & reports"},
                {"url": S + "unlinked-ussp-page.html", "type": "White papers & reports"},
            ],
            "exclude_patterns": [S + "lpeditor/*"],
            "match": "case_insensitive_path",
            **win,
        },
        "campaigns": [{"id": "c", "name": "C", "platform": "p", "ad_copy": "a",
                       "entry_links": [{"label": "a", "url": B + "a"}]}],
    })


def test_known_pages_match_case_insensitively_and_ignore_the_query():
    w = win_config().win
    assert w.url_matches(S + "sbr-signup-ussp-page.html")
    assert w.url_matches(S + "SBR-SIGNUP-USSP-PAGE.html?x=1")
    assert w.win_type(S + "sbr-signup-ussp-page.html") == "White papers & reports"
    assert w.win_type(S + "virtual-consultation-us-en-v4.html") == "Virtual consultation"
    assert w.win_type(S + "virtual-consultation-us-en-v5.html") == "consultation form"  # pattern only
    assert w.url_matches(S + "Virtual-Consultation-US-EN-v5.html")  # case-insensitive pattern
    assert w.win_type(B + "a") is None


def test_exact_match_mode_keeps_case():
    w = win_config(match="exact").win
    assert w.url_matches(S + "SBR-Signup-ussp-page.html")
    assert not w.url_matches(S + "sbr-signup-ussp-page.html")
    assert not w.url_matches(S + "Virtual-Consultation-US-EN-v5.html")


def test_exclude_patterns_win_over_everything():
    w = win_config(url_patterns=[S + "*"]).win
    assert w.url_matches(S + "anything.html")
    assert not w.url_matches(S + "lpeditor/devicePreview/1")
    assert not w.near_miss(S + "lpeditor/virtual-consultation-preview")


def test_known_page_validation_and_warnings():
    with pytest.raises(ConfigError, match="not an absolute http"):
        win_config(known_pages=[{"url": "/relative.html", "type": "t"}])
    c = win_config(known_pages=[{"url": "https://other.test/form", "type": "t"},
                                {"url": S + "lpeditor/x", "type": "t"}])
    warnings = " ".join(config_warnings(c))
    assert "not on an allowed domain" in warnings and "matches win.exclude_patterns" in warnings


def run_dir(tmp_path):
    cfg = win_config()
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
    s = Store(tmp_path / "crawl.db")
    s.set_meta(status="complete")
    s.add_entry(0, "a", B + "a", B + "a")
    s.save_page(PageRecord(url=B + "a", status="ok", title="A", jsonld_types=[]), [
        LinkRecord("x", S + "sbr-signup-ussp-page.html", "Sign up", "body", True),   # lower-case spelling
        LinkRecord("y", S + "virtual-consultation-us-en-v4.html", "Talk", "body", True),
        LinkRecord("z", S + "lpeditor/devicePreview/1", "Preview", "body", True),
    ])
    s.save_page(PageRecord(url=S + "sbr-signup-ussp-page.html", status="robots"), [])
    s.close()
    return tmp_path


def test_graph_marks_known_wins_and_adds_unlinked_ones(tmp_path):
    s = Store(run_dir(tmp_path) / "crawl.db")
    g, entries = graph_from_store(s, win_config().win)
    s.close()
    sbr, v4, unlinked = S + "sbr-signup-ussp-page.html", S + "virtual-consultation-us-en-v4.html", S + "unlinked-ussp-page.html"
    assert g.nodes[sbr]["win"] and g.nodes[sbr]["win_source"] == "known"
    assert g.nodes[sbr]["win_type"] == "White papers & reports"
    assert g.nodes[v4]["win_type"] == "Virtual consultation"
    assert g.nodes[unlinked]["win"] and g.in_degree(unlinked) == 0  # added, not fetched, not linked
    assert S + "SBR-Signup-ussp-page.html" not in g  # no duplicate node for the configured spelling
    assert not g.nodes[S + "lpeditor/devicePreview/1"]["win"]
    assert g.nodes[B + "a"]["win_type"] is None
    res = analyze(g, entries, max_depth=8).modes["content_only"]
    assert res.entries[0].shortest_clicks == 1


def test_report_lists_win_types_and_reasons(tmp_path):
    d = run_dir(tmp_path)
    result = CliRunner().invoke(app, ["report", "--run", str(d)])
    assert result.exit_code == 0, result.output
    md = (d / "report.md").read_text()
    assert f"- Win page {S}sbr-signup-ussp-page.html (White papers & reports): win page not fetched: blocked by robots.txt" in md
    assert f"{S}unlinked-ussp-page.html (White papers & reports): win page not fetched: listed in win.known_pages" in md
    data = json.loads((d / "report.json").read_text())
    types = {w["url"]: (w["win_type"], w["win_source"]) for w in data["win_pages"]}
    assert types[S + "virtual-consultation-us-en-v4.html"] == ("Virtual consultation", "known")
    import networkx as nx

    gx = nx.read_gexf(d / "graph.gexf")
    assert gx.nodes[S + "sbr-signup-ussp-page.html"]["win_type"] == "White papers & reports"
    assert gx.nodes[B + "a"]["win_type"] == ""


def test_ups_known_pages():
    from pathlib import Path

    c = load_config(Path(__file__).parent.parent / "configs" / "ups.yaml")
    w = c.win
    assert len(w.known_pages) == 8 and w.match == "case_insensitive_path"
    assert w.win_type("https://solutions.ups.com/SBR-SIGNUP-USSP-PAGE.html") == "White papers & reports"
    assert w.win_type("https://solutions.ups.com/virtual-consultation-us-en-v4.html") == "Virtual consultation"
    assert not w.url_matches("https://solutions.ups.com/lpeditor/devicePreview/abc")
    assert w.url_matches("https://solutions.ups.com/ussp-2605-gc-xxxx-manufacturingwhitepaper-mv_page.html")
