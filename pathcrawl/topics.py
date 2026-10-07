"""Shared topic tags for comparing sites (``configs/topics.yaml``).

Every site is tagged with the same frozen rules, from what a page says about
itself rather than its body text, in this order:

1. ``title``
2. ``h1``
3. ``breadcrumb``
4. ``nav_label``: the labels the site's own menus, header and footer give to
   links pointing at the page
5. ``url_path``: the path, with hyphens and underscores read as spaces

The first field that matches a topic is stored in ``page_topics`` with the
term that fired, so each tag can be checked. A page can have several topics.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path

from pathcrawl.entities import Taxonomy, compile_terms, load_taxonomy

FIELDS = ("title", "h1", "breadcrumb", "nav_label", "url_path")
DEFAULT_TAXONOMY = Path("configs/topics.yaml")


def taxonomy_fingerprint(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


def page_fields(store) -> dict[str, dict[str, str]]:
    """The text of each field for every loaded page that has page tags."""
    tags = store.page_tags()
    chrome_labels: dict[str, list[str]] = defaultdict(list)
    for r in store.db.execute(
            "SELECT url, text FROM links WHERE region IN ('nav', 'header', 'footer') AND url IS NOT NULL AND text != ''"):
        chrome_labels[store.resolve(r["url"])].append(r["text"])
    out = {}
    for url, t in tags.items():
        path = " ".join(re.sub(r"[-_.]+", " ", seg) for seg in t["url_path_segments"])
        path = re.sub(r"\b(html?|aspx?|php|page)\b", " ", path)
        out[url] = {
            "title": t["title"] or "",
            "h1": t["h1"] or "",
            "breadcrumb": " | ".join(t["breadcrumb"]),
            "nav_label": " | ".join(dict.fromkeys(chrome_labels.get(url, []))),
            "url_path": path,
        }
    return out


def tag_topics(fields: dict[str, dict[str, str]], taxonomy: Taxonomy) -> list[tuple[str, str, str, str]]:
    """(url, topic, field, rule) for every page and topic that matches."""
    rules = [(e.name, [(term, compile_terms([term])) for term in e.terms]) for e in taxonomy.entities]
    rows = []
    for url, f in sorted(fields.items()):
        for topic, terms in rules:
            hit = next(((field, term) for field in FIELDS for term, rx in terms if rx.search(f[field])), None)
            if hit:
                rows.append((url, topic, hit[0], hit[1]))
    return rows


def tag_run(store, taxonomy_path: Path = DEFAULT_TAXONOMY) -> dict:
    """Tag every page in the run and store the result; returns a summary."""
    from pathcrawl.store import now

    taxonomy = load_taxonomy(taxonomy_path)
    fields = page_fields(store)
    rows = tag_topics(fields, taxonomy)
    store.replace_page_topics(rows)
    summary = {
        "taxonomy": str(taxonomy_path),
        "version": taxonomy.version,
        "fingerprint": taxonomy_fingerprint(taxonomy_path),
        "tagged_at": now(),
        "pages": len(fields),
        "pages_tagged": len({r[0] for r in rows}),
        "tags": len(rows),
    }
    store.set_meta(topics=summary)
    return summary


def topics_by_page(store) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for r in store.page_topics():
        out[r["url"]].append(r["topic"])
    return dict(out)

