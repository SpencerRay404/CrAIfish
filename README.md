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
pathcrawl entities extract --run <run dir>                           # tag pages with taxonomy entities
pathcrawl entities propose --run <run dir>                           # local LLM suggests missing entities (for review)
pathcrawl entities accept  --proposals <csv> --taxonomy <yaml>       # add the reviewed suggestions
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
| `entities.yaml` | copy of the taxonomy the last entity extraction used |
| `page_entities.csv` | one row per page and entity: type, score, evidence (title, heading, body) |
| `entity_coverage.csv` | per entity: pages, links to the win, median clicks, share within 2 clicks, flag |
| `bridge_links.csv` | suggested content links that bring far pages within 2 clicks of the win |
| `<client>_knowledge_graph.gexf` | pages and entities with mention and link edges, laid out for Gephi |
| `entity_proposals.csv` | LLM-suggested entities awaiting review, from `pathcrawl entities propose` |

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

## Entities and the knowledge graph

The entity layer records what each page is about as typed entities (Industry,
Segment, Service, Topic, Customer), so journeys and ads can be reasoned about
by topic rather than by URL.

**Taxonomy.** The entities and the words that signal them live in
`configs/<client>.entities.yaml`, not in code; `configs/example.entities.yaml`
documents every setting. When that file exists, entities are extracted
automatically after each crawl and again before each report, so an edit to
the taxonomy shows up on the next `pathcrawl report` without re-crawling.
Check a file with `pathcrawl entities check --taxonomy <file>`.

**Extraction** (`pathcrawl/entities.py`) is deterministic:
1. Boilerplate is removed first. Any run of 8 words that appears on 30 or more
   pages is dropped (menus, cookie text, shared modules), and so is any
   heading on 5 or more pages (e.g. "Related stories"). Headings are taken out
   of the body text, since they are scored as headings.
2. Each entity's terms are matched in the title, the remaining headings and
   the remaining body: `score = 3 × title hits + 2 × heading hits + body hits
   (at most 5)`. A page is tagged at score 1 or more. `evidence` is the first
   place it was found, in the order title, heading, body.

Results are stored in the `page_entities` table of `crawl.db`.

**Coverage** (report section and `entity_coverage.csv`). For each entity:
- pages tagged;
- pages linking straight to the win;
- median clicks to the win;
- share of pages within 2 clicks.

Each is given with content links only and with all links. An entity with at
least `flag_min_pages` pages is flagged "no path" when none of its pages can
reach the win through content, or "mostly no path" when most can't.

**Bridge links** (report section and `bridge_links.csv`). Some tagged pages
are more than one content click from the win, or have no content path at all.
For each of them, the report lists pages about the same entities that link to
the win directly. They are ranked by shared entity score: for each shared
entity, take the lower of the two pages' scores, then add them up. A content
link to the top suggestion brings the page within two clicks.

**Knowledge graph** (`<client>_knowledge_graph.gexf`). The nodes are pages
and entities, with `node_type`, `entity_type`, `clicks_to_win` and
`pages_tagged`. `clicks_to_win` is the content-links distance; for an entity
it is the rounded median over its pages, and -1 means no path. The edges are:
- `mention`: page to entity, weighted by score;
- `link`: page to page, content links only.

Positions are precomputed. Entities are coloured by type and sized by pages
tagged.

**Suggestions from a local LLM** (optional). `pathcrawl entities propose --run
<run dir>` sends each page's text, with boilerplate removed, to a local model
behind an OpenAI-compatible endpoint. The defaults are Ollama at
`http://localhost:11434/v1` with model `hermes3`. Change them in the
taxonomy's `llm` section or with `--base-url` and `--model`, and use
`--limit 20` for a trial. The model is asked for entities and customer names
the taxonomy lacks.

A proposal is dropped if:
- its type isn't a taxonomy type;
- it is already known;
- none of its terms actually appear on the page.

The rest go to `entity_proposals.csv`, and **nothing else changes**. A reviewer
sets `decision` to `accept` on the rows to keep, and may edit the name, type
or terms first. Then:

```bash
pathcrawl entities accept --proposals <run dir>/entity_proposals.csv --taxonomy configs/<client>.entities.yaml
```

That appends only the accepted rows to the taxonomy, keeping its comments. It
checks the result, and restores the file if it no longer loads.

**Site-side recommendations.** Every report ends with recommendations for the
site itself, backed by this crawl's numbers:
- structured-data coverage and which schema.org types to add;
- how many pages depend on JavaScript for their text;
- consolidating the win and its look-alike URLs into one conversion target;
- declared page metadata (industry, journey stage, persona) to replace
  inferred tags.

## External entry points (posts collected by hand)

LinkedIn sits behind a login wall and is never crawled. Its posts can still be
entry points: list them in a CSV and set `external_seeds` on the campaign.
`data/templates/linkedin_scrape_template.csv` has the columns. Write one row
per outbound link in a post:

| column | content |
|---|---|
| `seed_url` | the post |
| `post_title` | optional |
| `outbound_url_raw` | the link as it appears (often a short link) |
| `anchor_text`, `link_order` | optional |
| `outbound_resolved_url` | where it lands, with its query string; the campaign tag is read from here |

- **Dedupe.** Posts already in `ad_urls` or `entry_links` are skipped, and
  repeated (post, landing page) pairs are dropped. Posts and landing pages are
  compared with a lowercase host and without query, fragment or trailing
  slash. A post with several links keeps them all.
- **Nodes and edges.** Each post becomes a node with status `external` and
  its platform as `channel`. `post_date_derived` comes from a LinkedIn
  activity ID, where the ID shifted right by 22 bits is epoch milliseconds.
  Each outbound link becomes a link carrying its tag, so it joins to leads
  like any other link. A link that resolves to another post adds that post
  as a seed.
- **Entry links.** A landing page on an allowed domain becomes an entry link,
  queued at depth 0, unless it already is one or is a win.
- **Privacy.** Only the post URL, title, date and links are stored.

A fresh crawl ingests the file automatically. For an existing run, use
`pathcrawl external-seeds --run <run dir> [--config ...]`, then `pathcrawl
crawl --resume <run dir>` to crawl the new entry links. Add `--max-pages` if
the run stopped at its page budget. The report gets an "External entry
points" section.

## Dead pages

A loaded page is **dead** if it answers HTTP 404 or 410, or is a soft 404
served with HTTP 200. A soft 404 is a title starting with `404` or containing
"Page Not Found", or body text saying "this page no longer exists". The
crawler records `pages.is_dead` and `dead_reason`, and `pathcrawl
backfill-links` fills them for older runs.

The report's **Dead pages** section lists:
- each dead page and why it counts as dead;
- the pages still linking to it;
- how many of those links are in the body, how many are in the nav, header
  or footer, and how many come from external posts;
- any campaign tag riding on those links.

`<client>_dead_pages.csv` has one row per link into a dead page. In
`graph.gexf` and `report.json`, nodes carry `is_dead`,
`inbound_dead_links`, `dead_inbound_pages` and `dead_inbound_body_links`,
and edges carry `to_dead`. A post's landing page that is dead keeps its edge
(flagged `to_dead`) and is never made an entry link.

## Website health

Every report has a **Website health** section, and `<client>_site_health.csv`
has one row per loaded page. The section covers dead pages, how easy content
is to reach, and how machine-readable each page is for search engines and AI
agents.

**Per page**
- **Click depth from home**, counted twice: over every link
  (`clicks_from_home_all_links`) and over body links only
  (`clicks_from_home_body_links`). The start page is `health.home_url`, or
  else the first entry link.
- **Inbound links**: `inbound_links`, plus `is_dead` and
  `dead_inbound_pages`.
- **Titles and descriptions**: `has_title`, `title_duplicated`,
  `has_meta_description` and `meta_duplicated`. Duplicate means the same
  lowercased text on more than one loaded page.
- **Structure**: `h1_count` and `canonical_self`.
- **Structured data**: `structured_data_types` (JSON-LD) and
  `has_structured_data` (JSON-LD, Microdata or RDFa), reported separately
  from `microdata_types`, `rdfa_types` and `og_properties` (Open Graph).
- **Language and indexing**: `hreflang` and `robots_meta`.
- **JavaScript dependence**: `js_dependent` (the raw HTML has under half the
  rendered text) and `raw_text_share`.
- **Redirects and leads**: `redirect_hops`, `carries_lead_tags` and
  `leads_allocated`.

Microdata, RDFa, Open Graph, hreflang and the robots meta tag are recorded
from the rendered page. Runs crawled before this change don't have them,
and the report says so.

**Per host**
- the robots.txt rules for named AI crawlers (GPTBot, ClaudeBot,
  PerplexityBot, Google-Extended and others; set `health.ai_crawlers` to
  change the list);
- whether `llms.txt` exists;
- the declared sitemaps, and how many crawled pages they list.

These are fetched once per host at the end of a crawl. Turn that off with
`health.check_site_files: false`, or fetch them for an existing run with
`pathcrawl site-signals --run <run dir>`.

**Breakdowns and graph attributes.** The report breaks the signals down by
section and by page type, with a separate row for pages carrying leads.
`graph.gexf` and `report.json` carry `is_dead`, `has_structured_data`,
`js_dependent` and both click depths.

## Lead evidence

With campaign tags kept on links (`scope.capture_params`, above), aggregated
lead counts per tag can be joined to the pages that carry each tag:

```bash
pathcrawl backfill-links --run <run dir>                      # older runs only: fill links.mc_id
pathcrawl leads  --run <run dir> --config configs/ups.yaml    # join; also runs inside `report`
pathcrawl report --run <run dir> --config configs/ups.yaml
```

The input is a CSV of **counts per tag only** (`leads.files`). The columns
are `wt_mc_id`, `leads_most_recent_tag`, `leads_source_initiative_tag`,
`paid_click_leads` and `main_conversion_page`. Other count columns, such
as `distinct_visitors`, are fine. The loader refuses any file that looks
like a raw CRM export: a column that identifies a person or visitor (a token
or tracking cookie, e-mail, lead, visitor or contact ID, IP address), a
`token:` value, or an e-mail address. `.gitignore` excludes `data/**/raw*` and
`*MKT_TRK*`.

- **Join.** Each lead tag is matched case-sensitively against `links.mc_id`.
  With `join.fallback: strip_numeric_suffix`, a tag with no exact match is
  tried again with a trailing `_NNNNN` (5 to 7 digits) removed on both sides.
- **Allocation.** A tag's leads are split evenly over the distinct pages
  whose links carry it: `exact` when one page carries the tag, `shared`
  otherwise. Rows go to the `lead_attribution` table and
  `<client>_lead_attribution.csv`.
- **Graph and JSON.** Nodes get `leads_origin` (allocated leads),
  `leads_exact`, and `leads_landed` (leads whose main conversion page is
  that page). Edges from a tagged page to its link target get `leads`.
- **Report section "Lead evidence"** lists:
  - the top pages carrying leads, and exact versus shared;
  - tags whose leads converted somewhere other than where the link points;
  - tags with leads that no crawled link carries;
  - crawled pages that link to the form without a tag;
  - tags on form links with no leads;
  - the paid-click share.

  Tags under `leads.min_cell` leads (default 5) are rolled into one
  "(other, <5 leads)" line. Full detail stays in the run folder's CSV.

## Peer comparison (FedEx, DHL, Flexport, Maersk)

Compares each peer with UPS using the same method and scope:
- how far every page is from a win;
- how deep content sits from the home page;
- machine readability;
- topic and service coverage.

This is structure and content only. There is no traffic or lead data for the
peers, so nothing in the output says how well a site converts.

1. **Confirm before running.** Each peer's terms of use, and whether crawling
   from a company network is allowed, are policy questions for Spence.
2. **Freeze the topic taxonomy.** `configs/topics.yaml` is one rule set for
   every site. Merge the explorer's UPS topic rules into it, bump `version`,
   and don't tune it per site.
3. **Crawl each site** with the same crawler and settings (pathcrawl
   compare checks the settings fingerprint):

   ```bash
   pathcrawl crawl --config configs/peers/fedex.yaml    --campaign peer-compare --non-interactive
   pathcrawl crawl --config configs/peers/dhl.yaml      --campaign peer-compare --non-interactive
   pathcrawl crawl --config configs/peers/flexport.yaml --campaign peer-compare --non-interactive
   pathcrawl crawl --config configs/peers/maersk.yaml   --campaign peer-compare --non-interactive
   ```

   Each run: robots.txt honoured (skips counted per host, including URLs
   with a query string), one request per second, waits and retries on 403 or
   429, and stops a host that keeps refusing (reported as blocked, not as a
   finding). A 5,000-page safety cap; `report.json` says whether the crawl
   was complete and how many URLs were still queued.
4. **Report each run**, which writes `report.md` and `report.json`. At the
   end of a crawl every linked win URL is checked without being loaded as a
   page; off-domain destinations are recorded, not fetched.
5. **Compare:**

   ```bash
   pathcrawl compare --sites configs/peers/compare.yaml --out compare \
     --run UPS=runs/ups/<campaign>/<ts> --run FedEx=runs/fedex/peer-compare/<ts> \
     --run DHL=runs/dhl/peer-compare/<ts> --run Flexport=runs/flexport/peer-compare/<ts> \
     --run Maersk=runs/maersk/peer-compare/<ts>
   ```

   UPS is re-sliced to the peers' kind of scope with the `slice` regexes in
   `compare.yaml`; check them against the UPS run's sections first.
   Distances are computed on each site's whole graph, and the slice only
   chooses which pages are counted.

**Win classes** (`win.classes`, regex):
- `talk_to_sales`, `quote_request` and `lead_onboarding` count as wins.
- `self_serve` (instant rates, open account, booking) and `to_verify` are
  reported separately and never counted as wins.
- No talk-to-sales page was found for Flexport or Maersk. `compare.yaml`
  states "none found" with the URLs checked.

**Outputs** in `compare/`:
- `distance_to_win.csv` and `depth_from_home.csv`: full distributions, all
  links and body links.
- `health.csv`: structured data, readable without JavaScript, unique title
  and description, exactly one H1 with text, self-canonical.
- `topic_matrix.csv`: pages, share of the site, and median clicks from home
  and to a win, per topic and site.
- `services.csv`: menu labels under services or solutions groups, plus
  Service and Product entities, as each site presents them.
- `summary.md`: the side-by-side table, win classes, robots.txt and site
  files, topic questions, and a "what this cannot tell us" paragraph.
- `summary.json`.

Depth figures from an incomplete crawl are labelled "incomplete, depth
understated". The comparison never reads lead tables or campaign tags.

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
- **`capture_params`** (optional): query params whose value is kept on each
  link before being stripped, e.g. `["WT.mc_id"]`. The value is stored in
  `links.mc_id` so lead counts can be joined to the link that carried the
  tag. For a run crawled before this existed, `pathcrawl backfill-links --run
  <run dir>` fills it from the raw hrefs in `crawl.db`, fetching nothing.
  It also merges pages stored under two URLs that now normalize the same,
  such as a page crawled once with `?msockid=...`. The tags on both copies'
  links are kept, and the old spelling becomes an alias.
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
- **`known_pages`** (optional): further win pages, each with a conversion
  `type`, e.g. `{url: ".../sbr-signup-ussp-page.html", type: "White papers &
  reports"}`. They are wins even if robots.txt blocks them or no crawled page
  links to them; unlinked ones are added as nodes. The report and graphs show
  each win's type.
- **`exclude_patterns`** (optional): globs for URLs that are never wins, such
  as an internal preview tool. These override `url_patterns` and
  `known_pages`.
- **`match`** (`exact` or `case_insensitive_path`): with
  `case_insensitive_path`, patterns, exclusions and known pages ignore path
  case, and known pages also ignore the query.
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
