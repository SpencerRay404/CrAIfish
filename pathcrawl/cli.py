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
    taxonomy: Path | None = typer.Option(
        None, "--taxonomy", help="Entity taxonomy (default: configs/<client>.entities.yaml if it exists)."
    ),
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
    if status != "quit":
        _extract_entities(run_dir, cfg.client.slug, taxonomy)
    ended = {"complete": "Crawl complete.", "budget": "Crawl stopped at the page budget.", "quit": "Crawl paused."}[status]
    next_step = f"pathcrawl crawl --resume {run_dir}" if status == "quit" else f"pathcrawl report --run {run_dir}"
    console.print(f"{ended} Next:")
    # On its own line and never wrapped, so it can be copied and pasted whole.
    console.print(escape(next_step), soft_wrap=True, highlight=False)


def _to_jsonable(value: object) -> object:
    if isinstance(value, (set, tuple)):
        return list(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


CONFIG_HELP = "Client config to use instead of the run's saved copy (for settings added since the crawl)."


def _open(run: Path, config: Path | None = None):
    from pathcrawl.run import RunError, open_run

    try:
        return open_run(run, config)
    except (RunError, ConfigError) as e:
        err_console.print(f"[bold red]error:[/] {escape(str(e))}")
        raise typer.Exit(code=2) from None


@app.command()
def analyze(
    run: Path = typer.Option(..., "--run", help="A run directory from pathcrawl crawl."),
    config: Path | None = typer.Option(None, "--config", "-c", help=CONFIG_HELP),
) -> None:
    """Compute every path metric for a crawl and save analysis.json."""
    import json

    from pathcrawl.graph import MODES

    r = _open(run, config)
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
def categorize(
    run: Path = typer.Option(..., "--run", help="A run directory from pathcrawl crawl."),
    config: Path | None = typer.Option(None, "--config", "-c", help=CONFIG_HELP),
) -> None:
    """Categorize every crawled page (section, page type, reachability, content signals) into categories.csv."""
    from pathcrawl.categorize import categorize as categorize_pages
    from pathcrawl.categorize import summarize, write_csv

    r = _open(run, config)
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
def report(
    run: Path = typer.Option(..., "--run", help="A run directory from pathcrawl crawl."),
    taxonomy: Path | None = typer.Option(
        None, "--taxonomy", help="Entity taxonomy (default: configs/<client>.entities.yaml, else the run's copy)."
    ),
    config: Path | None = typer.Option(None, "--config", "-c", help=CONFIG_HELP),
) -> None:
    """Write every report file for a run (report.md, report.json, graphs, CSVs).

    If an entity taxonomy is found, entities are re-extracted first, so edits
    to the taxonomy show up in the report without re-crawling."""
    from pathcrawl.report import headline, write_report

    r = _open(run, config)
    r.close()
    _extract_entities(run, r.config.client.slug, taxonomy)
    r = _open(run, config)
    if r.config.leads.files:
        missing = [f.path for f in r.config.leads.files if not Path(f.path).exists()]
        if missing:
            err_console.print(f"[yellow]Lead file(s) not found, lead join skipped: {escape(', '.join(missing))}[/]")
        else:
            _run_leads(r)
    paths = write_report(r)
    console.print(f"[bold]{escape(headline(r.analyze(), r.config.win.name))}[/]")
    r.close()
    for name, path in paths.items():
        console.print(f"  wrote {escape(str(path))}")


# --------------------------------------------------------------------------- entities

entities_app = typer.Typer(help="Tag pages with taxonomy entities, and review LLM-proposed additions.",
                           no_args_is_help=True)
app.add_typer(entities_app, name="entities")


def _load_taxonomy(path: Path):
    from pathcrawl.entities import TaxonomyError, load_taxonomy

    try:
        return load_taxonomy(path)
    except TaxonomyError as e:
        err_console.print(f"[bold red]Taxonomy error[/]\n{escape(str(e))}", highlight=False)
        raise typer.Exit(code=1) from None


def _extract_entities(run_dir: Path, slug: str, explicit: Path | None, required: bool = False) -> bool:
    """Tag the run's pages if a taxonomy is found. Returns whether it ran."""
    from pathcrawl.entities import extract_run, find_taxonomy
    from pathcrawl.store import Store

    path = find_taxonomy(run_dir, slug, explicit)
    if path is None:
        if required or explicit:
            err_console.print(f"[bold red]entities:[/] no taxonomy found (looked for "
                              f"{escape(str(explicit or f'configs/{slug}.entities.yaml'))} and "
                              f"{escape(str(run_dir / 'entities.yaml'))})")
            raise typer.Exit(code=2)
        return False
    tax = _load_taxonomy(path)
    store = Store(run_dir / "crawl.db")
    try:
        s = extract_run(store, tax, path, run_dir)
    finally:
        store.close()
    console.print(f"Entities ({escape(str(path))}): {s.pages_tagged} of {s.pages} pages tagged, {s.tags} tags, "
                  f"{s.entities_used} entities; boilerplate removed: {s.boilerplate_words_dropped} words, "
                  f"{s.boilerplate_headings_dropped} headings", highlight=False, soft_wrap=True)
    return True


@entities_app.command("check")
def entities_check(taxonomy: Path = typer.Option(..., "--taxonomy", help="Taxonomy YAML to check.")) -> None:
    """Check that a taxonomy file is valid and summarize it."""
    from collections import Counter

    tax = _load_taxonomy(taxonomy)
    counts = Counter(e.type for e in tax.entities)
    console.print(f"[green]{escape(str(taxonomy))} is valid[/]: {len(tax.entities)} entities ("
                  + ", ".join(f"{t} {counts.get(t, 0)}" for t in tax.types) + ")", highlight=False)


@entities_app.command("extract")
def entities_extract(
    run: Path = typer.Option(..., "--run", help="A run directory from pathcrawl crawl."),
    taxonomy: Path | None = typer.Option(None, "--taxonomy", help="Default: configs/<client>.entities.yaml."),
) -> None:
    """Tag every loaded page with taxonomy entities (the page_entities table)."""
    r = _open(run)
    r.close()
    _extract_entities(run, r.config.client.slug, taxonomy, required=True)


@entities_app.command("propose")
def entities_propose(
    run: Path = typer.Option(..., "--run", help="A run directory from pathcrawl crawl."),
    taxonomy: Path | None = typer.Option(None, "--taxonomy", help="Default: configs/<client>.entities.yaml."),
    base_url: str | None = typer.Option(None, "--base-url", help="OpenAI-compatible endpoint (default: taxonomy llm.base_url)."),
    model: str | None = typer.Option(None, "--model", help="Model name (default: taxonomy llm.model)."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Only send this many pages (for a trial run)."),
    out: Path | None = typer.Option(None, "--out", help="Default: <run>/entity_proposals.csv."),
) -> None:
    """Ask a local LLM for entities the taxonomy is missing. Writes a CSV for review; changes nothing else."""
    from pathcrawl.entities import find_taxonomy, source_pages, strip_boilerplate
    from pathcrawl.propose import OpenAICompatibleClient, propose, write_proposals

    r = _open(run)
    path = find_taxonomy(run, r.config.client.slug, taxonomy)
    if path is None:
        err_console.print("[bold red]entities propose:[/] no taxonomy found; give --taxonomy")
        raise typer.Exit(code=2)
    tax = _load_taxonomy(path)
    pages = strip_boilerplate(source_pages(r.store), tax.boilerplate)
    r.close()
    pages = [p for p in pages if p.body.strip()][:limit]
    client = OpenAICompatibleClient(base_url or tax.llm.base_url, model or tax.llm.model, tax.llm.timeout_s)
    console.print(f"Sending {len(pages)} pages to {escape(client.model)} at {escape(client.url)} ...")
    proposals, summary = propose(pages, tax, client,
                                 progress=lambda url, n: console.print(f"  {n} proposed  {escape(url)}", highlight=False))
    for e in summary.errors[:5]:
        err_console.print(f"[yellow]error:[/] {escape(e)}", highlight=False)
    if summary.errors and summary.pages_sent == len(summary.errors):
        err_console.print("[bold red]entities propose:[/] the model could not be reached; nothing written.")
        raise typer.Exit(code=1)
    out = out or run / "entity_proposals.csv"
    write_proposals(proposals, out, client.model)
    console.print(f"{len(proposals)} new entities proposed from {summary.pages_sent} pages "
                  f"(dropped: {summary.rejected_not_on_page} not on the page, {summary.rejected_known} already known, "
                  f"{summary.rejected_type} bad type).")
    console.print(f"Review {escape(str(out))}: set decision to 'accept' on rows to keep, then:")
    console.print(escape(f"pathcrawl entities accept --proposals {out} --taxonomy {path}"), soft_wrap=True, highlight=False)


@entities_app.command("accept")
def entities_accept(
    proposals: Path = typer.Option(..., "--proposals", help="A reviewed entity_proposals.csv."),
    taxonomy: Path = typer.Option(..., "--taxonomy", help="The taxonomy file to add accepted rows to."),
) -> None:
    """Add the proposals a reviewer marked 'accept' to the taxonomy file."""
    from pathcrawl.entities import TaxonomyError
    from pathcrawl.propose import accept

    try:
        added = accept(proposals, taxonomy)
    except TaxonomyError as e:
        err_console.print(f"[bold red]entities accept:[/] {escape(str(e))}", highlight=False)
        raise typer.Exit(code=1) from None
    if not added:
        console.print("Nothing added: no new rows are marked 'accept'.")
        return
    console.print(f"Added {len(added)} entities to {escape(str(taxonomy))}:")
    for a in added:
        console.print(f"  {escape(a)}", highlight=False)


@app.command("backfill-links")
def backfill_links_cmd(
    run: Path = typer.Option(..., "--run", help="A run directory from pathcrawl crawl."),
    config: Path | None = typer.Option(
        None, "--config", "-c",
        help="Client config for capture_params (default: the run's copy, else configs/<client>.yaml).",
    ),
) -> None:
    """Fill columns added since a run was crawled (link campaign tags, dead pages), from its crawl.db. Fetches nothing."""
    from pathcrawl.backfill import backfill_links

    r = _open(run, config)
    if not r.config.scope.capture_params and config is None:
        live = Path("configs") / f"{r.config.client.slug}.yaml"
        if live.exists():
            r.close()
            r = _open(run, live)
            console.print(f"Using {escape(str(live))} (the run's saved config has no capture_params)")
    if not r.config.scope.capture_params:
        err_console.print("[yellow]scope.capture_params is empty; no link tags to fill. Set it, e.g. "
                          '["WT.mc_id"], or pass --config.[/]')
    s = backfill_links(r.store, r.config.scope)
    r.close()
    console.print(f"Links: {s.links_tagged} of {s.links} carry a tag ({', '.join(r.config.scope.capture_params) or '-'}); "
                  f"{s.distinct_tags} distinct tags on {s.source_pages} source pages. Dead pages: {s.dead_pages}.",
                  highlight=False, soft_wrap=True)
    if s.duplicate_pages:
        console.print(f"[yellow]{len(s.duplicate_pages)} pages were stored under more than one URL that now "
                      "normalize the same (e.g. a stripped tracking param). Not merged; re-crawl to merge:[/]")
        for group in s.duplicate_pages[:20]:
            console.print("  " + escape(" = ".join(group)), highlight=False, soft_wrap=True)


@app.command()
def leads(
    run: Path = typer.Option(..., "--run", help="A run directory from pathcrawl crawl."),
    config: Path | None = typer.Option(None, "--config", "-c", help=CONFIG_HELP),
) -> None:
    """Join aggregated lead counts per campaign tag (leads.files) to the links carrying each tag."""
    r = _open(run, config)
    if not r.config.leads.files:
        err_console.print("[bold red]leads:[/] no leads.files in the config; pass --config configs/<client>.yaml")
        r.close()
        raise typer.Exit(code=2)
    try:
        _run_leads(r)
    finally:
        r.close()


def _run_leads(r) -> None:
    from pathcrawl.leads import LeadFileError, run_leads

    if not r.store.db.execute("SELECT 1 FROM links WHERE mc_id IS NOT NULL LIMIT 1").fetchone():
        err_console.print("[yellow]No link in this run carries a tag yet. Run pathcrawl backfill-links first "
                          "(or set scope.capture_params before crawling).[/]")
    try:
        result, out = run_leads(r.store, r.config, r.dir)
    except LeadFileError as e:
        err_console.print(f"[bold red]leads:[/] {escape(str(e))}", highlight=False)
        raise typer.Exit(code=1) from None
    matched = [t for t in result.tags if t.join]
    total = sum(t.leads for t in result.lead_tags.values())
    covered = sum(t.leads for t in matched)
    pages = {row.src for row in result.rows}
    console.print(
        f"Leads: {len(result.lead_tags)} tags, {len(matched)} joined to crawled links "
        f"({sum(1 for t in matched if t.join == 'exact')} exact), covering {covered:g} of {total:g} leads; "
        f"{len(pages)} pages carry allocated leads. Wrote {escape(str(out))}",
        highlight=False, soft_wrap=True,
    )


@app.command("external-seeds")
def external_seeds_cmd(
    run: Path = typer.Option(..., "--run", help="A run directory from pathcrawl crawl."),
    config: Path | None = typer.Option(None, "--config", "-c", help=CONFIG_HELP),
    seeds: Path | None = typer.Option(None, "--seeds", help="Seed CSV (default: the campaign's external_seeds)."),
) -> None:
    """Add hand-collected posts (e.g. LinkedIn) to a run as external entry points.

    New landing pages are queued; crawl them with pathcrawl crawl --resume."""
    from pathcrawl.seeds import SeedFileError, ingest

    r = _open(run, config)
    try:
        try:
            campaign = r.config.campaign(r.store.meta("campaign_id") or r.config.campaigns[0].id)
        except ConfigError as e:
            err_console.print(f"[bold red]external-seeds:[/] {escape(str(e))}")
            raise typer.Exit(code=2) from None
        path = seeds or (Path(campaign.external_seeds) if campaign.external_seeds else None)
        if path is None:
            err_console.print("[bold red]external-seeds:[/] no seed file; pass --seeds or set external_seeds")
            raise typer.Exit(code=2)
        try:
            result = ingest(r.store, r.config, campaign, path)
        except SeedFileError as e:
            err_console.print(f"[bold red]external-seeds:[/] {escape(str(e))}", highlight=False)
            raise typer.Exit(code=1) from None
    finally:
        r.close()
    console.print(f"{len(result.seeds)} posts added; {len(result.skipped_seeds)} skipped (already ad URLs or entry "
                  f"links), {result.duplicate_rows} duplicate rows. {len(result.new_entries)} new entry links queued, "
                  f"{len(result.existing_entries)} already entry links.", highlight=False, soft_wrap=True)
    if result.new_entries:
        console.print("Crawl the new entry links with:")
        console.print(escape(f"pathcrawl crawl --resume {run}"), soft_wrap=True, highlight=False)
