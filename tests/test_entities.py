"""Entity layer: boilerplate stripping, tagging, coverage, bridge links, the
knowledge graph, the LLM proposal gate, and the CLI. No browser needed: the
run is built directly in a crawl database.

The site (entry link: a):

    a  "Why inventory matters"        --body--> b        --nav--> talk (win)
    b  "Retail returns"               --body--> talk
    c  "Manufacturing inventory ..."  --body--> talk
    d  "Store returns desk"           --nav---> talk     (no content path)
    e  "About us"                     --nav---> talk     (no content path)

Every page starts with the same menu and ends with the same cookie banner,
which mention Manufacturing, Retail and Healthcare; a, b and c carry the same
"Related stories on healthcare" heading. None of that may produce a tag.
"""

from __future__ import annotations

import csv
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import networkx as nx
import pytest
import yaml
from typer.testing import CliRunner

from pathcrawl.cli import app
from pathcrawl.config import parse_config
from pathcrawl.coverage import bridge_links, entity_coverage
from pathcrawl.entities import (
    Boilerplate,
    SourcePage,
    TaxonomyError,
    extract_run,
    find_taxonomy,
    load_taxonomy,
    parse_taxonomy,
    source_pages,
    strip_boilerplate,
)
from pathcrawl.propose import LLMError, OpenAICompatibleClient, accept, propose, write_proposals
from pathcrawl.report import write_report
from pathcrawl.run import open_run
from pathcrawl.store import LinkRecord, PageRecord, Store

TAXONOMY = Path(__file__).parent / "fixtures" / "entities" / "taxonomy.yaml"
B = "https://x.test/"
WIN = B + "talk-v4.html"
MENU = "Shipping Tracking Solutions Manufacturing Retail Healthcare Support Center Log in"
COOKIE = "We use cookies to improve your experience and show relevant retail offers on this site"
RELATED = "Related stories on healthcare"

PAGES = {
    "a": ("Why inventory matters", ["Inventory planning for manufacturers", RELATED],
          "Inventory planning for manufacturers Manufacturing teams cut inventory costs with Acme Motors. " + RELATED),
    "b": ("Retail returns", [RELATED], "Retailers fight return fraud. " + RELATED),
    "c": ("Manufacturing inventory checklist", [RELATED], "A checklist for manufacturing inventory. " + RELATED),
    "d": ("Store returns desk", [], "How retail stores process returns."),
    "e": ("About us", [], "Our company history and healthcare heritage."),
}
LINKS = {"a": [("b", "body"), ("talk-v4.html", "nav")], "b": [("talk-v4.html", "body")],
         "c": [("talk-v4.html", "body")], "d": [("talk-v4.html", "nav")], "e": [("talk-v4.html", "nav")]}


def build_run(run_dir: Path) -> Path:
    cfg = parse_config({
        "client": {"name": "X", "slug": "xco"},
        "scope": {"allowed_domains": ["x.test"]},
        "win": {"name": "consultation form", "url_patterns": [B + "talk*"]},
        "campaigns": [{"id": "c", "name": "C", "platform": "test", "ad_copy": "ad",
                       "entry_links": [{"label": "a", "url": B + "a"}]}],
    })
    (run_dir / "config.yaml").write_text(yaml.safe_dump(cfg.model_dump()))
    s = Store(run_dir / "crawl.db")
    s.set_meta(client="X", campaign_id="c", campaign_name="C", status="complete")
    s.add_entry(0, "a", B + "a", B + "a")
    for name, (title, headings, body) in PAGES.items():
        links = [LinkRecord(B + t, B + t, "", region, True) for t, region in LINKS[name]]
        s.save_page(PageRecord(url=B + name, status="ok", depth=0, title=title, headings=[(2, h) for h in headings],
                               body_text=f"{MENU} {body} {COOKIE}", jsonld_types=[]), links)
    s.save_page(PageRecord(url=WIN, status="not_fetched", win=True, win_source="pattern"), [])
    s.close()
    return run_dir


@pytest.fixture
def run_dir(tmp_path):
    return build_run(tmp_path)


def tags(store) -> dict[str, dict[str, tuple[float, str]]]:
    out: dict[str, dict[str, tuple[float, str]]] = {}
    for r in store.page_entities():
        out.setdefault(r["url"].removeprefix(B), {})[r["entity"]] = (r["score"], r["evidence"])
    return out


# --------------------------------------------------------------------------- taxonomy


def test_taxonomy_loads_and_validates(tmp_path):
    tax = load_taxonomy(TAXONOMY)
    assert len(tax.entities) == 6 and tax.boilerplate.window_min_pages == 3
    assert tax.types == ["Industry", "Segment", "Service", "Topic", "Customer"]
    assert tax.entities[0].pattern.search("Manufacturers agree")  # trailing * wildcard
    assert not tax.entities[3].pattern.search("inventorying")  # no wildcard: whole word only
    with pytest.raises(TaxonomyError, match="not one of"):
        parse_taxonomy({"entities": [{"type": "Planet", "name": "Mars", "terms": ["mars"]}]})
    with pytest.raises(TaxonomyError, match="listed twice"):
        parse_taxonomy({"entities": [{"type": "Topic", "name": "A", "terms": ["a"]},
                                     {"type": "Topic", "name": "a", "terms": ["b"]}]})
    with pytest.raises(TaxonomyError, match="terms"):
        parse_taxonomy({"entities": [{"type": "Topic", "name": "A", "terms": []}]})
    # defaults are the thresholds from the spec
    d = parse_taxonomy({})
    assert (d.boilerplate.window_words, d.boilerplate.window_min_pages, d.boilerplate.heading_min_pages) == (8, 30, 5)


def test_terms_match_whole_words_and_phrases():
    tax = parse_taxonomy({"entities": [
        {"type": "Topic", "name": "Returns", "terms": ["return fraud"]},
        {"type": "Topic", "name": "RFID", "terms": ["RFID"]},
    ]})
    ret, rfid = tax.entities
    assert ret.pattern.search("fight Return   Fraud now")
    assert not ret.pattern.search("return | fraud")  # never across a removed gap
    assert rfid.pattern.search("rfid tags") and not rfid.pattern.search("rfidx")


def test_find_taxonomy_prefers_explicit_then_live_then_run_copy(tmp_path):
    configs = tmp_path / "configs"
    configs.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    assert find_taxonomy(run, "xco", configs_dir=configs) is None
    (run / "entities.yaml").write_text("{}")
    assert find_taxonomy(run, "xco", configs_dir=configs) == run / "entities.yaml"
    (configs / "xco.entities.yaml").write_text("{}")
    assert find_taxonomy(run, "xco", configs_dir=configs) == configs / "xco.entities.yaml"
    assert find_taxonomy(run, "xco", TAXONOMY, configs_dir=configs) == TAXONOMY


# --------------------------------------------------------------------------- boilerplate and tagging


def test_boilerplate_is_stripped():
    pages = [SourcePage(B + n, t, h, f"{MENU} {body} {COOKIE}") for n, (t, h, body) in PAGES.items()]
    clean = {p.url.removeprefix(B): p for p in strip_boilerplate(pages, Boilerplate(window_min_pages=3, heading_min_pages=3))}
    for name, p in clean.items():
        assert "Shipping Tracking" not in p.body and "cookies" not in p.body, name
        assert RELATED not in p.body and RELATED not in p.headings
    assert clean["a"].dropped_headings == [RELATED]
    assert clean["a"].headings == ["Inventory planning for manufacturers"]
    assert "Manufacturing teams cut inventory costs" in clean["a"].body
    assert clean["e"].body == "Our company history and healthcare heritage."

    # below the thresholds nothing is removed
    kept = strip_boilerplate(pages, Boilerplate())  # 30 pages / 5 headings
    assert all("Shipping Tracking" in p.body for p in kept)


def test_pages_are_tagged_with_known_entities(run_dir):
    s = Store(run_dir / "crawl.db")
    summary = extract_run(s, load_taxonomy(TAXONOMY), TAXONOMY, run_dir)
    got = tags(s)
    assert got == {
        "a": {"Manufacturing": (3.0, "heading"), "Inventory": (6.0, "title"), "Acme Motors": (1.0, "body")},
        "b": {"Retail": (4.0, "title"), "Returns": (4.0, "title")},
        "c": {"Manufacturing": (4.0, "title"), "Inventory": (4.0, "title")},
        "d": {"Retail": (1.0, "body"), "Returns": (4.0, "title")},
        "e": {"Healthcare": (1.0, "body")},
    }
    # menu, cookie text and the repeated heading tagged nothing (no Healthcare on a-c, no Retail on a/c/e)
    assert summary.pages == 5 and summary.pages_tagged == 5 and summary.tags == 10
    assert summary.boilerplate_headings_dropped == 3 and summary.boilerplate_words_dropped > 0
    assert (run_dir / "entities.yaml").read_text() == TAXONOMY.read_text()
    assert s.meta("entities")["tags"] == 10
    # re-running replaces, never duplicates
    extract_run(s, load_taxonomy(TAXONOMY))
    assert len(s.page_entities()) == 10
    s.close()


def test_only_loaded_pages_are_tagged(run_dir):
    s = Store(run_dir / "crawl.db")
    assert sorted(p.url for p in source_pages(s)) == [B + n for n in "abcde"]
    s.close()


# --------------------------------------------------------------------------- coverage and bridges


def test_entity_coverage(run_dir):
    s = Store(run_dir / "crawl.db")
    extract_run(s, load_taxonomy(TAXONOMY))
    s.close()
    run = open_run(run_dir)
    cov = {c.entity: c for c in entity_coverage(run.graph, run.store.page_entities(), flag_min_pages=1)}
    run.close()
    assert [c for c in cov] == ["Manufacturing", "Retail", "Inventory", "Returns", "Acme Motors", "Healthcare"]
    m = cov["Manufacturing"]
    assert (m.pages_tagged, m.pages_linking_win_content, m.median_clicks_content, m.pct_within_2_content) == (2, 1, 1.5, 100.0)
    assert (m.pages_linking_win_all, m.median_clicks_all, m.pct_within_2_all, m.flag) == (2, 1, 100.0, "")
    r = cov["Retail"]
    assert (r.pages_tagged, r.pages_linking_win_content, r.median_clicks_content, r.pct_within_2_content) == (2, 1, 1, 50.0)
    assert (r.no_path_content, r.flag) == (1, "")  # half, not most
    a = cov["Acme Motors"]
    assert (a.pages_linking_win_content, a.median_clicks_content, a.pct_within_2_content) == (0, 2, 100.0)
    h = cov["Healthcare"]
    assert (h.pages_tagged, h.median_clicks_content, h.pct_within_2_content, h.no_path_content, h.flag) == (1, None, 0.0, 1, "no path")
    assert h.pct_within_2_all == 100.0  # nav links do reach it


def test_bridge_links(run_dir):
    s = Store(run_dir / "crawl.db")
    extract_run(s, load_taxonomy(TAXONOMY))
    s.close()
    run = open_run(run_dir)
    got = [(b.page.removeprefix(B), b.page_clicks_to_win_content, b.rank, b.bridge_page.removeprefix(B),
            b.shared_score, b.shared_entities) for b in bridge_links(run.graph, run.store.page_entities())]
    run.close()
    assert got == [
        # a is 2 clicks out; c links to the win and shares Inventory and Manufacturing: min(6,4) + min(3,4),
        # strongest shared entity first
        ("a", 2, 1, "c", 7.0, "Topic: Inventory; Industry: Manufacturing"),
        # d has no content path; b links to the win and shares Retail and Returns: min(1,4) + min(4,4)
        ("d", None, 1, "b", 5.0, "Topic: Returns; Industry: Retail"),
        # e (Healthcare) has no same-entity page that links to the win: no suggestion
    ]


# --------------------------------------------------------------------------- report and graph


def test_report_with_entities_via_cli(run_dir):
    result = CliRunner().invoke(app, ["report", "--run", str(run_dir), "--taxonomy", str(TAXONOMY)])
    assert result.exit_code == 0, result.output
    assert "5 of 5 pages tagged" in result.output

    md = (run_dir / "report.md").read_text()
    assert "## Entities: what the content is about" in md
    assert "| Healthcare | Industry | 1 | 0 | - | 0.0% | 1 | 100.0% | ⚠ no path |" in md
    assert "### Bridge links" in md and "| /a | 2 | /c | Topic: Inventory; Industry: Manufacturing | 7.0 |" in md
    assert "## Site-side recommendations" in md
    assert "0 of 5 pages (0%) have any structured data" in md
    assert md.index("## Site-side recommendations") < md.index("## Metric definitions")

    with open(run_dir / "entity_coverage.csv", newline="") as f:
        rows = {r["entity"]: r for r in csv.DictReader(f)}
    assert rows["Manufacturing"]["median_clicks_content"] == "1.5" and rows["Healthcare"]["median_clicks_content"] == ""
    with open(run_dir / "bridge_links.csv", newline="") as f:
        assert len(list(csv.DictReader(f))) == 2
    with open(run_dir / "page_entities.csv", newline="") as f:
        assert len(list(csv.DictReader(f))) == 10

    data = json.loads((run_dir / "report.json").read_text())
    assert [c["entity"] for c in data["entities"]["flagged"]] == ["Healthcare"]
    assert data["site_findings"]["conversion_urls"] == [WIN]

    kg = nx.read_gexf(run_dir / "xco_knowledge_graph.gexf")
    types = {n: d["node_type"] for n, d in kg.nodes(data=True)}
    assert sorted(n for n, t in types.items() if t == "page") == sorted([B + n for n in "abcde"] + [WIN])
    assert "entity:Industry:Manufacturing" in kg and types["entity:Industry:Manufacturing"] == "entity"
    assert kg.nodes["entity:Industry:Manufacturing"]["pages_tagged"] == 2
    assert kg.nodes["entity:Industry:Manufacturing"]["clicks_to_win"] == 2  # median of 2 and 1, rounded
    assert kg.nodes[B + "a"]["clicks_to_win"] == 2 and kg.nodes[B + "d"]["clicks_to_win"] == -1
    assert kg.edges[B + "a", "entity:Topic:Inventory"]["edge_type"] == "mention"
    assert kg.edges[B + "a", B + "b"]["edge_type"] == "link"
    assert not kg.has_edge(B + "d", WIN)  # nav-only link: not in the knowledge graph
    positions = {(d["viz"]["position"]["x"], d["viz"]["position"]["y"]) for _, d in kg.nodes(data=True)}
    assert len(positions) == len(kg)
    # the existing exports are still written
    for name in ("graph.graphml", "graph.gexf", "graph_content_only.gexf", "categories.csv"):
        assert (run_dir / name).exists()


def test_report_without_taxonomy_has_no_entity_section(tmp_path):
    run_dir = build_run(tmp_path)
    result = CliRunner().invoke(app, ["report", "--run", str(run_dir)])
    assert result.exit_code == 0, result.output
    md = (run_dir / "report.md").read_text()
    assert "## Entities" not in md and "## Site-side recommendations" in md
    assert not (run_dir / "xco_knowledge_graph.gexf").exists()


def test_near_miss_urls_join_the_consolidation_advice(run_dir):
    s = Store(run_dir / "crawl.db")
    # looks like the win ("talk" in the path) but misses the pattern https://x.test/talk*
    s.save_page(PageRecord(url=B + "e", status="ok", depth=0, title="About us", body_text="x", jsonld_types=[]),
                [LinkRecord("t", B + "promo/talk-2023.html", "", "body", True)])
    s.close()
    run = open_run(run_dir)
    write_report(run)
    run.close()
    md = (run_dir / "report.md").read_text()
    assert "Consolidate the 2 conversion URLs into one consultation form" in md


def test_entities_extract_cli_needs_a_taxonomy(run_dir):
    result = CliRunner().invoke(app, ["entities", "extract", "--run", str(run_dir)])
    assert result.exit_code == 2 and "no taxonomy found" in result.output
    result = CliRunner().invoke(app, ["entities", "extract", "--run", str(run_dir), "--taxonomy", str(TAXONOMY)])
    assert result.exit_code == 0, result.output


# --------------------------------------------------------------------------- LLM proposals


class FakeModel:
    model = "fake-hermes"

    def __init__(self):
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append(user)
        return "Sure! ```json\n" + json.dumps({"entities": [
            {"type": "Customer", "name": "Acme Motors", "terms": ["Acme Motors"]},    # already known
            {"type": "Service", "name": "Returns Portal", "terms": ["returns"], "quote": "process returns"},
            {"type": "Customer", "name": "Globex", "terms": ["Globex"]},              # not on any page
            {"type": "Planet", "name": "Mars", "terms": ["Mars"]},                    # not a type
        ]}) + "\n```"


def cleaned_pages(run_dir):
    s = Store(run_dir / "crawl.db")
    pages = strip_boilerplate(source_pages(s), load_taxonomy(TAXONOMY).boilerplate)
    s.close()
    return pages


def test_llm_proposals_are_checked_against_the_page(run_dir):
    model = FakeModel()
    proposals, summary = propose(cleaned_pages(run_dir), load_taxonomy(TAXONOMY), model)
    assert [(p.type, p.name, p.terms, sorted(u.removeprefix(B) for u in p.urls)) for p in proposals] == [
        ("Service", "Returns Portal", ["returns"], ["b", "d"]),
    ]
    assert summary.pages_sent == 5 and summary.proposed == 2
    assert (summary.rejected_known, summary.rejected_type, summary.rejected_not_on_page) == (5, 5, 8)
    # the model is told what exists, and never sees the boilerplate
    assert "Acme Motors" in model.prompts[0] and "cookies" not in model.prompts[0]


def test_proposals_enter_the_taxonomy_only_when_accepted(run_dir, tmp_path):
    taxonomy = tmp_path / "tax.yaml"
    shutil.copy(TAXONOMY, taxonomy)
    proposals, _ = propose(cleaned_pages(run_dir), load_taxonomy(taxonomy), FakeModel())
    csv_path = tmp_path / "entity_proposals.csv"
    write_proposals(proposals, csv_path, "fake-hermes")
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["decision"] == "" and rows[0]["pages"] == "2" and rows[0]["model"] == "fake-hermes"

    assert accept(csv_path, taxonomy) == []  # nothing reviewed yet
    assert len(load_taxonomy(taxonomy).entities) == 6

    rows[0]["decision"] = "accept"
    rows[0]["terms"] = "returns portal; returns"  # reviewers may edit
    rows.append({**rows[0], "decision": "", "name": "Not Reviewed"})
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    result = CliRunner().invoke(app, ["entities", "accept", "--proposals", str(csv_path), "--taxonomy", str(taxonomy)])
    assert result.exit_code == 0, result.output
    assert "Service: Returns Portal" in result.output
    tax = load_taxonomy(taxonomy)
    assert [(e.type, e.name, e.terms) for e in tax.entities[6:]] == [("Service", "Returns Portal", ["returns portal", "returns"])]
    assert taxonomy.read_text().startswith("# Test taxonomy")  # comments kept
    assert accept(csv_path, taxonomy) == []  # accepting twice adds nothing


def test_accept_refuses_a_file_it_cannot_append_to(tmp_path):
    taxonomy = tmp_path / "tax.yaml"
    taxonomy.write_text("entities:\n  - {type: Topic, name: A, terms: [a]}\nscoring:\n  min_score: 1\n")
    csv_path = tmp_path / "p.csv"
    write_proposals([], csv_path, "m")
    with pytest.raises(TaxonomyError, match="last section"):
        accept(csv_path, taxonomy)


def test_openai_compatible_client(tmp_path):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = json.dumps({"choices": [{"message": {"content": '{"entities": []}'}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = OpenAICompatibleClient(f"http://127.0.0.1:{server.server_address[1]}/v1/", "hermes3", 5)
        assert client.complete("sys", "user") == '{"entities": []}'
        path, body = seen[0]
        assert path == "/v1/chat/completions" and body["model"] == "hermes3" and body["temperature"] == 0
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
    finally:
        server.shutdown()
        server.server_close()
    with pytest.raises(LLMError, match="cannot reach"):
        OpenAICompatibleClient("http://127.0.0.1:9/v1", "m", 2).complete("s", "u")


def test_shipped_taxonomies_are_valid():
    for path in (Path(__file__).parent.parent / "configs").glob("*.entities.yaml"):
        result = CliRunner().invoke(app, ["entities", "check", "--taxonomy", str(path)])
        assert result.exit_code == 0 and "is valid" in result.output, result.output
