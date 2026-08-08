"""SI-lite verification — the physics checks a routed board can fail even when
DRC passes. v1: differential-pair intra-pair length skew (P and N must arrive
together; skew converts to timing error at ~6.7 ps/mm on FR-4). Pure logic here;
board measurement lives with the callers so this stays pcbnew-free and testable.
"""


def pair_skew_findings(lengths, pairs, warn_mm=1.0):
    """lengths: {net: routed_mm}; pairs: {slave: master} (graph.diff_pairs).
    `warn_mm` is a float or a callable(master_net) -> mm (per-family constraint
    limits). Returns ([(level, code, msg)], [(master, slave, lm, ls, skew)]) —
    a WARN per pair whose |len(P) - len(N)| exceeds its limit, plus the full
    measured table. Pairs with an unrouted side report UNROUTED_PAIR instead of
    a bogus skew."""
    lim = warn_mm if callable(warn_mm) else (lambda m: warn_mm)
    findings, table = [], []
    for slave, master in sorted(pairs.items()):
        lm, ls = lengths.get(master), lengths.get(slave)
        if not lm or not ls:
            findings.append(("WARN", "UNROUTED_PAIR",
                             f"{master}/{slave}: a side has no routed copper"))
            continue
        skew = abs(lm - ls)
        table.append((master, slave, lm, ls, skew))
        if skew > lim(master):
            findings.append(("WARN", "PAIR_SKEW",
                             f"{master}/{slave}: {skew:.2f}mm intra-pair skew "
                             f"(P {lm:.1f} / N {ls:.1f}mm, limit {lim(master)}mm)"))
    return findings, table


def infer_bypass(parts, nets, power_nets):
    """[(cap_ref, ic_ref, rail, dist_mm)]: every 2-net capacitor sitting between a
    power rail and GND, paired with the nearest IC/module pin on that rail — the
    pin it exists to decouple. Pure geometry from the read_board dict."""
    out = []
    for ref, p in parts.items():
        if ref[0] != "C" or len(p.get("pins", {})) != 2:
            continue
        pn = set(p["pins"])
        if "GND" not in pn:
            continue
        rail = next((n for n in pn if n != "GND"), None)
        if rail not in power_nets:
            continue
        cx, cy = p["x"] + p["pins"][rail][0], p["y"] + p["pins"][rail][1]
        # ownership: same schematic SHEET beats distance (intent beats accident —
        # nearest-by-distance is self-fulfilling on a bad placement; calibrated
        # against Quilter's schematic-driven table, sheet match recovers the
        # assignments the distance rule got wrong)
        best = None
        for ic in nets.get(rail, []):
            if ic == ref or ic[0] not in "UQ":
                continue
            q = parts.get(ic)
            if not q or rail not in q.get("pins", {}):
                continue
            qx, qy = q["x"] + q["pins"][rail][0], q["y"] + q["pins"][rail][1]
            d = ((cx - qx) ** 2 + (cy - qy) ** 2) ** 0.5
            key = (0 if q.get("sheet") == p.get("sheet") else 1, d)
            if best is None or key < best[1]:
                best = (ic, key)
        if best:
            out.append((ref, best[0], rail, best[1][1]))
    return out


def bypass_findings(parts, nets, power_nets, warn_mm=10.0):
    """Physics checks: a bypass capacitor further than warn_mm from the pin it
    decouples is inductively useless at HF (industry rule <=1cm), and one on the
    OPPOSITE board side guarantees >=2 layer switches in the decoupling loop
    (each via ~0.5-1nH — the 'layer switch count' failure). Returns
    ([(level, code, msg)], [(cap, ic, rail, dist)])."""
    table = infer_bypass(parts, nets, power_nets)
    findings = []
    for c, ic, rail, d in table:
        if d > warn_mm:
            findings.append(("WARN", "BYPASS_FAR",
                             f"{c}: {d:.1f}mm from {ic} on {rail} (limit {warn_mm}mm)"))
        cs = parts.get(c, {}).get("side", "F")
        is_ = parts.get(ic, {}).get("side", "F")
        if cs != is_ and not parts.get(ic, {}).get("tht"):
            findings.append(("WARN", "BYPASS_SIDE",
                             f"{c}: on side {cs} but {ic} is on {is_} — the loop "
                             f"needs >=2 layer switches (via inductance)"))
    return findings, table


def return_via_findings(pair_vias, gnd_vias, max_mm=10.0):
    """Return-path check: every via on a diff-pair net needs a GND stitching via
    within max_mm — the pair's return current must change reference planes where
    the signal does. pair_vias/gnd_vias: [(net_or_None, x_mm, y_mm)]. Returns
    ([(level, code, msg)], [(net, x, y, nearest_gnd_mm|None)])."""
    findings, table = [], []
    for net, x, y in pair_vias:
        best = None
        for _, gx, gy in gnd_vias:
            d = ((x - gx) ** 2 + (y - gy) ** 2) ** 0.5
            if best is None or d < best:
                best = d
        table.append((net, x, y, best))
        if best is None:
            findings.append(("WARN", "NO_RETURN_VIA",
                             f"{net} via at ({x:.1f},{y:.1f}): no GND via on the board"))
        elif best > max_mm:
            findings.append(("WARN", "RETURN_VIA_FAR",
                             f"{net} via at ({x:.1f},{y:.1f}): nearest GND via "
                             f"{best:.1f}mm away (limit {max_mm}mm)"))
    return findings, table


def collect_vias(board, pair_nets):
    """([(net,x,y)] pair-net vias, [(None,x,y)] GND vias) from a pcbnew board."""
    pv, gv = [], []
    for t in board.GetTracks():
        if t.GetClass() != "PCB_VIA":
            continue
        n = t.GetNetname()
        p = t.GetPosition()
        if n in pair_nets:
            pv.append((n, p.x / 1e6, p.y / 1e6))
        elif n == "GND":
            gv.append((None, p.x / 1e6, p.y / 1e6))
    return pv, gv


def measure_net_lengths(board):
    """{net: total routed track length mm} from a pcbnew board (vias excluded)."""
    from collections import defaultdict
    L = defaultdict(float)
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            continue
        L[t.GetNetname()] += t.GetLength() / 1e6
    return dict(L)


def check_board(board_path, pairs, warn_mm=1.0, pcbnew=None):
    """Load `board_path`, measure, and run pair_skew_findings."""
    if pcbnew is None:
        import pcbnew
    b = pcbnew.LoadBoard(board_path)
    return pair_skew_findings(measure_net_lengths(b), pairs, warn_mm)
