"""Change 4: posts collected by hand (LinkedIn) as external entry points."""

from __future__ import annotations

import csv
import json

import networkx as nx
import yaml
from typer.testing import CliRunner

from pathcrawl.cli import app
from pathcrawl.config import LeadFile, parse_config
from pathcrawl.graph import graph_from_store
from pathcrawl.seeds import ingest, is_post, normalize_seed_url, plan, post_date_derived, read_rows
from pathcrawl.store import PageRecord, Store

W = "https://www.x.test/"
S = "https://solutions.x.test/"
LI = "https://www.linkedin.com/"
AD = LI + "pulse/already-an-ad-abc/"
POST_A = LI + "posts/acme_luxury-activity-7312345678901234567-AbCd?utm_source=share&rcm=x"
POST_B = LI + "pulse/consumer-trends-xyz/"
POST_C = LI + "feed/update/urn:li:activity:7380000000000000000/"

ROWS = [
    # an ad URL already in the campaign: skipped
    {"seed_url": AD, "outbound_url_raw": "https://lnkd.in/a", "outbound_resolved_url": W + "us/en/new-1"},
    # post A has two outbound links (both kept) and repeats one of them (dropped)
    {"seed_url": POST_A, "post_title": "Luxury spending", "outbound_url_raw": "https://lnkd.in/b",
     "outbound_resolved_url": W + "us/en/new-1?WT.mc_id=GM_PARTNER_CONTENT_Roundtable_2509", "link_order": "1"},
    {"seed_url": POST_A, "outbound_url_raw": "https://lnkd.in/c",
     "outbound_resolved_url": W + "us/en/entry/?trackingId=1", "link_order": "2"},
    {"seed_url": POST_A + "#comments", "outbound_url_raw": "https://lnkd.in/b",
     "outbound_resolved_url": W + "us/en/new-1?WT.mc_id=GM_PARTNER_CONTENT_Roundtable_2509"},
    # post B lands on a gated conversion page (a win) and links to post C
    {"seed_url": POST_B, "post_title": "Consumer trends", "outbound_url_raw": "https://spr.ly/q",
     "outbound_resolved_url": S + "consumer-trends-page.html?WT.mc_id=TK_HIE_DIR_ConsumerTrends_2603"},
    {"seed_url": POST_B, "outbound_url_raw": "https://lnkd.in/d", "outbound_resolved_url": POST_C},
    # an empty row: the post is kept, with no landing page
    {"seed_url": LI + "pulse/no-link-post/", "post_title": "No link", "outbound_url_raw": "", "outbound_resolved_url": ""},
    # a landing page outside the allowed domains
    {"seed_url": LI + "pulse/offsite/", "outbound_url_raw": "https://other.test/x", "outbound_resolved_url": ""},
]


def write_rows(path, rows=ROWS):
    fields = ["seed_url", "post_title", "outbound_url_raw", "anchor_text", "link_order", "outbound_resolved_url",
              "commenter_name"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({**r, "commenter_name": "Someone"})
    return path


def config(seed_file=None):
    return parse_config({
        "client": {"name": "X", "slug": "xco"},
        "scope": {"allowed_domains": ["www.x.test", "solutions.x.test"], "locale_include": ["/us/en/"],
                  "locale_hosts": ["www.x.test"], "strip_query_params": ["WT.*", "utm_*", "trackingId"],
                  "capture_params": ["WT.mc_id"]},
        "win": {"name": "form", "url_patterns": [S + "talk*"],
                "known_pages": [{"url": S + "consumer-trends-page.html", "type": "White papers & reports"}]},
        "campaigns": [{"id": "li", "name": "LI", "platform": "LinkedIn", "ad_copy": "a", "ad_urls": [AD],
                       "entry_links": [{"label": "entry", "url": W + "us/en/entry/"}],
                       **({"external_seeds": str(seed_file)} if seed_file else {})}],
    })


def test_helpers():
    assert normalize_seed_url(POST_A) == LI + "posts/acme_luxury-activity-7312345678901234567-AbCd"
    assert normalize_seed_url("https://WWW.LinkedIn.com/pulse/x/?trackingId=1#a") == LI + "pulse/x"
    assert normalize_seed_url("") is None
    assert is_post(POST_B) and is_post("https://lnkd.in.linkedin.com/x") and not is_post(W)
    assert post_date_derived(POST_A) == "2025-03-31"
    assert post_date_derived(POST_C) == "2025-10-03"
    assert post_date_derived(POST_B) is None


def test_plan_dedupes_and_classifies(tmp_path):
    c = config()
    p = plan(read_rows(write_rows(tmp_path / "s.csv")), c.scope, c.campaigns[0].ad_urls,
             [W + "us/en/entry/"], c.win)
    seeds = {s.url: s for s in p.seeds}
    assert p.skipped_seeds == [normalize_seed_url(AD)]
    assert p.duplicate_rows == 1
    a = seeds[normalize_seed_url(POST_A)]
    assert a.title == "Luxury spending" and a.date == "2025-03-31"
    assert [(lk.target, lk.kind, lk.mc_id) for lk in a.links] == [
        (W + "us/en/new-1", "page", "GM_PARTNER_CONTENT_Roundtable_2509"),
        (W + "us/en/entry/", "page", None),
    ]
    b = seeds[normalize_seed_url(POST_B)]
    assert [(lk.kind, lk.mc_id) for lk in b.links] == [("page", "TK_HIE_DIR_ConsumerTrends_2603"), ("seed", None)]
    assert normalize_seed_url(POST_C) in seeds  # a post linked from a post becomes a seed
    assert seeds[normalize_seed_url(POST_C)].date == "2025-10-03"
    assert seeds[LI + "pulse/no-link-post"].links[0].kind == "none"
    assert seeds[LI + "pulse/offsite"].links[0].kind == "offsite"
    # new-1 becomes an entry; entry/ already is one; the win is not made an entry
    assert p.new_entries == [W + "us/en/new-1"]
    assert p.existing_entries == [W + "us/en/entry/"]


def build_run(run_dir, seed_file):
    cfg = config(seed_file)
    (run_dir / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
    s = Store(run_dir / "crawl.db")
    s.set_meta(status="complete", campaign_id="li")
    s.add_entry(0, "entry", W + "us/en/entry/", W + "us/en/entry/")
    s.save_page(PageRecord(url=W + "us/en/entry/", status="ok", title="Entry", jsonld_types=[]), [])
    s.close()
    return cfg


def test_ingest_writes_external_nodes_and_entries(tmp_path):
    seed_file = write_rows(tmp_path / "s.csv")
    cfg = build_run(tmp_path, seed_file)
    s = Store(tmp_path / "crawl.db")
    ingest(s, cfg, cfg.campaigns[0], seed_file)
    ingest(s, cfg, cfg.campaigns[0], seed_file)  # idempotent
    a = normalize_seed_url(POST_A)
    row = s.db.execute("SELECT * FROM pages WHERE url = ?", (a,)).fetchone()
    assert (row["status"], row["channel"], row["post_date_derived"], row["title"]) == \
        ("external", "LinkedIn", "2025-03-31", "Luxury spending")
    assert [r["mc_id"] for r in s.db.execute("SELECT mc_id FROM links WHERE src = ? ORDER BY id", (a,))] == \
        ["GM_PARTNER_CONTENT_Roundtable_2509", None]
    assert [e["node_url"] for e in s.entries()] == [W + "us/en/entry/", W + "us/en/new-1"]
    assert s.db.execute("SELECT depth FROM queue WHERE url = ?", (W + "us/en/new-1",)).fetchone()[0] == 0
    # nothing beyond the post URL, title, date and links is stored
    assert "Someone" not in json.dumps([dict(r) for r in s.db.execute("SELECT * FROM pages")])

    g, _ = graph_from_store(s, cfg.win)
    s.close()
    assert g.nodes[a]["external"] is True and g.nodes[a]["explored"] is False
    assert g.has_edge(a, W + "us/en/new-1") and g.has_edge(a, W + "us/en/entry/")
    assert g.has_edge(normalize_seed_url(POST_B), S + "consumer-trends-page.html")
    assert g.nodes[S + "consumer-trends-page.html"]["win"]


def test_cli_report_and_lead_join(tmp_path):
    seed_file = write_rows(tmp_path / "s.csv")
    run = tmp_path / "run"
    run.mkdir()
    build_run(run, seed_file)
    result = CliRunner().invoke(app, ["external-seeds", "--run", str(run)])
    assert result.exit_code == 0, result.output
    assert "1 new entry links queued" in result.output and "pathcrawl crawl --resume" in result.output

    leads = tmp_path / "leads.csv"
    leads.write_text("wt_mc_id,leads_most_recent_tag\nTK_HIE_DIR_ConsumerTrends_2603,1\n")
    cfg = config(seed_file)
    cfg.leads.files = [LeadFile(path=str(leads))]
    live = tmp_path / "live.yaml"
    live.write_text(yaml.safe_dump(cfg.model_dump()))
    result = CliRunner().invoke(app, ["report", "--run", str(run), "--config", str(live)])
    assert result.exit_code == 0, result.output
    md = (run / "report.md").read_text()
    assert "## External entry points" in md
    assert "| Consumer trends | - | solutions.x.test/consumer-trends-page.html (win) | TK_HIE_DIR_ConsumerTrends_2603 |" in md
    assert "| No link | - | (no landing page) | - |" in md
    assert "(outside the crawl)" in md
    data = json.loads((run / "report.json").read_text())
    assert data["nodes"][normalize_seed_url(POST_B)]["leads_origin"] == 1.0  # the post's tag joins to leads
    gx = nx.read_gexf(run / "graph.gexf")
    assert gx.nodes[normalize_seed_url(POST_A)]["role"] == "external"
    with open(run / "categories.csv", newline="") as f:
        assert not any("linkedin.com" in r["url"] for r in csv.DictReader(f))


def test_ups_config_points_at_the_scrape_file():
    from pathlib import Path

    from pathcrawl.config import load_config

    c = load_config(Path(__file__).parent.parent / "configs" / "ups.yaml")
    assert c.campaign("linkedin-articles").external_seeds == "data/ups/linkedin_scrape.csv"
    template = Path(__file__).parent.parent / "data" / "templates" / "linkedin_scrape_template.csv"
    assert template.read_text().startswith("seed_url,post_title,outbound_url_raw,anchor_text,link_order,outbound_resolved_url")
