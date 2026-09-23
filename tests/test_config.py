import copy
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from pathcrawl.cli import app
from pathcrawl.config import ConfigError, config_warnings, load_config, parse_config

CONFIGS = Path(__file__).resolve().parent.parent / "configs"

VALID = {
    "client": {"name": "Acme", "slug": "acme"},
    "scope": {
        "allowed_domains": ["www.acme.test", "blog.acme.test"],
        "locale_include": ["/en/"],
        "strip_query_params": ["utm_*"],
    },
    "win": {"name": "Demo form", "url_patterns": ["https://www.acme.test/en/demo*"]},
    "campaigns": [
        {
            "id": "c1",
            "name": "Campaign 1",
            "platform": "linkedin",
            "ad_copy": "Some ad copy.",
            "entry_links": [{"label": "link", "url": "https://www.acme.test/en/start?utm_source=x"}],
        }
    ],
}


def cfg(**overrides):
    """VALID with nested overrides, e.g. cfg(win={"url_patterns": [...]})."""
    data = copy.deepcopy(VALID)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    return data


def error_for(data) -> str:
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    return str(exc.value)


def test_valid_config_parses_with_crawl_defaults():
    c = parse_config(cfg())
    assert c.crawl.max_depth == 8
    assert c.crawl.headed is True
    assert c.crawl.respect_robots is True
    assert c.win.require_form is False


def test_shipped_configs_are_valid():
    for name in ("example.yaml", "ups.yaml"):
        load_config(CONFIGS / name)


def test_ups_config_warns_about_placeholders():
    warnings = config_warnings(load_config(CONFIGS / "ups.yaml"))
    assert any("win.url_patterns[0]" in w and "REPLACE-ME" in w for w in warnings)
    assert any("campaigns[0].entry_links[0].url" in w for w in warnings)


def test_example_config_has_no_warnings():
    assert config_warnings(load_config(CONFIGS / "example.yaml")) == []


def test_unknown_key_is_rejected():
    msg = error_for(cfg(crawl={"max_dept": 3}))
    assert "crawl.max_dept" in msg and "Extra inputs are not permitted" in msg


def test_missing_section_is_reported_with_location():
    data = cfg()
    del data["win"]
    assert "win: Field required" in error_for(data)


@pytest.mark.parametrize("domain", ["https://www.acme.test", "www.acme.test/en", "*.acme.test", "www.acme.test:443", ""])
def test_allowed_domains_must_be_bare_hosts(domain):
    assert "not a bare hostname" in error_for(cfg(scope={"allowed_domains": [domain]}))


def test_allowed_domains_are_lowercased():
    c = parse_config(cfg(scope={"allowed_domains": ["WWW.Acme.Test"]}))
    assert c.scope.allowed_domains == ["www.acme.test"]


def test_bad_slug_is_rejected():
    assert "client.slug" in error_for(cfg(client={"name": "Acme", "slug": "Acme Co"}))


def test_entry_link_off_allowlist_is_rejected():
    data = cfg()
    data["campaigns"][0]["entry_links"] = [{"label": "short", "url": "https://lnkd.in/abc"}]
    msg = error_for(data)
    assert "'lnkd.in' is not in scope.allowed_domains" in msg
    assert "destination URL" in msg


def test_entry_link_must_be_absolute_http():
    data = cfg()
    data["campaigns"][0]["entry_links"] = [{"label": "rel", "url": "/en/start"}]
    assert "not an absolute http(s) URL" in error_for(data)


def test_locale_filters_apply_to_entry_links():
    data = cfg()
    data["campaigns"][0]["entry_links"] = [{"label": "gb", "url": "https://www.acme.test/gb/start"}]
    assert "excluded by the locale filters" in error_for(data)


def test_all_entry_link_problems_are_reported_together():
    data = cfg()
    data["campaigns"][0]["entry_links"] = [
        {"label": "a", "url": "https://lnkd.in/abc"},
        {"label": "b", "url": "https://www.acme.test/gb/start"},
    ]
    msg = error_for(data)
    assert "entry_links[0]" in msg and "entry_links[1]" in msg


def test_duplicate_campaign_ids_rejected():
    data = cfg()
    data["campaigns"].append(copy.deepcopy(data["campaigns"][0]))
    assert "used more than once" in error_for(data)


def test_empty_entry_links_rejected():
    data = cfg()
    data["campaigns"][0]["entry_links"] = []
    assert "campaigns.0.entry_links" in error_for(data)


def test_invalid_win_regex_rejected():
    assert "invalid regex" in error_for(cfg(win={"url_patterns": ["re:https://(unclosed"]}))


def test_require_form_needs_selector():
    assert "no form_selector" in error_for(cfg(win={"require_form": True}))


def test_crawl_limits_must_be_positive():
    msg = error_for(cfg(crawl={"max_depth": 0, "delay_ms": -1}))
    assert "crawl.max_depth" in msg and "crawl.delay_ms" in msg


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.acme.test/en/demo", True),
        ("https://www.acme.test/en/demo/request?x=1", True),
        ("https://www.acme.test/en/pricing", False),
        ("https://blog.acme.test/en/demo", False),
    ],
)
def test_win_glob_matching(url, expected):
    assert parse_config(cfg()).win.url_matches(url) is expected


def test_win_regex_must_match_whole_url():
    c = parse_config(cfg(win={"url_patterns": [r"re:https://www\.acme\.test/en/demo/\d+"]}))
    assert c.win.url_matches("https://www.acme.test/en/demo/42")
    assert not c.win.url_matches("https://www.acme.test/en/demo/42/extra")


def test_scope_helpers():
    scope = parse_config(cfg()).scope
    assert scope.normalize("/en/a?utm_source=x", base="https://www.acme.test/") == "https://www.acme.test/en/a"
    assert scope.in_scope("https://blog.acme.test/en/post")
    assert not scope.in_scope("https://blog.acme.test/fr/post")
    assert not scope.in_scope("https://evil.test/en/post")


def test_win_on_unreachable_host_warns():
    c = parse_config(cfg(win={"url_patterns": ["https://forms.acme.test/en/demo"]}))
    assert any("can never reach it" in w for w in config_warnings(c))


def test_duplicate_entry_links_after_normalization_warn():
    data = cfg()
    data["campaigns"][0]["entry_links"].append(
        {"label": "same", "url": "https://WWW.acme.test/en/start#top"}
    )
    assert any("normalize to the same URL" in w for w in config_warnings(parse_config(data)))


def test_campaign_lookup():
    c = parse_config(cfg())
    assert c.campaign("c1").name == "Campaign 1"
    with pytest.raises(ConfigError, match="known: c1"):
        c.campaign("nope")


def test_load_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("client: [unclosed")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad)
    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("just a string")
    with pytest.raises(ConfigError, match="YAML mapping"):
        load_config(scalar)


def test_cli_validate(tmp_path):
    runner = CliRunner()
    ok = runner.invoke(app, ["validate", "--config", str(CONFIGS / "ups.yaml")])
    assert ok.exit_code == 0, ok.output
    assert "is valid" in ok.output and "warning" in ok.output

    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(cfg(crawl={"max_pages": 0})))
    result = runner.invoke(app, ["validate", "--config", str(bad)])
    assert result.exit_code == 1
