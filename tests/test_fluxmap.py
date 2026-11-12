"""fluxmap.py (the meta map), its slices and renderer, and `place --objective plan`.  Pure text: any interpreter."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
import test_escape as TE
from fluxplace import fluxmap as FM, planplace as PP, planroute as PL, routediag as RD

CONS = {"placement": {"fixed": ["U1", "J1", "J2"], "group": {"cm": {"refs": ["J1", "J2"], "centre": [70, 40]}}}}


def mapped(tmp, gap=1.0, cons=CONS):
    B = RD.load(TE.board(tmp, gap))
    m = FM.build(B, cons, None, "x.kicad_pcb")
    return B, m


def test_map_is_deterministic_and_versioned(tmp_path):
    B, a = mapped(tmp_path)
    _, b = mapped(tmp_path)
    assert FM.dumps(a) == FM.dumps(b)
    assert a["fluxmap"] == FM.VERSION == 1
    assert list(a)[:6] == ["fluxmap", "board", "parts", "relations", "groups", "nets"]
    assert all(k in a for k in FM.SECTIONS)
    p = FM.save(a, str(tmp_path / "m.fluxmap.json"))
    assert p < 300_000 and FM.load(str(tmp_path / "m.fluxmap.json")) == json.loads(FM.dumps(a))


def test_map_schema_parts_nets_groups(tmp_path):
    B, m = mapped(tmp_path)
    parts = {p["ref"]: p for p in m["parts"]}
    assert parts["U1"]["mode"] in ("fixed", "anchor") and parts["R1"]["mode"] == "movable"
    assert parts["J1"]["group"] == "cm" and parts["U1"]["pitch"] == 0.5
    assert set(parts["U1"]) >= {"ref", "v", "fp", "x", "y", "rot", "side", "court", "h", "pads", "mode", "group"}
    nets = {n["n"]: n for n in m["nets"]}
    assert nets["S0"]["pads"][0][0] in ("J1.1", "U1.1") and nets["S0"]["state"] == "unrouted"
    g = {x["name"]: x for x in m["groups"]}
    assert g["cm"]["members"] == ["J1", "J2"] and "ext_nets" in g["cm"]
    assert m["board"]["floors"]["track"] == 0.127 and [l["role"] for l in m["board"]["layers"]] == ["signal", "signal"]
    assert m["board"]["outline"] and m["board"]["bbox"] == [0.0, 0.0, 100.0, 80.0]


def test_show_slices_are_small(tmp_path):
    B, m = mapped(tmp_path)
    s = json.loads(FM.show(m, "parts", ref="U1"))
    assert s["part"]["ref"] == "U1" and "S0" in s["nets"]
    assert len(FM.show(m, "nets", net="S0")) < 1000
    assert "unknown section" in FM.show(m, "nope")


def test_map_with_plan_sections_and_render(tmp_path):
    B = RD.load(TE.board(tmp_path, 0.35))
    R = PL.run(B, CONS, None, ignore_routing=True)
    m = FM.build(B, CONS, None, "x.kicad_pcb", cls=R["cls"])
    m.update(PL.plan_section(R))
    assert m["verdict"]["score"] == R["score"] and m["escapes"]["pins"]["U1"]
    assert any(v[0] == "s" for v in m["escapes"]["pins"]["U1"].values())
    assert m["grid"]["cell"] == 2.0 and len(m["grid"]["capH"]["F.Cu"]) == m["grid"]["nx"] * m["grid"]["ny"]
    files = FM.render(m, str(tmp_path / "out.png"))
    assert [os.path.basename(f) for f in files] == ["out_F.png", "out_B.png", "out_inner.png"]
    assert all(os.path.getsize(f) > 1000 for f in files)
    files = FM.render(m, str(tmp_path / "out.svg"))
    assert open(files[0]).read().startswith("<svg")


def test_plan_objective_moves_the_blocker_and_never_accepts_worse(tmp_path):
    B = RD.load(TE.board(tmp_path, 0.35))
    mv, st = PP.plan_place(B, CONS, None, log=lambda m: None, time_limit=60)
    assert st["score_after"] < st["score_before"]
    assert st["components_before"]["sealed"] == 8 and st["components_after"]["sealed"] == 0
    assert "R1" in mv and "U1" not in mv                      # the blocker moved, the fixed IC did not
    assert st["log"] and all(e["after"] < e["before"] for e in st["log"])
    prev = None
    for e in st["log"]:                                       # the score only goes down, move after move
        assert prev is None or e["before"] <= prev + 1e-6
        prev = e["after"]
    R = PL.run(RD.load(TE.board(tmp_path, 0.35)), CONS, None, ignore_routing=True)
    assert st["score_before"] == R["score"]                   # the incremental state starts from the full plan
