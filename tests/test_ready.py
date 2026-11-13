"""Readiness gate: impedance geometry computed from a declared stackup, the checklist, exit codes, NOT READY stamping."""
import argparse
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
import cli
from fluxplace import impedance as IM, ready as RDY, pipeline as PL, questions as QS
from fluxplace import stackup as ST

LAYERS = [
    {"name": "F.Cu", "type": "copper", "thickness": 0.035},
    {"name": "d1", "type": "prepreg", "thickness": 0.1, "epsilon_r": 4.1},
    {"name": "In1.Cu", "type": "copper", "thickness": 0.0152},
    {"name": "d2", "type": "core", "thickness": 0.4, "epsilon_r": 4.5},
    {"name": "In2.Cu", "type": "copper", "thickness": 0.0152},
    {"name": "d3", "type": "core", "thickness": 0.1, "epsilon_r": 4.5},
    {"name": "In3.Cu", "type": "copper", "thickness": 0.0152},
    {"name": "d4", "type": "core", "thickness": 0.4, "epsilon_r": 4.5},
    {"name": "In4.Cu", "type": "copper", "thickness": 0.0152},
    {"name": "d5", "type": "prepreg", "thickness": 0.1, "epsilon_r": 4.1},
    {"name": "B.Cu", "type": "copper", "thickness": 0.035},
]
ROWS = [{"name": l["name"] if l["type"] == "copper" else None, "type": l["type"], "thickness": l["thickness"],
         "epsilon_r": l.get("epsilon_r", 4.4)} for l in LAYERS]


def test_models_follow_the_planes():
    planes = ["In1.Cu", "In4.Cu"]
    assert IM.geometry(ROWS, planes, "F.Cu")["model"] == "microstrip"
    g2 = IM.geometry(ROWS, planes, "In2.Cu")
    assert g2["model"] == "asymmetric stripline" and g2["hu"] == pytest.approx(0.4) and g2["hd"] == pytest.approx(0.5, abs=0.01)
    assert IM.geometry(ROWS, ["In2.Cu", "In3.Cu"], "In3.Cu")["model"] in ("microstrip",)
    assert IM.geometry(ROWS, [], "F.Cu")["model"] is None
    assert IM.signal_layers(ROWS, planes) == ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"]


def test_solved_geometry_hits_the_target_and_states_formula_inputs_accuracy():
    g = IM.geometry(ROWS, ["In1.Cu", "In4.Cu"], "F.Cu")
    o = IM.options(50, False, g)[0]
    assert o["z"] == pytest.approx(50, abs=0.6) and o["model"] == "microstrip" and "IPC-2141" in o["formula"]
    assert o["inputs"]["hd"] == 0.1 and o["accuracy_pct"] == 10
    for tgt in (90, 100):
        d = IM.options(tgt, True, g, floor=0.127)
        assert d and all(x["z"] == pytest.approx(tgt, abs=1.0) for x in d)
        assert d[0]["width_mm"] >= 0.127 and d[0]["gap_mm"] >= 0.127 and d[0]["model"] == "edge-coupled microstrip"
    # the closed form matches stackup.py's own function
    w, h = 0.15, 0.1
    assert IM.z_single(w, g) == pytest.approx(ST.microstrip_z0(w, h, 0.035, 4.1))
    assert IM.accuracy(1.0, 0, g, False)[0] == 20                    # w/h = 10: outside the formula's range, said so
    assert IM.options(50, False, IM.geometry(ROWS, [], "F.Cu")) == []


def test_symmetric_stripline_and_edge_coupled_stripline():
    rows = [{"name": "F.Cu", "type": "copper", "thickness": 0.035, "epsilon_r": 4.4},
            {"name": None, "type": "core", "thickness": 0.3, "epsilon_r": 4.4},
            {"name": "In1.Cu", "type": "copper", "thickness": 0.035, "epsilon_r": 4.4},
            {"name": None, "type": "core", "thickness": 0.3, "epsilon_r": 4.4},
            {"name": "B.Cu", "type": "copper", "thickness": 0.035, "epsilon_r": 4.4}]
    g = IM.geometry(rows, ["F.Cu", "B.Cu"], "In1.Cu")
    assert g["model"] == "stripline"
    d = IM.options(100, True, g)[0]
    assert d["model"] == "edge-coupled stripline" and d["z"] == pytest.approx(100, abs=1.0)


def _spec():
    return {"components": [{"ref": "R1", "value": "10k", "lib": "L", "fp": "R_0603_1608Metric",
                            "mpn": "RC0603FR-0710KL", "mfr": "Yageo"}],
            "nets": {"USB_P": [["R1", "1"]], "USB_N": [["R1", "2"]], "RF_GNSS": [["R1", "1"]]}}


def _cons(tmp, extra=""):
    c = tmp / "c.toml"
    c.write_text('[parts]\ngeneric_passives = true\n[stackup]\nplanes = ["In1.Cu", "In4.Cu"]\n' +
                 "".join('[[stackup.layers]]\nname = "%s"\ntype = "%s"\nthickness = %s\n%s' %
                         (l["name"] or "", l["type"], l["thickness"], "epsilon_r = %s\n" % l["epsilon_r"] if "epsilon_r" in l else "")
                         for l in LAYERS) +
                 '[pairs.USB_]\nimpedance_diff = 90\n[rf]\ntarget_z = 50\n' + extra)
    return str(c)


def test_ready_computes_classes_from_the_declared_stackup(tmp_path, capsys):
    sp = tmp_path / "s.json"
    sp.write_text(json.dumps(_spec()))
    (tmp_path / "ds").mkdir()
    (tmp_path / "ds" / "RC0603FR-0710KL.pdf").write_bytes(b"%PDF")
    cli.main(["ready", str(sp), "--constraints", _cons(tmp_path), "--no-api", "--datasheets", str(tmp_path / "ds"), "--json"])
    out = json.loads(capsys.readouterr().out)
    cl = {c["class"]: c for c in out["classes"]}
    assert cl["pair USB_"]["chosen"]["z"] == pytest.approx(90, abs=1) and cl["rf"]["chosen"]["z"] == pytest.approx(50, abs=1)
    assert cl["rf"]["stackup"] == "constraints [stackup]" and cl["rf"]["chosen"]["formula"]
    st = {l["name"]: l["status"] for l in out["checklist"]}
    assert st["parts"] == "PASS" and st["class rf"] == "PASS" and st["board checks"] == "UNCHECKED"


def test_ready_fails_and_exit_codes(tmp_path, capsys):
    sp = tmp_path / "s.json"
    s = _spec()
    s["components"].append({"ref": "U1", "value": "mystery", "lib": "L", "fp": "SOIC-8"})
    sp.write_text(json.dumps(s))
    with pytest.raises(SystemExit) as e:
        cli.main(["ready", str(sp), "--constraints", _cons(tmp_path), "--no-api", "--datasheets", str(tmp_path)])
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "NOT READY" in out and "PART_MPN_MISSING" in out
    cli.main(["ready", str(sp), "--constraints", _cons(tmp_path), "--no-api", "--datasheets", str(tmp_path), "--allow-incomplete"])
    assert "NOT READY:" in capsys.readouterr().out


def test_no_stackup_is_a_question(tmp_path):
    sp = tmp_path / "s.json"
    sp.write_text(json.dumps(_spec()))
    c = tmp_path / "c.toml"
    c.write_text("[parts]\ngeneric_passives = true\n[rf]\ntarget_z = 50\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["ready", str(sp), "--constraints", str(c), "--no-api", "--datasheets", str(tmp_path)])
    assert e.value.code in (1, 3)
    q = json.load(open(tmp_path / "s.questions.json"))["questions"]
    assert "stackup" in [x["id"] for x in q]


def test_unreferenced_class_has_no_geometry(tmp_path, capsys):
    sp = tmp_path / "s.json"
    sp.write_text(json.dumps(_spec()))
    c = tmp_path / "c2.toml"
    c.write_text(open(_cons(tmp_path)).read().replace('planes = ["In1.Cu", "In4.Cu"]', "planes = []"))
    c = str(c)
    with pytest.raises(SystemExit) as e:
        cli.main(["ready", str(sp), "--constraints", c, "--no-api", "--datasheets", str(tmp_path)])
    assert e.value.code == 1 and "no computed geometry" in capsys.readouterr().out


def _board(tmp_path):
    return G.board(tmp_path, G.ic("U1", 20, 20, ["A", "B", "C", "D"], pitch=1.27) + G.two("R1", 40, 30, "D", "A"))


def test_not_ready_board_is_stamped_and_fab_deliver_refuse(tmp_path, capsys):
    b = _board(tmp_path)
    sp = tmp_path / "s.json"
    s = _spec()
    s["components"].append({"ref": "U1", "value": "mystery", "lib": "L", "fp": "SOIC-8"})
    sp.write_text(json.dumps(s))
    cons = _cons(tmp_path)
    cli.main(["ready", str(sp), "--board", b, "--constraints", cons, "--no-api", "--datasheets", str(tmp_path), "--allow-incomplete",
              "--answer", "rule-floors=standard"])
    assert QS.notready_reasons(b)
    with pytest.raises(SystemExit) as e:
        QS.refuse_assumed(b, "fab")
    assert "NOT READY" in str(e.value)
    c = tmp_path / "next.kicad_pcb"
    c.write_text(open(b).read())
    QS.inherit(b, str(c))
    assert QS.notready_reasons(str(c)) == QS.notready_reasons(b)


def test_pipeline_ready_stage_blocks_and_allow_incomplete_stamps(tmp_path):
    from test_pipeline import _args
    b = _board(tmp_path)
    sp = tmp_path / "s.json"
    s = _spec()
    s["components"].append({"ref": "U1", "value": "mystery", "lib": "L", "fp": "SOIC-8"})
    sp.write_text(json.dumps(s))
    cons = _cons(tmp_path)
    kw = dict(spec=str(sp), constraints=cons, answer=["rule-floors=standard"], to_stage="floorplan-check")
    a = _args(tmp_path, b, **kw)
    assert PL.run(a) == 1
    st = PL.load_state(a.workdir)
    assert st["stages"]["ready"]["verdict"] == "blocked" and "floorplan-check" not in st["stages"]
    a2 = _args(tmp_path, b, allow_incomplete=True, **kw)
    assert PL.run(a2) == 0
    st = PL.load_state(a2.workdir)
    assert st["stages"]["floorplan-check"]["verdict"] == "pass" and "parts" in st["not_ready"]
    assert "NOT READY" in PL.format_status(st)
