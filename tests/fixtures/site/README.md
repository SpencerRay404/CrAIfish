# Fixture site

A tiny static site with a known link graph. `tests/test_graph.py` checks every
path metric against the numbers below, which were worked out by hand.

Every page except `orphan.html` has the same chrome:

- `<nav>` links to `win.html` ("Contact sales"), so every page is one click
  from the win when all links count.
- `<footer>` links to `about.html`.

Body (content) links:

```
entry-near ──> win
entry-near ──> orphan                   (orphan has no links at all)
entry-far  ──> article-1 ──> article-2 ──> win
                  └──────> trap-a ──> trap-b ──> trap-c ──┐
                             ^─────────────────────────────┘
```

`win.html` is the win page and contains `form#contact-sales`. `about.html` has
only chrome links, so it can be reached only through the footer.
Self-links (the win page's own nav link, about's own footer link) are dropped.

## Expected results

Entry links: `entry-near.html` and `entry-far.html`. Default `max_depth` = 8.
9 pages are crawled. `win.html` is recorded as the win but never loaded, since
the journey ends there, so its links (including its footer link) don't count.

| metric | all links | content only |
|---|---|---|
| reachable pages | 10 | 9 (no `about`) |
| shortest, entry-near | 1: near → win | 1: near → win |
| shortest, entry-far | 1: far → win | 3: far → article-1 → article-2 → win |
| longest simple, entry-near | 2: near → about → win | 1 |
| longest simple, entry-far | 6: far → article-1 → trap-a → trap-b → trap-c → about → win | 3 |
| longest simple, entry-far, `max_depth` 4 | 4: far → article-1 → article-2 → about → win | 3 |
| worst-case distance | 1 (8 pages) | 3 (entry-far) |
| distribution {clicks: pages} | {0: 1, 1: 8} | {0: 1, 1: 2, 2: 1, 3: 1} |
| dead ends | orphan (1 of 9, 11.1%) | orphan, trap-a, trap-b, trap-c (4 of 9, 44.4%) |
| trap loops | none | [trap-a, trap-b, trap-c] |
| dead zone hit, entry-near | click 1 (orphan) | click 1 (orphan) |
| dead zone hit, entry-far | never | click 2 (trap-a) |
| convergence | both reach win.html | both reach win.html |
| path overlap (Jaccard) | 1/3 = 0.333 | 1/5 = 0.2 |
