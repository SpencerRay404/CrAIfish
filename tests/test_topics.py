"""Peer comparison, part 3: page tags and the shared topic taxonomy."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from pathcrawl.backfill import backfill_page_tags
from pathcrawl.cli import app
from pathcrawl.entities import load_taxonomy, parse_taxonomy
from pathcrawl.extract import extract_page
from pathcrawl.store import LinkRecord, PageRecord, Store
from pathcrawl.topics import tag_topics

TOPICS = Path(__file__).parent.parent / "configs" / "topics.yaml"
X = "https://x.test/en/"

HTML = """<html><head><title>Ship by sea</title><meta property="og:type" content="article">
<meta property="article:tag" content="Ocean"><meta property="article:tag" content="LCL">
<meta name="keywords" content="ocean freight, LCL">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"BreadcrumbList",
"itemListElement":[{"@type":"ListItem","position":10,"name":"Ocean"},{"@type":"ListItem","position":2,"name":"Services"},
{"@type":"ListItem","position":1,"item":{"@id":"/","name":"Home"}}]},{"@type":"Service","name":"LCL shipping"},
{"@type":"FAQPage","mainEntity":[{"@type":"Question","name":"What is LCL?"}]}]}</script></head>
<body><nav><ul><li><button>Services</button><ul><li><a href="/ocean">Ocean freight</a></li></ul></li>
<li><a href="/about">About</a></li></ul></nav><h1></h1><h1>Container shipping</h1>
<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">Ocean Box</span></div></body></html>"""


def test_page_tags_are_extracted():
    t = extract_page(HTML, X + "services/ocean-freight.html").tags
    assert t["h1"] == "Container shipping"
    assert t["url_path_segments"] == ["en", "services", "ocean-freight.html"]
    assert t["breadcrumb"] == ["Home", "Services", "Ocean"]  # numeric positions, not text order
    assert {"BreadcrumbList", "Service", "FAQPage", "Product"} <= set(t["schema_types"])
    assert (t["og_type"], t["article_tags"], t["meta_keywords"]) == ("article", ["Ocean", "LCL"], ["ocean freight", "LCL"])
    assert t["service_entities"] == ["LCL shipping", "What is LCL?", "Ocean Box"]
    assert t["nav_labels"] == [["Services", "Ocean freight"], ["", "About"]]
    visible = extract_page('<nav aria-label="Breadcrumb"><ol><li><a href="/">Home</a></li><li>Air</li></ol></nav>', X).tags
    assert visible["breadcrumb"] == ["Home", "Air"]


def test_page_tags_are_stored_and_backfilled(tmp_path):
    s = Store(tmp_path / "crawl.db")
    d = extract_page(HTML, X + "ocean")
    s.save_page(PageRecord(url=X + "ocean", status="ok", title=d.title, tags=d.tags), [])
    # a page from an older crawl: no tags recorded
    s.save_page(PageRecord(url=X + "air-freight", status="ok", title="Air", headings=[(1, ""), (1, "Air cargo")],
                           jsonld_types=["FAQPage"], microdata_types=["Product"]),
                [LinkRecord("/x", X + "x", "Customs", "nav", True)])
    assert backfill_page_tags(s) == 1 and backfill_page_tags(s) == 0
    tags = s.page_tags()
    assert tags[X + "ocean"]["source"] == "crawl" and tags[X + "ocean"]["breadcrumb"] == ["Home", "Services", "Ocean"]
    air = tags[X + "air-freight"]
    assert (air["source"], air["h1"], air["schema_types"], air["nav_labels"]) == \
        ("backfill", "Air cargo", ["FAQPage", "Product"], [["", "Customs"]])
    s.close()


def test_topic_rules_fire_on_the_first_field_and_record_the_rule():
    tax = parse_taxonomy({"types": ["Topic"], "entities": [
        {"type": "Topic", "name": "Ocean freight", "terms": ["ocean freight", "container shipping"]},
        {"type": "Topic", "name": "Customs", "terms": ["customs"]},
        {"type": "Topic", "name": "Air", "terms": ["air freight"]},
    ]})
    fields = {X + "p": {"title": "Ship by sea", "h1": "Container shipping", "breadcrumb": "Home | Ocean freight",
                        "nav_label": "Customs help", "url_path": "en services air freight"}}
    assert tag_topics(fields, tax) == [
        (X + "p", "Ocean freight", "h1", "container shipping"),
        (X + "p", "Customs", "nav_label", "customs"),
        (X + "p", "Air", "url_path", "air freight"),
    ]


def test_shared_taxonomy_is_valid_and_versioned():
    tax = load_taxonomy(TOPICS)
    assert tax.version and len(tax.entities) >= 30
    names = {e.name for e in tax.entities}
    for peer_topic in ("Ocean freight", "Customs brokerage", "Fulfillment", "Air freight"):
        assert peer_topic in names


def test_topics_cli(tmp_path):
    from pathcrawl.config import parse_config

    cfg = parse_config({
        "client": {"name": "X", "slug": "x"}, "scope": {"allowed_domains": ["x.test"]},
        "win": {"name": "w", "url_patterns": [X + "talk"]},
        "campaigns": [{"id": "c", "name": "c", "platform": "p", "ad_copy": "a",
                       "entry_links": [{"label": "a", "url": X + "home"}]}],
    })
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
    s = Store(tmp_path / "crawl.db")
    s.save_page(PageRecord(url=X + "home", status="ok", title="Home"),
                [LinkRecord("/ocean", X + "ocean-freight", "Ocean freight", "nav", True)])
    s.save_page(PageRecord(url=X + "ocean-freight", status="ok", title="Ship by sea"), [])
    s.close()
    result = CliRunner().invoke(app, ["topics", "--run", str(tmp_path), "--taxonomy", str(TOPICS)])
    assert result.exit_code == 0, result.output
    assert "Page tags rebuilt from stored data for 2 pages" in result.output
    s = Store(tmp_path / "crawl.db")
    rows = {(r["url"], r["topic"]): (r["field"], r["rule"]) for r in s.page_topics()}
    meta = s.meta("topics")
    s.close()
    assert rows[(X + "ocean-freight", "Ocean freight")] == ("nav_label", "ocean freight")
    assert meta["version"] == load_taxonomy(TOPICS).version and len(meta["fingerprint"]) == 12
