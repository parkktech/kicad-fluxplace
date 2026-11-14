"""Functional blocks (fluxplace/blocks.py): inventory overrides, the schematic field, the board field + guard,
BLOCK_SCATTERED, and modules.build with [placement] use_blocks (off = unchanged)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import audit as AU, blocks as BK, modules as MD, review as RV, routediag as RD, schblocks as SB, schinv as I


def _spec(tmp):
    spec = {"name": "t", "components": [
        {"ref": "U1", "value": "IC", "lib": "", "fp": "", "pinmap": {"1": "1", "2": "2"}},
        {"ref": "C1", "value": "100n", "lib": "", "fp": ""},
        {"ref": "C2", "value": "10u", "lib": "", "fp": ""},
        {"ref": "R1", "value": "10k", "lib": "", "fp": ""},
        {"ref": "J1", "value": "CONN", "lib": "", "fp": ""}],
        "nets": {"VCC": [["U1", "1"], ["C1", "1"], ["C2", "1"]], "GND": [["U1", "2"], ["C1", "2"], ["C2", "2"], ["R1", "2"]],
                 "SIG": [["U1", "2"], ["R1", "1"], ["J1", "1"]]}}
    p = tmp / "spec.json"
    p.write_text(json.dumps(spec))
    return str(p)


def _model(tmp):
    return I.from_spec(_spec(tmp))


def test_overrides_claim_first_and_rest_fall_back(tmp_path):
    M = _model(tmp_path)
    inv = BK.inventory(M, {"blocks": {"Audio codec": ["U1", "C1*"]}})
    assert BK.block_of(inv, "U1") == "Audio codec"
    assert BK.block_of(inv, "C1") == "Audio codec"
    # unclaimed parts still get an automatic block
    assert all(BK.block_of(inv, r) for r in ("C2", "R1", "J1"))
    assert BK.block_of(inv, "C2") != "Audio codec"
    assert inv["warnings"] == []


def test_schematic_alias_and_canonical_merge(tmp_path):
    M = _model(tmp_path)
    inv = BK.inventory(M, {"schematic": {"blocks": {"Power": ["C2"]}}, "blocks": {"Power": ["U1"]}})
    assert BK.block_of(inv, "C2") == "Power" and BK.block_of(inv, "U1") == "Power"


def test_ref_in_two_blocks_is_an_error(tmp_path):
    M = _model(tmp_path)
    with pytest.raises(BK.BlockError, match="U1 is in two blocks"):
        BK.inventory(M, {"blocks": {"A": ["U1"], "B": ["U*"]}})


def test_glob_matching_nothing_warns(tmp_path):
    M = _model(tmp_path)
    inv = BK.inventory(M, {"blocks": {"Ghost": ["X99*"]}})
    assert any("matches no part" in w for w in inv["warnings"])
    assert "Ghost" not in {b["name"] for b in inv["blocks"]}


def test_schematic_writes_functional_group_field(tmp_path):
    M = _model(tmp_path)
    cons = {"blocks": {"Power": ["U1", "C1"]}}
    out = tmp_path / "t.kicad_sch"
    SB.generate(M, str(out), cons, title="t")
    text = "".join(open(os.path.join(tmp_path, f)).read() for f in os.listdir(tmp_path) if f.endswith(".kicad_sch"))
    assert text.count('(property "Functional Group" "Power"') >= 2
    import re
    lines = [ln for ln in text.splitlines() if '(property "Functional Group" "Power"' in ln]
    assert lines and all(re.search(r"\bhide\b", ln) for ln in lines)     # hidden field


def _board(tmp, body):
    return PB.write(tmp, body)


def test_board_field_written_and_guard_passes(tmp_path):
    body = PB.fp("U1", 40, 40, [PB.pad("1", 0, 0, "VCC"), PB.pad("2", 1, 0, "GND")])
    body += PB.fp("C1", 44, 40, [PB.pad("1", -0.5, 0, "VCC", 0.5, 0.5), PB.pad("2", 0.5, 0, "GND", 0.5, 0.5)])
    src = _board(tmp_path, body)
    B = RD.load(src)
    inv = BK.of_board(B, {"blocks": {"Power": ["U1"]}}, None)
    out = str(tmp_path / "out.kicad_pcb")
    assert BK.write_board(src, out, inv) == 2
    text = open(out).read()
    assert text.count('"Functional Group" "Power"') == 1
    d = AU.connectivity_diff(AU.snapshot(src), AU.snapshot(out), expect=())
    assert d["ok"], d["violations"]


def _pos_B(parts):
    return {"parts": {r: {"pos": p} for r, p in parts.items()}}


def test_block_scattered_positive_and_negative():
    inv_split = {"blocks": [{"name": "A", "refs": ["A1", "A2", "A3", "A4", "A5", "A6"], "how": "auto"},
                            {"name": "B", "refs": ["B1"], "how": "auto"}],
                 "of": {}}
    inv_split["of"] = {r: b["name"] for b in inv_split["blocks"] for r in b["refs"]}
    # A splits into a main group at x~0 and a second group at x~40, B1 sits between them
    split = _pos_B({"A1": (0, 0), "A2": (1, 0), "A3": (2, 0), "A4": (40, 0), "A5": (41, 0), "A6": (42, 0), "B1": (21, 0.5)})
    rows = BK.spread(split, inv_split)
    a = [r for r in rows if r["block"] == "A"][0]
    assert a["scattered"] and a["clusters"] == 2 and "B1" in a["between"]
    f = BK.check(split, inv_split, "WARN")
    assert [x["code"] for x in f] == ["BLOCK_SCATTERED"] and f[0]["level"] == "WARN"
    # same spread, but the gap is empty of other blocks' parts: not scattered
    clean = _pos_B({"A1": (0, 0), "A2": (1, 0), "A3": (2, 0), "A4": (40, 0), "A5": (41, 0), "A6": (42, 0), "B1": (21, 30)})
    assert BK.check(clean, inv_split) == []
    # one compact region with a foreign part inside the block's box: not scattered
    compact = _pos_B({"A1": (0, 0), "A2": (1, 0), "A3": (2, 0), "A4": (3, 0), "A5": (3, 1), "A6": (1, 1), "B1": (1.5, 0.5)})
    assert BK.check(compact, inv_split) == []


def test_review_hook_runs_and_is_info_by_default(tmp_path):
    body = PB.fp("U1", 40, 40, [PB.pad("1", 0, 0, "VCC"), PB.pad("2", 1, 0, "GND")])
    body += PB.fp("C1", 44, 40, [PB.pad("1", -0.5, 0, "VCC", 0.5, 0.5), PB.pad("2", 0.5, 0, "GND", 0.5, 0.5)])
    src = _board(tmp_path, body)
    out = RV.check_blocks({"board_path": src}, None, {})
    assert all(f["level"] == "INFO" for f in out)
    out = RV.check_blocks({"board_path": src}, None, {"placement": {"use_blocks": True}})
    assert all(f["level"] in ("WARN", "INFO") for f in out)


def test_modules_use_blocks_keeps_supports_in_their_block(tmp_path):
    sig = [f"S{i}" for i in range(8)]
    body = PB.column_ic("U1", 40, 40, ["VX"] + sig[1:])
    body += PB.fp("C1", 44, 40, [PB.pad("1", -0.5, 0, "VX", 0.5, 0.5), PB.pad("2", 0.5, 0, "GND", 0.5, 0.5)])
    B = RD.load(PB.write(tmp_path, body))
    off = MD.build(B, {}, None, arrange=False)["of"]
    assert off["C1"] == off["U1"]                               # default: the support joins its IC
    cons = {"placement": {"use_blocks": True}, "blocks": {"Power": ["U1"], "Decoupling": ["C1"]}}
    on = MD.build(B, cons, None, arrange=False)
    assert on["of"]["C1"] != on["of"]["U1"]
    assert {m["name"]: m.get("block") for m in on["modules"]}[on["of"]["U1"]] == "Power"


def test_modules_default_unchanged_without_use_blocks(tmp_path):
    sig = [f"S{i}" for i in range(8)]
    body = PB.column_ic("U1", 40, 40, ["VX"] + sig[1:])
    body += PB.fp("C1", 44, 40, [PB.pad("1", -0.5, 0, "VX", 0.5, 0.5), PB.pad("2", 0.5, 0, "GND", 0.5, 0.5)])
    B = RD.load(PB.write(tmp_path, body))
    a = MD.build(B, {}, None, arrange=False)
    b = MD.build(B, {"blocks": {"Power": ["U1"]}}, None, arrange=False)   # claims alone change nothing
    assert a["of"] == b["of"] and all("block" not in m for m in a["modules"])
