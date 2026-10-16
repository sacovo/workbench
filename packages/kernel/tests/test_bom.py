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
