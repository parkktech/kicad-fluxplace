"""handroute._free_fields: same fields as the per-obstacle full-grid version it replaced, without one grid array per obstacle (W16 route-11
resume-5: a 63 x 68 mm AI packet window held 23 GB and the kernel OOM-killed the close run); procs.run memory cap; rung launcher outcome."""
import random
import sys
import tracemalloc

import numpy as np
import pytest

from fluxplace import closeexec as CX
from fluxplace import gaps
from fluxplace import handroute as H
from fluxplace import hrgeom as G
from fluxplace import procs

CU = ["F.Cu", "In1.Cu", "B.Cu"]


class FakeM:
    """The part of hrgeom.Model that _free_fields reads."""
    cu = CU
    hole_clr = 0.2
    floors = {"clr": 0.15}

    def __init__(self, obs):
        self.obs = obs

    in_box = G.Model.in_box
    foreign = G.Model.foreign

    def need(self, net, ob, for_via=False):
        return max(self.floors["clr"], ob.clr)


def _obs(n, box, seed=7):
    r = random.Random(seed)
    x0, y0, x1, y1 = box
    out = [G.Ob("edge", "", CU, ("seg", a, b, 0.0), 0.3) for a, b in
           (((x0 + 1, y0 + 1), (x1 - 1, y0 + 1)), ((x1 - 1, y0 + 1), (x1 - 1, y1 - 1)), ((x1 - 1, y1 - 1), (x0 + 1, y1 - 1)), ((x0 + 1, y1 - 1), (x0 + 1, y0 + 1)))]
    for i in range(n):
        x, y = r.uniform(x0 - 2, x1 + 2), r.uniform(y0 - 2, y1 + 2)
        k = i % 5
        net = r.choice(["A", "B", "N", ""])
        if k == 0:
            out.append(G.Ob("track", net, [r.choice(CU)], ("seg", (x, y), (x + r.uniform(-4, 4), y + r.uniform(-4, 4)), 0.1), 0.2, 0.2, id=f"t{i}"))
        elif k == 1:
            out.append(G.Ob("via", net, CU, ("seg", (x, y), (x, y), 0.3), 0.2, 0.2, id=f"v{i}"))
        elif k == 2:
            w, h = r.uniform(0.3, 1.5), r.uniform(0.3, 1.5)
            lay = CU if i % 2 else [r.choice(CU)]
            out.append(G.Ob("pad", net, lay, ("poly", [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]), 0.2, 0.2, ref=f"P{i}.1", id=f"p{i}"))
        elif k == 3:
            out.append(G.Ob("hole", "", CU, ("seg", (x, y), (x, y), 0.4), 0.25 if i % 2 else 0.0, 0.3, extra={"npth": bool(i % 2)}))
        else:
            out.append(G.Ob("ko", "", [r.choice(CU)], ("poly", [(x, y), (x + 1, y), (x + 1, y + 2), (x, y + 2)]), desc="ko",
                            extra={"no_tracks": True, "no_vias": bool(i % 2), "no_pads": False}))
    return out


def _reference(M, net, bbox, pitch, skip=None):
    """The implementation before the fix: every obstacle on the whole grid, kept in a dict."""
    x0, y0, x1, y1 = bbox
    xs, ys, X, Y, pitch = G.grid(bbox, pitch)
    near = (x0 - 3, y0 - 3, x1 + 3, y1 + 3)
    slack_t, slack_v, slack_h = {}, np.full(X.shape, 9.0), np.full(X.shape, 9.0)
    cache = {}
    inb = np.zeros(X.shape, bool)
    for o in M.obs:
        if o.kind == "edge":
            (ax, ay), (bx, by) = o.g[1], o.g[2]
            if ay != by:
                inb ^= ((ay > Y) != (by > Y)) & (X < (bx - ax) * (Y - ay) / (by - ay) + ax)
    for lay in M.cu:
        st = np.full(X.shape, 9.0)
        sv = np.full(X.shape, 9.0)
        for o in M.in_box(near, lay):
            if skip and skip(o):
                continue
            for kind in ("track", "via"):
                if not M.foreign(o, net, kind):
                    continue
                if id(o) not in cache:
                    cache[id(o)] = G.ob_dist_np(X, Y, o)
                d = cache[id(o)]
                if o.kind == "hole":
                    if kind == "track" and o.extra.get("npth"):
                        st = np.minimum(st, d - o.clr)
                    continue
                need = 0.0 if o.kind == "ko" else (o.clr if o.kind == "edge" else M.need(net, o))
                if kind == "track":
                    st = np.minimum(st, d - need)
                else:
                    sv = np.minimum(sv, d - need)
                    if o.kind in ("pad", "track", "via"):
                        slack_h = np.minimum(slack_h, d - M.hole_clr)
            if o.kind == "hole" and lay == M.cu[0]:
                d = cache.setdefault(id(o), G.ob_dist_np(X, Y, o))
                slack_h = np.minimum(slack_h, d - o.clr_hole)
        slack_t[lay] = np.where(inb, st, -9.0)
        slack_v = np.minimum(slack_v, np.where(inb, sv, -9.0))
    return xs, ys, pitch, slack_t, slack_v, slack_h


@pytest.mark.parametrize("box,pitch,skip", [((0, 0, 12, 9), 0.05, None), ((0, 0, 40, 30), 0.1, None),
                                             ((0, 0, 12, 9), 0.05, lambda o: o.kind == "track" and o.net == "B")])
def test_fields_equal_the_full_grid_version(box, pitch, skip):
    M = FakeM(_obs(150, box))
    new = H._free_fields(M, "N", CU, box, 0.2, pitch, skip=skip)
    old = _reference(M, "N", box, pitch, skip=skip)
    assert new[2] == old[2] and np.array_equal(new[0], old[0]) and np.array_equal(new[1], old[1])
    for lay in CU:
        assert np.array_equal(new[3][lay], old[3][lay]), lay
    assert np.array_equal(new[4], old[4]) and np.array_equal(new[5], old[5])
    assert (new[3]["F.Cu"] > 0).any() and (new[3]["F.Cu"] < 0).any()          # the scene has both free room and copper


def test_memory_does_not_grow_with_the_obstacle_count():
    box = (0, 0, 64, 68)                                                     # the window that blew up: grid capped at 450 k cells
    cells = G.grid(box, 0.05)[2].size
    peak = {}
    for n in (200, 1600):
        M = FakeM(_obs(n, box))
        tracemalloc.start()
        H._free_fields(M, "N", CU, box, 0.2, 0.05)
        peak[n] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    budget = 40 * cells * 8                                                   # a fixed number of grid arrays, whatever the count
    assert peak[1600] < budget, peak                                         # before: 1600 obstacles x 3.6 MB = 5.8 GB
    assert peak[1600] < 1.5 * peak[200] + 8e6, peak


HOG = "import time\nb = bytearray(%d * 1048576)\nfor i in range(0, len(b), 4096):\n    b[i] = 1\nprint('held', flush=True)\ntime.sleep(%d)\n"


def test_run_mem_cap_kills_the_tree_and_reports(monkeypatch):
    monkeypatch.setattr(procs, "MEM_POLL_S", 0.1)
    with pytest.raises(procs.MemoryExceeded) as e:
        procs.run([sys.executable, "-c", HOG % (300, 60)], capture_output=True, text=True, timeout=30, mem_cap_mb=120)
    assert e.value.rss_mb > 120 and e.value.cap_mb == 120 and "memory" in str(e.value)
    p = procs.run([sys.executable, "-c", HOG % (20, 0)], capture_output=True, text=True, timeout=30, mem_cap_mb=4000)
    assert p.returncode == 0 and "held" in p.stdout                         # under the cap: an ordinary run


def test_tree_rss_counts_descendants():
    kid = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {HOG % (150, 4)!r}], start_new_session=True)"
    p = procs.popen([sys.executable, "-c", kid])
    try:
        import time
        seen = 0.0
        for _ in range(60):
            seen = max(seen, procs.tree_rss_mb(p.pid))
            if seen > 150:
                break
            time.sleep(0.1)
        assert seen > 150, seen                                              # the grandchild in its own session is counted
    finally:
        procs._reap_proc(p, 0.5)


def test_rung_over_the_cap_is_a_rung_failure(monkeypatch, tmp_path):
    def over(cmd, **kw):
        assert kw["mem_cap_mb"] == 2048.0                                    # [flow] rung_mem_gb reaches the launcher
        raise procs.MemoryExceeded(cmd, 2300.0, kw["mem_cap_mb"], stderr="last words")
    monkeypatch.setattr(gaps._procs, "run", over)
    ctx = {"cons": {"flow": {"rung_mem_gb": 2}}, "budget_s": 5, "work": str(tmp_path)}
    with pytest.raises(CX.RungFailure) as e:
        gaps._call_rung("krt", str(tmp_path / "a.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), [], ctx)
    assert e.value.outcome == "memory" and "rung failed: memory" in str(e.value) and e.value.stderr_tail == "last words"
    assert "memory" in CX.OUTCOMES and "memory" in CX.CRASH_OUTCOMES
    assert not (tmp_path / "o.kicad_pcb.job.json").exists()


def test_rung_mem_cap_config():
    assert gaps._rung_mem_mb({"flow": {"rung_mem_gb": 0}}) is None
    assert gaps._rung_mem_mb({"flow": {"rung_mem_gb": 0.5}}) == 512.0
    assert 0 < gaps._rung_mem_mb({}) <= gaps.RUNG_MEM_GB * 1024
