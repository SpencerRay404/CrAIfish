"""The terminal operator prompt, driven through stdin."""

import io

import pytest
from rich.console import Console

from pathcrawl.crawler import GOTO_URL, MARK_WIN, NO_LINKS, QUIT, RETRY, SKIP, Problem, TerminalOperator
from pathcrawl.selftest import fixture_config

PROBLEM = Problem(NO_LINKS, "http://fixture.test/orphan.html", "no links", loaded=True)


def decide(monkeypatch, typed: str):
    monkeypatch.setattr("sys.stdin", io.StringIO(typed))
    out = io.StringIO()
    decision = TerminalOperator(Console(file=out, width=200)).decide(PROBLEM, fixture_config())
    return decision, out.getvalue()


@pytest.mark.parametrize(("typed", "action"), [("r\n", RETRY), ("s\n", SKIP), ("\n", SKIP), ("w\n", MARK_WIN), ("q\n", QUIT)])
def test_menu_choices(monkeypatch, typed, action):
    assert decide(monkeypatch, typed)[0].action == action


def test_panel_shows_every_key(monkeypatch):
    _, shown = decide(monkeypatch, "s\n")
    for key in ("[r] retry", "[s] accept this page and continue", "[u] enter a URL", "[w] mark this page as a win", "[q] save and quit"):
        assert key in shown


def test_url_must_be_in_scope_and_is_normalized(monkeypatch):
    decision, shown = decide(monkeypatch, "u\nhttps://evil.example/x\nhttp://FIXTURE.test/win.html#top\n")
    assert decision.action == GOTO_URL and decision.url == "http://fixture.test/win.html"
    assert "not a URL inside the allowed domains" in shown


def test_blank_url_goes_back_to_menu(monkeypatch):
    assert decide(monkeypatch, "u\n\nw\n")[0].action == MARK_WIN


def test_closed_input_saves_and_quits(monkeypatch):
    assert decide(monkeypatch, "")[0].action == QUIT
