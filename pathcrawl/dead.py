"""Dead pages: pages that no longer exist, and every link that still points at them.

A loaded page is dead when it answers 404 or 410, or is a soft 404 served with
HTTP 200 (see ``pathcrawl.extract.detect_dead``). The crawler records
``pages.is_dead`` and ``dead_reason``; ``backfill_dead`` fills them for runs
crawled earlier, from the stored status, title and text.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from pathcrawl.extract import BODY, detect_dead


def backfill_dead(store) -> int:
    """Set is_dead and dead_reason on every loaded page. Returns the number of dead pages."""
    rows = store.db.execute(
        "SELECT url, http_status, title, body_text FROM pages WHERE status IN ('ok', 'http_error')"
    ).fetchall()
    updates = []
    for r in rows:
        reason = detect_dead(r["http_status"], r["title"], r["body_text"])
        updates.append((int(reason is not None), reason, r["url"]))
    with store.db:
        store.db.executemany("UPDATE pages SET is_dead = ?, dead_reason = ? WHERE url = ?", updates)
    return sum(u[0] for u in updates)


def dead_pages(store) -> dict[str, dict]:
    return {r["url"]: dict(r) for r in store.db.execute(
        "SELECT url, dead_reason, http_status, title FROM pages WHERE is_dead = 1 ORDER BY url")}


@dataclass
class DeadLink:
    dead_url: str
    dead_reason: str
    http_status: int | None
    title: str
    src: str
    src_external: bool
    region: str  # body, nav, header, footer, external
    link_text: str
    mc_id: str


def dead_links(store) -> list[DeadLink]:
    """Every link whose target (resolved through redirects) is a dead page."""
    dead = dead_pages(store)
    external = {r["url"] for r in store.db.execute("SELECT url FROM pages WHERE status = 'external'")}
    out = []
    for r in store.db.execute("SELECT src, url, text, region, mc_id FROM links WHERE url IS NOT NULL ORDER BY id"):
        target = store.resolve(r["url"])
        if target in dead and target != r["src"]:
            d = dead[target]
            out.append(DeadLink(target, d["dead_reason"] or "", d["http_status"], d["title"] or "", r["src"],
                                r["src"] in external, r["region"] or "", r["text"] or "", r["mc_id"] or ""))
    return sorted(out, key=lambda x: (x.dead_url, x.src, x.region))


def write_csv(rows: list[DeadLink], dead: dict[str, dict], path: Path) -> None:
    """One row per link into a dead page; a dead page nothing links to gets one row with no source."""
    fields = list(DeadLink.__dataclass_fields__)
    linked = {r.dead_url for r in rows}
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))
        for url, d in dead.items():
            if url not in linked:
                w.writerow({"dead_url": url, "dead_reason": d["dead_reason"], "http_status": d["http_status"],
                            "title": d["title"] or "", "src": "", "src_external": False, "region": "",
                            "link_text": "", "mc_id": ""})


def dead_annotations(store) -> tuple[dict[str, dict], dict[tuple[str, str], dict]]:
    """Node is_dead and inbound counts; edge to_dead."""
    dead = dead_pages(store)
    links = dead_links(store)
    nodes: dict[str, dict] = {u: {"is_dead": True, "dead_reason": d["dead_reason"] or "", "inbound_dead_links": 0,
                                  "dead_inbound_pages": 0, "dead_inbound_body_links": 0} for u, d in dead.items()}
    sources: dict[str, set[str]] = defaultdict(set)
    for lk in links:
        n = nodes[lk.dead_url]
        n["inbound_dead_links"] += 1
        if lk.region == BODY:
            n["dead_inbound_body_links"] += 1
        sources[lk.dead_url].add(lk.src)
    for u, srcs in sources.items():
        nodes[u]["dead_inbound_pages"] = len(srcs)
    edges = {(lk.src, lk.dead_url): {"to_dead": True} for lk in links}
    return nodes, edges


def dead_section(store, short) -> list[str]:
    dead = dead_pages(store)
    links = dead_links(store)
    L = ["## Dead pages", ""]
    if not dead:
        L += ["No crawled page is a dead page (404, 410 or a soft 404).", ""]
        return L
    hosts: dict[str, int] = defaultdict(int)
    for u in dead:
        hosts[u.split("/")[2] if "://" in u else u] += 1
    body = [lk for lk in links if lk.region == BODY]
    external = [lk for lk in links if lk.src_external]
    chrome = len(links) - len(body) - len(external)
    L.append(f"**{len(dead)} pages no longer exist** (" + ", ".join(f"{n} on {h}" for h, n in sorted(hosts.items()))
             + f"). {len(links)} links still point at them, from {len({lk.src for lk in links})} distinct pages: "
             f"{len(body)} in the page body, {chrome} in the nav, header or footer"
             + (f", {len(external)} from external posts" if external else "") + ". "
             f"{len({lk.dead_url for lk in body})} dead pages are linked from a body link. Full list: the dead_pages CSV.")
    L.append("")
    by_url: dict[str, list] = defaultdict(list)
    for lk in links:
        by_url[lk.dead_url].append(lk)
    L.append("| dead page | why | linked from | body / nav-header-footer links | lead tags on those links |")
    L.append("|---|---|---|---|---|")
    order = sorted(dead, key=lambda u: (-len(by_url[u]), u))
    for u in order[:40]:
        lks = by_url[u]
        srcs = sorted({lk.src for lk in lks})
        shown = ", ".join(short(s) for s in srcs[:3]) + (f" +{len(srcs) - 3} more" if len(srcs) > 3 else "")
        tags = sorted({lk.mc_id for lk in lks if lk.mc_id})
        n_body = sum(1 for lk in lks if lk.region == BODY)
        n_chrome = sum(1 for lk in lks if lk.region != BODY and not lk.src_external)
        L.append(f"| {short(u)} | {dead[u]['dead_reason']} | {shown or '(nothing crawled links here)'} | "
                 f"{n_body} / {n_chrome} | {', '.join(tags[:3]) + (' …' if len(tags) > 3 else '') or '-'} |")
    if len(order) > 40:
        L.append(f"| … {len(order) - 40} more in the dead_pages CSV | | | | |")
    if external:
        L.append("")
        L.append(f"**From external posts:** {len(external)} post link{'s land' if len(external) != 1 else ' lands'} on a "
                 "dead page: " + "; ".join(f"{short(lk.src)} → {short(lk.dead_url)}"
                                           + (f" (tag {lk.mc_id})" if lk.mc_id else "") for lk in external[:10]) + ".")
    L.append("")
    return L
