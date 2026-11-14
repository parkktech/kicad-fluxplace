"""schematic --layout blocks: inventory, drafting, self-checks and the netlist / legibility gate on synthetic specs."""
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import schblocks as SB, schdraw as D, schinv as I, schlegible as LG

HAVE_CLI = shutil.which("kicad-cli") is not None


def spec(tmp_path, extra_nets=None, name="t"):
    """Two ICs with their supports, a connector, an RC chain, a crystal and a mounting hole."""
    comps = [
        {"ref": "U1", "value": "MCU", "lib": "Pkg", "fp": "QFN16",
         "pinmap": {"VDD": "1", "GND": "2", "SDA": "3", "SCL": "4", "XI": "5", "XO": "6", "RST": "7", "SPARE": "8", "EP": "9"}},
        {"ref": "U2", "value": "SENSOR", "lib": "Pkg", "fp": "SOT23-6"},
        {"ref": "J1", "value": "HDR4", "lib": "Conn", "fp": "PinHeader_1x04"},
        {"ref": "Y1", "value": "8MHz", "lib": "Crystal", "fp": "XTAL_3225"},
        {"ref": "MH1", "value": "M3", "lib": "Mech", "fp": "MountingHole_3.2mm_Pad"},
    ]
    for r, v in [("C1", "100nF"), ("C2", "10uF"), ("C3", "100nF"), ("C4", "12pF"), ("C5", "12pF"), ("C6", "1nF"),
                 ("R1", "4.7k"), ("R2", "4.7k"), ("R3", "10k"), ("R4", "100R")]:
        comps.append({"ref": r, "value": v, "lib": "Passive", "fp": "0402"})
    nets = {
        "+3V3": [["U1", "VDD"], ["U2", "1"], ["J1", "1"], ["C1", "1"], ["C2", "1"], ["C3", "1"], ["R1", "1"], ["R2", "1"], ["R3", "1"]],
        "GND": [["U1", "GND"], ["U1", "EP"], ["U2", "2"], ["J1", "4"], ["C1", "2"], ["C2", "2"], ["C3", "2"], ["C4", "2"],
                ["C5", "2"], ["C6", "2"], ["MH1", "1"]],
        "I2C_SDA": [["U1", "SDA"], ["U2", "3"], ["R1", "2"], ["J1", "2"]],
        "I2C_SCL": [["U1", "SCL"], ["U2", "4"], ["R2", "2"], ["J1", "3"]],
        "XI": [["U1", "XI"], ["Y1", "1"], ["C4", "1"]],
        "XO": [["U1", "XO"], ["Y1", "3"], ["C5", "1"]],
        "RST": [["U1", "RST"], ["R3", "2"], ["R4", "1"]],
        "RST_F": [["R4", "2"], ["C6", "1"], ["U2", "5"]],
        "Y1_GND": [["Y1", "2"], ["Y1", "4"]],
    }
    nets.update(extra_nets or {})
    p = tmp_path / ("%s.json" % name)
    p.write_text(json.dumps({"name": name, "components": comps, "nets": nets}))
    return str(p)


def test_model_resolves_pinmap_and_declared_no_connects(tmp_path):
    M = I.load(spec=spec(tmp_path))
    u1 = M["comps"]["U1"]
    assert u1["pins"]["3"] == {"name": "SDA", "net": "I2C_SDA"}      # pin NAME in the spec -> real pad number
    assert ("U1", "3") in M["nets"]["I2C_SDA"]
    assert u1["nc"] == [("8", "SPARE")]                               # in the pinmap, on no net
    assert M["comps"]["U2"]["nc"] == []                               # no pinmap: nothing is invented


def test_inventory_groups_supports_and_mechanics(tmp_path):
    M = I.load(spec=spec(tmp_path))
    inv = I.inventory(M, {})
    of = inv["of"]
    assert of["C4"] == of["U1"] and of["Y1"] == of["U1"]
    assert of["MH1"] == "Mechanical"
    assert sorted(r for b in inv["blocks"] for r in b["refs"]) == sorted(M["comps"])


def test_inventory_override_from_constraints(tmp_path):
    M = I.load(spec=spec(tmp_path))
    cons = {"schematic": {"blocks": {"Clock": ["Y1", "C4", "C5"], "Reset": "R[34]"}}}
    inv = I.inventory(M, cons)
    by = {b["name"]: set(b["refs"]) for b in inv["blocks"]}
    assert by["Clock"] == {"Y1", "C4", "C5"} and by["Reset"] == {"R3", "R4"}
    assert inv["blocks"][0]["how"] == "constraints"


def test_layout_has_no_net_conflicts_and_is_on_grid(tmp_path):
    M = I.load(spec=spec(tmp_path))
    kinds = I.net_kinds(M, {})
    inv = I.inventory(M, {})
    for b in inv["blocks"]:
        sheet, _ext = D.layout_block(M, kinds, b, D.Lib())
        assert D.conflicts(sheet) == []
        for w in sheet.wires:
            assert all(float(v).is_integer() for v in w[:4])
            assert w[0] == w[2] or w[1] == w[3]
        for s in sheet.syms:
            assert float(s["x"]).is_integer() and float(s["y"]).is_integer()


def test_split_and_junction_and_trim():
    s = D.Draw()
    s.wires = [[0, 0, 10, 0, "A"], [5, 0, 5, 5, "A"], [10, 0, 10, 3, "A"]]
    s.pts = [(0, 0, "A"), (5, 5, "A"), (10, 0, "A")]
    s.labels = []
    D.split_and_junction(s)
    # the T at (5, 0) splits the trunk and gets a junction; the stub to (10, 3) ends on nothing and is trimmed
    assert (5, 0) in s.junctions
    assert [0, 0, 5, 0, "A"] in s.wires and [5, 0, 5, 5, "A"] in s.wires
    assert not any(w[2:4] == [10, 3] for w in s.wires)


def test_legibility_finds_overlaps(tmp_path):
    p = tmp_path / "x.kicad_sch"
    p.write_text('''(kicad_sch (version 20231120) (generator "t")
  (lib_symbols (symbol "t:B" (symbol "B_0_1" (rectangle (start 0 0) (end 10 -10)))))
  (wire (pts (xy 0 20) (xy 20 20)))
  (wire (pts (xy 10 20) (xy 30 20)))
  (wire (pts (xy 5 0) (xy 5 15)))
  (symbol (lib_id "t:B") (at 0 0 0) (property "Reference" "U9" (at 50 50 0)))
  (label "AAA" (at 40 40 0) (effects (font (size 1.27 1.27))))
  (label "BBB" (at 41 40 0) (effects (font (size 1.27 1.27))))
)''')
    res = LG.check_tree(str(p))
    assert res["counts"]["WIRE_OVERLAP"] == 1
    assert res["counts"]["WIRE_THROUGH_BODY"] == 1
    assert res["counts"]["LABEL_OVERLAP"] == 1


@pytest.mark.skipif(not HAVE_CLI, reason="kicad-cli not installed")
def test_generate_and_gate_pass(tmp_path):
    sp = spec(tmp_path)
    M = I.load(spec=sp)
    out = str(tmp_path / "out" / "t.kicad_sch")
    os.makedirs(os.path.dirname(out))
    st = SB.generate(M, out, {})
    assert all(b["conflicts"] == 0 for b in st["blocks"])
    g = SB.gate(M, out, render_dir=str(tmp_path / "r"), sheet_names=[b["name"] for b in st["blocks"]])
    assert g["netlist"]["verdict"] == "PASS", g["netlist"]
    assert g["netlist"]["nc_declared"] == 1
    assert g["legibility"]["total"] == 0, g["legibility"]
    assert g["erc"].get("by_severity", {}).get("error", 0) == 0, g["erc"]
    assert g["render"]["pdf"] and g["render"]["pngs"]
    for f in st["files"]:                      # never an absolute path inside KiCad files
        if f.endswith((".kicad_sch", "sym-lib-table")):
            assert str(tmp_path) not in open(f).read()
    text = open(out).read()
    assert "PWR_FLAG" in text and '(sheet ' in text


@pytest.mark.skipif(not HAVE_CLI, reason="kicad-cli not installed")
def test_gate_fails_on_any_difference(tmp_path):
    M = I.load(spec=spec(tmp_path))
    out = str(tmp_path / "t.kicad_sch")
    st = SB.generate(M, out, {})
    names = [b["name"] for b in st["blocks"]]
    net = SB.export_netlist(out)
    M["nets"]["I2C_SDA"] = [c for c in M["nets"]["I2C_SDA"] if c != ("J1", "2")]
    r = SB.compare(M, net, names)
    assert r["verdict"] == "FAIL" and r["differing_nets"][0]["net"] == "I2C_SDA"
    M2 = I.load(spec=spec(tmp_path))
    M2["comps"]["R1"]["value"] = "10k"
    assert SB.compare(M2, net, names)["differing_components"][0]["ref"] == "R1"
