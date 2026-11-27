"""Differential-pair LENGTH TUNING with a DRC guard.

`repair.redundant_copper` removes loops — copper that is not on the shortest
existing path. It cannot fix a HAIRPIN (the path wanders 16 mm east and comes
back 1 mm from where it left, with no copper joining the two ends) or a pair
whose two sides simply took different routes. Those need new copper:

  shortcut   two points of a net's path on the same layer lie within
             `max_gap` of each other with a long detour between them ->
             replace the detour with one straight segment.
  meander    the shorter side of a pair gets a rectangular serpentine on its
             longest straight run, sized to the missing length.

Every change is made on a copy, DRC'd with kicad-cli, and accepted only if
the violation count did not rise — the same guard the last-mile patcher
uses. A change DRC rejects is simply not made; the report says so.
"""
import json
import math
import re
import os
import shutil
import subprocess
import signal
import threading
import tempfile
import time

from . import geom as GM
from . import repair as RP
from . import drcguard as DG

__all__ = ["drc_counts", "shortcut", "meander", "tune_pairs", "meander_to", "tune_match", "bump_plan",
           "Budget", "Stopped", "Stalled", "default_budget", "classify_pairs", "cleanup_work", "PER_PAIR_S", "STALL_TRIES"]


# ------------------------------------------------------------------- DRC
def drc_counts(board_path, kicad_cli="kicad-cli", timeout=600):
    """(violations, unconnected) from a kicad-cli DRC at all severities."""
    from . import drcguard as DG
    d = DG.drc_json(board_path, kicad_cli, timeout)
    return len(d.get("violations", [])), len(d.get("unconnected_items", []))


_MASK_POLY = re.compile(r"^Polygon on \S+\.Mask")


def _mask_poly(v):
    """A solder_mask_bridge violation that names a mask polygon (its item set changes run to run)."""
    return v.get("type") == "solder_mask_bridge" and any(_MASK_POLY.search(str(i.get("description", "")))
                                                         for i in v.get("items") or [])


def _mask_key(v):
    """(type, net of the other item): one mask polygon flickers between item pairs, the net does not."""
    other = [i for i in v.get("items") or [] if not _MASK_POLY.search(str(i.get("description", "")))]
    nets = [m.group(1) for i in other for m in [DG._NET.search(str(i.get("description", "")))] if m]
    return (str(v.get("type", "")), tuple(nets) or ("?",))


def drc_report(board_path, kicad_cli="kicad-cli", timeout=600):
    """(keys, (violations, unconnected)): see fluxplace/drcguard.py for the identity and noise rule. PHASE3G G2a:
    opens keyed per net (DG.keys_per_net) and mask-polygon bridges keyed by (type, net of the other item), so the
    acceptance guard does not reject a step for key flicker."""
    d = DG.drc_json(board_path, kicad_cli, timeout)
    keys = DG.keys_per_net(d, keep=lambda v: not _mask_poly(v))
    for v in d.get("violations", []):
        if _mask_poly(v):
            keys[_mask_key(v)] += 1
    return keys, (len(d.get("violations", [])), len(d.get("unconnected_items", [])))


# ------------------------------------------------------- budget, stall, clean exit
PER_PAIR_S = 90         # default budget = selected pairs x this (one DRC run is 10-40 s on a dense board)
PER_GROUP_S = 180       # tune --match: per selected [match.*] group
STALL_TRIES = 8         # DRC-guarded tries in a row without an accepted step, then the pair is left as it is


class Stopped(Exception):
    """The run must stop now: reason is "budget" or "signal". The working file holds only ACCEPTED steps."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Stalled(Exception):
    """One pair made `stall` DRC-guarded tries without an accepted step."""


def default_budget(n_units, per_unit_s=PER_PAIR_S):
    """Seconds for `n_units` selected pairs (or groups); never below one unit's allowance."""
    return max(1, n_units) * per_unit_s


class Budget:
    """Wall-clock budget + signal stop. As a context manager (main thread only) SIGTERM / SIGINT raise Stopped("signal") and
    SIGALRM at the deadline raises Stopped("budget"), so a long DRC subprocess is killed at once; check() is the polling form
    used everywhere else (and in threads). A second signal while stopping is ignored, so the clean-up cannot be interrupted."""
    def __init__(self, seconds=None, stall=STALL_TRIES, clock=time.monotonic):
        self.seconds = float(seconds) if seconds else None
        self.stall = int(stall)
        self.clock = clock
        self.t0 = clock()
        self.reason = None
        self._old = {}

    def elapsed(self):
        return self.clock() - self.t0

    def remaining(self):
        return None if self.seconds is None else self.seconds - self.elapsed()

    def drc_timeout(self, cap=600, floor=120.0):
        """A DRC the pass needs always gets `floor` s (a spent budget used to give 5 s: kicad-cli then timed out and the rung crashed)."""
        r = self.remaining()
        return cap if r is None else max(floor, min(cap, r + 5.0))

    def check(self):
        if self.reason:
            raise Stopped(self.reason)
        r = self.remaining()
        if r is not None and r <= 0:
            self.reason = "budget"
            raise Stopped("budget")

    def on_signal(self, signum, _frame=None):
        if self.reason:
            return
        self.reason = "budget" if signum == getattr(signal, "SIGALRM", None) else "signal"
        raise Stopped(self.reason)

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            for sg in (signal.SIGTERM, signal.SIGINT) + ((signal.SIGALRM,) if self.seconds and hasattr(signal, "SIGALRM") else ()):
                self._old[sg] = signal.signal(sg, self.on_signal)
            if self.seconds and hasattr(signal, "setitimer"):
                signal.setitimer(signal.ITIMER_REAL, max(0.01, self.remaining()))
        return self

    def __exit__(self, *_exc):
        if self._old:
            if hasattr(signal, "setitimer"):
                signal.setitimer(signal.ITIMER_REAL, 0)
            for sg, h in self._old.items():
                signal.signal(sg, h)
            self._old = {}
        return False


def _lock_of(path):
    return os.path.join(os.path.dirname(path), "~" + os.path.basename(path) + ".lck")


def lock_tuned(path, pairs):
    """PHASE3E 8: the tuned pairs' copper is locked in `path` (later stages route around it). The one place both the
    `tune` CLI and the krt stage (pipeline.st_tune) lock; pairs = {master: slave}. -> locked segment/via/arc count."""
    from . import boardtext as BT
    src = open(path, errors="surrogateescape").read()
    src, n = BT.lock_nets(src, set(pairs) | set(pairs.values()))
    with open(path, "w", errors="surrogateescape") as fh:
        fh.write(src)
    return n


def cleanup_work(work):
    """Remove every temp file a tune run makes for `work` (the working copy, its .try copy, DRC reports, project sidecars and
    the KiCad lock files of those copies). Safe to call twice."""
    base = os.path.splitext(work)[0]
    tries = work + ".try.kicad_pcb"
    tb = os.path.splitext(tries)[0]
    for q in (work, tries, work + ".drc.json", tries + ".drc.json", _lock_of(work), _lock_of(tries),
              *(b_ + e for b_ in (base, tb) for e in (".kicad_pro", ".kicad_dru", ".kicad_prl"))):
        try:
            os.unlink(q)
        except OSError:
            pass


def _finalize(work, out_path):
    """The best accepted board becomes `out_path` (atomic replace), every temp file goes."""
    if os.path.exists(work):
        os.replace(work, out_path)
    cleanup_work(work)


def _profile(src, net, want_path=False):
    """pairaudit.profile of a board file path (or an already loaded routediag board dict)."""
    from . import routediag as RD, pairaudit as PA
    B = src if isinstance(src, dict) else RD.load(src)
    return PA.profile(B, net, want_path)


def measure_pair(path, B, master, slave, cons=None, limit_mm=None, kicad_cli="kicad-cli"):
    """D71cx: the graded skew of a pair on the board file `path` (netlen.pair_skew: KiCad's DRC lengths for a 2-terminal pair, per-orientation
    paths for a joined one) and the unit it is graded in.  -> dict
      ok, why        ok False = unmeasured / MEASURE_DISAGREE: the pair is not tuned and cannot pass
      lm, ls         KiCad's P and N net lengths (mm)
      skew, unit     signed P - N in the budget's unit: "ps" (delay skew, when the budget is in ps) or "mm"
      limit          the target in that unit (limit_mm(master) for mm budgets, the ps target for ps budgets); None = not graded
      eq_mm, lim_eq  the same skew and limit as mm of the shorter side's copper, which is what a meander adds (ps / mean ps per mm)
      src            basis: kicad | path"""
    from . import netlen, pairaudit as PA
    B = dict(B, file=path)
    psb = PA.ps_budget(cons, master) if cons is not None else None
    ps = netlen.pair_skew(B, master, slave, cons, kicad_cli=kicad_cli, grade_ps=bool(psb))
    out = {"ok": ps["status"] == "ok", "why": ps["why"], "lm": ps["p_mm"], "ls": ps["n_mm"], "src": ps["basis"], "status": ps["status"]}
    if not out["ok"]:
        return out
    if psb:
        if ps["skew_ps"] is None:
            return dict(out, ok=False, why="PAIR_DELAY_UNKNOWN (a layer without a reference plane)")
        k = ((ps["p_ps"] / ps["p_mm"]) + (ps["n_ps"] / ps["n_mm"])) / 2 if ps["p_mm"] and ps["n_mm"] else None
        if not k:                                              # no delay per mm from the stackup: no built-in constant, the pair is unmeasured
            return dict(out, ok=False, why="PAIR_DELAY_UNKNOWN (no ps per mm from the stackup)")
        out.update(skew=ps["skew_ps"], unit="ps", limit=psb[0], eq_mm=ps["skew_ps"] / k, lim_eq=psb[0] / k)
    else:
        lim = limit_mm(master) if callable(limit_mm) else limit_mm
        out.update(skew=ps["skew_mm"], unit="mm", limit=lim, eq_mm=ps["skew_mm"], lim_eq=lim)
    return out


def path_len(src, net):
    """PHASE3G G1b c: the pad-to-pad path length of `net` (pairaudit's number, the one the audit and review grade).
    None when the net has no path between its two farthest pads (a leg open): the caller must not tune it."""
    p = _profile(src, net)
    return p["length"] if p["path"] else None


def seg_key(a, b, layer):
    """Identity of a track segment that does not depend on the reader: the rounded (mm, 3 dp) ends, unordered, and layer."""
    ends = sorted([(round(a[0], 3), round(a[1], 3)), (round(b[0], 3), round(b[1], 3))])
    return (ends[0], ends[1], layer)


def path_segs(src, net):
    """Set of seg_key of the track segments on `net`'s pad-to-pad path (pairaudit's walk), None when there is no path.
    meander_to / entry_jog take it as `on_path`: a stub or branch of the net is never lengthened (or it adds no path length)."""
    B = src if isinstance(src, dict) else None
    if B is None:
        from . import routediag as RD
        B = RD.load(src)
    p = _profile(B, net, want_path=True)
    if not p["path"]:
        return None
    return {seg_key(B["segs"][i]["a"], B["segs"][i]["b"], B["segs"][i]["layer"]) for i in p["segs"]}


def classify_pairs(board_path, pairs, allow_mismatch=False):
    """{master: reason} for the pairs {slave: master} whose legs cannot be tuned: a leg open, or the legs run through
    different layer sequences / via counts (pairaudit.profile). Length tuning cannot fix those; they need a re-route
    (`fluxplace pairs --only`)."""
    from . import routediag as RD, pairaudit as PA
    B = RD.load(board_path)
    out = {}
    for slave, master in pairs.items():
        pm, ps = PA.profile(B, master), PA.profile(B, slave)
        if not pm["path"] or not ps["path"]:
            out[master] = f"leg open: {master if not pm['path'] else slave} has no path between its pads"
            continue
        why = []
        if pm["layers"] != ps["layers"]:
            why.append(f"layers {'>'.join(pm['layers'])} vs {'>'.join(ps['layers'])}")
        if pm["vias"] != ps["vias"]:
            why.append(f"vias {pm['vias']} vs {ps['vias']}")
        if why and not allow_mismatch:                    # allow_mismatch: legs on different layers/vias are length-tuned anyway (last resort after a re-lay failed)
            out[master] = "; ".join(why)
    return out


# ------------------------------------------------------------------ paths
def _ordered_path(board, netcode):
    """[(node, item)] along the shortest path between the net's two most
    distant pads (a pair through an ESD array or a series element has more
    than two pads; the long run is still the one that matters), or None."""
    adj, pads = RP._net_graph(board, netcode)
    if len(pads) < 2:
        return None
    import heapq
    if len(pads) == 2:
        src, dst = pads
    else:
        # farthest pair by path length
        best = None
        for i in range(len(pads)):
            d0 = _dist_from(adj, pads[i])
            for j in range(i + 1, len(pads)):
                dd = d0.get(pads[j])
                if dd is not None and (best is None or dd > best[0]):
                    best = (dd, pads[i], pads[j])
        if not best:
            return None
        src, dst = best[1], best[2]
    dist, prev, pq, n = {src: 0.0}, {}, [(0.0, 0, src)], 0
    while pq:
        d, _, u = heapq.heappop(pq)
        if u == dst:
            break
        if d > dist.get(u, 1e18):
            continue
        for v, w, item in adj.get(u, []):
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v] = nd
                prev[v] = (u, item)
                n += 1
                heapq.heappush(pq, (nd, n, v))
    if dst not in dist:
        return None
    path, u = [], dst
    while u != src:
        p, item = prev[u]
        path.append((u, item))
        u = p
    path.append((src, None))
    path.reverse()
    return path


def _dist_from(adj, src):
    import heapq
    dist, pq, n = {src: 0.0}, [(0.0, 0, src)], 0
    while pq:
        d, _, u = heapq.heappop(pq)
        if d > dist.get(u, 1e18):
            continue
        for v, w, _item in adj.get(u, []):
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v] = nd
                n += 1
                heapq.heappush(pq, (nd, n, v))
    return dist


def shortcut(board, net, max_gap_mm=1.5, min_saving_mm=3.0, log=print):
    """Bridge the biggest hairpin on `net` with one segment. Returns saving
    in mm (0.0 when nothing qualified)."""
    import pcbnew
    codes = {t.GetNetname(): t.GetNetCode() for t in board.GetTracks()}
    code = codes.get(net)
    if not code:
        return 0.0
    path = _ordered_path(board, code)
    if not path:
        return 0.0
    pts = [(k, item) for k, item in path if isinstance(k, tuple) and k and isinstance(k[0], float)]
    # cumulative distance along the path
    cum = [0.0]
    for i in range(1, len(pts)):
        a, b = pts[i - 1][0], pts[i][0]
        cum.append(cum[-1] + math.hypot(a[0] - b[0], a[1] - b[1]))
    best = None
    for i in range(len(pts)):
        for j in range(i + 2, len(pts)):
            a, b = pts[i][0], pts[j][0]
            if a[2] != b[2]:
                continue
            gap = math.hypot(a[0] - b[0], a[1] - b[1])
            if gap > max_gap_mm:
                continue
            saving = cum[j] - cum[i] - gap
            if saving >= min_saving_mm and (best is None or saving > best[0]):
                best = (saving, i, j)
    if not best:
        return 0.0
    saving, i, j = best
    a, b = pts[i][0], pts[j][0]
    width = None
    victims = []
    for k in range(i + 1, j + 1):
        item = pts[k][1]
        if item is not None and item not in victims:
            victims.append(item)
            if item.GetClass() == "PCB_TRACK":
                width = item.GetWidth()
    if any(it.IsLocked() for it in victims):          # locked copper (tuned pairs, fan-out, RF) is never shortcut
        return 0.0
    for it in victims:
        RP._remove(board, it)
    seg = pcbnew.PCB_TRACK(board)
    seg.SetStart(pcbnew.VECTOR2I(int(a[0] * 1e6), int(a[1] * 1e6)))
    seg.SetEnd(pcbnew.VECTOR2I(int(b[0] * 1e6), int(b[1] * 1e6)))
    seg.SetLayer(a[2])
    seg.SetWidth(width or pcbnew.FromMM(0.15))
    seg.SetNetCode(code)
    board.Add(seg)
    log(f"    {net}: shortcut on {board.GetLayerName(a[2])} saves {saving:.1f} mm "
        f"({len(victims)} items replaced)")
    return saving


def _straight_candidates(board, net, min_len_mm):
    """Straight segments long enough for the meander, longest first."""
    out = []
    for t in board.GetTracks():
        if t.GetNetname() != net or t.GetClass() != "PCB_TRACK":
            continue
        L = t.GetLength() / 1e6
        if L >= min_len_mm:
            out.append((L, t))
    out.sort(key=lambda x: -x[0])
    return [t for _, t in out]


def meander(board, net, add_mm, amp_mm=0.6, pitch_mm=0.7, margin_mm=0.6,
            candidate=0, log=print):
    """Replace the `candidate`-th longest straight segment of `net` with a
    serpentine that adds `add_mm`. Returns the length actually added (0.0
    if no such segment)."""
    import pcbnew
    per_period = 4.0 * amp_mm
    periods = max(1, int(math.ceil(add_mm / per_period)))
    run = periods * pitch_mm + 2 * margin_mm
    cands = _straight_candidates(board, net, run)
    if candidate >= len(cands):
        return 0.0
    seg = cands[candidate]
    s, e = seg.GetStart(), seg.GetEnd()
    sx, sy, ex, ey = s.x / 1e6, s.y / 1e6, e.x / 1e6, e.y / 1e6
    L = math.hypot(ex - sx, ey - sy)
    ux, uy = (ex - sx) / L, (ey - sy) / L
    nx, ny = -uy, ux
    start = margin_mm + (L - run) / 2.0
    pts = [(sx, sy)]
    d = start
    pts.append((sx + ux * d, sy + uy * d))
    sign = 1
    for _ in range(periods):
        # up, across, down = one period of a rectangular meander
        x, y = pts[-1]
        pts.append((x + nx * amp_mm * sign, y + ny * amp_mm * sign))
        x, y = pts[-1]
        pts.append((x + ux * pitch_mm, y + uy * pitch_mm))
        x, y = pts[-1]
        pts.append((x - nx * amp_mm * sign, y - ny * amp_mm * sign))
        sign = -sign
    pts.append((ex, ey))
    width, layer, code = seg.GetWidth(), seg.GetLayer(), seg.GetNetCode()
    RP._remove(board, seg)
    added = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if abs(x1 - x0) < 1e-6 and abs(y1 - y0) < 1e-6:
            continue
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(int(round(x0 * 1e6)), int(round(y0 * 1e6))))
        t.SetEnd(pcbnew.VECTOR2I(int(round(x1 * 1e6)), int(round(y1 * 1e6))))
        t.SetLayer(layer)
        t.SetWidth(width)
        t.SetNetCode(code)
        board.Add(t)
        added += math.hypot(x1 - x0, y1 - y0)
    added -= L
    log(f"    {net}: meander of {periods} period(s) on {board.GetLayerName(layer)} "
        f"adds {added:.1f} mm (asked {add_mm:.1f})")
    return added


# --------------------------------------------------------------- driver
def _guarded(board_path, work, mutate, kicad_cli, base, log, stats=None, bud=None):
    """tune_pairs / tune_match acceptance: base = (keys, counts) from drc_report. With a Budget the DRC is bounded by it and
    its poll runs first; a DRC that outlives the budget is Stopped("budget")."""
    if bud is None:
        return DG.guard_board(board_path, work, mutate, base, kicad_cli, log, stats,
                              report=lambda p, c: drc_report(p, c))
    bud.check()

    def rep(p, c):
        try:
            return drc_report(p, c, timeout=bud.drc_timeout()) if bud.seconds else drc_report(p, c)
        except subprocess.TimeoutExpired:
            bud.reason = bud.reason or "budget"
            raise Stopped(bud.reason)
    return DG.guard_board(board_path, work, mutate, base, kicad_cli, log, stats, report=rep)


def _copy_sidecars(board_path, work):
    for ext in (".kicad_pro", ".kicad_dru"):
        side = os.path.splitext(board_path)[0] + ext
        if os.path.exists(side):
            shutil.copy(side, os.path.splitext(work)[0] + ext)


def tune_pairs(board_path, out_path, pairs, limit_mm, kicad_cli="kicad-cli",
               amp_mm=0.6, max_rounds=6, log=print, budget=None, stall=STALL_TRIES, result=None, cons=None, allow_mismatch=False):
    """pairs: {slave: master}. limit_mm: float or callable(master). Works on files so every step is a fresh pcbnew session
    (SWIG decays otherwise). Returns [(master, slave, P mm, N mm)].

    budget: seconds, or a Budget; None = unbounded. stall: guarded tries in a row without an accepted step before a pair is
    left as it is. When the budget runs out or SIGTERM / SIGINT arrives, the best ACCEPTED board so far is written to
    out_path (the input when nothing was accepted) and every temp file and lock file is removed. `result` (a dict) is filled:
    {stopped: None|"budget"|"stall"|"signal", elapsed_s, budget_s, pairs: [{pair, p_mm, n_mm, skew, limit, status, reason}]},
    status ok | over | needs_reroute | not_reached. A pair whose legs differ in layer sequence or via count (or are open) is
    needs_reroute: no tries are spent on it.
    cons: the constraints. With them a pair whose budget is in ps is tuned to equal DELAY (D71cx: its skew, limit and the length a meander
    adds are measure_pair's ps numbers); without them, or for an mm budget, the skew is KiCad's length difference."""
    import pcbnew
    bud = budget if isinstance(budget, Budget) else Budget(budget, stall)
    work = out_path + ".work.kicad_pcb"
    lim = limit_mm if callable(limit_mm) else (lambda m: limit_mm)
    stalls, reached, need, first = set(), set(), {}, {}
    stopped = None
    accepted = 0
    base = None
    try:
        with bud:
            shutil.copy(board_path, work)
            _copy_sidecars(board_path, work)
            need = classify_pairs(board_path, pairs, True) if allow_mismatch else classify_pairs(board_path, pairs)
            for master, why in need.items():
                log(f"  {master}: needs_reroute ({why}); not tuned")
            base = drc_report(work, kicad_cli, timeout=bud.drc_timeout()) if bud.seconds else drc_report(work, kicad_cli)
            log(f"  baseline DRC: {base[1][0]} violations, {base[1][1]} unconnected")
            stats = {}

            def attempt(mutate, tries):
                nonlocal base, accepted
                bud.check()
                if tries[0] >= bud.stall:
                    raise Stalled()
                n0 = stats.get("drc_runs", 0)
                ok, base = _guarded(board_path, work, mutate, kicad_cli, base, log, stats, bud)
                accepted += bool(ok)
                tries[0] = 0 if ok else tries[0] + (stats.get("drc_runs", 0) - n0)
                return ok

            for slave, master in sorted(pairs.items()):
                if master in need:
                    continue
                tries = [0]
                reached.add(master)
                try:
                    for rnd in range(max_rounds):
                        B = _load_routediag(work)
                        if not (_profile(B, master)["path"] and _profile(B, slave)["path"]):
                            log(f"    {master}/{slave}: a leg has no pad-to-pad path: not tuned")
                            break
                        m = measure_pair(work, B, master, slave, cons, lim, kicad_cli)
                        if not m["ok"]:
                            log(f"    {master}/{slave}: {m['why']}: not tuned")
                            break
                        skew = m["skew"]
                        first.setdefault(master, abs(skew))
                        if abs(skew) <= m["limit"]:
                            break
                        longer, shorter = (master, slave) if skew > 0 else (slave, master)
                        on_path = path_segs(B, shorter)
                        log(f"  {master}/{slave}: skew {skew:+.2f} {m['unit']} (limit {m['limit']:.3g}) round {rnd + 1}")
                        ok = False
                        for gap in (1.5, 3.0):                      # 1. shorten the long side
                            ok = attempt(lambda bd, g=gap: shortcut(bd, longer, max_gap_mm=g, log=log) > 0, tries)
                            if ok:
                                break
                        if ok:
                            continue
                        need_mm = abs(m["eq_mm"]) - 0.5 * m["lim_eq"]   # 2. lengthen the short side: land inside the tolerance
                        for cand in range(6):
                            for amp, pitch in ((amp_mm, 0.7), (amp_mm * 0.6, 0.5), (0.35, 0.45)):
                                ok = attempt(lambda bd, c=cand, a=amp, p=pitch: meander_to(
                                    bd, shorter, need_mm, amp_mm=a, period_mm=p, candidate=c, log=log,
                                    clearance_mm=_min_clear(bd), away_from=longer, locked_ok=True,
                                    on_path=on_path) > 0, tries)
                                if ok:
                                    break
                            if ok:
                                break
                        if not ok:                                   # no straight has room: a pad-entry jog
                            ok = attempt(lambda bd: entry_jog(bd, shorter, need_mm, away_from=longer,
                                                              clearance_mm=_min_clear(bd), locked_ok=True,
                                                              log=log, on_path=on_path) > 0, tries)
                        if ok:
                            continue
                        log(f"    {master}/{slave}: no DRC-clean tuning found — left at {skew:+.2f} {m['unit']}")
                        break
                except Stalled:
                    stalls.add(master)
                    log(f"    {master}/{slave}: stalled after {bud.stall} tries without an accepted step")
    except Stopped as e:
        stopped = e.reason
        log(f"  stopped ({stopped}): writing the best accepted board")
    except BaseException:
        cleanup_work(work)                                           # an error never leaves temp files behind
        raise
    if not os.path.exists(work):                                     # stopped while copying: the input is the result
        shutil.copy(board_path, work)
    _finalize(work, out_path)
    if os.path.abspath(out_path) != os.path.abspath(board_path):   # the output carries the input's project rules: a later
        _copy_sidecars(board_path, out_path)                       # pcbnew save would otherwise write a default .kicad_pro
    if stopped is None and stalls and set(reached) <= stalls:      # PHASE3G G2c: one stalled pair does not end the run
        stopped = "stall"
    report = []
    B_out = _load_routediag(out_path)
    out_rows = []
    for slave, master in sorted(pairs.items()):
        m = measure_pair(out_path, B_out, master, slave, cons, lim, kicad_cli)
        lm, ls = m["lm"], m["ls"]
        report.append((master, slave, lm if lm is not None else 0.0, ls if ls is not None else 0.0))
        limit = m.get("limit", lim(master))
        row = {"pair": f"{master}/{slave}", "p_mm": None if lm is None else round(lm, 3), "n_mm": None if ls is None else round(ls, 3),
               "skew": round(m["skew"], 3) if m["ok"] else None, "unit": m.get("unit", "mm"),
               "before": round(first[master], 3) if master in first else None,
               "limit": limit if limit is not None and limit != float("inf") else None, "reason": ""}
        if master in need:
            row.update(status="needs_reroute", reason=need[master])
        elif not m["ok"]:
            row.update(status="unmeasured", reason=m["why"])
        elif abs(m["skew"]) <= limit:
            row["status"] = "ok"
        elif master not in reached and stopped in ("budget", "signal"):
            row.update(status="not_reached", reason=stopped)
        else:
            row.update(status="over", reason=("stall" if master in stalls else stopped or "no DRC-clean tuning found"))
        out_rows.append(row)
    if base is not None:
        log(f"  final DRC: {base[1][0]} violations, {base[1][1]} unconnected")
    if result is not None:
        result.update(stopped=stopped, elapsed_s=round(bud.elapsed(), 1), budget_s=bud.seconds, pairs=out_rows,
                      out=out_path, accepted=accepted, needs_reroute=sum(1 for r in out_rows if r["status"] == "needs_reroute"))
    return report


# ------------------------------------------------------- length matching
def _bump_polyline(L, n, amp, period, side=None):
    """Local polyline (s along the segment, t across) of n rectangular bumps centred on a segment of length L.
    Bump i rises on its left leg at s_i, runs across its width (period / 2) and drops on its right leg; the copper
    between bumps is a straight run along the axis, so no leg reverses direction (no acute tip for DRC).
    side = +1 / -1: every bump on that side of the axis (PHASE3G G2b); None: the sides alternate."""
    w = period / 2.0
    s0 = (L - n * period) / 2.0
    pts = [(0.0, 0.0), (s0, 0.0)]
    for i in range(n):
        sg = (1.0 if i % 2 == 0 else -1.0) if side is None else float(side)
        x0 = s0 + i * period
        x1 = x0 + w
        pts += [(x0, 0.0), (x0, sg * amp), (x1, sg * amp), (x1, 0.0)]
    pts.append((L, 0.0))
    return [q for k, q in enumerate(pts) if k == 0 or q != pts[k - 1]]


def bump_plan(L, need_mm, amp_max, period, margin):
    """(n, amp) of accordion bumps adding `need_mm` on a segment of length L, or None when no room.
    Each bump adds exactly 2 * amp (two legs), so n = ceil(need / (2 amp_max)) and amp = need / (2 n)."""
    if need_mm <= 0:
        return None
    n = max(1, int(math.ceil(need_mm / (2.0 * amp_max) - 1e-9)))
    a = need_mm / (2.0 * n)
    if n * period + 2 * margin > L:
        return None
    return n, a


def _partner_side(board, layer, away_from, mid, nvec):
    """PHASE3G G2b: the side (+1 / -1 along nvec) that points AWAY from the nearest copper of net `away_from` on
    `layer` (its track midpoint nearest to `mid`). +1 when that net has no copper there."""
    best = None
    for t in board.GetTracks():
        if t.GetNetname() != away_from or t.GetClass() != "PCB_TRACK" or t.GetLayer() != layer:
            continue
        a, b = t.GetStart(), t.GetEnd()
        q = ((a.x + b.x) / 2e6, (a.y + b.y) / 2e6)
        d = math.hypot(q[0] - mid[0], q[1] - mid[1])
        if best is None or d < best[0]:
            best = (d, q)
    if best is None:
        return 1.0
    off = (best[1][0] - mid[0]) * nvec[0] + (best[1][1] - mid[1]) * nvec[1]
    return -1.0 if off > 0 else 1.0


def _min_clear(board):
    """The board's minimum copper clearance (mm), or None when this pcbnew does not report it."""
    try:
        return float(board.GetDesignSettings().m_MinClearance) / 1e6 or None
    except Exception:                                    # noqa: BLE001 - no design-settings access
        return None


def _arc_samples(a, m, b, n=16):
    """Points along the circle arc a -> m -> b (three points on it), n chords. Straight-line
    fallback when the three points are collinear."""
    ax, ay = a; bx, by = m; cx, cy = b
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [a, b]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    t0 = math.atan2(ay - uy, ax - ux)
    tm = math.atan2(by - uy, bx - ux)
    t1 = math.atan2(cy - uy, cx - ux)
    # sweep from t0 to t1 in the direction that passes through tm
    def norm(t):
        return t % (2 * math.pi)
    fwd = norm(tm - t0) <= norm(t1 - t0)
    sweep = norm(t1 - t0) if fwd else -(2 * math.pi - norm(t1 - t0))
    return [(ux + r * math.cos(t0 + sweep * k / n), uy + r * math.sin(t0 + sweep * k / n))
            for k in range(n + 1)]


def _obstacles(board, layer, net_code, clear_mm):
    """Copper/keepout obstacles on `layer` that are not `net_code`: (polyline-or-polygon pts, half width).
    Bounding-box prefiltering is left to the caller; this is the complete list."""
    import pcbnew
    obs = []
    for t in board.GetTracks():
        if t.GetNetCode() == net_code:
            continue
        cls = t.GetClass()
        if cls == "PCB_VIA":
            if not t.IsOnLayer(layer):
                continue
            p = t.GetPosition()
            obs.append(([(p.x / 1e6, p.y / 1e6)], t.GetWidth(layer) / 2e6))
        elif cls in ("PCB_TRACK", "PCB_ARC") and t.GetLayer() == layer:
            a, b = t.GetStart(), t.GetEnd()
            pts = [(a.x / 1e6, a.y / 1e6), (b.x / 1e6, b.y / 1e6)]
            if cls == "PCB_ARC":                       # two chords through the midpoint: never shorter than the arc
                m = t.GetMid()
                arc = _arc_samples(pts[0], (m.x / 1e6, m.y / 1e6), pts[1])
                for u, v in zip(arc, arc[1:]):
                    obs.append(([u, v], t.GetWidth() / 2e6))
            else:
                obs.append((pts, t.GetWidth() / 2e6))
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() == net_code or not pad.IsOnLayer(layer):
                continue
            bb = pad.GetBoundingBox()                  # bounding box: conservative (never under-reports)
            x0, y0, x1, y1 = bb.GetLeft() / 1e6, bb.GetTop() / 1e6, bb.GetRight() / 1e6, bb.GetBottom() / 1e6
            obs.append(([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], 0.0))
    for d in board.GetDrawings():                      # board outline: the candidate must stay inside it
        if d.GetLayer() == pcbnew.Edge_Cuts and d.GetClass() == "PCB_SHAPE" \
                and d.GetShape() == pcbnew.SHAPE_T_SEGMENT:
            a, b = d.GetStart(), d.GetEnd()
            obs.append(([(a.x / 1e6, a.y / 1e6), (b.x / 1e6, b.y / 1e6)], 0.0))
    for z in board.Zones():                            # keep-out (rule area) zones on this layer
        if z.GetIsRuleArea() and z.GetLayerSet().Contains(layer):
            ol = z.Outline().Outline(0)
            poly = [(ol.CPoint(i).x / 1e6, ol.CPoint(i).y / 1e6) for i in range(ol.PointCount())]
            if len(poly) >= 3:
                obs.append((poly, 0.0))
    return obs


def candidate_clear(board, pts, layer, net_code, width_mm, clear_mm):
    """In-process geometric pre-check of a candidate meander polyline (mm). True when every piece keeps
    `clear_mm` of copper gap to every other-net copper, pad (bounding box), keep-out zone and
    the board outline. Conservative: a False never needs a DRC run to confirm."""
    hw = width_mm / 2
    obs = _obstacles(board, layer, net_code, clear_mm)
    for a, b in zip(pts, pts[1:]):
        if abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9:
            continue
        xs, ys = (a[0], b[0]), (a[1], b[1])
        for opts, ohw in obs:
            # cheap bbox reject before the exact gap
            ox = [p[0] for p in opts]; oy = [p[1] for p in opts]
            reach = clear_mm + hw + ohw + 0.001
            if max(xs) + reach < min(ox) or min(xs) - reach > max(ox) or \
                    max(ys) + reach < min(oy) or min(ys) - reach > max(oy):
                continue
            if GM.core_gap([a, b], opts) - hw - ohw < clear_mm - 1e-9:
                return False
    return True


# ------------------------------------------------------------------ paths
def meander_to(board, net, need_mm, amp_mm=0.6, period_mm=0.8, margin_mm=0.6,
               candidate=0, log=print, clearance_mm=None, stats=None, away_from=None, locked_ok=False, on_path=None):
    """Lengthen `net` by exactly `need_mm` with accordion bumps on its `candidate`-th longest straight segment that
    has room (period_mm = 2 x bump width). Returns the length added (0.0 when no segment has room).
    PHASE3G G2b: away_from = a net name: every bump goes to the side away from that net's nearest copper (its
    partner leg), no alternation. locked_ok: locked copper of `net` is eligible (the pair being tuned); the new
    segments keep the lock flag of the segment they replace. Other nets' locks are never touched.
    PHASE3G G1b d/4: on_path = path_segs(...) of `net`: only segments on its pad-to-pad path are candidates (None = all)."""
    import pcbnew
    lockers = [t for t in board.GetTracks() if t.GetNetname() == net and t.GetClass() == "PCB_TRACK"
               and (locked_ok or not t.IsLocked()) and _on(board, t, on_path)]
    lockers.sort(key=lambda t: -t.GetLength())
    straights = [t for t in lockers
                 if bump_plan(t.GetLength() / 1e6, need_mm, amp_mm, period_mm, margin_mm) is not None]
    if candidate >= len(straights):
        return 0.0
    seg = straights[candidate]
    s, e = seg.GetStart(), seg.GetEnd()
    sx, sy, ex, ey = s.x / 1e6, s.y / 1e6, e.x / 1e6, e.y / 1e6
    L = math.hypot(ex - sx, ey - sy)
    n, a = bump_plan(L, need_mm, amp_mm, period_mm, margin_mm)
    ux, uy = (ex - sx) / L, (ey - sy) / L
    nx, ny = -uy, ux
    width, layer, code = seg.GetWidth(), seg.GetLayer(), seg.GetNetCode()
    side = None
    if away_from:
        side = _partner_side(board, layer, away_from, ((sx + ex) / 2, (sy + ey) / 2), (nx, ny))
    pts = [(sx + u * ux + t * nx, sy + u * uy + t * ny) for u, t in _bump_polyline(L, n, a, period_mm, side)]
    locked = seg.IsLocked()
    if clearance_mm is not None:                       # geometric pre-check: no DRC run for a bump that cannot pass
        if stats is not None:
            stats["geom_checked"] = stats.get("geom_checked", 0) + 1
        if not candidate_clear(board, pts, layer, code, width / 1e6, clearance_mm):
            if stats is not None:
                stats["geom_rejected"] = stats.get("geom_rejected", 0) + 1
            return 0.0
    RP._remove(board, seg)
    added = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(int(round(x0 * 1e6)), int(round(y0 * 1e6))))
        t.SetEnd(pcbnew.VECTOR2I(int(round(x1 * 1e6)), int(round(y1 * 1e6))))
        t.SetLayer(layer)
        t.SetWidth(width)
        t.SetNetCode(code)
        t.SetLocked(locked)
        board.Add(t)
        added += math.hypot(x1 - x0, y1 - y0)
    added -= L
    log(f"      {net}: accordion x{n} on {board.GetLayerName(layer)} adds {added:.3f} mm (asked {need_mm:.3f})")
    return added


def entry_jog(board, net, need_mm, away_from=None, clearance_mm=None, locked_ok=False, log=print, on_path=None):
    """PHASE3G G2b, last resort when no straight of `net` has room for a bump: on a leg's segment that ends at a
    pad or via of `net` (the uncoupled break-out part), replace the segment by a 45-degree dog-leg of offset h.
    The added length is 2 h (sqrt 2 - 1), so h = need / (2 (sqrt 2 - 1)). The pad/via end stays where it is.
    away_from: the dog-leg bends away from that net's copper. Returns the length added (0.0 when refused)."""
    import pcbnew
    h = need_mm / (2.0 * (math.sqrt(2.0) - 1.0))
    ends = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname() == net:
                p = pad.GetPosition()
                ends.append((p.x / 1e6, p.y / 1e6))
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA" and t.GetNetname() == net:
            p = t.GetPosition()
            ends.append((p.x / 1e6, p.y / 1e6))
    cands = []
    for t in board.GetTracks():
        if t.GetNetname() != net or t.GetClass() != "PCB_TRACK" or (t.IsLocked() and not locked_ok) \
                or not _on(board, t, on_path):
            continue
        s, e = t.GetStart(), t.GetEnd()
        S, E = (s.x / 1e6, s.y / 1e6), (e.x / 1e6, e.y / 1e6)
        L = math.hypot(E[0] - S[0], E[1] - S[1])
        if L < 2 * h + 0.3:                             # room for the two 45-degree legs and a straight run
            continue
        for T, O in ((E, S), (S, E)):
            if any(math.hypot(T[0] - q[0], T[1] - q[1]) < 0.02 for q in ends):
                cands.append((L, t, O, T))
                break
    cands.sort(key=lambda c: -c[0])
    for L, seg, O, T in cands:
        Lr = math.hypot(T[0] - O[0], T[1] - O[1])
        ux, uy = (T[0] - O[0]) / Lr, (T[1] - O[1]) / Lr
        nx, ny = -uy, ux
        side = 1.0
        if away_from:
            side = _partner_side(board, seg.GetLayer(), away_from, ((O[0] + T[0]) / 2, (O[1] + T[1]) / 2), (nx, ny))
        loc = [(0.0, 0.0), (h, side * h), (Lr - h, side * h), (Lr, 0.0)]
        pts = [(O[0] + x * ux + y * nx, O[1] + x * uy + y * ny) for x, y in loc]
        width, layer, code = seg.GetWidth(), seg.GetLayer(), seg.GetNetCode()
        if clearance_mm is not None and not candidate_clear(board, pts, layer, code, width / 1e6, clearance_mm):
            continue
        locked = seg.IsLocked()
        RP._remove(board, seg)
        added = 0.0
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(pcbnew.VECTOR2I(int(round(x0 * 1e6)), int(round(y0 * 1e6))))
            t.SetEnd(pcbnew.VECTOR2I(int(round(x1 * 1e6)), int(round(y1 * 1e6))))
            t.SetLayer(layer)
            t.SetWidth(width)
            t.SetNetCode(code)
            t.SetLocked(locked)
            board.Add(t)
            added += math.hypot(x1 - x0, y1 - y0)
        added -= Lr
        log(f"      {net}: entry jog h={h:.3f} on {board.GetLayerName(layer)} adds {added:.3f} mm (asked {need_mm:.3f})")
        return added
    return 0.0


def _on(board, t, on_path):
    """True when track `t` is on the path (on_path None = no restriction)."""
    if on_path is None:
        return True
    s, e = t.GetStart(), t.GetEnd()
    return seg_key((s.x / 1e6, s.y / 1e6), (e.x / 1e6, e.y / 1e6), board.GetLayerName(t.GetLayer())) in on_path


def _load_routediag(path):
    from . import routediag as RD
    return RD.load(path)


def _guarded_set(board_path, work, mutate, kicad_cli, base, log, stats, bud=None):
    """tune_match's acceptance (same rule as _guarded)."""
    return _guarded(board_path, work, mutate, kicad_cli, base, log, stats, bud)


def tune_match(board_path, out_path, groups_, net_lengths, targets, kicad_cli="kicad-cli",
               max_rounds=4, clearance_of=lambda net: 0.127, log=print, budget=None, stall=STALL_TRIES, result=None):
    """Lengthen the short members of each match group until they are within tolerance.

    groups_: lengthmatch.groups() entries (already selected for this run).
    net_lengths(board, group) -> {net: {"mm", "ps"}} for the group's members (measured on the working file).
    targets: {group name: (target, tolerance)} in the group's unit, fixed by the caller from the input board.
    clearance_of(net) -> mm: the default leg spacing is 3 x max(widest track of the net, clearance) unless the
    group sets spacing_mm (the centre-to-centre gap between adjacent legs, i.e. the bump width).
    Every bump is one DRC-guarded step (accepted only if violations and unconnected do not rise).
    budget / stall / result behave as in tune_pairs: a budget stop or SIGTERM / SIGINT writes the best ACCEPTED board to
    out_path and removes the temp files; result["stopped"] is None | "budget" | "stall" | "signal" (stall = at least one net
    made `stall` DRC-guarded tries in a row without an accepted bump). Nets not reached get status "not_reached".
    Returns [{group, net, before, after, target, tol, status, note}]."""
    import pcbnew
    bud = budget if isinstance(budget, Budget) else Budget(budget, stall)
    work = out_path + ".work.kicad_pcb"
    total_runs, report, stopped, base, base0, stalled = 0, [], None, None, None, False
    accepted = 0
    done = set()

    def tune_net(g, net, base):
        nonlocal accepted
        """-> (row, base); raises Stopped. A net that makes `stall` DRC-guarded tries in a row without an accepted bump stops."""
        target, tol = targets[g["name"]]
        unit = g["unit"]
        wb = pcbnew.LoadBoard(work)
        width = max([(t.GetWidth(pcbnew.F_Cu) if t.GetClass() == "PCB_VIA" else t.GetWidth()) / 1e6
                     for t in wb.GetTracks() if t.GetNetname() == net] or [0.0])
        spacing = g["spacing_mm"] or 3.0 * max(width, clearance_of(net))
        period = 2.0 * spacing                               # two legs per bump: leg gap = spacing
        row = {"group": g["name"], "net": net, "target": target, "tol": tol, "unit": unit, "spacing_mm": spacing}
        stats, tries = {}, 0
        try:
            for rnd in range(max_rounds):
                bud.check()
                cur = net_lengths(pcbnew.LoadBoard(work), g).get(net)
                val = None if cur is None else cur.get(unit if unit == "ps" else "mm")
                if rnd == 0:
                    row["before"] = val
                if val is None:
                    row.update(status="unknown", note="layer delay unknown")
                    break
                if abs(val - target) <= tol:
                    row.update(status="ok", note="")
                    break
                if val > target + tol:
                    row.update(status="fail", note="longer than target: never shortened")
                    break
                need = target - val
                if unit == "ps":
                    # ps -> mm with the net's average delay (the bump layer is picked by the guard)
                    avg = val / max(cur.get("mm") or 1e-9, 1e-9)
                    need = need / avg
                ok = False
                # PHASE3G G1b c/3: a pad-to-pad net lengthens only on its path (a stub is not length on the path)
                onp = path_segs(work, net) if cur.get("basis") == "pad-to-pad" else None
                for cand in range(4):
                    for amp, frac in ((g["amplitude_mm"], 1.0), (g["amplitude_mm"], 0.5), (g["amplitude_mm"] * 0.5, 1.0)):
                        if tries >= bud.stall:
                            raise Stalled()
                        n0 = stats.get("drc_runs", 0)
                        ok, base = _guarded_set(
                            board_path, work,
                            lambda bd, c=cand, a=amp, f=frac: meander_to(
                                bd, net, need * f, amp_mm=a, period_mm=period,
                                candidate=c, log=log, clearance_mm=clearance_of(net), stats=stats, on_path=onp) > 0,
                            kicad_cli, base, log, stats, bud)
                        tries = 0 if ok else tries + stats.get("drc_runs", 0) - n0
                        if ok:
                            accepted += 1
                            break
                    if ok:
                        break
                if not ok:
                    row.update(status="fail", note="no room: no DRC-clean bump found on any straight segment")
                    break
            else:
                row.update(status="fail", note=f"not within tolerance after {max_rounds} rounds")
        except Stalled:
            row.update(status="fail", note=f"stalled: {bud.stall} DRC-guarded tries without an accepted bump", stalled=True)
        cur = net_lengths(pcbnew.LoadBoard(work), g).get(net, {})
        row["after"] = cur.get(unit if unit == "ps" else "mm")
        row["drc_runs"] = stats.get("drc_runs", 0)
        row["geom_rejected"] = stats.get("geom_rejected", 0)
        row["basis"] = cur.get("basis", "total")
        row.setdefault("status", "ok")
        return row, base

    try:
        with bud:
            shutil.copy(board_path, work)
            _copy_sidecars(board_path, work)
            base = base0 = (drc_report(work, kicad_cli, timeout=bud.drc_timeout()) if bud.seconds
                            else drc_report(work, kicad_cli))
            log(f"  baseline DRC: {base[1][0]} violations, {base[1][1]} unconnected")
            for g in groups_:
                for net in sorted(net_lengths(pcbnew.LoadBoard(work), g)):
                    row, base = tune_net(g, net, base)
                    done.add((g["name"], net))
                    stalled = stalled or bool(row.get("stalled"))
                    total_runs += row["drc_runs"]
                    report.append(row)
                    log(f"  {g['name']}/{net}: {row.get('before')} -> {row['after']} {row['unit']} [{row['basis']}] "
                        f"spacing {row['spacing_mm']:.3f} mm ({row['status']}) {row.get('note', '')} "
                        f"[DRC runs {row['drc_runs']}, geometry rejects {row['geom_rejected']}]")
    except Stopped as e:
        stopped = e.reason
        log(f"  stopped ({stopped}): writing the best accepted board")
    except BaseException:
        cleanup_work(work)
        raise
    if not os.path.exists(work):
        shutil.copy(board_path, work)
    _finalize(work, out_path)
    if os.path.abspath(out_path) != os.path.abspath(board_path):   # the output carries the input's project rules: a later
        _copy_sidecars(board_path, out_path)                       # pcbnew save would otherwise write a default .kicad_pro
    if stopped is None and stalled:
        stopped = "stall"
    if stopped in ("budget", "signal"):                      # nets nobody reached: reported, not silently missing
        for g in groups_:
            target, tol = targets[g["name"]]
            for net in sorted(net_lengths(pcbnew.LoadBoard(out_path), g)):
                if (g["name"], net) not in done:
                    report.append({"group": g["name"], "net": net, "target": target, "tol": tol, "unit": g["unit"],
                                   "before": None, "after": None, "basis": "total", "status": "not_reached",
                                   "note": stopped, "drc_runs": 0, "geom_rejected": 0})
    if base is not None:
        log(f"  final DRC: {base[1][0]} violations, {base[1][1]} unconnected "
            f"(baseline {base0[1][0]} / {base0[1][1]}); DRC runs total {total_runs}")
    if result is not None:
        result.update(stopped=stopped, elapsed_s=round(bud.elapsed(), 1), budget_s=bud.seconds, out=out_path,
                      accepted=accepted, groups=report)
    return report
