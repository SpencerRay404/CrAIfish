"""robots.txt rules as RFC 9309 (and Google, Bing) apply them.

Python's ``urllib.robotparser`` is not used: it ignores ``*`` and ``$`` in
paths and applies the first matching rule in file order, so a file that opens
with ``Allow: /`` and later says ``Disallow: /*?*`` (FedEx) would allow every
URL. Here:

- Groups: ``User-agent`` lines followed by rules. A crawler follows the group
  whose user-agent token matches its own name (case-insensitive); if none
  does, the ``*`` group; if there is none, everything is allowed. Several
  groups naming the same agent are merged.
- Each rule's path may use ``*`` (any characters) and a trailing ``$`` (end
  of URL). It is matched against the URL's path plus query, from the start.
- The longest matching rule wins; on a tie, ``Allow`` wins. An empty
  ``Disallow`` allows everything. ``/robots.txt`` is always allowed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import unquote, urlsplit


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, path pattern)


def parse(text: str) -> list[Group]:
    groups: list[Group] = []
    current: Group | None = None
    last_was_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if current is None or not last_was_agent:
                current = Group()
                groups.append(current)
            current.agents.append(value.lower())
            last_was_agent = True
        elif key in ("allow", "disallow"):
            last_was_agent = False
            if current is None:
                continue
            if key == "disallow" and not value:
                continue  # "Disallow:" with no path allows everything
            current.rules.append((key == "allow", value))
        elif key != "sitemap":  # other records (crawl-delay, ...) end a run of user-agent lines
            last_was_agent = False
    return groups


@lru_cache(maxsize=4096)
def _compile(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(rx + ("$" if anchored else ""))


class Robots:
    def __init__(self, text: str = "", allow_all: bool = False, disallow_all: bool = False):
        self.groups = parse(text) if text else []
        self.allow_all = allow_all
        self.disallow_all = disallow_all

    def rules_for(self, agent: str) -> list[tuple[bool, str]]:
        agent = agent.lower()
        named = [g for g in self.groups if any(a != "*" and a in agent for a in g.agents)]
        chosen = named or [g for g in self.groups if "*" in g.agents]
        return [r for g in chosen for r in g.rules]

    def named(self, agent: str) -> bool:
        agent = agent.lower()
        return any(a != "*" and a in agent for g in self.groups for a in g.agents)

    def allowed(self, agent: str, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        parts = urlsplit(url)
        path = parts.path or "/"
        if path == "/robots.txt":
            return True
        target = unquote(path) + (f"?{unquote(parts.query)}" if parts.query else "")
        best: tuple[int, bool] | None = None  # (pattern length, allow)
        for allow, pattern in self.rules_for(agent):
            if _compile(unquote(pattern)).match(target):
                key = (len(pattern), allow)
                if best is None or key > best:  # longer wins; on a tie, allow (True > False) wins
                    best = key
        return True if best is None else best[1]
