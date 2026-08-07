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
