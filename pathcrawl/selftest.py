"""Self-test against the fixture site: expected vs actual for every metric.

This is the hands-on test gate. ``pathcrawl selftest`` runs every stage built
so far against ``tests/fixtures/site`` and compares the results with the
hand-worked numbers in ``tests/fixtures/site/expected.yaml``. The same check
runs in pytest and CI.

Stages:
- ``graph``: read the fixture HTML files directly, extract links, build the
  graph and run every metric. No browser needed.
- ``crawl``: serve the fixture site on localhost, crawl it with the real
  browser crawler, rebuild the graph from the crawl database, and check the
  same numbers. This is the end-to-end gate.
- ``report``: write every report file for that crawl, read them back, and
  check that the report's numbers and each page's category are right.
"""

from __future__ import annotations

import contextlib
import functools
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import networkx as nx
import yaml

from pathcrawl.config import Config, parse_config
from pathcrawl.extract import extract_links
from pathcrawl.graph import MODES, Analysis, Edge, EntryPoint, Page, analyze, build_graph

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_SITE = REPO_ROOT / "tests" / "fixtures" / "site"
FIXTURE_BASE = "http://fixture.test/"


def fixture_config(base: str = FIXTURE_BASE, entries: dict[str, str] | None = None) -> Config:
    """A client config for the fixture site served at ``base``."""
    host = base.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
    entries = entries or load_expected()["entries"]
    return parse_config(
        {
            "client": {"name": "Fixture", "slug": "fixture"},
            "scope": {"allowed_domains": [host], "strip_query_params": ["utm_*"]},
            "win": {
                "name": "Contact sales",
                "url_patterns": [base + "win.html"],
                "form_selector": "form#contact-sales",
            },
            "campaigns": [
                {
                    "id": "fixture",
                    "name": "Fixture campaign",
                    "platform": "test",
                    "ad_copy": "Fixture ad copy.",
                    "entry_links": [{"label": label, "url": f"{base}{stem}.html"} for label, stem in entries.items()],
                }
            ],
        }
    )


def load_expected(site: Path = FIXTURE_SITE) -> dict[str, Any]:
    path = site / "expected.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"fixture site not found at {site}; selftest must run from a checkout of the repo"
        )
    return yaml.safe_load(path.read_text())


def graph_from_html_files(cfg: Config, site: Path = FIXTURE_SITE, base: str = FIXTURE_BASE) -> nx.DiGraph:
    """Build the graph straight from the fixture's HTML files, the way the
    crawler will from rendered pages: extract links, keep in-scope ones,
    mark wins by URL pattern."""
    pages, edges = [], []
    for f in sorted(site.glob("*.html")):
        url = cfg.scope.normalize(base + f.name)
        win = cfg.win.url_matches(url)
        pages.append(Page(url=url, win=win, win_source="pattern" if win else None))
        for link in extract_links(
            f.read_text(), url, cfg.scope.strip_query_params, cfg.scope.region_selectors.model_dump()
        ):
            if link.url and cfg.scope.in_scope(link.url):
                edges.append(Edge(url, link.url, link.region))
    return build_graph(pages, edges)


def entry_points(cfg: Config) -> list[EntryPoint]:
    return [EntryPoint(link.label, cfg.scope.normalize(link.url)) for link in cfg.campaigns[0].entry_links]


# --------------------------------------------------------------------------- comparison


@dataclass
class Check:
    stage: str
    mode: str
    metric: str
    expected: Any
    actual: Any

    @property
    def ok(self) -> bool:
        return self.expected == self.actual


def _stem(url: str | None, base: str) -> str | None:
    if url is None:
        return None
    return url.removeprefix(base).removesuffix(".html")


def compare(analysis: Analysis, expected: dict[str, Any], base: str, stage: str, crawled_pages: int) -> list[Check]:
    """Every expected value side by side with what the analysis produced."""

    def stems(urls):
        return [_stem(u, base) for u in urls] if urls is not None else None

    checks = [Check(stage, "-", "crawled pages", expected["crawled_pages"], crawled_pages)]
    for mode in MODES:
        exp = expected["modes"][mode]
        res = analysis.modes[mode]
        by_label = {e.label: e for e in res.entries}

        def add(metric: str, want: Any, got: Any) -> None:
            checks.append(Check(stage, mode, metric, want, got))

        add("reachable pages", exp["reachable_pages"], res.reachable_pages)
        for label in expected["entries"]:
            e = by_label[label]
            add(f"shortest path ({label})", exp["shortest_path"][label], stems(e.shortest_path))
            add(f"longest simple path ({label})", exp["longest_path"][label], stems(e.longest_path))
            add(f"dead zone hit at click ({label})", exp["dead_zone_click"][label], e.dead_zone_click)
        add("worst-case clicks to win", exp["worst_case_max_clicks"], res.worst_case.max_clicks)
        add("distance distribution", exp["distance_distribution"], res.worst_case.distribution)
        add("dead ends", exp["dead_ends"], stems(res.dead_zones.dead_ends))
        add("dead ends % of crawled", exp["dead_end_pct"], res.dead_zones.dead_end_pct)
        add("trap loops", exp["trap_loops"], [stems(loop) for loop in res.dead_zones.trap_loops])
        add("entry links converge", exp["converged"], res.convergence.converged)
        overlap = res.convergence.pairwise_overlap
        add("path overlap (Jaccard)", exp["overlap_jaccard"], overlap[0]["jaccard"] if overlap else None)
    return checks


def run_graph_stage(site: Path = FIXTURE_SITE) -> list[Check]:
    """Stage 'graph': fixture HTML -> links -> graph -> metrics, no browser."""
    expected = load_expected(site)
    cfg = fixture_config(FIXTURE_BASE, expected["entries"])
    g = graph_from_html_files(cfg, site, FIXTURE_BASE)
    analysis = analyze(g, entry_points(cfg), max_depth=expected["max_depth"])
    crawled = sum(1 for _, d in g.nodes(data=True) if d["explored"])
    return compare(analysis, expected, FIXTURE_BASE, "graph", crawled)


# --------------------------------------------------------------------------- crawl stage


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - signature fixed by the base class
        pass


@contextlib.contextmanager
def serve_directory(directory: Path) -> Iterator[str]:
    """Serve ``directory`` on a free localhost port; yields the base URL."""
    handler = functools.partial(_QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def run_crawl_and_report_stages(site: Path = FIXTURE_SITE, headed: bool = False, console=None,
                                with_report: bool = True) -> list[Check]:
    """Stages 'crawl' and 'report' on one crawl (the report stage reads that crawl's files)."""
    with tempfile.TemporaryDirectory(prefix="pathcrawl-selftest-") as tmp:
        checks, base = _crawl(site, headed, Path(tmp), console)
        if with_report:
            checks += run_report_stage(Path(tmp), base, site)
        return checks


def run_report_stage(run_dir: Path, base: str, site: Path = FIXTURE_SITE) -> list[Check]:
    """Stage 'report': write the report files for a fixture crawl and check them."""
    import csv
    import json

    from pathcrawl.report import write_report
    from pathcrawl.run import open_run

    expected = load_expected(site)
    run = open_run(run_dir)
    paths = write_report(run)
    run.close()
    checks = [Check("report", "-", f"{name} written", True, p.exists() and p.stat().st_size > 0) for name, p in paths.items()]

    data = json.loads(paths["report.json"].read_text())
    for mode in MODES:
        exp, got = expected["modes"][mode], data["analysis"]["modes"][mode]
        by_label = {e["label"]: e for e in got["entries"]}
        for label in expected["entries"]:
            want = len(exp["shortest_path"][label]) - 1
            checks.append(Check("report", mode, f"report.json shortest clicks ({label})", want, by_label[label]["shortest_clicks"]))
        checks.append(Check("report", mode, "report.json dead ends", len(exp["dead_ends"]), got["dead_zones"]["dead_end_count"]))
        checks.append(Check("report", mode, "report.json trap loops", len(exp["trap_loops"]), len(got["dead_zones"]["trap_loops"])))

    with open(paths["categories.csv"], newline="") as f:
        rows = {_stem(r["url"], base): r["reach_content_only"] for r in csv.DictReader(f)}
    for stem, want in expected["categories_content_only"].items():
        checks.append(Check("report", "content_only", f"category of {stem}", want, rows.get(stem)))
    mmd = paths["paths.mmd"].read_text()
    checks.append(Check("report", "-", "paths.mmd marks the win and a dead zone",
                        True, "class " in mmd and " win" in mmd and " dead" in mmd))
    return checks


def run_crawl_stage(site: Path = FIXTURE_SITE, headed: bool = False, run_dir: Path | None = None, console=None) -> list[Check]:
    """Stage 'crawl': the real crawler against the served fixture site."""
    with contextlib.ExitStack() as stack:
        if run_dir is None:
            run_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="pathcrawl-selftest-")))
        return _crawl(site, headed, run_dir, console)[0]


def _crawl(site: Path, headed: bool, run_dir: Path, console) -> tuple[list[Check], str]:
    """Crawl the served fixture site into ``run_dir``; returns the checks and the base URL."""
    from rich.console import Console

    from pathcrawl.crawler import Crawler, NonInteractiveOperator
    from pathcrawl.graph import graph_from_store

    import yaml as _yaml

    expected = load_expected(site)
    with serve_directory(site) as base:
        cfg = fixture_config(base, expected["entries"])
        (run_dir / "config.yaml").write_text(_yaml.safe_dump(cfg.model_dump()))
        cfg.crawl.delay_ms = 0
        cfg.crawl.slow_mo_ms = 250 if headed else 0
        crawler = Crawler(
            cfg, cfg.campaigns[0], run_dir, NonInteractiveOperator(),
            console=console or Console(quiet=True), headed=headed,
        )
        crawler.run()
        g, entries = graph_from_store(crawler.store)
        crawled = crawler.store.explored_count()
        crawler.store.close()
        analysis = analyze(g, entries, max_depth=expected["max_depth"])
        return compare(analysis, expected, base, "crawl", crawled), base
