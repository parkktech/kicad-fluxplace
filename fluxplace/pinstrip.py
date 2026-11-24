"""Strip escape copper around gated pins: the gated legs' copper, the copper of `extra_nets`, and the fan-out of the
neighbour pins of the same parts (copper that would sit between a gated pin and its escape). Zones are never touched."""
import math

from . import boardtext as BT
from . import netclass, pairaudit, review, routediag
from .dedupe import remove_spans


def gated_legs(board, cons, spec=None):
    """sorted legs of every gated pair: pairaudit.pair_list(netclass.classify(routediag.load(board), cons, spec)) kept when
    review.pair_gated(cons, p) for the pair (p, n)."""
    pairs = pairaudit.pair_list(netclass.classify(routediag.load(board), cons, spec))
    return sorted({x for p, n in pairs if review.pair_gated(cons, p) for x in (p, n)})


def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def strip_escape(text, board, cons, spec=None, radius_mm=0.9, fan_mm=2.0, extra_nets=()):
    """Remove (locked or not) every segment/arc/via of the gated legs and of `extra_nets`, and the fan-out copper of the
    neighbour pins: pads of the SAME parts (same ref) whose centre is within radius_mm of a gated pin's centre, whose net is
    not a gated leg and not empty; their fan-out = segments/arcs with an end point within fan_mm of that pad's centre and
    vias whose centre is within fan_mm, on that pad's net. Zones are never touched.
    -> (text, report) report = {"legs": [...], "neighbours": ["REF.NUM NET", ...], "removed": n, "by_net": {net: n}}"""
    legs = set(gated_legs(board, cons, spec))
    pads = routediag.load(board)["pads"]
    gated = [p for p in pads if p.get("net") in legs]
    nb = {}
    for g in gated:
        for p in pads:
            if p is g or p.get("ref") != g.get("ref"):
                continue
            net = p.get("net") or ""
            if not net or net in legs:
                continue
            if _d((p["x"], p["y"]), (g["x"], g["y"])) <= radius_mm:
                nb[(p["ref"], p["num"], net, (p["x"], p["y"]))] = True
    neighbours = sorted({f"{r}.{n} {net}" for r, n, net, _c in nb})
    targets = set(legs) | set(extra_nets)
    kill, by_net = [], {}
    for i, j, _k, blk in BT.scan(text):
        kind = "via" if blk.startswith("(via") else "seg"
        net = BT.net_name(blk) or ""
        drop = net in targets
        if not drop:
            for (_r, _n, pnet, c) in nb:
                if net != pnet:
                    continue
                if kind == "via":
                    drop = _d(BT._num2(blk, "at"), c) <= fan_mm
                else:
                    drop = any(_d(BT._num2(blk, t), c) <= fan_mm for t in ("start", "end"))
                if drop:
                    break
        if drop:
            kill.append((i, j))
            by_net[net] = by_net.get(net, 0) + 1
    out = remove_spans(text, kill) if kill else text
    return out, {"legs": sorted(legs), "neighbours": neighbours, "removed": len(kill), "by_net": by_net}
