"""RF nets in the route stage: the impedance width per layer (zgeom.single_width, the model the review grades with) and the
outer-layer rule (project rule: 50 ohm RF on outer layers, over the GND planes next to them). An RF net is never routed at the
default width and never by the plain step: the rf step routes it at this width on the outer layers, or it stays open."""


def outer_layers(layers):
    """The outer copper layers among `layers` (F.Cu / B.Cu), stack order."""
    return [x for x in layers if x in ("F.Cu", "B.Cu")]


def widths(board, cons, nets, layers):
    """-> {net: {"w": mm or None, "layers": {layer: {w, z, err_pct, status}}}} on `layers`. `w` is the mean of the layers' required
    widths (the router takes one width per net). None when no layer has one (no stackup / no reference plane): the caller states it."""
    from . import qualaudit as QA, routediag as RD, zgeom as ZG
    out = {}
    try:
        B = RD.load(board)
        stackup, planes, _fmap = QA._ctx(board, B, cons)
    except Exception:                                          # noqa: BLE001 - no board facts: no width, the caller reports it
        return {n: {"w": None, "layers": {}} for n in nets}
    for n in nets:
        per = {}
        for lay in layers:
            try:
                sw = ZG.single_width(cons, stackup, planes, n, lay, board=board)
            except Exception:                                  # noqa: BLE001
                continue
            if sw.get("w"):
                per[lay] = {k: sw.get(k) for k in ("w", "z", "err_pct", "status")}
        ws = [v["w"] for v in per.values()]
        out[n] = {"w": round(sum(ws) / len(ws), 3) if ws else None, "layers": per}
    return out


def split(rf, still_open):
    """-> (routed, open): the RF nets of `rf` the rf step closed and those it left open (reported, never handed to the plain step)."""
    still = set(still_open)
    return sorted(n for n in rf if n not in still), sorted(n for n in rf if n in still)
