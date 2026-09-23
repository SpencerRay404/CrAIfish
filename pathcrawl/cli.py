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
