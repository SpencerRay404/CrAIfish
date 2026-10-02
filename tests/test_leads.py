"""Change 2: lead counts per campaign tag joined to the links carrying the tag.

Site (win: talk-v4.html; a second consultation URL talk-2023.html):

    retail   --body  TAG_RETAIL_123456 -->  talk-v4     (only page with this tag: exact)
    whole    --body  TAG_SHARED        -->  talk-v4     (two pages carry TAG_SHARED: shared)
    auto     --nav   TAG_SHARED        -->  talk-v4
    auto     --body  TAG_OLD           -->  talk-2023   (only page with it: exact; converted on v4 instead)
    support  --body  (no tag)          -->  talk-v4     (links to the form, untagged)
    news     --body  TAG_NOLEADS       -->  talk-v4     (tag with no leads)

Lead file:
    TAG_RETAIL_654321  12  (fallback: same tag with a different numeric suffix)
    TAG_SHARED         10  (split 5 / 5)
    TAG_OLD             8  (converted on v4, not where the link points)
    TAG_AD_ONLY         6  (no crawled link carries it)
    TAG_TINY            2  (no crawled link; under min_cell)
"""

from __future__ import annotations

import csv
import json

import networkx as nx
import pytest
import yaml
from typer.testing import CliRunner

from pathcrawl.cli import app
from pathcrawl.config import parse_config
from pathcrawl.leads import LeadFileError, attribute, load_lead_tags
from pathcrawl.normalize import normalize_conversion_url
from pathcrawl.run import open_run
from pathcrawl.store import LinkRecord, PageRecord, Store

B = "https://www.x.test/"
WIN = "https://solutions.x.test/talk-v4.html"
OLD = "https://solutions.x.test/talk-2023.html"

LINKS = {
    "retail": [(WIN, "body", "TAG_RETAIL_123456")],
    "whole": [(WIN, "body", "TAG_SHARED")],
    "auto": [(WIN, "nav", "TAG_SHARED"), (OLD, "body", "TAG_OLD")],
    "support": [(WIN, "body", None)],
    "news": [(WIN, "body", "TAG_NOLEADS")],
}
LEAD_ROWS = [
    # wt_mc_id, leads_most_recent_tag, leads_source_initiative_tag, paid_click_leads, main_conversion_page
    ("TAG_RETAIL_654321", 12, 11, 2, "https://Solutions.x.test/Talk-V4.html?x=1"),
    ("TAG_SHARED", 10, 9, 0, "https://solutions.x.test/talk-v4.html"),
    ("TAG_OLD", 8, 8, 1, "https://solutions.x.test/talk-v4.html"),
    ("TAG_AD_ONLY", 6, 0, 6, "null"),
    ("TAG_TINY", 2, 0, 0, "https://solutions.x.test/lpeditor/devicePreview/1"),
]


def write_leads(path, rows=LEAD_ROWS, header=None):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header or ["wt_mc_id", "leads_most_recent_tag", "leads_source_initiative_tag",
                              "paid_click_leads", "main_conversion_page"])
        w.writerows(rows)
    return path


def make_config(lead_file=None):
    return parse_config({
        "client": {"name": "X", "slug": "xco"},
        "scope": {"allowed_domains": ["www.x.test", "solutions.x.test"], "capture_params": ["WT.mc_id"],
                  "strip_query_params": ["WT.*"]},
        "win": {"name": "consultation form", "url_patterns": ["https://solutions.x.test/talk-v4*"]},
        "campaigns": [{"id": "c", "name": "C", "platform": "p", "ad_copy": "a",
                       "entry_links": [{"label": "retail", "url": B + "retail"}]}],
        "leads": {"files": [{"path": str(lead_file)}]} if lead_file else {},
    })


def build_run(run_dir, lead_file=None):
    cfg = make_config(lead_file)
    (run_dir / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
    s = Store(run_dir / "crawl.db")
    s.set_meta(status="complete", campaign_id="c")
    s.add_entry(0, "retail", B + "retail", B + "retail")
    for name, links in LINKS.items():
        s.save_page(PageRecord(url=B + name, status="ok", depth=1, title=name, jsonld_types=[]), [
            LinkRecord(f"{t}?WT.mc_id={tag}" if tag else t, t, "Talk", region, True, mc_id=tag)
            for t, region, tag in links
        ])
    s.save_page(PageRecord(url=WIN, status="robots", win=True, win_source="pattern"), [])
    s.close()
    return run_dir


@pytest.fixture
def lead_file(tmp_path):
    return write_leads(tmp_path / "lead_tags.csv")


def test_normalize_conversion_url():
    assert normalize_conversion_url("https://Solutions.UPS.com/SBR-Signup-USSP-Page.html?x=1#f") == \
        "https://solutions.ups.com/sbr-signup-ussp-page.html"
    assert normalize_conversion_url("solutions.ups.com/a.html") == "https://solutions.ups.com/a.html"
    for dropped in ("null", "", None, "https://solutions.ups.com/lpeditor/devicePreview/12"):
        assert normalize_conversion_url(dropped) is None


def test_load_lead_tags(lead_file):
    tags = load_lead_tags([lead_file])
    assert sorted(tags) == ["TAG_AD_ONLY", "TAG_OLD", "TAG_RETAIL_654321", "TAG_SHARED", "TAG_TINY"]
    t = tags["TAG_RETAIL_654321"]
    assert (t.leads, t.initiative_leads, t.paid_click_leads) == (12, 11, 2)
    assert t.main_conversion_page == "https://solutions.x.test/talk-v4.html"
    assert tags["TAG_AD_ONLY"].main_conversion_page is None and tags["TAG_TINY"].main_conversion_page is None


@pytest.mark.parametrize("header", [
    ["wt_mc_id", "leads_most_recent_tag", "MKT_TRK"],
    ["wt_mc_id", "leads_most_recent_tag", "Email Address"],
])
def test_raw_exports_are_refused(tmp_path, header):
    path = write_leads(tmp_path / "raw.csv", [("T", 1, "x")], header)
    with pytest.raises(LeadFileError, match="raw lead export"):
        load_lead_tags([path])


def test_visitor_tokens_are_refused(tmp_path):
    path = write_leads(tmp_path / "t.csv", [("T", 1, 0, 0, "token:abc")])
    with pytest.raises(LeadFileError, match="tokens"):
        load_lead_tags([path])


def test_missing_columns_and_bad_numbers(tmp_path):
    with pytest.raises(LeadFileError, match="missing column"):
        load_lead_tags([write_leads(tmp_path / "a.csv", [("T",)], ["wt_mc_id"])])
    with pytest.raises(LeadFileError, match="not a number"):
        load_lead_tags([write_leads(tmp_path / "b.csv", [("T", "many", 0, 0, "")])])


def test_allocation(tmp_path, lead_file):
    build_run(tmp_path)
    s = Store(tmp_path / "crawl.db")
    result = attribute(s, load_lead_tags([lead_file]))
    s.close()
    rows = {(r.src.removeprefix(B), r.lead_tag): r for r in result.rows}
    assert sorted(rows) == [("auto", "TAG_OLD"), ("auto", "TAG_SHARED"), ("retail", "TAG_RETAIL_654321"),
                            ("whole", "TAG_SHARED")]

    # single source page (via the numeric-suffix fallback): all 12 leads, exact
    r = rows[("retail", "TAG_RETAIL_654321")]
    assert (r.mc_id, r.join, r.leads_allocated, r.attribution, r.tag_source_pages) == \
        ("TAG_RETAIL_123456", "fallback", 12, "exact", 1)
    assert r.targets == WIN and r.region == "body"
    # shared tag: 10 leads split over two pages
    for page in ("whole", "auto"):
        r = rows[(page, "TAG_SHARED")]
        assert (r.join, r.leads_allocated, r.attribution, r.tag_source_pages, r.tag_leads_total) == \
            ("exact", 5, "shared", 2, 10)
    assert rows[("auto", "TAG_SHARED")].region == "nav"
    assert sum(r.leads_allocated for r in result.rows) == 12 + 10 + 8

    tags = {t.lead_tag: t for t in result.tags}
    # tag not in the crawl: kept, with no rows
    assert tags["TAG_AD_ONLY"].join is None and tags["TAG_AD_ONLY"].source_pages == 0
    # landed on a different page than the tagged link points at: kept, flagged
    assert tags["TAG_OLD"].lands_on_target is False and tags["TAG_OLD"].targets == [OLD]
    assert tags["TAG_SHARED"].lands_on_target is True
    assert tags["TAG_RETAIL_654321"].lands_on_target is True  # case-insensitive page match


def test_fallback_can_be_turned_off(tmp_path, lead_file):
    build_run(tmp_path)
    s = Store(tmp_path / "crawl.db")
    result = attribute(s, load_lead_tags([lead_file]), fallback=None)
    s.close()
    assert {t.lead_tag: t.join for t in result.tags}["TAG_RETAIL_654321"] is None


def test_leads_cli_and_report(tmp_path, lead_file):
    run = tmp_path / "run"
    run.mkdir()
    build_run(run)
    cfg_path = tmp_path / "live.yaml"
    cfg_path.write_text(yaml.safe_dump(make_config(lead_file).model_dump()))

    result = CliRunner().invoke(app, ["leads", "--run", str(run), "--config", str(cfg_path)])
    assert result.exit_code == 0, result.output
    assert "5 tags, 3 joined to crawled links (2 exact), covering 30 of 38 leads; 3 pages" in result.output
    for tag in ("TAG_AD_ONLY", "TAG_TINY"):
        assert tag not in result.output  # no lead-file rows in logs

    with open(run / "xco_lead_attribution.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4 and rows[0]["leads_allocated"] == "12.0"

    result = CliRunner().invoke(app, ["report", "--run", str(run), "--config", str(cfg_path)])
    assert result.exit_code == 0, result.output
    md = (run / "report.md").read_text()
    section = md[md.index("## Lead evidence"):]
    assert "5 tags in the lead file" in section
    assert "30 of 38 tagged leads (79%)" in section
    assert "Leads with a paid click ID: 9 (24%)" in section
    assert "**3 pages carry allocated leads** (30 in total); 2 of them hold exact leads (20, 67%" in section
    assert "| /retail | 12 | 12 | 0 |" in section
    assert "TAG_OLD (8 leads): links point at" in section
    assert "TAG_AD_ONLY (6)" in section
    assert "TAG_TINY" not in md and "(other, <5 leads) ×1 (2)" in section  # rolled up below min_cell
    assert ("- **Crawled pages linking to a win page:** 5; 4 carry a tag on that link. Without a tag: /support. "
            "Tags on those links: 3; with leads: 2, without leads in the lead file: 1.") in section

    data = json.loads((run / "report.json").read_text())
    assert data["nodes"][B + "retail"]["leads_origin"] == 12 and data["nodes"][B + "retail"]["leads_exact"] == 12
    auto = data["nodes"][B + "auto"]
    assert (auto["leads_origin"], auto["leads_exact"], auto["leads_landed"]) == (13.0, 8.0, 0.0)
    assert data["nodes"][WIN]["leads_landed"] == 30  # 12 + 10 + 8 converted on v4
    assert {"src": B + "whole", "dst": WIN, "leads": 5.0} in data["edges"]
    assert data["leads"]["leads_total"] == 38 and "tags" not in data["leads"]

    g = nx.read_gexf(run / "graph.gexf")
    assert g.nodes[B + "retail"]["leads_origin"] == 12.0
    assert g.nodes[B + "news"]["leads_origin"] == 0.0
    assert g.edges[B + "retail", WIN]["leads"] == 12.0
    assert g.edges[B + "news", WIN]["leads"] == 0.0


def test_report_runs_the_join_when_lead_files_are_configured(tmp_path, lead_file):
    run = tmp_path / "run"
    run.mkdir()
    build_run(run, lead_file)
    result = CliRunner().invoke(app, ["report", "--run", str(run)])
    assert result.exit_code == 0, result.output
    assert "## Lead evidence" in (run / "report.md").read_text()


def test_report_skips_a_missing_lead_file(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    build_run(run, tmp_path / "nowhere.csv")
    result = CliRunner().invoke(app, ["report", "--run", str(run)])
    assert result.exit_code == 0, result.output
    assert "lead join skipped" in result.output
    assert "## Lead evidence" not in (run / "report.md").read_text()


def test_leads_cli_needs_lead_files(tmp_path):
    build_run(tmp_path)
    result = CliRunner().invoke(app, ["leads", "--run", str(tmp_path)])
    assert result.exit_code == 2 and "no leads.files" in result.output


def test_ups_config_leads_section():
    from pathlib import Path

    from pathcrawl.config import load_config

    c = load_config(Path(__file__).parent.parent / "configs" / "ups.yaml")
    assert [f.path for f in c.leads.files] == ["data/ups/ups_lead_tags.csv"]
    assert c.leads.join.fallback == "strip_numeric_suffix" and c.leads.min_cell == 5


def test_lead_data_is_ignored_by_git():
    from pathlib import Path

    text = (Path(__file__).parent.parent / ".gitignore").read_text()
    assert "data/**/raw*" in text and "*MKT_TRK*" in text
