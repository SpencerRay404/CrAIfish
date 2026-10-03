"""Page data and link-region extraction.

Link extraction works on HTML text rather than a live browser page: the crawler
passes in the rendered DOM (``page.content()``), and the tests pass in the
fixture files. One implementation serves both, so the fixture tests exercise
exactly the region logic the crawler uses.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from pathcrawl.normalize import captured_params, normalize_url

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
    mc_id: str | None = None  # the lead-join tag: the first of scope.capture_params, from the raw href
    params: dict[str, str] | None = None  # every captured param present on the href


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
                mc_id=(found := captured_params(href, capture_params)).get(next(iter(capture_params), ""), None),
                params=found or None,
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
    # Machine-readability signals beyond JSON-LD (website health view)
    microdata_types: list[str] = field(default_factory=list)  # itemtype values (last path segment)
    rdfa_types: list[str] = field(default_factory=list)  # typeof values
    og_properties: list[str] = field(default_factory=list)  # og:* meta properties present
    hreflang: list[str] = field(default_factory=list)  # languages of <link rel=alternate hreflang>
    robots_meta: str | None = None  # content of <meta name=robots>
    # Content and service signals (peer comparison: the page_tags table)
    tags: dict | None = None


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
    jsonld: list[object] = []
    for script in soup.find_all("script", attrs={"type": lambda v: v and v.lower() == "application/ld+json"}):
        try:
            parsed = json.loads(script.string or "")
        except ValueError:
            types.append("(invalid JSON-LD)")
            continue
        jsonld.append(parsed)
        types.extend(_jsonld_types(parsed))

    form_present = bool(soup.select_one(form_selector)) if form_selector else None

    microdata = {_schema_name(str(t)) for el in soup.find_all(attrs={"itemtype": True})
                 for t in str(el["itemtype"]).split()}
    microdata |= {"(untyped)" for el in soup.find_all(attrs={"itemscope": True}) if not el.get("itemtype")}
    rdfa = {_schema_name(t) for el in soup.find_all(attrs={"typeof": True}) for t in str(el["typeof"]).split()}
    og = {str(m.get("property")).lower() for m in soup.find_all("meta", attrs={"property": True})
          if str(m.get("property")).lower().startswith("og:")}
    hreflang = set()
    for link in soup.find_all("link", attrs={"hreflang": True}):
        rel = link.get("rel") or []
        if "alternate" in [r.lower() for r in (rel if isinstance(rel, list) else [rel])]:
            hreflang.add(str(link["hreflang"]).strip().lower())
    robots = soup.find("meta", attrs={"name": lambda v: v and v.lower() == "robots"})
    tags = page_tags(soup, page_url, jsonld, sorted(set(types) | microdata | rdfa))

    return PageData(
        title=title,
        meta_description=description,
        headings=headings,
        canonical=canonical,
        jsonld_types=sorted(set(types)),
        form_present=form_present,
        text=visible_text(html),
        microdata_types=sorted(microdata),
        rdfa_types=sorted(rdfa),
        og_properties=sorted(og),
        hreflang=sorted(hreflang),
        robots_meta=" ".join(str(robots.get("content", "")).split()).lower() or None if robots else None,
        tags=tags,
    )


# --------------------------------------------------------------------------- page tags

SERVICE_TYPES = {"Service", "Product", "Offer", "FinancialProduct", "ProductModel"}
NAV_LABEL_LIMIT = 300


def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el is not None else ""


def _walk_jsonld(value: object):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from _walk_jsonld(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_jsonld(v)


def _types_of(node: dict) -> set[str]:
    t = node.get("@type")
    return {t} if isinstance(t, str) else {str(x) for x in t} if isinstance(t, list) else set()


def _position(item: dict) -> float:
    try:
        return float(item.get("position"))
    except (TypeError, ValueError):
        return float("inf")


def _jsonld_breadcrumb(jsonld: list[object]) -> list[str]:
    for node in (n for doc in jsonld for n in _walk_jsonld(doc)):
        if "BreadcrumbList" in _types_of(node):
            items = node.get("itemListElement") or []
            items = items if isinstance(items, list) else [items]
            out = []
            for it in sorted((i for i in items if isinstance(i, dict)), key=_position):
                name = it.get("name")
                if not name and isinstance(it.get("item"), dict):
                    name = it["item"].get("name")
                if name:
                    out.append(" ".join(str(name).split()))
            if out:
                return out
    return []


def _visible_breadcrumb(soup) -> list[str]:
    el = soup.find(attrs={"aria-label": lambda v: v and "breadcrumb" in v.lower()}) or soup.find(
        class_=lambda c: c and "breadcrumb" in (" ".join(c) if isinstance(c, list) else c).lower())
    if el is None:
        return []
    items = [_text(x) for x in el.find_all(["a", "li", "span"]) if not x.find(["a", "li"])]
    out = []
    for t in items:
        if t and t not in out and len(t) < 80:
            out.append(t)
    return out


def _service_entities(jsonld: list[object], soup) -> list[str]:
    names = []
    for node in (n for doc in jsonld for n in _walk_jsonld(doc)):
        types = _types_of(node)
        if types & SERVICE_TYPES and node.get("name"):
            names.append(" ".join(str(node["name"]).split()))
        if "FAQPage" in types:
            for q in node.get("mainEntity") or []:
                if isinstance(q, dict) and q.get("name"):
                    names.append(" ".join(str(q["name"]).split()))
    for el in soup.find_all(attrs={"itemtype": lambda v: v and any(t in v for t in SERVICE_TYPES)}):
        name = el.find(attrs={"itemprop": "name"})
        if name is not None and _text(name):
            names.append(_text(name))
    return list(dict.fromkeys(n for n in names if n))


def _parent_label(a) -> str:
    """The label of the menu group a navigation link sits in: the first
    heading, button or label-like child of its nearest list item or group
    that is not the link itself."""
    for anc in list(a.parents)[:6]:
        if not isinstance(anc, Tag) or anc.name in ("nav", "header", "footer", "body"):
            break
        for child in anc.find_all(["h2", "h3", "h4", "h5", "h6", "button", "span", "a", "p"], recursive=False):
            if child is a or a in child.descendants:
                continue
            label = _text(child)
            if label and len(label) < 60:
                return label
    return ""


def _nav_labels(soup) -> list[list[str]]:
    out, seen = [], set()
    for region in soup.find_all(["nav", "header", "footer"]) + soup.find_all(
            attrs={"role": lambda v: v in ("navigation", "banner", "contentinfo")}):
        for a in region.find_all("a", href=True):
            label = _text(a)
            if not label or len(label) > 80:
                continue
            pair = (_parent_label(a), label)
            if pair not in seen:
                seen.add(pair)
                out.append(list(pair))
            if len(out) >= NAV_LABEL_LIMIT:
                return out
    return out


def page_tags(soup, page_url: str, jsonld: list[object], schema_types: list[str]) -> dict:
    """What a page says about its content and services, beyond the body text."""
    from urllib.parse import urlsplit

    def meta(attr: str, value: str) -> list[str]:
        return [" ".join(str(m.get("content", "")).split()) for m in
                soup.find_all("meta", attrs={attr: lambda v: v and v.lower() == value}) if m.get("content")]

    h1 = next((_text(h) for h in soup.find_all("h1") if _text(h)), "")
    keywords = [k.strip() for m in meta("name", "keywords") for k in m.split(",") if k.strip()]
    return {
        "h1": h1,
        "url_path_segments": [s for s in urlsplit(page_url).path.split("/") if s],
        "breadcrumb": _jsonld_breadcrumb(jsonld) or _visible_breadcrumb(soup),
        "schema_types": schema_types,
        "og_type": (meta("property", "og:type") or [None])[0],
        "article_tags": meta("property", "article:tag"),
        "meta_keywords": keywords,
        "service_entities": _service_entities(jsonld, soup),
        "nav_labels": _nav_labels(soup),
    }


def _schema_name(t: str) -> str:
    """``https://schema.org/Article`` -> ``Article``; ``schema:Article`` -> ``Article``."""
    t = t.strip().rstrip("/")
    return t.rsplit("/", 1)[-1].rsplit(":", 1)[-1].rsplit("#", 1)[-1] or t


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


DEAD_STATUSES = (404, 410)
DEAD_TITLE_PHRASES = ("page not found",)
DEAD_TEXT_PHRASES = ("this page no longer exists",)


def detect_dead(http_status: int | None, title: str | None, text: str | None) -> str | None:
    """Why a page counts as gone, or None if it doesn't.

    HTTP 404 or 410; or a "soft 404" served with HTTP 200: a title starting
    with 404 or containing "Page Not Found", or body text saying the page no
    longer exists.
    """
    if http_status in DEAD_STATUSES:
        return f"HTTP {http_status}"
    t = (title or "").strip().lower()
    if t.startswith("404"):
        return "title starts with 404"
    for phrase in DEAD_TITLE_PHRASES:
        if phrase in t:
            return f"title says {phrase!r}"
    body = (text or "").lower()
    for phrase in DEAD_TEXT_PHRASES:
        if phrase in body:
            return f"page says {phrase!r}"
    return None


def h1_counts(headings) -> tuple[int, int]:
    """(non-empty H1s, empty H1s). Many templates emit an empty H1 next to the
    real one, so only H1s with text count as headings."""
    h1 = [text for level, text in headings if level == 1]
    empty = sum(1 for t in h1 if not (t or "").strip())
    return len(h1) - empty, empty
