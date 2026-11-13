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


def snap_text(text):
    """-> (new text, number of track ends moved, number of vias involved)."""
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
                         "net": BT.net_name(blk)})
    if not vias or not segs:
        return text, 0, 0
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
    # moved ends: {(seg idx, 0|1): via}
    move = {}
    for k, s in enumerate(segs):
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
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for k2, e2, p2 in ends.get((s["net"], s["layer"], gx + dx, gy + dy), ()):
                    if (k2, e2) not in move and math.hypot(p2[0] - p[0], p2[1] - p[1]) <= COINCIDENT:
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
    p = sub.add_parser("snap-vias", help="move track ends inside a via's copper (same net) to the via centre; connectivity unchanged")
    p.add_argument("board")
    p.add_argument("--out", required=True)
    p.add_argument("--no-check", action="store_true", help="skip the open-connection comparison")
    p.add_argument("--drc", action="store_true", help="also print the track_not_centered_on_via DRC count before/after")
    p.set_defaults(fn=cmd_snap)
