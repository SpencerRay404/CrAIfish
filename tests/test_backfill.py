"""Change 1: the campaign tag (WT.mc_id) kept on each link, and the backfill
that fills it in for runs crawled before the column existed."""

from __future__ import annotations

import sqlite3

import yaml
from typer.testing import CliRunner

from pathcrawl.backfill import backfill_links
from pathcrawl.cli import app
from pathcrawl.config import parse_config
from pathcrawl.extract import extract_links
from pathcrawl.normalize import captured_param
from pathcrawl.store import Store

B = "https://x.test/"


def config(**scope):
    return parse_config({
        "client": {"name": "X", "slug": "xco"},
        "scope": {"allowed_domains": ["x.test"], "strip_query_params": ["WT.*", "msockid"],
                  "capture_params": ["WT.mc_id"], **scope},
        "win": {"name": "form", "url_patterns": [B + "talk*"]},
        "campaigns": [{"id": "c", "name": "C", "platform": "p", "ad_copy": "a",
                       "entry_links": [{"label": "a", "url": B + "a"}]}],
    })


def test_captured_param():
    assert captured_param("/talk?WT.mc_id=ONLINE_WEB_X_123&x=1", ["WT.mc_id"]) == "ONLINE_WEB_X_123"
    assert captured_param("https://x.test/talk?wt.MC_ID=abc", ["WT.mc_id"]) == "abc"  # key case-insensitive
    assert captured_param("https://x.test/talk?WT.mc_id=AbC_1", ["wt.mc_id"]) == "AbC_1"  # value case kept
    assert captured_param("https://x.test/talk?WT.mc_id=", ["WT.mc_id"]) is None
    assert captured_param("https://x.test/talk", ["WT.mc_id"]) is None
    assert captured_param("https://x.test/talk?a=1", []) is None
    assert captured_param(None, ["WT.mc_id"]) is None
    assert captured_param("https://x.test/t?gclid=g&WT.mc_id=m", ["gclid", "WT.mc_id"]) == "g"  # first name wins


def test_tag_is_kept_on_the_link_but_not_in_the_url():
    html = '<main><a href="/talk-v4.html?WT.mc_id=ONLINE_WEB_A_1">Talk</a><a href="/a">a</a></main>'
    talk, a = extract_links(html, B + "start", ["WT.*"], capture_params=["WT.mc_id"])
    assert talk.url == B + "talk-v4.html" and talk.mc_id == "ONLINE_WEB_A_1"
    assert a.mc_id is None
    assert extract_links(html, B + "start", ["WT.*"])[0].mc_id is None  # not captured unless configured


OLD_LINKS = """CREATE TABLE links (id INTEGER PRIMARY KEY, src TEXT NOT NULL, href TEXT, url TEXT, text TEXT,
               region TEXT, in_scope INTEGER NOT NULL, operator INTEGER NOT NULL DEFAULT 0)"""


def old_run(tmp_path, live_config=None):
    """A run directory whose crawl.db predates links.mc_id."""
    db = sqlite3.connect(tmp_path / "crawl.db")
    db.execute(OLD_LINKS)
    db.executemany("INSERT INTO links(src, href, url, text, region, in_scope) VALUES (?, ?, ?, ?, ?, 1)", [
        (B + "a", "/talk-v4.html?WT.mc_id=TAG_1", B + "talk-v4.html", "Talk", "body"),
        (B + "a", "/talk-v4.html?WT.mc_id=TAG_2&utm_source=x", B + "talk-v4.html", "Talk", "nav"),
        (B + "b", "https://x.test/talk-v4.html?wt.mc_id=TAG_1", B + "talk-v4.html", "Talk", "body"),
        (B + "b", "/c", B + "c", "c", "body"),
        (B + "c", "mailto:x@x.test", None, "mail", "footer"),
    ])
    db.commit()
    db.close()
    s = Store(tmp_path / "crawl.db")  # migrates the old table
    for url in (B + "a", B + "b", B + "c", B + "c?msockid=123"):
        s.db.execute("INSERT INTO pages(url, status) VALUES (?, 'ok')", (url,))
    s.db.commit()
    s.close()
    old = config(capture_params=[]) if live_config else config()
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(old.model_dump()))
    return tmp_path


def test_old_database_is_migrated_and_backfilled(tmp_path):
    old_run(tmp_path)
    s = Store(tmp_path / "crawl.db")
    assert "mc_id" in {r["name"] for r in s.db.execute("PRAGMA table_info(links)")}
    out = backfill_links(s, config().scope)
    assert (out.links, out.links_tagged, out.distinct_tags, out.source_pages) == (5, 3, 2, 2)
    tags = [r["mc_id"] for r in s.db.execute("SELECT mc_id FROM links ORDER BY id")]
    assert tags == ["TAG_1", "TAG_2", "TAG_1", None, None]
    # the page crawled once with ?msockid= is merged into the clean URL
    assert out.duplicate_pages == [[B + "c", B + "c?msockid=123"]]
    assert [r["url"] for r in s.db.execute("SELECT url FROM pages ORDER BY url")] == [B + "a", B + "b", B + "c"]
    assert s.resolve(B + "c?msockid=123") == B + "c"
    # running it again changes nothing
    again = backfill_links(s, config().scope)
    assert again.links_tagged == 3 and again.duplicate_pages == []
    s.close()


def test_backfill_cli_uses_the_live_config_when_the_run_copy_has_no_capture_params(tmp_path, monkeypatch):
    (tmp_path / "run").mkdir()
    run = old_run(tmp_path / "run", live_config=True)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "xco.yaml").write_text(yaml.safe_dump(config().model_dump()))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["backfill-links", "--run", str(run)])
    assert result.exit_code == 0, result.output
    assert "configs/xco.yaml" in result.output
    assert "3 of 5 carry a tag" in result.output and "2 distinct tags on 2 source pages" in result.output
    assert "Merged 1 pages that were stored under more than one URL and renamed 0" in result.output


def test_new_links_store_the_tag(tmp_path):
    from pathcrawl.store import LinkRecord, PageRecord

    s = Store(tmp_path / "crawl.db")
    s.save_page(PageRecord(url=B + "a", status="ok"),
                [LinkRecord("/talk?WT.mc_id=T", B + "talk", "Talk", "body", True, mc_id="T")])
    assert [r["mc_id"] for r in s.links()] == ["T"]
    s.close()


def test_ups_config_captures_mc_id_and_strips_source_params():
    from pathlib import Path

    from pathcrawl.config import load_config

    c = load_config(Path(__file__).parent.parent / "configs" / "ups.yaml")
    assert c.scope.capture_params == ["WT.mc_id"]
    base = "https://www.ups.com/us/en/customized-shipping-logistic-services/retail-store-shipping-logistic-solutions"
    for param in ("msockid=abc", "_gl=1*x", "gbraid=g", "wbraid=w", "WT.mc_id=T"):
        assert c.scope.normalize(f"{base}?{param}") == base


def test_duplicate_pages_are_merged(tmp_path):
    from pathcrawl.store import LinkRecord, PageRecord

    s = Store(tmp_path / "crawl.db")
    clean, dup = B + "retail", B + "retail?msockid=abc"
    s.add_entry(0, "retail", dup, dup)
    s.save_page(PageRecord(url=clean, status="ok"), [
        LinkRecord("/talk?WT.mc_id=T1", B + "talk", "Talk", "body", True),
        LinkRecord("/a", B + "a", "a", "nav", True),
    ])
    s.save_page(PageRecord(url=dup, status="ok"), [
        LinkRecord("/talk?WT.mc_id=T2", B + "talk", "Talk", "body", True),  # only on the msockid copy
        LinkRecord("/a", B + "a", "a", "nav", True),                        # on both: kept once
    ])
    s.save_page(PageRecord(url=B + "other", status="ok"), [LinkRecord("x", dup, "r", "body", True)])
    # a page known only under its msockid spelling is renamed
    s.save_page(PageRecord(url=B + "solo?msockid=1", status="ok"), [LinkRecord("/a", B + "a", "a", "body", True)])
    out = backfill_links(s, config().scope)
    assert sorted(out.duplicate_pages) == [[clean, dup], [B + "solo?msockid=1"]]
    assert s.has_page(B + "solo") and s.resolve(B + "solo?msockid=1") == B + "solo"  # renamed
    rows = [(r["src"], r["href"], r["mc_id"]) for r in s.db.execute("SELECT * FROM links WHERE src = ? ORDER BY id", (clean,))]
    assert rows == [(clean, "/talk?WT.mc_id=T1", "T1"), (clean, "/a", None), (clean, "/talk?WT.mc_id=T2", "T2")]
    assert [r["url"] for r in s.db.execute("SELECT url FROM links WHERE src = ?", (B + "other",))] == [clean]
    assert [e["node_url"] for e in s.entries()] == [clean]
    assert s.resolve(dup) == clean
    assert not s.has_page(dup)
    s.close()
