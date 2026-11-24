"""PHASE3I B5 / F6: lock policy by class. A locked copper item is judged by its net's class, not by one flag:
hard  = RF nets, gated (high-speed) pair legs and controlled nets (route.controlled, nets rules, patterns): only the pair
        builder may rip, with an explicit opt-in (allow_locked);
soft  = low-speed pair legs and audio nets: a rung may rip them in a session, and session commit checks the re-lay;
free  = everything else: a rung may rip them under the ordinary edit guard (recorded like any locked rip).
F6: the hard set is complete (rf, is_rf, gated legs, every controlled net with a reason other than plain copper).
`names is None` or no constraints -> HARD (fail closed)."""
from . import graph as G, review as RV, netclass as NC, constraints as C

HARD, SOFT, FREE = "hard", "soft", "free"


def level(net, cons, names=None):
    """Lock class of `net`. names = every net of the board (pair partners are found by naming)."""
    if names is None or not cons:
        return HARD
    from . import drcguard as DG, krtchain as K, ripolicy as RP
    names = set(names) | {net}
    rf = set(((cons or {}).get("rf") or {}).get("nets") or [])
    dp = G.diff_pairs(sorted(names), exclude=C.not_pairs(cons))
    legs = set(dp) | set(dp.values()) | set(K.pair_nets(cons, sorted(names), None))
    gated = {x for slave, master in dp.items() if RV.pair_gated(cons, master) for x in (slave, master)}
    gated |= {x for x in K.pair_nets(cons, sorted(names), None) if RV.pair_gated(cons, x)}
    protected = DG.protected_nets(cons, sorted(names))
    ctl = RP.controlled_nets(None, cons, sorted(names))
    if net in protected and (net in rf or RV.is_rf(net) or net in gated):
        return HARD
    if net in ctl and ctl[net] != "plain copper":
        return HARD
    if net in protected and net in legs:
        return SOFT
    if NC.pair_kind(net) == "audio":
        return SOFT
    return FREE


def hard_locked(nets, cons, names=None):
    """The nets among `nets` whose locked copper is hard: a rip of them needs the explicit opt-in."""
    return sorted({n for n in nets if level(n, cons, names) == HARD})
