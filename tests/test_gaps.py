"""gaps.py: classify, guard, rung dispatch and the close loop. Fakes only: no kicad-cli, no pcbnew boards."""
from fluxplace import procs as _procs_mod
import hashlib, json, os, shutil, sys
from collections import Counter
import pytest
from fluxplace import refusal as refusal_mod
from fluxplace import closeexec as CX
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import types
from fluxplace import gaps as GP
from fluxplace import pipeline as PL
from fluxplace import calllog as CL
import functools
_close = functools.partial(GP.close, inproc=True)

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


@pytest.fixture(autouse=True)
def _no_prune(monkeypatch, request):
    """The loop tests below script the routing rungs; `prune` (first rung for open-free and dangling) is left out of the
    dispatch for them and has its own tests."""
    if "prune" in request.node.name:
        return
    for kind in ("open-free", "dangling"):
        monkeypatch.setitem(GP.DISPATCH, kind, [r for r in GP.DISPATCH[kind] if r != "prune"])


def test_prune_is_first_rung_for_open_free_and_dangling():
    assert GP.DISPATCH["open-free"][0] == "prune" and GP.DISPATCH["dangling"][0] == "prune"
    assert "prune" in GP.RUNGS


def test_prune_unreadable_board_removes_nothing(tmp_path):
    from fluxplace import launder as LA
    assert LA.prune_board(str(tmp_path / "missing.kicad_pcb"), str(tmp_path / "o.kicad_pcb")) == {"removed": 0, "nets": []}
    assert not (tmp_path / "o.kicad_pcb").exists()


def _rung(mode):
    def fn(base, out, gaps, ctx):
        if mode == "raise":
            raise RuntimeError("boom")
        if mode == "exit":
            raise SystemExit("bye")
        shutil.copy(base, out)
        if mode == "fix":
            open(out, "w").write("fixed")
        if mode == "other":
            open(out, "w").write("changed")
        return {"mode": mode}
    return fn


def test_close_accepts_and_stops_clean(monkeypatch, loop):
    monkeypatch.setitem(GP.RUNGS, "patch", _rung("fix"))
    res = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    assert res["stop"] == "clean" and res["exit"] == 0
    assert open(res["best"]).read() == "fixed"
    assert res["end"]["opens"] == 0 and res["start"]["opens"] == 1


def test_close_flat_and_memo(monkeypatch, loop):
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, _rung("same"))
    res = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    assert res["stop"] == "flat" and res["exit"] == 2
    res2 = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
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
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    keys = {"ts", "run", "board_fp", "tool", "stage", "level", "gap", "method", "tier", "in", "result", "why",
            "score", "cand", "s", "drc_runs", "precheck", "ai_tokens", "cleanup"}
    lines = [json.loads(x) for x in open(loop["attempts"])]
    assert lines and all(set(x) == keys for x in lines)
    assert lines[0]["gap"]["kind"] == "open-free" and lines[0]["stage"] == "close"


def test_rung_error_caught(monkeypatch, loop):
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, _rung("raise"))
    res = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    lines = [json.loads(x) for x in open(loop["attempts"])]
    assert res["stop"] == "flat"
    assert {x["result"] for x in lines} == {"error"}
    assert {x["method"] for x in lines} == set(GP.DISPATCH["open-free"])
    assert lines[0]["why"] == "RuntimeError: boom"


def test_stop_json(monkeypatch, loop):
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, _rung("same"))
    res = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    stop = json.load(open(os.path.join(loop["work"], "stop.json")))
    assert {"stop", "exit", "level", "minutes", "start", "end", "attempts", "left"} <= set(stop)
    assert stop["attempts"] == len(GP.DISPATCH["open-free"]) and stop["level"] == "L0"
    assert len(stop["left"]) == 1
    left = stop["left"][0]
    assert left["kind"] == "open-free" and left["last_method"] == "ai" and left["why"] == "no change"
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
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    lines = [json.loads(x) for x in open(loop["attempts"])]
    assert {x["result"] for x in lines} == {"error"}
    assert lines[0]["why"] == "SystemExit: bye"
    assert len(lines) == len(GP.DISPATCH["open-free"])


def test_st_close_warns_when_close_raises(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("bad")
    monkeypatch.setattr(PL, "in_fresh_process", boom)           # close runs in a fresh process (pcbnew bindings decay otherwise)
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
    res = _close(loop["board"], loop["work"], budget_min=1e-9, attempts=loop["attempts"], log=lambda m: None)
    assert res["stop"] == "budget" and res["exit"] == 2


def test_close_rounds_stop(monkeypatch, tmp_path, guard):
    monkeypatch.setattr(GP, "measure", _count_measure)
    monkeypatch.setattr(GP.versions, "copper_fingerprint", _fp)
    board = tmp_path / "in.kicad_pcb"
    board.write_text("n=2")
    monkeypatch.setitem(GP.RUNGS, "patch", _count_rung)
    res = _close(str(board), str(tmp_path / "w"), max_rounds=1, attempts=str(tmp_path / "a.jsonl"),
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
    _close(loop["board"], loop["work"], cons_path=str(c1), cons=cons, attempts=loop["attempts"],
             log=lambda m: None)
    n1 = len(open(loop["attempts"]).readlines())
    _close(loop["board"], loop["work"], cons_path=str(c2), cons=cons, attempts=loop["attempts"],
             log=lambda m: None)
    lines = [json.loads(x) for x in open(loop["attempts"])][n1:]
    assert lines and all(x["why"] != "memo" for x in lines)
    n2 = len(open(loop["attempts"]).readlines())
    _close(loop["board"], loop["work"], cons_path=str(c2), cons=cons, attempts=loop["attempts"],
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
    _close(str(board), str(tmp_path / "w"), attempts=attempts, log=lambda m: None)
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
    monkeypatch.setattr(_procs_mod, "run", fake_run)
    opens = iter([{"NET[0]"}, set()])                                  # open before the call, closed after it
    monkeypatch.setattr(GP, "_krt_open", lambda b, cp: next(opens))
    monkeypatch.setattr(GP, "_krt_rip", lambda b, net, cp, skip: ["BLOCK[1]", "OTHER"])
    gap = dict(_g("a"), nets=["NET[0]"])
    out = tmp_path / "out.kicad_pcb"
    res = GP._r_krt(str(tmp_path / "in.kicad_pcb"), str(out), [gap],
                    {"cons": {}, "work": str(tmp_path), "budget_s": 60})
    cmd = seen["cmd"]
    assert res == {"status": "ran", "rc": 0, "failed": 0, "open": 0, "closed": 1, "ripped": 0} and out.exists()
    assert cmd[cmd.index("--nets") + 1:] == ["NET[[]0]"]
    i = cmd.index("--rip-existing-nets")                               # the window nets are KRT's rip candidates
    assert cmd[i + 1:i + 3] == ["BLOCK[[]1]", "OTHER"] and i < cmd.index("--nets")
    assert cmd.index("--layers") < cmd.index("F.Cu") < cmd.index("B.Cu") < cmd.index("--nets")
    assert seen["kw"]["cwd"] == str(tmp_path)


def test_krt_keeps_previous_board_on_a_trade_and_skips_pair_legs(monkeypatch, tmp_path):
    import subprocess
    from fluxplace import routerloc as RL, routerules as RR, adaptive, batchroute as BR
    monkeypatch.setattr(RL, "resolve", lambda *a, **k: {"py": "/usr/bin/python3", "dir": str(tmp_path / "krt")})
    monkeypatch.setattr(RR, "rules", lambda cons, board=None, default=None, **k: {"track": 0.2})
    monkeypatch.setattr(RR, "size_args", lambda R: [])
    monkeypatch.setattr(RR, "flags", lambda R, d, w, script="route.py": [])
    monkeypatch.setattr(adaptive, "krt_extra", lambda d, s: [])
    monkeypatch.setattr(BR, "has_fanout", lambda src: False)
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd[cmd.index("--nets") + 1:])
        open(cmd[cmd.index("--output") + 1], "w").write("routed")
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(_procs_mod, "run", fake_run)
    opens = iter([{"A", "P_N"}, {"P_N", "LOST"}])                      # A closes, another net opens: a trade
    monkeypatch.setattr(GP, "_krt_open", lambda b, cp: next(opens))
    monkeypatch.setattr(GP, "_krt_rip", lambda b, net, cp, skip: [])
    out = tmp_path / "out.kicad_pcb"
    gaps = [dict(_g("a"), nets=["P_N"]), dict(_g("b"), nets=["A"])]
    ctx = {"cons": {}, "work": str(tmp_path), "budget_s": 60, "protect": {"P_N"}}
    res = GP._r_krt(str(tmp_path / "in.kicad_pcb"), str(out), gaps, ctx)
    assert calls == [["A"]]                                            # the pair leg is never routed alone
    assert res["closed"] == 0 and not out.exists()
    assert GP._r_krt(str(tmp_path / "in.kicad_pcb"), str(out), gaps[:1], ctx) == {"status": "no plain net"}


def test_krt_before_region_in_open_free():
    d = GP.DISPATCH["open-free"]
    assert d.index("krt") < d.index("region")
    assert GP.LEVEL["krt"] == "L0"


RUN_KEYS = {"ts", "run", "tool", "board", "board_fp", "stage", "variant", "start", "end", "stop", "minutes",
            "attempts", "closed", "methods_closed", "top_refusals", "full_pass", "ai_tokens"}


def test_runs_line_keys_and_full_pass(monkeypatch, loop, tmp_path):
    runs = str(tmp_path / "runs.jsonl")
    monkeypatch.setitem(GP.RUNGS, "patch", _rung("fix"))
    _close(loop["board"], loop["work"], attempts=loop["attempts"], runs=runs, log=lambda m: None)
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, _rung("same"))
    _close(loop["board"], str(tmp_path / "w2"), attempts=loop["attempts"], runs=runs, log=lambda m: None)
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


# ---- out-of-process rungs (_call_rung) ----

def _cp(rc):
    return types.SimpleNamespace(returncode=rc, stdout="", stderr="")


def _rung_ctx(tmp_path, **kw):
    c = {"cons": {}, "cons_path": None, "kicad_cli": "kicad-cli", "work": str(tmp_path), "budget_s": 60,
         "ai_mode": "packet", "ai_dir": str(tmp_path / "ai"), "log": lambda m: None}
    c.update(kw)
    return c


def test_call_rung_signal_crash_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(_procs_mod, "run", lambda *a, **k: _cp(-11))
    out = str(tmp_path / "o.kicad_pcb")
    with pytest.raises(RuntimeError, match="signal 11"):
        GP._call_rung("patch", str(tmp_path / "in.kicad_pcb"), out, [_g("a")], _rung_ctx(tmp_path))
    assert not os.path.exists(out + ".job.json")


def test_call_rung_exit_and_timeout(monkeypatch, tmp_path):
    import subprocess
    out = str(tmp_path / "o.kicad_pcb")
    monkeypatch.setattr(_procs_mod, "run", lambda *a, **k: _cp(3))
    with pytest.raises(RuntimeError, match="rung exit 3"):
        GP._call_rung("patch", "in", out, [], _rung_ctx(tmp_path))

    def slow(*a, **k):
        assert k["timeout"] == 60 + CX.KILL_SLACK_S          # B6a: bounded slack, no more +120 s
        raise subprocess.TimeoutExpired(cmd="x", timeout=k["timeout"])
    monkeypatch.setattr(_procs_mod, "run", slow)
    with pytest.raises(RuntimeError, match="rung timeout"):
        GP._call_rung("patch", "in", out, [], _rung_ctx(tmp_path))


def test_call_rung_child_round_trip_returns_krt_last(monkeypatch, tmp_path):
    """The child entry (--rung-job) runs the rung from its job file; the parent gets the summary and krt_last."""
    def fake_rung(base, out, gaps, ctx):
        open(out, "w").write("child wrote")
        ctx["krt_last"] = str(tmp_path / "k.krt.json")
        return {"status": "ok", "n": len(gaps)}
    monkeypatch.setitem(GP.RUNGS, "fakechild", fake_rung)

    def run_child(cmd, **k):
        assert cmd[1:] == ["-m", "fluxplace.gaps", "--rung-job", cmd[-1]]
        assert k["cwd"] == GP._repo_root()
        assert GP._main(["--rung-job", cmd[-1]]) == 0
        return _cp(0)
    monkeypatch.setattr(_procs_mod, "run", run_child)
    ctx = _rung_ctx(tmp_path)
    out = str(tmp_path / "o.kicad_pcb")
    res = GP._call_rung("fakechild", str(tmp_path / "in.kicad_pcb"), out, [_g("a"), _g("b")], ctx)
    assert res == {"status": "ok", "n": 2} and open(out).read() == "child wrote"
    assert ctx["krt_last"] == str(tmp_path / "k.krt.json")
    assert not os.path.exists(out + ".job.json") and not os.path.exists(out + ".job.out.json")


def test_close_records_crashed_rung_as_error(monkeypatch, loop):
    """A crashed rung is an attempt with result error; the loop goes on with the other rungs."""
    def crash(name, base, out, gaps, ctx):
        raise RuntimeError("rung crashed (signal 11)")
    monkeypatch.setattr(GP, "_call_rung", crash)
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    rows = [json.loads(x) for x in open(loop["attempts"])]
    assert rows and all(r["result"] == "error" for r in rows[:5])
    assert "signal 11" in rows[0]["why"]


# ---- R2 cleanup (snapiter / launder on the candidate) ----

def _cl_measure(board, kicad_cli="kicad-cli"):
    s = open(board).read()
    unc = [FREE] if "open" in s else []
    keys = Counter()
    if "offc" in s:
        keys[("track_not_centered_on_via", "k")] += 1
    if "clr" in s:
        keys[("clearance", "k")] += 1
    return {"drc": {"violations": [], "unconnected_items": unc}, "opens": len(unc), "shorts": 0, "errors": 0,
            "err_keys": keys, "keys": keys}


def _spy(name, calls, write):
    def fn(base, out, gaps, ctx):
        calls.append(name)
        open(out, "w").write(write)
        return {"status": "ok"}
    return fn


def _cleanup_setup(monkeypatch, loop, patch_write):
    monkeypatch.setattr(GP, "measure", _cl_measure)
    open(loop["board"], "w").write("open")
    calls = []
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, _rung("same"))
    monkeypatch.setitem(GP.RUNGS, "patch", _spy("patch", calls, patch_write))
    monkeypatch.setitem(GP.RUNGS, "snapiter", _spy("snapiter", calls, "fixed"))
    monkeypatch.setitem(GP.RUNGS, "launder", _spy("launder", calls, "fixed"))
    return calls


def test_r2_offcentre_cleaned_by_snapiter_then_accepted(monkeypatch, loop):
    calls = _cleanup_setup(monkeypatch, loop, "fixed offc")
    res = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    rows = [json.loads(x) for x in open(loop["attempts"])]
    assert rows[0]["method"] == "patch" and rows[0]["result"] == "closed"
    assert rows[0]["cleanup"] == ["snapiter"] and calls == ["patch", "snapiter"]
    assert rows[0]["drc_runs"] == 2 and res["stop"] == "clean"


def test_r2_clearance_no_cleanup(monkeypatch, loop):
    calls = _cleanup_setup(monkeypatch, loop, "fixed clr")
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    row = json.loads(open(loop["attempts"]).readline())
    assert row["result"] == "refused" and row["why"].startswith("R2 new DRC key clearance")
    assert row["cleanup"] == [] and calls == ["patch"]


def test_r4_no_cleanup(monkeypatch, loop):
    calls = _cleanup_setup(monkeypatch, loop, "open x")
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    row = json.loads(open(loop["attempts"]).readline())
    assert row["result"] == "refused" and row["why"].startswith("R4 ")
    assert row["cleanup"] == [] and calls == ["patch"]


def test_cleanup_rung_writing_nothing_keeps_file(monkeypatch, loop):
    calls = _cleanup_setup(monkeypatch, loop, "fixed offc")
    monkeypatch.setitem(GP.RUNGS, "snapiter", lambda b, o, g, c: calls.append("snapiter-nothing") or {})
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    row = json.loads(open(loop["attempts"]).readline())
    assert row["cleanup"] == ["snapiter"] and row["result"] == "refused" and row["why"].startswith("R2 ")


# ---- krt_region: rip the window, KRT re-route (fakes only) ----

def _region_fakes(monkeypatch, tmp_path, first_margin_fixes):
    from fluxplace import handroute as HR
    base = tmp_path / "base.kicad_pcb"
    base.write_text("open")
    margins, rips, krt = [], [], []

    def open_nets(path, constraints=None, **k):
        text = open(path).read() if os.path.exists(path) else ""
        return {"connections": [{"net": "G1"}] if "open" in text else []}

    def window(board, conn=None, constraints=None, margin=1.5, **k):
        margins.append(margin)
        return {"window": [0, 0, 1, 1], "layers": ["F.Cu"],
                "tracks": [{"net": "X", "locked": False}, {"net": "P1", "locked": False},
                           {"net": "Y", "locked": True}, {"net": "G1", "locked": False}],
                "vias": [{"net": "Z", "locked": False}]}

    def rip(src, dst, box, nets):
        rips.append((box, sorted(nets)))
        open(dst, "w").write(open(src).read() + " ripped")
        return 1

    def krt_cmd(board, out, nets, ctx, layers=None):
        krt.append((sorted(nets), layers))
        return ["krt", out]

    def run(cmd, **k):
        ok = len(krt) == 1 and first_margin_fixes
        open(cmd[1], "w").write("fixed" if ok or len(krt) > 1 else "open")
        return _cp(0)
    monkeypatch.setattr(HR, "open_nets", open_nets)
    monkeypatch.setattr(HR, "window", window)
    monkeypatch.setattr(GP, "_rip_window", rip)
    monkeypatch.setattr(GP, "_krt_cmd", krt_cmd)
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    monkeypatch.setattr(_procs_mod, "run", run)
    ctx = {"cons": {}, "cons_path": None, "work": str(tmp_path), "budget_s": 60, "protect": {"P1"},
           "kicad_cli": "kicad-cli"}
    return base, ctx, margins, rips, krt


def test_krt_region_excludes_protected_locked_and_gap_nets(monkeypatch, tmp_path):
    base, ctx, margins, rips, krt = _region_fakes(monkeypatch, tmp_path, first_margin_fixes=True)
    out = tmp_path / "out.kicad_pcb"
    res = GP._r_krt_region(str(base), str(out), [dict(_g("a"), nets=["G1"])], ctx)
    assert res == {"status": "ran", "nets": 2, "margin": 1.5}
    assert margins == [1.5]
    assert rips == [([0, 0, 1, 1], ["G1", "X", "Z"])]           # P1 protected, Y locked, G1 is the gap net itself
    assert krt == [(["G1", "X", "Z"], ["F.Cu", "B.Cu"])]
    assert open(out).read() == "fixed"
    assert not [p for p in os.listdir(tmp_path) if ".rip." in p]


def test_krt_region_tries_three_mm_when_gap_stays_open(monkeypatch, tmp_path):
    base, ctx, margins, rips, krt = _region_fakes(monkeypatch, tmp_path, first_margin_fixes=False)
    out = tmp_path / "out.kicad_pcb"
    res = GP._r_krt_region(str(base), str(out), [dict(_g("a"), nets=["G1"])], ctx)
    assert res == {"status": "ran", "nets": 2, "margin": 3.0}
    assert margins == [1.5, 3.0] and len(krt) == 2 and len(rips) == 2
    assert open(out).read() == "fixed"


def test_krt_region_no_connection_no_run(monkeypatch, tmp_path):
    base, ctx, margins, rips, krt = _region_fakes(monkeypatch, tmp_path, first_margin_fixes=True)
    res = GP._r_krt_region(str(base), str(tmp_path / "o.kicad_pcb"), [dict(_g("a"), nets=["NOPE"])], ctx)
    assert res == {"status": "no conn"} and krt == [] and rips == []


def test_krt_region_rung_registered_after_krt():
    d = GP.DISPATCH["open-free"]
    assert d.index("krt") < d.index("krt_region") < d.index("region")
    assert GP.LEVEL["krt_region"] == "L1" and "krt_region" in GP.RUNGS


def test_classify_mask_opening_bridge():
    v = {"type": "solder_mask_bridge", "severity": "error", "description": "Solder mask bridge",
         "items": [{"description": "Polygon on F.Mask", "pos": {"x": 1, "y": 1}},
                   {"description": "Track [SIG_A] on F.Cu", "pos": {"x": 2, "y": 2}}]}
    gs = GP.classify({"violations": [v], "unconnected_items": []}, {})
    assert gs[0]["kind"] == "mask-opening" and gs[0]["id"].startswith("mask-opening:")
    assert gs[0]["evidence"] == GP.MASK_OPENING_EVIDENCE and GP.DISPATCH["mask-opening"] == ["maskcut"]
    assert GP.ORDER.index("mask-opening") == GP.ORDER.index("open-free") + 1     # PHASE3I B6: after copper kinds
    plain = dict(v, items=[{"description": "Track [SIG_A] on F.Cu", "pos": {"x": 2, "y": 2}}] * 2)
    assert GP.classify({"violations": [plain], "unconnected_items": []}, {})[0]["kind"] == "short"


# ---- PHASE3A (D71cj a): review findings as gap kinds, score terms, protect only audit-complete pairs ----
from fluxplace import power as _PW, boardfile as _BF, preroute as _PR, routediag as _RD, qualaudit as _QA


def test_score_counts_new_kinds():
    gaps = [{"kind": "power-width", "class": "power", "mag": 2}, {"kind": "power-vias", "class": "power", "mag": 1},
            {"kind": "pair-open", "class": "hs-pair", "sev": 3}, {"kind": "pair-skew", "class": "pair", "sev": 1},
            {"kind": "pin-escape", "class": "plain"}, {"kind": "short", "class": "plain"}]
    m = {"shorts": 1, "opens": 2, "errors": 3}
    # [hs_pair sev, rf, shorts, opens, power count, power mag, pair sev, pin_escape, errors, quality]
    assert GP.score(m, gaps) == [3, 0, 1, 2, 2, 3, 1, 1, 3, 0]


def test_pair_gated_strips_trailing_digits():
    from fluxplace import review as RV
    assert RV.pair_gated({}, "USB3_M2_TX_P") and RV.pair_gated({}, "PCIE2_RX_P")
    assert not RV.pair_gated({}, "SENSE_ADC_P")


def test_classify_reports_failed_review_sources(monkeypatch):
    monkeypatch.setattr(GP, "review_gaps", lambda board, cons, opn=None: ([], ["pairs"]))
    out = []
    GP.classify(DRC, {}, board="b.kicad_pcb", failed_out=out)
    assert out == ["pairs"]


def test_accept_review_failure_and_sev_drop_target(monkeypatch):
    calls = []
    monkeypatch.setattr(GP.drcguard, "accept", lambda base, cand, mode, target, *a, **k: calls.append(list(target)) or (True, ""))
    z = {"shorts": 0, "opens": 0, "errors": 0}
    base = dict(z, gaps=[{"id": "pair:X_P/X_N", "sev": 3, "nets": ["X_P", "X_N"], "kind": "pair-open", "class": "pair"}],
                review_failed=[])
    assert GP._accept(base, "b", dict(z, review_failed=["pairs"]), "c", ["pair:X_P/X_N"], [], "pair-open") == \
        (False, "review source failed: pairs")
    lower = [{"id": "pair:X_P/X_N", "sev": 1, "nets": ["X_P", "X_N"], "kind": "pair-skew", "class": "pair"}]
    assert GP._accept(base, "b", dict(z, review_failed=[]), "c", ["pair:X_P/X_N"], lower, "pair-open") == (True, "")
    assert calls[-1] == []                      # sev dropped 3 -> 1: the target no longer has to disappear


def test_classify_without_board_adds_no_review_gaps(monkeypatch):
    monkeypatch.setattr(GP, "review_gaps", lambda *a, **k: pytest.fail("review_gaps called without board"))
    assert GP.classify(DRC, {}) == GP.classify(DRC, {}, board=None)


def test_review_gaps_kinds_classes_and_skips(monkeypatch):
    rows = [
        {"pair": "USB_D_P/USB_D_N", "p": "USB_D_P", "n": "USB_D_N", "status": "half", "causes": [], "why": "one leg open"},
        {"pair": "Y_P/Y_N", "p": "Y_P", "n": "Y_N", "status": "mismatch", "causes": ["skew"], "why": "skew 2 mm"},
        {"pair": "Z_P/Z_N", "p": "Z_P", "n": "Z_N", "status": "unknown", "causes": [], "why": "?"},
        {"pair": "SENSE_ADC_P/SENSE_ADC_N", "p": "SENSE_ADC_P", "n": "SENSE_ADC_N", "status": "open", "causes": [], "why": "x"},
    ]
    monkeypatch.setattr(GP, "_pair_audit", lambda board, cons, opn=None: (None, {}, rows))
    monkeypatch.setattr(_BF, "facts_from_file", lambda board: {})
    monkeypatch.setattr(_PW, "check", lambda facts, cons, dc=None, **k: [
        {"code": "POWER_WIDTH", "refs": ["VBAT"], "msg": "VBAT narrow"},
        {"code": "POWER_CURRENT_UNKNOWN", "refs": ["X"], "msg": "warn"}])
    monkeypatch.setattr(_RD, "load", lambda board: None)
    monkeypatch.setattr(_QA, "_ctx", lambda *a, **k: ([], set(), None))
    monkeypatch.setattr(_QA, "rf_rows", lambda *a, **k: [])
    monkeypatch.setattr(_PR, "pin_escape_blocked", lambda B, **k: [{"code": "PIN_ESCAPE_BLOCKED", "refs": ["U3"], "msg": "U3 blocked"}])
    cons = {"not_pairs": ["SENSE_ADC_P", "SENSE_ADC_N"]}
    rows_out, failed = GP.review_gaps("b.kicad_pcb", cons)
    got = {(g["kind"], g["class"], g["evidence"]) for g in rows_out}
    assert got == {("pair-open", "hs-pair", "one leg open"), ("pair-skew", "pair", "skew 2 mm"), ("pair-unmeasured", "pair", "?"),
                   ("power-width", "power", "VBAT narrow"), ("pin-escape", "plain", "U3 blocked")}       # an unknown row is a gap (D71cx)
    assert failed == [] and {g["base"] for g in rows_out if g["kind"].startswith("pair")} == {
        "pair:USB_D_P/USB_D_N", "pair:Y_P/Y_N", "pair:Z_P/Z_N"}


def test_rf_net_at_signal_width_on_inner_layer_is_an_rf_impedance_gap_and_the_gate_is_not_clean(monkeypatch):
    from test_review import STACK6
    monkeypatch.setattr(_BF, "facts_from_file", lambda board: {})
    monkeypatch.setattr(_PW, "check", lambda *a, **k: [])
    monkeypatch.setattr(GP, "_pair_audit", lambda board, cons, opn=None: (None, {}, []))
    monkeypatch.setattr(_PR, "pin_escape_blocked", lambda B, **k: [])
    monkeypatch.setattr(_RD, "load", lambda board: {"pads": [{"net": "RF_X"}], "zones": []})
    monkeypatch.setattr(_QA, "_ctx", lambda *a, **k: (STACK6, {"In1.Cu", "In4.Cu"}, None))
    monkeypatch.setattr(_QA, "tracks_of", lambda B, net: {"length": 30.0, "vias": 0, "layers": {"In2.Cu": 30.0},
                                                          "segments": [("In2.Cu", 0.15, 30.0)]})
    rows_out, failed = GP.review_gaps("b.kicad_pcb", {"rf": {"nets": ["RF_X"]}})
    assert failed == [] and [(g["kind"], g["class"], g["nets"]) for g in rows_out] == [("rf-impedance", "rf", ["RF_X"])]
    assert "rf-impedance" in GP.gate_kinds({}, "route") and "net-contract" in GP.gate_kinds({}, "route")
    assert GP.in_scope(rows_out[0], GP.gate_kinds({}, "route")) and GP.DISPATCH["rf-impedance"] == ["rfroute"]
    assert GP.deferred_left(rows_out, ("tune", "route", "close")) is None      # no later stage owns it: the gate stays closed


def test_impedance_off_nets_is_unknown_not_empty_on_exception(monkeypatch):
    monkeypatch.setattr(_BF, "facts_from_file", lambda board: (_ for _ in ()).throw(RuntimeError("boom")))
    assert GP.impedance_off_nets("b.kicad_pcb", {}) is None


def test_review_gaps_isolates_failing_source(monkeypatch):
    monkeypatch.setattr(_BF, "facts_from_file", lambda board: {})
    monkeypatch.setattr(_PW, "check", lambda facts, cons: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(GP, "_pair_audit", lambda board, cons, opn=None: (None, {}, []))
    monkeypatch.setattr(_RD, "load", lambda board: None)
    monkeypatch.setattr(_QA, "_ctx", lambda *a, **k: ([], set(), None))
    monkeypatch.setattr(_QA, "rf_rows", lambda *a, **k: [])
    monkeypatch.setattr(_PR, "pin_escape_blocked", lambda B, **k: [{"code": "PIN_ESCAPE_BLOCKED", "refs": ["U9"], "msg": "m"}])
    rows_out, failed = GP.review_gaps("b.kicad_pcb", {})
    assert [g["kind"] for g in rows_out] == ["pin-escape"] and failed == ["power"]


def test_relax_nets_are_incomplete_pair_legs_and_only_they_are_relaxed(monkeypatch):
    gaps = [{"kind": "pair-skew", "class": "pair", "nets": ["Y_P", "Y_N"]}]
    assert GP.relax_nets(gaps) == {"Y_P", "Y_N"}
    prot = {"X_P", "X_N", "Y_P", "Y_N", "RF1"}
    # a pair-kind try relaxes only its own target legs; a plain try keeps every protected net
    assert GP._r5_set("pair-skew", [{"nets": ["Y_P", "Y_N"]}], prot, {"Y_P", "Y_N"}) == {"X_P", "X_N", "RF1"}
    assert GP._r5_set("open-gap", [{"nets": ["Y_P", "Y_N"]}], prot, {"Y_P", "Y_N"}) == prot


def test_score_terms_named_and_match_score():
    m = _mm(opens=2, gaps=[_g("a", kind="power-width"), _g("b", kind="pair-skew", cls="hs-pair")])
    m["gaps"][1]["sev"] = 1
    m["gaps"][0]["mag"] = 0.25
    t = GP.score_terms(m, m["gaps"])
    assert list(t) == ["hs_pair", "rf", "shorts", "opens", "power", "power_mag", "pair", "pin_escape", "errors", "quality"]
    assert GP.score(m, m["gaps"]) == list(t.values())
    assert t["power"] == 1 and t["power_mag"] == 0.25 and t["hs_pair"] == 1 and t["opens"] == 2


def test_r6_refuses_power_worse_even_with_fewer_opens(guard):
    base = _mm(opens=3, gaps=[_g("a"), _g("b")])
    power = dict(_g("p", kind="power-width"), mag=0.1)
    ok, why = _accept(guard, base, _mm(opens=2, gaps=[_g("b"), power]))
    assert ok is False and why.startswith("R6 power worse 0->1")


def test_r6_refuses_rf_and_passes_equal_terms(guard):
    base = _mm(opens=3, gaps=[_g("a"), _g("b")])
    rf = dict(_g("r", cls="rf"))
    assert _accept(guard, base, _mm(opens=2, gaps=[_g("b"), rf]))[1].startswith("R6 rf worse")
    assert _accept(guard, base, _mm(opens=2, gaps=[_g("b")])) == (True, "")


def test_refill_runs_before_every_measure(monkeypatch, loop):
    import fluxplace.opengate as OG
    events = []
    monkeypatch.setattr(OG, "refill", lambda p, out=None: events.append("refill:" + p) or 0)

    def rec_measure(board, kicad_cli="kicad-cli"):
        events.append("measure:" + board)
        return _fake_measure(board, kicad_cli)
    monkeypatch.setattr(GP, "measure", rec_measure)
    monkeypatch.setitem(GP.RUNGS, "patch", _rung("fix"))
    res = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    assert res["stop"] == "clean"
    measures = [i for i, e in enumerate(events) if e.startswith("measure:")]
    assert len(measures) >= 2
    for i in measures:
        assert events[i - 1] == "refill:" + events[i].split(":", 1)[1]


def test_refill_failure_refuses_candidate_never_accepts(monkeypatch, loop):
    import fluxplace.opengate as OG

    def refill(p, out=None):
        if "close" in p:
            raise RuntimeError("fill helper died")
        return 0
    monkeypatch.setattr(OG, "refill", refill)
    monkeypatch.setitem(GP.RUNGS, "patch", _rung("fix"))
    res = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    assert res["stop"] != "clean" and res["end"]["opens"] == res["start"]["opens"] == 1
    lines = [json.loads(x) for x in open(loop["attempts"])]
    assert any(x["result"] == "refused" and x["why"].startswith("refill failed") for x in lines)
    assert not any(x["result"] in ("closed", "partial") for x in lines)


def test_no_gain_rounds_stop(monkeypatch, loop):
    def counting(base, out, gaps, ctx):
        shutil.copy(base, out)
        with open(out, "a") as fh:
            fh.write("#%d" % counting.n)
        counting.n += 1
        return {"mode": "count"}
    counting.n = 0
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, counting)
    monkeypatch.setattr(GP, "_accept", lambda *a, **k: (True, ""))      # accepted every round, score never drops
    res = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    assert res["stop"] == "no gain 5 rounds" and res["attempts"] == GP.NO_GAIN_ROUNDS == 5


def test_no_gain_rounds_overridable_by_flow(monkeypatch, loop):
    def counting(base, out, gaps, ctx):
        shutil.copy(base, out)
        with open(out, "a") as fh:
            fh.write("#%d" % counting.n)
        counting.n += 1
        return {"mode": "count"}
    counting.n = 0
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, counting)
    monkeypatch.setattr(GP, "_accept", lambda *a, **k: (True, ""))
    cons = {"flow": {"no_gain_rounds": 2}}
    res = _close(loop["board"], loop["work"], cons=cons, attempts=loop["attempts"], log=lambda m: None)
    assert res["stop"] == "no gain 2 rounds" and res["attempts"] == 2


def test_ctx_protect_round_trips_job_json(monkeypatch, tmp_path):
    """PHASE3E 1: the protect set is written sorted and rebuilt as a set in the child rung."""
    seen = {}

    def fake_run(argv, **kw):
        job = argv[-1]
        seen["job"] = json.load(open(job))
        out = seen["job"]["out"]
        json.dump({}, open(out + ".job.out.json", "w"))
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(_procs_mod, "run", fake_run)
    ctx = {"budget_s": 60, "protect": {"A_P", "A_N"}, "cons_path": None, "kicad_cli": "kicad-cli"}
    GP._call_rung("krt", str(tmp_path / "b.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [], ctx)
    assert seen["job"]["ctx"]["protect"] == ["A_N", "A_P"]
    got = {}
    monkeypatch.setitem(GP.RUNGS, "krt", lambda base, out, gaps, c: got.update(protect=c["protect"]) or {})
    job = tmp_path / "j.job.json"
    json.dump(seen["job"], open(job, "w"))
    GP._rung_job(str(job))
    assert got["protect"] == {"A_P", "A_N"} and isinstance(got["protect"], set)


def test_pairgroup_rung_budget_is_per_pair_capped_by_close_budget(monkeypatch, tmp_path):
    """PHASE3E 7: budget = PER_PAIR_S x pairs, never more than the close budget left."""
    from fluxplace import pairsonly as PO, routediag as RD, netclass as NC, pairaudit as PA
    monkeypatch.setattr(RD, "load", lambda p: None)
    monkeypatch.setattr(NC, "classify", lambda *a, **k: None)
    monkeypatch.setattr(PA, "pair_list", lambda _c: [("A_P", "A_N"), ("B_P", "B_N")])
    seen = []
    monkeypatch.setattr(PO, "regroup", lambda base, out, cons, spec, pairs, log=None, budget_s=None:
                        seen.append(budget_s) or {"groups": {}, "failed": []})
    gaps = [{"kind": "pair-skew", "nets": ["A_P", "A_N", "B_P", "B_N"]}]
    ctx = {"cons": {}, "log": lambda m: None, "budget_s": 10000.0}
    GP._r_pairgroup(str(tmp_path / "b"), str(tmp_path / "o"), gaps, ctx)
    ctx["budget_s"] = 50.0
    GP._r_pairgroup(str(tmp_path / "b"), str(tmp_path / "o"), gaps, ctx)
    assert seen == [2 * PO.PER_PAIR_S, 50.0]


def test_no_gain_counts_quality_only_rounds(monkeypatch, loop):
    """PHASE3E 9: accepts that only lower `quality` (the last score term) are no gain and stop the close."""
    def counting(base, out, gaps, ctx):
        shutil.copy(base, out)
        with open(out, "a") as fh:
            fh.write("#%d" % counting.n)
        counting.n += 1
        return {"mode": "count"}
    counting.n = 0
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, counting)
    accepts = {"n": 0}

    def accept(*a, **k):
        accepts["n"] += 1
        return True, ""
    monkeypatch.setattr(GP, "_accept", accept)
    monkeypatch.setattr(GP, "score", lambda m, gaps: [0] * 9 + [-accepts["n"]])
    res = _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    assert res["stop"] == "no gain 5 rounds" and res["attempts"] == GP.NO_GAIN_ROUNDS == 5


def _ctx_spy(monkeypatch, seen):
    def fn(base, out, gaps, ctx):
        seen.append(ctx["budget_s"])
        return {"status": "none"}
    for r in GP.DISPATCH["open-free"]:
        monkeypatch.setitem(GP.RUNGS, r, fn)


def test_rung_cap_default_900_s(monkeypatch, loop):
    seen = []
    _ctx_spy(monkeypatch, seen)
    _close(loop["board"], loop["work"], budget_min=84, attempts=loop["attempts"], log=lambda m: None)
    assert seen[0] == GP.RUNG_CAP_S == 900


def test_rung_cap_flow_override_minutes(monkeypatch, loop):
    seen = []
    _ctx_spy(monkeypatch, seen)
    _close(loop["board"], loop["work"], cons={"flow": {"rung_cap_min": 5}}, budget_min=84,
           attempts=loop["attempts"], log=lambda m: None)
    assert seen[0] == 300


def test_rung_cap_keeps_small_remaining_budget(monkeypatch, loop):
    seen = []
    _ctx_spy(monkeypatch, seen)
    _close(loop["board"], loop["work"], budget_min=100 / 60, attempts=loop["attempts"], log=lambda m: None)
    assert seen[0] == pytest.approx(100, abs=1)


def test_attempt_cand_is_refused_candidate_vector(monkeypatch, loop):
    def changed(base, out, gaps, ctx):                  # writes a different board that keeps the open: refused
        open(out, "w").write("changed")
        return {"status": "ok"}
    monkeypatch.setitem(GP.RUNGS, "patch", changed)
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    first = json.loads(open(loop["attempts"]).readline())
    assert first["method"] == "patch" and first["result"] == "refused"
    assert isinstance(first["cand"], list) and len(first["cand"]) == len(first["score"][0])


def test_attempt_cand_null_when_rung_wrote_no_board(monkeypatch, loop):
    monkeypatch.setitem(GP.RUNGS, "patch", lambda base, out, gaps, ctx: {"status": "none"})
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=lambda m: None)
    first = json.loads(open(loop["attempts"]).readline())
    assert first["method"] == "patch" and first["cand"] is None


def _ids_measure(board, kicad_cli="kicad-cli"):
    """Stub board: the file holds a JSON list of open gap ids."""
    ids = json.load(open(board))
    gaps = [_g(i) for i in ids]
    return {"drc": {}, "opens": len(ids), "shorts": 0, "errors": 0, "err_keys": Counter(), "gaps": gaps}


def _split_setup(monkeypatch, loop, rung):
    monkeypatch.setattr(GP, "_remeasure", _ids_measure)
    monkeypatch.setattr(GP, "_classify_into", lambda mm, cons, board: None)
    monkeypatch.setattr(GP, "_protect", lambda board, cons: set())
    monkeypatch.setattr(GP, "_clean_board", lambda p: None)
    open(loop["board"], "w").write(json.dumps(["g1", "g2", "g3", "g4"]))
    monkeypatch.setitem(GP.DISPATCH, "open-free", ["krt"])
    monkeypatch.setitem(GP.RUNGS, "krt", rung)


def _split_accept(base_m, base_board, cand_m, cand_board, target_ids, cand_gaps, *rest):
    """Guard stub: a candidate that still holds gap g3 while g3 was a target is refused (R6), else accepted."""
    ids = {x["id"] for x in cand_gaps}
    if "g3" in ids and "g3" in set(target_ids):
        return False, "R6 power worse 1->2"
    return True, ""


def test_split_refused_batch_closes_all_but_the_bad_gap(monkeypatch, loop):
    calls = []

    def rung(base, out, gaps, ctx):
        calls.append([g["id"] for g in gaps])
        keep = [i for i in json.load(open(base)) if i not in {g["id"] for g in gaps} or i == "g3"]
        json.dump(keep, open(out, "w"))
        return {"status": "ok"}
    _split_setup(monkeypatch, loop, rung)
    monkeypatch.setattr(GP, "_accept", _split_accept)
    res = _close(loop["board"], loop["work"], max_rounds=1, attempts=loop["attempts"], log=lambda m: None)
    assert [x["id"] for x in res["left"]] == ["g3"]
    assert len(calls) <= 1 + GP.SPLIT_MAX
    assert calls[0] == ["g1", "g2", "g3", "g4"]


def test_split_not_done_for_nothing_laid(monkeypatch, loop):
    calls = []

    def rung(base, out, gaps, ctx):
        calls.append([g["id"] for g in gaps])
        return {"status": "nothing laid"}
    _split_setup(monkeypatch, loop, rung)
    _close(loop["board"], loop["work"], max_rounds=1, attempts=loop["attempts"], log=lambda m: None)
    assert calls == [["g1", "g2", "g3", "g4"]]


def test_r6_power_mag_refused_per_net_when_rail_opens_are_equal(guard):
    base = _mm(opens=1, gaps=[_g("a"), dict(_g("p", kind="power-width"), mag=1, nets=["RAILX"])])
    base["opens_by_net"] = Counter({"RAILX": 1})
    cand = _mm(opens=1, gaps=[_g("b"), dict(_g("p", kind="power-width"), mag=2, nets=["RAILX"])])
    cand["opens_by_net"] = Counter({"RAILX": 1})
    ok, why = _accept(guard, base, cand)
    assert ok is False and why == "R6 power_mag worse RAILX 1->2"


def test_r6_power_mag_allowed_when_rail_closes_its_own_open(guard):
    base = _mm(opens=3, gaps=[_g("a"), dict(_g("p", kind="power-width"), mag=1, nets=["RAILX"])])
    base["opens_by_net"] = Counter({"RAILX": 3})
    cand = _mm(opens=1, gaps=[_g("b"), dict(_g("p", kind="power-width"), mag=2, nets=["RAILX"])])
    cand["opens_by_net"] = Counter({"RAILX": 1})
    ok, why = _accept(guard, base, cand)
    assert ok is True and "power_mag" not in why


def test_krt_command_routes_rail_at_its_width(monkeypatch, tmp_path):
    from fluxplace import routerloc as RL, routerules as RR, power as PWR
    monkeypatch.setattr(RL, "resolve", lambda *a, **k: {"py": "/usr/bin/python3", "dir": str(tmp_path / "krt")})
    monkeypatch.setattr(RR, "rules", lambda cons, board=None, default=None, **k: {"track": 0.2})
    monkeypatch.setattr(RR, "size_args", lambda R: [])
    monkeypatch.setattr(RR, "flags", lambda R, d, w, script="route.py": [])
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    monkeypatch.setattr(GP, "copper_layers", lambda b: ["F.Cu", "B.Cu"])
    monkeypatch.setattr(PWR, "route_widths", lambda cons, layers=None, track_mm=0.15, exclude=(): {"VCCRAIL": 0.6})
    cmd = GP._krt_cmd(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), ["VCCRAIL"],
                      {"cons": {}, "work": str(tmp_path)})
    assert cmd[cmd.index("--power-nets") + 1] == "VCCRAIL"
    assert cmd[cmd.index("--power-nets-widths") + 1] == "0.6"
    cmd = GP._krt_cmd(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), ["SIG"],
                      {"cons": {}, "work": str(tmp_path)})
    assert "--power-nets" not in cmd


def test_rail_is_never_a_krt_rip_candidate(monkeypatch, tmp_path):
    import subprocess
    from fluxplace import routerloc as RL, routerules as RR, adaptive, batchroute as BR, power as PWR
    monkeypatch.setattr(RL, "resolve", lambda *a, **k: {"py": "/usr/bin/python3", "dir": str(tmp_path / "krt")})
    monkeypatch.setattr(RR, "rules", lambda cons, board=None, default=None, **k: {"track": 0.2})
    monkeypatch.setattr(RR, "size_args", lambda R: [])
    monkeypatch.setattr(RR, "flags", lambda R, d, w, script="route.py": [])
    monkeypatch.setattr(adaptive, "krt_extra", lambda d, s: [])
    monkeypatch.setattr(BR, "has_fanout", lambda src: False)
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    monkeypatch.setattr(GP, "copper_layers", lambda b: ["F.Cu", "B.Cu"])
    monkeypatch.setattr(PWR, "route_widths", lambda cons, layers=None, track_mm=0.15, exclude=(): {"VCCRAIL": 0.6})
    monkeypatch.setattr(_procs_mod, "run", lambda cmd, **kw: (open(cmd[cmd.index("--output") + 1], "w").write("x"),
                                                          types.SimpleNamespace(returncode=0))[1])
    skips = []
    monkeypatch.setattr(GP, "_krt_rip", lambda b, net, cp, skip: skips.append(set(skip)) or [])
    opens = iter([{"A"}, set()] * 2)
    monkeypatch.setattr(GP, "_krt_open", lambda b, cp: next(opens))
    gap = dict(_g("a"), nets=["A"])
    GP._r_krt(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [gap],
              {"cons": {}, "work": str(tmp_path), "budget_s": 60})
    assert skips and "VCCRAIL" in skips[0]


def test_pair_refused_by_pairgroup_goes_to_fallback(monkeypatch, loop):
    pair_gap = dict(_g("pg", kind="pair-open", cls="pair"), nets=["PX", "NX"])
    calls = []

    def refuse(base, out, gaps, ctx):
        return {"status": "nothing laid"}
    monkeypatch.setitem(GP.RUNGS, "pairgroup", refuse)
    monkeypatch.setitem(GP.DISPATCH, "pair-open", ["pairgroup"])
    monkeypatch.setattr(GP, "_remeasure", lambda p, kicad_cli="kicad-cli": {"drc": {}, "opens": 2, "shorts": 0,
                                                                              "errors": 0, "err_keys": Counter(),
                                                                              "gaps": [pair_gap]})
    monkeypatch.setattr(GP, "_classify_into", lambda mm, cons, board: None)
    monkeypatch.setattr(GP, "_protect", lambda board, cons: set())
    monkeypatch.setattr(GP, "_clean_board", lambda p: None)
    _close(loop["board"], loop["work"], max_rounds=1, attempts=loop["attempts"], log=lambda m: None)
    st = json.load(open(os.path.join(loop["work"], "gaps.json")))
    assert st["fallback"] == [["PX", "NX"]]


def test_hs_pair_refused_is_never_in_fallback(monkeypatch, loop):
    pair_gap = dict(_g("pg", kind="pair-open", cls="hs-pair"), nets=["PX", "NX"])
    monkeypatch.setitem(GP.RUNGS, "pairgroup", lambda base, out, gaps, ctx: {"status": "nothing laid"})
    monkeypatch.setitem(GP.DISPATCH, "pair-open", ["pairgroup"])
    monkeypatch.setattr(GP, "_remeasure", lambda p, kicad_cli="kicad-cli": {"drc": {}, "opens": 2, "shorts": 0,
                                                                              "errors": 0, "err_keys": Counter(),
                                                                              "gaps": [pair_gap]})
    monkeypatch.setattr(GP, "_classify_into", lambda mm, cons, board: None)
    monkeypatch.setattr(GP, "_protect", lambda board, cons: set())
    monkeypatch.setattr(GP, "_clean_board", lambda p: None)
    _close(loop["board"], loop["work"], max_rounds=1, attempts=loop["attempts"], log=lambda m: None)
    st = json.load(open(os.path.join(loop["work"], "gaps.json")))
    assert st["fallback"] == []


def _fb_krt_env(monkeypatch, tmp_path, opens_seq):
    import subprocess
    from fluxplace import routerloc as RL, routerules as RR, adaptive, batchroute as BR
    monkeypatch.setattr(RL, "resolve", lambda *a, **k: {"py": "/usr/bin/python3", "dir": str(tmp_path / "krt")})
    monkeypatch.setattr(RR, "rules", lambda cons, board=None, default=None, **k: {"track": 0.2})
    monkeypatch.setattr(RR, "size_args", lambda R: [])
    monkeypatch.setattr(RR, "flags", lambda R, d, w, script="route.py": [])
    monkeypatch.setattr(adaptive, "krt_extra", lambda d, s: [])
    monkeypatch.setattr(BR, "has_fanout", lambda src: False)
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd[cmd.index("--nets") + 1:])
        open(cmd[cmd.index("--output") + 1], "w").write("routed")
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(_procs_mod, "run", fake_run)
    opens = iter(opens_seq)
    monkeypatch.setattr(GP, "_krt_open", lambda b, cp: next(opens))
    monkeypatch.setattr(GP, "_krt_rip", lambda b, net, cp, skip: [])
    return calls


def test_fallback_pair_routed_in_one_krt_call(monkeypatch, tmp_path):
    calls = _fb_krt_env(monkeypatch, tmp_path, [{"PX", "NX"}, set()])
    gaps = [dict(_g("a"), nets=["PX"]), dict(_g("b"), nets=["NX"])]
    ctx = {"cons": {}, "work": str(tmp_path), "budget_s": 60, "protect": {"PX", "NX"},
           "pair_fallback": [["PX", "NX"]]}
    res = GP._r_krt(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), gaps, ctx)
    assert calls == [["PX", "NX"]]
    assert res["closed"] == 2 and (tmp_path / "o.kicad_pcb").exists()


def test_fallback_pair_one_leg_open_keeps_previous_board(monkeypatch, tmp_path):
    calls = _fb_krt_env(monkeypatch, tmp_path, [{"PX", "NX"}, {"NX"}])
    gaps = [dict(_g("a"), nets=["PX"]), dict(_g("b"), nets=["NX"])]
    ctx = {"cons": {}, "work": str(tmp_path), "budget_s": 60, "protect": {"PX", "NX"},
           "pair_fallback": [["PX", "NX"]]}
    res = GP._r_krt(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), gaps, ctx)
    assert calls == [["PX", "NX"]]
    assert res["closed"] == 0 and not (tmp_path / "o.kicad_pcb").exists()


def test_r5_relaxes_fallback_legs_for_open_free(guard):
    prot = {"PX", "NX", "RF1"}
    tg = [dict(_g("a"), nets=["PX"])]
    assert GP._r5_set("open-free", tg, prot, set(), {"PX", "NX"}) == {"NX", "RF1"}
    assert GP._r5_set("open-free", tg, prot, set(), set()) == prot


def test_mask_opening_gaps_dispatch_to_maskcut(monkeypatch, tmp_path):
    from fluxplace import thermal as TH
    seen = {}

    def fake_recut(board, cons, out):
        seen["args"] = (board, out)
        open(out, "w").write("recut")
        return {"blocks": 0, "inferred": 1, "skipped": [], "log": []}
    monkeypatch.setattr(TH, "recut", fake_recut)
    out = tmp_path / "o.kicad_pcb"
    res = GP._r_maskcut(str(tmp_path / "in.kicad_pcb"), str(out), [], {"cons": {}})
    assert res == {"status": "ran", "blocks": 0, "inferred": 1, "skipped": 0} and out.exists()
    assert "maskcut" in GP.RUNGS and GP.DISPATCH["mask-opening"] == ["maskcut"]


# ---- PHASE3G G4: power rungs dispatch; a power_mag drop with equal power is a lower score -----------------------
def test_power_gaps_dispatch_to_the_power_rungs():
    from fluxplace import gaps as GP
    assert GP.DISPATCH["power-width"] == ["powerwiden", "railpour"]
    assert GP.DISPATCH["power-vias"] == ["powervias"]
    for r in ("powerwiden", "railpour", "powervias"):
        assert r in GP.RUNGS
    assert "powerwiden" in GP.SPLIT_RUNGS


def test_power_mag_falling_with_equal_power_is_a_lower_score():
    from fluxplace import gaps as GP
    m = {"shorts": 0, "opens": 0, "errors": 0}
    base = [{"id": "a", "kind": "power-width", "class": "power", "nets": ["V"], "mag": 2.0},
            {"id": "b", "kind": "pair-open", "class": "plain", "nets": ["P", "N"], "sev": 3}]
    cand = [dict(base[0], mag=1.0), base[1]]
    assert GP.score(m, cand) < GP.score(m, base)
    assert GP.score_terms(m, cand)["power"] == GP.score_terms(m, base)["power"]


def test_accept_power_width_target_with_less_narrow_copper_is_progress(monkeypatch):
    """W15 close: powerwiden took a rail from 259 to 166 narrow mm and was refused as "target gap still there", so the widen was thrown
    away.  A power target whose mag dropped no longer has to disappear; one whose mag is unchanged still does; a rise is R6."""
    calls = []
    monkeypatch.setattr(GP.drcguard, "accept", lambda base, cand, mode, target, *a, **k: calls.append(list(target)) or (True, ""))
    z = {"shorts": 0, "opens": 0, "errors": 0, "review_failed": []}
    row = lambda mag: [{"id": "power-width:VR", "mag": mag, "nets": ["VR"], "kind": "power-width", "class": "power"}]
    base = dict(z, gaps=row(259))
    assert GP._accept(base, "b", dict(z), "c", ["power-width:VR"], row(166), "power-width") == (True, "")
    assert calls[-1] == []
    GP._accept(base, "b", dict(z), "c", ["power-width:VR"], row(259), "power-width")
    assert calls[-1] == ["power-width:VR"]
    assert GP._accept(base, "b", dict(z), "c", ["power-width:VR"], row(300), "power-width")[1].startswith("R6 power_mag worse VR")


def test_packets_are_written_after_every_kind_had_its_own_rungs(monkeypatch, tmp_path, guard):
    """W15 close: 9 pair packets (no answer waiting) took 9 of the 30 minutes before the open nets had a single rung.  With no answer
    pending the `ai` rung of a kind is deferred to the end of the round: the open-pour packet comes after the open-free rungs."""
    pour = DRC["unconnected_items"][0]
    monkeypatch.setattr(GP, "measure", lambda board, kicad_cli="kicad-cli": {
        "drc": {"violations": [], "unconnected_items": [pour, FREE]}, "opens": 2, "shorts": 0, "errors": 0, "err_keys": Counter()})
    monkeypatch.setattr(GP.versions, "copper_fingerprint", _fp)
    board = tmp_path / "in.kicad_pcb"
    board.write_text("original")
    for kind in ("open-pour", "open-free"):
        for r in GP.DISPATCH[kind]:
            monkeypatch.setitem(GP.RUNGS, r, (lambda base, out, gaps, ctx: {"status": "packet", "n": 1}) if r == "ai" else _rung("same"))
    attempts = str(tmp_path / "a.jsonl")
    res = _close(str(board), str(tmp_path / "w"), attempts=attempts, log=lambda m: None)
    seq = [(x["gap"]["kind"], x["method"]) for x in map(json.loads, open(attempts))]
    ai = [i for i, (_k, m) in enumerate(seq) if m == "ai"]
    assert [seq[i][0] for i in ai] == ["open-pour", "open-free"] and ai[0] > max(i for i, (_k, m) in enumerate(seq) if m != "ai")
    assert res["stop"] == "packets"


# ---- B6 step 0: refusal record ----

def test_accept_trace_lists_every_rule_same_verdict(guard):
    base = _mm(opens=3, gaps=[_g("a"), _g("b")])
    cand = _mm(opens=3, err_keys=[("clearance", ("x",))], gaps=[_g("a"), _g("b")])
    plain = _accept(guard, base, cand)
    trace = []
    got = GP._accept(base, guard["base"], cand, guard["cand"], ["a"], cand["gaps"], trace=trace)
    assert got == plain and plain[1] == "R2 new DRC key clearance"
    st = {t["rule"]: t["status"] for t in trace}
    assert st["R2"] == st["R4"] == st["target"] == "fail" and st["R0"] == st["R1"] == st["R6:rf"] == "pass"
    assert {"review", "R6:hs_pair", "R6:power", "R6:power_mag", "R3"} <= set(st)


def test_crashed_check_is_unknown_and_refuses(monkeypatch, guard):
    def boom(*a, **k):
        raise RuntimeError("snapshot died")
    monkeypatch.setattr(GP.audit, "connectivity_diff", boom)
    base = _mm(opens=3, gaps=[_g("a"), _g("b")])
    cand = _mm(opens=2, gaps=[_g("b")])                       # would be accepted if R0 simply passed
    trace = []
    ok, why = GP._accept(base, guard["base"], cand, guard["cand"], ["a"], cand["gaps"], trace=trace)
    r0 = next(t for t in trace if t["rule"] == "R0")
    assert not ok and r0["status"] == "UNKNOWN" and why.startswith("UNKNOWN R0")
    assert all(t["status"] == "pass" for t in trace if t["rule"] != "R0")     # UNKNOWN is not an ordinary violation
    rec = refusal_mod.record(ctx={"rung": "x"}, rules=trace, base_m=base, cand_m=cand, cand_gaps=cand["gaps"], target_ids=["a"])
    assert rec["failing"] == ["R0"]
    with pytest.raises(RuntimeError):                         # the untraced path is unchanged: the crash propagates
        GP._accept(base, guard["base"], cand, guard["cand"], ["a"], cand["gaps"])


def test_close_writes_refusal_records_and_histogram(monkeypatch, loop):
    monkeypatch.setitem(GP.RUNGS, "patch", _rung("other"))
    for r in GP.DISPATCH["open-free"]:
        if r != "patch":
            monkeypatch.setitem(GP.RUNGS, r, _rung("same"))
    out = []
    _close(loop["board"], loop["work"], attempts=loop["attempts"], log=out.append)
    recs = refusal_mod.load(refusal_mod.path(loop["work"]))
    cand = [r for r in recs if r["rung"] == "patch"][0]
    for f in ("run", "attempt", "kind", "rung", "why", "targets", "rules", "blockers", "score_delta", "target", "drc",
              "changed", "failing", "refused"):
        assert f in cand
    assert {"R4", "target"} <= set(cand["failing"]) and cand["score_delta"]["opens"]["delta"] == 0
    assert cand["target"]["closed_all"] is False and cand["drc"] == {"added": [], "removed": []}
    assert cand["changed"]["status"] == "UNKNOWN"             # a text stub is not a board: UNKNOWN, no crash
    assert any(r["failing"] == ["no_change"] for r in recs)   # candidate-less refusals are recorded too
    assert any(l.startswith("close refusals ") and "score-equal-no-other-failure=" in l for l in out)
    h = refusal_mod.histogram(recs)
    assert h["n"] == len(recs) and h["first"]["R4"] >= 1


def test_histogram_buckets():
    r = lambda failing, d: {"failing": failing, "score_delta": {"opens": {"delta": d}}}      # noqa: E731
    h = refusal_mod.histogram([r(["R4"], 0), r(["R4", "target"], 0), r(["R2", "R4"], 1), r(["R0"], 0)])
    assert h["first"] == {"R4": 2, "R2": 1, "R0": 1} and h["any"]["R4"] == 3 and h["score_equal_only"] == 1
