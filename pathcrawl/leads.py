"""Lead evidence: join aggregated lead counts per campaign tag to the links
that carry that tag (``links.mc_id``, see ``scope.capture_params``).

Input is an aggregated CSV per tag (counts only, made from a CRM export by a
separate one-off script). Columns used:

- ``wt_mc_id``: the join key, matched case-sensitively against ``links.mc_id``.
- ``leads_most_recent_tag``: the lead weight (last-touch reading of the tag).
- ``leads_source_initiative_tag``: a second weight, kept for reference.
- ``paid_click_leads``: leads that carried a paid-search click ID.
- ``main_conversion_page``: where most of that tag's leads converted.

Allocation (``even_split_across_source_pages``): a tag's leads are split
evenly over the distinct pages whose links carry it. With one such page the
attribution is ``exact``; with several it is ``shared``. Results go to the
``lead_attribution`` table and ``<client>_lead_attribution.csv``.

Privacy: the loader refuses anything that looks like a raw export (a visitor
token column or ``token:`` values), nothing here prints individual lead-file
rows, and report.md rolls tags under ``leads.min_cell`` into one line.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pathcrawl.normalize import normalize_conversion_url

REQUIRED_COLUMNS = ("wt_mc_id", "leads_most_recent_tag")
OPTIONAL_COLUMNS = ("leads_source_initiative_tag", "paid_click_leads", "main_conversion_page")
FORBIDDEN_COLUMN_MARKERS = ("mkt_trk", "token", "email", "lead_id", "leadid", "visitor")
NUMERIC_SUFFIX = re.compile(r"_\d{5,7}$")
OTHER = "(other, <{n} leads)"


class LeadFileError(Exception):
    pass


@dataclass
class TagLeads:
    tag: str
    leads: float = 0.0  # leads_most_recent_tag
    initiative_leads: float = 0.0
    paid_click_leads: float = 0.0
    conversion_pages: dict[str, float] = field(default_factory=dict)  # normalized page -> leads

    @property
    def main_conversion_page(self) -> str | None:
        if not self.conversion_pages:
            return None
        return max(sorted(self.conversion_pages), key=lambda p: self.conversion_pages[p])


def _number(value: str | None, where: str) -> float:
    value = (value or "").strip().replace(",", "")
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        raise LeadFileError(f"{where}: {value!r} is not a number") from None


def load_lead_tags(paths: list[Path]) -> dict[str, TagLeads]:
    """Aggregated lead counts per tag, summed over every file."""
    out: dict[str, TagLeads] = {}
    for path in paths:
        try:
            f = open(path, newline="", encoding="utf-8-sig")
        except OSError as e:
            raise LeadFileError(f"cannot read lead file {path}: {e}") from None
        with f:
            reader = csv.DictReader(f)
            header = [h.strip() for h in reader.fieldnames or []]
            lowered = [h.lower() for h in header]
            bad = [h for h, low in zip(header, lowered) if any(m in low for m in FORBIDDEN_COLUMN_MARKERS)]
            if bad:
                raise LeadFileError(
                    f"{path} has column(s) {bad} that look like a raw lead export. Only the aggregated "
                    "per-tag counts may be used; build them with a separate script and leave the raw export "
                    "out of the repo."
                )
            missing = [c for c in REQUIRED_COLUMNS if c not in lowered]
            if missing:
                raise LeadFileError(f"{path} is missing column(s): {', '.join(missing)}")
            for n, raw in enumerate(reader, start=2):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
                if any(v.lower().startswith("token:") for v in row.values()):
                    raise LeadFileError(f"{path} contains visitor tokens; use the aggregated tag file only")
                tag = row.get("wt_mc_id", "")
                if not tag:
                    continue
                where = f"{path.name} line {n}"
                t = out.setdefault(tag, TagLeads(tag))
                leads = _number(row.get("leads_most_recent_tag"), where)
                t.leads += leads
                t.initiative_leads += _number(row.get("leads_source_initiative_tag"), where)
                t.paid_click_leads += _number(row.get("paid_click_leads"), where)
                page = normalize_conversion_url(row.get("main_conversion_page"))
                if page:
                    t.conversion_pages[page] = t.conversion_pages.get(page, 0.0) + leads
    return out


# --------------------------------------------------------------------------- attribution


@dataclass
class Attribution:
    src: str
    mc_id: str
    lead_tag: str
    join: str  # exact or fallback (strip_numeric_suffix)
    targets: str  # space-separated link targets (resolved through redirects)
    region: str  # comma-separated regions the tagged links sit in
    tag_leads_total: float
    tag_source_pages: int
    leads_allocated: float
    attribution: str  # exact (one source page) or shared


@dataclass
class TagMatch:
    lead_tag: str
    join: str | None  # exact, fallback, or None when no crawled link carries it
    link_tags: list[str]
    leads: float
    source_pages: int
    main_conversion_page: str | None
    targets: list[str]
    lands_on_target: bool | None  # main conversion page is one of the tagged links' targets


@dataclass
class LeadResult:
    rows: list[Attribution]
    tags: list[TagMatch]
    lead_tags: dict[str, TagLeads]


def _strip_suffix(tag: str) -> str:
    return NUMERIC_SUFFIX.sub("", tag)


def tagged_links(store) -> dict[str, dict[str, dict[str, set[str]]]]:
    """mc_id -> src -> {"targets": {...}, "regions": {...}} for every tagged link."""
    out: dict[str, dict[str, dict[str, set[str]]]] = defaultdict(lambda: defaultdict(lambda: {"targets": set(), "regions": set()}))
    for r in store.db.execute("SELECT src, url, href, region, mc_id FROM links WHERE mc_id IS NOT NULL"):
        entry = out[r["mc_id"]][r["src"]]
        target = store.resolve(r["url"]) if r["url"] else (r["href"] or "")
        if target:
            entry["targets"].add(target)
        if r["region"]:
            entry["regions"].add(r["region"])
    return out


def attribute(store, lead_tags: dict[str, TagLeads], fallback: str | None = "strip_numeric_suffix") -> LeadResult:
    links = tagged_links(store)
    by_stripped: dict[str, list[str]] = defaultdict(list)
    for t in links:
        by_stripped[_strip_suffix(t)].append(t)

    rows, tags = [], []
    for lead_tag in sorted(lead_tags):
        t = lead_tags[lead_tag]
        if lead_tag in links:
            join, link_tags = "exact", [lead_tag]
        elif fallback == "strip_numeric_suffix" and by_stripped.get(_strip_suffix(lead_tag)):
            join, link_tags = "fallback", sorted(by_stripped[_strip_suffix(lead_tag)])
        else:
            tags.append(TagMatch(lead_tag, None, [], t.leads, 0, t.main_conversion_page, [], None))
            continue
        sources: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"targets": set(), "regions": set(), "tags": set()})
        for lt in link_tags:
            for src, e in links[lt].items():
                sources[src]["targets"] |= e["targets"]
                sources[src]["regions"] |= e["regions"]
                sources[src]["tags"].add(lt)
        n_src = len(sources)
        all_targets = sorted({x for e in sources.values() for x in e["targets"]})
        main = t.main_conversion_page
        lands = None if main is None else main in {normalize_conversion_url(x) for x in all_targets}
        tags.append(TagMatch(lead_tag, join, link_tags, t.leads, n_src, main, all_targets, lands))
        for src in sorted(sources):
            e = sources[src]
            rows.append(Attribution(
                src=src,
                mc_id=", ".join(sorted(e["tags"])),
                lead_tag=lead_tag,
                join=join,
                targets=" ".join(sorted(e["targets"])),
                region=",".join(sorted(e["regions"])),
                tag_leads_total=t.leads,
                tag_source_pages=n_src,
                leads_allocated=t.leads / n_src,
                attribution="exact" if n_src == 1 else "shared",
            ))
    return LeadResult(rows, tags, lead_tags)


def save(store, result: LeadResult, files: list[str]) -> None:
    from pathcrawl.store import now

    store.replace_lead_attribution(result.rows)
    matched = [t for t in result.tags if t.join]
    store.set_meta(leads={
        "files": files,
        "joined_at": now(),
        "lead_tags": len(result.lead_tags),
        "lead_tags_matched_exact": sum(1 for t in matched if t.join == "exact"),
        "lead_tags_matched_fallback": sum(1 for t in matched if t.join == "fallback"),
        "leads_total": sum(t.leads for t in result.lead_tags.values()),
        "leads_matched": sum(t.leads for t in matched),
        "paid_click_leads": sum(t.paid_click_leads for t in result.lead_tags.values()),
        # per-tag outcome without counts below the line: kept for the report
        "tags": [asdict(t) for t in result.tags],
        "conversion_pages": _conversion_totals(result.lead_tags),
    })


def _conversion_totals(lead_tags: dict[str, TagLeads]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for t in lead_tags.values():
        for page, n in t.conversion_pages.items():
            out[page] += n
    return dict(sorted(out.items()))


def write_csv(rows, path: Path) -> None:
    fields = list(Attribution.__dataclass_fields__)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            d = r if isinstance(r, dict) else asdict(r)
            w.writerow({k: d[k] for k in fields})


def run_leads(store, config, run_dir: Path) -> tuple[LeadResult, Path]:
    """Load the configured lead files, attribute, store and write the CSV."""
    files = [Path(f.path) for f in config.leads.files]
    result = attribute(store, load_lead_tags(files), config.leads.join.fallback)
    save(store, result, [str(f) for f in files])
    out = run_dir / f"{config.client.slug}_lead_attribution.csv"
    write_csv(sorted(result.rows, key=lambda r: (-r.leads_allocated, r.src, r.lead_tag)), out)
    return result, out


# --------------------------------------------------------------------------- graph annotations


def lead_annotations(store, g) -> tuple[dict[str, dict], dict[tuple[str, str], float]]:
    """Node attributes (leads_origin, leads_exact, leads_landed) and edge leads
    from the stored attribution. Empty when ``pathcrawl leads`` has not run."""
    nodes: dict[str, dict] = defaultdict(lambda: {"leads_origin": 0.0, "leads_exact": 0.0, "leads_landed": 0.0})
    edges: dict[tuple[str, str], float] = defaultdict(float)
    rows = store.lead_attribution()
    for r in rows:
        n = nodes[r["src"]]
        n["leads_origin"] += r["leads_allocated"]
        if r["attribution"] == "exact":
            n["leads_exact"] += r["leads_allocated"]
        targets = [t for t in (r["targets"] or "").split() if t]
        in_graph = [t for t in targets if t in g]
        for t in in_graph:
            edges[(r["src"], t)] += r["leads_allocated"] / len(in_graph)
    meta = store.meta("leads", {}) or {}
    if meta:
        by_lower = {n.lower(): n for n in g}
        for page, leads in (meta.get("conversion_pages") or {}).items():
            node = by_lower.get(page.lower())
            if node:
                nodes[node]["leads_landed"] += leads
    return {k: {a: round(v, 3) for a, v in d.items()} for k, d in nodes.items()}, {k: round(v, 3) for k, v in edges.items()}


# --------------------------------------------------------------------------- report


def _rollup(items: list[tuple[str, float]], min_cell: int) -> list[tuple[str, float]]:
    shown = [(k, v) for k, v in items if v >= min_cell]
    small = [v for _, v in items if v < min_cell]
    if small:
        shown.append((OTHER.format(n=min_cell) + f" ×{len(small)}", sum(small)))
    return shown


def _n(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def lead_section(store, g, min_cell: int, short) -> list[str]:
    meta = store.meta("leads", {}) or {}
    rows = store.lead_attribution()
    if not meta:
        return []
    tags = [TagMatch(**t) for t in meta.get("tags", [])]
    total, matched = meta.get("leads_total", 0), meta.get("leads_matched", 0)
    L = ["## Lead evidence", ""]
    L.append(
        f"{meta.get('lead_tags', 0)} tags in the lead file ({', '.join(Path(f).name for f in meta.get('files', []))}); "
        f"{meta.get('lead_tags_matched_exact', 0)} match a crawled link tag exactly and "
        f"{meta.get('lead_tags_matched_fallback', 0)} after dropping a numeric suffix. They cover "
        f"{_n(matched)} of {_n(total)} tagged leads ({round(100 * matched / total) if total else 0}%). "
        "Each tag's leads are split evenly over the pages carrying it: exact when one page carries the tag, "
        f"shared otherwise. Leads with a paid click ID: {_n(meta.get('paid_click_leads', 0))} "
        f"({round(100 * meta.get('paid_click_leads', 0) / total) if total else 0}%). Tags with fewer than "
        f"{min_cell} leads are rolled up in this report; full detail is in the run folder's lead_attribution CSV."
    )
    L.append("")

    origin: dict[str, dict[str, float]] = defaultdict(lambda: {"all": 0.0, "exact": 0.0})
    for r in rows:
        origin[r["src"]]["all"] += r["leads_allocated"]
        if r["attribution"] == "exact":
            origin[r["src"]]["exact"] += r["leads_allocated"]
    exact_pages = sum(1 for v in origin.values() if v["exact"])
    exact_total = sum(v["exact"] for v in origin.values())
    allocated = sum(v["all"] for v in origin.values())
    L.append(f"**{len(origin)} pages carry allocated leads** ({_n(round(allocated, 1))} in total); "
             f"{exact_pages} of them hold exact leads ({_n(round(exact_total, 1))}, "
             f"{round(100 * exact_total / allocated) if allocated else 0}% of the allocated total).")
    L.append("")
    if origin:
        L.append("| page carrying the tag | leads allocated | exact | shared |")
        L.append("|---|---|---|---|")
        for src, v in sorted(origin.items(), key=lambda kv: (-kv[1]["all"], kv[0]))[:15]:
            L.append(f"| {short(src)} | {_n(round(v['all'], 1))} | {_n(round(v['exact'], 1))} | "
                     f"{_n(round(v['all'] - v['exact'], 1))} |")
        L.append("")

    off_target = [t for t in tags if t.join and t.lands_on_target is False]
    if off_target:
        n = sum(t.leads for t in off_target)
        L.append(f"**Landed elsewhere:** {len(off_target)} matched tag{'s' if len(off_target) != 1 else ''} "
                 f"({_n(n)} leads) mostly converted on a page other than the one the tagged link points at. "
                 "They are kept in the totals:")
        for name, v in _rollup([(t.lead_tag, t.leads) for t in off_target], min_cell):
            t = next((x for x in off_target if x.lead_tag == name), None)
            where = f": links point at {', '.join(short(x) for x in t.targets[:2])}, converted on {t.main_conversion_page}" if t else ""
            L.append(f"- {name} ({_n(v)} leads){where}")
        L.append("")

    unmatched = [t for t in tags if not t.join and t.leads > 0]
    if unmatched:
        L.append(f"**Tags with leads that no crawled link carries** ({len(unmatched)} tags, "
                 f"{_n(sum(t.leads for t in unmatched))} leads; ads, email or pages outside the crawl): "
                 + ", ".join(f"{k} ({_n(v)})" for k, v in _rollup(
                     sorted(((t.lead_tag, t.leads) for t in unmatched), key=lambda kv: (-kv[1], kv[0])), min_cell)))
        L.append("")

    win_type = {n: d.get("win_type") or "win" for n, d in g.nodes(data=True) if d["win"]}
    with_leads = {lt for t in tags if t.join for lt in t.link_tags}
    types = sorted(set(win_type.values()))
    for wtype in [None, *types] if len(types) > 1 else [None]:
        wins = {n for n, t in win_type.items() if wtype is None or t == wtype}
        linking: dict[str, bool] = {}
        win_tags: set[str] = set()
        for r in store.db.execute("SELECT src, url, mc_id FROM links WHERE url IS NOT NULL"):
            if store.resolve(r["url"]) in wins and r["src"] in g and g.nodes[r["src"]]["explored"]:
                linking[r["src"]] = linking.get(r["src"], False) or bool(r["mc_id"])
                if r["mc_id"]:
                    win_tags.add(r["mc_id"])
        untagged = sorted(p for p, tagged in linking.items() if not tagged)
        target = "a win page" if wtype is None else f"a {wtype} page"
        L.append(f"- **Crawled pages linking to {target}:** {len(linking)}; {len(linking) - len(untagged)} carry a "
                 "tag on that link." + (" Without a tag: " + ", ".join(short(p) for p in untagged[:20]) + "."
                                        if untagged else "")
                 + f" Tags on those links: {len(win_tags)}; with leads: {len(win_tags & with_leads)}, without leads "
                 f"in the lead file: {len(win_tags - with_leads)}.")
    L.append("")
    return L
