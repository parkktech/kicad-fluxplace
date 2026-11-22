"""final-routing tournament (D71ct): params, _Sub, tournament winner, lean_krt/krtchain extra flags (router stubbed)."""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import krtchain as K, lookahead as LA, placetour as PT, routetour as RT  # noqa: E402


def test_params_default_is_off_with_eight_variants():
    p = RT.params({})
    assert p["enabled"] is False and len(p["variants"]) == 8 and p["top"] == 2


def test_params_custom_variants():
    p = RT.params({"route": {"tournament": {"enabled": True, "variants": [{"name": "a", "flags": ["--bus"]}]}}})
    assert p["enabled"] is True and p["variants"] == [{"name": "a", "flags": ["--bus"]}]


def test_sub_dir_and_attribute_fallback(tmp_path):
    parent = types.SimpleNamespace(cons={"x": 1}, a="A", dir=lambda n, nm: str(tmp_path))
    sub = RT._Sub(parent, str(tmp_path / "root"), ["--bus"])
    p = sub.dir(3, "route")
    assert p == str(tmp_path / "root" / "3-route") and os.path.isdir(p)
    assert sub.cons == {"x": 1} and sub.a == "A" and sub.krt_extra == ["--bus"]


def test_run_picks_best_full_and_passes_flags(tmp_path, monkeypatch):
    variants = [{"name": "p", "flags": []}, {"name": "q", "flags": ["--bus"]}, {"name": "r", "flags": ["--via-cost", "40"]}]
    cons = {"route": {"tournament": {"enabled": True, "top": 2, "variants": variants}}}
    c = types.SimpleNamespace(a=types.SimpleNamespace(router_py="py", router_dir="/r"), cons=cons,
                              dir=lambda n, nm: str(tmp_path / f"{n}-{nm}"), work=str(tmp_path), state={})
    os.makedirs(c.dir(1, "route"), exist_ok=True)
    lean_extra = {}

    def fake_lean(a, board, outdir, budget, extra=()):
        lean_extra[os.path.basename(outdir)] = list(extra)
        return f"/judge/{os.path.basename(outdir)}.pcb"
    monkeypatch.setattr(LA, "lean_krt", fake_lean)
    monkeypatch.setattr(PT, "_review", lambda a, routed, path: {"open": [], "hot": []})
    keys = {"p": (0, 3, 0.0, 10, 0.0), "q": (0, 1, 0.0, 12, 0.0), "r": (0, 2, 0.0, 9, 0.0)}
    monkeypatch.setattr(PT, "judge_key", lambda routed, blame, cons=None: keys[os.path.basename(routed).split(".")[0]])
    fk = {"p": (0, 0, 2), "q": (0, 0, 1), "r": (0, 0, 0)}
    monkeypatch.setattr(RT, "full_key", lambda board: fk[os.path.basename(board).split(".")[0]])
    seen = {}

    def fake_krt_run(sub, n, board):
        name = os.path.basename(sub._root)
        seen[name] = list(sub.krt_extra)
        return {"board": f"/full/{name}.pcb", "verdict": "pass", "line": "x"}
    monkeypatch.setattr(K, "run", fake_krt_run)

    r = RT.run(c, 1, str(tmp_path / "in.kicad_pcb"))
    assert lean_extra == {"p": [], "q": ["--bus"], "r": ["--via-cost", "40"]}
    assert seen == {"q": ["--bus"], "r": ["--via-cost", "40"]}   # top 2 by judge key (opens): q (1), r (2); p (3) cut
    assert r["board"] == "/full/r.pcb"                           # full key: r (0 errors) beats q (1 error)
    assert r["line"].startswith("routetour winner r ")
    txt = open(os.path.join(c.dir(1, "route"), "TOURNAMENT.md")).read()
    assert "| judge | p |" in txt and "| full | r |" in txt and "winner: r" in txt


def test_lean_krt_appends_extra_after_quick(monkeypatch, tmp_path):
    captured = {}
    def fake_capped(cmd, *a, **k):
        captured["cmd"] = cmd
        return False, 0.0
    monkeypatch.setattr(LA, "_run_capped", fake_capped)
    monkeypatch.setattr(LA, "_ghost_copy", lambda board, outdir: board)
    monkeypatch.setattr(LA, "_signal_nets", lambda copy: (["F.Cu", "B.Cu"], []))
    monkeypatch.setattr(LA, "_ghost_stats", lambda *a, **k: None)
    import fluxplace.routediag as RD
    monkeypatch.setattr(RD, "load", lambda b: {"pads": [{"net": "N"}, {"net": "N"}]})
    import fluxplace.routerules as RR
    monkeypatch.setattr(RR, "rules_for", lambda *a, **k: {"track": 0.15, "clearance": 0.127, "via": 0.45, "drill": 0.2})
    monkeypatch.setattr(RR, "write_overrides", lambda p, R: None)
    monkeypatch.setattr(RR, "size_args", lambda R: [])
    a = types.SimpleNamespace(router_py="py", router_dir="/r", track=None, clearance=None)
    LA.lean_krt(a, str(tmp_path / "b.kicad_pcb"), str(tmp_path), 5, extra=["--bus"])
    assert captured["cmd"][-3:] == ["--quick", "2", "--bus"]


def test_krtchain_cmd_route_gets_krt_extra(tmp_path, monkeypatch):
    (tmp_path / "in.kicad_pcb").write_text("x")
    monkeypatch.setattr(K, "plane_plan", lambda b: (["F.Cu", "B.Cu"], []))
    monkeypatch.setattr(K, "_all_nets", lambda b: ["X"])
    monkeypatch.setattr(K.RC, "open_by_net", lambda b, **k: {})
    R = {"min_clearance": 0.127, "min_track": 0.127, "min_via": 0.45, "min_drill": 0.2, "clearance": 0.127, "track": 0.15,
         "via": 0.45, "drill": 0.2, "clearance_class": 0.0}
    monkeypatch.setattr(K.RR, "rules_for", lambda *a, **k: dict(R))
    c = types.SimpleNamespace(a=types.SimpleNamespace(router_py="py", router_dir="/r", track=0.15, clearance=0.127),
                              cons={"route": {"whole_post": False}}, work=str(tmp_path), state={},
                              dir=lambda n, nm: str(tmp_path), krt_extra=["--via-cost", "40"])
    calls = []

    def runner(cmd, log, **kw):
        calls.append(cmd)
        open(cmd[3], "w").write("y")
        return 0, 1.0
    K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    route_calls = [x for x in calls if x[1].endswith("route.py")]
    diff_calls = [x for x in calls if x[1].endswith("route_diff.py")]
    assert route_calls and all("--via-cost" in x for x in route_calls)
    assert all("--via-cost" not in x for x in diff_calls)
