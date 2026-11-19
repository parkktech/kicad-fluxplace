"""gaps.py: classify, guard, rung dispatch and the close loop. Fakes only: no kicad-cli, no pcbnew boards."""
import hashlib, json, os, shutil, sys
from collections import Counter
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import types
from fluxplace import gaps as GP
from fluxplace import pipeline as PL
from fluxplace import calllog as CL

DRC = {
    "violations": [
        {"type": "shorting_items", "severity": "error", "description": "Short",
         "items": [{"description": "Track [GND] on F.Cu", "pos": {"x": 1, "y": 1}},
                   {"description": "Track [VCC] on F.Cu", "pos": {"x": 1.5, "y": 1}}]},
        {"type": "clearance", "severity": "error", "description": "Clearance",
         "items": [{"description": "Track [SIG1] on F.Cu", "pos": {"x": 2, "y": 2}}]},
        {"type": "track_dangling", "severity": "warning", "description": "Dangling",
         "items": [{"description": "Track [SIG2] on F.Cu", "pos": {"x": 3, "y": 3}}]},
        {"type": "track_not_centered_on_via", "severity": "warning", "description": "Off centre",
         "items": [{"description": "Track [SIG3] on B.Cu", "pos": {"x": 4, "y": 4}}]},
        {"type": "silk_over_copper", "severity": "warning", "description": "Silk",
         "items": [{"description": "Silk text", "pos": {"x": 5, "y": 5}}]},
    ],
    "unconnected_items": [
        {"description": "Missing connection", "items": [
            {"description": "Zone [GND] on F.Cu, priority 0", "pos": {"x": 0, "y": 0}},
            {"description": "Pad 1 [VCC]", "pos": {"x": 9, "y": 9}}]},
        {"description": "Missing connection", "items": [
            {"description": "Pad 1 [SIG1] of R1", "pos": {"x": 10, "y": 10}},
            {"description": "Track [SIG1]", "pos": {"x": 10.1, "y": 10}}]},
        {"description": "Missing connection", "items": [
            {"description": "Pad 2 [SIG2]", "pos": {"x": 20, "y": 20}},
            {"description": "Pad 3 [SIG2]", "pos": {"x": 25, "y": 20}}]},
    ],
}
FREE = DRC["unconnected_items"][2]
ALL_KINDS = {"short", "open-gap", "open-pour", "open-free", "clearance", "dangling", "via-offcentre", "other"}


def _mm(opens=0, shorts=0, errors=0, err_keys=None, gaps=None):
    return {"opens": opens, "shorts": shorts, "errors": errors, "err_keys": Counter(err_keys or []),
            "gaps": gaps or []}


def _g(gid, kind="open-free", cls="plain"):
    return {"id": gid, "kind": kind, "class": cls, "nets": [], "at": None, "layer": None, "evidence": ""}


def test_classify_kinds():
    gs = GP.classify(DRC, {})
    assert {g["kind"] for g in gs} == ALL_KINDS
    assert len(gs) == 8


def test_classify_ids_stable():
    a = [g["id"] for g in GP.classify(DRC, {})]
    b = [g["id"] for g in GP.classify(DRC, {})]
    assert a == b and len(set(a)) == len(a)
    dup = {"violations": [DRC["violations"][2], DRC["violations"][2]], "unconnected_items": []}
    ids = [g["id"] for g in GP.classify(dup, {})]
    assert ids[1] == ids[0] + "#2"


def test_classify_class():
    cons = {"rf": {"nets": ["SIG2"]}}
    by = {g["nets"][0]: g["class"] for g in GP.classify(DRC, cons) if g["nets"] == ["SIG2"]}
    assert by["SIG2"] == "rf"
    pair = {"violations": [{"type": "clearance", "severity": "error", "description": "c",
                            "items": [{"description": "Track [USB_DP] on F.Cu", "pos": {"x": 0, "y": 0}}]}],
            "unconnected_items": []}
    assert GP.classify(pair, {"pairs": {"USB_": {}}})[0]["class"] == "pair"
    assert GP.classify(pair, {})[0]["class"] == "plain"


def test_order():
    gs = GP.classify(DRC, {})
    rank = [GP.ORDER.index(g["kind"]) if g["kind"] in GP.ORDER else len(GP.ORDER) for g in gs]
    assert rank == sorted(rank)
    assert gs[-1]["kind"] == "other"


def test_dispatch_registered():
    assert set(GP.DISPATCH) <= set(GP.ORDER)
    for kind, rungs in GP.DISPATCH.items():
        for r in rungs:
            assert r in GP.RUNGS, r


@pytest.fixture
def guard(monkeypatch, tmp_path):
    monkeypatch.setattr(GP.audit, "snapshot", lambda *a, **k: {})
    diff = {"ok": True, "violations": []}
    monkeypatch.setattr(GP.audit, "connectivity_diff", lambda b, a, expect=(): diff)
    base = tmp_path / "base.kicad_pcb"
    base.write_text("base")
    cand = tmp_path / "cand.kicad_pcb"
    cand.write_text("cand")
    return {"diff": diff, "base": str(base), "cand": str(cand)}


def _accept(g, base_m, cand_m, targets=("a",), cand_gaps=None):
    return GP._accept(base_m, g["base"], cand_m, g["cand"], list(targets),
                      cand_gaps if cand_gaps is not None else cand_m["gaps"])


def test_accept_rules(guard):
    base_gaps = [_g("a"), _g("b")]
    base = _mm(opens=3, gaps=base_gaps)
    assert _accept(guard, base, _mm(opens=2, gaps=[_g("b")])) == (True, "")

    guard["diff"].update(ok=False, violations=["moved U1 by 1.2 mm"])
    assert _accept(guard, base, _mm(opens=2, gaps=[_g("b")]))[1] == "R0 moved U1 by 1.2 mm"
    guard["diff"].update(ok=True, violations=[])

    assert _accept(guard, base, _mm(opens=2, shorts=1, gaps=[_g("b")]))[1] == "R1 shorts up"
    base_s = _mm(opens=3, gaps=base_gaps, shorts=1)
    assert _accept(guard, base_s, _mm(opens=2, shorts=1, gaps=[_g("b")]))[0] is True

    cand = _mm(opens=2, err_keys=[("clearance", ("x",))], gaps=[_g("b")])
    assert _accept(guard, base, cand)[1] == "R2 new DRC key clearance"

    same = _mm(opens=3, gaps=base_gaps)
    assert _accept(guard, base, same)[1] == "R4 score not lower"

    still = _mm(opens=2, gaps=[_g("a"), _g("b")])
    assert _accept(guard, base, still)[1] == "target gap still there"


def _fake_measure(board, kicad_cli="kicad-cli"):
    fixed = "fixed" in open(board).read()
    d = {"violations": [], "unconnected_items": [] if fixed else [FREE]}
    return {"drc": d, "opens": len(d["unconnected_items"]), "shorts": 0, "errors": 0, "err_keys": Counter()}


def _fp(path):
    return hashlib.sha1(open(path, "rb").read()).hexdigest()


@pytest.fixture
def loop(monkeypatch, tmp_path, guard):
    monkeypatch.setattr(GP, "measure", _fake_measure)
    monkeypatch.setattr(GP.versions, "copper_fingerprint", _fp)
    board = tmp_path / "in.kicad_pcb"
    board.write_text("original")
    return {"board": str(board), "work": str(tmp_path / "work"), "attempts": str(tmp_path / "attempts.jsonl")}


def _rung(mode):
    def fn(base, out, gaps, ctx):
        if mode == "raise":
            raise RuntimeError("boom")
        if mode == "exit":
            raise SystemExit("bye")
        shutil.copy(base, out)
        if mode == "fix":
            open(out, "w").write("fixed")
        return {"mode": mode}
    return fn


def test_close_accepts_and_stops_clean(monkeypatch, loop):
    monkeypatch.setitem(GP.RUNGS, "patch", _rung("fix"))
    res = GP.close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    assert res["stop"] == "clean" and res["exit"] == 0
    assert open(res["best"]).read() == "fixed"
    assert res["end"]["opens"] == 0 and res["start"]["opens"] == 1


def test_close_flat_and_memo(monkeypatch, loop):
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, _rung("same"))
    res = GP.close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    assert res["stop"] == "flat" and res["exit"] == 2
    res2 = GP.close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    assert res2["stop"] == "flat"
    lines = [json.loads(x) for x in open(loop["attempts"])]
    k = len(GP.DISPATCH["open-free"])
    assert [x["result"] for x in lines[:k]] == ["refused"] * k
    assert [x["why"] for x in lines[:k]] == ["no change"] * k
    assert all(x["drc_runs"] == 0 for x in lines[:k])
    assert [x["why"] for x in lines[-k:]] == ["memo"] * k
    assert all(x["result"] == "skipped" for x in lines[-k:])


def test_attempt_line_schema(monkeypatch, loop):
    monkeypatch.setitem(GP.RUNGS, "patch", _rung("fix"))
    GP.close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    keys = {"ts", "run", "board_fp", "tool", "stage", "level", "gap", "method", "tier", "in", "result", "why",
            "score", "s", "drc_runs", "precheck", "ai_tokens"}
    lines = [json.loads(x) for x in open(loop["attempts"])]
    assert lines and all(set(x) == keys for x in lines)
    assert lines[0]["gap"]["kind"] == "open-free" and lines[0]["stage"] == "close"


def test_rung_error_caught(monkeypatch, loop):
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, _rung("raise"))
    res = GP.close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    lines = [json.loads(x) for x in open(loop["attempts"])]
    assert res["stop"] == "flat"
    assert {x["result"] for x in lines} == {"error"}
    assert {x["method"] for x in lines} == set(GP.DISPATCH["open-free"])
    assert lines[0]["why"] == "RuntimeError: boom"


def test_stop_json(monkeypatch, loop):
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, _rung("same"))
    res = GP.close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    stop = json.load(open(os.path.join(loop["work"], "stop.json")))
    assert {"stop", "exit", "level", "minutes", "start", "end", "attempts", "left"} <= set(stop)
    assert stop["attempts"] == len(GP.DISPATCH["open-free"]) and stop["level"] == "L0"
    assert len(stop["left"]) == 1
    left = stop["left"][0]
    assert left["kind"] == "open-free" and left["last_method"] == "patch_rip" and left["why"] == "no change"
    assert "| open-free x1 left |" in open(os.path.join(loop["work"], "ledger-rows.md")).read()
    assert res["best"] == os.path.join(loop["work"], "best.kicad_pcb")



def _count_measure(board, kicad_cli="kicad-cli"):
    n = int(open(board).read().split("=")[1])
    d = {"violations": [], "unconnected_items": [FREE] * n}
    return {"drc": d, "opens": n, "shorts": 0, "errors": 0, "err_keys": Counter()}


def _count_rung(base, out, gaps, ctx):
    n = int(open(base).read().split("=")[1])
    open(out, "w").write(f"n={n - 1}")
    return {"status": "ok"}


def test_rung_systemexit_is_error_and_loop_continues(monkeypatch, loop):
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, _rung("exit"))
    GP.close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    lines = [json.loads(x) for x in open(loop["attempts"])]
    assert {x["result"] for x in lines} == {"error"}
    assert lines[0]["why"] == "SystemExit: bye"
    assert len(lines) == len(GP.DISPATCH["open-free"])


def test_st_close_warns_when_close_raises(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("bad")
    monkeypatch.setattr(GP, "close", boom)
    board = tmp_path / "in.kicad_pcb"
    board.write_text("x")
    c = types.SimpleNamespace(dir=lambda n, name: str(tmp_path / "w"), cons_path=None, cons={},
                              board_in=str(board))
    res = PL.st_close(c, 1, str(board))
    assert res["verdict"] == "warn"
    assert res["line"] == "close: crashed RuntimeError: bad"
    assert res["board"] == str(board)


def test_close_budget_stop(monkeypatch, loop):
    monkeypatch.setitem(GP.RUNGS, "patch", _rung("fix"))
    res = GP.close(loop["board"], loop["work"], budget_min=1e-9, attempts=loop["attempts"], log=lambda m: None)
    assert res["stop"] == "budget" and res["exit"] == 2


def test_close_rounds_stop(monkeypatch, tmp_path, guard):
    monkeypatch.setattr(GP, "measure", _count_measure)
    monkeypatch.setattr(GP.versions, "copper_fingerprint", _fp)
    board = tmp_path / "in.kicad_pcb"
    board.write_text("n=2")
    monkeypatch.setitem(GP.RUNGS, "patch", _count_rung)
    res = GP.close(str(board), str(tmp_path / "w"), max_rounds=1, attempts=str(tmp_path / "a.jsonl"),
                   log=lambda m: None)
    assert res["stop"] == "rounds"
    assert res["end"]["opens"] == 1 and res["start"]["opens"] == 2


def test_memo_dropped_when_cons_changes(monkeypatch, loop, tmp_path):
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, _rung("same"))
    c1, c2 = tmp_path / "c1.toml", tmp_path / "c2.toml"
    c1.write_text("a = 1\n")
    c2.write_text("a = 2\n")
    cons = {"flow": {}}
    GP.close(loop["board"], loop["work"], cons_path=str(c1), cons=cons, attempts=loop["attempts"],
             log=lambda m: None)
    n1 = len(open(loop["attempts"]).readlines())
    GP.close(loop["board"], loop["work"], cons_path=str(c2), cons=cons, attempts=loop["attempts"],
             log=lambda m: None)
    lines = [json.loads(x) for x in open(loop["attempts"])][n1:]
    assert lines and all(x["why"] != "memo" for x in lines)
    n2 = len(open(loop["attempts"]).readlines())
    GP.close(loop["board"], loop["work"], cons_path=str(c2), cons=cons, attempts=loop["attempts"],
             log=lambda m: None)
    lines = [json.loads(x) for x in open(loop["attempts"])][n2:]
    assert lines and all(x["why"] == "memo" for x in lines)


CLEAR = {"type": "clearance", "severity": "warning", "description": "Clearance",
         "items": [{"description": "Track [SIG1] on F.Cu", "pos": {"x": 2, "y": 2}}]}


def _clear_measure(board, kicad_cli="kicad-cli"):
    fixed = "fixed" in open(board).read()
    d = {"violations": [CLEAR], "unconnected_items": [] if fixed else [FREE]}
    return {"drc": d, "opens": len(d["unconnected_items"]), "shorts": 0, "errors": 0, "err_keys": Counter()}


def test_next_kind_tried_after_accept(monkeypatch, tmp_path, guard):
    monkeypatch.setattr(GP, "measure", _clear_measure)
    monkeypatch.setattr(GP.versions, "copper_fingerprint", _fp)
    board = tmp_path / "in.kicad_pcb"
    board.write_text("original")
    monkeypatch.setitem(GP.RUNGS, "patch", _rung("fix"))
    monkeypatch.setitem(GP.RUNGS, "drcfix", _rung("same"))
    monkeypatch.setitem(GP.RUNGS, "region", _rung("same"))
    attempts = str(tmp_path / "a.jsonl")
    GP.close(str(board), str(tmp_path / "w"), attempts=attempts, log=lambda m: None)
    lines = [json.loads(x) for x in open(attempts)]
    assert lines[0]["method"] == "patch" and lines[0]["result"] == "closed"
    assert lines[1]["gap"]["kind"] == "clearance" and lines[1]["method"] == "drcfix"


def _fake_layers(monkeypatch, layers, lens):
    monkeypatch.setattr(GP, "copper_layers", lambda board: list(layers))
    monkeypatch.setattr(GP, "_track_len_by_layer", lambda board: dict(lens))


def test_signal_layers_drops_tiny_planes(monkeypatch):
    _fake_layers(monkeypatch, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                 {"F.Cu": 100.0, "B.Cu": 90.0, "In1.Cu": 0.5, "In2.Cu": 0.2})
    sig = GP.signal_layers("x")
    assert sig == ["F.Cu", "B.Cu"]
    planes = set(GP.copper_layers("x")) - set(sig)
    assert planes == {"In1.Cu", "In2.Cu"}


def test_signal_layers_all_tiny_returns_all(monkeypatch):
    _fake_layers(monkeypatch, ["F.Cu", "B.Cu"], {})
    assert GP.signal_layers("x") == ["F.Cu", "B.Cu"]
    # 25 equal layers: each carries 4 %, under the 5 % bar, so no layer passes
    layers = [f"L{i}" for i in range(25)]
    _fake_layers(monkeypatch, layers, {n: 1.0 for n in layers})
    assert GP.signal_layers("x") == layers


def test_accept_own_kind_key_passes_r2(guard):
    base = _mm(opens=3, gaps=[_g("a"), _g("b")])
    key = ("track_not_centered_on_via", ("Track [SIG3] on B.Cu",))
    cand = _mm(opens=2, err_keys=[key], gaps=[_g("b")])
    assert GP._accept(base, guard["base"], cand, guard["cand"], ["a"], cand["gaps"], "via-offcentre") == (True, "")
    ok, why = GP._accept(base, guard["base"], cand, guard["cand"], ["a"], cand["gaps"], "clearance")
    assert not ok and why == "R2 new DRC key track_not_centered_on_via"


def test_krt_not_found_no_board(monkeypatch, tmp_path):
    from fluxplace import routerloc as RL
    def missing(*a, **k):
        raise RuntimeError("no krt")
    monkeypatch.setattr(RL, "resolve", missing)
    out = tmp_path / "out.kicad_pcb"
    res = GP._r_krt(str(tmp_path / "in.kicad_pcb"), str(out), [_g("a")], {"cons": {}, "work": str(tmp_path),
                                                                          "budget_s": 60})
    assert res == {"status": "krt not found"}
    assert not out.exists()


def test_krt_command_carries_nets_and_layers(monkeypatch, tmp_path):
    import subprocess
    from fluxplace import routerloc as RL, routerules as RR, adaptive, batchroute as BR
    monkeypatch.setattr(RL, "resolve", lambda *a, **k: {"py": "/usr/bin/python3", "dir": str(tmp_path / "krt")})
    monkeypatch.setattr(RR, "rules", lambda cons, board=None, default=None, **k: {"track": 0.2})
    monkeypatch.setattr(RR, "size_args", lambda R: [])
    monkeypatch.setattr(RR, "flags", lambda R, d, w, script="route.py": [])
    monkeypatch.setattr(adaptive, "krt_extra", lambda d, s: [])
    monkeypatch.setattr(BR, "has_fanout", lambda src: False)
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    seen = {}
    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        open(cmd[cmd.index("--output") + 1], "w").write("routed")
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    gap = dict(_g("a"), nets=["NET[0]"])
    out = tmp_path / "out.kicad_pcb"
    res = GP._r_krt(str(tmp_path / "in.kicad_pcb"), str(out), [gap],
                    {"cons": {}, "work": str(tmp_path), "budget_s": 60})
    cmd = seen["cmd"]
    assert res == {"status": "ran", "rc": 0} and out.exists()
    assert cmd[cmd.index("--nets") + 1:] == ["NET[[]0]"]
    assert cmd.index("--layers") < cmd.index("F.Cu") < cmd.index("B.Cu") < cmd.index("--nets")
    assert seen["kw"]["cwd"] == str(tmp_path)


def test_krt_before_region_in_open_free():
    d = GP.DISPATCH["open-free"]
    assert d.index("krt") < d.index("region")
    assert GP.LEVEL["krt"] == "L0"


RUN_KEYS = {"ts", "run", "tool", "board", "board_fp", "stage", "variant", "start", "end", "stop", "minutes",
            "attempts", "closed", "methods_closed", "top_refusals", "full_pass", "ai_tokens"}


def test_runs_line_keys_and_full_pass(monkeypatch, loop, tmp_path):
    runs = str(tmp_path / "runs.jsonl")
    monkeypatch.setitem(GP.RUNGS, "patch", _rung("fix"))
    GP.close(loop["board"], loop["work"], attempts=loop["attempts"], runs=runs, log=lambda m: None)
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, _rung("same"))
    GP.close(loop["board"], str(tmp_path / "w2"), attempts=loop["attempts"], runs=runs, log=lambda m: None)
    lines = [json.loads(x) for x in open(runs)]
    assert len(lines) == 2
    assert set(lines[0]) == RUN_KEYS
    assert lines[0]["full_pass"] is True and lines[0]["stop"] == "clean" and lines[0]["closed"] == 1
    assert lines[0]["methods_closed"] == {"patch": 1} and lines[0]["board"] == "in.kicad_pcb"
    assert lines[1]["full_pass"] is False and lines[1]["stop"] == "flat"
    assert lines[1]["top_refusals"] == [["no change", len(GP.DISPATCH["open-free"])]]


def test_report_rows_and_skipped_excluded(tmp_path):
    def rec(method, kind, result, why="", s=1.0):
        return {"method": method, "gap": {"kind": kind}, "result": result, "why": why, "s": s}
    rows_in = [rec("patch", "open-free", "closed", "{}", 2.0),
               rec("patch", "open-free", "refused", "R4 score not lower", 1.0),
               rec("patch", "open-free", "refused", "R4 score not lower", 1.0),
               rec("patch", "open-free", "skipped", "memo", 0.0),
               rec("patch", "open-free", "skipped", "memo", 0.0),
               rec("krt", "open-free", "error", "RuntimeError: x", 3.0)]
    f = tmp_path / "attempts.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows_in))
    rows = GP.report([str(f)])
    assert [(r["method"], r["kind"]) for r in rows] == [("patch", "open-free"), ("krt", "open-free")]
    p = rows[0]
    assert (p["tries"], p["closed"], p["refused"], p["skipped"]) == (5, 1, 2, 2)
    assert p["close_pct"] == 33.3                       # 1 of 3 non-skipped tries
    assert p["top_why"] == "R4 score not lower"
    assert p["s_per_close"] == 4.0                      # 4 s of non-skipped tries / 1 close
    k = rows[1]
    assert k["close_pct"] == 0.0 and k["error"] == 1 and k["top_why"] == "RuntimeError: x"
    runs = tmp_path / "runs.jsonl"
    runs.write_text(json.dumps({"full_pass": True}) + "\n" + json.dumps({"full_pass": False}) + "\n")
    assert GP.runs_summary(str(runs)) == (2, 1)
