"""Compare sites crawled the same way: reachability of a win, overall
accessibility, machine readability, and content and service coverage.

Structure and content only. No traffic or lead data is read, so nothing here
says how well a site converts. Lead tables and campaign tags in a run (UPS)
are never read.

A comparison file (``configs/peers/compare.yaml``) lists the sites: each has
a run directory, the client config to read it with, and optionally a
``slice`` (regexes) that restricts which pages are counted. The UPS slice
re-cuts the UPS crawl to the same kind of scope as the peers. Distances are
always computed on the site's whole graph; the slice only chooses which pages
are reported, so the numbers match the UPS run for the same pages.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

import networkx as nx
import yaml

from pathcrawl.extract import BODY, h1_counts
from pathcrawl.graph import distances_to_win

LOADED = ("ok", "http_error")
SERVICE_GROUP = re.compile(r"servic|solution|product|shipping|logistic|freight|industr|capabilit", re.IGNORECASE)
CANNOT_TELL = (
    "This compares how the sites are built and what they say about themselves, from public pages only. It cannot "
    "tell us how well any site converts, how many visitors take any path, or how visitors arrive (search, ads, "
    "email): no traffic or lead data is held for the peers. Click counts follow links as written, not how people "
    "browse. Topic tags come from one frozen keyword list applied to titles, headings, breadcrumbs, menu labels "
    "and URLs, so they show what a site names, not the depth of what it offers. Service lists are a reading of "
    "each site's menus and structured data, as the site presents them, not verified product lists. Pages that "
    "robots.txt forbids, that sit behind a login or form, or that a host refused, are not counted."
)


# --------------------------------------------------------------------------- config


@dataclass
class SiteSpec:
    name: str
    run: Path | None
    config: Path | None = None
    slice: list[str] = field(default_factory=list)
    none_found: dict[str, list[str]] = field(default_factory=dict)  # class -> URLs checked, none found
    notes: list[str] = field(default_factory=list)


def load_compare(path: Path, runs: dict[str, str] | None = None) -> tuple[list[SiteSpec], Path]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    runs = {k.lower(): v for k, v in (runs or {}).items()}
    sites = []
    for s in data.get("sites", []):
        run = runs.get(s["name"].lower(), s.get("run"))
        sites.append(SiteSpec(
            name=s["name"],
            run=Path(run) if run else None,
            config=Path(s["config"]) if s.get("config") else None,
            slice=list(s.get("slice") or []),
            none_found=dict(s.get("none_found") or {}),
            notes=list(s.get("notes") or []),
        ))
    return sites, Path(data.get("taxonomy", "configs/topics.yaml"))


# --------------------------------------------------------------------------- per-site metrics


def pct(values: list[int], q: float) -> int | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    v = sorted(values)
    return v[max(0, math.ceil(q * len(v)) - 1)]


def dist_summary(values: list[int | None], total: int) -> dict:
    finite = [v for v in values if v is not None]
    return {
        "pages": total,
        "reached": len(finite),
        "no_route": total - len(finite),
        "no_route_share": round(100 * (total - len(finite)) / total, 1) if total else None,
        "p50": pct(finite, 0.5),
        "p90": pct(finite, 0.9),
        "max": max(finite) if finite else None,
        "distribution": dict(sorted(Counter(finite).items())),
    }


def _view(g: nx.DiGraph, body_only: bool):
    """Links as clickable: never out of a win (the journey ends there), and in
    body-only mode only links in the page body."""
    def ok(u, v):
        d = g.edges[u, v]
        if g.nodes[u]["win"] or not d["regions"]:
            return False
        return BODY in d["regions"] if body_only else True
    return nx.subgraph_view(g, filter_edge=ok)


def _bfs_from(h, start: str | None) -> dict[str, int]:
    if not start or start not in h:
        return {}
    return nx.single_source_shortest_path_length(h, start)


@dataclass
class SiteResult:
    name: str
    summary: dict
    pages: list[dict]  # one per counted page
    topics: dict[str, list[str]]
    services: list[dict]
    win_checks: dict
    notes: list[str]


def analyze_site(spec: SiteSpec, taxonomy: Path) -> SiteResult:
    from pathcrawl.backfill import backfill_page_tags
    from pathcrawl.categorize import categorize
    from pathcrawl.health import site_state
    from pathcrawl.report import crawl_coverage, health_home
    from pathcrawl.run import open_run
    from pathcrawl.topics import tag_run, topics_by_page

    run = open_run(spec.run, spec.config)
    try:
        store, g = run.store, run.graph
        backfill_page_tags(store)
        topic_meta = tag_run(store, taxonomy)
        page_topics = topics_by_page(store)
        tags = store.page_tags()
        rows = {r["url"]: r for r in store.pages()}
        cats = {c.url: c for c in categorize(store, g, run.analyze(), run.config.scope.locale_include)}
        slice_rx = [re.compile(p) for p in spec.slice]

        def counted(url: str) -> bool:
            r = rows.get(url)
            if not r or r["status"] not in LOADED or r["is_dead"]:
                return False
            return not slice_rx or any(rx.match(url) for rx in slice_rx)

        pages = sorted(u for u in g if counted(u))
        all_v, body_v = _view(g, False), _view(g, True)
        win_all, win_body = distances_to_win(all_v), distances_to_win(body_v)
        home = health_home(run)
        home_all, home_body = _bfs_from(all_v, home), _bfs_from(body_v, home)
        live = {u for u, r in rows.items() if r["status"] in LOADED and not r["is_dead"]}

        # first step on the shortest all-links path to a win, for pages 2+ clicks away
        link_text: dict[tuple[str, str], str] = {}
        for r in store.db.execute("SELECT src, url, text FROM links WHERE url IS NOT NULL ORDER BY id"):
            link_text.setdefault((r["src"], store.resolve(r["url"])), r["text"] or "")

        titles = Counter((rows[u]["title"] or "").strip().lower() for u in live if rows[u]["title"])
        metas = Counter((rows[u]["meta_description"] or "").strip().lower() for u in live if rows[u]["meta_description"])
        out_pages = []
        for u in pages:
            r = rows[u]
            d_all, d_body = win_all.get(u), win_body.get(u)
            first_step = ""
            if d_all is not None and d_all >= 2:
                nxt = next((v for v in sorted(all_v.successors(u)) if win_all.get(v) == d_all - 1), None)
                first_step = link_text.get((u, nxt), "") if nxt else ""
            headings = json.loads(r["headings"]) if r["headings"] else []
            jsonld = json.loads(r["jsonld_types"]) if r["jsonld_types"] else []
            micro = json.loads(r["microdata_types"]) if r["microdata_types"] else []
            rdfa = json.loads(r["rdfa_types"]) if r["rdfa_types"] else []
            raw, rendered = r["raw_text_len"], r["rendered_text_len"]
            c = cats.get(u)
            inbound_live = len({p for p in g.predecessors(u) if p in live})
            out_pages.append({
                "url": u,
                "section": c.section if c else "",
                "page_type": c.page_type if c else "",
                "clicks_to_win_all": d_all,
                "clicks_to_win_body": d_body,
                "menu_reliant": d_all is not None and (d_body is None or d_body - d_all >= 2),
                "first_step_text": first_step,
                "clicks_from_home_all": home_all.get(u),
                "clicks_from_home_body": home_body.get(u),
                "inbound_live": inbound_live,
                "structured_data": bool(jsonld or micro or rdfa),
                "readable_without_js": None if raw is None or not rendered else raw >= rendered / 2,
                "unique_title": bool(r["title"]) and titles[(r["title"] or "").strip().lower()] == 1,
                "unique_meta": bool(r["meta_description"])
                and metas[(r["meta_description"] or "").strip().lower()] == 1,
                "one_h1": h1_counts(headings)[0] == 1,
                "self_canonical": bool(r["canonical"]) and run.config.scope.normalize(r["canonical"]) == u,
                "topics": page_topics.get(u, []),
            })

        cov = crawl_coverage(run)
        n = len(out_pages)

        def share(key, test=bool):
            vals = [p[key] for p in out_pages if p[key] is not None]
            return round(100 * sum(1 for v in vals if test(v)) / len(vals), 1) if vals else None

        home_all_vals = [p["clicks_from_home_all"] for p in out_pages]
        content = [p for p in out_pages if p["page_type"] == "content"]
        product = [p for p in out_pages if p["page_type"] in ("product/service", "solutions", "service", "product")]

        def deep_share(ps):
            vals = [p["clicks_from_home_all"] for p in ps if p["clicks_from_home_all"] is not None]
            return round(100 * sum(1 for v in vals if v >= 4) / len(vals), 1) if vals else None

        site_signals = store.meta("site_signals") or {}
        summary = {
            "site": spec.name,
            "run": str(spec.run),
            "pages": n,
            "complete": cov["complete"],
            "queued_unvisited": cov["queued_unvisited"],
            "depth_note": "" if cov["complete"] else "incomplete, depth understated",
            "robots_skipped_by_host": cov["robots_skipped_by_host"],
            "robots_skipped_with_query_by_host": cov["robots_skipped_with_query_by_host"],
            "blocked_hosts": cov["blocked_hosts"],
            "pathcrawl_version": cov["pathcrawl_version"],
            "settings_fingerprint": cov["settings_fingerprint"],
            "taxonomy_version": topic_meta["version"],
            "taxonomy_fingerprint": topic_meta["fingerprint"],
            "home": home,
            "win_all": dist_summary([p["clicks_to_win_all"] for p in out_pages], n),
            "win_body": dist_summary([p["clicks_to_win_body"] for p in out_pages], n),
            "menu_reliance_share": share("menu_reliant"),
            "home_all": dist_summary(home_all_vals, n),
            "home_body": dist_summary([p["clicks_from_home_body"] for p in out_pages], n),
            "share_4plus_from_home": round(100 * sum(1 for v in home_all_vals if v is not None and v >= 4) / n, 1) if n else None,
            "share_5plus_from_home": round(100 * sum(1 for v in home_all_vals if v is not None and v >= 5) / n, 1) if n else None,
            "single_inbound_share": round(100 * sum(1 for p in out_pages if p["inbound_live"] == 1) / n, 1) if n else None,
            "deep_share_content": deep_share(content),
            "deep_share_product_service": deep_share(product),
            "health": {k: share(k) for k in ("structured_data", "readable_without_js", "unique_title", "unique_meta",
                                             "one_h1", "self_canonical")},
            "site_files": {h: {"robots": site_state(v, "robots"), "llms": site_state(v, "llms"),
                               "sitemap": site_state(v, "sitemap"),
                               "sitemap_not_crawled": v.get("sitemap_not_crawled"),
                               "ai_crawlers_blocked": sorted(b for b, x in v.get("ai_crawlers", {}).items()
                                                             if not x["allowed_home"]),
                               "detail": v.get("sitemap_detail") or v.get("robots_detail") or ""}
                           for h, v in site_signals.items()},
            "win_classes": _win_classes(g),
            "none_found": spec.none_found,
        }
        services = _services(tags, pages)
        return SiteResult(spec.name, summary, out_pages, {p["url"]: p["topics"] for p in out_pages}, services,
                          cov["win_checks"], spec.notes)
    finally:
        run.close()


def _win_classes(g) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for n, d in g.nodes(data=True):
        cls = d.get("conversion_class")
        if not cls:
            continue
        e = out.setdefault(cls, {"pages": 0, "linked": 0, "counts_as_win": bool(d["win"])})
        e["pages"] += 1
        e["linked"] += 1 if g.in_degree(n) else 0
    return out


def _services(tags: dict[str, dict], pages: list[str]) -> list[dict]:
    """Service offerings as the site presents them: menu labels under a
    services or solutions group, and Service or Product entities."""
    counted = set(pages)
    seen, out = set(), []
    for url, t in tags.items():
        for parent, label in t["nav_labels"]:
            if parent and SERVICE_GROUP.search(parent) and (parent, label) not in seen:
                seen.add((parent, label))
                out.append({"source": "menu", "group": parent, "label": label})
        if url in counted:
            for name in t["service_entities"]:
                if ("entity", name) not in seen and not name.endswith("?"):
                    seen.add(("entity", name))
                    out.append({"source": "structured data", "group": "", "label": name})
    return sorted(out, key=lambda r: (r["source"], r["group"].lower(), r["label"].lower()))


# --------------------------------------------------------------------------- outputs


def write_outputs(results: list[SiteResult], out: Path, taxonomy: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = {name: out / name for name in ("distance_to_win.csv", "depth_from_home.csv", "health.csv",
                                           "topic_matrix.csv", "services.csv", "summary.md", "summary.json")}
    with open(paths["distance_to_win.csv"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["site", "mode", "clicks", "pages", "complete"])
        for r in results:
            for mode in ("all", "body"):
                s = r.summary[f"win_{mode}"]
                for clicks, k in s["distribution"].items():
                    w.writerow([r.name, mode, clicks, k, r.summary["complete"]])
                w.writerow([r.name, mode, "no route", s["no_route"], r.summary["complete"]])
    with open(paths["depth_from_home.csv"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["site", "mode", "clicks", "pages", "complete", "note"])
        for r in results:
            for mode in ("all", "body"):
                s = r.summary[f"home_{mode}"]
                for clicks, k in s["distribution"].items():
                    w.writerow([r.name, mode, clicks, k, r.summary["complete"], r.summary["depth_note"]])
                w.writerow([r.name, mode, "no route", s["no_route"], r.summary["complete"], r.summary["depth_note"]])
    checks = ("structured_data", "readable_without_js", "unique_title", "unique_meta", "one_h1", "self_canonical")
    with open(paths["health.csv"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["site", "pages", *[f"{c}_pct" for c in checks]])
        for r in results:
            w.writerow([r.name, r.summary["pages"], *[r.summary["health"][c] for c in checks]])
    topics = sorted({t for r in results for ts in r.topics.values() for t in ts})
    matrix = []
    for r in results:
        by_topic: dict[str, list[dict]] = defaultdict(list)
        for p in r.pages:
            for t in p["topics"]:
                by_topic[t].append(p)
        for t in topics:
            ps = by_topic.get(t, [])
            home = [p["clicks_from_home_all"] for p in ps if p["clicks_from_home_all"] is not None]
            win = [p["clicks_to_win_all"] for p in ps if p["clicks_to_win_all"] is not None]
            matrix.append({"site": r.name, "topic": t, "pages": len(ps),
                           "share_pct": round(100 * len(ps) / r.summary["pages"], 1) if r.summary["pages"] else 0,
                           "median_clicks_from_home": median(home) if home else None,
                           "median_clicks_to_win": median(win) if win else None})
    with open(paths["topic_matrix.csv"], "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(matrix[0]) if matrix else ["site", "topic"])
        w.writeheader()
        w.writerows(matrix)
    with open(paths["services.csv"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["site", "source", "group", "label", "note"])
        for r in results:
            for s in r.services:
                w.writerow([r.name, s["source"], s["group"], s["label"], "as the site presents them"])
    paths["summary.md"].write_text(summary_markdown(results, matrix, taxonomy), encoding="utf-8")
    paths["summary.json"].write_text(json.dumps({
        "sites": [r.summary for r in results],
        "win_checks": {r.name: r.win_checks for r in results},
        "topic_matrix": matrix,
    }, indent=2, default=str), encoding="utf-8")
    return paths


def _v(x) -> str:
    if x is None:
        return "-"
    return str(int(x)) if isinstance(x, (int, float)) and float(x).is_integer() else str(x)


def summary_markdown(results: list[SiteResult], matrix: list[dict], taxonomy: Path) -> str:
    L = ["# Site comparison: structure and content", ""]
    versions = {(r.summary["pathcrawl_version"], r.summary["settings_fingerprint"]) for r in results}
    taxos = {r.summary["taxonomy_fingerprint"] for r in results}
    L.append(f"Sites: {', '.join(r.name for r in results)}. Topic taxonomy {taxonomy} "
             f"(version {results[0].summary['taxonomy_version'] if results else '-'}).")
    if len(versions) > 1:
        L.append("")
        L.append("**Warning:** the sites were not all crawled with the same crawler version and settings: "
                 + "; ".join(f"{r.name} {r.summary['pathcrawl_version']}/{r.summary['settings_fingerprint']}"
                             for r in results) + ". Numbers may not be comparable.")
    if len(taxos) > 1:
        L.append("")
        L.append("**Warning:** the sites were tagged with different topic taxonomies.")
    incomplete = [r.name for r in results if not r.summary["complete"]]
    if incomplete:
        L.append("")
        L.append(f"**Incomplete crawls, depth understated:** {', '.join(incomplete)}. Their depth figures are lower "
                 "bounds and should not be compared as-is.")
    L.append("")
    L.append("| | " + " | ".join(r.name for r in results) + " |")
    L.append("|---|" + "---|" * len(results))

    def row(label, fn):
        L.append(f"| {label} | " + " | ".join(fn(r.summary) for r in results) + " |")

    row("pages counted", lambda s: _v(s["pages"]))
    row("crawl complete (URLs still queued)", lambda s: f"{'yes' if s['complete'] else 'no'} ({s['queued_unvisited']})")
    row("clicks to nearest win, all links: p50 / p90 / max",
        lambda s: f"{_v(s['win_all']['p50'])} / {_v(s['win_all']['p90'])} / {_v(s['win_all']['max'])}")
    row("clicks to nearest win, body links: p50 / p90 / max",
        lambda s: f"{_v(s['win_body']['p50'])} / {_v(s['win_body']['p90'])} / {_v(s['win_body']['max'])}")
    row("no route to a win (all links)", lambda s: f"{_v(s['win_all']['no_route_share'])}%")
    row("menu reliance (body path 2+ clicks longer, or none)", lambda s: f"{_v(s['menu_reliance_share'])}%")
    row("clicks from home, all links: p50 / p90 / max",
        lambda s: f"{_v(s['home_all']['p50'])} / {_v(s['home_all']['p90'])} / {_v(s['home_all']['max'])}"
        + (" *" if s["depth_note"] else ""))
    row("clicks from home, body links: p50 / p90 / max",
        lambda s: f"{_v(s['home_body']['p50'])} / {_v(s['home_body']['p90'])} / {_v(s['home_body']['max'])}"
        + (" *" if s["depth_note"] else ""))
    row("4+ / 5+ clicks from home", lambda s: f"{_v(s['share_4plus_from_home'])}% / {_v(s['share_5plus_from_home'])}%")
    row("no route from home", lambda s: f"{_v(s['home_all']['no_route_share'])}%")
    row("exactly one inbound link", lambda s: f"{_v(s['single_inbound_share'])}%")
    row("4+ clicks: articles and resources / product and service",
        lambda s: f"{_v(s['deep_share_content'])}% / {_v(s['deep_share_product_service'])}%")
    for key, label in (("structured_data", "structured data"), ("readable_without_js", "readable without JavaScript"),
                       ("unique_title", "unique title"), ("unique_meta", "unique meta description"),
                       ("one_h1", "exactly one H1 with text"), ("self_canonical", "self-canonical")):
        row(label, lambda s, k=key: f"{_v(s['health'][k])}%")
    L.append("")
    if any(r.summary["depth_note"] for r in results):
        L.append("\\* incomplete crawl, depth understated.")
        L.append("")
    L.append("Full distributions: `distance_to_win.csv`, `depth_from_home.csv`. Compare whole distributions, not one "
             "median or maximum.")
    L.append("")

    L.append("## Win classes, robots.txt and site files")
    L.append("")
    for r in results:
        s = r.summary
        classes = "; ".join(f"{c}: {v['pages']} page{'s' if v['pages'] != 1 else ''}"
                            + ("" if v["counts_as_win"] else " (not counted as a win)")
                            for c, v in sorted(s["win_classes"].items())) or "no conversion page found"
        L.append(f"- **{r.name}**: {classes}.")
        for cls, urls in s["none_found"].items():
            L.append(f"  - {cls}: none found (checked: {', '.join(urls)}).")
        failed = [u for u, c in r.win_checks.items() if c.get("ok") is False]
        if failed:
            L.append(f"  - Win URLs that did not answer: {', '.join(failed[:5])}.")
        for host, n in sorted(s["robots_skipped_by_host"].items()):
            q = s["robots_skipped_with_query_by_host"].get(host, 0)
            L.append(f"  - robots.txt kept the crawl off {n} URLs on {host}" + (f" ({q} with a query string)" if q else "")
                     + ".")
        if s["blocked_hosts"]:
            L.append(f"  - Blocked (refused requests; not a finding about the site): {', '.join(s['blocked_hosts'])}.")
        for host, f in sorted(s["site_files"].items()):
            missed = f["sitemap_not_crawled"]
            sm = ("sitemap " + f["sitemap"] + (f" ({f['detail']})" if f["sitemap"] == "unreadable" and f["detail"] else "")
                  + (f", {missed} sitemap URLs the link crawl never reached" if missed is not None else ""))
            L.append(f"  - {host}: robots.txt {f['robots']}, llms.txt {f['llms']}, {sm}"
                     + (f"; AI crawlers blocked: {', '.join(f['ai_crawlers_blocked'])}" if f["ai_crawlers_blocked"] else "")
                     + ".")
        for note in r.notes:
            L.append(f"  - {note}")
    L.append("")

    L.append("## Topics: questions to ask")
    L.append("")
    base = results[0].name if results else ""
    share = {(m["site"], m["topic"]): m["share_pct"] for m in matrix}
    pages = {(m["site"], m["topic"]): m["pages"] for m in matrix}
    questions = []
    for m in matrix:
        if m["site"] == base:
            continue
        b = share.get((base, m["topic"]), 0)
        if m["pages"] >= 5 and m["share_pct"] >= 2 * max(b, 0.5):
            questions.append(f"{m['site']} gives {m['topic']} {m['share_pct']}% of its pages ({m['pages']}) against "
                             f"{b}% for {base}. Is that a gap in what {base} covers, or does {base} cover it elsewhere?")
    for t in sorted({m["topic"] for m in matrix}):
        if pages.get((base, t), 0) >= 5 and all(pages.get((r.name, t), 0) == 0 for r in results[1:]):
            questions.append(f"{base} has {pages[(base, t)]} pages on {t} and no peer page was tagged with it. Is it "
                             "a differentiator, or do peers name it differently?")
    L += [f"- {q}" for q in questions[:25]] or ["- No topic stood out."]
    L.append("")
    L.append("Topic by site: `topic_matrix.csv`. Services as each site presents them: `services.csv` (compare with "
             "the UPS list by hand).")
    L.append("")
    L.append("## What this cannot tell us")
    L.append("")
    L.append(CANNOT_TELL)
    L.append("")
    return "\n".join(L)


def compare(sites: list[SiteSpec], taxonomy: Path, out: Path) -> tuple[list[SiteResult], dict[str, Path]]:
    missing = [s.name for s in sites if not s.run or not (s.run / "crawl.db").exists()]
    if missing:
        raise FileNotFoundError(f"no run for: {', '.join(missing)} (set run: in the comparison file or pass --run NAME=DIR)")
    results = [analyze_site(s, taxonomy) for s in sites]
    return results, write_outputs(results, out, taxonomy)
