"""The real crawler, headless, against small local sites built for each case.

Every operator prompt, the resume path and the hard safety rules are exercised
here. These tests need Chromium; see conftest.py.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pathcrawl.config import parse_config
from pathcrawl.crawler import (
    BLOCKED,
    CONSENT,
    GOTO_URL,
    MARK_WIN,
    NAV_ERROR,
    NO_LINKS,
    OFFSITE_REDIRECT,
    QUIT,
    SKIP,
    Crawler,
    Decision,
    NonInteractiveOperator,
    ScriptedOperator,
)
from pathcrawl.graph import CONTENT_ONLY, analyze, graph_from_store

pytestmark = pytest.mark.browser

NAV = '<nav><a href="/win.html">Contact</a></nav>'


def html(body: str, head: str = "") -> str:
    return f"<!doctype html><html><head><title>t</title>{head}</head><body>{body}</body></html>"


class Site:
    """A tiny programmable web server: path -> (status, headers, body)."""

    def __init__(self, routes: dict[str, tuple[int, dict, str]]):
        self.routes = routes
        self.requests: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                site.requests.append(self.path)
                status, headers, body = site.routes.get(self.path, (404, {}, html("not found")))
                self.send_response(status)
                headers = {"Content-Type": "text/html; charset=utf-8", **headers}
                for k, v in headers.items():
                    self.send_header(k, v.replace("{base}", site.base))
                self.end_headers()
                self.wfile.write(body.replace("{base}", site.base).encode())

            do_POST = do_GET  # noqa: N815 - recorded so a form submission would be caught

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def make_site():
    sites = []

    def _make(routes):
        s = Site(routes)
        sites.append(s)
        return s

    yield _make
    for s in sites:
        s.close()


def config_for(site: Site, entries: list[str], **crawl):
    return parse_config(
        {
            "client": {"name": "Test", "slug": "test"},
            "scope": {"allowed_domains": ["127.0.0.1"]},
            "win": {"name": "Win", "url_patterns": [site.base + "/win.html"], "form_selector": "form#lead"},
            "campaigns": [
                {
                    "id": "c",
                    "name": "C",
                    "platform": "test",
                    "ad_copy": "ad",
                    "entry_links": [{"label": e.strip("/"), "url": site.base + e} for e in entries],
                }
            ],
            "crawl": {"delay_ms": 0, "headed": False, "page_timeout_ms": 5000, **crawl},
        }
    )


def crawl(cfg, tmp_path, operator=None):
    c = Crawler(cfg, cfg.campaigns[0], tmp_path, operator or NonInteractiveOperator(), headed=False)
    status = c.run()
    return c, status


def page_row(crawler, url):
    return crawler.store.db.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()


WIN = (200, {}, html(NAV + '<form id="lead" action="/submitted" method="post"><input name="e"><button>Go</button></form>'))


# ------------------------------------------------------------------ operator prompts


def test_block_prompts_operator_and_skip_leaves_page_unexplored(make_site, tmp_path):
    site = make_site({
        "/start.html": (200, {}, html(NAV + '<a href="/wall.html">Next</a>')),
        "/wall.html": (403, {}, html("<h1>Access denied</h1>")),
        "/win.html": WIN,
    })
    op = ScriptedOperator([Decision(SKIP)])
    c, status = crawl(config_for(site, ["/start.html"]), tmp_path, op)
    assert status == "complete"
    assert [p.kind for p in op.problems] == [BLOCKED]
    assert page_row(c, site.base + "/wall.html")["status"] == "skipped"
    g, _ = graph_from_store(c.store)
    assert g.nodes[site.base + "/wall.html"]["explored"] is False


def test_navigation_error_prompts_and_retry_then_skip(make_site, tmp_path):
    site = make_site({
        # Same host, closed port: in scope, but the connection is refused.
        "/start.html": (200, {}, html(NAV + '<a href="http://127.0.0.1:1/x.html">Broken</a>')),
        "/win.html": WIN,
    })
    op = ScriptedOperator([Decision("retry"), Decision(SKIP)])
    c, _ = crawl(config_for(site, ["/start.html"]), tmp_path, op)
    assert [p.kind for p in op.problems] == [NAV_ERROR, NAV_ERROR]
    assert page_row(c, "http://127.0.0.1:1/x.html")["status"] == "skipped"
    actions = [r["action"] for r in c.store.operator_actions()]
    assert actions == ["retry", "skip"]


def test_entry_redirecting_off_allowlist_prompts(make_site, tmp_path):
    site = make_site({"/win.html": WIN})
    # "localhost" is the same server but a different host, so it is off the allowlist.
    site.routes["/go"] = (302, {"Location": f"http://localhost:{site.port}/win.html"}, "")
    op = ScriptedOperator([Decision(SKIP)])
    c, _ = crawl(config_for(site, ["/go"]), tmp_path, op)
    assert [p.kind for p in op.problems] == [OFFSITE_REDIRECT]
    assert c.store.entries()[0]["status"] == "offsite"
    # never crawled the off-allowlist host
    assert not c.store.has_page(f"http://localhost:{site.port}/win.html")


def test_no_links_operator_url_becomes_flagged_operator_edge(make_site, tmp_path):
    site = make_site({
        "/start.html": (200, {}, html('<p>No way forward.</p>')),
        "/win.html": WIN,
    })
    op = ScriptedOperator([Decision(GOTO_URL, site.base + "/win.html")])
    c, _ = crawl(config_for(site, ["/start.html"]), tmp_path, op)
    assert [p.kind for p in op.problems] == [NO_LINKS]
    start = page_row(c, site.base + "/start.html")
    assert start["status"] == "ok"  # the page itself loaded and is kept
    g, entries = graph_from_store(c.store)
    assert g.edges[site.base + "/start.html", site.base + "/win.html"]["operator"] is True
    res = analyze(g, entries, max_depth=5)
    assert res.operator_edges == [(site.base + "/start.html", site.base + "/win.html")]
    assert res.modes[CONTENT_ONLY].operator_dependent_entries == ["start.html"]


def test_operator_can_mark_a_page_as_win(make_site, tmp_path):
    site = make_site({"/start.html": (200, {}, html("<p>Thanks, we'll call you.</p>"))})
    c, _ = crawl(config_for(site, ["/start.html"]), tmp_path, ScriptedOperator([Decision(MARK_WIN)]))
    row = page_row(c, site.base + "/start.html")
    assert row["win"] == 1 and row["win_source"] == "operator"


def test_quit_saves_and_resume_finishes_without_duplicates(make_site, tmp_path):
    site = make_site({
        "/start.html": (200, {}, html(NAV + '<a href="/a.html">A</a>')),
        "/a.html": (200, {}, html("<p>dead end</p>")),
        "/win.html": WIN,
    })
    cfg = config_for(site, ["/start.html"])
    c1, status = crawl(cfg, tmp_path, ScriptedOperator([Decision(QUIT)]))
    assert status == "quit"
    assert c1.store.meta("status") == "quit"
    assert not c1.store.has_page(site.base + "/a.html")  # the page in flight was not saved
    c1.store.close()

    c2, status = crawl(cfg, tmp_path)  # same run directory: resumes
    assert status == "complete"
    urls = [r["url"] for r in c2.store.pages()]
    assert sorted(urls) == sorted({site.base + p for p in ("/start.html", "/a.html", "/win.html")})
    assert site.requests.count("/start.html") == 2  # page load + raw fetch, once, not re-crawled


# ------------------------------------------------------------------ scope, redirects, robots, budget


def test_redirects_resolve_to_one_node(make_site, tmp_path):
    site = make_site({
        "/start.html": (200, {}, html(NAV + '<a href="/old">Old link</a>')),
        "/old": (301, {"Location": "/win.html"}, ""),
        "/win.html": WIN,
    })
    c, _ = crawl(config_for(site, ["/start.html"]), tmp_path)
    assert c.store.resolve(site.base + "/old") == site.base + "/win.html"
    g, entries = graph_from_store(c.store)
    assert site.base + "/old" not in g
    assert g.edges[site.base + "/start.html", site.base + "/win.html"]["regions"] == {"nav", "body"}


def test_robots_txt_is_respected(make_site, tmp_path):
    site = make_site({
        "/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /private\n"),
        "/start.html": (200, {}, html(NAV + '<a href="/private.html">Private</a>')),
        "/private.html": (200, {}, html("secret")),
        "/win.html": WIN,
    })
    c, _ = crawl(config_for(site, ["/start.html"]), tmp_path)
    assert page_row(c, site.base + "/private.html")["status"] == "robots"
    assert "/private.html" not in site.requests


def test_off_allowlist_and_out_of_locale_links_are_never_loaded(make_site, tmp_path):
    site = make_site({
        "/en/start.html": (200, {}, html(
            '<a href="/en/win.html">Win</a><a href="/fr/page.html">FR</a>'
            '<a href="https://ads.example.net/click">Ad</a>'
        )),
        "/en/win.html": WIN,
    })
    cfg = config_for(site, ["/en/start.html"])
    cfg.scope.locale_include = ["/en/"]
    cfg.win.url_patterns = [site.base + "/en/win.html"]
    c, _ = crawl(cfg, tmp_path)
    assert "/fr/page.html" not in site.requests
    links = {r["url"]: r["in_scope"] for r in c.store.links()}
    assert links[site.base + "/fr/page.html"] == 0
    assert links["https://ads.example.net/click"] == 0


def test_page_budget_stops_the_crawl(make_site, tmp_path):
    chain = {f"/p{i}.html": (200, {}, html(f'<a href="/p{i + 1}.html">next</a>')) for i in range(10)}
    site = make_site(chain)
    c, status = crawl(config_for(site, ["/p0.html"], max_pages=3), tmp_path)
    assert status == "budget"
    assert c.store.explored_count() == 3


def test_max_depth_limits_how_far_the_crawl_goes(make_site, tmp_path):
    chain = {f"/p{i}.html": (200, {}, html(f'<a href="/p{i + 1}.html">next</a>')) for i in range(10)}
    site = make_site(chain)
    c, _ = crawl(config_for(site, ["/p0.html"], max_depth=2), tmp_path)
    assert c.store.explored_count() == 3  # depths 0, 1, 2
    g, _ = graph_from_store(c.store)
    assert g.nodes[site.base + "/p3.html"]["explored"] is False


# ------------------------------------------------------------------ page data and safety


def test_page_data_is_captured(make_site, tmp_path):
    jsonld = '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Article"}</script>'
    js_only = "<div id=app></div><script>document.getElementById('app').textContent = '" + "Rendered by JS. " * 40 + "'</script>"
    site = make_site({
        "/start.html": (200, {}, html(
            NAV + "<h1>Main</h1><h2>Sub</h2><p>" + "Static text. " * 40 + "</p><a href='/js.html'>js</a>",
            head='<meta name="description" content="A test page"><link rel="canonical" href="/start.html">' + jsonld,
        )),
        "/js.html": (200, {}, html(NAV + js_only)),
        "/win.html": WIN,
    })
    cfg = config_for(site, ["/start.html"])
    cfg.win.require_form = True  # the win is loaded only to check its form
    c, _ = crawl(cfg, tmp_path)
    start = page_row(c, site.base + "/start.html")
    assert start["meta_description"] == "A test page"
    assert start["canonical"] == site.base + "/start.html"
    assert start["headings"] == '[[1, "Main"], [2, "Sub"]]'
    assert start["jsonld_types"] == '["Article"]'
    assert start["js_dependent"] == 0
    assert start["screenshot"] and (tmp_path / start["screenshot"]).exists()
    assert "Static text." in start["body_text"]
    assert page_row(c, site.base + "/js.html")["js_dependent"] == 1

    win = page_row(c, site.base + "/win.html")
    assert win["win"] == 1 and win["win_source"] == "pattern" and win["form_present"] == 1


WIN_WITH_NEXT_PAGE = (200, {}, html(
    '<form id="lead" action="/submitted" method="post"><button>Send</button></form>'
    '<a href="/thanks.html">After</a>'
))


def test_win_is_terminal_and_not_loaded(make_site, tmp_path):
    site = make_site({
        "/start.html": (200, {}, html(NAV)),
        "/win.html": WIN_WITH_NEXT_PAGE,
        "/thanks.html": (200, {}, html(NAV)),
    })
    c, _ = crawl(config_for(site, ["/start.html"]), tmp_path)
    assert "/win.html" not in site.requests  # matched by URL: nothing to load
    win = page_row(c, site.base + "/win.html")
    assert win["status"] == "not_fetched" and win["win"] == 1 and win["win_source"] == "pattern"
    assert not c.store.has_page(site.base + "/thanks.html")
    assert c.store.explored_count() == 1


def test_forms_are_never_submitted_and_win_links_not_followed(make_site, tmp_path):
    site = make_site({
        "/start.html": (200, {}, html(NAV)),
        "/win.html": WIN_WITH_NEXT_PAGE,
        "/thanks.html": (200, {}, html(NAV)),
    })
    cfg = config_for(site, ["/start.html"])
    cfg.win.require_form = True  # loaded to check the form, still terminal
    c, _ = crawl(cfg, tmp_path)
    assert "/win.html" in site.requests and "/submitted" not in site.requests
    assert page_row(c, site.base + "/win.html")["form_present"] == 1
    assert not c.store.has_page(site.base + "/thanks.html")
    assert "/thanks.html" not in site.requests


def test_robots_blocked_win_still_counts(make_site, tmp_path):
    """Regression: a win URL forbidden by robots.txt used to be saved as a
    non-win, so every journey reported "no path to the win"."""
    from pathcrawl.report import write_report
    from pathcrawl.run import open_run
    import yaml

    site = make_site({
        "/robots.txt": (200, {"Content-Type": "text/plain"}, "User-agent: *\nDisallow: /win.html\n"),
        "/start.html": (200, {}, html('<main><a href="/win.html">Talk to an expert</a></main>')),
        "/win.html": WIN,
    })
    cfg = config_for(site, ["/start.html"])
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
    c, _ = crawl(cfg, tmp_path)
    assert "/win.html" not in site.requests
    assert page_row(c, site.base + "/win.html")["status"] == "robots"
    c.store.close()

    run = open_run(tmp_path)
    win_url = site.base + "/win.html"
    assert run.graph.nodes[win_url]["win"] is True
    for mode in ("all_links", "content_only"):
        res = run.analyze().modes[mode]
        assert res.entries[0].shortest_clicks == 1
        assert res.entries[0].shortest_path == [site.base + "/start.html", win_url]
        assert res.dead_zones.unknown == [] and res.dead_zones.unexplored_reachable == []
    write_report(run)
    report = (tmp_path / "report.md").read_text()
    assert "win page not fetched: blocked by robots.txt" in report
    import json
    data = json.loads((tmp_path / "report.json").read_text())
    assert data["win_pages"][0]["fetched"] is False
    assert data["win_pages"][0]["not_fetched_reason"] == "blocked by robots.txt"
    run.close()


def test_consent_banner_is_dismissed(make_site, tmp_path):
    banner = (
        '<div id="onetrust-banner-sdk">We use cookies '
        "<button id=\"onetrust-accept-btn-handler\" onclick=\"this.parentNode.style.display='none'\">OK</button></div>"
    )
    site = make_site({"/start.html": (200, {}, html(NAV + banner)), "/win.html": WIN})
    op = ScriptedOperator([])
    crawl(config_for(site, ["/start.html"]), tmp_path, op)
    assert [p.kind for p in op.problems] == []


def test_stuck_consent_banner_prompts_operator(make_site, tmp_path):
    banner = '<div id="onetrust-banner-sdk">We use cookies and there is no button.</div>'
    site = make_site({"/start.html": (200, {}, html(NAV + banner)), "/win.html": WIN})
    op = ScriptedOperator([Decision(SKIP)])
    c, _ = crawl(config_for(site, ["/start.html"]), tmp_path, op)
    assert [p.kind for p in op.problems] == [CONSENT]
    assert page_row(c, site.base + "/start.html")["status"] == "ok"  # accepted as is


def test_closing_the_browser_saves_and_resumes(make_site, tmp_path):
    """Closing the window mid-crawl is a save-and-quit, not a crash."""
    site = make_site({
        "/start.html": (200, {}, html(NAV + '<a href="/a.html">A</a>')),
        "/a.html": (200, {}, html(NAV + "<p>a</p>")),
        "/win.html": WIN,
    })
    cfg = config_for(site, ["/start.html"])

    class ClosesWindowAfterFirstPage(Crawler):
        def _process(self, url, depth):
            super()._process(url, depth)
            self.page.close()  # what happens when someone closes the visible window

    c1 = ClosesWindowAfterFirstPage(cfg, cfg.campaigns[0], tmp_path, NonInteractiveOperator(), headed=False)
    assert c1.run() == "quit"
    assert c1.store.meta("status") == "quit"
    assert c1.store.explored_count() == 1
    assert c1.store.operator_actions() == []  # not mistaken for a navigation error
    c1.store.close()

    c2, status = crawl(cfg, tmp_path)
    assert status == "complete"
    assert c2.store.explored_count() == 2  # the win is recorded, not loaded


def test_file_download_instead_of_page_is_named_and_recorded(make_site, tmp_path):
    """Some servers answer certain clients with a file instead of the page."""
    from pathcrawl.crawler import DOWNLOAD

    site = make_site({
        "/start.html": (200, {"Content-Type": "application/octet-stream",
                              "Content-Disposition": 'attachment; filename="home.bin"'}, "binary"),
    })
    op = ScriptedOperator([Decision(SKIP)])
    c, _ = crawl(config_for(site, ["/start.html"]), tmp_path, op)
    assert [p.kind for p in op.problems] == [DOWNLOAD]
    detail = op.problems[0].detail
    assert "HTTP 200" in detail and "application/octet-stream" in detail and "home.bin" in detail
    assert "headless" in detail and "HeadlessChrome" in detail
    assert page_row(c, site.base + "/start.html")["error"].startswith(DOWNLOAD)
    assert "HeadlessChrome" in c.store.meta("user_agent")
