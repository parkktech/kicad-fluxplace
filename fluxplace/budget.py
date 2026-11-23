"""One budget model (REFACTOR-PLAN-W16 section 2). Slice 1 (A6): `family_of`, the one family matcher.
A family is a constraints table key (`[pairs.F]`, `[channel.F]`) that is the longest prefix of a net name AND the net is a
leg of a classified pair.  A bare prefix hit also matches plain signals (X_LED, X_CLKREQ, X_CC): those are not pairs."""


_TAILS = ("_P", "_N", "DP", "DM", "P", "N", "+", "-")


def pair_legs(names, cons):
    """Set of nets that are a leg of a differential pair: a partner is in `names` (graph.diff_pairs / family tails), or the net sits
    under a [pairs.F] family and ends in a P/N tail (its partner is just not in `names`, e.g. a DRC item list). Never a net the
    owner declared plain (`not_pairs`), never a plain signal under the family prefix (X_LED, X_CLKREQ, X_CC)."""
    from . import netclass, constraints as C
    cons = cons or {}
    legs = set(netclass._pairs(sorted(names), cons))
    nopair = C.not_pairs(cons)
    fams = cons.get("pairs") or {}
    for n in names:
        if n not in nopair and n.endswith(_TAILS) and any(n.startswith(f) for f in fams):
            legs.add(n)
    return legs


def family_of(table, net, legs=None):
    """Longest key of `table` that prefixes `net`, else None. legs: set of pair-leg nets (pair_legs); when given, a net that is
    not a leg has no family."""
    if legs is not None and net not in legs:
        return None
    best = None
    for f in table or {}:
        if net.startswith(f) and (best is None or len(f) > len(best)):
            best = f
    return best
