"""Page data and link-region extraction.

Link extraction works on HTML text rather than a live browser page: the crawler
passes in the rendered DOM (``page.content()``), and the tests pass in the
fixture files. One implementation serves both, so the fixture tests exercise
exactly the region logic the crawler uses.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from pathcrawl.normalize import normalize_url

NAV, HEADER, FOOTER, BODY = "nav", "header", "footer", "body"
# Site chrome: links here are excluded in "content links only" mode.
CHROME_REGIONS = frozenset({NAV, HEADER, FOOTER})

# Per the HTML spec, <header>/<footer> only mean the page banner/footer when
# they are not inside one of these; an <article>'s own <header> is content.
_SECTIONING = frozenset({"article", "aside", "main", "nav", "section"})
_ROLE_REGIONS = {"navigation": NAV, "banner": HEADER, "contentinfo": FOOTER}


@dataclass(frozen=True)
class Link:
    href: str  # the raw href attribute
    url: str | None  # normalized absolute URL, or None if not crawlable (mailto:, etc.)
    text: str  # anchor text, falling back to aria-label / title / image alt
    region: str  # nav, header, footer or body


def _anchor_text(a: Tag) -> str:
    text = " ".join(a.get_text(" ", strip=True).split())
    if text:
        return text
    for attr in ("aria-label", "title"):
        if a.get(attr):
            return " ".join(str(a[attr]).split())
    img = a.find("img", alt=True)
    return " ".join(str(img["alt"]).split()) if img else ""


def _inside_sectioning(el: Tag) -> bool:
    return any(p.name in _SECTIONING for p in el.parents)


def _own_region(el: Tag, custom: dict[int, str]) -> str | None:
    """The region this element itself establishes, if any."""
    if id(el) in custom:
        return custom[id(el)]
    role = (el.get("role") or "").split()
    if role and role[0] in _ROLE_REGIONS:
        return _ROLE_REGIONS[role[0]]
    if el.name == "nav":
        return NAV
    if el.name in (HEADER, FOOTER) and not _inside_sectioning(el):
        return el.name
    return None


def link_region(a: Tag, custom: dict[int, str] | None = None) -> str:
    """Region of a link: the nearest ancestor (or the link itself) that is a
    nav, page header or page footer; otherwise body."""
    custom = custom or {}
    for el in (a, *a.parents):
        if not isinstance(el, Tag) or el.name == "[document]":
            break
        region = _own_region(el, custom)
        if region:
            return region
    return BODY


def extract_links(
    html: str,
    page_url: str,
    strip_params: Iterable[str] = (),
    region_selectors: dict[str, list[str]] | None = None,
) -> list[Link]:
    """Every ``<a href>`` on the page, in document order.

    ``region_selectors`` maps nav/header/footer to extra CSS selectors (from the
    client config) for menus that aren't built from semantic elements.
    """
    soup = BeautifulSoup(html, "html.parser")

    base = page_url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base = urljoin(page_url, str(base_tag["href"]))

    custom: dict[int, str] = {}
    for region in (NAV, HEADER, FOOTER):
        for selector in (region_selectors or {}).get(region, []):
            for el in soup.select(selector):
                custom.setdefault(id(el), region)

    strip = list(strip_params)
    links = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        links.append(
            Link(
                href=href,
                url=normalize_url(href, base=base, strip_params=strip),
                text=_anchor_text(a),
                region=link_region(a, custom),
            )
        )
    return links
