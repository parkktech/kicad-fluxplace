"""Rip policy of the merge: may a router result that removes or alters other nets' copper be adopted?

`[route] allow_rip = "low-speed"` (default) | "never" | "any".  "never": such a result is always dropped.  "low-speed": adopted only
when every affected net is NOT controlled, NOT carrying fan-out-locked copper, NOT plain copper, and the merge then shows
the affected nets re-routed and the board's total open count (pcbnew after zone refill) lower than before; otherwise it is dropped
with reason rips-other-net.  "any": every affected net may be ripped; the open-count test still applies.

Controlled nets = pairs, RF, clocks, switch nodes and impedance-controlled classes from the net classes (`netclass.classify`: the
constraints first, name patterns only for nets the constraints do not mention), `[pairs.*]`, `[rf] nets`, `[nets.X]` rules
(straight / straight_copper / max_vias), `[route] controlled = [nets]`, and `[route] controlled_patterns = [regex]` (a
documented fallback for projects without classes)."""
import re

CONTROLLED_CLASSES = ("pair", "rf", "clock", "switch_node")


def policy(cons):
    p = str(((cons or {}).get("route") or {}).get("allow_rip", "low-speed")).lower()
    return p if p in ("never", "low-speed", "any") else "low-speed"


def plain_nets(cons):
    """Nets that must be plain straight copper: `[nets.X] straight = true` or `straight_copper = [pads]`."""
    out = set()
    for n, v in ((cons or {}).get("nets") or {}).items():
        if isinstance(v, dict) and (v.get("straight") is True or v.get("straight_copper")):
            out.add(n)
    return out


def controlled_nets(cls, cons, names=()):
    """{net: why} for the nets that a rip may never touch."""
    out = {}
    for n, info in (cls or {}).items():
        if info.get("class") in CONTROLLED_CLASSES:
            out[n] = info["class"]
    r = (cons or {}).get("route") or {}
    for n in (cons or {}).get("rf", {}).get("nets", []) or []:
        out[n] = "rf"
    for n in r.get("controlled", []) or []:
        out[n] = "route.controlled"
    for n, v in ((cons or {}).get("nets") or {}).items():
        if isinstance(v, dict) and (v.get("max_vias") is not None or v.get("impedance")):
            out[n] = "nets rule"
    for n in plain_nets(cons):
        out[n] = "plain copper"
    pats = [re.compile(x) for x in (r.get("controlled_patterns") or [])]
    for n in names:
        if n not in out and any(p.search(n) for p in pats):
            out[n] = "pattern"
    return out


def affected(rem, job_net_set):
    """nets (other than the job's) that the result removed copper of -> {net: count}."""
    out = {}
    for x in list(rem[0]) + list(rem[1]):
        if x["net"] not in job_net_set:
            out[x["net"]] = out.get(x["net"], 0) + 1
    return out


def prefilter(pol, aff, controlled, rem=()):
    """(ok to try, why not) before the expensive open-count check. `rem` = removed copper items (segs + vias, with `locked`)."""
    if not aff:
        return True, ""
    if pol == "never":
        return False, "allow_rip = never"
    if any(x.get("locked") for x in rem):
        return False, "fan-out locked copper removed"
    if pol == "any":
        return True, ""
    bad = sorted(n for n in aff if n in controlled)
    if bad:
        return False, f"{bad[0]} is {controlled[bad[0]]}"
    return True, ""
