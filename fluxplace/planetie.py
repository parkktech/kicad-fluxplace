"""Plane-net connections, owned by one explicit step after the router (route stage) and re-run after any later copper change.

The router counts a plane net's pads as connected through a pour, so a pad whose copper floats (no via or drilled pad
into the plane, or a void in the fill) stays open with no owner.  This step walks the real connectivity clusters of each
plane net on each inner layer that carries its zone (planes.tie_floating_clusters: one fill-verified, clearance-checked
via per floating cluster), refills the zones and reports what is still open with a reason.  Nothing else is changed."""


def plane_zone_layers(board, netname):
    """copper layer names where `netname` has a (non-keepout) zone, most zones first."""
    net = board.FindNet(netname)
    if net is None:
        return []
    nc = net.GetNetCode()
    best = {}
    for z in board.Zones():
        if z.GetIsRuleArea() or z.GetNetCode() != nc:
            continue
        for lid in z.GetLayerSet().Seq():
            name = board.GetLayerName(lid)
            best[name] = best.get(name, 0) + 1
    return [k for k, _ in sorted(best.items(), key=lambda kv: -kv[1])]


def run(board_in, board_out, plane_nets, conn, log=None):
    """Tie every floating cluster of each plane net to its plane, refill, save board_out.
    -> {"placed": n, "per_net": {net: {"open_before", "open_after", "layer", "vias", "reason"}}, "board": board_out}.
    `conn` is batchroute's connectivity count (net -> open connections) and is the only judge of "connected"."""
    import pcbnew
    from . import planes as PL
    before = conn(board_in)
    rec = {"placed": 0, "per_net": {}, "board": board_out}
    targets = [n for n in plane_nets if before.get(n)]
    if not targets:
        import shutil
        shutil.copy(board_in, board_out)
        return rec
    board = pcbnew.LoadBoard(board_in)
    PL.refill(board)                 # the tie tests the FILLED copper: fill the zones first
    for n in targets:
        layers = plane_zone_layers(board, n)
        r = {"open_before": before[n], "layer": layers[0] if layers else None, "vias": 0, "reason": None}
        if not layers:
            r["reason"] = "no zone of this net on any layer (pour missing)"
        else:
            for ly in layers:
                r["vias"] += PL.tie_floating_clusters(board, n, ly)
        rec["per_net"][n] = r
        rec["placed"] += r["vias"]
    PL.refill(board)
    board.Save(board_out)
    after = conn(board_out)
    for n, r in rec["per_net"].items():
        r["open_after"] = after.get(n, 0)
        if r["open_after"] and not r["reason"]:
            r["reason"] = "still open after tie: pad islands not reached by a clearance-legal via (see route_owners reason)"
    if log:
        log(f"planetie: {rec['placed']} via(s) placed on {len(targets)} plane net(s)")
    return rec


def lines(rec):
    """one line per plane net for the stage text / route_owners"""
    return [f"{n}: open {r['open_before']} -> {r['open_after']} via(s) {r['vias']} layer {r['layer']}"
            + (f" REASON {r['reason']}" if r.get("reason") and r.get("open_after") else "")
            for n, r in sorted(rec["per_net"].items())]

