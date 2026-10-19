"""Documents and cart marks (R-0260 in my-robot): files copied into documents/<kind>s/ and recorded
with their bom items; cart marks per item and shop. Reading them back into the bom is the kernel's
(packages/kernel/tests/test_bom.py); here the bom's item list is given."""

from __future__ import annotations

import json

import pytest

from test_hub import running  # noqa: F401 - fixture
from test_jobs import run
from workbench.errors import InvalidInput
from workbench.ops import default_registry, doc_ops


@pytest.fixture
def kernel(monkeypatch):
    def fake(project, *arguments):
        rows = [{"id": i, "name": i, "group": "Parts", "kind": "bought", "qty": 1.0, "have": 0.0, "short": 1.0, "action": "to order"}
                for i in ("servo", "motor")]
        return {"title": "Parts", "items": rows, "short": {"to order": 2}, "documents": []}

    monkeypatch.setattr(doc_ops, "kernel_json", fake)


def records(project, name=doc_ops.DOCUMENTS_FILE):
    path = project.root / name
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_document_is_copied_under_its_kind_and_recorded_with_its_items(project, kernel, tmp_path):
    source = tmp_path / "STS3215 data sheet?.pdf"
    source.write_bytes(b"%PDF-1.7 servo")
    result = run(project, "doc-add", source=str(source), kind="datasheet", items=["servo", "servo"], title="Feetech sheet")
    rec = result["record"]
    assert rec["path"] == "documents/datasheets/STS3215 data sheet_.pdf"
    assert (project.root / rec["path"]).read_bytes() == b"%PDF-1.7 servo"
    assert (rec["items"], rec["kind"], rec["title"], rec["size"], rec["by"]["role"]) == (["servo"], "datasheet", "Feetech sheet", 14, "agent")
    assert records(project) == [rec]


def test_same_content_is_recorded_again_and_other_content_gets_a_new_name(project, kernel, tmp_path):
    a = tmp_path / "sheet.pdf"
    a.write_bytes(b"one")
    first = run(project, "doc-add", source=str(a), kind="datasheet", items=["servo"])["record"]
    again = run(project, "doc-add", source=str(a), kind="datasheet", items=["motor"])["record"]
    a.write_bytes(b"two")
    other = run(project, "doc-add", source=str(a), kind="datasheet")["record"]
    assert first["path"] == again["path"] == "documents/datasheets/sheet.pdf" and again["items"] == ["motor"]
    assert other["path"] == "documents/datasheets/sheet-2.pdf"
    assert (project.root / first["path"]).read_bytes() == b"one" and (project.root / other["path"]).read_bytes() == b"two"


@pytest.mark.parametrize("kind", ["order", "receipt"])
def test_orders_and_receipts_come_only_from_the_owner(project, kernel, tmp_path, kind):
    source = tmp_path / "order.pdf"
    source.write_bytes(b"order 123")
    with pytest.raises(InvalidInput, match="only the owner"):
        run(project, "doc-add", source=str(source), kind=kind)
    assert records(project) == [] and not (project.root / "documents").exists()
    assert run(project, "doc-add", source=str(source), kind=kind, role="owner")["record"]["path"] == f"documents/{kind}s/order.pdf"
    assert default_registry().get("doc-add").writes_as_owner({"role": "owner"})


def test_an_unknown_item_or_an_empty_file_is_refused(project, kernel, tmp_path):
    source = tmp_path / "x.pdf"
    source.write_bytes(b"x")
    with pytest.raises(InvalidInput, match="'nonsense'"):
        run(project, "doc-add", source=str(source), kind="other", items=["nonsense"])
    source.write_bytes(b"")
    with pytest.raises(InvalidInput, match="empty"):
        run(project, "doc-add", source=str(source), kind="other")
    assert records(project) == []


def test_edit_replaces_items_and_remove_deletes_the_file(project, kernel, tmp_path):
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"m")
    path = run(project, "doc-add", source=str(source), kind="manual", items=["servo"], note="old")["record"]["path"]
    edited = run(project, "doc-edit", path=path, items=["motor"], note="")["record"]
    assert (edited["items"], "note" in edited, edited["sha256"]) == (["motor"], False, records(project)[0]["sha256"])
    removed = run(project, "doc-remove", path=path)["record"]
    assert removed["removed"] and not (project.root / path).exists()
    with pytest.raises(InvalidInput, match="no document"):
        run(project, "doc-edit", path=path, items=[])
    assert default_registry().get("doc-remove").owner_only


def test_a_cart_mark_and_its_clearing_are_recorded(project, kernel):
    run(project, "cart", id="servo", shop="Galaxus", url="https://www.galaxus.ch/x", qty=8)
    run(project, "cart", id="servo", shop="Galaxus", url="https://www.galaxus.ch/x", clear=True)
    marks = records(project, doc_ops.CARTS_FILE)
    assert [(m["state"], m.get("qty"), m.get("url")) for m in marks] == [("in cart", 8, "https://www.galaxus.ch/x"), ("cleared", None, None)]
    with pytest.raises(InvalidInput, match="'nonsense'"):
        run(project, "cart", id="nonsense", shop="Galaxus")
    with pytest.raises(InvalidInput, match="shop"):
        run(project, "cart", id="servo", shop=" ")
    assert default_registry().get("cart").owner_only


def test_the_page_uploads_a_document_as_the_owner_and_reads_it_back(running, kernel):  # noqa: F811
    query = "/api/page/document?filename=order%2042.pdf&kind=order&items=servo,motor&title=Order%2042"
    status, body = running.request("POST", query, b"%PDF order", page_token=None)
    assert status == 403
    status, body = running.request("POST", query, b"%PDF order")
    assert status == 200, body
    rec = body["record"]
    assert (rec["path"], rec["items"], rec["by"]["role"], rec["title"]) == ("documents/orders/order 42.pdf", ["servo", "motor"], "owner", "Order 42")
    status, raw = running.request("GET", "/api/document?path=documents/orders/order%2042.pdf")
    assert (status, raw) == (200, b"%PDF order")
    for bad in ("review/INDEX.md", "documents/../pyproject.toml", ""):
        status, _ = running.request("GET", f"/api/document?path={bad}")
        assert status == 400, bad
