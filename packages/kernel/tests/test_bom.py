"""Materials and tools: bom.toml, the owner's inventory and what is still short."""

import json

import pytest

from workbench_kernel import bom

BOM = """
title = "Parts"
intro = "First list."

[[hold]]
what = "Plates"
wait_for = "R-0027"

[[group]]
name = "Bought"
kind = "bought"
[[group.item]]
id = "servo"
name = "Servo"
qty = 9
qty_text = "8 + 1"
status = "decided"
[[group.item]]
id = "arm"
name = "Arm"
qty = 2
status = "owned"

[[group]]
name = "Printed"
kind = "printed"
[[group.item]]
id = "mount"
name = "Wheel mount"
qty = 3

[[group]]
name = "Tools"
kind = "tools"
[[group.item]]
id = "calipers"
name = "Calipers"
qty = 1
for = "measuring the hub ring"
service = ""
"""


@pytest.fixture
def root(tmp_path):
    (tmp_path / "bom.toml").write_text(BOM)
    return tmp_path


def have(root, item, qty):
    path = root / bom.INVENTORY_FILE
    path.parent.mkdir(exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"id": item, "have": qty, "by": {"role": "owner"}, "at": "2026-10-16T10:00:00Z"}) + "\n")


def rows(root):
    return {row["id"]: row for row in bom.status(root)["items"]}


def test_nothing_recorded_means_everything_short_except_what_is_owned(root):
    r = rows(root)
    assert (r["servo"]["short"], r["servo"]["action"]) == (9, "to order")
    assert (r["arm"]["have"], r["arm"]["short"]) == (2, 0)  # owned counts in full
    assert (r["mount"]["action"], r["calipers"]["action"]) == ("to make", "missing")


def test_the_latest_inventory_record_counts(root):
    have(root, "servo", 4)
    have(root, "servo", 7)
    have(root, "arm", 1)  # a record overrides "owned"
    r = rows(root)
    assert (r["servo"]["have"], r["servo"]["short"]) == (7, 2)
    assert r["arm"]["short"] == 1
    assert bom.status(root)["short"] == {"to order": 2, "to make": 1, "missing": 1, "on hold": 0, "decide first": 0, "optional": 0}


@pytest.mark.parametrize("record", [{"id": "nonsense", "have": 1}, {"id": "servo", "have": "abc"}, {"id": "servo", "have": None},
                                    {"id": "servo", "have": -5}, {"id": "servo", "have": float("nan")}, {"id": 5, "have": 1}])
def test_a_stale_or_bad_record_is_skipped_and_reported_not_fatal(root, record):
    """The ledger is append-only: a record for a renamed item, or a hand-edited value, must not
    stop the list (R-0116, R-0120); the latest usable record still counts."""
    have(root, "servo", 4)
    path = root / bom.INVENTORY_FILE
    with path.open("a") as f:
        f.write(json.dumps({**record, "by": {"role": "owner"}, "at": "2026-10-16T10:01:00Z"}) + "\n")
    status = bom.status(root)
    assert len(status["problems"]) == 1 and "skipped" in status["problems"][0]
    assert {r["id"]: r["have"] for r in status["items"]}["servo"] == 4


def test_held_and_undecided_items_are_listed_but_not_counted_as_ready_to_order(tmp_path):
    text = BOM.replace('wait_for = "R-0027"', 'wait_for = "R-0027"\nitems = ["mount"]').replace('qty = 2\nstatus = "owned"', 'qty = 2\nstatus = "assumed"')
    (tmp_path / "bom.toml").write_text(text)
    status = bom.status(tmp_path)
    r = {row["id"]: row for row in status["items"]}
    assert (r["mount"]["action"], r["mount"]["hold"], r["mount"]["short"]) == ("on hold", "R-0027", 3)
    assert (r["arm"]["action"], r["arm"]["short"]) == ("decide first", 2)
    assert status["short"]["to order"] == 1 and status["short"]["to make"] == 0 and status["short"]["on hold"] == 1
    assert "| Arm | 2 | assumed |" in bom.markdown(bom.load(tmp_path))


def test_shortfalls_are_rounded(root):
    (root / "bom.toml").write_text(BOM.replace('qty = 3', 'qty = 0.3\nunit = "m"'))
    have(root, "mount", 0.1)  # 0.3 - 0.1 is 0.19999999999999998 in floating point
    assert repr(rows(root)["mount"]["short"]) == "0.2" and rows(root)["mount"]["unit"] == "m"


@pytest.mark.parametrize("bad", [
    BOM.replace('qty = 3', 'qty = -1'),
    BOM.replace('kind = "printed"', 'kind = "glued"'),
    BOM.replace('id = "mount"', 'id = "servo"'),
    BOM.replace('status = "decided"', 'status = "maybe"'),
    BOM.replace('qty_text = "8 + 1"', 'colour = "red"'),
    BOM.replace('wait_for = "R-0027"', ''),
    BOM.replace('wait_for = "R-0027"', 'wait_for = "R-0027"\nitems = ["nonsense"]'),
    BOM.replace('qty_text = "8 + 1"', 'notes = 5'),
    BOM.replace('id = "mount"', 'id = 5'),
    BOM.replace('qty = 3', 'qty = nan'),
    BOM.replace('qty = 3', 'qty = inf'),
])
def test_malformed_boms_are_refused(tmp_path, bad):
    (tmp_path / "bom.toml").write_text(bad)
    with pytest.raises(bom.BomError):
        bom.load(tmp_path)


def test_markdown_lists_holds_groups_and_tools_without_inventory(root):
    have(root, "servo", 9)
    text = bom.markdown(bom.load(root))
    assert text.startswith("# Parts") and "Generated from bom.toml" in text
    assert "| Plates | R-0027 |" in text
    assert "| Servo | 8 + 1 | decided |" in text
    assert "| Calipers | 1 | measuring the hub ring |" in text
    assert "have" not in text.lower().split("generated")[1]


def test_made_items_name_their_output_files_and_unknown_names_are_reported(tmp_path):
    (tmp_path / "bom.toml").write_text(BOM.replace('id = "mount"\nname = "Wheel mount"\nqty = 3', 'id = "mount"\nname = "Wheel mount"\nqty = 3\noutputs = ["wheel_mount.stl"]'))
    loaded = bom.load(tmp_path)
    assert loaded.items["mount"].outputs == ("wheel_mount.stl",)
    assert "| Wheel mount | 3 |  |  | wheel_mount.stl |" in bom.markdown(loaded)
    assert bom.check_outputs(loaded, {"wheel_mount.stl"}) == []
    assert bom.check_outputs(loaded, {"lower_plate.dxf"}) == ["mount: output 'wheel_mount.stl' is not declared on the manifest"]
    assert {r["id"]: r for r in bom.status(tmp_path)["items"]}["mount"]["outputs"] == ["wheel_mount.stl"]


@pytest.mark.parametrize("bad", [
    BOM.replace('qty = 3', 'qty = 3\noutputs = "wheel_mount.stl"'),
    BOM.replace('qty = 3', 'qty = 3\noutputs = [5]'),
    BOM.replace('qty = 2\nstatus = "owned"', 'qty = 2\nstatus = "owned"\noutputs = ["arm.stl"]'),  # a bought item
])
def test_bad_outputs_are_refused(tmp_path, bad):
    (tmp_path / "bom.toml").write_text(bad)
    with pytest.raises(bom.BomError, match="outputs"):
        bom.load(tmp_path)


def link(root, item, url, state, **extra):
    path = root / bom.LINKS_FILE
    path.parent.mkdir(exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"id": item, "url": url, "state": state, "by": {"role": "owner"}, "at": "2026-10-16T10:00:00Z", **extra}) + "\n")


def test_the_latest_link_and_the_latest_good_one_are_given_per_item(root):
    link(root, "servo", "https://a.ch/s", "requested")
    link(root, "servo", "https://a.ch/s", "ok", price=13.9, shop="A")
    link(root, "servo", "https://b.ch/s", "requested")
    r = rows(root)
    assert r["servo"]["source"]["url"] == "https://a.ch/s" and r["servo"]["source"]["price"] == 13.9
    assert (r["servo"]["link"]["url"], r["servo"]["link"]["state"]) == ("https://b.ch/s", "requested")
    assert "link" not in r["arm"] and "source" not in r["arm"]
    link(root, "servo", "https://b.ch/s", "wrong", summary="7.4 V version")
    r = rows(root)
    assert r["servo"]["source"]["url"] == "https://a.ch/s" and r["servo"]["link"]["state"] == "wrong"


@pytest.mark.parametrize("record", [{"id": "nonsense", "url": "https://a.ch", "state": "ok"}, {"id": "servo", "url": "https://a.ch", "state": "great"}])
def test_a_stale_or_bad_link_is_skipped_and_reported(root, record):
    path = root / bom.LINKS_FILE
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(record) + "\n")
    status = bom.status(root)
    assert len(status["problems"]) == 1 and "skipped" in status["problems"][0]
    assert "link" not in {r["id"]: r for r in status["items"]}["servo"]


def test_a_broken_links_line_is_an_error(root):
    path = root / bom.LINKS_FILE
    path.parent.mkdir(exist_ok=True)
    path.write_text('{"id": "servo"\n')
    with pytest.raises(bom.BomError, match="links.jsonl:1"):
        bom.status(root)


def record(root, name, **fields):
    path = root / name
    path.parent.mkdir(exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({**fields, "by": {"role": "owner"}, "at": "2026-10-19T10:00:00Z"}) + "\n")


def test_documents_are_listed_and_given_to_the_items_they_name(root):
    sheet = root / "documents" / "datasheets" / "servo.pdf"
    sheet.parent.mkdir(parents=True)
    sheet.write_bytes(b"%PDF")
    record(root, bom.DOCUMENTS_FILE, path="documents/datasheets/servo.pdf", kind="datasheet", items=["servo", "gone"])
    record(root, bom.DOCUMENTS_FILE, path="documents/receipts/shop.pdf", kind="receipt", items=[])
    result = bom.status(root)
    docs = {d["path"]: d for d in result["documents"]}
    assert set(docs) == {"documents/datasheets/servo.pdf", "documents/receipts/shop.pdf"}
    assert docs["documents/datasheets/servo.pdf"]["items"] == ["servo"]  # the stale id is dropped
    assert docs["documents/receipts/shop.pdf"]["missing"] is True
    assert any("'gone'" in p for p in result["problems"]) and any("shop.pdf" in p and "missing" in p for p in result["problems"])
    r = {row["id"]: row for row in result["items"]}
    assert r["servo"]["documents"] == ["documents/datasheets/servo.pdf"]
    assert "documents" not in r["arm"]


def test_the_latest_document_record_counts_and_a_removal_unlists_it(root):
    record(root, bom.DOCUMENTS_FILE, path="documents/datasheets/a.pdf", kind="datasheet", items=["servo"])
    record(root, bom.DOCUMENTS_FILE, path="documents/datasheets/a.pdf", kind="datasheet", items=["arm"], title="Arm sheet")
    record(root, bom.DOCUMENTS_FILE, path="documents/other/b.txt", kind="other", items=[])
    record(root, bom.DOCUMENTS_FILE, path="documents/other/b.txt", removed=True)
    record(root, bom.DOCUMENTS_FILE, path="../outside.pdf", kind="other", items=[])
    result = bom.status(root)
    (doc,) = result["documents"]
    assert (doc["items"], doc["title"]) == (["arm"], "Arm sheet")
    assert any("outside.pdf" in p for p in result["problems"])


def test_a_cart_mark_holds_per_shop_until_cleared(root):
    record(root, bom.CARTS_FILE, id="servo", shop="Galaxus", state="in cart", qty=9)
    record(root, bom.CARTS_FILE, id="servo", shop="Digitec", state="in cart")
    record(root, bom.CARTS_FILE, id="servo", shop="digitec", state="cleared")  # shop names match without case
    record(root, bom.CARTS_FILE, id="arm", shop="Galaxus", state="lost")
    result = bom.status(root)
    r = {row["id"]: row for row in result["items"]}
    assert [(c["shop"], c.get("qty")) for c in r["servo"]["carts"]] == [("Galaxus", 9)]
    assert "carts" not in r["arm"] and any("'lost'" in p for p in result["problems"])
