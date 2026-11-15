"""Plane-net connections, owned by one explicit step after the router (route stage) and re-run after any later copper change.

The router counts a plane net's pads as connected through a pour, so a pad whose copper floats (no via or drilled pad
into the plane, or a void in the fill) stays open with no owner.  This step walks the real connectivity clusters of each
plane net on each inner layer that carries its zone (planes.tie_floating_clusters: one fill-verified, clearance-checked
via per floating cluster), refills the zones and reports what is still open with a reason.  Nothing else is changed."""
import math
import os



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


def unconnected_pads(board_path, nets, cli="/usr/bin/kicad-cli", workdir=None):
    """kicad-cli's own verdict: {net: [(x_mm, y_mm), ...]} for every pad of `nets` in a 'Missing connection' DRC item
    (zones refilled by kicad-cli). The pads are where a via can reach the plane; nothing is inferred."""
    import json, subprocess, tempfile
    wd = workdir or tempfile.mkdtemp(prefix="planetie-")
    out = os.path.join(wd, "drc.json")
    subprocess.run([cli, "pcb", "drc", "--refill-zones", "--format", "json", "--severity-all", "--output", out, board_path],
                   capture_output=True, text=True, timeout=900)
    try:
        items = json.load(open(out)).get("unconnected_items", [])
    except (OSError, ValueError):
        return {}
    want = set(nets)
    res = {}
    import re
    for it in items:
        for sub in it.get("items", []):
            m = re.match(r"Pad \S+ \[(.+?)\] of .* on (\S+)$", sub.get("description", ""))
            if m and m.group(1) in want:
                pos = sub["pos"]
                res.setdefault(m.group(1), set()).add((round(pos["x"], 4), round(pos["y"], 4), m.group(2)))
    return {k: sorted(v) for k, v in res.items()}


def _pad_ties(board, netname, pads):
    """Each unconnected pad (x, y, layer): a via on a short same-net stub from the pad, at the nearest of a few ring sites
    that planes.via_at_points accepts (it keeps the via clear of other-net copper and holes). The stub is on the pad layer.
    kicad-cli judges the result (the caller re-runs the DRC); a pad with no legal site is left open and reported."""
    import pcbnew
    from . import planes as PL
    n_ok = 0
    nc = board.FindNet(netname)
    for x, y, layer in pads:
        for r_mm in (0.0, 0.8, 1.1, 1.4):
            cands = [(x, y)] if r_mm == 0.0 else [(x + r_mm * math.cos(a), y + r_mm * math.sin(a))
                                                    for a in [k * math.pi / 6 for k in range(12)]]
            done = False
            for cx, cy in cands:
                if not PL.hole_ok(board, cx, cy):
                    continue
                if PL.via_at_points(board, netname, [(cx, cy)]) != 1:
                    continue
                if r_mm > 0.0:
                    tr = pcbnew.PCB_TRACK(board)
                    tr.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
                    tr.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(cx), pcbnew.FromMM(cy)))
                    tr.SetWidth(pcbnew.FromMM(0.15))
                    tr.SetLayer(board.GetLayerID(layer))
                    tr.SetNet(nc)
                    board.Add(tr)
                n_ok += 1
                done = True
                break
            if done:
                break
    return n_ok


def run(board_in, board_out, plane_nets, conn, log=None, workdir=None):
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
    pad_pts = unconnected_pads(board_in, targets, workdir=workdir)
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
            r["vias"] += _pad_ties(board, n, pad_pts.get(n, []))
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

