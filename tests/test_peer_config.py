"""Peer comparison, part 1: scope by regex, conversion classes, off-domain destinations."""

from __future__ import annotations

import json

import pytest
import yaml
from typer.testing import CliRunner

from pathcrawl.cli import app
from pathcrawl.config import ConfigError, parse_config
from pathcrawl.graph import graph_from_store
from pathcrawl.store import LinkRecord, PageRecord, Store

F = "https://www.fedex.test/en-us/"


def peer_config(**win):
    return parse_config({
        "client": {"name": "FedEx", "slug": "fedex"},
        "scope": {"allowed_domains": ["www.fedex.test"], "include_patterns": [r"^https://www\.fedex\.test/en-us/"]},
        "win": {
            "name": "any win",
            "classes": [
                {"name": "talk_to_sales", "patterns": [rf"^{F}small-business/sales-support\.html",
                                                        r"^https://isell\.my\.site\.test/s/schedule"]},
                {"name": "self_serve", "patterns": [rf"^{F}open-account/"], "any_win": False},
            ],
            **win,
        },
        "campaigns": [{"id": "peer", "name": "Peer", "platform": "site", "ad_copy": "none",
                       "entry_links": [{"label": "home", "url": F + "home.html"}]}],
        "health": {"home_url": F + "home.html"},
    })


def test_scope_include_patterns():
    c = peer_config()
    assert c.scope.in_scope(F + "shipping.html")
    assert not c.scope.in_scope("https://www.fedex.test/fr-ca/shipping.html")
    with pytest.raises(ConfigError, match="invalid regex"):
        parse_config({**peer_config().model_dump(), "scope": {"allowed_domains": ["x.test"], "include_patterns": ["("]}})


def test_classes():
    w = peer_config().win
    assert w.url_matches(F + "small-business/sales-support.html")
    assert w.win_type(F + "small-business/sales-support.html") == "talk_to_sales"
    assert w.url_matches("https://isell.my.site.test/s/schedule?x=1")
    assert not w.url_matches(F + "open-account/start.html")  # self-serve: tracked, not a win
    assert w.win_class(F + "open-account/start.html").name == "self_serve"
    assert w.destination(F + "open-account/start.html")
    with pytest.raises(ConfigError, match="no win defined"):
        parse_config({**peer_config().model_dump(), "win": {"name": "x", "classes": [
            {"name": "self_serve", "patterns": ["^x"], "any_win": False}]}})
    with pytest.raises(ConfigError, match="unique"):
        peer_config(classes=[{"name": "a", "patterns": ["^x"]}, {"name": "a", "patterns": ["^y"]}])


def test_off_domain_destination_is_a_node_never_fetched(tmp_path):
    c = peer_config()
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(c.model_dump()))
    s = Store(tmp_path / "crawl.db")
    s.set_meta(status="complete", campaign_id="peer")
    s.add_entry(0, "home", F + "home.html", F + "home.html")
    s.save_page(PageRecord(url=F + "home.html", status="ok", title="Home", jsonld_types=[]), [
        LinkRecord("x", "https://isell.my.site.test/s/schedule", "Schedule a call", "body", False),
        LinkRecord("y", F + "open-account/start.html", "Open an account", "nav", True),
        LinkRecord("z", "https://elsewhere.test/page", "Other", "body", False),
    ])
    s.save_page(PageRecord(url=F + "open-account/start.html", status="ok", title="Open", jsonld_types=[]), [])
    g, entries = graph_from_store(s, c.win)
    s.close()
    sched = "https://isell.my.site.test/s/schedule"
    assert g.nodes[sched]["win"] and g.nodes[sched]["win_type"] == "talk_to_sales"
    assert g.nodes[sched]["explored"] is False and g.nodes[sched]["status"] is None
    assert "https://elsewhere.test/page" not in g
    assert g.nodes[F + "open-account/start.html"]["conversion_class"] == "self_serve"
    assert not g.nodes[F + "open-account/start.html"]["win"]

    result = CliRunner().invoke(app, ["report", "--run", str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "report.json").read_text())
    assert data["win_types"]["talk_to_sales"]["counts_as_win"] is True
    assert data["win_types"]["self_serve"] == {"counts_as_win": False, "win_pages": 1, "pages_linking_directly": 1,
                                               "column": "clicks_to_self_serve"}
    assert data["nodes"][F + "home.html"]["clicks_to_any_win"] == 1
    assert data["nodes"][F + "home.html"]["clicks_to_self_serve"] == 1
