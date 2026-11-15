"""Via-centre snap: `fluxplace snap-vias BOARD --out O`; also run after every routing merge.

A track end inside a via's copper on the SAME net (and a layer the via reaches) is moved to the via centre.  Track ends that
sit at the same point as a moved end (joints) move with it, so connectivity cannot change.  Arcs are left alone.  KiCad DRC
reports the unsnapped ends as `track_not_centered_on_via`."""
import math
import os
import re
import shutil

from . import boardtext as BT

_COPPER = re.compile(r'\(\d+ "([^"]+\.Cu)"')
COINCIDENT = 0.005        # mm: ends this close to a moved end are the same joint


def _layers_of(vblk, copper):
    m = re.search(r"\(layers((?:\s+\"[^\"]*\")+)\s*\)", vblk)
    names = re.findall(r'"([^"]*)"', m.group(1)) if m else []
    names = [n for n in names if n in copper] or list(copper)
    if len(names) >= 2 and copper:
        i, j = sorted((copper.index(names[0]), copper.index(names[-1])))
        return set(copper[i:j + 1])
    return set(names)


def _fmt(v):
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _scan(text):
    """-> (vias, segs, via_at) of a board text; via_at(point, net, layer) = the same-net via whose copper holds the point."""
    copper = _COPPER.findall(text[:20000])
    items = BT.scan(text)
    vias = []
    segs = []
    for i, j, key, blk in items:
        if key[0] == "v":
            sz = float(key[3] or 0)
            vx, vy = BT._num2(blk, "at")
            vias.append({"x": vx, "y": vy, "r": sz / 2, "net": BT.net_name(blk),
                         "layers": _layers_of(blk, copper)})
        elif blk.startswith("(segment"):
            a = BT._num2(blk, "start")
            b = BT._num2(blk, "end")
            ly = re.search(r'\(layer "([^"]*)"\)', blk)
            segs.append({"i": i, "j": j, "blk": blk, "a": a, "b": b, "layer": ly.group(1) if ly else "",
                         "net": BT.net_name(blk), "locked": BT.is_locked(blk)})
    if not vias or not segs:
        return vias, segs, None
    cell = {}
    for v in vias:
        for cx in range(int((v["x"] - v["r"]) // 1.0), int((v["x"] + v["r"]) // 1.0) + 1):
            for cy in range(int((v["y"] - v["r"]) // 1.0), int((v["y"] + v["r"]) // 1.0) + 1):
                cell.setdefault((cx, cy), []).append(v)

    def via_at(p, net, layer):
        for v in cell.get((int(p[0] // 1.0), int(p[1] // 1.0)), ()):
            if v["net"] == net and layer in v["layers"] and math.hypot(p[0] - v["x"], p[1] - v["y"]) < v["r"] - 1e-3:
                return v
        return None
    return vias, segs, via_at


def off_centre(text):
    """Track ends (locked or not) inside a same-net via's copper, off its centre, with no same-net track end on that layer at the
    via centre (a through-joint is fine) -> [(net, layer, x, y, locked)].  Approximates KiCad's track_not_centered_on_via."""
    vias, segs, via_at = _scan(text)
    if via_at is None:
        return []
    cen = set()
    for s in segs:
        for p in (s["a"], s["b"]):
            v = via_at(p, s["net"], s["layer"])
            if v and math.hypot(p[0] - v["x"], p[1] - v["y"]) <= 1e-3:
                cen.add((id(v), s["layer"]))
    out = []
    for s in segs:
        for p in (s["a"], s["b"]):
            v = via_at(p, s["net"], s["layer"])
            if v and math.hypot(p[0] - v["x"], p[1] - v["y"]) > 1e-3 and (id(v), s["layer"]) not in cen:
                out.append((s["net"], s["layer"], round(p[0], 3), round(p[1], 3), s["locked"]))
    return out


def snap_text(text):
    """-> (new text, number of track ends moved, number of vias involved)."""
    vias, segs, via_at = _scan(text)
    if via_at is None:
        return text, 0, 0
    # moved ends: {(seg idx, 0|1): via}
    move = {}
    for k, s in enumerate(segs):
        if s["locked"]:                      # locked (fan-out) copper is never edited
            continue
        for e, p in ((0, s["a"]), (1, s["b"])):
            v = via_at(p, s["net"], s["layer"])
            if v and math.hypot(p[0] - v["x"], p[1] - v["y"]) > 1e-6:
                move[(k, e)] = v
    if not move:
        return text, 0, 0
    # joints: other ends coincident (same net and layer) with a moved end move with it
    ends = {}
    for k, s in enumerate(segs):
        for e, p in ((0, s["a"]), (1, s["b"])):
            ends.setdefault((s["net"], s["layer"], round(p[0] / 0.01), round(p[1] / 0.01)), []).append((k, e, p))
    for (k, e), v in list(move.items()):
        p = segs[k]["a"] if e == 0 else segs[k]["b"]
        s = segs[k]
        gx, gy = round(p[0] / 0.01), round(p[1] / 0.01)
        mates = [(k2, e2) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                 for k2, e2, p2 in ends.get((s["net"], s["layer"], gx + dx, gy + dy), ())
                 if (k2, e2) != (k, e) and math.hypot(p2[0] - p[0], p2[1] - p[1]) <= COINCIDENT]
        if any(segs[k2]["locked"] for k2, _e2 in mates):         # a joint with locked copper stays as it is
            del move[(k, e)]
            continue
        for k2, e2 in mates:
            if (k2, e2) not in move:
                move[(k2, e2)] = v
    out, last, n = [], 0, 0
    by_seg = {}
    for (k, e), v in move.items():
        by_seg.setdefault(k, {})[e] = v
    for k in sorted(by_seg):
        s = segs[k]
        blk = s["blk"]
        for e, v in by_seg[k].items():
            tag = "start" if e == 0 else "end"
            blk = re.sub(r"\(%s %s %s\)" % (tag, BT._NUM, BT._NUM), "(%s %s %s)" % (tag, _fmt(v["x"]), _fmt(v["y"])),
                         blk, count=1)
            n += 1
        out.append(text[last:s["i"]])
        a, b = BT._num2(blk, "start"), BT._num2(blk, "end")
        if math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-6:          # both ends in one via: a zero-length stub, the via joins it
            last = s["j"] + 1 + (1 if text[s["j"] + 1:s["j"] + 2] == "\n" else 0)
            continue
        out.append(blk)
        last = s["j"] + 1
    out.append(text[last:])
    return "".join(out), n, len({id(v) for v in move.values()})


def snap_file(src, dst):
    text = open(src, errors="surrogateescape").read()
    new, n, nv = snap_text(text)
    if dst:
        with open(dst, "w", errors="surrogateescape") as fh:
            fh.write(new)
        a, b = os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(dst)[0] + ".kicad_pro"
        if os.path.exists(a) and not os.path.exists(b):
            shutil.copy(a, b)
    return n, nv


def snap_guarded(src, dst, report, iters=4, tol_mm=0.02):
    """Move every unlocked track end that lies inside a same-net via's copper (not at its centre) onto the centre, keeping a
    move only when it creates no violation that `src` did not already have (`report(board)` -> kicad-cli DRC json dict;
    a violation is old when one of the same type sits at the same place). A track named in a new violation is excluded and
    the pass repeats (`iters` times). -> {moved, before, after, accepted, why}; the board is written to `dst` only when it
    is accepted (fewer off-centre ends, no new violation)."""
    import pcbnew
    base = report(src)

    def where(d):
        return [(v["type"], [(i["pos"]["x"], i["pos"]["y"]) for i in v.get("items", [])]) for v in d.get("violations", [])
                if v["type"] != "track_not_centered_on_via"]

    def n_off(d):
        return sum(1 for v in d.get("violations", []) if v["type"] == "track_not_centered_on_via")
    base_p = where(base)

    def is_new(v):
        for t, ps in base_p:
            if t == v["type"] and len(ps) == len(v.get("items", [])) and all(
                    any(math.hypot(a[0] - i["pos"]["x"], a[1] - i["pos"]["y"]) < tol_mm for a in ps) for i in v["items"]):
                return False
        return True
    excl, best = set(), None
    for _ in range(iters):
        b = pcbnew.LoadBoard(src)
        vias = [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA"]
        moved = 0
        for t in [x for x in b.GetTracks() if x.GetClass() == "PCB_TRACK"]:
            if t.IsLocked() or t.m_Uuid.AsString() in excl:
                continue
            for end in ("s", "e"):
                p = t.GetStart() if end == "s" else t.GetEnd()
                hit = None
                for v in vias:
                    if v.GetNetCode() != t.GetNetCode() or not v.IsOnLayer(t.GetLayer()):
                        continue
                    dd = math.hypot(p.x - v.GetPosition().x, p.y - v.GetPosition().y) / 1e6
                    if 0 < dd <= v.GetWidth() / 2e6 and (hit is None or dd < hit[0]):
                        hit = (dd, v)
                if hit:
                    (t.SetStart if end == "s" else t.SetEnd)(hit[1].GetPosition())
                    moved += 1
        if not moved:
            break
        tmp = dst + ".try.kicad_pcb"
        b.Save(tmp)
        shutil.copy(os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(tmp)[0] + ".kicad_pro")
        d = report(tmp)
        new = [v for v in d.get("violations", []) if v["type"] != "track_not_centered_on_via" and is_new(v)]
        best = {"moved": moved, "before": n_off(base), "after": n_off(d), "new": len(new), "tmp": tmp}
        if not new:
            break
        for v in new:
            for i in v["items"]:
                if i.get("uuid") and str(i.get("description", "")).startswith("Track"):
                    excl.add(i["uuid"])
    if not best:
        return {"moved": 0, "before": n_off(base), "after": n_off(base), "accepted": False, "why": "nothing to move"}
    ok = best["new"] == 0 and best["after"] < best["before"]
    out = {"moved": best["moved"], "before": best["before"], "after": best["after"], "accepted": ok,
           "why": "" if ok else f"{best['new']} new violation(s) after {iters} pass(es)"}
    if ok:
        os.replace(best["tmp"], dst)
        side = os.path.splitext(src)[0] + ".kicad_pro"
        if os.path.exists(side):
            shutil.copy(side, os.path.splitext(dst)[0] + ".kicad_pro")
    elif os.path.exists(best["tmp"]):
        os.unlink(best["tmp"])
    for ext in (".kicad_pro", ".kicad_prl"):
        q = os.path.splitext(dst + ".try.kicad_pcb")[0] + ext
        if os.path.exists(q):
            os.unlink(q)
    return out


def cmd_snap(a):
    src = a.board
    out = a.out
    if os.path.abspath(out) == os.path.abspath(src):
        raise SystemExit("snap-vias: --out must differ from the input board")
    from . import routeconn as RC
    before = RC.open_by_net(src, fill=True, method="auto") if not a.no_check else None
    n, nv = snap_file(src, out)
    msg = f"snap-vias: {n} track end(s) moved to the centre of {nv} via(s)"
    if before is not None:
        after = RC.open_by_net(out, fill=True, method="auto")
        same = sum(before.values()) == sum(after.values())
        msg += f"; open connections {sum(before.values())} -> {sum(after.values())}" + ("" if same else "  CONNECTIVITY CHANGED")
        if not same:
            print(msg)
            return 1
    if a.drc:
        from . import routeaudit as RA
        d0, d1 = RA.drc_types(src), RA.drc_types(out)
        msg += f"; track_not_centered_on_via {d0.get('track_not_centered_on_via', 0)} -> {d1.get('track_not_centered_on_via', 0)}"
        new = {k: v for k, v in d1.items() if v > d0.get(k, 0)}
        if new:
            msg += f"; NEW DRC types/counts {new}"
    print(msg)
    print(f"wrote {out}")
    return 0


def add_parser(sub):
    p = sub.add_parser("snap-vias", help="track ends in a via go to its centre")
    p.add_argument("board")
    p.add_argument("--out", required=True)
    p.add_argument("--no-check", action="store_true", help="skip the open-connection comparison")
    p.add_argument("--drc", action="store_true", help="also print the track_not_centered_on_via DRC count before/after")
    p.set_defaults(fn=cmd_snap)
