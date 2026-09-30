"""LLM scoring: out of scope for v1. This module fixes the interface only.

Two future scorers plug in here:

- ``JourneyScorer``: rates how friendly a journey is, from the screenshots of
  the pages along a path and a rubric (clarity of the next step, visual
  prominence of the call to action, distractions).
- ``RelevanceScorer``: rates how well each page on a path follows through on
  the ad's promise, comparing the campaign's ``ad_copy`` with the page text
  the crawler already stores in ``pages.body_text``.

Both take data the crawl already collected, so they can run on any past run
without re-crawling. Scores are 0-1 with a short written rationale, so the
report can show them next to the deterministic path metrics without mixing
the two: path math stays in code, judgment calls stay here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class JourneyStep:
    url: str
    title: str | None
    text: str  # visible page text (pages.body_text)
    screenshot_path: str | None


@dataclass(frozen=True)
class Score:
    value: float  # 0.0 (worst) to 1.0 (best)
    rationale: str


class JourneyScorer(Protocol):
    def score_journey(self, steps: list[JourneyStep], rubric: str) -> Score: ...


class RelevanceScorer(Protocol):
    def score_relevance(self, ad_copy: str, step: JourneyStep) -> Score: ...


class NotConfiguredScorer:
    """The v1 stub: every call explains that scoring isn't built yet."""

    def score_journey(self, steps: list[JourneyStep], rubric: str) -> Score:
        raise NotImplementedError("Journey scoring is planned for v2; see pathcrawl/scoring/__init__.py")

    def score_relevance(self, ad_copy: str, step: JourneyStep) -> Score:
        raise NotImplementedError("Relevance scoring is planned for v2; see pathcrawl/scoring/__init__.py")
