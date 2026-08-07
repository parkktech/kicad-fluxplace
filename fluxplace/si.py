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
        best = None
        for ic in nets.get(rail, []):
            if ic == ref or ic[0] not in "UQ":
                continue
            q = parts.get(ic)
            if not q or rail not in q.get("pins", {}):
                continue
            qx, qy = q["x"] + q["pins"][rail][0], q["y"] + q["pins"][rail][1]
            d = ((cx - qx) ** 2 + (cy - qy) ** 2) ** 0.5
            if best is None or d < best[1]:
                best = (ic, d)
        if best:
            out.append((ref, best[0], rail, best[1]))
    return out


def bypass_findings(parts, nets, power_nets, warn_mm=10.0):
    """Physics check: a bypass capacitor further than warn_mm from the pin it
    decouples is inductively useless at HF (industry rule <=1cm). Returns
    ([(level, code, msg)], [(cap, ic, rail, dist)])."""
    table = infer_bypass(parts, nets, power_nets)
    findings = [("WARN", "BYPASS_FAR",
                 f"{c}: {d:.1f}mm from {ic} on {rail} (limit {warn_mm}mm)")
                for c, ic, rail, d in table if d > warn_mm]
    return findings, table


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
