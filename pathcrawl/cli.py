"""Command-line entry point: ``pathcrawl``."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from pathcrawl.config import ConfigError, blocking_placeholders, config_warnings, load_config

app = typer.Typer(help="Measure how reachable a conversion goal is from a campaign's entry links.", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)


@app.callback()
def main() -> None:
    """Campaign path crawler."""


@app.command()
def validate(
    config: Path = typer.Option(..., "--config", "-c", help="Client config YAML."),
) -> None:
    """Check a client config and summarize what a crawl would do."""
    try:
        cfg = load_config(config)
    except ConfigError as e:
        err_console.print(f"[bold red]Config error[/]\n{escape(str(e))}", highlight=False)
        raise typer.Exit(code=1) from None

    console.print(f"[bold green]✓[/] {escape(str(config))} is valid: client [bold]{escape(cfg.client.name)}[/] ({cfg.client.slug})")
    console.print(f"  Allowed domains: {', '.join(cfg.scope.allowed_domains)}")
    if cfg.scope.locale_include or cfg.scope.locale_exclude:
        console.print(
            escape(f"  Locale include: {cfg.scope.locale_include or '-'}  exclude: {cfg.scope.locale_exclude or '-'}")
        )
    console.print(f"  Win: [bold]{escape(cfg.win.name)}[/] <- {escape(', '.join(cfg.win.url_patterns))}", highlight=False)

    table = Table(title="Campaigns", show_lines=False)
    table.add_column("id")
    table.add_column("name")
    table.add_column("platform")
    table.add_column("entry links", justify="right")
    for c in cfg.campaigns:
        table.add_row(c.id, escape(c.name), escape(c.platform), str(len(c.entry_links)))
    console.print(table)

    for w in config_warnings(cfg):
        console.print(f"[yellow]warning:[/] {escape(w)}", highlight=False)


def _show(value: object, is_path: bool = False) -> str:
    """Compact display for the selftest table: paths as a → b → c, lists as a, b."""
    if value is None:
        return "none"
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return (" → " if is_path else ", ").join(value)
    if isinstance(value, list) and value and all(isinstance(v, list) for v in value):
        return "; ".join("[" + ", ".join(v) + "]" for v in value)
    if value == []:
        return "none"
    return str(value)


@app.command()
def selftest(
    stage: str = typer.Option("all", help="Which stage to run: all, graph, crawl or report."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser during the crawl stage."),
) -> None:
    """Check every stage against the fixture site's hand-worked numbers (exit 1 on any mismatch)."""
    from pathcrawl.selftest import FIXTURE_SITE, run_crawl_and_report_stages, run_graph_stage

    if stage not in ("all", "graph", "crawl", "report"):
        err_console.print("[bold red]selftest:[/] --stage must be all, graph, crawl or report")
        raise typer.Exit(code=2)

    checks = []
    try:
        if stage in ("all", "graph"):
            checks += run_graph_stage()
        if stage in ("all", "crawl", "report"):
            console.print("[dim]crawl stage: serving the fixture site locally and crawling it"
                          + (" in a visible browser" if headed else "") + "...[/]")
            checks += run_crawl_and_report_stages(headed=headed, console=console, with_report=stage != "crawl")
    except FileNotFoundError as e:
        err_console.print(f"[bold red]selftest:[/] {escape(str(e))}")
        raise typer.Exit(code=2) from None
    except Exception as e:
        if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
            err_console.print("[bold red]selftest:[/] Chromium is not installed. Run: playwright install chromium")
            raise typer.Exit(code=2) from None
        raise

    site = FIXTURE_SITE.relative_to(Path.cwd()) if FIXTURE_SITE.is_relative_to(Path.cwd()) else FIXTURE_SITE
    table = Table(title=f"Fixture site selftest ({site})")
    table.add_column("", width=1)
    table.add_column("stage")
    table.add_column("mode")
    table.add_column("metric")
    table.add_column("expected")
    table.add_column("actual")
    for c in checks:
        mark = "[green]✓[/]" if c.ok else "[bold red]✗[/]"
        is_path = "path" in c.metric and "overlap" not in c.metric
        actual = escape(_show(c.actual, is_path))
        expected = escape(_show(c.expected, is_path))
        table.add_row(mark, c.stage, c.mode, escape(c.metric), expected, actual if c.ok else f"[red]{actual}[/]")
    console.print(table)

    failed = [c for c in checks if not c.ok]
    if failed:
        console.print(f"[bold red]FAIL[/]: {len(failed)} of {len(checks)} checks did not match.")
        raise typer.Exit(code=1)
    console.print(f"[bold green]PASS[/]: all {len(checks)} checks match the expected numbers.")


@app.command()
def crawl(
    config: Path | None = typer.Option(None, "--config", "-c", help="Client config YAML."),
    campaign: str | None = typer.Option(None, "--campaign", help="Campaign id from the config."),
    headless: bool = typer.Option(False, "--headless", help="Hide the browser (default: visible)."),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Never pause for the operator: accept loaded pages, skip failed ones."
    ),
    resume: Path | None = typer.Option(None, "--resume", help="Resume an unfinished run directory."),
    runs_dir: Path = typer.Option(Path("runs"), "--runs-dir", help="Where new run directories go."),
    max_pages: int | None = typer.Option(None, "--max-pages", min=1, help="Override crawl.max_pages for this run."),
    max_depth: int | None = typer.Option(None, "--max-depth", min=1, help="Override crawl.max_depth for this run."),
) -> None:
    """Crawl a campaign from its entry links."""
    from pathcrawl.crawler import Crawler, NonInteractiveOperator, TerminalOperator, new_run_dir, snapshot_config
    from pathcrawl.store import Store

    try:
        if resume:
            cfg = load_config(resume / "config.yaml")
            store = Store(resume / "crawl.db")
            campaign_id = store.meta("campaign_id")
            store.close()
            camp = cfg.campaign(campaign_id)
            run_dir = resume
        else:
            if not config or not campaign:
                err_console.print("[bold red]crawl:[/] give --config and --campaign, or --resume RUN_DIR")
                raise typer.Exit(code=2)
            cfg = load_config(config)
            camp = cfg.campaign(campaign)
    except ConfigError as e:
        err_console.print(f"[bold red]Config error[/]\n{escape(str(e))}", highlight=False)
        raise typer.Exit(code=1) from None

    placeholders = blocking_placeholders(cfg, camp.id)
    if placeholders:
        for w in placeholders:
            err_console.print(f"[red]error:[/] {escape(w)}", highlight=False)
        err_console.print("Fill in the placeholders before crawling.")
        raise typer.Exit(code=1)

    if not resume:
        run_dir = new_run_dir(cfg, camp, runs_dir)
        snapshot_config(config, run_dir)
    # Limits given on the command line are saved with the run, so --resume keeps them.
    store = Store(run_dir / "crawl.db")
    overrides = dict(store.meta("crawl_overrides", {}) or {})
    overrides.update({k: v for k, v in (("max_pages", max_pages), ("max_depth", max_depth)) if v is not None})
    store.set_meta(crawl_overrides=overrides)
    store.close()
    for key, value in overrides.items():
        setattr(cfg.crawl, key, value)
    headed = cfg.crawl.headed and not headless
    operator = NonInteractiveOperator() if non_interactive else TerminalOperator(console)
    console.print(f"Run directory: [bold]{escape(str(run_dir))}[/]" + ("  (resuming)" if resume else ""))
    try:
        status = Crawler(cfg, camp, run_dir, operator, console=console, headed=headed).run()
    except Exception as e:
        msg = str(e)
        if "Missing X server" in msg or "XServer" in msg or "no DISPLAY" in msg.lower():
            err_console.print("[bold red]crawl:[/] no display for a visible browser here. Use --headless.")
            raise typer.Exit(code=2) from None
        if "Executable doesn't exist" in msg:
            err_console.print("[bold red]crawl:[/] Chromium is not installed. Run: playwright install chromium")
            raise typer.Exit(code=2) from None
        raise
    store = Store(run_dir / "crawl.db")
    loaded = store.explored_count()
    store.close()
    if loaded == 0:
        err_console.print("[bold red]crawl:[/] no pages loaded; see the errors above. Nothing to analyze.")
        raise typer.Exit(code=1)
    console.print(f"Crawl {status}. Next: pathcrawl analyze --run {escape(str(run_dir))}")


def _to_jsonable(value: object) -> object:
    if isinstance(value, (set, tuple)):
        return list(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _open(run: Path):
    from pathcrawl.run import RunError, open_run

    try:
        return open_run(run)
    except (RunError, ConfigError) as e:
        err_console.print(f"[bold red]error:[/] {escape(str(e))}")
        raise typer.Exit(code=2) from None


@app.command()
def analyze(run: Path = typer.Option(..., "--run", help="A run directory from pathcrawl crawl.")) -> None:
    """Compute every path metric for a crawl and save analysis.json."""
    import json

    from pathcrawl.graph import MODES

    r = _open(run)
    result = r.analyze()
    (run / "analysis.json").write_text(json.dumps(result.to_dict(), indent=2, default=_to_jsonable))

    store = r.store
    console.print(
        f"[bold]{escape(str(store.meta('client')))}[/] · {escape(str(store.meta('campaign_name')))} · "
        f"crawl {store.meta('status')} · {store.explored_count()} pages crawled · win pages: {len(result.win_pages)}"
    )
    for mode in MODES:
        m = result.modes[mode]
        table = Table(title=f"{mode.replace('_', ' ')}")
        for col in ("entry link", "shortest", "longest simple", "dead zone at click"):
            table.add_column(col)
        for e in m.entries:
            longest = "-" if e.longest_clicks is None else f"{e.longest_clicks}" + ("" if e.longest_exhaustive else "+")
            table.add_row(
                escape(e.label),
                "no path" if e.shortest_clicks is None else f"{e.shortest_clicks} clicks",
                longest,
                "-" if e.dead_zone_click is None else str(e.dead_zone_click),
            )
        console.print(table)
        dz = m.dead_zones
        console.print(
            f"  worst case {m.worst_case.max_clicks} clicks · dead ends {dz.dead_end_count} ({dz.dead_end_pct}%) · "
            f"trap loops {len(dz.trap_loops)} · unknown {len(dz.unknown)} · converged {m.convergence.converged}"
        )
    r.close()
    console.print(f"Saved {escape(str(run / 'analysis.json'))}")


@app.command()
def categorize(run: Path = typer.Option(..., "--run", help="A run directory from pathcrawl crawl.")) -> None:
    """Categorize every crawled page (section, page type, reachability, content signals) into categories.csv."""
    from pathcrawl.categorize import categorize as categorize_pages
    from pathcrawl.categorize import summarize, write_csv

    r = _open(run)
    rows = categorize_pages(r.store, r.graph, r.analyze(), r.config.scope.locale_include)
    write_csv(rows, run / "categories.csv")
    summary = summarize(rows)
    table = Table(title=f"{summary['pages_loaded']} pages by section")
    for col in ("section", "pages", "reach win (all)", "reach win (content)", "dead/trap (content)"):
        table.add_column(col)
    for row in summary["by_section"][:25]:
        table.add_row(escape(row["section"]), str(row["pages"]), str(row["reach_win_all_links"]),
                      str(row["reach_win_content_only"]), str(row["dead_or_trap_content_only"]))
    console.print(table)
    r.close()
    console.print(f"Saved {escape(str(run / 'categories.csv'))}")


@app.command()
def report(run: Path = typer.Option(..., "--run", help="A run directory from pathcrawl crawl.")) -> None:
    """Write report.md, report.json, graph.graphml, paths.mmd and categories.csv for a run."""
    from pathcrawl.report import headline, write_report

    r = _open(run)
    paths = write_report(r)
    console.print(f"[bold]{escape(headline(r.analyze(), r.config.win.name))}[/]")
    r.close()
    for name, path in paths.items():
        console.print(f"  wrote {escape(str(path))}")
