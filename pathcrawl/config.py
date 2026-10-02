"""Client config: pydantic models and the YAML loader.

Everything client-specific (domains, win page, campaigns, entry links) lives in
one YAML file per client. Nothing in the code may assume a particular client.

``load_config`` fails fast with a ``ConfigError`` whose message lists every
problem with its location in the file. ``config_warnings`` reports things that
are valid but probably not what the operator meant (placeholder values, a win
pattern on a host the crawl can never reach, duplicate entry links).
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import soupsieve
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pathcrawl.normalize import host_allowed, host_of, locale_allowed, normalize_url

PLACEHOLDER = "REPLACE-ME"
REGEX_PREFIX = "re:"

Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=64)]
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


class ConfigError(Exception):
    """The config file is missing, unparseable, or invalid."""


class _Strict(BaseModel):
    # Unknown keys are almost always typos; reject them instead of ignoring them.
    model_config = ConfigDict(extra="forbid")


class ClientConfig(_Strict):
    name: str = Field(min_length=1)
    slug: Slug


class RegionSelectors(_Strict):
    """Extra CSS selectors that mark a link's region.

    A link inside ``<nav>``/``<header>``/``<footer>`` (or the matching ARIA
    roles) is classified automatically. Use these for sites whose menus are
    plain ``<div>``s, e.g. ``nav: [".global-nav", "#mega-menu"]``.
    """

    nav: list[str] = []
    header: list[str] = []
    footer: list[str] = []

    @field_validator("nav", "header", "footer")
    @classmethod
    def _check_selectors(cls, selectors: list[str]) -> list[str]:
        for sel in selectors:
            try:
                soupsieve.compile(sel)
            except Exception as e:  # soupsieve raises SelectorSyntaxError and others
                raise ValueError(f"invalid CSS selector {sel!r}: {e}") from None
        return selectors


class ScopeConfig(_Strict):
    allowed_domains: list[str] = Field(min_length=1)
    locale_include: list[str] = []
    locale_exclude: list[str] = []
    # Hosts the locale filters apply to. Empty = every allowed domain. Use it
    # when only some hosts put the locale in the path (e.g. www.ups.com/us/en/
    # but solutions.ups.com/some-page.html).
    locale_hosts: list[str] = []
    strip_query_params: list[str] = []
    # Query params whose value is kept on each link (as links.mc_id) before it
    # is stripped for node identity, e.g. ["WT.mc_id"]. Names match
    # case-insensitively; the first one present on a link wins.
    capture_params: list[str] = []
    region_selectors: RegionSelectors = RegionSelectors()

    @field_validator("allowed_domains")
    @classmethod
    def _check_domains(cls, domains: list[str]) -> list[str]:
        cleaned = []
        for d in domains:
            host = d.strip().lower().rstrip(".")
            if not _HOST_RE.match(host):
                raise ValueError(
                    f"{d!r} is not a bare hostname; write e.g. 'www.example.com' "
                    "(no scheme, path, port or wildcard; list each subdomain explicitly)"
                )
            cleaned.append(host)
        return cleaned

    def normalize(self, url: str, base: str | None = None) -> str | None:
        return normalize_url(url, base=base, strip_params=self.strip_query_params)

    def domain_allowed(self, url: str) -> bool:
        return host_allowed(url, self.allowed_domains)

    @model_validator(mode="after")
    def _locale_hosts_are_allowed(self) -> ScopeConfig:
        self.locale_hosts = [h.strip().lower().rstrip(".") for h in self.locale_hosts]
        unknown = [h for h in self.locale_hosts if h not in self.allowed_domains]
        if unknown:
            raise ValueError(f"locale_hosts {unknown} must also be in allowed_domains")
        return self

    def locale_allowed(self, url: str) -> bool:
        if self.locale_hosts and host_of(url) not in self.locale_hosts:
            return True
        return locale_allowed(url, self.locale_include, self.locale_exclude)

    def in_scope(self, url: str) -> bool:
        """Allowed domain and passes the locale filters."""
        return self.domain_allowed(url) and self.locale_allowed(url)


class WinConfig(_Strict):
    name: str = Field(min_length=1)
    url_patterns: list[str] = Field(min_length=1)
    form_selector: str | None = None
    # When false (the default), a page matching url_patterns is a win even if
    # form_selector is not found; the report flags it as "form not rendered".
    require_form: bool = False
    # Words that make a URL "look like" the win. The report warns about pages
    # containing one that match no url_pattern (a variant the patterns miss).
    # Left empty, they are derived from the patterns (see keywords()).
    near_miss_keywords: list[str] = []

    @field_validator("url_patterns")
    @classmethod
    def _check_patterns(cls, patterns: list[str]) -> list[str]:
        for p in patterns:
            if p.startswith(REGEX_PREFIX):
                try:
                    re.compile(p[len(REGEX_PREFIX):])
                except re.error as e:
                    raise ValueError(f"invalid regex in {p!r}: {e}") from None
            elif not p.strip():
                raise ValueError("empty win pattern")
        return patterns

    @model_validator(mode="after")
    def _form_required_needs_selector(self) -> WinConfig:
        if self.require_form and not self.form_selector:
            raise ValueError("require_form is true but no form_selector is set")
        return self

    def url_matches(self, url: str) -> bool:
        """True if a normalized URL matches any win pattern.

        Plain patterns are globs matched against the whole URL (``*`` also
        matches ``/``). Patterns starting with ``re:`` are Python regexes that
        must match the whole URL.
        """
        for p in self.url_patterns:
            if p.startswith(REGEX_PREFIX):
                if re.fullmatch(p[len(REGEX_PREFIX):], url):
                    return True
            elif fnmatchcase(url, p):
                return True
        return False

    def keywords(self) -> list[str]:
        """Lowercase words that make a URL look like the win.

        ``near_miss_keywords`` if set; otherwise, for each glob pattern, the
        first two words of its last path segment before any wildcard, e.g.
        ``virtual-consultation`` from ``.../virtual-consultation-us-en*``
        (one word if that is all there is). Regex patterns contribute nothing.
        """
        if self.near_miss_keywords:
            return sorted({k.lower() for k in self.near_miss_keywords if k.strip()})
        out = set()
        for p in self.url_patterns:
            if p.startswith(REGEX_PREFIX):
                continue
            segment = re.split(r"[*?\[]", p, maxsplit=1)[0].rstrip("/").rsplit("/", 1)[-1].lower()
            segment = re.sub(r"\.[a-z0-9]+$", "", segment)
            words = list(re.finditer(r"[a-z0-9]{3,}", segment))
            if words:
                out.add(segment[words[0].start():words[min(1, len(words) - 1)].end()])
        return sorted(out)

    def near_miss(self, url: str) -> bool:
        """True if ``url`` looks like the win (a keyword in its path) but
        matches no url_pattern."""
        if self.url_matches(url):
            return False
        path = urlsplit(url).path.lower()
        return any(k in path for k in self.keywords())


class EntryLink(_Strict):
    label: str = Field(min_length=1)
    url: str = Field(min_length=1)


class CampaignConfig(_Strict):
    id: Slug
    name: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    ad_copy: str = Field(min_length=1)
    # Where the ad itself lives (e.g. LinkedIn posts). Recorded in the report,
    # never crawled: the crawl starts at entry_links.
    ad_urls: list[str] = []
    entry_links: list[EntryLink] = Field(min_length=1)


class CrawlConfig(_Strict):
    max_depth: int = Field(default=8, ge=1)  # clicks from an entry link
    max_pages: int = Field(default=300, ge=1)
    delay_ms: int = Field(default=1500, ge=0)
    headed: bool = True
    slow_mo_ms: int = Field(default=400, ge=0)
    respect_robots: bool = True
    page_timeout_ms: int = Field(default=30000, ge=1000)
    screenshot: bool = True


class LeadFile(_Strict):
    path: str = Field(min_length=1)  # relative to the directory pathcrawl runs in


class LeadJoin(_Strict):
    key: str = "wt_mc_id"  # matched case-sensitively against links.mc_id
    # strip_numeric_suffix: a lead tag with no exact match is also tried with a
    # trailing _NNNNN (5-7 digits) removed on both sides; none: exact only.
    fallback: str | None = "strip_numeric_suffix"

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if v != "wt_mc_id":
            raise ValueError("only 'wt_mc_id' is supported as the join key")
        return v

    @field_validator("fallback")
    @classmethod
    def _fallback(cls, v: str | None) -> str | None:
        if v not in (None, "none", "strip_numeric_suffix"):
            raise ValueError("fallback must be strip_numeric_suffix or none")
        return None if v == "none" else v


class LeadsConfig(_Strict):
    """Aggregated lead counts per campaign tag, joined to links.mc_id (pathcrawl leads)."""

    files: list[LeadFile] = []
    join: LeadJoin = LeadJoin()
    allocation: str = "even_split_across_source_pages"
    # report.md rolls tags with fewer leads than this into one line
    min_cell: int = Field(5, ge=1)

    @field_validator("allocation")
    @classmethod
    def _allocation(cls, v: str) -> str:
        if v != "even_split_across_source_pages":
            raise ValueError("only 'even_split_across_source_pages' is supported")
        return v


class Config(_Strict):
    client: ClientConfig
    scope: ScopeConfig
    win: WinConfig
    campaigns: list[CampaignConfig] = Field(min_length=1)
    crawl: CrawlConfig = CrawlConfig()
    leads: LeadsConfig = LeadsConfig()

    @model_validator(mode="after")
    def _check_campaigns(self) -> Config:
        problems: list[str] = []

        seen: set[str] = set()
        for c in self.campaigns:
            if c.id in seen:
                problems.append(f"campaign id {c.id!r} is used more than once")
            seen.add(c.id)

        for c in self.campaigns:
            for i, link in enumerate(c.entry_links):
                where = f"campaign {c.id!r} entry_links[{i}] ({link.label!r})"
                normalized = self.scope.normalize(link.url)
                if normalized is None:
                    problems.append(f"{where}: {link.url!r} is not an absolute http(s) URL")
                elif not self.scope.domain_allowed(normalized):
                    problems.append(
                        f"{where}: host {host_of(normalized)!r} is not in scope.allowed_domains. "
                        "Entry links must start on an allowed domain; if the ad uses a link "
                        "shortener, paste the destination URL instead."
                    )
                elif not self.scope.locale_allowed(normalized):
                    problems.append(
                        f"{where}: {link.url!r} is excluded by the locale filters "
                        "(scope.locale_include / scope.locale_exclude)"
                    )

        if problems:
            raise ValueError("\n".join(problems))
        return self

    def campaign(self, campaign_id: str) -> CampaignConfig:
        for c in self.campaigns:
            if c.id == campaign_id:
                return c
        known = ", ".join(c.id for c in self.campaigns)
        raise ConfigError(f"no campaign {campaign_id!r} in config (known: {known})")


def _format_validation_error(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"])
        msg = e["msg"].removeprefix("Value error, ")
        # Cross-field checks raise one error with a line per problem.
        for part in msg.splitlines():
            lines.append(f"  - {loc}: {part}" if loc else f"  - {part}")
    return "\n".join(lines)


def parse_config(data: object, source: str = "<config>") -> Config:
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a YAML mapping at the top level")
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"{source} is invalid:\n{_format_validation_error(e)}") from None


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e.strerror or e}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from None
    return parse_config(data, source=str(path))


def _placeholder_paths(value: object, path: str = "") -> list[str]:
    if isinstance(value, str):
        return [path] if PLACEHOLDER in value else []
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _placeholder_paths(v, f"{path}.{k}" if path else k)]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _placeholder_paths(v, f"{path}[{i}]")]
    return []


def blocking_placeholders(config: Config, campaign_id: str) -> list[str]:
    """Placeholders that would break a crawl of this campaign (the win and the
    campaign itself). Placeholders in other campaigns don't block it."""
    idx = next(i for i, c in enumerate(config.campaigns) if c.id == campaign_id)
    return [
        f"{p} still contains the placeholder {PLACEHOLDER!r}"
        for p in _placeholder_paths(config.model_dump())
        if p.startswith("win.") or p.startswith(f"campaigns[{idx}]")
    ]


def config_warnings(config: Config) -> list[str]:
    """Things that are valid but probably mistakes. Never fatal."""
    warnings = [
        f"{p} still contains the placeholder {PLACEHOLDER!r}"
        for p in _placeholder_paths(config.model_dump())
    ]

    for p in config.win.url_patterns:
        if p.startswith(REGEX_PREFIX):
            continue
        host = host_of(p)
        if host and not any(ch in host for ch in "*?[") and host not in config.scope.allowed_domains:
            warnings.append(
                f"win pattern {p!r} is on host {host!r}, which is not in scope.allowed_domains, "
                "so the crawl can never reach it"
            )

    for c in config.campaigns:
        seen: dict[str, str] = {}
        for link in c.entry_links:
            norm = config.scope.normalize(link.url)
            if norm in seen:
                warnings.append(
                    f"campaign {c.id!r}: entry links {seen[norm]!r} and {link.label!r} "
                    f"normalize to the same URL {norm}"
                )
            else:
                seen[norm] = link.label

    return warnings
