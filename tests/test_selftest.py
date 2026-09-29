"""The selftest gate must pass on the real code and must fail when anything is off."""

import copy

import pytest
from typer.testing import CliRunner

from pathcrawl.cli import app
from pathcrawl.graph import analyze
from pathcrawl.selftest import (
    FIXTURE_BASE,
    compare,
    entry_points,
    fixture_config,
    graph_from_html_files,
    load_expected,
    run_crawl_and_report_stages,
    run_crawl_stage,
    run_graph_stage,
)


def test_graph_stage_matches_expected():
    checks = run_graph_stage()
    failed = [c for c in checks if not c.ok]
    assert not failed, "\n".join(f"{c.mode} {c.metric}: expected {c.expected}, got {c.actual}" for c in failed)
    assert len(checks) == 29


def test_gate_catches_a_wrong_number():
    expected = load_expected()
    cfg = fixture_config()
    g = graph_from_html_files(cfg)
    analysis = analyze(g, entry_points(cfg), max_depth=expected["max_depth"])

    wrong = copy.deepcopy(expected)
    wrong["modes"]["content_only"]["shortest_path"]["far"] = ["entry-far", "win"]
    wrong["modes"]["content_only"]["trap_loops"] = []
    failed = [c for c in compare(analysis, wrong, FIXTURE_BASE, "graph", expected["crawled_pages"]) if not c.ok]
    assert [(c.mode, c.metric) for c in failed] == [
        ("content_only", "shortest path (far)"),
        ("content_only", "trap loops"),
    ]


def test_cli_selftest_passes():
    result = CliRunner().invoke(app, ["selftest", "--stage", "graph"])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


@pytest.mark.browser
def test_crawl_stage_matches_expected():
    """The end-to-end gate: a real browser crawl of the fixture site."""
    checks = run_crawl_stage()
    failed = [c for c in checks if not c.ok]
    assert not failed, "\n".join(f"{c.mode} {c.metric}: expected {c.expected}, got {c.actual}" for c in failed)
    assert len(checks) == 29


@pytest.mark.browser
def test_report_stage_matches_expected():
    """Crawl, then report: every report file written and every number and category right."""
    checks = run_crawl_and_report_stages()
    failed = [c for c in checks if not c.ok]
    assert not failed, "\n".join(f"{c.stage} {c.mode} {c.metric}: expected {c.expected}, got {c.actual}" for c in failed)
    assert len(checks) == 29 + 28
