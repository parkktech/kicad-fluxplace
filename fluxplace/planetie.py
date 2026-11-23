"""Plane-net connections, owned by one explicit step after the router (route stage) and re-run after any later copper change.

The router counts a plane net's pads as connected through a pour, so a pad whose copper floats (no via or drilled pad
into the plane, or a void in the fill) stays open with no owner.  This step walks the real connectivity clusters of each
plane net on each inner layer that carries its zone (planes.tie_floating_clusters: one fill-verified, clearance-checked
via per floating cluster), refills the zones and reports what is still open with a reason.  Nothing else is changed."""
import math
import os
import subprocess
from fluxplace import procs as _procs



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
    _procs.run([cli, "pcb", "drc", "--refill-zones", "--format", "json", "--severity-all", "--output", out, board_path],
                   capture_output=True, text=True, timeout=900)
    try:
        from . import drcfull as DF
        items = DF.complete(board_path, json.load(open(out)), fill=True).get("unconnected_items", [])
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


def stub_ok(board, nc, lid, a, b, width_mm, clear_mm, step_mm=0.05):
    """True when a `width_mm` track from a to b (mm) on copper layer `lid` keeps `clear_mm` off every other-net pad, track and via."""
    import pcbnew
    acc = pcbnew.FromMM(width_mm / 2 + clear_mm)
    n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / step_mm))
    pts = [pcbnew.VECTOR2I(pcbnew.FromMM(a[0] + (b[0] - a[0]) * k / n), pcbnew.FromMM(a[1] + (b[1] - a[1]) * k / n)) for k in range(n + 1)]
    pads = [p for f in board.GetFootprints() for p in f.Pads() if p.GetNetCode() != nc and p.IsOnLayer(lid)]
    trks = [t for t in board.GetTracks() if t.GetNetCode() != nc and t.IsOnLayer(lid)]
    return not any(o.HitTest(q, acc) for o in pads + trks for q in pts)


def _pad_ties(board, netname, pads, rules=None):
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
                if rules and r_mm > 0.0 and not stub_ok(board, nc.GetNetCode(), board.GetLayerID(layer), (x, y), (cx, cy),
                                                       rules["track"], rules["clearance"]):
                    continue                                   # the stub is copper too: it keeps the rule's clearance
                if PL.via_at_points(board, netname, [(cx, cy)], **({"via_d": rules["via"], "via_drill": rules["drill"]} if rules else {})) != 1:
                    continue
                if r_mm > 0.0:
                    tr = pcbnew.PCB_TRACK(board)
                    tr.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
                    tr.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(cx), pcbnew.FromMM(cy)))
                    tr.SetWidth(pcbnew.FromMM(rules["track"] if rules else 0.15))
                    tr.SetLayer(board.GetLayerID(layer))
                    tr.SetNet(nc)
                    board.Add(tr)
                n_ok += 1
                done = True
                break
            if done:
                break
    return n_ok


def pour_census(board, netname):
    """Signal-layer pours of `netname` (copper layers other than the nets' plane layers) and the census of its pads: how many
    are reached by a same-net track on a non-pour layer (track-made connections), and how many of those have a hole-legal
    via site (a proxy for 'a short via into the pour would have covered it'). Measured, not acted on."""
    import pcbnew
    from . import planes as PL
    pour_layers = set()
    for z in board.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != netname:
            continue
        for l in z.GetLayerSet().Seq():
            if pcbnew.IsCopperLayer(l):
                pour_layers.add(board.GetLayerName(l))
    if not pour_layers:
        return None
    pads = [p for f in board.GetFootprints() for p in f.Pads() if p.GetNetname() == netname]
    tracks = [t for t in board.GetTracks() if t.Type() != pcbnew.PCB_VIA_T and t.GetNetname() == netname
              and t.GetLayerName() not in pour_layers]
    tracked = via_ok = 0
    r2 = pcbnew.FromMM(0.3) ** 2
    for p in pads:
        pp = p.GetPosition()
        if any(p.IsOnLayer(t.GetLayer()) and min((t.GetStart().x - pp.x) ** 2 + (t.GetStart().y - pp.y) ** 2,
                                                  (t.GetEnd().x - pp.x) ** 2 + (t.GetEnd().y - pp.y) ** 2) < r2 for t in tracks):
            tracked += 1
            via_ok += 1 if PL.hole_ok(board, pp.x, pp.y) else 0
    return {"pour_layers": sorted(pour_layers), "pads": len(pads), "track_made_pads": tracked, "via_into_pour_sites": via_ok}


def run(board_in, board_out, plane_nets, conn, log=None, workdir=None, plane_layers=(), rules=None):
    """`plane_layers`: the copper layers the router may not use (the planes); a pour on any other layer is a signal-layer pour.
    Tie every floating cluster of each plane net to its plane, refill, save board_out.
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
    census = {n: pour_census(board, n) for n in plane_nets}
    for n in targets:
        layers = plane_zone_layers(board, n)
        r = {"open_before": before[n], "layer": layers[0] if layers else None, "vias": 0, "reason": None,
             "signal_pour": bool(census.get(n) and [l for l in census[n]["pour_layers"] if l not in plane_layers]),
             "census": census.get(n)}
        if not layers:
            r["reason"] = "no zone of this net on any layer (pour missing)"
        else:
            for ly in layers:
                kw = {"via_d": rules["via"], "via_drill": rules["drill"]} if rules else {}
                r["vias"] += PL.tie_floating_clusters(board, n, ly, **kw)
            r["vias"] += _pad_ties(board, n, pad_pts.get(n, []), rules)
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


def run_guarded(board_in, board_out, plane_nets, conn, log=None, workdir=None, plane_layers=(), rules=None):
    """`run`, one plane net at a time, each accepted only when kicad-cli finds no violation key the board did not already have
    (drcguard); a net whose ties add one is dropped and says why.  -> the merged `run` record."""
    import shutil
    from . import drcguard as DG
    cur = board_in + ".guard.kicad_pcb"
    shutil.copy(board_in, cur)
    side = os.path.splitext(board_in)[0] + ".kicad_pro"
    if os.path.exists(side):
        shutil.copy(side, os.path.splitext(cur)[0] + ".kicad_pro")
    rec = {"placed": 0, "per_net": {}, "board": board_out}
    try:
        base = DG.drc_keys(cur)[0]
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:       # no DRC, no guard: no ties, never unguarded
        before = conn(cur)
        rec["per_net"] = {n: {"open_before": before.get(n, 0), "open_after": before.get(n, 0), "layer": None, "vias": 0,
                              "signal_pour": False, "census": None, "reason": f"ties skipped: DRC guard unavailable ({e})"}
                          for n in plane_nets if before.get(n)}
        shutil.copy(cur, board_out)
        return rec
    for n in plane_nets:
        if not conn(cur).get(n):
            continue
        tmp = board_in + ".try.kicad_pcb"
        shutil.copy(os.path.splitext(cur)[0] + ".kicad_pro", os.path.splitext(tmp)[0] + ".kicad_pro")
        r = run(cur, tmp, [n], conn, log, workdir, plane_layers, rules)
        if not r["placed"]:
            rec["per_net"].update(r["per_net"])
            continue
        keys = DG.drc_keys(tmp)[0]
        new = DG.new_keys(base, keys)
        if new:
            r["per_net"][n]["reason"] = f"ties dropped: {sum(new.values())} new DRC violation(s), e.g. {sorted(new)[0][0]}"
            r["per_net"][n].update(vias=0, open_after=r["per_net"][n]["open_before"])
            rec["per_net"][n] = r["per_net"][n]
            continue
        shutil.copy(tmp, cur)
        base = keys
        rec["per_net"].update(r["per_net"])
        rec["placed"] += r["placed"]
    shutil.copy(cur, board_out)
    return rec


def lines(rec):
    """one line per plane net for the stage text / route_owners"""
    return [f"{n}: open {r['open_before']} -> {r['open_after']} via(s) {r['vias']} layer {r['layer']}"
            + (f" REASON {r['reason']}" if r.get("reason") and r.get("open_after") else "")
            for n, r in sorted(rec["per_net"].items())]

