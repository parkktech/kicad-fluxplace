"""PHASE3I B5 / F6: lock policy by class. A locked copper item is judged by its net's class, not by one flag:
hard  = RF nets, gated (high-speed) pair legs and controlled nets (route.controlled, nets rules, patterns): only the pair
        builder may rip, with an explicit opt-in (allow_locked);
soft  = low-speed pair legs and audio nets: a rung may rip them in a session, and session commit checks the re-lay;
free  = everything else: a rung may rip them under the ordinary edit guard (recorded like any locked rip).
F6: the hard set is complete (rf, is_rf, gated legs, every controlled net with a reason other than plain copper).
`names is None` or no constraints -> HARD (fail closed)."""
from . import netclass as NC

HARD, SOFT, FREE = "hard", "soft", "free"


def level(net, cons, names=None):
    """Lock class of `net` (the net contract's `lock`: netclass.lock_level). names = every net of the board."""
    return NC.lock_level(net, cons, names)


def hard_locked(nets, cons, names=None):
    """The nets among `nets` whose locked copper is hard: a rip of them needs the explicit opt-in."""
    return sorted({n for n in nets if level(n, cons, names) == HARD})
