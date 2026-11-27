"""Re-net on a routed board: at route entry, find copper that touches a pad of another net (a netlist change after routing), strip only that
copper plus the strands the removal leaves dangling, and hand the affected nets to the route stage as ordinary open nets.
Connectivity is routeconn's (T-junctions, vias inside a segment, rotated pads); no ref, net name or constant of any board."""
import collections
import math
import os

from . import boardwrite as BW, netclass as NC, routeconn as RC, routediag as RD


def _crosses(p, seg):
    """True when the centre line of track `seg` runs through pad `p` (a track across a pad without ending in it; an in-place footprint swap
    puts new pads under shipped copper).  Centre line only: copper beside a pad is a clearance matter, not a connection."""
    if seg["layer"] not in p["layers"]:
        return False
    (x0, y0), (x1, y1) = seg["a"], seg["b"]
    n = max(1, int(math.hypot(x1 - x0, y1 - y0) / 0.05))
    return any(RC._in_pad(p, x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n, 0.0) for k in range(n + 1))


def _pad_gap(p, x, y):
    """Distance from point (x, y) to the pad's copper outline (0 inside): circle, oval (capsule) or the rotated rectangle (roundrect corners
    count as square, a conservative few um)."""
    rad = math.radians(-(p.get("angle") or 0.0))
    dx, dy = x - p["x"], y - p["y"]
    lx, ly = abs(dx * math.cos(rad) - dy * math.sin(rad)), abs(dx * math.sin(rad) + dy * math.cos(rad))
    w, h = p["w"] / 2, p["h"] / 2
    if p.get("shape") == "circle":
        return max(0.0, math.hypot(lx, ly) - max(w, h))
    if p.get("shape") == "oval":
        r = min(w, h)
        ax, ay = (max(lx - (w - r), 0.0), ly) if w >= h else (lx, max(ly - (h - r), 0.0))
        return max(0.0, math.hypot(ax, ay) - r)
    return math.hypot(max(lx - w, 0.0), max(ly - h, 0.0))


def _too_close(B, p, kind, o):
    """True when track/via `o` of another net is nearer to pad `p` than the board clearance on a layer both have copper on."""
    clr = B["clearance"]
    if kind == "via":
        return _pad_gap(p, o["x"], o["y"]) - o["d"] / 2 < clr
    if o["layer"] not in p["layers"]:
        return False
    (x0, y0), (x1, y1) = o["a"], o["b"]
    n = max(1, int(math.hypot(x1 - x0, y1 - y0) / 0.05))
    return any(_pad_gap(p, x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) - o["w"] / 2 < clr for k in range(n + 1))


def changed_refs(before, after):
    """Refs whose pad set (number, position, size) differs between two boards, or that only `after` has: the parts an in-place footprint
    swap / a seat put new pads under."""
    def sig(path):
        out = collections.defaultdict(set)
        for p in RD.load(path)["pads"]:
            out[p["ref"]].add((p["num"], round(p["x"], 2), round(p["y"], 2), round(p["w"], 2), round(p["h"], 2)))
        return out
    a, b = sig(before), sig(after)
    return {r for r, s in b.items() if a.get(r) != s}


def _near_pads(B, pads, trk, refs):
    """{track index: [pads]}: foreign-net copper inside the board clearance of a netted pad of one of `refs` (an in-place footprint swap seats
    new pads beside shipped vias/tracks: no overlap, so no connection, but a clearance violation).  Other parts' pads are left alone: shipped
    copper may sit under local rules below the default clearance."""
    pads = [p for p in pads if p["ref"] in refs and p["net"]]
    m = 2 * B["clearance"]
    fat = [(k, dict(o, w=o["w"] + m) if k == "seg" else dict(o, d=o["d"] + m)) for k, o in trk]
    off, out = len(pads), collections.defaultdict(list)
    for i, j in RC.near_pairs([("pad", p) for p in pads] + fat):
        i, j = min(i, j), max(i, j)
        if i < off <= j:
            p, (kind, o) = pads[i], trk[j - off]
            if p["net"] != o["net"] and _too_close(B, p, kind, o):
                out[j - off].append(p)
    return out


def _scan(B, near=()):
    """-> (seg indexes, via indexes) to strip, the pad refs that disagree with their copper, the nets those pads now carry.
    `near` (refs) also seeds foreign copper closer than the clearance to a pad of those parts (incremental seat)."""
    pads = [p for p in B["pads"] if not p.get("np")]
    trk = [("seg", s) for s in B["segs"]] + [("via", v) for v in B["vias"]]
    items = [("pad", p) for p in pads] + trk
    off = len(pads)
    touch_pads, same = collections.defaultdict(list), collections.defaultdict(set)      # track -> pads / same-net neighbours
    for i, j in RC.near_pairs(items):
        i, j = min(i, j), max(i, j)
        if j < off:
            continue
        if i < off:
            p, (kind, o) = items[i][1], items[j]
            if RC.touch(B, items[i], items[j]) or (kind == "seg" and p["net"] != o["net"] and _crosses(p, o)):
                touch_pads[j - off].append(p)
        elif items[i][1]["net"] == items[j][1]["net"] and RC.touch(B, items[i], items[j]):
            same[i - off].add(j - off)
            same[j - off].add(i - off)
    net = [o["net"] for _, o in trk]
    seeds = {t for t, ps in touch_pads.items() if any(p["net"] != net[t] for p in ps)}
    close = _near_pads(B, pads, trk, near) if near else {}
    seeds |= close.keys()
    if not seeds:
        return set(), set(), [], set()
    comp, todo = set(seeds), list(seeds)
    while todo:
        for u in same[todo.pop()]:
            if u not in comp:
                comp.add(u)
                todo.append(u)
    gone = set(seeds)

    def on_end(pt, layer, j):                       # does the point (a zero-length segment) touch track j
        return RC.touch(B, ("seg", {"a": pt, "b": pt, "w": 0.0, "layer": layer, "net": ""}), trk[j])

    grew = True
    while grew:                                     # strands left dangling by the removal go too
        grew = False
        for t in sorted(comp - gone):
            kind, o = trk[t]
            live = same[t] - gone
            own = [p for p in touch_pads[t] if p["net"] == net[t]]
            if kind == "via":
                dead = not own and len({trk[j][1]["layer"] for j in live if trk[j][0] == "seg"}) < 2
            else:
                dead = any(not any(RC._in_pad(p, *e, RC.TOL) and o["layer"] in p["layers"] for p in own)
                           and not any(on_end(e, o["layer"], j) for j in live) for e in (o["a"], o["b"]))
            if dead:
                gone.add(t)
                grew = True
    bad = [p for t in seeds for p in touch_pads[t] if p["net"] != net[t]]
    refs = {f"{p['ref']}.{p['num']}" for p in bad + [p for ps in close.values() for p in ps]}
    ns = len(B["segs"])
    return {t for t in gone if t < ns}, {t - ns for t in gone if t >= ns}, sorted(refs), {p["net"] for p in bad if p["net"]}


def _rows(B):
    return collections.Counter([("seg", s["net"], s["layer"], tuple(s["a"]), tuple(s["b"]), s["w"]) for s in B["segs"]] +
                               [("via", v["net"], v["x"], v["y"], v["d"]) for v in B["vias"]])


def apply(board, out, cons=None, near=()):
    """Write `out` = `board` minus its mismatched copper (`board` is never touched). -> report {nets, release, removed {net: n}, pads, items}
    or None (nothing written) when no copper disagrees with a pad net.  `release` = every net to re-route: `nets`, the nets the re-netted pads now carry, and the partner leg of each pair leg
    (constraints), so the pairs machinery can re-lay the pair coupled.  `near` = refs of seated/swapped parts: copper nearer than the clearance to another net's pad of those parts goes too.  A strip that removes other copper than chosen raises."""
    B = RD.load(board)
    segs, vias, refs, padnets = _scan(B, near)
    if not segs and not vias:
        return None
    with open(board, errors="surrogateescape") as fh:
        text = BW.strip_indexed(fh.read(), segs, vias)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", errors="surrogateescape") as fh:
        fh.write(text)
    want = _rows(dict(segs=[B["segs"][i] for i in segs], vias=[B["vias"][i] for i in vias]))
    if _rows(B) - _rows(RD.load(out)) != want:
        os.remove(out)
        raise RuntimeError("re-net strip removed other copper than chosen")
    removed = collections.Counter([B["segs"][i]["net"] for i in segs] + [B["vias"][i]["net"] for i in vias])
    mate = NC._pairs(sorted({p["net"] for p in B["pads"] if p["net"]}), cons or {})
    rel = set(removed) | padnets
    rel |= {mate[n] for n in rel if n in mate}
    return {"nets": sorted(removed), "release": sorted(rel), "removed": dict(sorted(removed.items())), "pads": refs,
            "items": len(segs) + len(vias)}
