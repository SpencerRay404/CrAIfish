# CrAIfish
A client-agnostic tool that measures how reachable a conversion goal is from a set of known marketing entry links.

It crawls a website starting from the links in a campaign's ad, builds a link
graph, and reports how many clicks it takes to reach the "win" page, where the
journey stalls, and which parts of the site can never reach the win at all.
The question it answers is not "what's broken?" but **"how reachable is our win?"**

Nothing in the code is client-specific: every client detail lives in a YAML
config file.

> **Status:** v1 in progress. Built so far: config validation, URL
> normalization, every path metric, and the headed crawler with operator
> prompts. Every stage is proven against a fixture site; see [Test gates](#test-gates).
> Coming next: reports (`report.md`, `report.json`, GraphML, Mermaid).

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
pytest
```

## Usage

```bash
pathcrawl validate --config configs/ups.yaml                  # check a config
pathcrawl crawl    --config configs/ups.yaml --campaign tl-2026-q3-01
pathcrawl analyze  --run runs/ups/tl-2026-q3-01/<timestamp>   # every metric -> analysis.json
pathcrawl selftest                                            # the test gate (see below)
```

`validate` either prints a summary of the config or lists every problem with
its location in the file (for example `crawl.max_dept: Extra inputs are not
permitted`). It also warns about things that are valid but probably wrong, such
as leftover `REPLACE-ME` placeholders. `crawl` refuses to start while any
placeholders remain.

### Crawling

`crawl` opens a visible Chromium window, slowed down (`slow_mo_ms`) so people
can watch it. It starts from the campaign's entry links and works outward
breadth-first: every page one click away, then two clicks, and so on, up to
`max_depth` clicks and `max_pages` pages. A small badge in the corner of the
browser shows the depth and page count. The terminal prints one line per page:
its depth, HTTP status, load time, links found, and **WIN** when it hits the
win page.

What it records for each page: the requested and final URL and redirect chain,
the HTTP status and load time, title, meta description, the H1-H6 outline,
the full visible text, the canonical tag, JSON-LD types, whether the win form
rendered, a full-page screenshot, and every link with its anchor text and
region (nav, header, footer or body). It also fetches each page without
JavaScript and flags pages whose content only appears after scripts run.

Options:

| flag | effect |
|---|---|
| `--headless` | no visible browser |
| `--non-interactive` | never pause: accept pages that loaded, skip pages that failed |
| `--resume RUN_DIR` | continue a run that was quit or crashed |
| `--runs-dir DIR` | where run directories go (default `runs/`) |

**When it gets stuck, it asks you.** It pauses on a block, CAPTCHA, 403 or
429, on a timeout or navigation error (after one automatic retry in a fresh
tab), on an entry link that redirects off the allowed domains, on a page with
no links it can follow, and on a cookie banner it couldn't dismiss:

```
[r] retry   [s] skip this page   [u] enter a URL to continue to   [w] mark this page as a win   [q] save and quit
```

- `u` records an **operator edge**: a jump that you made, not a link on the
  site. Operator edges are flagged separately in every metric, because a real
  visitor couldn't make that jump.
- `w` marks the page as a win (flagged as operator-marked).
- `q` saves everything. `pathcrawl crawl --resume <run dir>` picks up where
  the crawl stopped. A crash is equally safe: each page is saved the moment
  it's crawled.

**Hard rules.** The crawler only ever loads URLs on the allowed domains that
pass the locale filters. It never clicks links: it loads their URLs directly.
The only thing it clicks is a cookie-consent button. It never fills in or
submits a form and never logs in. It obeys robots.txt (unless
`respect_robots: false`) and waits `delay_ms` between pages.

### Run directory

Each crawl writes to `runs/<client>/<campaign>/<timestamp>/`:

| file | contents |
|---|---|
| `config.yaml` | the exact config the run used (resume reads this) |
| `crawl.db` | SQLite: pages, links, redirects, queue, operator actions |
| `screenshots/` | one full-page JPEG per page |
| `analysis.json` | every metric, written by `pathcrawl analyze` |

## Test gates

Each build step has to pass these before it merges. You can run all of them
yourself; CI runs the same ones on every pull request
(`.github/workflows/tests.yml`).

```bash
pytest                    # unit tests
pathcrawl selftest            # expected vs actual for every metric, on the fixture site
pathcrawl selftest --headed   # same, and watch the browser crawl it
```

The browser tests and the `crawl` stage need Chromium (`playwright install
chromium`). If Playwright's own browser can't be installed, point
`PATHCRAWL_CHROMIUM` at a Chromium executable. Locally, browser tests are
skipped with a reason when Chromium is missing; CI requires them.

`pathcrawl selftest` runs every stage built so far against the fixture site in
`tests/fixtures/site/` and prints one row per metric with a ✓ or ✗, comparing
the result with the hand-worked numbers in `tests/fixtures/site/expected.yaml`.
It exits non-zero if anything differs.

| stage | what it proves | added in |
|---|---|---|
| `graph` | link extraction and every path metric, reading the fixture HTML directly | step 2 |
| `crawl` | a real headless-browser crawl of the served fixture site reproduces the same numbers (`--headed` to watch it) | step 3 |
| `report` | report files are written, and their numbers match the analysis | step 4 |

To see the fixture site the numbers describe, serve it and click around:

```bash
python -m http.server -d tests/fixtures/site 8000   # then open http://localhost:8000/entry-far.html
```

## Writing a client config

Copy `configs/example.yaml` to `configs/<client>.yaml`. The sections:

### `client`
`name` is shown in reports. `slug` (lowercase letters, digits, `-`, `_`) names
the output directory `runs/<slug>/...`.

### `scope`: where the crawl may go
- **`allowed_domains`**: bare hostnames. The crawl never leaves these.
  Subdomains are not implied: list `www.example.com` and `blog.example.com`
  separately.
- **`locale_include` / `locale_exclude`**: optional case-insensitive
  substrings of the URL path. With `locale_include: ["/us/en/"]`, only paths
  containing `/us/en/` are crawled. These filters apply to entry links too,
  so `validate` rejects an entry link that the filters would exclude.
- **`strip_query_params`**: query params removed before URLs are compared.
  Globs are allowed (`utm_*`), and matching ignores case.
- **`region_selectors`** (optional): extra CSS selectors for `nav`,
  `header` and `footer`. Links inside `<nav>`, `<header>` and `<footer>`
  (and the matching ARIA roles) are classified automatically. Add selectors
  here for menus built from plain `<div>`s, e.g. `nav: [".global-nav"]`.
  This matters because the "content links only" metrics exclude those
  regions.

### `win`: the conversion goal
- **`url_patterns`**: a page is a win if its URL matches any pattern. A plain
  pattern is a glob matched against the whole URL (`*` also matches `/`). A
  pattern starting with `re:` is a Python regex that must match the whole
  URL. Patterns are matched against the normalized URL, which has a lowercase
  host, no fragment, and no stripped params.
- **`form_selector`** (optional): a CSS selector that confirms the win form
  rendered on the page.
- **`require_form`** (default `false`): when false, a URL match counts as a win
  even if the form is missing, and the report flags it as "form not rendered".
  When true, the form must be present.

### `campaigns`
Each campaign is one ad. `id` names the output directory. `ad_copy` is the full
ad text, stored for later relevance scoring. `entry_links` are the links inside
the ad, each with a `label` (the anchor text or a description) and a `url`.

Entry links must start on an allowed domain. If the ad uses a link shortener
such as `lnkd.in`, paste the destination URL instead. v1 never visits social
platforms.

### `crawl`
| key | default | meaning |
|---|---|---|
| `max_depth` | 8 | max clicks from an entry link |
| `max_pages` | 300 | cap on pages fetched per campaign |
| `delay_ms` | 1500 | pause between page loads |
| `headed` | true | visible browser (`--headless` overrides) |
| `slow_mo_ms` | 400 | slow each browser action so people can watch |
| `respect_robots` | true | obey robots.txt |
| `page_timeout_ms` | 30000 | navigation timeout |
| `screenshot` | true | save a screenshot per page |

## How URLs are normalized

Pages are identified by their final URL after redirects, normalized so that
trivial variants become one graph node:

- Relative links are resolved.
- Scheme and host are lowercased, a trailing dot on the host is removed, and
  default ports are dropped.
- The fragment is dropped.
- Configured query params are stripped, and the remaining params are sorted.
- An empty path becomes `/`. Path case and trailing slashes are kept, because
  servers can serve different pages for them.

A page's `rel=canonical` tag is recorded but never used to merge pages.
