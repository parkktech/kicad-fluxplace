"""Timing budget: `[timing.NAME]` flight-time checks for a clocked bus (DDR-style). Analysis only: nothing is
moved here. `tune --match` fixes the lengths when the user also declares [match.*] groups.

  [timing.DDR]
  clock       = "CK_P"                          # the clock net (exact name)
  data        = ["DQ\\d+"]                      # data nets, regex fullmatch
  setup_ps    = 40                               # data may arrive at most this LATE vs its clock/strobe
  hold_ps     = 40                               # ... and at most this EARLY
  include_vias = true                            # default true: flight time includes via barrels
  strobe_groups = { DQS0 = ["DQ[0-7]"], DQS1 = ["DQ[89]|DQ1[0-5]"] }   # a data net checked against its own strobe
  flyby_order = ["U1", "D1", "D2"]              # clock stub must reach these parts in this order
  flyby_origin = "U1"                           # the driver of the clock (default: the one clock pad not in flyby_order)

Skew of a data net = its flight time minus its reference's (clock, or its strobe). FAIL TIMING_SKEW when that is
above setup_ps (late) or below -hold_ps (early). Flight time = the lengths and layer delays of lengthmatch.
Flyby: the clock copper path length from the origin pad to each listed part must strictly increase in list order.
"""
import re

from . import lengthmatch as LM

__all__ = ["groups", "check"]


def _num(g, key, name):
    if key not in g:
        raise ValueError(f"[timing.{name}] needs {key}")
    v = g[key]
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError(f"[timing.{name}] {key} must be a number (ps)")
    return float(v)


def groups(cons):
    out = []
    for name, g in sorted(((cons or {}).get("timing") or {}).items()):
        if not isinstance(g, dict):
            raise ValueError(f"[timing.{name}] must be a table")
        if not isinstance(g.get("clock"), str) or not g["clock"]:
            raise ValueError(f"[timing.{name}] clock must be a net name")
        data = g.get("data")
        if not isinstance(data, list) or not data:
            raise ValueError(f"[timing.{name}] data must be a non-empty list of regex strings")
        for p in data:
            re.compile(p)
        sg = g.get("strobe_groups") or {}
        for s, pats in sg.items():
            if not isinstance(pats, list) or not pats:
                raise ValueError(f"[timing.{name}] strobe_groups.{s} must be a non-empty list")
            for p in pats:
                re.compile(p)
        fo = g.get("flyby_order")
        if fo is not None and (not isinstance(fo, list) or len(fo) < 2):
            raise ValueError(f"[timing.{name}] flyby_order needs at least two refs")
        out.append({
            "name": name, "clock": g["clock"], "data": data,
            "setup_ps": _num(g, "setup_ps", name), "hold_ps": _num(g, "hold_ps", name),
            "include_vias": bool(g.get("include_vias", True)),
            "strobe_groups": {s: list(p) for s, p in sg.items()},
            "flyby_order": list(fo) if fo else None,
            "flyby_origin": g.get("flyby_origin"),
        })
    return out


def _flyby(board, clock, order, origin):
    """(rows, problems): [(ref, mm)] along the clock copper from the origin pad, and reasons it cannot be checked."""
    from . import repair as RP
    from . import tune as TU
    netcode = None
    for t in board.GetTracks():
        if t.GetNetname() == clock:
            netcode = t.GetNetCode()
            break
    if netcode is None:
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                if pad.GetNetname() == clock:
                    netcode = pad.GetNetCode()
                    break
            if netcode is not None:
                break
    if netcode is None:
        return [], [f"clock net {clock} has no copper or pads"]
    adj, pads = RP._net_graph(board, netcode)
    refs_on = {p[1] for p in pads}
    if origin is None:
        cand = sorted(r for r in refs_on if r not in order)
        if len(cand) != 1:
            return [], [f"clock driver is ambiguous ({', '.join(cand) or 'none'}): set flyby_origin"]
        origin = cand[0]
    src = [p for p in pads if p[1] == origin]
    if not src:
        return [], [f"flyby_origin {origin} has no pad on {clock}"]
    dist = {}
    for s in src:
        d = TU._dist_from(adj, s)
        for k, v in d.items():
            if k not in dist or v < dist[k]:
                dist[k] = v
    rows, problems = [], []
    for ref in order:
        hits = [dist[p] for p in pads if p[1] == ref and p in dist]
        if not hits:
            problems.append(f"{ref} is not reached by {clock}")
            rows.append((ref, None))
        else:
            rows.append((ref, min(hits)))
    return rows, problems


def check(board, facts, cons, open_nets=frozenset()):
    """Findings in review format ({level, code, msg, refs}) for every [timing.*] group.
    board: a loaded pcbnew board of facts['board_path']."""
    from . import review as RV          # local: review imports this module lazily too
    _f, FAIL, WARN = RV._f, RV.FAIL, RV.WARN
    out = []
    names = list(facts.get("net_pads") or {})
    stackup, planes = facts.get("stackup") or [], facts.get("plane_layers") or []
    for g in groups(cons or {}):
        clock = g["clock"]
        data = LM.members({"nets": g["data"]}, names)
        strobe_of = {}
        for s, pats in g["strobe_groups"].items():
            for d in LM.members({"nets": pats}, names):
                strobe_of[d] = s
        need = set(data) | {clock} | set(strobe_of.values())
        unrouted = sorted(n for n in need if n in open_nets)
        if unrouted:
            out.append(_f(WARN, "TIMING_UNROUTED",
                          f"[timing.{g['name']}] not evaluated: not fully routed: {', '.join(unrouted)}", refs=unrouted))
            continue
        lens = LM.lengths_from_board(board, need, stackup, planes, g["include_vias"])
        if lens.get(clock, {}).get("ps") is None:
            out.append(_f(WARN, "TIMING_DELAY_UNKNOWN",
                          f"[timing.{g['name']}] clock {clock}: flight time unknown (layer delay or no copper)"))
        for d in data:
            ref = strobe_of.get(d, clock)
            ld, lr = lens.get(d, {}), lens.get(ref, {})
            if ld.get("ps") is None or lr.get("ps") is None:
                out.append(_f(WARN, "TIMING_DELAY_UNKNOWN",
                              f"[timing.{g['name']}] {d} vs {ref}: flight time unknown — not checked", refs=[d, ref]))
                continue
            skew = ld["ps"] - lr["ps"]
            if skew > g["setup_ps"]:
                out.append(_f(FAIL, "TIMING_SKEW",
                              f"[timing.{g['name']}] {d} is {skew:.1f} ps LATE vs {ref} "
                              f"(flight {ld['ps']:.1f} / {lr['ps']:.1f} ps, {ld['mm']:.2f} / {lr['mm']:.2f} mm), "
                              f"setup budget {g['setup_ps']:g} ps", refs=[d, ref]))
            elif skew < -g["hold_ps"]:
                out.append(_f(FAIL, "TIMING_SKEW",
                              f"[timing.{g['name']}] {d} is {-skew:.1f} ps EARLY vs {ref} "
                              f"(flight {ld['ps']:.1f} / {lr['ps']:.1f} ps, {ld['mm']:.2f} / {lr['mm']:.2f} mm), "
                              f"hold budget {g['hold_ps']:g} ps", refs=[d, ref]))
        if g["flyby_order"]:
            rows, problems = _flyby(board, clock, g["flyby_order"], g["flyby_origin"])
            for p in problems:
                out.append(_f(FAIL, "TIMING_FLYBY_ORDER", f"[timing.{g['name']}] {p}"))
            if not problems:
                prev = None
                for ref, mm in rows:
                    if prev is not None and mm <= prev[1]:
                        out.append(_f(FAIL, "TIMING_FLYBY_ORDER",
                                      f"[timing.{g['name']}] clock reaches {ref} at {mm:.2f} mm, not after "
                                      f"{prev[0]} at {prev[1]:.2f} mm (flyby_order {' > '.join(g['flyby_order'])})",
                                      refs=[ref, prev[0]]))
                    prev = (ref, mm)
    return out
