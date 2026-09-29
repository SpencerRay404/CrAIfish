# CrAIfish
A client-agnostic tool that measures how reachable a conversion goal is from a set of known marketing entry links.

It crawls a website starting from the links in a campaign's ad, builds a link
graph, and reports how many clicks it takes to reach the "win" page, where the
journey stalls, and which parts of the site can never reach the win at all.
The question it answers is not "what's broken?" but **"how reachable is our win?"**

Nothing in the code is client-specific: every client detail lives in a YAML
config file.

> **Status:** v1 feature-complete: config validation, URL normalization, the
> headed crawler with operator prompts, every path metric, page
> categorization, and reports. Every stage is proven against a fixture site;
> see [Test gates](#test-gates). LLM scoring is stubbed for v2 (`pathcrawl/scoring/`).

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
pathcrawl validate   --config configs/ups.yaml                       # check a config
pathcrawl crawl      --config configs/ups.yaml --campaign linkedin-articles
pathcrawl report     --run runs/ups/linkedin-articles/<timestamp>        # every output file
pathcrawl analyze    --run <run dir>                                 # just the metrics -> analysis.json
pathcrawl categorize --run <run dir>                                 # just the page categories -> categories.csv
pathcrawl selftest                                                   # the test gate (see below)
```

**Try it on a live site first.** `configs/demo.yaml` crawls
[quotes.toscrape.com](https://quotes.toscrape.com), a public site built for
scraping practice, with its login page as the "win":

```bash
pathcrawl crawl  --config configs/demo.yaml --campaign demo-quotes
pathcrawl report --run runs/demo/demo-quotes/<timestamp>
```

The site's only link to the login page is in its header, so the report shows
the two modes disagreeing: every page is 1 click from the win counting all
links, and no page reaches it through content links.

**Mapping a whole site.** A campaign doesn't have to be an ad: seed it with the
home page and let the crawler map everything in scope. `configs/ups.yaml` has
one (`site-map-us-en`). Raise the limits for the run from the command line:

```bash
pathcrawl crawl  --config configs/ups.yaml --campaign site-map-us-en --headless
pathcrawl report --run runs/ups/site-map-us-en/<timestamp>
```

The UPS config allows 500 pages per run; add `--max-pages 2000` for a bigger
map. At the default 1.5 s delay, 500 pages take about 20–30 minutes. Quit any time
with `q` (or Ctrl-C at a prompt) and continue with `--resume`; the limits you
passed are saved with the run.

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
| `--max-pages N`, `--max-depth N` | override the config's limits for this run (kept on resume) |

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

**The win is the end of the journey.** A URL matching `win.url_patterns` is a
win as soon as a crawled page links to it. The crawler records it without
loading it (status `not_fetched`) and never follows its links. A win URL that
robots.txt forbids is still a win (status `robots`). The report says which
wins were matched by URL but not loaded, since the link is confirmed but the
form itself wasn't checked. With `require_form: true` the win is loaded to
check the form, and its links are still not followed.

### Run directory

Each crawl writes to `runs/<client>/<campaign>/<timestamp>/`:

| file | contents |
|---|---|
| `config.yaml` | the exact config the run used (resume reads this) |
| `crawl.db` | SQLite: pages, links, redirects, queue, operator actions |
| `screenshots/` | one full-page JPEG per page |
| `report.md` | the human-readable report (see below) |
| `report.json` | every metric, the categories summary, crawl facts, operator actions |
| `graph.graphml` | the full link graph with page attributes, no layout |
| `graph.gexf` | the full link graph laid out for Gephi (see below) |
| `graph_content_only.gexf` | the same without nav, header and footer links |
| `paths.mmd` | Mermaid diagram of each entry link's shortest path and nearest dead zone |
| `categories.csv` | one row per page: section, page type, reachability, content signals |
| `analysis.json` | the raw metrics, written by `pathcrawl analyze` |

**Gephi.** Open `graph.gexf` (or the less tangled `graph_content_only.gexf`),
choose "Append to existing workspace" or a new one, and it opens laid out
instead of stacked on one point. Positions come from a weighted spring layout
(content links pull harder than nav links). Size follows how many pages link
in. Colour shows the role: blue entry, green win, grey crawled, orange not
crawled. Only entries, wins and the ten most-linked pages are labelled. Edges
carry `region` (body, nav, header, footer, or several) and `content_link`.
Node attributes include `role`, `section`, `page_type`, `status` and
`in_degree`, for filtering and partitioning. To re-run the layout in Gephi,
ForceAtlas 2 with "Prevent overlap" works well.

At the end of every crawl the terminal prints a summary (pages loaded, skipped,
win pages found). If nothing loaded at all it says so in red with the first
error, and `crawl` exits with an error.

## Reading the report

`report.md` opens with a one-paragraph headline in plain language, then:

1. **Entry links**: for each link in the ad, the shortest and longest simple
   path to the win in both modes, and the click at which the journey can first
   fall into a dead zone. Read the *content only* columns first: they show
   whether the page content itself leads people to the win. The *all links*
   columns include the site navigation, which usually makes everything look
   close.
2. **Shortest journeys**: the actual pages, and a Mermaid diagram (GitHub and
   VS Code render it): blue = entry link, green = win, red dashed = the nearest
   way into a dead zone.
3. **Dead zones**: dead ends, trap loops, and pages marked *unknown* because
   they lead only to pages the crawl didn't reach. A large *unknown* count
   means the crawl was cut short; raise `--max-pages` or `--max-depth`.
4. **Distance to the win**: how many pages sit 1, 2, 3… clicks from the win.
5. **Convergence**: whether all the ad's links lead to the same win.
6. **Operator dependency**: journeys that only worked because the operator
   jumped (`[u]`) or marked a win (`[w]`). A real visitor likely couldn't
   complete them.
7. **Site map: page categories**: every page grouped by site section and page
   type, with the share that can reach the win in each mode, plus content
   signals that matter for AI and agent readability (JavaScript-only content,
   missing structured data, missing H1 or meta description, slow pages).
8. **Metric definitions**: the exact meaning of every number above.

Page types come from generic URL patterns (`support`, `tool`, `content`,
`corporate`, `product/service`, `account`, `home`, `other`; see
`PAGE_TYPE_RULES` in `pathcrawl/categorize.py`). They are a first cut: check
`categories.csv` and adjust the rules if a site names things differently.

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
| `report` | every report file is written for that crawl, and its numbers and each page's category match | step 4 |

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
- **`locale_hosts`** (optional): the hosts the locale filters apply to. Empty
  means all of them. Use it when only some hosts put the locale in the path,
  e.g. `www.ups.com/us/en/...` but `solutions.ups.com/some-page.html`.
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
  When false the win is never loaded at all (see "The win is the end of the
  journey"). When true, the win is loaded and the form must be present.
- **`near_miss_keywords`** (optional): words that make a URL look like the win.
  The report warns about pages whose path contains one but matches no pattern
  (a variant the patterns miss, such as `virtual-consultation-2023-...` next
  to `virtual-consultation-us-en*`). By default they come from the patterns:
  the first two words of each glob's last path segment.
  List several patterns if several pages count as the win.

### `campaigns`
Each campaign is one ad (or a set of related ads). `id` names the output
directory. `ad_copy` is the full ad text, stored for later relevance scoring.
`ad_urls` (optional) lists where the ads live, such as LinkedIn posts; they're
shown in the report and never crawled. `entry_links` are the links inside
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
