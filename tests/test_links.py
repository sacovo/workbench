"""Product links (R-0162 in my-robot): the owner sends a page for an item, the agent records its
verdict, and a page found right becomes the item's source. The bom is the kernel's
(packages/kernel/tests/test_bom.py reads ledger/links.jsonl); here its answer is given."""

from __future__ import annotations

import json

import pytest

from test_jobs import run
from workbench import owner_cli
from workbench.errors import InvalidInput
from workbench.ops import bom_ops, default_registry
from workbench.review import ReviewStore

URL = "https://www.pololu.com/product/4758"


@pytest.fixture
def kernel(monkeypatch):
    def fake(project, *arguments):
        row = {"id": "lift-motor", "name": "Pololu 37D 10:1", "group": "Motors", "kind": "bought", "qty": 1.0, "have": 0.0,
               "recorded": False, "action": "to order", "short": 1.0, "notes": "with encoder"}
        return {"title": "Parts", "items": [row], "short": {"to order": 1}}

    monkeypatch.setattr(bom_ops, "kernel_json", fake)


def records(project):
    path = project.root / bom_ops.LINKS_FILE
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_link_opens_a_comment_for_the_agent_and_is_recorded_as_requested(project, kernel):
    result = run(project, "check-link", id="lift-motor", url=URL, note="the one with the encoder")
    (record,) = records(project)
    assert (record["id"], record["url"], record["state"], record["by"]["role"]) == ("lift-motor", URL, "requested", "owner")
    state = ReviewStore(project).state(result["review"]["id"])
    assert state["kind"] == "comment" and state["status"] == "waiting_agent"
    assert URL in state["body"] and "Pololu 37D 10:1" in state["body"] and "the one with the encoder" in state["body"]
    assert record["review"] == state["id"]


@pytest.mark.parametrize("url", ["", "www.pololu.com/product/4758", "ftp://example.com/x", "https://", "https://a b.com/x"])
def test_only_a_web_page_is_taken(project, kernel, url):
    with pytest.raises(InvalidInput, match="product page"):
        run(project, "check-link", id="lift-motor", url=url)
    assert records(project) == []


def test_an_unknown_item_is_named(project, kernel):
    with pytest.raises(InvalidInput, match="no item 'nonsense'"):
        run(project, "check-link", id="nonsense", url=URL)


def test_check_link_writes_as_the_owner():
    assert default_registry().get("check-link").owner_only
    assert not default_registry().get("link-verdict").owner_only


def test_a_verdict_needs_the_owners_request_and_is_appended(project, kernel):
    with pytest.raises(InvalidInput, match="has not asked"):
        run(project, "link-verdict", id="lift-motor", url=URL, verdict="ok", summary="right part")
    asked = run(project, "check-link", id="lift-motor", url=URL)
    run(project, "link-verdict", id="lift-motor", url=URL, verdict="ok", summary="70:1 is #4754; this is #4758, 10:1, with encoder", price=60.95, currency="USD", shop="Pololu")
    _, verdict = records(project)
    assert verdict["state"] == "ok" and verdict["price"] == 60.95 and verdict["shop"] == "Pololu" and verdict["by"]["role"] == "agent"
    assert verdict["review"] == asked["review"]["id"]
    with pytest.raises(InvalidInput, match="has not asked"):
        run(project, "link-verdict", id="lift-motor", url=URL + "?other", verdict="ok", summary="x")


@pytest.mark.parametrize("bad", [{"verdict": "fine"}, {"summary": ""}, {"summary": "two\nlines"}, {"price": -1}, {"price": float("nan")}])
def test_a_malformed_verdict_is_refused(project, kernel, bad):
    run(project, "check-link", id="lift-motor", url=URL)
    with pytest.raises(InvalidInput):
        run(project, "link-verdict", **{"id": "lift-motor", "url": URL, "verdict": "ok", "summary": "right part", **bad})
    assert [r["state"] for r in records(project)] == ["requested"]


def test_the_bom_shows_the_source_and_a_newer_link_in_question():
    source = {"url": URL, "state": "ok", "price": 60.95, "currency": "USD", "shop": "Pololu"}
    newer = {"url": "https://example.ch/37d", "state": "check", "summary": "no encoder"}
    row = {"id": "lift-motor", "name": "Pololu 37D 10:1", "group": "Motors", "qty": 1.0, "have": 0.0, "short": 1.0, "action": "to order",
           "source": source, "link": newer}
    text = bom_ops._human_bom({"items": [row], "short": {"to order": 1}})
    assert f"source: Pololu, 60.95 USD {URL}" in text
    assert "link check: https://example.ch/37d (no encoder)" in text


def test_the_owner_terminal_sends_a_link_found_by_part_of_the_name(project, kernel, monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(owner_cli, "owner_items", lambda project: [])
    replies = iter(["l", "zzz", "37d", URL, "", "q"])
    said = []
    assert owner_cli.run(str(project.root), lambda prompt: next(replies), said.append) == 0
    assert [r["state"] for r in records(project)] == ["requested"]
    assert any("0 items match" in line for line in said)
