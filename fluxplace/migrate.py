"""Layer-count migration: promote a board to more layers WITHOUT re-placing it.

The expensive part of a board is the placement and the routing topology, not the
layer count. When a 4-layer board turns out to have no usable reference plane —
because both inner layers are carrying signals — the fix is not to start again.
It is to add the two layers the design needed all along, slide the existing inner
routing onto them, and let the freed layers become the planes they were always
supposed to be.

Everything else is preserved exactly: component positions, rotations, sides, all
outer-layer routing, and every via. This is the same reasoning that produced the
V1.3 board from V1.2's proven placement — reuse what is known good, change only
what must change.

Why this is safe on a typical board: through-hole vias span the whole stack by
definition, so a via that connected F.Cu to B.Cu on 4 layers connects F.Cu to
B.Cu on 6. Only a board using blind or buried vias needs care, and migrate()
refuses those rather than guessing.

  4-layer                             6-layer
  F.Cu    signal          ------->    F.Cu    signal        (unchanged)
  In1.Cu  signal + pour   ------->    In2.Cu  signal
  In2.Cu  signal + pour   ------->    In3.Cu  signal
  B.Cu    signal          ------->    B.Cu    signal        (unchanged)
                                      In1.Cu  GND PLANE     (freed)
                                      In4.Cu  PWR PLANE     (freed)
"""

import os
import shutil


# KiCad copper layer ids. F.Cu is 0 and B.Cu is 2; inner layers are the even
# numbers from 4 upward, in order.
F_CU, B_CU = 0, 2
IN = {1: 4, 2: 6, 3: 8, 4: 10, 5: 12, 6: 14}


def plan_4l_to_6l():
    """The layer remap, as data so it can be shown before it is done."""
    return {
        "moves": [
            {"from": "In1.Cu", "to": "In2.Cu",
             "why": "frees In1.Cu to become the solid GND reference plane"},
            {"from": "In2.Cu", "to": "In3.Cu",
             "why": "frees In4.Cu to become the solid PWR plane"},
        ],
        "unchanged": ["F.Cu", "B.Cu"],
        "new_planes": [{"layer": "In1.Cu", "role": "GND reference"},
                       {"layer": "In4.Cu", "role": "PWR"}],
    }


def inspect(board_path):
    """What a migration would have to handle. Read-only."""
    import pcbnew
    b = pcbnew.LoadBoard(board_path)
    n_cu = b.GetCopperLayerCount()

    vias, exotic = 0, 0
    for t in b.GetTracks():
        if t.GetClass() != "PCB_VIA":
            continue
        vias += 1
        if not (t.TopLayer() == F_CU and t.BottomLayer() == B_CU):
            exotic += 1

    per_layer = {}
    for t in b.GetTracks():
        if t.GetClass() == "PCB_TRACK":
            nm = b.GetLayerName(t.GetLayer())
            d = per_layer.setdefault(nm, {"mm": 0.0, "nets": set()})
            d["mm"] += pcbnew.ToMM(t.GetLength())
            d["nets"].add(t.GetNetname())

    zones = []
    for z in b.Zones():
        seq = z.GetLayerSet().Seq()
        zones.append({"net": z.GetNetname(),
                      "layers": [b.GetLayerName(seq[i]) for i in range(len(seq))]})

    return {
        "copper_layers": n_cu,
        "footprints": len(list(b.GetFootprints())),
        "vias": vias,
        "blind_or_buried_vias": exotic,
        "migratable": n_cu == 4 and exotic == 0,
        "routing": {k: {"mm": round(v["mm"], 1), "nets": len(v["nets"])}
                    for k, v in sorted(per_layer.items())},
        "zones": zones,
    }


def migrate_4l_to_6l(board_path, gnd_net="GND", pwr_net=None,
                     backup=True, log=print):
    """Promote a 4-layer board to 6 layers, freeing In1.Cu and In4.Cu as planes.

    Returns a dict describing what changed. Does NOT refill zones or write the
    stackup — those are separate, explicit steps, because a caller may want to
    inspect the remap before committing to a fill.
    """
    import pcbnew
    info = inspect(board_path)
    if info["copper_layers"] != 4:
        return {"changed": False,
                "reason": "board has %d copper layers, expected 4"
                          % info["copper_layers"]}
    if info["blind_or_buried_vias"]:
        return {"changed": False,
                "reason": "%d blind/buried via(s) present; their spans would have "
                          "to be re-derived, which this migration will not guess"
                          % info["blind_or_buried_vias"]}

    if backup:
        bak = board_path + ".bak-4layer"
        shutil.copy2(board_path, bak)
        log("backup: %s" % bak)

    b = pcbnew.LoadBoard(board_path)

    # 1. widen the stack FIRST so the destination layers exist
    b.SetCopperLayerCount(6)
    ls = b.GetEnabledLayers()
    for n in (3, 4):
        ls.addLayer(IN[n])
    b.SetEnabledLayers(ls)
    b.SetLayerName(IN[1], "In1.Cu")
    b.SetLayerName(IN[2], "In2.Cu")
    b.SetLayerName(IN[3], "In3.Cu")
    b.SetLayerName(IN[4], "In4.Cu")

    # 2. slide inner routing outward: In2->In3 BEFORE In1->In2, or the first
    #    move would collide with the second's source layer.
    moved = {"In2.Cu->In3.Cu": 0, "In1.Cu->In2.Cu": 0}
    for t in b.GetTracks():
        if t.GetClass() != "PCB_TRACK":
            continue
        if t.GetLayer() == IN[2]:
            t.SetLayer(IN[3]); moved["In2.Cu->In3.Cu"] += 1
    for t in b.GetTracks():
        if t.GetClass() != "PCB_TRACK":
            continue
        if t.GetLayer() == IN[1]:
            t.SetLayer(IN[2]); moved["In1.Cu->In2.Cu"] += 1

    # 3. the old inner pours move with their layers; the PWR pour lands on In4
    zones_moved = []
    for z in b.Zones():
        seq = z.GetLayerSet().Seq()
        names = [b.GetLayerName(seq[i]) for i in range(len(seq))]
        if "In2.Cu" in names:                       # old +5V pour
            new = pcbnew.LSET()
            new.addLayer(IN[4])
            z.SetLayerSet(new)
            zones_moved.append({"net": z.GetNetname(), "to": "In4.Cu"})
        elif "In1.Cu" in names:                     # old GND pour: stays put
            zones_moved.append({"net": z.GetNetname(), "to": "In1.Cu (unchanged)"})

    b.Save(board_path)
    return {
        "changed": True,
        "copper_layers": 6,
        "tracks_moved": moved,
        "zones": zones_moved,
        "vias_untouched": info["vias"],
        "footprints_untouched": info["footprints"],
        "backup": (board_path + ".bak-4layer") if backup else None,
        "next": ["write the 6-layer stackup (fluxplace stackup-define --apply)",
                 "refill zones",
                 "re-run DRC at full scope"],
    }


def refill(board_path, log=print):
    """Refill every zone. Mandatory after moving copper — a stale fill reports
    clearance violations that do not exist and hides ones that do."""
    import pcbnew
    b = pcbnew.LoadBoard(board_path)
    zones = b.Zones()
    ok = pcbnew.ZONE_FILLER(b).Fill(zones)
    b.Save(board_path)
    log("refilled %d zone(s): %s" % (len(zones), "ok" if ok else "FAILED"))
    return bool(ok)


def stitch_to_planes(board_path, drc_report, plane_layers=None, log=print):
    """Add a via wherever the layer migration left a track hanging.

    Moving a pour to a different layer strands every stub that used to terminate
    IN that pour: the copper is still there, the plane it reached is not. The
    connectivity is one via deep — a through via passes through the new plane
    layers by definition — but the vias have to be placed, and only where they
    are actually needed.

    So this is driven by the DRC report rather than by guessing: it adds a via
    at each reported dangling endpoint and each unconnected item, on that item's
    own net. Foreign-net planes keep their clearance around the new via
    automatically when the zones are refilled, which is why refill() must follow.
    """
    import json
    import pcbnew

    with open(drc_report) as fh:
        rep = json.load(fh)

    wanted = []
    for v in rep.get("violations", []):
        if v.get("type") in ("track_dangling", "via_dangling"):
            for it in v.get("items", []):
                p = it.get("pos") or {}
                net = _net_from_desc(it.get("description", ""))
                if net and "x" in p:
                    wanted.append((net, p["x"], p["y"]))
    for u in rep.get("unconnected_items", []):
        for it in u.get("items", []):
            p = it.get("pos") or {}
            net = _net_from_desc(it.get("description", ""))
            if net and "x" in p:
                wanted.append((net, p["x"], p["y"]))

    # dedupe by net + rounded position
    seen, uniq = set(), []
    for net, x, y in wanted:
        key = (net, round(x, 3), round(y, 3))
        if key not in seen:
            seen.add(key); uniq.append((net, x, y))

    b = pcbnew.LoadBoard(board_path)
    ds = b.GetDesignSettings()
    added = []
    for net, x, y in uniq:
        ni = b.FindNet(net)          # NETNAMES_MAP is not a dict; FindNet is the API
        if ni is None:
            log("  no such net on the board: %s" % net); continue
        v = pcbnew.PCB_VIA(b)
        v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetLayerPair(F_CU, B_CU)
        try:
            v.SetWidth(ds.GetCurrentViaSize())
            v.SetDrill(ds.GetCurrentViaDrill())
        except Exception:
            v.SetWidth(pcbnew.FromMM(0.45)); v.SetDrill(pcbnew.FromMM(0.2))
        v.SetNet(ni)
        b.Add(v)
        added.append({"net": net, "x": round(x, 3), "y": round(y, 3)})
    if added:
        b.Save(board_path)
    log("stitched %d via(s) to the plane layers" % len(added))
    return {"added": added, "count": len(added)}


def _net_from_desc(desc):
    """'Track [+5V] on In3.Cu, length ...' -> '+5V'."""
    import re
    m = re.search(r"\[([^\]]+)\]", desc or "")
    return m.group(1) if m else None


def absorb_into_plane(board_path, drc_report, plane_of=None, log=print):
    """Move stranded stubs ONTO the plane layer of their own net.

    Better than stitching a via for the common case. When a pour moves to a new
    layer, the stubs it used to feed are on the SAME net as that pour — so the
    fix is not to drill down to the plane, it is to put the stub on the plane,
    where the fill absorbs it.

    Learned the hard way: the via approach placed through-vias under a 0.4 mm
    pitch CM5 connector and produced 15 shorting_items and 6 solder-mask bridges
    against its pads. A stub stranded under a dense connector is exactly where
    there is no room to drill, and that is precisely where these stubs live,
    because that is where the routing was tightest in the first place.

    plane_of maps net name -> plane layer name. Inferred from the zones if None.
    """
    import json
    import pcbnew

    b = pcbnew.LoadBoard(board_path)

    if plane_of is None:
        plane_of = {}
        for z in b.Zones():
            seq = z.GetLayerSet().Seq()
            for i in range(len(seq)):
                nm = b.GetLayerName(seq[i])
                if nm.startswith("In"):              # inner pours are the planes
                    plane_of.setdefault(z.GetNetname(), nm)

    with open(drc_report) as fh:
        rep = json.load(fh)

    targets = set()
    for v in rep.get("violations", []):
        if v.get("type") in ("track_dangling",):
            for it in v.get("items", []):
                p, net = it.get("pos") or {}, _net_from_desc(it.get("description", ""))
                if net and "x" in p:
                    targets.add((net, round(p["x"], 3), round(p["y"], 3)))
    for u in rep.get("unconnected_items", []):
        for it in u.get("items", []):
            p, net = it.get("pos") or {}, _net_from_desc(it.get("description", ""))
            if net and "x" in p:
                targets.add((net, round(p["x"], 3), round(p["y"], 3)))

    # Only ever absorb from an INNER layer. An outer-layer track terminates on
    # outer-layer pads; moving it to a plane disconnects it from the very pads
    # it exists to reach. (Caught the hard way: absorbing two B.Cu tracks onto
    # In4.Cu turned 0 unconnected into 2.)
    inner = {IN[n] for n in IN}
    moved = []
    for t in list(b.GetTracks()):
        if t.GetClass() != "PCB_TRACK":
            continue
        if t.GetLayer() not in inner:
            continue
        net = t.GetNetname()
        dest = plane_of.get(net)
        if not dest:
            continue
        lid = b.GetLayerID(dest)
        if lid < 0 or t.GetLayer() == lid:
            continue
        s, e = t.GetStart(), t.GetEnd()
        hit = any((net, round(pcbnew.ToMM(p.x), 3), round(pcbnew.ToMM(p.y), 3)) in targets
                  for p in (s, e))
        if hit:
            old = b.GetLayerName(t.GetLayer())
            t.SetLayer(lid)
            moved.append({"net": net, "from": old, "to": dest,
                          "at": (round(pcbnew.ToMM(s.x), 3), round(pcbnew.ToMM(s.y), 3))})
    if moved:
        b.Save(board_path)
    log("absorbed %d stranded track(s) onto their plane layer" % len(moved))
    return {"moved": moved, "count": len(moved)}


def remove_vias_at(board_path, positions, net=None, log=print):
    """Remove vias at given (x, y) mm positions — undo for a bad stitch."""
    import pcbnew
    b = pcbnew.LoadBoard(board_path)
    want = {(round(x, 3), round(y, 3)) for x, y in positions}
    gone = 0
    for t in list(b.GetTracks()):
        if t.GetClass() != "PCB_VIA":
            continue
        if net and t.GetNetname() != net:
            continue
        p = t.GetPosition()
        if (round(pcbnew.ToMM(p.x), 3), round(pcbnew.ToMM(p.y), 3)) in want:
            b.Remove(t); gone += 1
    if gone:
        b.Save(board_path)
    log("removed %d via(s)" % gone)
    return gone


def converge(board_path, kicad_cli="kicad-cli", max_rounds=6, log=print):
    """Absorb -> refill -> re-DRC until no track is left dangling.

    One pass is not enough and that is not a bug in the absorber. Refilling the
    zones changes what the fill touches, which can strand a stub that the
    previous DRC had no reason to report. So the honest loop is: fix what the
    report names, refill, ask again, and stop when a round finds nothing new.

    Returns the history so a caller can see it actually converged rather than
    merely stopping at the round limit.
    """
    import json
    import subprocess
    import tempfile

    history = []
    for rnd in range(1, max_rounds + 1):
        rpt = os.path.join(tempfile.gettempdir(),
                           "fluxplace-converge-%d.json" % rnd)
        subprocess.run([kicad_cli, "pcb", "drc", "--format", "json",
                        "--severity-all", "--output", rpt, board_path],
                       capture_output=True, text=True, timeout=1800)
        if not os.path.exists(rpt):
            return {"converged": False, "reason": "DRC produced no report",
                    "history": history}
        with open(rpt) as fh:
            rep = json.load(fh)
        dangling = [v for v in rep.get("violations", [])
                    if v.get("type") in ("track_dangling", "via_dangling")]
        unconn = rep.get("unconnected_items", [])
        history.append({"round": rnd, "dangling": len(dangling),
                        "unconnected": len(unconn),
                        "violations": len(rep.get("violations", []))})
        log("  round %d: %d dangling, %d unconnected, %d violations total"
            % (rnd, len(dangling), len(unconn), len(rep.get("violations", []))))
        if not dangling and not unconn:
            return {"converged": True, "rounds": rnd, "history": history,
                    "final_violations": len(rep.get("violations", []))}
        res = absorb_into_plane(board_path, rpt, log=lambda *_: None)
        if res["count"] == 0:
            return {"converged": False,
                    "reason": "nothing left to absorb but %d still dangling — "
                              "these need a routing decision, not a layer move"
                              % len(dangling),
                    "history": history}
        refill(board_path, log=lambda *_: None)
    return {"converged": False, "reason": "hit the round limit",
            "history": history}
