"""Change 6: the website health view."""

from __future__ import annotations

import csv
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import networkx as nx
import yaml
from typer.testing import CliRunner

from pathcrawl.cli import app
from pathcrawl.config import parse_config
from pathcrawl.extract import extract_page
from pathcrawl.health import collect_site_signals, robots_ai_rules, site_bases
from pathcrawl.store import LinkRecord, PageRecord, Store

W = "https://www.x.test/"
HOME = W + "us/en/home"


def test_extract_machine_readability_signals():
    html = """<html><head>
      <title>T</title>
      <meta property="og:title" content="T"><meta property="OG:Image" content="i.png">
      <meta property="article:author" content="x">
      <meta name="ROBOTS" content="NoIndex,  Follow">
      <link rel="alternate" hreflang="en-US" href="/us/en/"><link rel="alternate" hreflang="x-default" href="/">
      <link rel="stylesheet" hreflang="fr" href="/x.css">
    </head><body>
      <div itemscope itemtype="https://schema.org/Product"><span itemprop="name">P</span></div>
      <div itemscope><span itemprop="x">untyped</span></div>
      <div vocab="https://schema.org/" typeof="Organization schema:LocalBusiness">O</div>
    </body></html>"""
    d = extract_page(html, W + "p")
    assert d.og_properties == ["og:image", "og:title"]
    assert d.microdata_types == ["(untyped)", "Product"]
    assert d.rdfa_types == ["LocalBusiness", "Organization"]
    assert d.hreflang == ["en-us", "x-default"]
    assert d.robots_meta == "noindex, follow"
    plain = extract_page("<html><head><title>x</title></head><body>hi</body></html>", W)
    assert (plain.og_properties, plain.microdata_types, plain.rdfa_types, plain.hreflang, plain.robots_meta) == \
        ([], [], [], [], None)


PAGES = {
    # url: (title, meta, h1 count, jsonld, microdata, raw, rendered, canonical, redirect chain, http)
    HOME: ("Home", "Shipping", 1, [], [], 900, 1000, HOME, [], 200),
    W + "us/en/a": ("Same title", "Same desc", 0, ["FAQPage"], [], 100, 1000, W + "us/en/a?x=1", [], 200),
    W + "us/en/b": ("same TITLE ", "same desc", 2, [], ["Product"], 600, 1000, W + "us/en/elsewhere",
                    [W + "old-b", W + "us/en/b"], 200),
    W + "us/en/c": (None, None, 1, [], [], 10, 1000, None, [], 200),
    W + "us/en/gone": ("404 | X", "", 0, [], [], 10, 50, None, [], 404),
}
LINKS = [
    (HOME, W + "us/en/a", "nav", None),
    (HOME, W + "us/en/b", "body", "TAG_1"),
    (W + "us/en/a", W + "us/en/c", "body", None),
    (W + "us/en/b", W + "us/en/c", "footer", None),
    (W + "us/en/b", W + "us/en/gone", "body", None),
]


def build_run(run_dir, recorded=True):
    cfg = parse_config({
        "client": {"name": "X", "slug": "xco"},
        "scope": {"allowed_domains": ["www.x.test"], "strip_query_params": ["x"]},
        "win": {"name": "form", "url_patterns": [W + "talk*"]},
        "campaigns": [{"id": "c", "name": "C", "platform": "p", "ad_copy": "a",
                       "entry_links": [{"label": "a", "url": W + "us/en/a"}]}],
        "health": {"home_url": HOME},
    })
    (run_dir / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
    s = Store(run_dir / "crawl.db")
    s.set_meta(status="complete", campaign_id="c")
    s.add_entry(0, "a", W + "us/en/a", W + "us/en/a")
    for url, (title, meta, h1, jsonld, micro, raw, rendered, canonical, chain, http) in PAGES.items():
        links = [LinkRecord(t, t, "l", region, True, mc_id=tag) for src, t, region, tag in LINKS if src == url]
        s.save_page(PageRecord(
            url=url, status="ok" if http < 400 else "http_error", http_status=http, title=title, meta_description=meta,
            headings=[(1, "H")] * h1, jsonld_types=jsonld, raw_text_len=raw, rendered_text_len=rendered,
            canonical=canonical, redirect_chain=chain, is_dead=http == 404, dead_reason="HTTP 404" if http == 404 else None,
            microdata_types=micro if recorded else None, rdfa_types=[] if recorded else None,
            og_properties=(["og:title"] if url == HOME else []) if recorded else None,
            hreflang=[] if recorded else None, robots_meta=None,
        ), links)
    s.close()
    return run_dir


def health_rows(run_dir):
    with open(run_dir / "xco_site_health.csv", newline="") as f:
        return {r["url"]: r for r in csv.DictReader(f)}


def test_site_health_csv(tmp_path):
    build_run(tmp_path)
    result = CliRunner().invoke(app, ["report", "--run", str(tmp_path)])
    assert result.exit_code == 0, result.output
    rows = health_rows(tmp_path)
    assert len(rows) == 5
    home, a, b, c, gone = (rows[u] for u in (HOME, W + "us/en/a", W + "us/en/b", W + "us/en/c", W + "us/en/gone"))
    # click depth from home: every link vs body links only
    assert (a["clicks_from_home_all_links"], a["clicks_from_home_body_links"]) == ("1", "")
    assert (b["clicks_from_home_all_links"], b["clicks_from_home_body_links"]) == ("1", "1")
    assert (c["clicks_from_home_all_links"], c["clicks_from_home_body_links"]) == ("2", "")
    assert gone["clicks_from_home_body_links"] == "2"
    assert c["inbound_links"] == "2" and home["inbound_links"] == "0"
    # titles and descriptions: missing and duplicated (case and spaces ignored)
    assert (a["title_duplicated"], b["title_duplicated"], home["title_duplicated"]) == ("True", "True", "False")
    assert (a["meta_duplicated"], c["has_meta_description"], c["has_title"]) == ("True", "False", "False")
    assert (a["h1_count"], b["h1_count"]) == ("0", "2")
    # canonical: self after normalization, elsewhere, missing
    assert (home["canonical_self"], a["canonical_self"], b["canonical_self"], c["canonical_self"]) == \
        ("True", "True", "False", "")
    # structured data: JSON-LD or Microdata; JS dependence: raw text under half the rendered text
    assert (a["has_structured_data"], b["has_structured_data"], c["has_structured_data"]) == ("True", "True", "False")
    assert (home["js_dependent"], a["js_dependent"], b["js_dependent"]) == ("False", "True", "False")
    assert a["raw_text_share"] == "0.1"
    assert b["redirect_hops"] == "1" and home["redirect_hops"] == "0"
    assert (gone["is_dead"], gone["dead_inbound_pages"]) == ("True", "1")
    assert (home["carries_lead_tags"], a["carries_lead_tags"]) == ("True", "False")
    assert home["og_properties"] == "og:title"


def test_health_report_section_and_graph(tmp_path):
    build_run(tmp_path)
    CliRunner().invoke(app, ["report", "--run", str(tmp_path)])
    md = (tmp_path / "report.md").read_text()
    section = md[md.index("## Website health"):]
    assert "5 pages loaded. Click depth is counted from /us/en/home" in section
    assert "| dead (404, 410, soft 404) | 1 (20%) |" in section
    assert "| no structured data (JSON-LD, Microdata or RDFa) | 3 (60%) |" in section
    assert "| content depends on JavaScript | 3 (60%) |" in section
    assert "| duplicated title | 2 (40%) |" in section
    assert "| median clicks from home (any link / body links) | 1 / 1 |" in section  # [0,1,1,2,2] and [0,1,2]
    assert "| Open Graph tags | 1 (20%) |" in section
    assert "### By section" in section and "### By page type" in section
    assert "have not been checked for this run" in section
    data = json.loads((tmp_path / "report.json").read_text())
    assert data["health"]["summary"]["no_structured_data"] == 3 and data["health"]["home_url"] == HOME
    assert data["nodes"][W + "us/en/a"]["js_dependent"] is True
    assert data["nodes"][W + "us/en/c"]["clicks_from_home_body_links"] == -1
    g = nx.read_gexf(tmp_path / "graph.gexf")
    assert g.nodes[W + "us/en/b"]["has_structured_data"] is True
    assert g.nodes[W + "us/en/b"]["clicks_from_home_body_links"] == 1


def test_lead_bearing_pages_get_their_own_row(tmp_path):
    build_run(tmp_path)
    s = Store(tmp_path / "crawl.db")
    from pathcrawl.leads import Attribution

    s.replace_lead_attribution([Attribution(HOME, "TAG_1", "TAG_1", "exact", W + "us/en/b", "body", 7, 1, 7, "exact")])
    s.set_meta(leads={"files": [], "tags": []})
    s.close()
    CliRunner().invoke(app, ["report", "--run", str(tmp_path)])
    md = (tmp_path / "report.md").read_text()
    assert "| **pages carrying leads** | 1 | 0 | 1 (100%) | 0 (0%) | 0 |" in md
    assert health_rows(tmp_path)[HOME]["leads_allocated"] == "7.0"


def test_old_runs_say_the_new_signals_were_not_recorded(tmp_path):
    build_run(tmp_path, recorded=False)
    CliRunner().invoke(app, ["report", "--run", str(tmp_path)])
    md = (tmp_path / "report.md").read_text()
    assert "were not recorded for this run" in md and "| Open Graph tags |" not in md


ROBOTS = """User-agent: *
Disallow: /private/

User-agent: GPTBot
Disallow: /

User-agent: ClaudeBot
Allow: /

Sitemap: https://www.x.test/sitemap-index.xml
"""
INDEX = """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://www.x.test/sm-1.xml.gz</loc></sitemap><sitemap><loc>https://www.x.test/sm-missing.xml</loc></sitemap>
</sitemapindex>"""
CHILD = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.x.test/us/en/home</loc></url><url><loc>https://www.x.test/us/en/a?x=9</loc></url>
<url><loc>https://www.x.test/us/en/never-crawled</loc></url></urlset>"""


def fake_fetch(files):
    def fetch(url):
        body = files.get(url)
        return (200, body) if body is not None else (404, b"")
    return fetch


def test_robots_ai_rules():
    rules = robots_ai_rules(ROBOTS, "https://www.x.test", ["GPTBot", "ClaudeBot", "PerplexityBot"])
    assert rules == {"GPTBot": {"named": True, "allowed_home": False},
                     "ClaudeBot": {"named": True, "allowed_home": True},
                     "PerplexityBot": {"named": False, "allowed_home": True}}


def test_collect_site_signals():
    files = {
        "https://www.x.test/robots.txt": ROBOTS.encode(),
        "https://www.x.test/sitemap-index.xml": INDEX.encode(),
        "https://www.x.test/sm-1.xml.gz": gzip.compress(CHILD.encode()),
        "https://www.x.test/llms.txt": b"# X\n> Shipping company\n",
    }
    crawled = [HOME, W + "us/en/a", W + "us/en/b"]
    normalize = lambda u: u.split("?")[0]  # noqa: E731
    out = collect_site_signals(["https://www.x.test"], fake_fetch(files), crawled, normalize, ["GPTBot"])
    h = out["www.x.test"]
    assert h["robots_txt"] and h["llms_txt"] and h["ai_crawlers"]["GPTBot"]["allowed_home"] is False
    assert (h["sitemaps_read"], h["sitemap_urls"], h["crawled_pages"], h["crawled_pages_in_sitemap"]) == (3, 3, 3, 2)
    assert any("sm-missing" in e for e in h["errors"])
    # no robots.txt: the default sitemap location is tried; an HTML llms.txt (soft 404) does not count
    out = collect_site_signals(["https://www.x.test"], fake_fetch({"https://www.x.test/llms.txt": b"<html>404</html>"}),
                               crawled, normalize)
    h = out["www.x.test"]
    assert not h["robots_txt"] and not h["llms_txt"] and h["sitemaps_declared"] == ["https://www.x.test/sitemap.xml"]


def test_site_bases():
    assert site_bases([HOME, "http://127.0.0.1:8000/a", "https://other.test/x"], ["www.x.test", "127.0.0.1"]) == \
        ["http://127.0.0.1:8000", "https://www.x.test"]


def test_site_signals_cli_against_a_local_server(tmp_path):
    files = {"/robots.txt": b"User-agent: GPTBot\nDisallow: /\n", "/llms.txt": b"# Site\n"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = files.get(self.path)
            self.send_response(200 if body else 404)
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        cfg = parse_config({
            "client": {"name": "X", "slug": "xco"}, "scope": {"allowed_domains": ["127.0.0.1"]},
            "win": {"name": "form", "url_patterns": [base + "talk"]},
            "campaigns": [{"id": "c", "name": "C", "platform": "p", "ad_copy": "a",
                           "entry_links": [{"label": "a", "url": base + "a"}]}],
        })
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
        s = Store(tmp_path / "crawl.db")
        s.save_page(PageRecord(url=base + "a", status="ok", title="A", jsonld_types=[]), [])
        s.close()
        result = CliRunner().invoke(app, ["site-signals", "--run", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "AI crawlers blocked from home: 1, llms.txt yes" in result.output
        CliRunner().invoke(app, ["report", "--run", str(tmp_path)])
        md = (tmp_path / "report.md").read_text()
        assert "### Hosts: robots.txt, llms.txt and sitemaps" in md and "(blocked: GPTBot)" in md
    finally:
        server.shutdown()
        server.server_close()


def test_ups_health_home():
    from pathlib import Path

    from pathcrawl.config import load_config

    c = load_config(Path(__file__).parent.parent / "configs" / "ups.yaml")
    assert c.health.home_url == "https://www.ups.com/us/en/home"
