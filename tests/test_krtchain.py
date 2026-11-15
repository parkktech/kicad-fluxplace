"""krt-whole chain: flag construction, step chaining, exit-3 acceptance (router stubbed)."""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import krtchain as K  # noqa: E402

R = {"min_clearance": 0.127, "min_track": 0.127, "min_via": 0.45, "min_drill": 0.2, "clearance": 0.127, "track": 0.15,
     "via": 0.45, "drill": 0.2, "clearance_class": 0.0}


def test_flags(tmp_path):
    f = K.base_flags(R, str(tmp_path))
    assert f[:8] == ["--track-width", "0.15", "--clearance", "0.127", "--via-size", "0.45", "--via-drill", "0.2"]
    assert "--strict-sizes" in f and f[f.index("--escalation") + 1] == "off"
    assert "clearance = 0.127" in open(f[f.index("--fab-overrides") + 1]).read()
    c = K.cmd_route("py", "/r", "a", "b", ["F.Cu", "B.Cu"], f, {"GND": 0.3})
    assert c[c.index("--power-nets") + 1] == "GND" and c[c.index("--power-nets-widths") + 1] == "0.3"
    assert "--keep-input-copper" in c and "--keep-input-copper" not in K.cmd_planes("py", "/r", "a", "b", "GND", "In1.Cu", f)


def _ctx(tmp_path, monkeypatch, rcs):
    (tmp_path / "in.kicad_pcb").write_text("x")
    monkeypatch.setattr(K, "plane_plan", lambda b: (["F.Cu", "B.Cu"], [("GND", "In1.Cu")]))
    monkeypatch.setattr(K, "_all_nets", lambda b: ["USB_DP", "USB_DM", "X"])
    monkeypatch.setattr(K.RC, "open_by_net", lambda b, **k: {"USB_DP": 1})
    monkeypatch.setattr(K.RR, "rules", lambda *a, **k: dict(R))
    c = types.SimpleNamespace(a=types.SimpleNamespace(router_py="py", router_dir="/r", track=0.15, clearance=0.127),
                              cons={"pairs": {"USB_": {}}}, work=str(tmp_path), state={}, dir=lambda n, nm: str(tmp_path))
    calls = []

    def runner(cmd, log):
        calls.append(cmd)
        rc = rcs.pop(0)
        if rc in (0, 3):
            open(cmd[3], "w").write("y")
        return rc, 1.0
    return c, calls, runner


def test_chain_accepts_exit3(tmp_path, monkeypatch):
    c, calls, runner = _ctx(tmp_path, monkeypatch, [0, 0, 3])
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert [os.path.basename(x[1]) for x in calls] == ["route_planes.py", "route_diff.py", "route.py"]
    assert calls[1][calls[1].index("--nets") + 1:calls[1].index("--layers")] == ["USB_DM", "USB_DP"]
    assert r["verdict"] == "warn" and "USB_DP" in r["line"]


def test_chain_stops_on_failure(tmp_path, monkeypatch):
    c, calls, runner = _ctx(tmp_path, monkeypatch, [0, 1, 0])
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert r["verdict"] == "fail" and len(calls) == 2


def test_stub_ok_keeps_clearance(tmp_path):
    pcbnew = __import__("pytest").importorskip("pcbnew")
    from fluxplace import planetie as PT
    from test_routerules_syn import _board
    b = pcbnew.LoadBoard(_board(str(tmp_path / "s.kicad_pcb"), pads=[("U1", 5, 6, "A"), ("U2", 5.5, 6, "B")]))
    nc = b.FindNet("A").GetNetCode()
    f = pcbnew.F_Cu
    assert not PT.stub_ok(b, nc, f, (5, 6), (6, 6), 0.15, 0.127)        # runs into the B pad
    assert PT.stub_ok(b, nc, f, (5, 6), (5, 7), 0.15, 0.127)
