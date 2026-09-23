# CrAIfish
A client-agnostic tool that measures how reachable a conversion goal is from a set of known marketing entry links.

It crawls a website starting from the links in a campaign's ad, builds a link
graph, and reports how many clicks it takes to reach the "win" page, where the
journey stalls, and which parts of the site can never reach the win at all.
The question it answers is not "what's broken?" but **"how reachable is our win?"**

Nothing in the code is client-specific: every client detail lives in a YAML
config file.

> **Status:** v1 in progress. Built so far: config validation, URL
> normalization, link-region extraction, and every path metric, proven
> against a fixture site (`tests/fixtures/site/README.md` lists its expected
> numbers). Coming next: the headed crawler and reports.

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
pathcrawl validate --config configs/ups.yaml
```

`validate` either prints a summary of the config or lists every problem with
its location in the file (for example `crawl.max_dept: Extra inputs are not
permitted`). It also warns about things that are valid but probably wrong, such
as leftover `REPLACE-ME` placeholders.

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
