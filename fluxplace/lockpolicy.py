"""PHASE3I B5: lock policy by class. A locked copper item is judged by its net's class, not by one flag:
hard  = high-speed (gated) pair legs and RF nets: only the pair builder may rip, with an explicit opt-in (allow_locked);
soft  = low-speed pair legs and audio nets: a rung may rip them, the caller re-lays them in the same guarded edit;
free  = everything else: a rung may rip them under the ordinary edit guard."""
from . import graph as G, review as RV, netclass as NC

HARD, SOFT, FREE = "hard", "soft", "free"


def level(net, cons, names=None):
    """Lock class of `net`. names = every net of the board (pair partners are found by naming); None = this net alone."""
    rf = set(((cons or {}).get("rf") or {}).get("nets") or [])
    if net in rf:
        return HARD
    pairs = G.diff_pairs(set(names) if names is not None else {net})
    master = pairs.get(net) or (net if net in pairs.values() else None)
    if master is not None:
        return HARD if RV.pair_gated(cons, master) else SOFT
    if NC.pair_kind(net) == "audio":
        return SOFT
    return FREE


def hard_locked(nets, cons, names=None):
    """The nets among `nets` whose locked copper is hard: a rip of them needs the explicit opt-in."""
    return sorted({n for n in nets if level(n, cons, names) == HARD})
