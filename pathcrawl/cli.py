"""Command-line entry point: ``pathcrawl``."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from pathcrawl.config import ConfigError, config_warnings, load_config

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
def selftest() -> None:
    """Run every stage built so far against the fixture site and compare the
    results with the hand-worked expected numbers. Exits 1 on any mismatch."""
    from pathcrawl.selftest import FIXTURE_SITE, run_graph_stage

    try:
        checks = run_graph_stage()
    except FileNotFoundError as e:
        err_console.print(f"[bold red]selftest:[/] {escape(str(e))}")
        raise typer.Exit(code=2) from None

    table = Table(title=f"Fixture site selftest ({FIXTURE_SITE.relative_to(Path.cwd()) if FIXTURE_SITE.is_relative_to(Path.cwd()) else FIXTURE_SITE})")
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
