"""Re-net on a routed board: at route entry, find copper that touches a pad of another net (a netlist change after routing), strip only that
copper plus the strands the removal leaves dangling, and hand the affected nets to the route stage as ordinary open nets.
Connectivity is routeconn's (T-junctions, vias inside a segment, rotated pads); no ref, net name or constant of any board."""
import collections
import os

from . import boardwrite as BW, netclass as NC, routeconn as RC, routediag as RD


def _scan(B):
    """-> (set of seg indexes, set of via indexes) to strip, the pad refs that disagree with their copper."""
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
            if RC.touch(B, items[i], items[j]):
                touch_pads[j - off].append(items[i][1])
        elif items[i][1]["net"] == items[j][1]["net"] and RC.touch(B, items[i], items[j]):
            same[i - off].add(j - off)
            same[j - off].add(i - off)
    net = [o["net"] for _, o in trk]
    seeds = {t for t, ps in touch_pads.items() if any(p["net"] != net[t] for p in ps)}
    if not seeds:
        return set(), set(), []
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
    refs = sorted({f"{p['ref']}.{p['num']}" for t in seeds for p in touch_pads[t] if p["net"] != net[t]})
    ns = len(B["segs"])
    return {t for t in gone if t < ns}, {t - ns for t in gone if t >= ns}, refs


def _rows(B):
    return collections.Counter([("seg", s["net"], s["layer"], tuple(s["a"]), tuple(s["b"]), s["w"]) for s in B["segs"]] +
                               [("via", v["net"], v["x"], v["y"], v["d"]) for v in B["vias"]])


def apply(board, out, cons=None):
    """Write `out` = `board` minus its mismatched copper (`board` is never touched). -> report {nets, release, removed {net: n}, pads, items}
    or None (nothing written) when no copper disagrees with a pad net.  `release` = `nets` plus the partner leg of every stripped pair leg
    (constraints), so the pairs machinery can re-lay the pair coupled.  A strip that removes other copper than chosen raises."""
    B = RD.load(board)
    segs, vias, refs = _scan(B)
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
    rel = set(removed) | {mate[n] for n in removed if n in mate}
    return {"nets": sorted(removed), "release": sorted(rel), "removed": dict(sorted(removed.items())), "pads": refs,
            "items": len(segs) + len(vias)}
