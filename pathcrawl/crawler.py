"""The Playwright crawl loop and operator prompts.

The crawler walks breadth-first from a campaign's entry links, one page at a
time, in a visible browser by default so people can watch it work. When it
gets stuck (a block, an error, an entry link that leaves the allowlist, a page
with no links) it pauses and asks the operator what to do.

Hard rules, enforced here:
- Navigation happens only by loading a URL that is in scope (allowed domain
  and locale filters) or is an entry link. Links are never clicked.
- The only element ever clicked is a cookie-consent button, to get the banner
  out of the way. Forms are never filled or submitted, and nothing logs in.
- robots.txt is respected unless the config turns it off, and there is a delay
  between page loads.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.robotparser import RobotFileParser

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt

from pathcrawl.config import CampaignConfig, Config
from pathcrawl.extract import detect_block, extract_links, extract_page, visible_text
from pathcrawl.store import LinkRecord, PageRecord, Store

# --------------------------------------------------------------------------- operator

BLOCKED = "blocked"
NAV_ERROR = "navigation error"
OFFSITE_REDIRECT = "entry link left the allowlist"
NO_LINKS = "no crawlable links"
CONSENT = "consent banner"
DOWNLOAD = "file download instead of a page"

RETRY, SKIP, GOTO_URL, MARK_WIN, QUIT = "retry", "skip", "url", "win", "quit"


@dataclass
class Problem:
    kind: str
    url: str
    detail: str
    loaded: bool  # whether the page itself loaded (its data can still be saved)


@dataclass
class Decision:
    action: str
    url: str | None = None  # for GOTO_URL: normalized, already checked to be in scope


class Operator(Protocol):
    def decide(self, problem: Problem, config: Config) -> Decision: ...


class OperatorQuit(Exception):
    """The operator chose to save and quit. The run can be resumed."""


class BrowserClosed(Exception):
    """The browser window was closed (usually by the person watching). Treated
    like save-and-quit: everything crawled so far is kept and the run can resume."""


def _is_browser_closed(error: BaseException) -> bool:
    from playwright._impl._errors import TargetClosedError

    return isinstance(error, (BrowserClosed, TargetClosedError)) or "has been closed" in str(error)


class TerminalOperator:
    """Asks the person at the keyboard."""

    def __init__(self, console: Console):
        self.console = console

    def decide(self, problem: Problem, config: Config) -> Decision:
        accept = problem.loaded and problem.kind in (NO_LINKS, CONSENT)
        skip_label = "accept this page and continue" if accept else "skip this page"
        self.console.print(
            Panel(
                f"[bold]{escape(problem.kind)}[/]\n{escape(problem.url)}\n\n{escape(problem.detail)}\n\n"
                + escape(f"[r] retry   [s] {skip_label}   [u] enter a URL to continue to   "
                         "[w] mark this page as a win   [q] save and quit"),
                title="Crawler paused: operator needed",
                border_style="yellow",
            )
        )
        try:
            while True:
                choice = Prompt.ask("Choice", choices=["r", "s", "u", "w", "q"], default="s", console=self.console)
                if choice != "u":
                    return Decision({"r": RETRY, "s": SKIP, "w": MARK_WIN, "q": QUIT}[choice])
                url = self._ask_url(config)
                if url:
                    return Decision(GOTO_URL, url)
        except (EOFError, KeyboardInterrupt):
            # No one left to answer (input closed, or Ctrl-C): save and quit so the run can resume.
            self.console.print("\n[yellow]No operator input; saving and quitting.[/]")
            return Decision(QUIT)

    def _ask_url(self, config: Config) -> str | None:
        """Ask until the URL is in scope; blank goes back to the menu."""
        while True:
            raw = Prompt.ask("URL to continue to (blank to go back)", default="", console=self.console)
            if not raw.strip():
                return None
            url = config.scope.normalize(raw)
            if url and config.scope.in_scope(url):
                return url
            self.console.print(f"[red]{escape(raw.strip())} is not a URL inside the allowed domains / locale filters.[/]")


class NonInteractiveOperator:
    """For unattended runs and tests: never waits for a person.

    Pages that loaded are accepted as they are; pages that failed are skipped.
    """

    def decide(self, problem: Problem, config: Config) -> Decision:
        return Decision(SKIP)


class ScriptedOperator:
    """Replays a fixed list of decisions (tests)."""

    def __init__(self, decisions: list[Decision]):
        self.decisions = list(decisions)
        self.problems: list[Problem] = []

    def decide(self, problem: Problem, config: Config) -> Decision:
        self.problems.append(problem)
        return self.decisions.pop(0) if self.decisions else Decision(SKIP)


# --------------------------------------------------------------------------- browser helpers

CONSENT_BUTTONS = (
    # Prefer declining; fall back to accepting just to clear the banner.
    "#onetrust-reject-all-handler",
    "#CybotCookiebotDialogBodyButtonDecline",
    "#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#truste-consent-button",
    ".cc-allow",
    "button:has-text('Reject all')",
    "button:has-text('Accept all')",
    "button:has-text('Accept All Cookies')",
    "button:has-text('I agree')",
)
CONSENT_BANNERS = (
    "#onetrust-banner-sdk",
    "#CybotCookiebotDialog",
    "#truste-consent-track",
    ".cc-window:not(.cc-invisible)",
)


def chromium_executable() -> str | None:
    """Optional override for environments where Playwright's own download is missing."""
    return os.environ.get("PATHCRAWL_CHROMIUM") or None


@dataclass
class Visit:
    http_status: int | None
    final_url: str  # as the browser reports it, not yet normalized
    redirect_chain: list[str]
    load_ms: int
    html: str


class RobotsCache:
    def __init__(self, request_context, enabled: bool):
        self.request = request_context
        self.enabled = enabled
        self.parsers: dict[str, RobotFileParser] = {}

    def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        scheme = url.split("://", 1)[0]
        origin = f"{scheme}://{url.split('://', 1)[1].split('/', 1)[0]}"
        if origin not in self.parsers:
            parser = RobotFileParser()
            try:
                resp = self.request.get(origin + "/robots.txt", timeout=15000)
                if resp.status in (401, 403):
                    parser.disallow_all = True
                elif resp.status >= 400:
                    parser.allow_all = True
                else:
                    parser.parse(resp.text().splitlines())
            except Exception:
                parser.allow_all = True  # unreachable robots.txt: treat as no rules
            self.parsers[origin] = parser
        return self.parsers[origin].can_fetch("pathcrawl", url)


# --------------------------------------------------------------------------- crawler


def new_run_dir(config: Config, campaign: CampaignConfig, runs_root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = runs_root / config.client.slug / campaign.id / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


class Crawler:
    def __init__(
        self,
        config: Config,
        campaign: CampaignConfig,
        run_dir: Path,
        operator: Operator,
        console: Console | None = None,
        headed: bool | None = None,
    ):
        self.config = config
        self.campaign = campaign
        self.run_dir = Path(run_dir)
        self.operator = operator
        self.console = console or Console()
        self.headed = config.crawl.headed if headed is None else headed
        self.store = Store(self.run_dir / "crawl.db")
        self.screens = self.run_dir / "screenshots"
        self.scope = config.scope

    # ------------------------------------------------------------------ setup

    def seed(self) -> None:
        """Queue the entry links (only on a fresh run; a resumed run keeps its queue)."""
        if self.store.meta("started_at"):
            return
        self.store.set_meta(
            client=self.config.client.name,
            campaign_id=self.campaign.id,
            campaign_name=self.campaign.name,
            ad_copy=self.campaign.ad_copy,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
            status="running",
        )
        for i, link in enumerate(self.campaign.entry_links):
            url = self.scope.normalize(link.url)
            self.store.add_entry(i, link.label, link.url, url)
            self.store.enqueue(url, 0, None)

    # ------------------------------------------------------------------ main loop

    def run(self) -> str:
        """Crawl until the queue is empty or the page budget is spent.

        Returns the final status: "complete", "budget" or "quit".
        """
        from playwright.sync_api import sync_playwright

        self.seed()
        self.screens.mkdir(exist_ok=True)
        cc = self.config.crawl
        status = "complete"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=not self.headed,
                slow_mo=cc.slow_mo_ms if self.headed else 0,
                executable_path=chromium_executable(),
            )
            context = browser.new_context(viewport={"width": 1366, "height": 900})
            self.context = context
            self.page = self._new_page()
            self.user_agent = self.page.evaluate("navigator.userAgent")
            self.store.set_meta(user_agent=self.user_agent, headless=not self.headed)
            self.robots = RobotsCache(context.request, cc.respect_robots)
            self.request = context.request
            try:
                while True:
                    if self.store.explored_count() >= cc.max_pages:
                        status = "budget"
                        self.console.print(f"[yellow]Page budget reached ({cc.max_pages} pages).[/]")
                        break
                    item = self.store.next_pending()
                    if item is None:
                        break
                    self._process(item.url, item.depth)
                    if cc.delay_ms and self.store.pending_count():
                        time.sleep(cc.delay_ms / 1000)
            except OperatorQuit:
                status = "quit"
                self._resume_hint("Saved.")
            except KeyboardInterrupt:
                status = "quit"
                self._resume_hint("Interrupted; progress saved.")
            except Exception as e:
                if not _is_browser_closed(e):
                    raise
                status = "quit"
                self._resume_hint("The browser window was closed; progress saved.")
            finally:
                self.store.set_meta(status=status, finished_at=datetime.now(UTC).isoformat(timespec="seconds"))
                for closeable in (context, browser):
                    try:
                        closeable.close()
                    except Exception:
                        pass  # already closed by the person watching
        self._summary()
        return status

    def _resume_hint(self, what: str) -> None:
        self.console.print(f"[yellow]{what} Resume with: pathcrawl crawl --resume {escape(str(self.run_dir))}[/]")

    def _summary(self) -> None:
        """One line on how the crawl went; loud when nothing loaded at all."""
        rows = self.store.db.execute("SELECT status, COUNT(*) FROM pages GROUP BY status").fetchall()
        counts = {r[0]: r[1] for r in rows}
        loaded = counts.get("ok", 0) + counts.get("http_error", 0)
        failed = counts.get("skipped", 0)
        parts = [f"{loaded} pages loaded"] + [f"{n} {k}" for k, n in sorted(counts.items()) if k not in ("ok", "http_error")]
        wins = self.store.db.execute("SELECT COUNT(*) FROM pages WHERE win = 1").fetchone()[0]
        parts.append(f"{wins} win page{'s' if wins != 1 else ''} found")
        if loaded == 0 and failed:
            first = self.store.db.execute(
                "SELECT url, error FROM pages WHERE status = 'skipped' ORDER BY crawled_at LIMIT 1"
            ).fetchone()
            self.console.print(
                f"[bold red]No pages loaded.[/] First error on {escape(first['url'])}:\n  {escape(first['error'] or '')}",
                highlight=False,
            )
        else:
            self.console.print("Summary: " + ", ".join(parts), highlight=False)

    # ------------------------------------------------------------------ one page

    def _ask(self, problem: Problem) -> Decision:
        decision = self.operator.decide(problem, self.config)
        self.store.log_action(problem.url, problem.kind, decision.action, decision.url or problem.detail)
        # Always leave a trace in the terminal, even when no one was asked.
        self.console.print(
            f"[yellow]  ! {escape(problem.kind)}[/] {escape(problem.url)}  [dim]{escape(problem.detail)}[/]"
            f"  → {decision.action}" + (f" {escape(decision.url)}" if decision.url else ""),
            highlight=False,
        )
        if decision.action == QUIT:
            raise OperatorQuit
        return decision

    def _new_page(self):
        page = self.context.new_page()
        page.set_default_timeout(self.config.crawl.page_timeout_ms)
        return page

    def _reset_tab(self) -> None:
        """A failed load can leave the tab mid-navigation to an error page, which
        then interrupts the next load. Start over in a clean tab."""
        try:
            self.page.close()
        except Exception:
            pass
        self.page = self._new_page()

    def _visit_with_reset(self, url: str) -> Visit:
        """Load a page; after a failure, reset the tab and try once more before
        giving up (the operator is only asked if the second attempt fails too)."""
        try:
            return self._visit(self.page, url)
        except Exception as e:
            if _is_browser_closed(e) or self.page.is_closed():
                raise BrowserClosed from e
            self._reset_tab()
        try:
            return self._visit(self.page, url)
        except Exception as e:
            if _is_browser_closed(e) or self.page.is_closed():
                raise BrowserClosed from e
            self._reset_tab()
            raise

    def _visit(self, page, url: str) -> Visit:
        start = time.monotonic()
        response = page.goto(url, wait_until="load")
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass  # busy pages never go idle; "load" already fired
        load_ms = int((time.monotonic() - start) * 1000)
        chain: list[str] = []
        req = response.request if response else None
        while req is not None:
            chain.append(req.url)
            req = req.redirected_from
        return Visit(
            http_status=response.status if response else None,
            final_url=page.url,
            redirect_chain=chain[::-1],
            load_ms=load_ms,
            html=page.content(),
        )

    def _dismiss_consent(self, page) -> bool:
        """Click a known consent button if one is showing. Returns False if a
        known banner is still visible afterwards."""
        for sel in CONSENT_BUTTONS:
            try:
                btn = page.locator(sel).first
                if btn.is_visible():
                    btn.click(timeout=3000)
                    page.wait_for_timeout(500)
                    break
            except Exception:
                continue
        for sel in CONSENT_BANNERS:
            try:
                if page.locator(sel).first.is_visible():
                    return False
            except Exception:
                continue
        return True

    def _describe_download(self, url: str) -> str:
        """What the server sent instead of a page, as evidence for the report."""
        mode = "visible" if self.headed else "headless"
        try:
            resp = self.request.get(url, timeout=self.config.crawl.page_timeout_ms, max_redirects=10)
            h = resp.headers
            parts = [f"HTTP {resp.status}", f"content-type {h.get('content-type') or 'none'}"]
            if h.get("content-disposition"):
                parts.append(f"content-disposition {h['content-disposition']}")
            sent = ", ".join(parts)
        except Exception as e:
            sent = f"could not re-fetch it ({str(e).splitlines()[0]})"
        return (f"the server answered with a file download, not a web page ({sent}); "
                f"browser: {mode}, user agent: {self.user_agent}")

    def _raw_text_len(self, url: str) -> int | None:
        """Visible text length of the plain HTTP response, before any JavaScript runs."""
        try:
            resp = self.request.get(url, timeout=self.config.crawl.page_timeout_ms)
            if "html" not in (resp.headers.get("content-type") or "html"):
                return None
            return len(visible_text(resp.text()))
        except Exception:
            return None

    def _screenshot(self, page, url: str) -> str | None:
        if not self.config.crawl.screenshot:
            return None
        name = hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg"
        try:
            page.screenshot(path=str(self.screens / name), full_page=True, type="jpeg", quality=60)
            return f"screenshots/{name}"
        except Exception:
            return None

    def _save_failed(self, url: str, depth: int, status: str, error: str, win: bool = False) -> None:
        self.store.save_page(
            PageRecord(url=url, requested_url=url, status=status, depth=depth, error=error,
                       win=win, win_source="operator" if win else None),
            [],
            queue_url=url,
        )

    def _process(self, url: str, depth: int) -> None:
        is_entry = self.store.is_entry(url)
        if not self.robots.allowed(url):
            self._save_failed(url, depth, "robots", "disallowed by robots.txt")
            self._log(depth, "robots", url, "skipped: disallowed by robots.txt")
            return

        while True:  # retried on operator request
            try:
                visit = self._visit_with_reset(url)
                page = self.page
            except Exception as e:
                if _is_browser_closed(e):
                    raise
                if "Download is starting" in str(e):
                    kind, detail = DOWNLOAD, self._describe_download(url)
                else:
                    kind, detail = NAV_ERROR, str(e).splitlines()[0]
                d = self._ask(Problem(kind, url, detail, loaded=False))
                if d.action == RETRY:
                    continue
                self._save_failed(url, depth, "skipped", f"{kind}: {detail}", win=d.action == MARK_WIN)
                self._follow_operator_url(url, depth, d)
                return

            final = self.scope.normalize(visit.final_url) or url
            if not self.scope.domain_allowed(final):
                if is_entry:
                    d = self._ask(Problem(OFFSITE_REDIRECT, url, f"redirected to {visit.final_url}", loaded=False))
                    if d.action == RETRY:
                        continue
                    self.store.update_entry(url, status="offsite")
                    self._save_failed(url, depth, "skipped", f"redirected off the allowlist to {visit.final_url}",
                                      win=d.action == MARK_WIN)
                    self._follow_operator_url(url, depth, d)
                else:
                    self._save_failed(url, depth, "offsite", f"redirected off the allowlist to {visit.final_url}")
                    self._log(depth, "offsite", url, f"redirected to {visit.final_url}; not followed")
                return

            if final != url and self.store.has_page(final):
                # Another URL already led here: just remember this one is an alias.
                self.store.add_alias(url, final)
                if is_entry:
                    self.store.update_entry(url, new_node_url=final, status="ok")
                self._log(depth, visit.http_status, url, f"same page as {final}")
                return

            data = extract_page(visit.html, final, self.config.win.form_selector)
            block = detect_block(visit.http_status, data.title, data.text)
            if block:
                d = self._ask(Problem(BLOCKED, url, block, loaded=False))
                if d.action == RETRY:
                    continue
                self._save_failed(url, depth, "skipped", f"blocked: {block}", win=d.action == MARK_WIN)
                self._follow_operator_url(url, depth, d)
                return

            consent_cleared = self._dismiss_consent(page)
            decision: Decision | None = None
            if not consent_cleared:
                decision = self._ask(Problem(CONSENT, url, "a cookie banner is still covering the page", loaded=True))
                if decision.action == RETRY:
                    continue

            links = [
                LinkRecord(lk.href, lk.url, lk.text, lk.region, bool(lk.url and self.scope.in_scope(lk.url)))
                for lk in extract_links(visit.html, final, self.scope.strip_query_params,
                                        self.scope.region_selectors.model_dump())
            ]
            crawlable = {lk.url for lk in links if lk.in_scope and lk.url != final}
            # A win page with nowhere to go is fine: the journey is already complete.
            if not crawlable and decision is None and not self.config.win.url_matches(final):
                decision = self._ask(Problem(NO_LINKS, url, "this page has no links the crawler can follow", loaded=True))
                if decision.action == RETRY:
                    continue
            break

        record = self._page_record(page, url, final, depth, visit, data, decision)
        self.store.save_page(record, links, queue_url=url)
        if is_entry:
            self.store.update_entry(url, new_node_url=final, status="ok")

        new = 0
        if depth + 1 <= self.config.crawl.max_depth:
            for target in sorted(crawlable):
                new += self.store.enqueue(self.store.resolve(target), depth + 1, final)
        self._log(depth, visit.http_status, final,
                  f"{visit.load_ms} ms · {len(crawlable)} links ({new} new)"
                  + (" · win URL but form not rendered" if record.win and data.form_present is False else ""),
                  win=record.win)
        if decision:
            self._follow_operator_url(final, depth, decision)
        if self.headed:
            self._overlay(page, depth, record.win)

    def _page_record(self, page, url, final, depth, visit: Visit, data, decision: Decision | None) -> PageRecord:
        win_cfg = self.config.win
        url_win = win_cfg.url_matches(final)
        win = url_win and (data.form_present is not False or not win_cfg.require_form)
        win_source = "pattern" if win else None
        if decision and decision.action == MARK_WIN and not win:
            win, win_source = True, "operator"
        raw_len = self._raw_text_len(final)
        rendered_len = len(data.text)
        return PageRecord(
            url=final,
            requested_url=url,
            status="ok" if (visit.http_status or 200) < 400 else "http_error",
            depth=depth,
            http_status=visit.http_status,
            load_ms=visit.load_ms,
            redirect_chain=[c for c in (self.scope.normalize(u) for u in visit.redirect_chain) if c],
            canonical=data.canonical,
            title=data.title,
            meta_description=data.meta_description,
            headings=data.headings,
            body_text=data.text,
            form_present=data.form_present,
            jsonld_types=data.jsonld_types,
            raw_text_len=raw_len,
            rendered_text_len=rendered_len,
            # Content "only exists after JavaScript" if the raw response has
            # under half the rendered text (ignoring near-empty pages).
            js_dependent=None if raw_len is None else (rendered_len >= 200 and raw_len < rendered_len / 2),
            screenshot=self._screenshot(page, final),
            win=win,
            win_source=win_source,
        )

    def _follow_operator_url(self, src: str, depth: int, decision: Decision) -> None:
        if decision.action == MARK_WIN:
            self.store.mark_win(src, "operator")
        if decision.action == GOTO_URL and decision.url:
            self.store.add_operator_link(src, decision.url)
            self.store.enqueue(self.store.resolve(decision.url), depth + 1, src)
            self._log(depth, "operator", src, f"operator jump → {decision.url}")

    def _overlay(self, page, depth: int, win: bool) -> None:
        """A small status badge in the corner so people watching know where the crawl is."""
        text = (f"pathcrawl · depth {depth} · {self.store.explored_count()}/{self.config.crawl.max_pages} pages"
                + (" · WIN" if win else ""))
        try:
            page.evaluate(
                """t => { const d = document.createElement('div'); d.textContent = t;
                d.style.cssText = 'position:fixed;bottom:12px;right:12px;z-index:2147483647;padding:6px 10px;'
                  + 'background:#111;color:#fff;font:13px system-ui;border-radius:6px;opacity:.85';
                document.body.appendChild(d); }""",
                text,
            )
        except Exception:
            pass

    def _log(self, depth: int, status: object, url: str, note: str, win: bool = False) -> None:
        badge = "  [bold green]WIN[/]" if win else ""
        self.console.print(f"[dim]d{depth}[/] {status!s:>8}  {escape(url)}  [dim]{escape(note)}[/]{badge}", highlight=False)


def snapshot_config(config_path: Path, run_dir: Path) -> Path:
    """Keep the exact config a run used next to its data, for resume and audit."""
    dest = run_dir / "config.yaml"
    shutil.copyfile(config_path, dest)
    return dest

