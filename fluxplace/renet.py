"""Re-net on a routed board: at route entry, find copper that touches a pad of another net (a netlist change after routing), strip only that
copper plus the strands the removal leaves dangling, and hand the affected nets to the route stage as ordinary open nets.
Board geometry only (pcbnew read, text write through boardwrite); no ref, net name or constant of any board."""
import collections
import os

from . import boardwrite as BW


def _scan(b):
    """-> (indexes into b.GetTracks() to strip, tracks, pad refs that disagree with their copper)."""
    import pcbnew
    tr = list(b.GetTracks())
    pads = [p for f in b.GetFootprints() for p in f.Pads()]
    pt = lambda p: (int(p.x), int(p.y))                                       # noqa: E731
    via = [t.GetClass() == "PCB_VIA" for t in tr]
    net = [t.GetNetname() for t in tr]
    ends = [[pt(t.GetPosition())] if v else [pt(t.GetStart()), pt(t.GetEnd())] for t, v in zip(tr, via)]
    hit = lambda p, e, t: p.HitTest(pcbnew.VECTOR2I(*e)) and (p.IsOnLayer(t.GetLayer()) if not isinstance(t, pcbnew.PCB_VIA)      # noqa: E731
                                                                else p.IsOnLayer(t.TopLayer()) or p.IsOnLayer(t.BottomLayer()))
    touch = collections.defaultdict(dict)                                     # item -> {end point: [pads]}
    for i, t in enumerate(tr):
        for e in ends[i]:
            ps = [p for p in pads if hit(p, e, t)]
            if ps:
                touch[i][e] = ps
    seeds = {i for i, d in touch.items() if any(p.GetNetname() != net[i] for ps in d.values() for p in ps)}
    if not seeds:
        return [], tr, []
    at = collections.defaultdict(list)
    for i, es in enumerate(ends):
        for e in es:
            at[(net[i], e)].append(i)
    nbr = lambda i, gone=(): {j for e in ends[i] for j in at[(net[i], e)] if j != i and j not in gone}      # noqa: E731
    comp, todo = set(seeds), list(seeds)
    while todo:
        for j in nbr(todo.pop()):
            if j not in comp:
                comp.add(j)
                todo.append(j)
    gone = set(seeds)
    grew = True
    while grew:                                                               # strands left dangling by the removal go too
        grew = False
        for i in sorted(comp - gone):
            live = nbr(i, gone)
            padded = {e for e, ps in touch[i].items() if any(p.GetNetname() == net[i] for p in ps)}
            if via[i]:
                dead = not padded and len({tr[j].GetLayer() for j in live if not via[j]}) < 2
            else:
                dead = any(e not in padded and not any(e in ends[j] for j in live) for e in ends[i])
            if dead:
                gone.add(i)
                grew = True
    refs = sorted({f"{p.GetParentFootprint().GetReference()}.{p.GetNumber()}" for i in seeds for ps in touch[i].values()
                   for p in ps if p.GetNetname() != net[i]})
    return sorted(gone), tr, refs


def apply(board, out):
    """Write `out` = `board` minus its mismatched copper (`board` is never touched). -> report {nets, removed {net: n}, pads, items}, or None
    (nothing written) when no copper disagrees with a pad net.  A strip that does not remove exactly what was chosen raises."""
    import pcbnew
    b = pcbnew.LoadBoard(board)
    gone, tr, refs = _scan(b)
    if not gone:
        return None
    gs = set(gone)
    segs, vias, si, vi = set(), set(), 0, 0
    for k, t in enumerate(tr):                                                # file order, as boardwrite.strip_indexed counts it
        if t.GetClass() == "PCB_VIA":
            vi += 1
            if k in gs:
                vias.add(vi - 1)
        else:
            si += 1
            if k in gs:
                segs.add(si - 1)
    text = BW.strip_indexed(open(board, errors="surrogateescape").read(), segs, vias)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", errors="surrogateescape") as fh:
        fh.write(text)
    want = collections.Counter(BW.row_hash(BW.item_row(b, tr[i])) for i in gone)
    have = collections.Counter(BW.row_hash(BW.item_row(b, t)) for t in tr)
    left = collections.Counter(h for rows in BW.net_rows(out).values() for h, _ in rows)
    left.update(BW.row_hash(BW.item_row(b, t)) for t in tr if not t.GetNetname())
    if have - left != want:
        os.remove(out)
        raise RuntimeError("re-net strip removed other copper than chosen")
    removed = collections.Counter(tr[i].GetNetname() for i in gone)
    return {"nets": sorted(removed), "removed": dict(sorted(removed.items())), "pads": refs, "items": len(gone)}
