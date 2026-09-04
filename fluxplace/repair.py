"""Copper REPAIRS — the mechanical fixes a review finding asks for, done by
the tool instead of by hand, so they are reproducible and DRC-guarded.

Each function takes a loaded pcbnew BOARD and mutates it; `cli.py repair`
strings them together, saves, and (optionally) runs the last-mile patcher
for anything the remap left unrouted.

  redundant_copper   2-pin nets: keep the shortest pad-to-pad path through
                     the existing tracks/vias, delete every other segment.
                     A router's leftover loop on ONE side of a Gigabit pair
                     was 45 mm of intra-pair skew on utv-comms V1.4.
  rf_widths          re-width RF segments to the impedance the LAYER they
                     are on actually needs (review.layer_geometry), instead
                     of one width for the whole net.
  remap_pins         move pads to new nets (a pinmap correction), rip the
                     stubs that fed the old nets, drop a GND via beside any
                     pad that became ground.
  fix_dup_pads       footprints with two pads sharing a number (KMR2 tact
                     switches, some connectors): the generator netted one of
                     them; copy the net to its twin.
  add_text           silkscreen text in a free spot (board id / version).
  bridge             maze-route one unconnected pad to its net across layers
                     when the patcher and freerouting both give up.
"""
import heapq
import math
import re

__all__ = ["redundant_copper", "rf_widths", "remap_pins", "fix_dup_pads",
           "add_text", "prune_dangling", "measure", "stitch", "stitch_islands",
           "clear_under", "bridge"]

# Proxies of items removed from a board must OUTLIVE the session: once Python
# garbage-collects one ("memory leak of type 'PCB_TRACK *', no destructor
# found") the SWIG type table degrades and board.GetTracks() starts returning
# an un-iterable SwigPyObject. Measured on KiCad 10.0.5. Park them here.
_GRAVEYARD = []


def _remove(board, item):
    board.Remove(item)
    _GRAVEYARD.append(item)


def _mm(v):
    return v / 1e6


def _key(pt, layer):
    return (round(_mm(pt.x), 3), round(_mm(pt.y), 3), int(layer))


def _cu_layers(board):
    """Enabled copper layer ids, front to back. Uses per-layer predicates
    rather than LSET/CuStack, which some SWIG builds hand back unwrapped."""
    import pcbnew
    n = board.GetCopperLayerCount()
    ids = [pcbnew.F_Cu] + [pcbnew.In1_Cu + 2 * i for i in range(max(0, n - 2))] \
        + ([pcbnew.B_Cu] if n > 1 else [])
    return [l for l in ids if board.IsLayerEnabled(l)]


def _pad_layers(pad, cu):
    return [l for l in cu if pad.IsOnLayer(l)]


def board_seq(cu):
    return {l: i for i, l in enumerate(cu)}


def _between(seq, l, a, b):
    i, ia, ib = seq[l], seq[a], seq[b]
    return min(ia, ib) <= i <= max(ia, ib)


def measure(board, prefix=None):
    """{net: (routed_mm, vias)} for nets starting with `prefix` (or all)."""
    out = {}
    for t in board.GetTracks():
        n = t.GetNetname()
        if not n or (prefix and not n.startswith(prefix)):
            continue
        L, v = out.get(n, (0.0, 0))
        if t.GetClass() == "PCB_VIA":
            out[n] = (L, v + 1)
        else:
            out[n] = (L + _mm(t.GetLength()), v)
    return out


# --------------------------------------------------------------- redundancy
def _net_graph(board, netcode, via_cost=0.3):
    """Nodes: (x,y,layer) endpoints + ('pad',ref,num). Edges: tracks (length),
    vias (via_cost between every layer pair they span), pad membership (0),
    and T-junctions (an endpoint resting on another segment's body)."""
    import pcbnew
    cu = _cu_layers(board)
    seq = board_seq(cu)
    adj = {}
    items = {}          # edge id -> board item

    def add(a, b, w, item=None):
        adj.setdefault(a, []).append((b, w, item))
        adj.setdefault(b, []).append((a, w, item))

    tracks, vias = [], []
    for t in board.GetTracks():
        if t.GetNetCode() != netcode:
            continue
        if t.GetClass() == "PCB_VIA":
            vias.append(t)
        elif t.GetClass() == "PCB_TRACK":
            tracks.append(t)
    for t in tracks:
        a, b = _key(t.GetStart(), t.GetLayer()), _key(t.GetEnd(), t.GetLayer())
        add(a, b, _mm(t.GetLength()), t)
    for v in vias:
        p = v.GetPosition()
        span = [l for l in cu if v.IsOnLayer(l)]
        for i in range(len(span)):
            for j in range(i + 1, len(span)):
                add(_key(p, span[i]), _key(p, span[j]), via_cost, v)
    # T-junctions: endpoint on another same-layer segment's body
    endpoints = [(k, pcbnew.VECTOR2I(int(k[0] * 1e6), int(k[1] * 1e6)))
                 for k in list(adj) if isinstance(k[0], float)]
    for t in tracks:
        la = t.GetLayer()
        a, b = _key(t.GetStart(), la), _key(t.GetEnd(), la)
        for k, pt in endpoints:
            if k[2] != la or k in (a, b):
                continue
            if t.HitTest(pt, int(t.GetWidth() / 2)):
                da = math.hypot(k[0] - a[0], k[1] - a[1])
                db = math.hypot(k[0] - b[0], k[1] - b[1])
                add(k, a, da, t)
                add(k, b, db, t)
    pads = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() != netcode:
                continue
            pnode = ("pad", fp.GetReference(), pad.GetNumber())
            pads.append(pnode)
            pl = set(_pad_layers(pad, cu))
            hit = False
            for k, pt in endpoints:
                if k[2] in pl and pad.HitTest(pt):
                    add(pnode, k, 0.0, None)
                    hit = True
            if not hit:
                adj.setdefault(pnode, [])
    return adj, pads


def _dijkstra(adj, src, dst):
    dist = {src: 0.0}
    prev = {}
    pq = [(0.0, 0, src)]
    n = 0
    while pq:
        d, _, u = heapq.heappop(pq)
        if u == dst:
            break
        if d > dist.get(u, 1e18):
            continue
        for v, w, item in adj.get(u, []):
            nd = d + w
            if nd < dist.get(v, 1e18):
                dist[v] = nd
                prev[v] = (u, item)
                n += 1
                heapq.heappush(pq, (nd, n, v))
    if dst not in dist:
        return None, None
    used = set()
    u = dst
    while u != src:
        p, item = prev[u]
        if item is not None:
            used.add(item.m_Uuid.AsString() if hasattr(item, "m_Uuid") else id(item))
        u = p
    return dist[dst], used


def redundant_copper(board, prefix="ETH_", nets=None, min_gain_mm=0.5, log=print):
    """Remove every track/via on a 2-pin net that is not on the shortest
    existing path between its two pads. Returns [(net, before, after)]."""
    # KiCad 10 python exposes neither NetsByName nor a usable FindNet proxy
    # here; the routed copper knows every (name, code) pair that matters
    codes = {}
    for t in board.GetTracks():
        n = t.GetNetname()
        if n and (not prefix or n.startswith(prefix)) and (not nets or n in nets):
            codes[n] = t.GetNetCode()
    out = []
    for name in sorted(codes):
        code = codes[name]
        if code <= 0:
            continue
        adj, pads = _net_graph(board, code)
        # a differential pair through a series element still has 2 pads per net
        if len(pads) != 2:
            log(f"    {name}: {len(pads)} pads — skipped (2-pin nets only)")
            continue
        d, used = _dijkstra(adj, pads[0], pads[1])
        if used is None:
            log(f"    {name}: pads not connected through copper — skipped")
            continue
        before = measure(board, name)[name][0]
        victims = [t for t in board.GetTracks()
                   if t.GetNetCode() == code and t.m_Uuid.AsString() not in used]
        gain = sum(_mm(t.GetLength()) for t in victims if t.GetClass() == "PCB_TRACK")
        if gain < min_gain_mm and not any(t.GetClass() == "PCB_VIA" for t in victims):
            continue
        for t in victims:
            _remove(board, t)
        after = measure(board, name).get(name, (0.0, 0))[0]
        log(f"    {name}: {before:.1f} -> {after:.1f} mm "
            f"(-{len(victims)} items, {gain:.1f} mm of loop/stub removed)")
        out.append((name, before, after))
    return out


# ---------------------------------------------------------------- RF widths
def rf_widths(board, stackup, planes, nets, target_z=50.0, tol_pct=5.0,
              w_min=0.1, w_max=1.0, log=print):
    """Set each RF segment's width to what ITS layer needs for target_z.
    Returns [(net, layer, old_w, new_w, mm)]."""
    import pcbnew
    from . import review as R
    from .stackup import _bisect
    solved = {}
    out = []
    for t in board.GetTracks():
        n = t.GetNetname()
        if n not in nets or t.GetClass() != "PCB_TRACK":
            continue
        layer = board.GetLayerName(t.GetLayer())
        if layer not in solved:
            geom = R.layer_geometry(stackup, set(planes), layer)
            w = None
            if geom and not (geom[0] is None and geom[2] is None):
                w = _bisect(lambda x: R.z0_on_layer(x, geom)[0], target_z, w_min, w_max)
            solved[layer] = round(w, 3) if w else None
        w = solved[layer]
        old = round(_mm(t.GetWidth()), 3)
        if w is None or abs(w - old) / old * 100 <= tol_pct:
            continue
        t.SetWidth(pcbnew.FromMM(w))
        out.append((n, layer, old, w, round(_mm(t.GetLength()), 2)))
    by = {}
    for n, layer, old, w, mm in out:
        k = (n, layer, old, w)
        by[k] = by.get(k, 0.0) + mm
    for (n, layer, old, w), mm in sorted(by.items()):
        log(f"    {n}: {mm:.1f} mm on {layer} {old} -> {w} mm")
    return out


# ---------------------------------------------------------------- remapping
def _zone_touch(board, netcode, pt, layer):
    """Is `pt` inside a filled zone of this net on this layer? Plane nets
    (GND, rails) connect copper through pours, not pads."""
    for z in board.Zones():
        if z.GetNetCode() != netcode or not z.IsOnLayer(layer):
            continue
        try:
            if z.HitTestFilledArea(layer, pt, 0):
                return True
        except Exception:
            if z.HitTest(pt):
                return True
    return False


def prune_dangling(board, netcode, protect=(), near=None, radius_mm=6.0):
    """Delete tracks/vias of a net that no longer reach anything: an
    endpoint touching no pad, no other track, no via and no filled zone.
    Iterates to a fixpoint. `near`/`radius_mm` confine the search to the
    stub's neighbourhood — pruning a whole plane net once deleted 225
    items of GND copper on utv-comms V1.5 (measured). Returns removed count."""
    import pcbnew
    removed = 0

    def close(pt):
        if near is None:
            return True
        return math.hypot(pt.x / 1e6 - near[0], pt.y / 1e6 - near[1]) <= radius_mm

    while True:
        tracks = [t for t in board.GetTracks() if t.GetNetCode() == netcode]
        pads = [p for fp in board.GetFootprints() for p in fp.Pads()
                if p.GetNetCode() == netcode]
        segs = [t for t in tracks if t.GetClass() == "PCB_TRACK"]
        vias = [t for t in tracks if t.GetClass() == "PCB_VIA"]

        def touched(pt, layer, me):
            for p in pads:
                if p.IsOnLayer(layer) and p.HitTest(pt):
                    return True
            for s in segs:
                if s is me or s.GetLayer() != layer:
                    continue
                if s.HitTest(pt, int(s.GetWidth() / 2)):
                    return True
            for v in vias:
                if v is me:
                    continue
                if v.IsOnLayer(layer) and v.HitTest(pt, int(v.GetWidth(layer) / 2)):
                    return True
            return _zone_touch(board, netcode, pt, layer)

        victims = []
        for s in segs:
            if s.m_Uuid.AsString() in protect:
                continue
            if not (close(s.GetStart()) or close(s.GetEnd())):
                continue
            la = s.GetLayer()
            if not touched(s.GetStart(), la, s) or not touched(s.GetEnd(), la, s):
                victims.append(s)
        for v in vias:
            if v.m_Uuid.AsString() in protect or not close(v.GetPosition()):
                continue
            p = v.GetPosition()
            hits = 0
            for s in segs:
                if v.IsOnLayer(s.GetLayer()) and \
                        (s.GetStart() == p or s.GetEnd() == p or
                         s.HitTest(p, int(s.GetWidth() / 2))):
                    hits += 1
            if hits <= 1 and not any(pd.HitTest(p) for pd in pads):
                victims.append(v)
        if not victims:
            return removed
        for t in victims:
            _remove(board, t)
            removed += 1


def _rip_pad_stubs(board, pad, netcode):
    """Remove tracks that land in `pad` on `netcode`, then prune what that
    leaves dangling. Returns removed count."""
    removed = 0
    for t in list(board.GetTracks()):
        if t.GetNetCode() != netcode or t.GetClass() != "PCB_TRACK":
            continue
        if not pad.IsOnLayer(t.GetLayer()):
            continue
        if pad.HitTest(t.GetStart()) or pad.HitTest(t.GetEnd()):
            _remove(board, t)
            removed += 1
    c = pad.GetPosition()
    return removed + prune_dangling(board, netcode, near=(c.x / 1e6, c.y / 1e6))


def _gnd_via(board, fp, pad, gnd, via_mm=0.45, drill_mm=0.25, gap_mm=0.75,
             track_mm=0.2):
    """Drop a via to ground beside `pad`, pointing AWAY from the footprint
    body, joined by a short track. Returns the via."""
    import pcbnew
    c, p = fp.GetPosition(), pad.GetPosition()
    dx, dy = p.x - c.x, p.y - c.y
    d = math.hypot(dx, dy) or 1.0
    ux, uy = dx / d, dy / d
    vp = pcbnew.VECTOR2I(int(p.x + ux * gap_mm * 1e6), int(p.y + uy * gap_mm * 1e6))
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(vp)
    via.SetViaType(pcbnew.VIATYPE_THROUGH)
    via.SetWidth(pcbnew.FromMM(via_mm))
    via.SetDrill(pcbnew.FromMM(drill_mm))
    via.SetNet(gnd)
    board.Add(via)
    tr = pcbnew.PCB_TRACK(board)
    tr.SetStart(p)
    tr.SetEnd(vp)
    tr.SetLayer(pad.GetLayer() if hasattr(pad, "GetLayer") else pcbnew.F_Cu)
    tr.SetWidth(pcbnew.FromMM(track_mm))
    tr.SetNet(gnd)
    board.Add(tr)
    return via


def remap_pins(board, mapping, rip=True, gnd_via=True, log=print):
    """mapping: {ref: {pad_number: new_net}}. Pads are re-netted; stubs on
    the old net are ripped; pads that become GND get a via to the plane.
    Returns {"changed": [(ref, pad, old, new)], "ripped": n, "vias": n,
    "unrouted": [(ref, pad, net)]} — unrouted is what the patcher must close."""
    import pcbnew
    changed, ripped, vias, unrouted = [], 0, 0, []
    gnd = board.FindNet("GND")
    for ref, pins in mapping.items():
        fp = board.FindFootprintByReference(ref)
        if not fp:
            log(f"    {ref}: not on the board")
            continue
        for pad in fp.Pads():
            num = pad.GetNumber()
            if num not in pins:
                continue
            new = pins[num]
            old = pad.GetNetname()
            if old == new:
                continue
            oldcode = pad.GetNetCode()
            net = board.FindNet(new)
            if net is None and new:
                net = pcbnew.NETINFO_ITEM(board, new)
                board.Add(net)
            if rip and oldcode > 0:
                ripped += _rip_pad_stubs(board, pad, oldcode)
            pad.SetNet(net)
            changed.append((ref, num, old, new))
            if new == "GND" and gnd_via and gnd:
                _gnd_via(board, fp, pad, gnd)
                vias += 1
            elif new:
                unrouted.append((ref, num, new))
            log(f"    {ref}.{num}: {old or '-'} -> {new or '-'}")
    return {"changed": changed, "ripped": ripped, "vias": vias, "unrouted": unrouted}


# ------------------------------------------------------------------ stitch
def stitch(board, ref, padnum, max_mm=3.0, width_mm=0.2, log=print, twin_only=False):
    """Join a pad to the nearest same-net copper on its own layer with one
    straight track (the last inch a grid router keeps failing on: a pad
    whose net already passes 1-2 mm away). Same-net pads count as targets
    too — a twin pad (two pads, one number) joins its sibling. With
    `twin_only`, an already-connected pad is skipped. Returns length or 0."""
    import pcbnew
    fp = board.FindFootprintByReference(ref)
    if not fp:
        return 0.0
    pads_here = [p for p in fp.Pads() if p.GetNumber() == padnum]
    pad = pads_here[0] if pads_here else None
    if pad is None or pad.GetNetCode() <= 0:
        return 0.0
    if len(pads_here) > 1:
        # twin pads: pick the one with no track landing on it
        def landed(p):
            return any(t.GetClass() == "PCB_TRACK" and t.GetNetCode() == p.GetNetCode()
                       and (p.HitTest(t.GetStart()) or p.HitTest(t.GetEnd()))
                       for t in board.GetTracks())
        bare = [p for p in pads_here if not landed(p)]
        if bare:
            pad = bare[0]
    code, pp = pad.GetNetCode(), pad.GetPosition()
    layers = [l for l in _cu_layers(board) if pad.IsOnLayer(l)]
    best = None
    for ofp in board.GetFootprints():
        for op in ofp.Pads():
            if op is pad or op.GetNetCode() != code:
                continue
            if not any(op.IsOnLayer(l) for l in layers):
                continue
            d = math.hypot(op.GetPosition().x - pp.x, op.GetPosition().y - pp.y) / 1e6
            if d <= max_mm and (best is None or d < best[0]):
                best = (d, op.GetPosition(), next(l for l in layers if op.IsOnLayer(l)))
    for t in board.GetTracks():
        if t.GetNetCode() != code:
            continue
        if t.GetClass() == "PCB_VIA":
            cands = [(t.GetPosition(), layers[0])] if layers else []
        elif t.GetLayer() in layers:
            cands = [(t.GetStart(), t.GetLayer()), (t.GetEnd(), t.GetLayer())]
        else:
            continue
        for q, layer in cands:
            if pad.HitTest(q):
                return 0.0          # already joined
            d = math.hypot(q.x - pp.x, q.y - pp.y) / 1e6
            if d <= max_mm and (best is None or d < best[0]):
                best = (d, q, layer)
    if best is None:
        log(f"    {ref}.{padnum}: no {pad.GetNetname()} copper within {max_mm} mm")
        return 0.0
    d, q, layer = best
    tr = pcbnew.PCB_TRACK(board)
    tr.SetStart(pp)
    tr.SetEnd(q)
    tr.SetLayer(layer)
    tr.SetWidth(pcbnew.FromMM(width_mm))
    tr.SetNetCode(code)
    board.Add(tr)
    log(f"    {ref}.{padnum}: stitched {d:.2f} mm to {pad.GetNetname()} on "
        f"{board.GetLayerName(layer)}")
    return d


# ------------------------------------------------------------ clear under
def clear_under(board, ref, clearance_mm=0.13, log=print):
    """Rip every track/via of ANOTHER net that collides with a pad of `ref`
    (pad shape grown by the clearance), then prune what that leaves
    dangling near the part. For a footprint swap onto a bigger land
    pattern: the copper that used to run beside the old pads now runs
    through the new ones. Returns (ripped, nets)."""
    import pcbnew
    fp = board.FindFootprintByReference(ref)
    if not fp:
        return 0, set()
    pads = [(p, p.GetNetCode(), p.GetPosition()) for p in fp.Pads() if p.GetNumber()]
    grow = int(clearance_mm * 1e6)
    victims, nets = [], set()
    for t in board.GetTracks():
        for p, code, pos in pads:
            if t.GetNetCode() == code:
                continue
            if t.GetClass() == "PCB_VIA":
                hit = p.HitTest(t.GetPosition(), int(t.GetWidth(pcbnew.F_Cu) / 2) + grow)
            else:
                if not p.IsOnLayer(t.GetLayer()):
                    continue
                try:
                    shp = p.GetEffectiveShape(t.GetLayer())
                    hit = t.GetEffectiveShape(t.GetLayer()).Collide(shp, grow)
                except Exception:
                    hit = p.HitTest(t.GetStart(), grow) or p.HitTest(t.GetEnd(), grow)
            if hit:
                victims.append((t, pos))
                nets.add(t.GetNetCode())
                break
    for t, pos in victims:
        log(f"    {ref}: rip {t.GetClass()} [{t.GetNetname()}] under pad area")
        _remove(board, t)
    c = fp.GetPosition()
    for code in nets:
        prune_dangling(board, code, near=(c.x / 1e6, c.y / 1e6), radius_mm=12.0)
    return len(victims), {board.FindNet(n).GetNetname() if False else n for n in nets}


# ---------------------------------------------------------------- islands
def _clear_for_via(board, pt, r_mm, netcode):
    """No copper of another net within r_mm of `pt` on ANY layer, and no
    hole of any net within r_mm: the only way a through via is safe."""
    import pcbnew
    R = int(r_mm * 1e6)
    for t in board.GetTracks():
        if t.GetNetCode() == netcode and t.GetClass() != "PCB_VIA":
            continue
        if t.GetClass() == "PCB_VIA":
            if t.HitTest(pt, R):
                return False
        elif t.HitTest(pt, R):
            return False
    for fp in board.GetFootprints():
        for p in fp.Pads():
            if p.GetNetCode() == netcode and p.GetDrillSize().x == 0:
                continue
            if p.HitTest(pt, R):
                return False
    for z in board.Zones():
        if z.GetNetCode() != netcode and z.HitTestFilledArea(pcbnew.F_Cu, pt, R):
            return False
    return True


def stitch_islands(board, net="GND", via_mm=0.45, drill_mm=0.25, clearance_mm=0.15,
                   rings=(0.8, 1.1, 1.4, 1.8, 2.2, 2.6, 3.0), log=print):
    """A via beside every SMD pad of `net` whose outer-layer pour island
    holds no via and no through-hole pad — that island is the pad's only
    connection. Candidate spots ring the pad at 0.8..1.4 mm and must be
    clear of every other net's copper and every hole on all layers (a via
    dropped by island centroid alone landed on inner-layer copper: 8 vias,
    34 hole-clearance violations, measured). Returns vias added."""
    import pcbnew
    ni = None
    for fp in board.GetFootprints():
        for p in fp.Pads():
            if p.GetNetname() == net:
                ni = p.GetNet()
                break
        if ni:
            break
    if ni is None:
        return 0
    code = ni.GetNetCode()
    vias = [t.GetPosition() for t in board.GetTracks()
            if t.GetClass() == "PCB_VIA" and t.GetNetCode() == code]
    tht = [p.GetPosition() for fp in board.GetFootprints() for p in fp.Pads()
           if p.GetNetCode() == code and p.GetDrillSize().x > 0]
    zones = [z for z in board.Zones() if z.GetNetCode() == code]
    added = 0
    r_need = via_mm / 2 + clearance_mm + 0.05
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() != code or pad.GetDrillSize().x > 0:
                continue
            layer = pcbnew.F_Cu if pad.IsOnLayer(pcbnew.F_Cu) else pcbnew.B_Cu
            pp = pad.GetPosition()
            island = None
            for z in zones:
                if not z.IsOnLayer(layer):
                    continue
                polys = z.GetFilledPolysList(layer)
                for i in range(polys.OutlineCount()):
                    if polys.Contains(pp, i):
                        island = (polys, i)
                        break
                if island:
                    break
            if island is None:
                continue
            polys, i = island
            if any(polys.Contains(v, i) for v in vias) or any(polys.Contains(t, i) for t in tht):
                continue
            placed = False
            for ring in rings:
                for k in range(12):
                    ang = k * math.pi / 6
                    pt = pcbnew.VECTOR2I(int(pp.x + ring * 1e6 * math.cos(ang)),
                                         int(pp.y + ring * 1e6 * math.sin(ang)))
                    if not polys.Contains(pt, i) or not _clear_for_via(board, pt, r_need, code):
                        continue
                    via = pcbnew.PCB_VIA(board)
                    via.SetPosition(pt)
                    via.SetViaType(pcbnew.VIATYPE_THROUGH)
                    via.SetWidth(pcbnew.FromMM(via_mm))
                    via.SetDrill(pcbnew.FromMM(drill_mm))
                    via.SetNet(ni)
                    board.Add(via)
                    vias.append(pt)
                    added += 1
                    placed = True
                    log(f"    {fp.GetReference()}.{pad.GetNumber()} ({net}) island on "
                        f"{board.GetLayerName(layer)}: via at ({pt.x / 1e6:.1f},{pt.y / 1e6:.1f})")
                    break
                if placed:
                    break
            if not placed:
                log(f"    {fp.GetReference()}.{pad.GetNumber()} ({net}): island has no "
                    f"clear spot for a via")
    return added


# ------------------------------------------------------------- duplicate pads
def fix_dup_pads(board, log=print):
    """Copy a net to unnetted pads that share a number with a netted pad."""
    n = 0
    for fp in board.GetFootprints():
        by = {}
        for p in fp.Pads():
            by.setdefault(p.GetNumber(), []).append(p)
        for num, pads in by.items():
            netted = [p for p in pads if p.GetNetCode() > 0]
            bare = [p for p in pads if p.GetNetCode() <= 0]
            if netted and bare:
                for p in bare:
                    p.SetNet(netted[0].GetNet())
                    n += 1
                log(f"    {fp.GetReference()}.{num}: {len(bare)} twin pad(s) "
                    f"netted to {netted[0].GetNetname()}")
    return n


# ------------------------------------------------------------------- text
def _free_spot(board, w_mm, h_mm, layer, margin=1.5, step=1.0, prefer="bottom-right"):
    """Search the board for a w x h rectangle clear of footprints (either
    side's courtyard/body), silkscreen drawings and the edge."""
    import pcbnew
    bb = board.GetBoardEdgesBoundingBox()
    x0, y0 = _mm(bb.GetLeft()) + margin, _mm(bb.GetTop()) + margin
    x1, y1 = _mm(bb.GetRight()) - margin, _mm(bb.GetBottom()) - margin
    blocks = []
    for fp in board.GetFootprints():
        b = fp.GetBoundingBox(True, False)
        blocks.append((_mm(b.GetLeft()), _mm(b.GetTop()), _mm(b.GetRight()), _mm(b.GetBottom())))
    for d in board.GetDrawings():
        if d.GetLayer() in (pcbnew.F_SilkS, pcbnew.B_SilkS, pcbnew.Edge_Cuts):
            b = d.GetBoundingBox()
            blocks.append((_mm(b.GetLeft()), _mm(b.GetTop()), _mm(b.GetRight()), _mm(b.GetBottom())))
    for t in board.GetTracks():
        pass
    pad = 0.4

    def clear(x, y):
        ax0, ay0, ax1, ay1 = x - w_mm / 2 - pad, y - h_mm / 2 - pad, x + w_mm / 2 + pad, y + h_mm / 2 + pad
        if ax0 < x0 or ay0 < y0 or ax1 > x1 or ay1 > y1:
            return False
        for bx0, by0, bx1, by1 in blocks:
            if not (ax1 < bx0 or ax0 > bx1 or ay1 < by0 or ay0 > by1):
                return False
        return True

    cands = []
    y = y0 + h_mm / 2
    while y <= y1 - h_mm / 2:
        x = x0 + w_mm / 2
        while x <= x1 - w_mm / 2:
            cands.append((x, y))
            x += step
        y += step
    cx, cy = {"bottom-right": (x1, y1), "bottom-left": (x0, y1),
              "top-left": (x0, y0), "top-right": (x1, y0)}.get(prefer, (x1, y1))
    cands.sort(key=lambda c: math.hypot(c[0] - cx, c[1] - cy))
    for x, y in cands:
        if clear(x, y):
            return x, y
    return None


def add_text(board, text, layer="F.SilkS", at=None, size_mm=1.0,
             thickness_mm=0.15, prefer="bottom-right", log=print):
    """Add a silkscreen text item; picks a free spot when `at` is None."""
    import pcbnew
    lid = board.GetLayerID(layer)
    w = len(text) * size_mm * 0.85
    h = size_mm * 1.3
    if at is None:
        at = _free_spot(board, w, h, lid, prefer=prefer)
        if at is None:
            log(f"    no free {w:.0f}x{h:.0f} mm spot for '{text}' on {layer}")
            return None
    item = pcbnew.PCB_TEXT(board)
    item.SetText(text)
    item.SetLayer(lid)
    item.SetPosition(pcbnew.VECTOR2I(int(at[0] * 1e6), int(at[1] * 1e6)))
    item.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(size_mm), pcbnew.FromMM(size_mm)))
    item.SetTextThickness(pcbnew.FromMM(thickness_mm))
    if layer.startswith("B."):
        item.SetMirrored(True)
    board.Add(item)
    log(f"    text '{text}' on {layer} at ({at[0]:.1f}, {at[1]:.1f})")
    return at


def bridge(board, ref, padnum, layers=None, cell=0.2, width_mm=0.15, clearance_mm=0.13,
           via_mm=0.45, drill_mm=0.25, margin_mm=4.0, via_cost_mm=3.0, log=print):
    """Maze-route ONE unconnected pad to the nearest copper of its own net.

    The last-mile patcher and freerouting both gave up on U13.14 (I2C_SDA,
    utv-comms V1.5): the only way out of the IMU corner crosses a USB pair,
    VBUS and I2S on In2, and no single-layer dogleg exists. This is a
    multi-layer Dijkstra on a `cell` mm grid: every foreign track, via and
    pad on each layer is rasterised with the track clearance; a layer change
    is allowed only where a through via clears EVERY copper layer (the
    island-via lesson: check all layers by shape, not bbox). Own-net copper
    is the target set, so the route ends on whichever track, via or pad of
    the net is cheapest to reach. Straight runs are simplified against the
    same grid. Returns the list of items added ([] if no path)."""
    import numpy as np
    import pcbnew
    fp = board.FindFootprintByReference(ref)
    pad = next(p for p in fp.Pads() if p.GetNumber() == str(padnum))
    net = pad.GetNetCode()
    ni = pad.GetNet()
    cu = _cu_layers(board)
    lids = [l for l in cu if not layers or board.GetLayerName(l) in layers]
    r_t = width_mm / 2 + clearance_mm
    r_v = via_mm / 2 + clearance_mm
    own_tracks = [t for t in board.GetTracks() if t.GetNetCode() == net]
    def is_src(f, p):   # SWIG proxies never compare `is`; key on ref + number
        return f.GetReference() == ref and p.GetNumber() == str(padnum)
    own_pads = [p for f in board.GetFootprints() for p in f.Pads()
                if p.GetNetCode() == net and not is_src(f, p)]
    if not own_tracks and not own_pads:
        log(f"    bridge {ref}.{padnum}: net has no other copper")
        return []
    # region
    P = pad.GetPosition()
    xs = [P.x] + [t.GetStart().x for t in own_tracks] + [t.GetEnd().x for t in own_tracks] + [p.GetPosition().x for p in own_pads]
    ys = [P.y] + [t.GetStart().y for t in own_tracks] + [t.GetEnd().y for t in own_tracks] + [p.GetPosition().y for p in own_pads]
    eb = board.GetBoardEdgesBoundingBox()
    x0 = max(min(xs) / 1e6 - margin_mm, eb.GetLeft() / 1e6 + 0.5)
    x1 = min(max(xs) / 1e6 + margin_mm, eb.GetRight() / 1e6 - 0.5)
    y0 = max(min(ys) / 1e6 - margin_mm, eb.GetTop() / 1e6 + 0.5)
    y1 = min(max(ys) / 1e6 + margin_mm, eb.GetBottom() / 1e6 - 0.5)
    nx = int((x1 - x0) / cell) + 1
    ny = int((y1 - y0) / cell) + 1
    gx = x0 + np.arange(nx) * cell
    gy = y0 + np.arange(ny) * cell
    GX, GY = np.meshgrid(gx, gy)

    def mark(grid, ax, ay, bx, by, r):
        lo_i = max(0, int((min(ax, bx) - r - x0) / cell) - 1)
        hi_i = min(nx, int((max(ax, bx) + r - x0) / cell) + 2)
        lo_j = max(0, int((min(ay, by) - r - y0) / cell) - 1)
        hi_j = min(ny, int((max(ay, by) + r - y0) / cell) + 2)
        if lo_i >= hi_i or lo_j >= hi_j:
            return
        X = GX[lo_j:hi_j, lo_i:hi_i]
        Y = GY[lo_j:hi_j, lo_i:hi_i]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        if L2 < 1e-12:
            d = np.hypot(X - ax, Y - ay)
        else:
            u = np.clip(((X - ax) * dx + (Y - ay) * dy) / L2, 0, 1)
            d = np.hypot(X - (ax + u * dx), Y - (ay + u * dy))
        grid[lo_j:hi_j, lo_i:hi_i] |= d <= r

    def mark_rect(grid, bb, r):
        lo_i = max(0, int((bb.GetLeft() / 1e6 - r - x0) / cell) - 1)
        hi_i = min(nx, int((bb.GetRight() / 1e6 + r - x0) / cell) + 2)
        lo_j = max(0, int((bb.GetTop() / 1e6 - r - y0) / cell) - 1)
        hi_j = min(ny, int((bb.GetBottom() / 1e6 + r - y0) / cell) + 2)
        if lo_i >= hi_i or lo_j >= hi_j:
            return
        X = GX[lo_j:hi_j, lo_i:hi_i]
        Y = GY[lo_j:hi_j, lo_i:hi_i]
        ddx = np.maximum(np.maximum(bb.GetLeft() / 1e6 - X, X - bb.GetRight() / 1e6), 0)
        ddy = np.maximum(np.maximum(bb.GetTop() / 1e6 - Y, Y - bb.GetBottom() / 1e6), 0)
        grid[lo_j:hi_j, lo_i:hi_i] |= np.hypot(ddx, ddy) <= r

    obs = {}
    via_obs = np.zeros((ny, nx), bool)
    tgt = {}
    for lid in cu:
        o_t = np.zeros((ny, nx), bool)
        o_v = np.zeros((ny, nx), bool)
        tg = np.zeros((ny, nx), bool)
        for t in board.GetTracks():
            isvia = t.GetClass() == "PCB_VIA"
            if isvia and not t.IsOnLayer(lid):
                continue
            if not isvia and t.GetLayer() != lid:
                continue
            if t.GetNetCode() == net:
                if isvia:
                    mark(tg, t.GetPosition().x / 1e6, t.GetPosition().y / 1e6,
                         t.GetPosition().x / 1e6, t.GetPosition().y / 1e6, t.GetWidth() / 2e6)
                else:
                    mark(tg, t.GetStart().x / 1e6, t.GetStart().y / 1e6,
                         t.GetEnd().x / 1e6, t.GetEnd().y / 1e6, t.GetWidth() / 2e6)
                continue
            hw = t.GetWidth() / 2e6
            if isvia:
                px, py = t.GetPosition().x / 1e6, t.GetPosition().y / 1e6
                mark(o_t, px, py, px, py, hw + r_t)
                mark(o_v, px, py, px, py, hw + r_v)
            else:
                a, b = t.GetStart(), t.GetEnd()
                mark(o_t, a.x / 1e6, a.y / 1e6, b.x / 1e6, b.y / 1e6, hw + r_t)
                mark(o_v, a.x / 1e6, a.y / 1e6, b.x / 1e6, b.y / 1e6, hw + r_v)
        for f in board.GetFootprints():
            for p in f.Pads():
                if not p.IsOnLayer(lid) and p.GetAttribute() not in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH):
                    continue
                if p.GetNetCode() == net:
                    if p.IsOnLayer(lid) and not is_src(f, p):
                        mark_rect(tg, p.GetBoundingBox(), 0.0)
                    continue
                mark_rect(o_t, p.GetBoundingBox(), r_t)
                mark_rect(o_v, p.GetBoundingBox(), r_v)
        obs[lid] = o_t
        via_obs |= o_v
        tgt[lid] = tg
    # source: the pad's own cell(s)
    si = int(round((P.x / 1e6 - x0) / cell))
    sj = int(round((P.y / 1e6 - y0) / cell))
    src = [(l, sj, si) for l in lids if pad.IsOnLayer(l)]
    src = [s for s in src if 0 <= s[1] < ny and 0 <= s[2] < nx and not obs[s[0]][s[1], s[2]]]
    if not src:
        log(f"    bridge {ref}.{padnum}: source cell blocked")
        return []
    # Dijkstra over (layer, j, i)
    dist = {}
    prev = {}
    pq = []
    for s in src:
        dist[s] = 0.0
        heapq.heappush(pq, (0.0, s))
    steps = [(-1, 0, cell), (1, 0, cell), (0, -1, cell), (0, 1, cell),
             (-1, -1, cell * 1.4142), (-1, 1, cell * 1.4142), (1, -1, cell * 1.4142), (1, 1, cell * 1.4142)]
    goal = None
    while pq:
        d, s = heapq.heappop(pq)
        if d > dist.get(s, 1e18):
            continue
        l, j, i = s
        if tgt[l][j, i] and (j, i) != (sj, si):
            goal = s
            break
        for dj, di, c in steps:
            nj, ni_ = j + dj, i + di
            if 0 <= nj < ny and 0 <= ni_ < nx and not obs[l][nj, ni_]:
                if dj and di and (obs[l][j, ni_] or obs[l][nj, i]):
                    continue  # no corner cutting
                t = (l, nj, ni_)
                nd = d + c
                if nd < dist.get(t, 1e18):
                    dist[t] = nd
                    prev[t] = s
                    heapq.heappush(pq, (nd, t))
        if not via_obs[j, i] and (j, i) != (sj, si):
            for l2 in lids:
                if l2 == l or obs[l2][j, i]:
                    continue
                t = (l2, j, i)
                nd = d + via_cost_mm
                if nd < dist.get(t, 1e18):
                    dist[t] = nd
                    prev[t] = s
                    heapq.heappush(pq, (nd, t))
    if goal is None:
        log(f"    bridge {ref}.{padnum}: no path on {[board.GetLayerName(l) for l in lids]} "
            f"(grid {nx}x{ny} @ {cell} mm)")
        return []
    path = [goal]
    while path[-1] in prev:
        path.append(prev[path[-1]])
    path.reverse()
    # split into per-layer runs, simplify each by line of sight on the grid
    runs = []
    for l, j, i in path:
        if runs and runs[-1][0] == l:
            runs[-1][1].append((i, j))
        else:
            runs.append((l, [(i, j)]))

    def clear_line(l, a, b):
        n = max(2, int(math.hypot(b[0] - a[0], b[1] - a[1]) * 2) + 1)
        for k in range(n + 1):
            f = k / n
            i = int(round(a[0] + (b[0] - a[0]) * f))
            j = int(round(a[1] + (b[1] - a[1]) * f))
            if obs[l][j, i]:
                return False
        return True

    def simplify(l, pts):
        out = [pts[0]]
        k = 0
        while k < len(pts) - 1:
            m = len(pts) - 1
            while m > k + 1 and not clear_line(l, pts[k], pts[m]):
                m -= 1
            out.append(pts[m])
            k = m
        return out

    def V(i, j):
        return pcbnew.VECTOR2I(int(round((x0 + i * cell) * 1e6)), int(round((y0 + j * cell) * 1e6)))

    added = []
    first = True
    for idx, (l, pts) in enumerate(runs):
        pts = simplify(l, pts)
        if idx == len(runs) - 1 and len(pts) == 1:
            # the via landed inside the target copper: still run a track
            # from it to the pad centre / track centreline so the join is real
            a = V(*pts[0])
            b = _snap_to_net(board, a, l, net, own_pads, skip=_uuids(added))
            if a != b:
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(a)
                t.SetEnd(b)
                t.SetLayer(l)
                t.SetWidth(pcbnew.FromMM(width_mm))
                t.SetNet(ni)
                board.Add(t)
                added.append(t)
        for k in range(len(pts) - 1):
            a = P if (first and k == 0) else V(*pts[k])
            b = V(*pts[k + 1])
            if idx == len(runs) - 1 and k == len(pts) - 2:
                b = _snap_to_net(board, b, l, net, own_pads, skip=_uuids(added))
            if a == b:
                continue
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(a)
            t.SetEnd(b)
            t.SetLayer(l)
            t.SetWidth(pcbnew.FromMM(width_mm))
            t.SetNet(ni)
            board.Add(t)
            added.append(t)
        first = False
        if idx < len(runs) - 1:
            v = pcbnew.PCB_VIA(board)
            v.SetPosition(V(*pts[-1]))
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetWidth(pcbnew.FromMM(via_mm))
            v.SetDrill(pcbnew.FromMM(drill_mm))
            v.SetNet(ni)
            board.Add(v)
            added.append(v)
    log(f"    bridge {ref}.{padnum}: {len(added)} item(s), {len(runs)} layer run(s) "
        f"{[board.GetLayerName(l) for l, _ in runs]}, {dist[goal]:.1f} mm cost")
    return added


def _uuids(items):
    return {i.m_Uuid.AsString() for i in items}


def _snap_to_net(board, pt, layer, net, own_pads, skip=()):
    """Move the final point onto the centreline of the nearest own-net
    item on `layer` (pad centre, via centre, or the foot of the
    perpendicular on a track) so the join is exact, not a grid cell.
    `skip` holds the uuids of copper this bridge itself just added."""
    import pcbnew
    best = None
    for p in own_pads:
        if p.IsOnLayer(layer):
            d = math.hypot(p.GetPosition().x - pt.x, p.GetPosition().y - pt.y)
            if best is None or d < best[0]:
                best = (d, p.GetPosition())
    for t in board.GetTracks():
        if t.GetNetCode() != net or t.m_Uuid.AsString() in skip:
            continue
        if t.GetClass() == "PCB_VIA":
            if t.IsOnLayer(layer):
                d = math.hypot(t.GetPosition().x - pt.x, t.GetPosition().y - pt.y)
                if best is None or d < best[0]:
                    best = (d, t.GetPosition())
            continue
        if t.GetLayer() != layer:
            continue
        a, b = t.GetStart(), t.GetEnd()
        dx, dy = b.x - a.x, b.y - a.y
        L2 = dx * dx + dy * dy
        u = 0.0 if L2 == 0 else max(0.0, min(1.0, ((pt.x - a.x) * dx + (pt.y - a.y) * dy) / L2))
        q = pcbnew.VECTOR2I(int(a.x + u * dx), int(a.y + u * dy))
        d = math.hypot(q.x - pt.x, q.y - pt.y)
        if best is None or d < best[0]:
            best = (d, q)
    return best[1] if best and best[0] < 1.0e6 else pt
