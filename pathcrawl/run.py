"""Open a finished (or paused) run directory for analysis and reporting."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from pathcrawl.config import Config, load_config
from pathcrawl.graph import Analysis, EntryPoint, analyze, graph_from_store
from pathcrawl.store import Store


class RunError(Exception):
    pass


@dataclass
class Run:
    dir: Path
    config: Config
    store: Store
    graph: nx.DiGraph
    entries: list[EntryPoint]

    def analyze(self) -> Analysis:
        return analyze(self.graph, self.entries, max_depth=self.config.crawl.max_depth)

    def close(self) -> None:
        self.store.close()


def open_run(run_dir: str | Path, config_path: str | Path | None = None) -> Run:
    """Open a run. ``config_path`` replaces the config snapshot saved with the
    run, so settings added since the crawl (lead files, known win pages,
    capture params) apply to an older run without re-crawling."""
    run_dir = Path(run_dir)
    if not (run_dir / "crawl.db").exists() or not (run_dir / "config.yaml").exists():
        raise RunError(f"{run_dir} is not a run directory (needs crawl.db and config.yaml)")
    config = load_config(config_path or run_dir / "config.yaml")
    store = Store(run_dir / "crawl.db")
    for key, value in (store.meta("crawl_overrides", {}) or {}).items():
        setattr(config.crawl, key, value)
    graph, entries = graph_from_store(store, config.win)
    return Run(run_dir, config, store, graph, entries)
