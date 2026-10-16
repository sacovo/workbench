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
    assert bom.status(root)["short"] == {"to order": 2, "to make": 1, "missing": 1}


def test_a_record_for_an_unknown_item_is_an_error(root):
    have(root, "nonsense", 1)
    with pytest.raises(bom.BomError, match="not in the bom"):
        bom.status(root)


@pytest.mark.parametrize("bad", [
    BOM.replace('qty = 3', 'qty = -1'),
    BOM.replace('kind = "printed"', 'kind = "glued"'),
    BOM.replace('id = "mount"', 'id = "servo"'),
    BOM.replace('status = "decided"', 'status = "maybe"'),
    BOM.replace('qty_text = "8 + 1"', 'colour = "red"'),
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
