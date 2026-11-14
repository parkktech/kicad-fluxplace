"""Connectivity invariant (audit.snapshot / connectivity_diff / ledger), the cli.main guard on every board write,
and the pipeline's per-stage ledger with its INCOMPLETE marker."""
import json, os, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
from fluxplace import audit as AU, pipeline as PL
import cli


def _body(gnd2="GND", value=None, extra=""):
    # U1 has a two-piece pin "1" (both GND), like a QFN's split exposed pad
    u = G.fp("U1", 20, 20, [G.pad("1", 0, 0, "GND"), G.pad("1", 1, 0, gnd2), G.pad("2", 2, 0, "SIG")])
    if value:
        u = u.replace('(property "Reference" "U1")', f'(property "Reference" "U1") (property "Value" "{value}")')
    return u + G.two("R1", 40, 20, "SIG", "GND") + extra


def test_multi_piece_pins_compare_as_a_multiset(tmp_path):
    a = AU.snapshot(G.board(tmp_path, _body(), name="a.kicad_pcb"))
    swapped = G.fp("U1", 20, 20, [G.pad("1", 1, 0, "GND"), G.pad("1", 0, 0, "GND"), G.pad("2", 2, 0, "SIG")])
    b = AU.snapshot(G.board(tmp_path, swapped + G.two("R1", 40, 20, "SIG", "GND"), name="b.kicad_pcb"))
    d = AU.connectivity_diff(a, b)
    assert d["ok"] and d["moved"] == {}
    c = AU.snapshot(G.board(tmp_path, _body(gnd2="SIG"), name="c.kicad_pcb"))
    d = AU.connectivity_diff(a, c)
    assert not d["ok"] and d["violations"] == ["net changed: U1.1 GND,GND -> GND,SIG"]


def test_footprint_identity_moves_and_declared_changes(tmp_path):
    a = AU.snapshot(G.board(tmp_path, _body(value="10k"), name="a.kicad_pcb"))
    b = AU.snapshot(G.board(tmp_path, _body(value="22k").replace("(at 40 20", "(at 45 20"), name="b.kicad_pcb"))
    d = AU.connectivity_diff(a, b)
    assert d["violations"] == ["value changed: U1 '10k' -> '22k'"] and d["moved"] == {"R1": [5.0, 0.0, 0.0]}
    assert AU.connectivity_diff(a, b, ("footprints",))["ok"]
    gone = AU.snapshot(G.board(tmp_path, G.fp("U1", 20, 20, [G.pad("1", 0, 0, "GND"), G.pad("1", 1, 0, "GND"),
                                                            G.pad("2", 2, 0, "SIG")]), name="g.kicad_pcb"))
    v = AU.connectivity_diff(a, gone)["violations"]
    assert any(x.startswith("footprint removed: R1") for x in v) and not any(x.startswith("pad removed") for x in v)
    a = AU.snapshot(G.board(tmp_path, _body(), name="a0.kicad_pcb"))
    rn = AU.snapshot(G.board(tmp_path, _body(gnd2="NEW"), name="r.kicad_pcb"))
    d = AU.connectivity_diff(a, rn, AU.EXPECT["sync-nets"])
    assert d["ok"] and d["expected"] and "NEW" in d["nets_touched"]
    rec, line = AU.ledger("x", a, rn, AU.connectivity_diff(a, rn))
    assert line.startswith("x: FAIL connectivity") and rec["checks"]["open"][0] is not None


R1_SIG = '(pad "1" smd rect (at -0.5 0 0.0) (size 0.5 0.5) (layers "F.Cu") (net "SIG"))'


def _renet_cmd(a):
    """stand-in for a buggy board-writing command: re-nets R1.1 on the way through."""
    txt = open(a.board).read().replace(R1_SIG, R1_SIG.replace("SIG", "OOPS"))
    open(a.out or a.board, "w").write(txt)


def test_cli_guard_rejects_an_unintended_net_change(tmp_path, monkeypatch, capsys):
    src = G.board(tmp_path, _body())
    out = str(tmp_path / "o.kicad_pcb")
    monkeypatch.setattr(cli, "cmd_route_ready", _renet_cmd)
    with pytest.raises(SystemExit) as e:
        cli.main(["--full", "route-ready", src, "--out", out])
    o = capsys.readouterr().err
    assert e.value.code == 1 and "net changed: R1.1 SIG -> OOPS" in o
    assert not os.path.exists(out) and os.path.exists(str(tmp_path / "o.rejected.kicad_pcb"))
    assert json.load(open(tmp_path / "o.ledger.json"))["ok"] is False
    before = open(src).read()
    with pytest.raises(SystemExit):                       # in place: the input comes back untouched
        cli.main(["--full", "route-ready", src])
    assert open(src).read() == before


def test_cli_guard_passes_a_clean_write_and_writes_the_ledger(tmp_path, monkeypatch, capsys):
    src = G.board(tmp_path, _body())
    out = str(tmp_path / "o.kicad_pcb")
    def move(a):
        open(a.out, "w").write(open(a.board).read().replace("(at 40 20", "(at 41 20"))
    monkeypatch.setattr(cli, "cmd_route_ready", move)
    cli.main(["--full", "route-ready", src, "--out", out])
    assert "guard: route-ready: OK  moved 1" in capsys.readouterr().err
    rec = json.load(open(tmp_path / "o.ledger.json"))
    assert rec["ok"] and rec["moved"] == {"R1": [1.0, 0.0, 0.0]}


def test_pipeline_stage_invariant_ledger_and_incomplete_marker(tmp_path, monkeypatch):
    from test_pipeline import _args
    src = G.board(tmp_path, _body())

    def bad_stage(c, n, board):
        d = c.dir(n, "anchor-check")
        out = os.path.join(d, "board.kicad_pcb")
        open(out, "w").write(open(board).read().replace(R1_SIG, R1_SIG.replace("SIG", "OOPS")))
        return {"verdict": "pass", "line": "rotated nothing", "board": out}

    monkeypatch.setitem(PL.FUNCS, "anchor-check", bad_stage)
    a = _args(tmp_path, src, to_stage="place")
    assert PL.run(a) == 1
    st = PL.load_state(a.workdir)
    assert st["stages"]["anchor-check"]["verdict"] == "fail" and "connectivity changed" in st["stages"]["anchor-check"]["line"]
    assert st["incomplete"].startswith("INCOMPLETE - do not treat as done; remaining: anchor-check, place")
    led = open(os.path.join(a.workdir, "LEDGER.txt")).read()
    assert "[2] floorplan-check: OK" in led and "[3] anchor-check: FAIL connectivity" in led and "INCOMPLETE" in led
    rec = json.load(open(os.path.join(a.workdir, "3-anchor-check", "ledger.json")))
    assert rec["ok"] is False and rec["violations"]


def test_pipeline_stopped_by_to_is_still_marked_incomplete(tmp_path):
    from test_pipeline import _args
    a = _args(tmp_path, G.board(tmp_path, _body()), to_stage="floorplan-check")
    assert PL.run(a) == 0
    st = PL.load_state(a.workdir)
    assert "INCOMPLETE" in st["incomplete"]                 # --to stops early: still not done
    assert os.path.exists(os.path.join(a.workdir, "2-floorplan-check", "ledger.txt"))


def test_guard_snapshot_uses_the_source_project_rules(tmp_path):
    """A pcbnew save writes a default .kicad_pro beside the output; the collision count must not depend on it.
    Same geometry, the source's rules (Default clearance 0.25 mm) -> same collision_fail before and after a re-save."""
    import json as _j
    from fluxplace import audit as AU2, kicad_io as IO
    import planboards as PB
    body = PB.fp("R1", 40, 40, [PB.pad("1", 0, 0, "N1", 0.5, 0.5)])
    body += PB.fp("R2", 40.72, 40, [PB.pad("1", 0, 0, "N2", 0.5, 0.5)])     # 0.22 mm copper gap: under 0.25, over 0.2
    src = PB.write(tmp_path, body, name="src.kicad_pcb")
    pro = tmp_path / "src.kicad_pro"
    pro.write_text(_j.dumps({"meta": {"version": 3}, "net_settings": {"classes": [
        {"name": "Default", "clearance": 0.25, "track_width": 0.2}]}, "board": {"design_settings": {
        "rules": {"min_clearance": 0.25}, "rule_severities": {"clearance": "error"}}}}))
    before = AU2.snapshot(src, project=str(pro))
    assert before["checks"]["collision_fail"] >= 1
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = str(out_dir / "resaved.kicad_pcb")
    IO.save(IO.load(src), out)
    after = AU2.snapshot(out, project=str(pro))
    assert after["checks"] == before["checks"]
    assert AU2.connectivity_diff(before, after)["ok"]


def test_guard_resave_keeps_the_same_findings_without_a_project(tmp_path):
    """A board with no .kicad_pro: the source's rules are the defaults (clearance 0.14). pcbnew's save writes its own
    project beside the output (clearance 0.2), which used to change the findings for the same pads (0.17 mm gap:
    courtyard overlap before, copper clearance after). The guard passes the source's project to both snapshots."""
    from fluxplace import audit as AU2, kicad_io as IO, legality as LGL, routediag as RD2
    import planboards as PB
    body = PB.fp("R1", 40, 40, [PB.pad("1", 0, 0, "N1", 0.5, 0.5)])
    body += PB.fp("R2", 40.67, 40, [PB.pad("1", 0, 0, "N2", 0.5, 0.5)])
    src = PB.write(tmp_path, body, name="src.kicad_pcb")
    (tmp_path / "out").mkdir()
    out = str(tmp_path / "out" / "resaved.kicad_pcb")
    IO.save(IO.load(src), out)
    pro = str(tmp_path / "src.kicad_pro")                  # does not exist: defaults
    kinds = lambda p: sorted(sorted(x["kinds"]) for x in LGL.collisions(RD2.load(p, project=pro)) if x["level"] == "FAIL")
    assert kinds(src) == kinds(out) and kinds(src)
    assert AU2.snapshot(src, project=pro)["checks"] == AU2.snapshot(out, project=pro)["checks"]


def test_guard_reports_no_collision_change_for_a_plain_resave(tmp_path, capsys):
    """The guard line must not show a collision_fail change for a resave of the same geometry (the source's
    project rules are used for both snapshots, so the output's pcbnew-written project does not count)."""
    import argparse
    from fluxplace import kicad_io as IO
    import planboards as PB
    # 0.17 mm copper gap, courtyards clear: a clearance hit only under the 0.2 mm default pcbnew writes beside the output
    body = PB.fp("R1", 40, 40, [PB.pad("1", 0, 0, "N1", 0.5, 0.5)], court=(-0.2, -0.2, 0.2, 0.2))
    body += PB.fp("R2", 40.67, 40, [PB.pad("1", 0, 0, "N2", 0.5, 0.5)], court=(-0.2, -0.2, 0.2, 0.2))
    src = PB.write(tmp_path, body, name="src.kicad_pcb")
    (tmp_path / "out").mkdir()
    out = str(tmp_path / "out" / "resaved.kicad_pcb")
    ns = argparse.Namespace(cmd="sync-nets", board=src, out=out)
    g = cli._guard_begin(ns)
    IO.save(IO.load(src), out)
    assert cli._guard_end(g, ns)
    import re
    err = capsys.readouterr().err
    m = re.search(r"collision_fail (\d+)->(\d+)", err)
    assert m and m.group(1) == m.group(2), err
