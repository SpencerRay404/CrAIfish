"""Page data and link-region extraction.

Link extraction works on HTML text rather than a live browser page: the crawler
passes in the rendered DOM (``page.content()``), and the tests pass in the
fixture files. One implementation serves both, so the fixture tests exercise
exactly the region logic the crawler uses.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from pathcrawl.normalize import captured_param, normalize_url

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
    mc_id: str | None = None  # campaign tag from the raw href (scope.capture_params)


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
    capture_params: Iterable[str] = (),
) -> list[Link]:
    """Every ``<a href>`` on the page, in document order.

    ``region_selectors`` maps nav/header/footer to extra CSS selectors (from the
    client config) for menus that aren't built from semantic elements.
    ``capture_params`` names query params (e.g. ``WT.mc_id``) whose value is kept
    on the link as ``mc_id`` before normalization strips them from the URL.
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
                mc_id=captured_param(href, capture_params),
            )
        )
    return links


# --------------------------------------------------------------------------- page data

_INVISIBLE = ("script", "style", "noscript", "template", "svg")


def visible_text(html: str) -> str:
    """Human-readable text of a document, whitespace-collapsed.

    Used for both the raw HTTP response and the rendered DOM, so the two
    lengths are directly comparable for the render-dependency check.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_INVISIBLE):
        tag.decompose()
    root = soup.body or soup
    return " ".join(root.get_text(" ").split())


def _jsonld_types(value: object) -> list[str]:
    types: list[str] = []
    if isinstance(value, dict):
        t = value.get("@type")
        if isinstance(t, str):
            types.append(t)
        elif isinstance(t, list):
            types.extend(str(x) for x in t)
        for v in value.values():
            if isinstance(v, (dict, list)):
                types.extend(_jsonld_types(v))
    elif isinstance(value, list):
        for v in value:
            types.extend(_jsonld_types(v))
    return types


@dataclass
class PageData:
    title: str | None
    meta_description: str | None
    headings: list[tuple[int, str]]
    canonical: str | None
    jsonld_types: list[str]  # empty = no structured data found
    form_present: bool | None  # None when no form_selector is configured
    text: str


def extract_page(html: str, page_url: str, form_selector: str | None = None) -> PageData:
    soup = BeautifulSoup(html, "html.parser")

    title = " ".join(soup.title.get_text().split()) if soup.title else None
    meta = soup.find("meta", attrs={"name": lambda v: v and v.lower() == "description"})
    description = " ".join(str(meta.get("content", "")).split()) if meta else None

    headings = [
        (int(h.name[1]), " ".join(h.get_text(" ", strip=True).split()))
        for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    ]

    canonical = None
    for link in soup.find_all("link", href=True):
        rel = link.get("rel") or []
        if "canonical" in [r.lower() for r in (rel if isinstance(rel, list) else [rel])]:
            canonical = normalize_url(str(link["href"]), base=page_url)
            break

    types: list[str] = []
    for script in soup.find_all("script", attrs={"type": lambda v: v and v.lower() == "application/ld+json"}):
        try:
            types.extend(_jsonld_types(json.loads(script.string or "")))
        except ValueError:
            types.append("(invalid JSON-LD)")

    form_present = bool(soup.select_one(form_selector)) if form_selector else None

    return PageData(
        title=title,
        meta_description=description,
        headings=headings,
        canonical=canonical,
        jsonld_types=sorted(set(types)),
        form_present=form_present,
        text=visible_text(html),
    )


# A few phrases that bot walls and CAPTCHA interstitials reliably show.
_BLOCK_PHRASES = (
    "captcha",
    "are you a robot",
    "are you human",
    "verify you are human",
    "unusual traffic",
    "access denied",
    "request blocked",
    "just a moment",  # Cloudflare interstitial title
    "attention required",
    "pardon our interruption",
)


def detect_block(http_status: int | None, title: str | None, text: str) -> str | None:
    """Why this response looks like a block or bot wall, or None if it doesn't."""
    if http_status in (403, 429):
        return f"HTTP {http_status}"
    haystack = f"{title or ''} {text[:2000]}".lower()
    for phrase in _BLOCK_PHRASES:
        # Short pages only: a long article that merely mentions "captcha" is not a wall.
        if phrase in haystack and len(text) < 3000:
            return f"page says {phrase!r}"
    return None
