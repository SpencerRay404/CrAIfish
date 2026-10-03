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
import math
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pathcrawl.normalize import clean_tag, normalize_conversion_url

REQUIRED_COLUMNS = ("wt_mc_id", "leads_most_recent_tag")
OPTIONAL_COLUMNS = ("leads_source_initiative_tag", "paid_click_leads", "main_conversion_page")
# Column names that identify a person or visitor (a raw export), as opposed to
# aggregated counts such as distinct_visitors or leads_most_recent_tag.
IDENTIFIER_COLUMN = re.compile(
    r"mkto?_?trk|token|e_?mail|cookie|ip_?address|(?:^|_)(?:lead|visitor|person|contact|user)_?id(?:$|_)"
)
EMAIL_VALUE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _identifier_column(name: str) -> bool:
    return bool(IDENTIFIER_COLUMN.search(re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")))
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


class LeadTags(dict):
    """Lead counts per tag (``dict[str, TagLeads]``), plus the leads in rows
    with no tag (``untagged``), which can't be joined to any link."""

    untagged: float = 0.0


def load_lead_tags(paths: list[Path]) -> LeadTags:
    """Aggregated lead counts per tag, summed over every file. Tags are cleaned
    (whitespace and leading commas removed) before they are used."""
    out = LeadTags()
    for path in paths:
        try:
            f = open(path, newline="", encoding="utf-8-sig")
        except OSError as e:
            raise LeadFileError(f"cannot read lead file {path}: {e}") from None
        with f:
            reader = csv.DictReader(f)
            header = [h.strip() for h in reader.fieldnames or []]
            lowered = [h.lower() for h in header]
            bad = [h for h in header if _identifier_column(h)]
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
                if any(EMAIL_VALUE.match(v) for v in row.values()):
                    raise LeadFileError(f"{path} contains e-mail addresses; use the aggregated tag file only")
                tag = clean_tag(row.get("wt_mc_id"))
                where = f"{path.name} line {n}"
                if not tag:
                    out.untagged += _number(row.get("leads_most_recent_tag"), where)
                    continue
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
    leads_allocated: float  # the share, at full precision (same as share; kept for older readers)
    attribution: str  # exact (one source page) or shared
    share: float = 0.0  # this page's share of the tag's leads, full precision, for audit
    leads_whole: int = 0  # floor(share): what is shown to readers (a lead is a whole record)
    carries_tag: bool = True  # true even when leads_whole is 0

    def __post_init__(self) -> None:
        if not self.share and self.leads_allocated:
            self.share = self.leads_allocated
            self.leads_whole = math.floor(self.share + 1e-9)


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
        tag = clean_tag(r["mc_id"])
        if not tag:
            continue
        entry = out[tag][r["src"]]
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
                share=t.leads / n_src,
                leads_whole=math.floor(t.leads / n_src),
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
        "leads_total": sum(t.leads for t in result.lead_tags.values()),  # tagged leads
        "leads_untagged": getattr(result.lead_tags, "untagged", 0.0),
        "leads_all": sum(t.leads for t in result.lead_tags.values()) + getattr(result.lead_tags, "untagged", 0.0),
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


def lead_annotations(store, g) -> tuple[dict[str, dict], dict[tuple[str, str], int]]:
    """Node attributes and edge lead counts from the stored attribution, as
    whole leads (rounded down; a lead is a whole record):

    - ``leads_origin``: floor of the page's summed shares; ``leads_exact``: the
      same for its exact (single-carrier) shares; ``leads_share``: the summed
      shares at full precision, for audit; ``carries_lead_tag``: true for every
      page with a share, even when its whole count is 0.
    - ``leads_landed``: leads whose main conversion page is this page.
    - Edge ``leads``: floor of the shares on that page-to-target link.

    Empty when ``pathcrawl leads`` has not run.
    """
    share: dict[str, float] = defaultdict(float)
    exact: dict[str, float] = defaultdict(float)
    edges: dict[tuple[str, str], float] = defaultdict(float)
    for r in store.lead_attribution():
        value = r["share"] if r["share"] is not None else r["leads_allocated"]
        share[r["src"]] += value
        if r["attribution"] == "exact":
            exact[r["src"]] += value
        in_graph = [t for t in (r["targets"] or "").split() if t in g]
        for t in in_graph:
            edges[(r["src"], t)] += value / len(in_graph)
    nodes: dict[str, dict] = {
        n: {"leads_origin": _floor(v), "leads_exact": _floor(exact.get(n, 0.0)), "leads_share": round(v, 6),
            "carries_lead_tag": True, "leads_landed": 0}
        for n, v in share.items()
    }
    meta = store.meta("leads", {}) or {}
    if meta:
        by_lower = {n.lower(): n for n in g}
        for page, leads in (meta.get("conversion_pages") or {}).items():
            node = by_lower.get(page.lower())
            if node:
                nodes.setdefault(node, {"leads_origin": 0, "leads_exact": 0, "leads_share": 0.0,
                                        "carries_lead_tag": False, "leads_landed": 0})
                nodes[node]["leads_landed"] += int(round(leads))
    return nodes, {k: _floor(v) for k, v in edges.items()}


def _floor(v: float) -> int:
    """Round down, tolerating float noise (2.9999999 -> 3)."""
    return math.floor(v + 1e-9)


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
        f"{meta.get('lead_tags_matched_fallback', 0)} after dropping a numeric suffix. **They cover "
        f"{_n(matched)} of {_n(total)} tagged leads ({round(100 * matched / total) if total else 0}%).** "
        "Each tag's leads are split evenly over the pages carrying it: exact when one page carries the tag, "
        f"shared otherwise. Leads with a paid click ID: {_n(meta.get('paid_click_leads', 0))} "
        f"({round(100 * meta.get('paid_click_leads', 0) / total) if total else 0}%). Tags with fewer than "
        f"{min_cell} leads are rolled up in this report; full detail is in the run folder's lead_attribution CSV."
    )
    L.append("")
    L.append(f"- Tagged leads (a tag in the lead file): {_n(total)}")
    L.append(f"- Untagged leads (no tag, so they can't be joined to a link): {_n(meta.get('leads_untagged', 0))}")
    L.append(f"- All leads in the file: {_n(meta.get('leads_all', total))}")
    L.append("")

    origin: dict[str, dict[str, float]] = defaultdict(lambda: {"all": 0.0, "exact": 0.0})
    for r in rows:
        value = r["share"] if r["share"] is not None else r["leads_allocated"]
        origin[r["src"]]["all"] += value
        if r["attribution"] == "exact":
            origin[r["src"]]["exact"] += value
    exact_pages = sum(1 for v in origin.values() if v["exact"])
    exact_total = sum(v["exact"] for v in origin.values())
    allocated = math.fsum(v["all"] for v in origin.values())
    whole = {src: _floor(v["all"]) for src, v in origin.items()}
    whole_total = sum(whole.values())
    under_one = sum(1 for src in origin if whole[src] == 0)
    L.append(f"**{len(origin)} pages carry allocated leads** ({_n(round(allocated, 6))} in total); "
             f"{exact_pages} of them hold exact leads ({_n(_floor(exact_total))} whole leads from a single carrier), "
             f"the rest share a tag with other pages.")
    L.append("")
    L.append(f"- Leads are shown as whole numbers, rounded down per page (a lead is a whole record). Pages add to "
             f"**{whole_total} of {_n(round(allocated, 6))} allocated**; the gap of "
             f"{_n(round(allocated - whole_total, 6))} is the fractions lost to rounding down.")
    L.append(f"- {under_one} page{'s carry' if under_one != 1 else ' carries'} a tag with a share under one lead and show 0; "
             "they still count as pages where a lead was allocated.")
    L.append("")
    if origin:
        L.append("| page carrying the tag | leads (whole) | exact | shared |")
        L.append("|---|---|---|---|")
        for src, v in sorted(origin.items(), key=lambda kv: (-kv[1]["all"], kv[0]))[:15]:
            L.append(f"| {short(src)} | {whole[src]} | {_floor(v['exact'])} | {_floor(v['all'] - v['exact'])} |")
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
    zero = zero_lead_tags(store, g)
    unrewarded = pages_with_unrewarded_win_tags(store, g)
    L.append(f"**Tags that earned no leads:** {len(zero)} tags carried by crawled links have no leads in the lead "
             f"file ({sum(1 for z in zero if z.pages_linking_to_win)} of them on links to a win page); "
             f"{len(unrewarded)} pages carry a tagged link to a win page and earned no leads. "
             "List: the zero_lead_tags CSV.")
    L.append("")
    return L


# --------------------------------------------------------------------------- zero-lead tags


@dataclass
class ZeroLeadTag:
    tag: str
    pages_carrying: int
    pages_linking_to_win: int  # pages where the tag is on a link to a win page
    win_types: str


def zero_lead_tags(store, g) -> list[ZeroLeadTag]:
    """Tags carried by crawled links that earned no leads in the lead file."""
    meta = store.meta("leads", {}) or {}
    with_leads = {lt for t in meta.get("tags", []) if t.get("join") and t.get("leads", 0) > 0
                  for lt in t.get("link_tags", [])}
    carriers: dict[str, set[str]] = defaultdict(set)
    to_win: dict[str, set[str]] = defaultdict(set)
    types: dict[str, set[str]] = defaultdict(set)
    for r in store.db.execute("SELECT src, url, mc_id FROM links WHERE mc_id IS NOT NULL"):
        tag = clean_tag(r["mc_id"])
        if not tag or tag in with_leads:
            continue
        carriers[tag].add(r["src"])
        target = store.resolve(r["url"]) if r["url"] else None
        if target in g and g.nodes[target]["win"]:
            to_win[tag].add(r["src"])
            types[tag].add(g.nodes[target].get("win_type") or "win")
    return sorted((ZeroLeadTag(t, len(carriers[t]), len(to_win[t]), ", ".join(sorted(types[t])))
                   for t in carriers), key=lambda z: (-z.pages_linking_to_win, -z.pages_carrying, z.tag))


def pages_with_unrewarded_win_tags(store, g) -> list[str]:
    """Pages carrying a tagged link to a win page that earned no allocated leads."""
    earning = {r["src"] for r in store.lead_attribution()}
    pages = set()
    for r in store.db.execute("SELECT src, url FROM links WHERE mc_id IS NOT NULL AND url IS NOT NULL"):
        target = store.resolve(r["url"])
        if target in g and g.nodes[target]["win"] and r["src"] not in earning:
            pages.add(r["src"])
    return sorted(pages)


def write_zero_lead_csv(rows: list[ZeroLeadTag], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(ZeroLeadTag.__dataclass_fields__))
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))
