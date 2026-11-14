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
import os
import shutil
import subprocess
import tempfile

from . import repair as RP

__all__ = ["drc_counts", "shortcut", "meander", "tune_pairs", "meander_to", "tune_match", "bump_plan"]


# ------------------------------------------------------------------- DRC
def drc_counts(board_path, kicad_cli="kicad-cli", timeout=600):
    """(violations, unconnected) from a kicad-cli DRC at all severities."""
    out = board_path + ".drc.json"
    r = subprocess.run([kicad_cli, "pcb", "drc", "--format", "json",
                        "--severity-all", "--all-track-errors", "--refill-zones", "-o", out,
                        board_path], capture_output=True, text=True, timeout=timeout)
    if not os.path.exists(out):
        raise RuntimeError(f"DRC produced no report: {r.stderr[-200:]}")
    d = json.load(open(out))
    os.unlink(out)
    return len(d.get("violations", [])), len(d.get("unconnected_items", []))


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
def _guarded(board_path, work, mutate, kicad_cli, base_counts, log):
    """Load work, mutate(board) -> bool changed, save to tmp, DRC, accept
    into `work` only if violations did not rise. Returns (accepted, counts)."""
    import pcbnew
    board = pcbnew.LoadBoard(work)
    if not mutate(board):
        return False, base_counts
    tmp = work + ".try.kicad_pcb"
    pcbnew.SaveBoard(tmp, board)
    for ext in (".kicad_pro", ".kicad_dru"):
        side = os.path.splitext(board_path)[0] + ext
        if os.path.exists(side):
            shutil.copy(side, os.path.splitext(tmp)[0] + ext)
    v, u = drc_counts(tmp, kicad_cli)
    if v <= base_counts[0] and u <= base_counts[1]:
        shutil.move(tmp, work)
        return True, (v, u)
    log(f"      rejected by DRC: {v} violations / {u} unconnected "
        f"(was {base_counts[0]} / {base_counts[1]})")
    os.unlink(tmp)
    return False, base_counts


def tune_pairs(board_path, out_path, pairs, limit_mm, kicad_cli="kicad-cli",
               amp_mm=0.6, max_rounds=6, log=print):
    """pairs: {slave: master}. limit_mm: float or callable(master). Works on
    files so every step is a fresh pcbnew session (SWIG decays otherwise)."""
    import pcbnew
    work = out_path + ".work.kicad_pcb"
    shutil.copy(board_path, work)
    for ext in (".kicad_pro", ".kicad_dru"):
        side = os.path.splitext(board_path)[0] + ext
        if os.path.exists(side):
            shutil.copy(side, os.path.splitext(work)[0] + ext)
    base = drc_counts(work, kicad_cli)
    log(f"  baseline DRC: {base[0]} violations, {base[1]} unconnected")
    lim = limit_mm if callable(limit_mm) else (lambda m: limit_mm)
    report = []
    for slave, master in sorted(pairs.items()):
        for rnd in range(max_rounds):
            b = pcbnew.LoadBoard(work)
            m = RP.measure(b)
            lm, ls = m.get(master, (0, 0))[0], m.get(slave, (0, 0))[0]
            skew = lm - ls
            if abs(skew) <= lim(master):
                break
            longer, shorter = (master, slave) if skew > 0 else (slave, master)
            log(f"  {master}/{slave}: skew {skew:+.2f} mm (limit {lim(master)}) round {rnd + 1}")
            # 1. shorten the long side
            ok = False
            for gap in (1.5, 3.0):
                ok, base = _guarded(board_path, work,
                                    lambda bd, g=gap: shortcut(bd, longer, max_gap_mm=g, log=log) > 0,
                                    kicad_cli, base, log)
                if ok:
                    break
            if ok:
                continue
            # 2. lengthen the short side: several segments, two amplitudes,
            #    and a partial meander when the full one will not fit
            need = abs(skew)
            ok = False
            for cand in range(6):
                for amp, pitch in ((amp_mm, 0.7), (amp_mm * 0.6, 0.5), (0.35, 0.45)):
                    for frac in (1.0, 0.5, 0.25):
                        ok, base = _guarded(
                            board_path, work,
                            lambda bd, c=cand, a=amp, p=pitch, f=frac: meander(
                                bd, shorter, need * f, amp_mm=a, pitch_mm=p,
                                candidate=c, log=log) > 0,
                            kicad_cli, base, log)
                        if ok:
                            break
                    if ok:
                        break
                if ok:
                    break
            if ok:
                continue
            log(f"    {master}/{slave}: no DRC-clean tuning found — left at {skew:+.2f} mm")
            break
        b = pcbnew.LoadBoard(work)
        m = RP.measure(b)
        report.append((master, slave, m.get(master, (0, 0))[0], m.get(slave, (0, 0))[0]))
    shutil.move(work, out_path)
    for ext in (".kicad_pro", ".kicad_dru"):
        p = os.path.splitext(work)[0] + ext
        if os.path.exists(p):
            os.unlink(p)
    log(f"  final DRC: {base[0]} violations, {base[1]} unconnected")
    return report


# ------------------------------------------------------- length matching
def _bump_polyline(L, n, amp, period):
    """Local polyline (s along the segment, t across) of n rectangular bumps centred on a segment of length L.
    Bump i rises on its left leg at s_i, runs across its width (period / 2) and drops on its right leg; the copper
    between bumps is a straight run along the axis, so no leg reverses direction (no acute tip for DRC)."""
    w = period / 2.0
    s0 = (L - n * period) / 2.0
    pts = [(0.0, 0.0), (s0, 0.0)]
    for i in range(n):
        sg = 1.0 if i % 2 == 0 else -1.0
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


def meander_to(board, net, need_mm, amp_mm=0.6, period_mm=0.8, margin_mm=0.6,
               candidate=0, log=print):
    """Lengthen `net` by exactly `need_mm` with accordion bumps on its `candidate`-th longest UNLOCKED straight
    segment that has room (period_mm = 2 x bump width). Returns the length added (0.0 when no segment has room)."""
    import pcbnew
    lockers = [t for t in board.GetTracks() if t.GetNetname() == net and t.GetClass() == "PCB_TRACK"
               and not t.IsLocked()]
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
    pts = [(sx + u * ux + t * nx, sy + u * uy + t * ny) for u, t in _bump_polyline(L, n, a, period_mm)]
    width, layer, code = seg.GetWidth(), seg.GetLayer(), seg.GetNetCode()
    RP._remove(board, seg)
    added = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(int(round(x0 * 1e6)), int(round(y0 * 1e6))))
        t.SetEnd(pcbnew.VECTOR2I(int(round(x1 * 1e6)), int(round(y1 * 1e6))))
        t.SetLayer(layer)
        t.SetWidth(width)
        t.SetNetCode(code)
        board.Add(t)
        added += math.hypot(x1 - x0, y1 - y0)
    added -= L
    log(f"      {net}: accordion x{n} on {board.GetLayerName(layer)} adds {added:.3f} mm (asked {need_mm:.3f})")
    return added


def tune_match(board_path, out_path, groups_, net_lengths, targets, kicad_cli="kicad-cli",
               max_rounds=4, log=print):
    """Lengthen the short members of each match group until they are within tolerance.

    groups_: lengthmatch.groups() entries (already selected for this run).
    net_lengths(board, group) -> {net: {"mm", "ps"}} for the group's members (measured on the working file).
    targets: {group name: (target, tolerance)} in the group's unit, fixed by the caller from the input board.
    Every bump is one DRC-guarded step (accepted only if violations and unconnected do not rise).
    Returns [{group, net, before, after, target, tol, status, note}]."""
    import pcbnew
    work = out_path + ".work.kicad_pcb"
    shutil.copy(board_path, work)
    for ext in (".kicad_pro", ".kicad_dru"):
        side = os.path.splitext(board_path)[0] + ext
        if os.path.exists(side):
            shutil.copy(side, os.path.splitext(work)[0] + ext)
    base = base0 = drc_counts(work, kicad_cli)
    log(f"  baseline DRC: {base[0]} violations, {base[1]} unconnected")
    report = []
    for g in groups_:
        target, tol = targets[g["name"]]
        unit = g["unit"]
        period = 2.0 * (g["spacing_mm"] or 0.4)          # spacing_mm = bump width
        for net in sorted(net_lengths(pcbnew.LoadBoard(work), g)):
            row = {"group": g["name"], "net": net, "target": target, "tol": tol, "unit": unit}
            for rnd in range(max_rounds):
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
                for cand in range(4):
                    for amp, frac in ((g["amplitude_mm"], 1.0), (g["amplitude_mm"], 0.5),
                                      (g["amplitude_mm"] * 0.5, 1.0)):
                        ok, base = _guarded(
                            board_path, work,
                            lambda bd, c=cand, a=amp, f=frac: meander_to(
                                bd, net, need * f, amp_mm=a, period_mm=period,
                                candidate=c, log=log) > 0,
                            kicad_cli, base, log)
                        if ok:
                            break
                    if ok:
                        break
                if not ok:
                    row.update(status="fail", note="no room: no DRC-clean bump found on any straight segment")
                    break
            else:
                row.update(status="fail", note=f"not within tolerance after {max_rounds} rounds")
            cur = net_lengths(pcbnew.LoadBoard(work), g).get(net, {})
            row["after"] = cur.get(unit if unit == "ps" else "mm")
            row.setdefault("status", "ok")
            report.append(row)
            log(f"  {g['name']}/{net}: {row.get('before')} -> {row['after']} ({row['status']}) {row.get('note', '')}")
    shutil.move(work, out_path)
    for ext in (".kicad_pro", ".kicad_dru"):
        p = os.path.splitext(work)[0] + ext
        if os.path.exists(p):
            os.unlink(p)
    log(f"  final DRC: {base[0]} violations, {base[1]} unconnected (baseline {base0[0]} / {base0[1]})")
    return report
