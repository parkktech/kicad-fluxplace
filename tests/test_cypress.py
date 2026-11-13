"""Placement engine "cypress": Bookshelf conversion, a fake Cypress tool, legality after the legaliser,
fallback to the built-in placer when the tool is absent, engine recorded by the pipeline."""
import argparse, json, math, os, subprocess, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as g
from fluxplace import (cypress_io as IO, placer_cypress as PC, routediag as RD, netclass as NC, geom as GM,
                       legalise as LG, pipeline as PL)

ROOT = os.path.join(os.path.dirname(__file__), "..")

# stands in for dreamplace/Placer.py: piles every movable cell on the middle of the grid, turns the first one 90 degrees
FAKE = '''
import json, os, sys
cfg = json.load(open(sys.argv[1]))
aux = cfg["aux_input"]
d, files = os.path.dirname(aux), open(aux).read().split(":")[1].split()
name = os.path.splitext(os.path.basename(aux))[0]
rows = sum(1 for l in open(os.path.join(d, name + ".scl")) if l.startswith("CoreRow"))
out, n = ["UCLA pl 1.0", ""], 0
for l in open(os.path.join(d, name + ".pl")):
    t = l.split()
    if len(t) >= 5 and t[3] == ":":
        if "/FIXED" in l:
            out.append(l.strip())
        else:
            side = "F" if t[4].startswith("F") else ""
            out.append("%s %d %d : %s%s" % (t[0], 200 + n % 3, rows // 2 + n % 2, side, "W" if n == 0 else "N"))
            n += 1
res = os.path.join(cfg["result_dir"], name)
os.makedirs(res, exist_ok=True)
open(os.path.join(res, name + ".gp.pl"), "w").write("\\n".join(out) + "\\n")
print("non-linear placement takes 0.01 seconds")
print("Final PPA: {'rsmt': 1.0, 'hpwl': 123.0, 'overflow': 0.05}")
'''


def fake_home(tmp_path):
    h = tmp_path / "Cypress"
    (h / "dreamplace" / "ops" / "hpwl").mkdir(parents=True)
    (h / "dreamplace" / "ops" / "hpwl" / "hpwl_cpp.so").write_text("")
    (h / "dreamplace" / "Placer.py").write_text(FAKE)
    return str(h)


def val(ref, x, y, a, b, **kw):
    return g.two(ref, x, y, a, b, court=(-1.0, -0.5, 1.0, 0.5), **kw)


def mk(tmp_path, **kw):
    b = g.fp("J1", 5, 40, [g.pad(str(i + 1), 0, (i - 2.5) * 2, n, 0.8, 0.6, typ="thru_hole")
                           for i, n in enumerate(["A", "B", "C", "D", "E", "F"])], court=(-1, -7, 1, 7))
    b += g.ic("U1", 80, 70, ["A", "B", "C", "VDD", "GND", "Z1"], pitch=1.27, court=(-3, -3, 3, 3))
    b += g.ic("U2", 20, 10, ["D", "E", "F", "VDD", "GND", "Z2"], pitch=1.27, court=(-3, -3, 3, 3))
    b += val("C1", 90, 10, "VDD", "GND") + val("C2", 70, 15, "Z1", "GND") + val("R1", 15, 70, "Z2", "Z1")
    b += val("R2", 60, 60, "A", "Z2", side="B")
    b += g.fp("MH1", 50, 5, [g.pad("1", 0, 0, "", typ="np_thru_hole")], court=(-1, -1, 1, 1), locked=True)
    return g.board(tmp_path, b, **kw)


CONS = {"placement": {"fixed": ["J1"]}}


def model(B, cons=CONS, **kw):
    from fluxplace import preroute as PR
    return IO.build_model(B, PR.fixed_parts(B, cons), NC.classify(B, cons, None), **kw)


def test_model_cells_sides_fixed_and_nets(tmp_path):
    B = RD.load(mk(tmp_path))
    m = model(B, clusters=[{"ic": "U1", "supports": [{"ref": "C2", "via": "1", "pad": "6", "weight": 1.0}]}])
    by = {c["ref"]: c for c in m["cells"] if c["ref"]}
    assert by["J1"]["fixed"] and by["MH1"]["fixed"] and not by["U1"]["fixed"]
    assert by["R2"]["side"] == "B" and by["U1"]["side"] == "F"
    # the through-hole connector occupies both sides: a second pinless fixed cell
    assert sum(1 for c in m["cells"] if c["fixed"] and c["w"] == by["J1"]["w"] and c["h"] == by["J1"]["h"]) == 2
    nets = {n["net"]: n for n in m["nets"]}
    assert "GND" not in nets and "VDD" not in nets                   # rails: weight 0, dropped
    assert nets["support:C2"]["weight"] == 8.0 and len(nets["support:C2"]["pins"]) == 2
    assert nets["A"]["weight"] > 0 and len(nets["A"]["pins"]) == 3
    # courtyard 6 x 6 mm + 0.2 mm spacing on a 0.1 mm grid
    assert (by["U1"]["w"], by["U1"]["h"]) == (62, 62)


def test_bookshelf_files_and_round_trip(tmp_path):
    B = RD.load(mk(tmp_path))
    m = model(B)
    aux = IO.write_bookshelf(m, str(tmp_path / "bs"))
    d = os.path.dirname(aux)
    nodes = open(os.path.join(d, "board.nodes")).read()
    assert f"NumNodes : {len(m['cells'])}" in nodes and nodes.count(" terminal") == sum(c["fixed"] for c in m["cells"])
    pl = IO.read_pl(os.path.join(d, "board.pl"))
    assert any(o == "FN" for _x, _y, o in pl.values())               # the bottom-side part
    assert open(os.path.join(d, "board.wts")).read().count("\n") == len(m["nets"]) + 2
    tg = IO.targets(m, B, pl)
    assert set(tg) == {"U1", "U2", "C1", "C2", "R1", "R2"}
    for r, (x, y, a) in tg.items():
        assert math.hypot(x - B["parts"][r]["pos"][0], y - B["parts"][r]["pos"][1]) <= 0.08 and a == B["parts"][r]["angle"]


def test_rotation_turns_the_cell_about_its_centre(tmp_path):
    body = g.fp("U1", 30, 30, [g.pad("1", -2, 0, "A"), g.pad("2", 2, 1, "B")], court=(-3, -1, 5, 2)) + val("R1", 60, 30, "A", "B")
    B = RD.load(g.board(tmp_path, body))
    m = model(B, cons={}, gap=0.0)
    c = next(c for c in m["cells"] if c["ref"] == "U1")
    for o, th in (("N", 0), ("W", 90), ("S", 180), ("E", 270)):
        x, y, a = IO.targets(m, B, {c["name"]: (c["x"], c["y"], o)})["U1"]
        court = GM.moved(B["parts"]["U1"], x, y, a)[1]
        assert a == th
        # the courtyard centre stays where the cell centre is
        assert abs((court[0] + court[2]) / 2 - 31) < 1e-6 and abs((court[1] + court[3]) / 2 - 30.5) < 1e-6


def test_outline_blocks_cover_what_is_outside_a_notched_outline(tmp_path):
    pts = [(0, 0), (100, 0), (100, 80), (40, 80), (40, 50), (0, 50)]
    edge = "".join(f'(gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (layer "Edge.Cuts"))\n'
                   for a, b in zip(pts, pts[1:] + pts[:1]))
    B = RD.load(mk(tmp_path, edge=edge))
    blocks = IO.outline_blocks(B)
    assert blocks and abs(sum((r[2] - r[0]) * (r[3] - r[1]) for r in blocks) - 40 * 30) < 1e-6
    assert all(r[0] >= 0 and r[2] <= 40 and r[1] >= 50 for r in blocks)
    assert sum(1 for c in model(B)["cells"] if c["name"].startswith("k")) == 2 * len(blocks)


def test_locate_reports_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("CYPRESS_HOME", raising=False)
    monkeypatch.delenv("CYPRESS_PYTHON", raising=False)
    with pytest.raises(PC.Unavailable, match="no built dreamplace"):
        PC.locate(str(tmp_path))
    with pytest.raises(PC.Unavailable, match="no Python"):
        PC.locate(fake_home(tmp_path))
    t = PC.locate(fake_home(tmp_path / "x"), sys.executable)
    assert t["placer"].endswith("Placer.py") and t["python"] == sys.executable
    monkeypatch.setenv("CYPRESS_HOME", t["home"])
    monkeypatch.setenv("CYPRESS_PYTHON", sys.executable)
    assert PC.locate()["home"] == t["home"]
    with pytest.raises(ValueError):
        PC.engine_of({"placement": {"engine": "nope"}})
    assert PC.engine_of({"placement": {"engine": "cypress"}}) == "cypress" and PC.engine_of({}) == "builtin"
    assert PC.engine_of({"placement": {"engine": "cypress"}}, "builtin") == "builtin"


def test_place_with_fake_tool_is_legal_and_keeps_fixed_parts(tmp_path):
    p = mk(tmp_path)
    B = RD.load(p)
    mv, rep = PC.place(B, CONS, None, str(tmp_path / "w"), fake_home(tmp_path), sys.executable, log=lambda m: None)
    assert rep["engine"] == "cypress" and rep["version"] and rep["tool_hpwl"] == 123.0
    assert len(rep["trials"]) == 4 and rep["score"] == min(t["score"] for t in rep["trials"])
    assert rep["overlaps"] == 0 and rep["outside"] == 0 and rep["fixed_moved"] == []
    assert rep["legaliser_moved"] >= 1                                # the pile had to be spread
    assert "J1" not in mv and "MH1" not in mv and {"U1", "U2"} <= set(mv)
    P = B["parts"]
    refs = sorted(P)
    for i, a in enumerate(refs):
        assert GM.inside(P[a]["court"], B["bbox"])
        for b in refs[i + 1:]:
            if P[a]["side"] == P[b]["side"] or P[a]["tht"] or P[b]["tht"]:
                assert not GM.overlap(P[a]["court"], P[b]["court"], -0.001), (a, b)


def _cli(*argv, env=None):
    return subprocess.run([sys.executable, os.path.join(ROOT, "cli.py"), *argv], capture_output=True, text=True,
                          timeout=120, env=dict(os.environ, **(env or {})))


def test_cli_engine_cypress_and_fallback_when_absent(tmp_path):
    p, out = mk(tmp_path), str(tmp_path / "o.kicad_pcb")
    cons = tmp_path / "c.toml"
    cons.write_text('[placement]\nfixed = ["J1"]\n')
    r = _cli("place", "--board", p, "--strategy", "routable", "--engine", "cypress", "--cypress-home",
             str(tmp_path / "none"), "--constraints", str(cons), "--out", out)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "engine cypress unavailable" in r.stdout and "falling back to the built-in placer" in r.stdout
    assert "routable:" in r.stdout and PC.read_record(out)["engine"] == "builtin"
    r = _cli("place", "--board", p, "--strategy", "routable", "--engine", "cypress", "--cypress-home", fake_home(tmp_path),
             "--cypress-python", sys.executable, "--constraints", str(cons), "--out", out)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "engine cypress" in r.stdout and "overlaps 0" in r.stdout
    rec = PC.read_record(out)
    assert rec["engine"] == "cypress" and rec["version"] and rec["overlaps"] == 0
    B = RD.load(out)
    assert B["parts"]["J1"]["pos"] == (5, 40) and B["parts"]["U1"]["pos"] != (80, 70)


def test_pipeline_records_the_engine(tmp_path, monkeypatch):
    b = g.board(tmp_path, g.ic("U1", 20, 20, ["A", "B", "C", "D"], pitch=1.27) +
                g.ic("U2", 60, 40, ["A", "B", "C", "E"], pitch=1.27) + g.two("R1", 40, 30, "D", "E"))
    monkeypatch.setenv("CYPRESS_HOME", fake_home(tmp_path))
    monkeypatch.setenv("CYPRESS_PYTHON", sys.executable)
    cons = tmp_path / "c.toml"
    cons.write_text('[placement]\nengine = "cypress"\n')
    d = dict(board=b, workdir=str(tmp_path / "W"), constraints=str(cons), spec=None, from_stage=None, to_stage="place",
             status=False, waive=[], force=True, api=False)
    assert PL.run(argparse.Namespace(**d)) == 0
    st = PL.load_state(d["workdir"])
    assert st["stages"]["place"]["verdict"] == "pass" and st["stages"]["place"]["engine"].startswith("cypress ")
    # the default engine is the built-in placer, and it is recorded too
    d2 = dict(d, workdir=str(tmp_path / "W2"), constraints=None)
    assert PL.run(argparse.Namespace(**d2)) == 0
    assert PL.load_state(d2["workdir"])["stages"]["place"]["engine"].startswith("builtin ")


def test_a_run_that_did_not_converge_is_not_used(tmp_path):
    # Cypress writes the START positions when it diverges: that must never be taken for a placement
    h = fake_home(tmp_path)
    pl = os.path.join(h, "dreamplace", "Placer.py")
    open(pl, "w").write(FAKE.replace("'hpwl': 123.0", "'hpwl': inf") + 'print("placement failed")\n')
    B = RD.load(mk(tmp_path))
    said = []
    with pytest.raises(PC.Unavailable, match="no Cypress trial gave a legal placement.*did not converge"):
        PC.place(B, CONS, None, str(tmp_path / "w"), h, sys.executable, log=said.append)
    assert B["parts"]["U1"]["pos"] == (80, 70)
    # it settled at overflow 0.05: each trial was repeated once with that level accepted
    assert sum("once more with stop_overflow 0.07" in m for m in said) == 4
    assert json.load(open(tmp_path / "w" / "trial0r" / "cypress.json"))["stop_overflow"] == 0.07


def test_constructive_flow_uses_the_engine_in_the_floorplan_stage(tmp_path, monkeypatch):
    b = mk(tmp_path)
    monkeypatch.setenv("CYPRESS_HOME", fake_home(tmp_path))
    monkeypatch.setenv("CYPRESS_PYTHON", sys.executable)
    cons = tmp_path / "c.toml"
    cons.write_text('[placement]\nengine = "cypress"\nfixed = ["J1"]\n')
    d = dict(board=b, workdir=str(tmp_path / "W"), constraints=str(cons), spec=None, from_stage="floorplan",
             to_stage="floorplan", flow="constructive", status=False, waive=[], force=True, api=False)
    PL.run(argparse.Namespace(**d))
    st = PL.load_state(d["workdir"])["stages"]["floorplan"]
    assert st["verdict"] == "pass" and st["engine"].startswith("cypress ") and st["line"].startswith("cypress:")
    assert RD.load(st["board"])["parts"]["J1"]["pos"] == (5, 40)
