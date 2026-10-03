# Data the site explorer can read

Everything below is written by `pathcrawl report` into the run folder. The
explorer should read it rather than recompute it. Node attributes appear in
`graph.gexf`, `graph_content_only.gexf` and `report.json` (`nodes`); edge
attributes appear in the GEXF files and `report.json` (`edges`).

## Explorer backlog: where each item's data comes from

| backlog item | status in the crawler | where |
|---|---|---|
| LinkedIn entry points from outside UPS.com | ready | external post nodes: role `external`, attribute `external=true`; `report.json` `external_seeds` lists each post, its landing pages and the tags on those links. Run `pathcrawl external-seeds` for an existing run. |
| Audience archetype view | ready | node `archetypes` (comma-separated); `report.json` `archetypes` (pages, pages linking to a win page, whole leads); report section "Audience archetypes". The rule is `leads.archetype_pattern` in `configs/ups.yaml`. |
| Win list additions (SBR signup, USSP-2605 whitepaper) | ready | `win.known_pages` in `configs/ups.yaml`; node `win_type`; `report.json` `win_types`. |
| Page-view weights | blocked | waiting on page-view data |
| Removing the five LinkedIn entry highlights | explorer-side | the five configured entry links are still entry nodes (role `entry`); hide them in the explorer if not wanted. |

## Node attributes

| attribute | meaning |
|---|---|
| `role` | entry, win, crawled, uncrawled, external |
| `win_type` | the conversion type of a win page (e.g. Virtual consultation) |
| `clicks_to_any_win`, `clicks_to_<type>` | clicks over all links to the nearest win, overall and per win type; -1 = no path |
| `clicks_from_home_all_links`, `clicks_from_home_body_links` | click depth from `health.home_url`; -1 = not reachable |
| `section`, `page_type` | the grouping the report uses (also in `crawl.db` `pages.section`, `pages.page_type`) |
| `leads_origin`, `leads_exact` | whole leads allocated to the page (rounded down), and the part from tags only this page carries |
| `leads_share` | the same before rounding, for audit |
| `carries_lead_tag` | the page has an allocated share, even if `leads_origin` is 0 |
| `leads_landed` | whole leads whose main conversion page is this page |
| `archetypes` | audience archetypes of the tags the page's links carry |
| `is_dead`, `dead_reason`, `inbound_dead_links`, `dead_inbound_pages`, `dead_inbound_body_links` | dead pages, and links into them from live pages only |
| `has_structured_data`, `js_dependent` | health signals |

## Edge attributes

| attribute | meaning |
|---|---|
| `region` | body, nav, header, footer, external (several are comma-separated) |
| `content_link` | the link sits in the body |
| `leads` | whole leads on this page-to-target link (rounded down) |
| `to_dead` | the target is a dead page |

## CSV files

`<client>_site_health.csv` (one row per page, including `h1_count` and
`h1_empty_count`, so `h1_check.csv` is no longer needed),
`<client>_lead_attribution.csv` (with `share` and `leads_whole`),
`<client>_zero_lead_tags.csv`, `<client>_dead_pages.csv`, `categories.csv`.

## Sharing builds

The no-leads build for sharing outside UPS should drop: the `leads_*`,
`carries_lead_tag` and `archetypes` node attributes, the edge `leads`, and
`report.json` `leads` and `archetypes`. It should also leave out the lead
CSVs and the report sections "Lead evidence" and "Audience archetypes".
