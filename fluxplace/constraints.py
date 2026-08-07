"""Constraint ingest — the 'Constraints' stage: a hand-written TOML file that
overrides the pipeline's inferences with the engineer's numbers. Everything is
optional; anything not stated keeps the heuristic default. Schema:

    [power."+5V"]           # per power net (quote names with symbols)
    max_current_ma = 4000   # -> routed width via a 1oz outer-layer rule
    pour = true             # rail rides a plane/pour, not a fat trace

    [pairs.PCIE_TX]         # diff-pair family, matched by net-name prefix
    impedance_diff = 85     # recorded for the stackup/report (not yet enforced)
    skew_mm = 0.1           # SI-lite intra-pair skew limit for these nets

    [si]
    default_skew_mm = 1.0   # SI-lite limit for pairs with no family entry

Width rule: conservative 1oz external copper, ~0.5 mm per amp with a 0.3 mm
floor — wide enough for a 10C rise at the stated current, narrow enough not to
eat the board. The engineer can always state width_mm directly instead.
"""
import tomllib


def load(path):
    """Parse the TOML; returns the raw dict ({} for None path)."""
    if not path:
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def power_width_mm(cons, net, default_mm):
    """Routed trace width for a power net: explicit width_mm wins, else the
    ampacity rule on max_current_ma, else the caller's heuristic default."""
    p = cons.get("power", {}).get(net)
    if not p:
        return default_mm
    if "width_mm" in p:
        return float(p["width_mm"])
    if "max_current_ma" in p:
        return max(0.3, 0.5 * float(p["max_current_ma"]) / 1000.0)
    return default_mm


def pour_nets(cons):
    """Power nets the engineer says ride a plane/pour — the router should not
    also draw them as fat traces."""
    return {n for n, p in cons.get("power", {}).items() if p.get("pour")}


def skew_limit_mm(cons, master):
    """SI-lite intra-pair skew limit for the pair whose master net is `master`:
    the longest matching [pairs.*] prefix wins, else [si].default_skew_mm."""
    best = None
    for fam, p in cons.get("pairs", {}).items():
        if master.startswith(fam) and "skew_mm" in p:
            if best is None or len(fam) > best[0]:
                best = (len(fam), float(p["skew_mm"]))
    if best:
        return best[1]
    return float(cons.get("si", {}).get("default_skew_mm", 1.0))
