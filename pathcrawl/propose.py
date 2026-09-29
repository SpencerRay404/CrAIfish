"""Optional LLM pass: a local model proposes entities the taxonomy is missing.

Nothing it proposes is used until a person reviews it:

1. ``pathcrawl entities propose --run DIR`` sends each page's text (boilerplate
   removed) to a local model behind an OpenAI-compatible endpoint (Ollama, LM
   Studio, llama.cpp server; a Hermes model by default). The model is asked
   for industries, segments, services, topics and customer names that aren't
   in the taxonomy yet.
2. Every proposal is checked against the page it came from: its type must be
   one of the taxonomy's types, and at least one of its terms must actually
   appear in the page text. Anything else is dropped as a likely invention.
3. Surviving proposals are merged across pages and written to
   ``entity_proposals.csv`` with an empty ``decision`` column.
4. A reviewer sets ``decision`` to ``accept`` on the rows to keep (and may edit
   the name, type or terms), then runs ``pathcrawl entities accept``, which
   appends only those rows to the taxonomy file.
"""

from __future__ import annotations

import csv
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

import yaml

from pathcrawl.entities import CleanPage, Taxonomy, TaxonomyError, compile_terms, load_taxonomy

ACCEPT_WORDS = {"accept", "accepted", "yes", "y"}
PROPOSAL_FIELDS = ["decision", "type", "name", "terms", "pages", "example_urls", "example_quote", "model"]

TYPE_HINTS = {
    "Industry": "an industry or vertical the company serves (e.g. healthcare, automotive)",
    "Segment": "a customer segment or company size (e.g. small business, enterprise)",
    "Service": "a product or service the company offers",
    "Topic": "a subject the content is about (e.g. returns, inventory, sustainability)",
    "Customer": "a named customer or partner organization featured in the content",
}


class LLMError(Exception):
    pass


class ChatClient(Protocol):
    model: str

    def complete(self, system: str, user: str) -> str: ...


class OpenAICompatibleClient:
    """POST {base_url}/chat/completions. An API key is sent only if
    PATHCRAWL_LLM_API_KEY is set (local servers don't need one)."""

    def __init__(self, base_url: str, model: str, timeout_s: float = 180):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.timeout_s = timeout_s

    def complete(self, system: str, user: str) -> str:
        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }).encode()
        headers = {"Content-Type": "application/json"}
        if os.environ.get("PATHCRAWL_LLM_API_KEY"):
            headers["Authorization"] = f"Bearer {os.environ['PATHCRAWL_LLM_API_KEY']}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError(f"cannot reach the model at {self.url}: {e}") from None
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"unexpected response from {self.url}: {str(data)[:200]}") from None


def build_prompt(page: CleanPage, taxonomy: Taxonomy, max_chars: int) -> tuple[str, str]:
    system = (
        "You tag web pages for a knowledge graph. Reply with JSON only, no prose. "
        "Only propose entities the page text explicitly names; never guess."
    )
    known = {}
    for e in taxonomy.entities:
        known.setdefault(e.type, []).append(e.name)
    types = "\n".join(f"- {t}: {TYPE_HINTS.get(t, t)}" for t in taxonomy.types)
    already = "\n".join(f"- {t}: {', '.join(sorted(v))}" for t, v in sorted(known.items())) or "- (none yet)"
    text = page.body[:max_chars]
    user = (
        f"Entity types:\n{types}\n\nAlready in the taxonomy (do not propose these):\n{already}\n\n"
        "Propose NEW entities this page is about. For each give its type, a short canonical name, the exact "
        "words or phrases on the page that signal it (terms), and a short exact quote from the page.\n"
        'Reply as: {"entities": [{"type": "...", "name": "...", "terms": ["..."], "quote": "..."}]}\n'
        'Reply {"entities": []} if there are none.\n\n'
        f"Page title: {page.title}\nHeadings: {' | '.join(page.headings[:20])}\n\nPage text:\n{text}"
    )
    return system, user


def parse_reply(reply: str) -> list[dict]:
    """The entities list from a model reply, tolerating code fences and prose around the JSON."""
    m = re.search(r"\{.*\}", reply, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    items = data.get("entities") if isinstance(data, dict) else None
    return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []


@dataclass
class Proposal:
    type: str
    name: str
    terms: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    quote: str = ""


@dataclass
class ProposalSummary:
    pages_sent: int = 0
    proposed: int = 0
    rejected_type: int = 0
    rejected_known: int = 0
    rejected_not_on_page: int = 0
    errors: list[str] = field(default_factory=list)


def propose(pages: list[CleanPage], taxonomy: Taxonomy, client: ChatClient,
            max_chars: int | None = None, progress=None) -> tuple[list[Proposal], ProposalSummary]:
    max_chars = max_chars or taxonomy.llm.max_chars
    merged: dict[tuple[str, str], Proposal] = {}
    summary = ProposalSummary()
    for page in pages:
        if not page.body.strip():
            continue
        summary.pages_sent += 1
        system, user = build_prompt(page, taxonomy, max_chars)
        try:
            items = parse_reply(client.complete(system, user))
        except LLMError as e:
            summary.errors.append(f"{page.url}: {e}")
            if len(summary.errors) >= 3 and summary.pages_sent == len(summary.errors):
                break  # the model is not reachable at all; stop early
            continue
        page_text = " | ".join([page.title, *page.headings, page.body[:max_chars]])
        for item in items:
            etype, name = str(item.get("type", "")).strip(), str(item.get("name", "")).strip()
            terms = [str(t).strip() for t in item.get("terms") or [] if str(t).strip()] or ([name] if name else [])
            if etype not in taxonomy.types or not name:
                summary.rejected_type += 1
                continue
            if taxonomy.known(etype, name):
                summary.rejected_known += 1
                continue
            present = [t for t in terms if compile_terms([t]).search(page_text)]
            if not present:
                summary.rejected_not_on_page += 1
                continue
            p = merged.setdefault((etype, name.lower()), Proposal(etype, name))
            p.terms = sorted(set(p.terms) | set(present), key=str.lower)
            if page.url not in p.urls:
                p.urls.append(page.url)
            if not p.quote and item.get("quote"):
                p.quote = str(item["quote"])[:200]
            summary.proposed += 1
        if progress:
            progress(page.url, len(items))
    return sorted(merged.values(), key=lambda p: (-len(p.urls), p.type, p.name.lower())), summary


def write_proposals(proposals: list[Proposal], path: Path, model: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PROPOSAL_FIELDS)
        w.writeheader()
        for p in proposals:
            w.writerow({"decision": "", "type": p.type, "name": p.name, "terms": "; ".join(p.terms),
                        "pages": len(p.urls), "example_urls": " ".join(p.urls[:3]),
                        "example_quote": p.quote, "model": model})


def accept(proposals_csv: Path, taxonomy_path: Path) -> list[str]:
    """Append the rows marked accepted to the taxonomy file. Returns the names added.

    The file is appended to, not rewritten, so its comments and layout are kept;
    ``entities`` must therefore be the last section of the file. The result is
    validated, and the file is restored if it doesn't load.
    """
    original = taxonomy_path.read_text(encoding="utf-8")
    data = yaml.safe_load(original) or {}
    if not isinstance(data, dict) or "entities" not in data or list(data)[-1] != "entities":
        raise TaxonomyError(f"{taxonomy_path}: 'entities:' must be the last section of the file to append to it")
    taxonomy = load_taxonomy(taxonomy_path)

    with open(proposals_csv, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("decision") or "").strip().lower() in ACCEPT_WORDS]
    if re.search(r"^entities:\s*\[\s*\]\s*$", original, re.MULTILINE):
        raise TaxonomyError(f"{taxonomy_path}: write 'entities:' as a block list (not []) to append to it")
    tail = original[re.search(r"^entities:", original, re.MULTILINE).end():]
    first_item = re.search(r"^( *)- ", tail, re.MULTILINE)
    pad = first_item.group(1) if first_item else "  "
    added, blocks = [], []
    for r in rows:
        etype, name = r["type"].strip(), r["name"].strip()
        terms = [t.strip() for t in r["terms"].split(";") if t.strip()] or [name]
        if taxonomy.known(etype, name) or (etype, name.lower()) in {(t, n.lower()) for t, n in added}:
            continue
        blocks.append(f"{pad}- type: {json.dumps(etype)}\n{pad}  name: {json.dumps(name)}\n"
                      f"{pad}  terms: {json.dumps(terms, ensure_ascii=False)}\n")
        added.append((etype, name))
    if not blocks:
        return []

    header = f"{pad}# accepted from {proposals_csv.name} on {date.today().isoformat()}\n"
    text = original if original.endswith("\n") else original + "\n"
    taxonomy_path.write_text(text + header + "".join(blocks), encoding="utf-8")
    try:
        load_taxonomy(taxonomy_path)
    except TaxonomyError:
        taxonomy_path.write_text(original, encoding="utf-8")
        raise
    return [f"{t}: {n}" for t, n in added]
