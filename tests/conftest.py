"""Browser tests need Chromium.

Locally they are skipped with a clear reason if Chromium can't start. CI sets
PATHCRAWL_REQUIRE_BROWSER=1 so a missing browser fails instead of skipping.
"""

import functools
import os

import pytest


@functools.cache
def _browser_problem() -> str | None:
    try:
        from playwright.sync_api import sync_playwright

        from pathcrawl.crawler import chromium_executable

        with sync_playwright() as p:
            p.chromium.launch(executable_path=chromium_executable()).close()
        return None
    except Exception as e:
        return str(e).splitlines()[0]


def pytest_collection_modifyitems(config, items):
    browser_items = [i for i in items if i.get_closest_marker("browser")]
    if not browser_items:
        return
    problem = _browser_problem()
    if problem is None:
        return
    if os.environ.get("PATHCRAWL_REQUIRE_BROWSER"):
        raise pytest.UsageError(f"Chromium is required but could not start: {problem}")
    skip = pytest.mark.skip(reason=f"Chromium unavailable ({problem}); run: playwright install chromium")
    for item in browser_items:
        item.add_marker(skip)
