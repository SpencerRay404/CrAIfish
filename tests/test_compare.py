"""Peer comparison, part 4: pathcrawl compare.

Two small sites:

UPS (complete crawl, carries lead data that must never reach the output):
    home --nav--> ocean --body--> article --body--> deep
    home --body--> talk (win)         ocean --nav--> talk
    other-section (outside the slice) --body--> talk

Peer (budget stop, one URL still queued; an off-domain scheduling win):
    home --body--> services --body--> customs --body--> schedule (off-domain win)
    home --nav--> rates (self-serve, not a win)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from pathcrawl.cli import app
from pathcrawl.compare import pct
from pathcrawl.config import parse_config
from pathcrawl.leads import Attribution
from pathcrawl.store import LinkRecord, PageRecord, Store

TOPICS = Path(__file__).parent.parent / "configs" / "topics.yaml"
U = "https://www.u.test/us/en/"
P = "https://www.peer.test/en-us/"
SECRET_TAG = "ONLINE_WEB_Wholesalers_MktgVirtualConsultationMainPage_109541"

CRAWL = {"max_depth": 50, "max_pages": 5000, "delay_ms": 1000, "backoff_s": 30, "backoff_retries": 2,
         "host_block_limit": 5, "headed": False, "screenshot": False}


def make_run(run_dir: Path, cfg: dict, pages: dict, status="complete", queued=()):
    run_dir.mkdir(parents=True)
    c = parse_config(cfg)
    (run_dir / "config.yaml").write_text(yaml.safe_dump(c.model_dump()))
    s = Store(run_dir / "crawl.db")
    s.set_meta(status=status, campaign_id=c.campaigns[0].id)
    s.add_entry(0, "home", c.campaigns[0].entry_links[0].url, c.campaigns[0].entry_links[0].url)
    for url, (title, links, extra) in pages.items():
        s.save_page(PageRecord(url=url, status="ok", title=title, meta_description=f"About {title}",
                               headings=[(1, title)], jsonld_types=extra.get("jsonld", []), canonical=url,
                               raw_text_len=extra.get("raw", 900), rendered_text_len=1000),
                    [LinkRecord(t, t, text, region, in_scope, mc_id=tag) for t, text, region, in_scope, tag in links])
    for q in queued:
        s.enqueue(q, 3, None)
    return s, c, run_dir


def ups_run(root: Path) -> Path:
    cfg = {
        "client": {"name": "U", "slug": "u"},
        "scope": {"allowed_domains": ["www.u.test"], "capture_params": ["WT.mc_id"]},
        "win": {"name": "consultation", "url_patterns": [U + "talk*"]},
        "campaigns": [{"id": "c", "name": "c", "platform": "p", "ad_copy": "a", "entry_links": [{"label": "h", "url": U + "home"}]}],
        "health": {"home_url": U + "home"},
        "crawl": CRAWL,
    }
    pages = {
        U + "home": ("Home", [(U + "ocean-freight", "Ocean freight", "nav", True, None),
                              (U + "talk", "Talk to an expert", "body", True, SECRET_TAG)], {"jsonld": ["Organization"]}),
        U + "ocean-freight": ("Ocean freight", [(U + "insights/article", "Read the report", "body", True, None),
                                                (U + "talk", "Contact", "nav", True, None)], {}),
        U + "insights/article": ("Ocean freight trends", [(U + "insights/deep", "More", "body", True, None)], {"raw": 100}),
        U + "insights/deep": ("Deep dive", [], {}),
        U + "other/page": ("Careers", [(U + "talk", "Talk", "body", True, None)], {}),
    }
    s, c, d = make_run(root / "ups", cfg, pages)
    s.replace_lead_attribution([Attribution(U + "home", SECRET_TAG, SECRET_TAG, "exact", U + "talk", "body",
                                            12345, 1, 12345, "exact")])
    s.set_meta(leads={"files": ["secret.csv"], "tags": [{"lead_tag": SECRET_TAG}]})
    s.close()
    return d


def peer_run(root: Path) -> Path:
    cfg = {
        "client": {"name": "Peer", "slug": "peer"},
        "scope": {"allowed_domains": ["www.peer.test"], "include_patterns": [r"^https://www\.peer\.test/en-us/"]},
        "win": {"name": "any win", "classes": [
            {"name": "talk_to_sales", "patterns": [r"^https://sched\.elsewhere\.test/"]},
            {"name": "self_serve", "patterns": [rf"^{P}rates"], "any_win": False},
        ]},
        "campaigns": [{"id": "peer-compare", "name": "p", "platform": "site", "ad_copy": "a",
                       "entry_links": [{"label": "h", "url": P + "home.html"}]}],
        "health": {"home_url": P + "home.html"},
        "crawl": CRAWL,
    }
    pages = {
        P + "home.html": ("Home", [(P + "services.html", "Services", "body", True, None),
                                   (P + "rates.html", "Rates", "nav", True, None)], {}),
        P + "services.html": ("Our services", [(P + "customs-brokerage.html", "Customs", "body", True, None)], {}),
        P + "customs-brokerage.html": ("Customs brokerage", [("https://sched.elsewhere.test/book", "Schedule a call",
                                                              "body", False, None)], {}),
        P + "rates.html": ("Rates", [], {}),
    }
    s, c, d = make_run(root / "peer", cfg, pages, status="budget", queued=[P + "later.html"])
    s.close()
    return d


def compare_file(root: Path, ups: Path, peer: Path) -> Path:
    f = root / "compare.yaml"
    f.write_text(yaml.safe_dump({
        "taxonomy": str(TOPICS),
        "sites": [
            {"name": "UPS", "run": str(ups), "slice": [r"^https://www\.u\.test/us/en/(home|ocean|insights|talk)"]},
            {"name": "Peer", "run": str(peer), "none_found": {"quote_request": [P + "quote.html"]}},
        ],
    }))
    return f


def test_percentile():
    assert pct([], 0.5) is None
    assert pct([1, 2, 3, 4], 0.5) == 2 and pct([1, 2, 3, 4], 0.9) == 4 and pct([5], 0.9) == 5


def test_compare(tmp_path):
    ups, peer = ups_run(tmp_path), peer_run(tmp_path)
    out = tmp_path / "compare"
    result = CliRunner().invoke(app, ["compare", "--sites", str(compare_file(tmp_path, ups, peer)), "--out", str(out)])
    assert result.exit_code == 0, result.output
    data = json.loads((out / "summary.json").read_text())
    u, p = data["sites"]

    # the slice keeps 4 UPS pages (other/page is outside it); distances use the whole graph
    assert u["pages"] == 4 and u["complete"] is True and u["depth_note"] == ""
    # home and ocean are one click from the win; article and deep only link onward (no route)
    assert u["win_all"]["distribution"] == {"1": 2} and u["win_all"]["no_route"] == 2
    assert (u["win_all"]["p50"], u["win_all"]["p90"], u["win_all"]["max"]) == (1, 1, 1)
    assert u["win_all"]["no_route_share"] == 50.0
    assert u["win_body"]["distribution"] == {"1": 1} and u["win_body"]["no_route"] == 3
    assert u["menu_reliance_share"] == 25.0  # ocean reaches the win only through its menu
    assert u["home_all"]["distribution"] == {"0": 1, "1": 1, "2": 1, "3": 1}
    assert u["home_body"]["no_route"] == 3  # ocean is reached from home only by the nav
    assert u["share_4plus_from_home"] == 0.0
    assert u["health"]["readable_without_js"] == 75.0 and u["health"]["structured_data"] == 25.0
    assert u["health"]["one_h1"] == 100.0 and u["health"]["self_canonical"] == 100.0

    # the peer crawl stopped early: depth is labelled understated
    assert p["complete"] is False and p["queued_unvisited"] == 1 and p["depth_note"] == "incomplete, depth understated"
    assert p["win_all"]["distribution"] == {"1": 1, "2": 1, "3": 1}  # customs, services, home -> off-domain schedule
    assert p["win_classes"]["talk_to_sales"] == {"pages": 1, "linked": 1, "counts_as_win": True}
    assert p["win_classes"]["self_serve"]["counts_as_win"] is False
    assert u["settings_fingerprint"] == p["settings_fingerprint"]
    assert u["taxonomy_fingerprint"] == p["taxonomy_fingerprint"]

    md = (out / "summary.md").read_text()
    assert "| clicks to nearest win, all links: p50 / p90 / max | 1 / 1 / 1 | 2 / 3 / 3 |" in md
    assert "**Incomplete crawls, depth understated:** Peer." in md
    assert "quote_request: none found (checked: https://www.peer.test/en-us/quote.html)." in md
    assert "## What this cannot tell us" in md and "how well any site converts" in md
    assert "Warning" not in md

    with open(out / "topic_matrix.csv", newline="") as f:
        matrix = {(r["site"], r["topic"]): r for r in csv.DictReader(f)}
    assert matrix[("UPS", "Ocean freight")]["pages"] == "2"
    assert matrix[("Peer", "Customs brokerage")]["pages"] == "1"
    with open(out / "distance_to_win.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert {"site": "UPS", "mode": "all", "clicks": "no route", "pages": "2", "complete": "True"} in rows
    for name in ("depth_from_home.csv", "health.csv", "services.csv"):
        assert (out / name).exists()


def test_compare_never_writes_lead_data_or_conversion_claims(tmp_path):
    ups, peer = ups_run(tmp_path), peer_run(tmp_path)
    out = tmp_path / "compare"
    CliRunner().invoke(app, ["compare", "--sites", str(compare_file(tmp_path, ups, peer)), "--out", str(out)])
    for f in out.iterdir():
        text = f.read_text()
        assert SECRET_TAG not in text and "12345" not in text and "secret.csv" not in text, f.name
        assert "Wholesalers" not in text
        for claim in ("converts better", "conversion rate", "more leads", "fewer leads"):
            assert claim not in text.lower(), (f.name, claim)


def test_settings_mismatch_is_flagged(tmp_path):
    ups, peer = ups_run(tmp_path), peer_run(tmp_path)
    cfg = yaml.safe_load((peer / "config.yaml").read_text())
    cfg["crawl"]["delay_ms"] = 200
    (peer / "config.yaml").write_text(yaml.safe_dump(cfg))
    out = tmp_path / "compare"
    CliRunner().invoke(app, ["compare", "--sites", str(compare_file(tmp_path, ups, peer)), "--out", str(out)])
    assert "not all crawled with the same crawler version and settings" in (out / "summary.md").read_text()


def test_missing_runs_are_reported(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text(yaml.safe_dump({"sites": [{"name": "UPS", "run": None}]}))
    result = CliRunner().invoke(app, ["compare", "--sites", str(f), "--out", str(tmp_path / "o")])
    assert result.exit_code == 2 and "no run for: UPS" in result.output


def test_shipped_peer_configs():
    from pathcrawl.compare import load_compare
    from pathcrawl.config import load_config
    from pathcrawl.report import settings_fingerprint

    root = Path(__file__).parent.parent / "configs"
    sites, taxonomy = load_compare(root / "peers" / "compare.yaml")
    assert [s.name for s in sites] == ["UPS", "FedEx", "DHL", "Flexport", "Maersk"]
    fps = {settings_fingerprint(load_config(Path(__file__).parent.parent / s.config)) for s in sites}
    assert len(fps) == 1  # every site crawled with the same settings
    fedex = load_config(root / "peers" / "fedex.yaml")
    assert fedex.win.win_type("https://www.fedex.com/en-us/small-business/sales-support.html") == "talk_to_sales"
    assert fedex.win.destination("https://isell.my.site.com/s/schedule")
    assert not fedex.win.url_matches("https://www.fedex.com/en-us/open-account/start.html")
    dhl = load_config(root / "peers" / "dhl.yaml")
    assert dhl.win.win_type("https://www.dhl.com/global-en/microsites/supply-chain/fulfillment-network/get-a-quote.html") \
        == "quote_request"
    assert not dhl.scope.in_scope("https://www.dhl.com/global-en/home.html")
    for s in sites[1:]:
        assert load_config(Path(__file__).parent.parent / s.config).scope.capture_params == []  # public data only
