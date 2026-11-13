"""Route-stage escalation ladder (routeladder.py) against a fake router world: no pcbnew, no external router."""
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import routeladder as RLD
from fluxplace import routediag as RD

WANT = ["A", "B", "C"]


def world(tmp_path, cons=None, left=1000.0, extra_layers=("In1.Cu",)):
    body = PB.fp("U1", 20, 20, [PB.pad("1", 0, 0, "A"), PB.pad("2", 2, 0, "B")]) + PB.fp("U2", 60, 20, [PB.pad("1", 0, 0, "A"), PB.pad("2", 2, 0, "B"), PB.pad("3", 4, 0, "C")])
    body += '(segment (start 30 30) (end 40 30) (width 0.2) (layer "F.Cu") (net "BLK"))\n'
    body += '(segment (start 30 31) (end 40 31) (width 0.2) (layer "F.Cu") (locked yes) (net "BLK"))\n'
    best = PB.write(tmp_path, body, "best.kicad_pcb")

    def routed(path):
        t = open(path).read()
        return set(re.findall(r'\(fake_routed "([^"]+)"\)', t))

    def conn(path):
        r = routed(path)
        o = {n: 1 for n in WANT if n not in r}
        if '(net "BLK")' not in open(path).read().replace('(locked yes) (net "BLK")', "") and "BLK" not in r:
            o["BLK"] = 1                                  # its unlocked copper was ripped
        return o

    log = []

    def make_run(layers):
        def run(src, dst, nets, timeout, limited=True):
            t = open(src).read()
            add = ""
            for n in nets:
                ok = {"A": True, "B": "BLK-free" in t or True, "C": "In1.Cu" in layers}.get(n, True)
                if n == "B" and '(net "BLK")' in t.replace('(locked yes) (net "BLK")', ""):
                    ok = False                           # B is blocked while BLK's unlocked copper is there
                if ok:
                    add += f'(fake_routed "{n}")\n'
            log.append((tuple(nets), tuple(layers)))
            open(dst, "w").write(t.rstrip()[:-1] + add + ")\n")
        return run

    def keep(path):
        if path and os.path.exists(path) and sum(conn(path).values()) < sum(conn(best).values()):
            shutil.copy(path, best)
        return sum(conn(best).values())
    return best, conn, keep, make_run, log, left


def ladder(tmp_path, monkeypatch, cons=None, left=1000.0):
    best, conn, keep, make_run, log, _ = world(tmp_path)
    B = RD.load(best)
    monkeypatch.setattr(RLD, "blockers_of", lambda B_, n, c, s: ["BLK"] if n == "B" else [])
    monkeypatch.setattr(RLD.RD, "diagnose", lambda B_, nets, c=None, s=None: {"nets": []})
    monkeypatch.setattr(RLD, "signal_layers_unused", lambda B_, c, used: ["In1.Cu"])
    clock = [left]
    L = RLD.Ladder(best, str(tmp_path / "lad"), make_run(["F.Cu", "B.Cu"]), make_run, conn, keep, lambda: clock[0], B, cons or {}, None,
                   {}, ["F.Cu", "B.Cu"])
    return L, best, conn, log, clock


def test_ladder_closes_each_net_with_its_rung_and_records_it(tmp_path, monkeypatch):
    L, best, conn, log, _ = ladder(tmp_path, monkeypatch)
    # start: A routed, B blocked by BLK, C needs the inner layer
    open(best, "a").write("")
    t = open(best).read().rstrip()[:-1] + '(fake_routed "A")\n)\n'
    open(best, "w").write(t)
    res = L.climb()
    assert conn(best) == {}                              # everything closed
    closed = {r["rung"]: r["closed"] for r in res["rungs"] if r["closed"]}
    assert closed.get("ripup") == 1 and closed.get("layer") == 1
    assert [r["nets"] for r in res["rungs"] if r["rung"] == "ripup" and r["closed"]] == [["B"]]
    assert any(layers == ("F.Cu", "B.Cu", "In1.Cu") for _n, layers in log)      # the layer rung offered the inner layer
    # the locked copper of the blocker was never ripped
    assert '(locked yes) (net "BLK")' in open(best).read()
    assert "ladder ripup closed 1" in RLD.summary(res)


def test_ladder_respects_allow_rip_never_and_stops_on_exhaustion(tmp_path, monkeypatch):
    L, best, conn, log, _ = ladder(tmp_path, monkeypatch, cons={"route": {"allow_rip": "never"}})
    res = L.climb()
    assert any(r["rung"] == "ripup" and "allow_rip" in r["what"] for r in res["rungs"])
    assert "B" in conn(best) and "C" not in conn(best)    # C closed by the layer rung, B stays open: nothing else to try
    assert res["exhausted"] and not res["timed_out"]


def test_ladder_stops_at_the_time_budget(tmp_path, monkeypatch):
    L, best, conn, log, clock = ladder(tmp_path, monkeypatch, left=5.0)
    res = L.climb()
    assert res["timed_out"] and not log


def test_post_step_snaps_unlocked_ends_and_warns_about_the_rest(tmp_path):
    from fluxplace import routepost as RPO
    body = ('(via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "N"))\n'
            '(segment (start 10.2 10) (end 14 10) (width 0.2) (layer "F.Cu") (net "N"))\n'
            '(via (at 30 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "M"))\n'
            '(segment (start 30.2 10) (end 34 10) (width 0.2) (layer "F.Cu") (locked yes) (net "M"))\n')
    path = PB.write(tmp_path, body)
    rep = RPO.post_round(path, lambda p: {"N": 1}, join=False)
    assert rep["snapped"] == 1 and "(start 10 10)" in open(path).read()
    assert [(n, l, x, y, k) for n, l, x, y, k in rep["off_centre"]] == [("M", "F.Cu", 30.2, 10.0, True)]
    line = RPO.gate_line(rep)
    assert line.startswith("WARN 1 track end(s) off-centre") and "M F.Cu 30.2,10 locked" in line
    assert RPO.gate_line({"off_centre": []}) == ""
