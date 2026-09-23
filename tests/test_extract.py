import copy

import pytest

from pathcrawl.config import ConfigError, parse_config
from pathcrawl.extract import extract_links

PAGE = "https://x.test/dir/page.html"


def regions(html, selectors=None):
    return {link.text: link.region for link in extract_links(html, PAGE, region_selectors=selectors)}


def test_semantic_regions():
    html = """
    <header><a href="/logo">Logo</a><nav><a href="/menu">Menu</a></nav></header>
    <main><p><a href="/story">Story</a></p></main>
    <footer><a href="/legal">Legal</a></footer>
    """
    assert regions(html) == {"Logo": "header", "Menu": "nav", "Story": "body", "Legal": "footer"}


def test_aria_roles_count_as_regions():
    html = """
    <div role="banner"><a href="/a">A</a></div>
    <div role="navigation"><a href="/b">B</a></div>
    <div role="contentinfo"><a href="/c">C</a></div>
    <div role="main"><a href="/d">D</a></div>
    """
    assert regions(html) == {"A": "header", "B": "nav", "C": "footer", "D": "body"}


def test_article_header_and_footer_are_content():
    """An <article>'s own <header>/<footer> is part of the content, not page chrome."""
    html = """
    <main><article>
      <header><a href="/author">Author</a></header>
      <p>Text</p>
      <footer><a href="/related">Related</a></footer>
    </article></main>
    """
    assert regions(html) == {"Author": "body", "Related": "body"}


def test_custom_region_selectors():
    html = """
    <div class="global-nav"><ul><li><a href="/a">Products</a></li></ul></div>
    <div id="site-foot"><a href="/b">Careers</a></div>
    <div class="content"><a href="/c">Read more</a></div>
    """
    selectors = {"nav": [".global-nav"], "footer": ["#site-foot"]}
    assert regions(html, selectors) == {"Products": "nav", "Careers": "footer", "Read more": "body"}


def test_nearest_region_wins():
    html = '<footer><nav><a href="/a">Sitemap</a></nav><a href="/b">Privacy</a></footer>'
    assert regions(html) == {"Sitemap": "nav", "Privacy": "footer"}


def test_urls_are_resolved_normalized_and_filtered():
    html = """
    <a href="next.html#top">Next</a>
    <a href="/abs?utm_source=x&id=1">Abs</a>
    <a href="mailto:a@b.test">Mail</a>
    <a href="javascript:void(0)">JS</a>
    <a>No href</a>
    """
    links = extract_links(html, PAGE, strip_params=["utm_*"])
    assert [(link.text, link.url) for link in links] == [
        ("Next", "https://x.test/dir/next.html"),
        ("Abs", "https://x.test/abs?id=1"),
        ("Mail", None),
        ("JS", None),
    ]
    assert links[0].href == "next.html#top"


def test_base_tag_changes_resolution():
    html = '<head><base href="https://x.test/other/"></head><body><a href="p.html">P</a></body>'
    assert extract_links(html, PAGE)[0].url == "https://x.test/other/p.html"


def test_anchor_text_fallbacks():
    html = """
    <a href="/1">  Multi
        line   text </a>
    <a href="/2" aria-label="Close menu"><svg></svg></a>
    <a href="/3" title="Home page"></a>
    <a href="/4"><img src="x.png" alt="Company logo"></a>
    <a href="/5"></a>
    """
    assert [link.text for link in extract_links(html, PAGE)] == [
        "Multi line text", "Close menu", "Home page", "Company logo", "",
    ]


def test_invalid_region_selector_rejected_by_config():
    data = {
        "client": {"name": "A", "slug": "a"},
        "scope": {"allowed_domains": ["x.test"], "region_selectors": {"nav": ["div[unclosed"]}},
        "win": {"name": "w", "url_patterns": ["https://x.test/win"]},
        "campaigns": [
            {"id": "c", "name": "c", "platform": "p", "ad_copy": "c",
             "entry_links": [{"label": "l", "url": "https://x.test/"}]}
        ],
    }
    with pytest.raises(ConfigError, match="invalid CSS selector"):
        parse_config(data)
    ok = copy.deepcopy(data)
    ok["scope"]["region_selectors"]["nav"] = [".global-nav > ul"]
    parse_config(ok)
