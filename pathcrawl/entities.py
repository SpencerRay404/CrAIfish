"""Entity layer: what each page is about, as typed entities from a taxonomy.

The taxonomy (industries, segments, services, topics, customers and the words
that signal them) lives in a YAML file next to the client config, e.g.
``configs/ups.entities.yaml``, so it can be edited without a code change.
Extraction is deterministic dictionary matching:

1. **Strip boilerplate.** Menus, cookie text and "related stories" modules
   repeat across pages and would tag every page with everything. Any run of
   ``window_words`` words (8) that appears on ``window_min_pages`` (30) or more
   pages is dropped from the body text, and so is any heading that appears on
   ``heading_min_pages`` (5) or more pages.
2. **Match terms** in the title, the remaining headings and the remaining body.
   ``score = title_weight * title hits + heading_weight * heading hits +
   body_weight * min(body hits, body_cap)``. A page is tagged with an entity
   when the score reaches ``min_score``. ``evidence`` is where it was found
   first, in the order title, heading, body.

Results go to the ``page_entities`` table of the run's crawl.db.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ENTITY_TYPES = ("Industry", "Segment", "Service", "Topic", "Customer")
EVIDENCE_ORDER = ("title", "heading", "body")
REGEX_PREFIX = "re:"
SEPARATOR = " | "  # put where text was removed, so a phrase can never match across the gap


class TaxonomyError(Exception):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityDef(_Strict):
    type: str
    name: str = Field(min_length=1)
    # Words or phrases that signal the entity, case-insensitive, matched as
    # whole words. A trailing * matches any word ending ("manufactur*").
    # "re:" starts a Python regex.
    terms: list[str] = Field(min_length=1)

    @field_validator("terms")
    @classmethod
    def _check_terms(cls, terms: list[str]) -> list[str]:
        for t in terms:
            if not t.strip():
                raise ValueError("empty term")
            if t.startswith(REGEX_PREFIX):
                try:
                    re.compile(t[len(REGEX_PREFIX):])
                except re.error as e:
                    raise ValueError(f"invalid regex in {t!r}: {e}") from None
        return terms

    @cached_property
    def pattern(self) -> re.Pattern:
        return compile_terms(self.terms)


class Boilerplate(_Strict):
    window_words: int = Field(8, ge=2)
    window_min_pages: int = Field(30, ge=2)
    heading_min_pages: int = Field(5, ge=2)


class Scoring(_Strict):
    title_weight: float = Field(3.0, ge=0)
    heading_weight: float = Field(2.0, ge=0)
    body_weight: float = Field(1.0, ge=0)
    body_cap: int = Field(5, ge=1)  # body mentions past this many add nothing
    min_score: float = Field(1.0, gt=0)


class Coverage(_Strict):
    flag_min_pages: int = Field(10, ge=1)  # "many pages" when flagging entities with no path to the win
    bridges_per_page: int = Field(3, ge=1)


class LLM(_Strict):
    """A local model behind an OpenAI-compatible chat endpoint (Ollama, LM
    Studio, llama.cpp server, vLLM). Only used by ``pathcrawl entities propose``."""

    base_url: str = "http://localhost:11434/v1"
    model: str = "hermes3"
    max_chars: int = Field(6000, ge=500)  # page text sent per request
    timeout_s: float = Field(180, gt=0)


class Taxonomy(_Strict):
    types: list[str] = Field(default_factory=lambda: list(ENTITY_TYPES))
    boilerplate: Boilerplate = Boilerplate()
    scoring: Scoring = Scoring()
    coverage: Coverage = Coverage()
    llm: LLM = LLM()
    entities: list[EntityDef] = []

    @model_validator(mode="after")
    def _check(self) -> Taxonomy:
        problems, seen = [], set()
        for i, e in enumerate(self.entities):
            if e.type not in self.types:
                problems.append(f"entities[{i}] ({e.name}): type {e.type!r} is not one of {', '.join(self.types)}")
            key = (e.type, e.name.lower())
            if key in seen:
                problems.append(f"entities[{i}]: {e.type} {e.name!r} is listed twice")
            seen.add(key)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def known(self, entity_type: str, name: str) -> bool:
        return any(e.type == entity_type and e.name.lower() == name.lower() for e in self.entities)


def parse_taxonomy(data: object) -> Taxonomy:
    if not isinstance(data, dict):
        raise TaxonomyError("the taxonomy must be a YAML mapping")
    try:
        return Taxonomy.model_validate(data)
    except ValidationError as e:
        lines = [f"{'.'.join(str(p) for p in err['loc']) or '(top)'}: {err['msg']}" for err in e.errors()]
        raise TaxonomyError("invalid taxonomy:\n  " + "\n  ".join(lines)) from None


def load_taxonomy(path: str | Path) -> Taxonomy:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise TaxonomyError(f"cannot read taxonomy {path}: {e}") from None
    except yaml.YAMLError as e:
        raise TaxonomyError(f"{path} is not valid YAML: {e}") from None
    try:
        return parse_taxonomy(data)
    except TaxonomyError as e:
        raise TaxonomyError(f"{path}: {e}") from None


def find_taxonomy(run_dir: Path, client_slug: str, explicit: Path | None = None,
                  configs_dir: Path = Path("configs")) -> Path | None:
    """The taxonomy to use for a run: the one given, else the client's live
    file ``configs/<slug>.entities.yaml`` (so edits apply on the next report),
    else the copy saved in the run directory by the last extraction."""
    for p in (explicit, configs_dir / f"{client_slug}.entities.yaml", run_dir / "entities.yaml"):
        if p is not None and Path(p).exists():
            return Path(p)
    return None


def compile_terms(terms: list[str]) -> re.Pattern:
    parts = []
    for t in terms:
        if t.startswith(REGEX_PREFIX):
            parts.append(f"(?:{t[len(REGEX_PREFIX):]})")
            continue
        wildcard = t.endswith("*")
        words = t.rstrip("*").split()
        body = r"\s+".join(re.escape(w) for w in words)
        parts.append(r"(?<!\w)" + body + (r"\w*" if wildcard else r"(?!\w)"))
    return re.compile("|".join(parts), re.IGNORECASE)


# --------------------------------------------------------------------------- boilerplate


def _norm_heading(text: str) -> str:
    return " ".join(text.lower().split())


def _token_key(token: str) -> str:
    return re.sub(r"^\W+|\W+$", "", token.lower())


@dataclass
class SourcePage:
    url: str
    title: str
    headings: list[str]
    body: str


@dataclass
class CleanPage:
    url: str
    title: str
    headings: list[str]
    body: str
    dropped_headings: list[str] = field(default_factory=list)
    dropped_words: int = 0


def strip_boilerplate(pages: list[SourcePage], cfg: Boilerplate) -> list[CleanPage]:
    """Remove text that repeats across the site (see the module docstring)."""
    heading_df = Counter()
    for p in pages:
        heading_df.update({_norm_heading(h) for h in p.headings if h.strip()})
    common_headings = {h for h, n in heading_df.items() if n >= cfg.heading_min_pages}

    # Headings are scored as headings, so they are taken out of the body text.
    bodies = []
    for p in pages:
        body = p.body or ""
        for h in sorted({h.strip() for h in p.headings if h.strip()}, key=len, reverse=True):
            body = body.replace(h, SEPARATOR)
        bodies.append(body.split())

    n = cfg.window_words
    window_df = Counter()
    keyed = []
    for tokens in bodies:
        keys = [_token_key(t) for t in tokens]
        keyed.append(keys)
        window_df.update({hash(tuple(keys[i:i + n])) for i in range(len(keys) - n + 1)})
    common = {h for h, c in window_df.items() if c >= cfg.window_min_pages}

    out = []
    for p, tokens, keys in zip(pages, bodies, keyed):
        drop = [False] * len(tokens)
        if common:
            for i in range(len(keys) - n + 1):
                if hash(tuple(keys[i:i + n])) in common:
                    drop[i:i + n] = [True] * n
        kept, gap = [], False
        for tok, dropped in zip(tokens, drop):
            if dropped:
                gap = True
                continue
            if gap and kept:
                kept.append(SEPARATOR.strip())
            gap = False
            kept.append(tok)
        out.append(CleanPage(
            url=p.url,
            title=p.title or "",
            headings=[h for h in p.headings if h.strip() and _norm_heading(h) not in common_headings],
            body=" ".join(kept),
            dropped_headings=[h for h in p.headings if _norm_heading(h) in common_headings],
            dropped_words=sum(drop),
        ))
    return out


# --------------------------------------------------------------------------- matching


@dataclass(frozen=True)
class PageEntity:
    url: str
    entity_type: str
    entity: str
    score: float
    evidence: str  # title, heading or body


def extract(page: CleanPage, taxonomy: Taxonomy) -> list[PageEntity]:
    s = taxonomy.scoring
    headings = SEPARATOR.join(page.headings)
    out = []
    for e in taxonomy.entities:
        hits = {
            "title": len(e.pattern.findall(page.title)),
            "heading": len(e.pattern.findall(headings)),
            "body": len(e.pattern.findall(page.body)),
        }
        score = (s.title_weight * hits["title"] + s.heading_weight * hits["heading"]
                 + s.body_weight * min(hits["body"], s.body_cap))
        if score >= s.min_score:
            evidence = next(k for k in EVIDENCE_ORDER if hits[k])
            out.append(PageEntity(page.url, e.type, e.name, round(score, 2), evidence))
    return out


def source_pages(store) -> list[SourcePage]:
    """Every successfully loaded page, with its title, headings and body text."""
    import json

    pages = []
    for row in store.pages():
        if row["status"] != "ok":
            continue
        headings = [text for _, text in json.loads(row["headings"])] if row["headings"] else []
        pages.append(SourcePage(row["url"], row["title"] or "", headings, row["body_text"] or ""))
    return pages


@dataclass
class ExtractionSummary:
    pages: int
    pages_tagged: int
    tags: int
    entities_used: int
    boilerplate_words_dropped: int
    boilerplate_headings_dropped: int


def extract_run(store, taxonomy: Taxonomy, taxonomy_path: Path | None = None,
                run_dir: Path | None = None) -> ExtractionSummary:
    """Strip boilerplate, tag every loaded page, and replace the run's
    ``page_entities`` rows. A copy of the taxonomy is saved in the run directory."""
    from pathcrawl.store import now

    clean = strip_boilerplate(source_pages(store), taxonomy.boilerplate)
    rows = [pe for page in clean for pe in extract(page, taxonomy)]
    store.replace_page_entities(rows)
    summary = ExtractionSummary(
        pages=len(clean),
        pages_tagged=len({r.url for r in rows}),
        tags=len(rows),
        entities_used=len({(r.entity_type, r.entity) for r in rows}),
        boilerplate_words_dropped=sum(p.dropped_words for p in clean),
        boilerplate_headings_dropped=sum(len(p.dropped_headings) for p in clean),
    )
    store.set_meta(entities={
        "taxonomy": str(taxonomy_path) if taxonomy_path else None,
        "extracted_at": now(),
        **summary.__dict__,
    })
    if run_dir is not None and taxonomy_path is not None and Path(taxonomy_path).resolve() != (run_dir / "entities.yaml").resolve():
        (run_dir / "entities.yaml").write_text(Path(taxonomy_path).read_text(encoding="utf-8"), encoding="utf-8")
    return summary
