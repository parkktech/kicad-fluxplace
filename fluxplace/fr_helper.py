"""Runs under KiCad's python (has `pcbnew`), one board per process:  python3 fr_helper.py CMD ARGS.json
The pcbnew half of routefr.py (whole-board Freerouting).  Every command prints one JSON line.

  strip        {board, out, planes, netclasses}      copy without the pours on signal layers (they are obstacles in a DSN);
                                                     netclasses = the DSN's net classes (see _apply_classes)
  export       {board, dsn}                          Specctra DSN of `board`
  import       {board, ses, out, skip_nets}          session copper added to the board's own (kept) copper, zones refilled
  widen        {board, out, floor, clearance}        unlocked tracks below `floor` -> net-class width (else the floor) where
                                                     clearance to other-net copper allows; the rest are listed
  planenets    {board, planes}                       nets with a zone on a plane layer
  unlink       {board, out, uuids}                   remove the unlocked tracks/vias with these UUIDs (pre-route gate)
  fingerprint  {board, floor, planes}                locked-copper hash, track/via counts, sub-floor count, tracks on planes
"""
import hashlib
import importlib.util
import json
import os
import sys

import pcbnew

MM = pcbnew.ToMM


def _load(path):
    b = pcbnew.LoadBoard(path)
    if b is None:
        raise SystemExit("cannot load " + path)
    return b


def _tracks(b):
    try:
        return list(b.GetTracks())
    except TypeError:
        return []


def _is_via(t):
    return t.GetClass() == "PCB_VIA"


def _apply_classes(b, spec):
    """Net classes of the DSN copy (KiCad writes them as the router's `class` rules): `spec` = {"default": {width, clearance},
    "classes": [{"name", "width", "clearance", "nets"}]} in mm.  The default class takes the default values; every named
    class gets its nets.  Widths and clearances are whatever the caller resolved (never below the floors)."""
    ns = b.GetDesignSettings().m_NetSettings
    dn = ns.GetDefaultNetclass()
    d = spec.get("default") or {}
    if d.get("width"):
        dn.SetTrackWidth(pcbnew.FromMM(float(d["width"])))
    if d.get("clearance"):
        dn.SetClearance(pcbnew.FromMM(float(d["clearance"])))
    n = 0
    for c in spec.get("classes") or ():
        nc = pcbnew.NETCLASS(c["name"])
        nc.SetTrackWidth(pcbnew.FromMM(float(c["width"])))
        nc.SetClearance(pcbnew.FromMM(float(c["clearance"])))
        ns.SetNetclass(c["name"], nc)
        names = list(c.get("nets") or ())
        if c.get("prefixes"):                      # every board net under a pair family prefix (`[pairs.F]`)
            allnets = [str(k) for k in b.GetNetInfo().NetsByName().keys()]
            names += [x for x in allnets if any(x.startswith(p) for p in c["prefixes"]) and x not in names]
        for net in names:
            ni = b.FindNet(net)
            if ni is not None and ni.GetNetCode() > 0:
                ni.SetNetClass(nc)
                n += 1
    return {"classes": len(spec.get("classes") or ()), "nets_assigned": n}


def cmd_strip(a):
    b = _load(a["board"])
    planes = set(a.get("planes") or ())
    cls = _apply_classes(b, a["netclasses"]) if a.get("netclasses") else {}
    removed = kept = trimmed = 0
    hold = []
    for z in list(b.Zones()):
        if z.GetIsRuleArea():
            continue
        ids = list(z.GetLayerSet().Seq())
        cu = [l for l in ids if pcbnew.IsCopperLayer(l)]
        on_plane = [l for l in cu if pcbnew.LayerName(l) in planes]
        if cu and len(on_plane) == len(cu):
            kept += 1
            continue
        if on_plane:                                   # one zone over planes and signal layers: keep its plane part only
            ls = pcbnew.LSET()
            for l in on_plane:
                ls.AddLayer(l)
            z.SetLayerSet(ls)
            trimmed += 1
            continue
        b.Remove(z)
        hold.append(z)
        removed += 1
    pcbnew.SaveBoard(a["out"], b)
    return {"zones_removed": removed, "zones_kept": kept, "zones_trimmed": trimmed, **cls}


def cmd_export(a):
    b = _load(a["board"])
    ok = bool(pcbnew.ExportSpecctraDSN(b, a["dsn"]))
    return {"ok": ok and os.path.exists(a["dsn"]), "tracks": len(_tracks(b))}


def cmd_planenets(a):
    """Nets with a zone on one of the plane layers (the `planes` of the route stage)."""
    b = _load(a["board"])
    planes = set(a.get("planes") or ())
    nets = set()
    for z in b.Zones():
        if z.GetIsRuleArea():
            continue
        if any(pcbnew.LayerName(l) in planes for l in z.GetLayerSet().Seq()):
            nets.add(str(z.GetNetname()))
    return {"nets": sorted(n for n in nets if n)}


def cmd_unlink(a):
    """Remove the UNLOCKED tracks and vias whose UUIDs are listed (the pre-route gate's victims); locked copper stays and is
    reported.  -> removed/locked lists (net, kind, layer, start point in mm) and the UUIDs found on the board."""
    b = _load(a["board"])
    want = set(a.get("uuids") or ())
    removed, locked, hit = [], [], []
    for t in list(b.GetTracks()):
        u = str(t.m_Uuid.AsString())
        if u not in want:
            continue
        hit.append(u)
        st = t.GetStart()
        row = {"net": str(t.GetNetname()), "kind": "via" if _is_via(t) else "track",
               "layer": "" if _is_via(t) else b.GetLayerName(t.GetLayer()),
               "at": [round(MM(st.x), 3), round(MM(st.y), 3)], "uuid": u}
        if t.IsLocked():
            locked.append(row)
            continue
        b.Remove(t)
        removed.append(row)
    pcbnew.SaveBoard(a["out"], b)
    return {"removed": removed, "locked": locked, "uuids_hit": hit}


def _ses_module():
    spec = importlib.util.spec_from_file_location("fluxplace_ses", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ses.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def cmd_import(a):
    b = _load(a["board"])
    before = len(_tracks(b))
    info = {}
    nt, nv, nskip, _un = _ses_module().import_into(b, a["ses"], replace=False, skip_nets=a.get("skip_nets") or (),
                                                   dedupe=True, added=info)
    zones = len(b.Zones())
    if zones:
        pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    pcbnew.SaveBoard(a["out"], b)
    return {"tracks_before": before, "tracks_added": nt, "vias_added": nv, "unknown_nets": nskip,
            "skipped_items": info["skipped_items"], "duplicates": info["duplicates"], "zones": zones}


def _class_width(t):
    try:
        return int(t.GetEffectiveNetClass().GetTrackWidth())
    except Exception:                                   # noqa: BLE001 - no project next to the board
        return 0


def cmd_widen(a):
    b = _load(a["board"])
    floor, clr = pcbnew.FromMM(float(a["floor"])), pcbnew.FromMM(float(a.get("clearance") or 0.0))
    eps = 100                                           # nm: 0.1269999 is the floor
    tr = [t for t in _tracks(b) if not _is_via(t)]
    low = [t for t in tr if t.GetWidth() < floor - eps]
    locked = [t for t in low if t.IsLocked()]
    todo = [t for t in low if not t.IsLocked()]
    obst = []                                           # (bbox, net, layers-test, shape-getter)
    if todo:
        for t in _tracks(b):
            obst.append((t.GetBoundingBox(), t.GetNetCode(), t, None))
        for p in b.GetPads():
            obst.append((p.GetBoundingBox(), p.GetNetCode(), p, True))
    fixed, failed = [], []
    for t in todo:
        lay, net = t.GetLayer(), t.GetNetCode()
        wants = sorted({max(floor, _class_width(t)), floor}, reverse=True)
        reach = max(wants) // 2 + clr + 1000
        box = t.GetBoundingBox()
        box.Inflate(reach)
        near = [(o, pad) for bb, n, o, pad in obst if n != net and o is not t and o.IsOnLayer(lay) and box.Intersects(bb)]
        done = why = None
        for w in wants:
            seg = pcbnew.SHAPE_SEGMENT(t.GetStart(), t.GetEnd(), int(w))
            hit = None
            for o, pad in near:
                sh = o.GetEffectiveShape(lay)
                if sh.Collide(seg, max(0, clr - eps)):
                    hit = o
                    break
            if hit is None:
                done = w
                break
            why = (hit.GetClass(), hit.GetNetname())
        s, e = t.GetStart(), t.GetEnd()
        rec = {"net": t.GetNetname(), "layer": b.GetLayerName(lay), "from": round(MM(t.GetWidth()), 4),
               "at": [round(MM(s.x), 3), round(MM(s.y), 3), round(MM(e.x), 3), round(MM(e.y), 3)]}
        if done:
            t.SetWidth(int(done))
            rec["to"] = round(MM(done), 4)
            fixed.append(rec)
        else:
            rec["blocked_by"] = f"{why[0]} {why[1]}" if why else "?"
            failed.append(rec)
    if fixed:
        pcbnew.SaveBoard(a["out"], b)
    elif a["out"] != a["board"]:
        pcbnew.SaveBoard(a["out"], b)
    return {"subfloor": len(low), "locked": len(locked), "fixed": len(fixed), "failed": failed,
            "widths": sorted({r["from"] for r in fixed + failed})}


def cmd_fingerprint(a):
    b = _load(a["board"])
    floor = pcbnew.FromMM(float(a.get("floor") or 0.0))
    planes = set(a.get("planes") or ())
    rows, n_lock, sub, on_plane, nt, nv = [], 0, 0, 0, 0, 0
    wmin = None
    for t in _tracks(b):
        via = _is_via(t)
        nv += via
        nt += not via
        if not via:
            w = t.GetWidth()
            wmin = w if wmin is None else min(wmin, w)
            sub += bool(floor) and w < floor - 100
            on_plane += b.GetLayerName(t.GetLayer()) in planes
        if t.IsLocked():
            n_lock += 1
            s = t.GetPosition() if via else t.GetStart()
            e = s if via else t.GetEnd()
            rows.append((t.GetNetname(), "via" if via else b.GetLayerName(t.GetLayer()), s.x, s.y, e.x, e.y,
                         t.GetDrill() if via else t.GetWidth()))
    h = hashlib.sha1(json.dumps(sorted(rows)).encode()).hexdigest()[:16]
    return {"locked": n_lock, "locked_hash": h, "tracks": nt, "vias": nv, "subfloor": int(sub),
            "min_width": round(MM(wmin), 4) if wmin is not None else None, "plane_tracks": int(on_plane)}


def main():
    cmd, args = sys.argv[1], json.load(open(sys.argv[2]))
    out = {"strip": cmd_strip, "export": cmd_export, "import": cmd_import, "widen": cmd_widen,
           "fingerprint": cmd_fingerprint, "planenets": cmd_planenets,
           "unlink": cmd_unlink}[cmd](args)
    print(json.dumps(out), flush=True)
    sys.stdout.flush()
    os._exit(0)                                         # skip SWIG teardown (it can crash after the result is out)


main()
