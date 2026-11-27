"""T4/T5: inputs checklist (absolute manifest, .kicad_pro, derived_from, doctor) and the kickoff stamp."""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import kickoff as KO, ready as RDY


def _w(p, t):
    p.write_text(t)
    return str(p)


def test_inputs_other_cwd_manifest_absolute_and_project_required(tmp_path, monkeypatch):
    d = tmp_path / "p"
    d.mkdir()
    board, spec = _w(d / "b.kicad_pcb", "x"), _w(d / "s.json", "{}")
    monkeypatch.chdir(tmp_path)
    rel = os.path.relpath(board, tmp_path)
    lines, man = KO.inputs_lines(rel, os.path.relpath(spec, tmp_path), None, {}, None, None, with_doctor=False)
    st = {l["name"]: l["status"] for l in lines}
    assert man["board"] == board and man["spec"] == spec and os.path.isabs(man["board"])
    assert st["project file"] == RDY.FAIL and st["inputs"] == RDY.PASS          # no .kicad_pro -> FAIL
    _w(d / "b.kicad_pro", "{}")
    monkeypatch.chdir("/")                                                      # another cwd: still resolved from the given path
    lines, man = KO.inputs_lines(board, spec, None, {}, None, None, with_doctor=False)
    assert man["project"] == str(d / "b.kicad_pro") and all(l["status"] == RDY.PASS for l in lines)
    lines, _ = KO.inputs_lines(board, str(d / "gone.json"), None, {}, None, None, with_doctor=False)
    assert lines[0]["status"] == RDY.FAIL


def test_derived_from_stale_copy_fails(tmp_path):
    src = tmp_path / "constraints.toml"
    src.write_text("a = 1\n")
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    cp = tmp_path / "route.toml"
    cp.write_text("# GENERATED from constraints.toml\na = 1\n")
    assert KO.derived_line(str(cp))["status"] == RDY.WARN                       # generated, no derived_from: legacy, WARN
    cp.write_text(f'# GENERATED\nderived_from = "constraints.toml#{sha[:16]}"\na = 1\n')
    assert KO.derived_line(str(cp))["status"] == RDY.PASS
    src.write_text("a = 2\n")                                                    # the source moved on: the copy is stale
    l = KO.derived_line(str(cp))
    assert l["status"] == RDY.FAIL and "STALE" in l["detail"]
    hand = tmp_path / "hand.toml"
    hand.write_text("a = 1\n")
    assert KO.derived_line(str(hand)) is None                                   # a hand-written file is not a copy


def test_doctor_line_is_a_checklist_line():
    l = KO.doctor_line()
    assert l["name"] == "doctor" and l["status"] in (RDY.PASS, RDY.FAIL)


def test_stamp_staleness_by_input(tmp_path):
    spec, cons = tmp_path / "s.json", tmp_path / "c.toml"
    spec.write_text("{}")
    cons.write_text("a=1")
    ds = tmp_path / "ds"
    ds.mkdir()
    (ds / "x.pdf").write_text("1")
    facts = {"parts": {"R1": {"value": "1k", "footprint": "R_0603"}}}
    man = KO.manifest(str(tmp_path / "b.kicad_pcb"), str(spec), str(cons), {}, str(ds), None)
    h = KO.shas(man, facts)
    p = tmp_path / "b.kickoff.json"
    KO.write_stamp(str(p), man, h, [{"name": "parts gate", "status": "PASS"}])
    stamp = KO.load_stamp(str(p))
    assert KO.stale(stamp, KO.shas(man, facts)) == []
    spec.write_text('{"parts": ["MOD200"]}')
    assert [k for k, _ in KO.stale(stamp, KO.shas(man, facts))] == ["spec"]
    facts["parts"]["R107"] = {"value": "10k", "footprint": "R_0603"}            # a new part
    k = dict(KO.stale(stamp, KO.shas(man, facts)))
    assert set(k) == {"spec", "parts"} and {"parts", "mech", "pin", "placement"} <= set(k["parts"])
    (ds / "y.pdf").write_text("2")
    assert "datasheets" in dict(KO.stale(stamp, KO.shas(man, facts)))


def test_reopen_runs_decider_and_defaults_to_reopen(tmp_path):
    spec = tmp_path / "s.json"
    spec.write_text("{}")
    man = {"spec": str(spec)}
    h = KO.shas(man, None)
    work = tmp_path / "w"
    work.mkdir()
    KO.write_stamp(str(work / "kickoff.json"), man, h, [])

    class C:
        spec_path, cons_path, cons = str(spec), None, {}
    assert KO.reopen(C, None, str(work), log=lambda *a: None) is None
    spec.write_text('{"new": 1}')
    r = KO.reopen(C, None, str(work), log=lambda *a: None)
    assert r["choice"] == "reopen" and r["changed"][0][0] == "spec"


def test_pipeline_reopens_ready_on_a_changed_input_and_stops_on_fail(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.dirname(__file__))
    import test_pipeline as TP
    from fluxplace import pipeline as PL
    spec = tmp_path / "spec.json"
    spec.write_text("{}")
    b = TP._board(tmp_path)
    a = TP._args(tmp_path, b, from_stage="critroute", to_stage="critroute", spec=str(spec))
    os.makedirs(a.workdir)
    KO.write_stamp(os.path.join(a.workdir, "kickoff.json"), {"spec": str(spec)}, KO.shas({"spec": str(spec)}, None), [])
    from fluxplace import decider as DC
    monkeypatch.setattr(DC, "call_model", lambda *x, **k: (None, "offline"))     # no model: the default (reopen)
    ran = []
    monkeypatch.setitem(PL.FUNCS, "critroute", lambda c, n, board: ran.append("critroute") or {"verdict": "pass", "line": "x", "board": board})
    monkeypatch.setattr(PL, "st_ready", lambda c, n, board: ran.append("ready") or {"verdict": "blocked", "line": "new part fails parts gate", "board": board})
    assert PL.run(a) == 0 and ran == ["critroute"]                              # unchanged input: no re-run
    ran.clear()
    spec.write_text('{"parts": ["MOD200"]}')                                    # a new part in the spec
    assert PL.run(a) == 1 and ran == ["ready"]                                  # ready re-opened, FAILed, the stage never started
    monkeypatch.setattr(PL, "st_ready", lambda c, n, board: ran.append("ready") or {"verdict": "pass", "line": "ok", "board": board})
    ran.clear()
    assert PL.run(a) == 0 and ran == ["ready", "critroute"]


def test_stock_only_failures_are_warn_for_the_stale_recheck(tmp_path):
    rj = tmp_path / "b.ready.json"
    mk = lambda *ls: rj.write_text(json.dumps({"checklist": list(ls)}))        # noqa: E731
    mk({"name": "parts gate", "status": "FAIL", "blockers": ["STOCK_UNCHECKED R1 x: stock not checked", "distributor data and stock NOT CHECKED"]},
       {"name": "x", "status": "PASS", "blockers": []})
    assert KO.stock_only(str(rj))
    mk({"name": "parts gate", "status": "FAIL", "blockers": ["STOCK_UNCHECKED R1"]}, {"name": "3D", "status": "FAIL", "blockers": ["MODEL_MISSING C1"]})
    assert not KO.stock_only(str(rj))
    assert not KO.stock_only(str(tmp_path / "none.json"))


def test_pipeline_without_a_stamp_writes_one_and_continues_and_offline_stock_is_warn(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.dirname(__file__))
    import test_pipeline as TP
    from fluxplace import pipeline as PL, decider as DC
    monkeypatch.setattr(DC, "call_model", lambda *x, **k: (None, "offline"))
    spec = tmp_path / "spec.json"
    spec.write_text("{}")
    b = TP._board(tmp_path)
    a = TP._args(tmp_path, b, from_stage="critroute", to_stage="critroute", spec=str(spec))
    ran = []
    monkeypatch.setitem(PL.FUNCS, "critroute", lambda c, n, board: ran.append("critroute") or {"verdict": "pass", "line": "x", "board": board})
    assert PL.run(a) == 0 and ran == ["critroute"]                              # no stamp: written, stage ran
    sp = os.path.join(a.workdir, "kickoff.json")
    assert KO.load_stamp(sp)["inputs"]["spec"]
    spec.write_text('{"p": 1}')
    ready_json = os.path.splitext(b)[0] + ".ready.json"
    open(ready_json, "w").write(json.dumps({"checklist": [{"name": "parts gate", "status": "FAIL", "blockers": ["STOCK_UNCHECKED R1 x"]}]}))
    monkeypatch.setattr(PL, "st_ready", lambda c, n, board: {"verdict": "blocked", "line": "parts gate BLOCKED", "board": board})
    ran.clear()
    assert PL.run(a) == 0 and ran == ["critroute"]                              # only stock unchecked: WARN, the stage starts
