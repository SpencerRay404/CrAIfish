"""URL normalization and scope checks.

Every URL the crawler sees passes through ``normalize_url`` before it becomes a
graph node, so that trivially different spellings of the same page (tracking
params, fragments, host case, default ports, param order) merge into one node.

This module deliberately takes plain arguments rather than config objects so it
has no dependencies inside the package and is easy to test in isolation.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

CRAWLABLE_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}


def param_is_stripped(name: str, patterns: Iterable[str]) -> bool:
    """True if a query param name matches any strip pattern.

    Patterns are shell-style globs (``utm_*``) and matching is case-insensitive,
    so ``UTM_Source`` is stripped by ``utm_*``.
    """
    lowered = name.lower()
    return any(fnmatchcase(lowered, p.lower()) for p in patterns)


def normalize_url(
    url: str,
    base: str | None = None,
    strip_params: Iterable[str] = (),
) -> str | None:
    """Return the canonical node form of ``url``, or None if it is not crawlable.

    - Relative URLs are resolved against ``base``.
    - Only http(s) URLs are crawlable; ``mailto:``, ``tel:``, ``javascript:``
      and friends return None.
    - Scheme and host are lowercased, a trailing dot on the host is dropped,
      and default ports (80/443) are removed.
    - An empty path becomes ``/``. Path case and trailing slashes are kept,
      because servers are free to treat those as different pages.
    - The fragment is dropped.
    - Query params matching ``strip_params`` are removed; the rest are sorted
      (stable, so repeated keys keep their relative order) so param order does
      not create duplicate nodes.
    """
    url = (url or "").strip()
    if not url:
        return None
    if base:
        url = urljoin(base, url)

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in CRAWLABLE_SCHEMES:
        return None

    host = (parts.hostname or "").rstrip(".")
    if not host:
        return None
    if ":" in host:  # IPv6 literal: urlsplit strips the brackets
        host = f"[{host}]"
    netloc = host if port is None or port == DEFAULT_PORTS[scheme] else f"{host}:{port}"

    path = parts.path or "/"

    strip = list(strip_params)
    params = [
        (k, v)
        for k, v in ((_unescape_key(k), v) for k, v in parse_qsl(parts.query, keep_blank_values=True))
        if not param_is_stripped(k, strip)
    ]
    params.sort(key=lambda kv: kv[0])
    query = urlencode(params)

    return urlunsplit((scheme, netloc, path, query, ""))


def clean_tag(value: str | None) -> str:
    """A campaign tag without surrounding whitespace or leading commas or
    semicolons (``", ONLINE_X_1"`` -> ``"ONLINE_X_1"``), which otherwise stop it
    matching the same tag elsewhere."""
    return (value or "").strip().lstrip(",; ").strip()


def _unescape_key(key: str) -> str:
    """``amp;gclsrc`` -> ``gclsrc``: a query written with a literal ``&amp;``
    (HTML-escaped twice) still names the same param."""
    while key.lower().startswith("amp;"):
        key = key[4:]
    return key


def captured_params(href: str | None, names: Iterable[str]) -> dict[str, str]:
    """Every param in ``names`` present on ``href`` (case-insensitive), keyed by
    the name as configured. Empty values count as absent."""
    names = list(names)
    if not href or not names:
        return {}
    try:
        query = urlsplit(href.strip()).query
    except ValueError:
        return {}
    present: dict[str, str] = {}
    for k, v in parse_qsl(query, keep_blank_values=False):
        k, v = _unescape_key(k).lower(), clean_tag(v)
        if v and k not in present:
            present[k] = v
    return {n: present[n.lower()] for n in names if n.lower() in present}


def captured_param(href: str | None, names: Iterable[str]) -> str | None:
    """The value of the first query param in ``href`` whose name matches one of
    ``names`` (case-insensitive), read from the raw href before normalization.

    Used to keep a campaign tag such as ``WT.mc_id`` on a link even though the
    param is stripped from the URL for node identity. Empty values count as absent.
    """
    found = captured_params(href, names)
    return next((found[n] for n in names if n in found), None)


def normalize_conversion_url(url: str | None) -> str | None:
    """Normalize a conversion-page URL from a CRM or lead export for matching.

    Drops the query and fragment and lowercases host and path (exports spell
    the same page in different cases). Returns None for empty values, the
    literal ``null`` and landing-page editor previews (``lpeditor``,
    ``devicePreview``). A missing scheme is taken as https.
    """
    url = (url or "").strip()
    if not url or url.lower() in ("null", "none", "nan"):
        return None
    if "://" not in url:
        url = "https://" + url.lstrip("/")
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").rstrip(".").lower()
    path = (parts.path or "/").lower()
    if not host or "lpeditor" in path or "devicepreview" in path:
        return None
    scheme = parts.scheme.lower() if parts.scheme.lower() in CRAWLABLE_SCHEMES else "https"
    return urlunsplit((scheme, host, path, "", ""))


def host_of(url: str) -> str:
    """Lowercased host of ``url`` (empty string if there is none)."""
    try:
        return (urlsplit(url).hostname or "").rstrip(".")
    except ValueError:
        return ""


def host_allowed(url: str, allowed_domains: Iterable[str]) -> bool:
    """True if the URL's host is exactly one of ``allowed_domains``.

    Subdomains are not implied: ``www.example.com`` does not allow
    ``shop.example.com``. List each host explicitly.
    """
    host = host_of(url)
    return bool(host) and host in {d.lower().rstrip(".") for d in allowed_domains}


def locale_allowed(url: str, include: Iterable[str] = (), exclude: Iterable[str] = ()) -> bool:
    """Apply the locale path filters to ``url``.

    Filters are case-insensitive substrings of the URL path. If ``include`` is
    non-empty the path must contain at least one of them; the path must contain
    none of ``exclude``.
    """
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return False
    include = [s.lower() for s in include]
    if include and not any(s in path for s in include):
        return False
    return not any(s.lower() in path for s in exclude)
