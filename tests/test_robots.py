"""robots.txt matching per RFC 9309: wildcards, $ anchors, longest match wins."""

from __future__ import annotations

from pathlib import Path

import pytest

from pathcrawl.robots import Robots

FEDEX = (Path(__file__).parent / "fixtures" / "robots" / "fedex.txt").read_text()  # fetched 2026-10-03


@pytest.mark.parametrize(("url", "allowed"), [
    ("https://www.fedex.com/en-us/home.html", True),
    ("https://www.fedex.com/en-us/shipping.html?campaign=x", False),     # Disallow: /*?* beats Allow: /
    ("https://www.fedex.com/en-us/home.html?location=home", True),       # longer Allow wins
    ("https://www.fedex.com/en-us/tracking.html?action=track", True),
    ("https://www.fedex.com/en-us/quick-help/faq.html", False),
    ("https://www.fedex.com/content/fedex-com/sites/us/page.html", False),
    ("https://www.fedex.com/robots.txt", True),
])
def test_fedex_query_string_rule(url, allowed):
    """The stdlib parser applied "Allow: /" first and ignored "*", allowing everything."""
    assert Robots(FEDEX).allowed("pathcrawl", url) is allowed


def test_longest_match_and_ties():
    r = Robots("User-agent: *\nDisallow: /a\nAllow: /a/b\nDisallow: /a/b/c\nAllow: /x\nDisallow: /x\n")
    assert not r.allowed("bot", "https://h/a/z")
    assert r.allowed("bot", "https://h/a/b/z")
    assert not r.allowed("bot", "https://h/a/b/c/d")
    assert r.allowed("bot", "https://h/x")  # same length: allow wins
    assert r.allowed("bot", "https://h/other")


def test_dollar_anchor_and_wildcards():
    r = Robots("User-agent: *\nDisallow: /*.pdf$\nDisallow: /search*q=\n")
    assert not r.allowed("bot", "https://h/files/a.pdf")
    assert r.allowed("bot", "https://h/files/a.pdf?download=1")
    assert not r.allowed("bot", "https://h/search?x=1&q=boxes")
    assert r.allowed("bot", "https://h/searching")


def test_groups_and_agents():
    text = """
User-agent: GPTBot
User-agent: CCBot
Disallow: /

User-agent: *
Disallow: /private
Crawl-delay: 5

User-agent: gptbot
Allow: /public
"""
    r = Robots(text)
    assert not r.allowed("GPTBot", "https://h/page") and r.allowed("GPTBot", "https://h/public/x")  # groups merged
    assert not r.allowed("CCBot/2.0", "https://h/page")
    assert r.allowed("pathcrawl", "https://h/page") and not r.allowed("pathcrawl", "https://h/private/x")
    assert r.named("GPTBot") and not r.named("ClaudeBot")
    assert Robots("Disallow: /x\n").allowed("bot", "https://h/x")  # rules before any user-agent are ignored
    assert Robots("User-agent: *\nDisallow:\n").allowed("bot", "https://h/anything")
    assert Robots("User-agent: other\nDisallow: /\n").allowed("bot", "https://h/a")  # no group for us: allowed


def test_percent_encoding_is_compared_decoded():
    r = Robots("User-agent: *\nDisallow: /caf%C3%A9\n")
    assert not r.allowed("bot", "https://h/café/menu")
