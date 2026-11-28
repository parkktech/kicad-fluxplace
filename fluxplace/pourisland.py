"""Guarded same-net pour-island repair: one stitching via at a legal site, never an unverified island deletion."""
import math
import os
import re

from . import handroute as H, planes as PL, pourtie as PTF, routerules as RR


def _start(records, net, at):
    if at is not None:
        return tuple(at)
    zones = [side for r in records if r["net"] == net for side in (r["a"], r["b"]) if side["kind"] == "zone"]
    if zones:
        return zones[0]["x"], zones[0]["y"]
    sides = [side for r in records if r["net"] == net for side in (r["a"], r["b"])]
    if not sides:
        raise ValueError(f"net {net!r} has no open pour-island connection")
    return sides[0]["x"], sides[0]["y"]


def outline_index(b, net):
    """-> {layer id: [(area, filled poly set, outline index)]}: the filled outlines of `net`'s copper pours, per layer."""
    rows = {}
    for z in b.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != net:
            continue
        for lid in z.GetLayerSet().CuStack():
            ps = z.GetFilledPolysList(lid)
            for i in range(ps.OutlineCount()):
                rows.setdefault(lid, []).append((abs(ps.Outline(i).Area()), ps, i))
    return rows


def island_sites(b, net, step=0.4, per_island=12):
    """Via sites inside the untied pour islands of `net` -> [(x, y)] in mm (island_groups, flat)."""
    return [s for g in island_groups(b, net, step, per_island) for s in g]


def island_groups(b, net, step=0.4, per_island=12):
    """Via sites inside the untied pour islands of `net`: a fill outline that holds a pad of the net but no via and no
    through-hole pad of it has no path to the other layers. KiCad reports such an open at the zone's anchor corner, not
    at the island, so the sites are taken from the fill itself: grid points inside the outline, nearest its pads first.
    -> [[(x, y)] per island] in mm."""
    import pcbnew
    vias = [t.GetPosition() for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and t.GetNetname() == net]
    pads = [p for f in b.GetFootprints() for p in f.Pads() if p.GetNetname() == net]
    out = []
    for z in b.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != net:
            continue
        for lid in z.GetLayerSet().CuStack():
            ps = z.GetFilledPolysList(lid)
            for i in range(ps.OutlineCount()):
                if any(ps.Contains(v, i) for v in vias) or any(p.HasHole() and ps.Contains(p.GetPosition(), i) for p in pads):
                    continue
                own = [p.GetPosition() for p in pads if p.IsOnLayer(lid) and ps.Contains(p.GetPosition(), i)]
                if not own:
                    continue
                bb = ps.Outline(i).BBox()
                st = pcbnew.FromMM(step)
                pts = []
                x = bb.GetLeft() + st // 2
                while x < bb.GetRight():
                    y = bb.GetTop() + st // 2
                    while y < bb.GetBottom():
                        if ps.Contains(pcbnew.VECTOR2I(x, y), i):
                            pts.append((min((x - o.x) ** 2 + (y - o.y) ** 2 for o in own), x, y))
                        y += st
                    x += st
                pts.sort()
                rest = pts[per_island:]                     # k15: a large island also gets sites spread over the rest of it
                pick = pts[:per_island] + rest[::max(1, len(rest) // per_island)][:per_island]
                out.append([(pcbnew.ToMM(x), pcbnew.ToMM(y)) for _d, x, y in pick])
    return out


STUB_R = (0.6, 0.9, 1.3)            # k11: via distance (mm) from an island pad for a pad stub + via
STUB_PER_ISLAND = 16


def island_stubs(b, net):
    """k11: an island too small (or too crowded) for a via inside it is tied by a short track from one of its pads to a via
    outside it that lands on the net's pour on another layer. -> [path string] (handroute path syntax), nearest first."""
    import pcbnew
    vias = [t.GetPosition() for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T and t.GetNetname() == net]
    pads = [p for f in b.GetFootprints() for p in f.Pads() if p.GetNetname() == net]
    lids = {lid for z in b.Zones() if not z.GetIsRuleArea() and z.GetNetname() == net for lid in z.GetLayerSet().CuStack()}
    out = []
    for z in b.Zones():
        if z.GetIsRuleArea() or z.GetNetname() != net:
            continue
        for lid in z.GetLayerSet().CuStack():
            ps = z.GetFilledPolysList(lid)
            for i in range(ps.OutlineCount()):
                if any(ps.Contains(v, i) for v in vias) or any(p.HasHole() and ps.Contains(p.GetPosition(), i) for p in pads):
                    continue
                own = [p for p in pads if p.IsOnLayer(lid) and ps.Contains(p.GetPosition(), i)]
                rows = []
                for p in own:
                    c = p.GetPosition()
                    for r in STUB_R:
                        for k in range(8):
                            vx, vy = c.x + pcbnew.FromMM(r) * math.cos(k * math.pi / 4), c.y + pcbnew.FromMM(r) * math.sin(k * math.pi / 4)
                            if any(PTF.fill_hit(b, net, o, vx, vy) for o in lids - {lid}):
                                px, py, qx, qy = (pcbnew.ToMM(v) for v in (c.x, c.y, vx, vy))
                                rows.append((r, f"{b.GetLayerName(lid)}: {px:.4f},{py:.4f} {qx:.4f},{qy:.4f}; via {qx:.4f},{qy:.4f}"))
                out += [path for _r, path in sorted(rows, key=lambda t: t[0])[:STUB_PER_ISLAND]]
    return out


def repair(board, out, net, constraints=None, at=None, max_sites=24):
    """Join an open same-net pour island with one guarded via. Writes `out` only when its open count falls."""
    import pcbnew
    R = RR.rules(constraints and __import__("fluxplace.constraints", fromlist=["load"]).load(constraints), board)
    via = (R["via"], R["drill"])
    b = pcbnew.LoadBoard(board)
    PL.refill(b)
    pours = [z for z in b.Zones() if not z.GetIsRuleArea() and z.GetNetname() == net]
    if not pours:
        return {"accepted": False, "net": net, "reason": f"net {net!r} has no copper pour"}
    records = H.open_nets(board, constraints)["connections"]
    x, y = _start(records, net, at)
    sites = [(0.0, x, y)]
    for radius in (0.3, 0.6, 1.0, 1.5):
        sites += [(radius, x + radius * math.cos(k * math.pi / 4), y + radius * math.sin(k * math.pi / 4)) for k in range(8)]
    if at is None:
        sites = [(0.0, ix, iy) for ix, iy in island_sites(b, net)] + sites
        max_sites += len(sites) - 33
    reasons = []
    M = H._model(board, constraints)                 # k11: one copper model for every probe of this call (was one per site)
    for _radius, cx, cy in sites[:max_sites]:
        pos = pcbnew.VECTOR2I(pcbnew.FromMM(cx), pcbnew.FromMM(cy))
        hit_layers = {lid for lid in {lid for z in pours for lid in z.GetLayerSet().Seq()}
                      if PTF.fill_hit(b, net, lid, pcbnew.FromMM(cx), pcbnew.FromMM(cy))}
        pads = [p for f in b.GetFootprints() for p in f.Pads() if p.GetNetname() == net]
        hit_layers.update(lid for p in pads for lid in p.GetLayerSet().Seq() if p.HitTest(pos, int(via[0] * 1e6 / 2)))
        if len(hit_layers) < 2:
            continue
        path = f"via {cx:.4f},{cy:.4f}"
        probe = H.probe(board, net, path, constraints, via=via, M=M)
        if not probe["ok"]:
            reasons.append({"at": [round(cx, 4), round(cy, 4)], "reason": "probe: " + str(probe["violations"][:1])})
            continue
        result = H.lay(board, net, path, out=out, constraints=constraints, via=via)
        if result["accepted"]:
            return {"accepted": True, "action": "via", "net": net, "at": [round(cx, 4), round(cy, 4)],
                    "via": list(via), "out": out, "measure": result.get("measure", {})}
        reasons.append({"at": [round(cx, 4), round(cy, 4)], "reason": result.get("reason")})
        session = out + ".session"
        if os.path.isdir(session):
            import shutil
            shutil.rmtree(session)
    for path in (island_stubs(b, net) if at is None else []):     # k11: no via site inside: a pad stub to a via outside the island
        probe = H.probe(board, net, path, constraints, via=via, M=M)
        if not probe["ok"]:
            reasons.append({"path": path, "reason": "probe: " + str(probe["violations"][:1])})
            continue
        result = H.lay(board, net, path, out=out, constraints=constraints, via=via)
        if result["accepted"]:
            return {"accepted": True, "action": "stub+via", "net": net, "path": path, "via": list(via), "out": out,
                    "measure": result.get("measure", {})}
        reasons.append({"path": path, "reason": result.get("reason")})
        session = out + ".session"
        if os.path.isdir(session):
            import shutil
            shutil.rmtree(session)
    if at is None:                                    # k15: every free site is blocked: rip the blocking unlocked track, then lay
        hit = _rip_lay(board, out, net, constraints, via, M, reasons, sum(1 for r in records if r["net"] == net))
        if hit:
            return hit
    return {"accepted": False, "net": net, "reason": "no legal guarded stitching-via site joins the island", "attempts": reasons}


RIP_IDS = 16                        # k15: most blocking track pieces one rip-and-lay may remove (routed nets come in short pieces)
RIP_NETS = 2                        # k15: most nets one rip-and-lay may open (each is re-laid by the caller)
RIP_TRIES = 8                       # k15: rip-and-lay candidates tried per call (each is one guarded edit)
RIP_KINDS = ("clearance", "crossing", "via_clearance")
_STILL = re.compile(r"target not closed: open connections of \S+ (\d+) -> (\d+)")


def _rippable(M, net, viols, names, cons, never=()):
    """Full ids of the tracks behind `viols` when every violation is one an unlocked, non-hard track of another net (not in `never`:
    the [power] rails) causes; else None."""
    from . import lockpolicy as LP
    ids, named = set(), {str(v.get("item")) for v in viols if v.get("id")}
    for v in viols:
        if not v.get("id") and v.get("kind") == "hole_clearance" and v.get("with") in named:
            continue                                  # the hole check of a via against a track that is ripped anyway
        if v.get("kind") not in RIP_KINDS or not str(v.get("item", "")).startswith("track ") or v.get("locked") or v.get("net") in (None, "", net):
            return None
        if v["net"] in never or LP.level(v["net"], cons, names) == LP.HARD:
            return None
        hit = [o for o in H._resolve(M, v["id"]) if o.kind == "track" and not o.locked]
        if not hit:
            return None
        ids.update(o.id for o in hit)
    nets = {o.net for o in M.obs if o.id in ids}
    return ids if 0 < len(ids) <= RIP_IDS and len(nets) <= RIP_NETS else None


def _strip_ids(board, dst, ids):
    """Copy `board` to `dst` without the tracks whose uuid is in `ids` -> {net: count}."""
    import pcbnew
    b = pcbnew.LoadBoard(board)
    cut = {}
    for t in list(b.GetTracks()):
        if t.m_Uuid.AsString() in ids:
            cut[t.GetNetname()] = cut.get(t.GetNetname(), 0) + 1
            b.Delete(t)
    b.Save(dst)
    pro = os.path.splitext(board)[0] + ".kicad_pro"
    if os.path.exists(pro):
        import shutil
        shutil.copyfile(pro, os.path.splitext(dst)[0] + ".kicad_pro")
    return cut


def _rip_lay_closed(board, out, net, path, ids, constraints, via):
    """Rip `ids` and lay `path` in ONE guarded edit of `board`: refused when any net opens (handroute._edit), so a rail keeps its copper
    whole. -> ({net: ripped count}, lay result)."""
    cut = {}

    def mutate(b, M):
        for t in list(b.GetTracks()):
            if t.m_Uuid.AsString() in ids:
                cut[t.GetNetname()] = cut.get(t.GetNetname(), 0) + 1
                b.Delete(t)
        prims = H.G.parse_path(path, M.cu)
        pr = H.probe_prims(M, net, prims, None, via, skip_ids=ids)
        if not pr["ok"]:
            raise ValueError("probe failed: " + str(pr["violations"][:2]))
        H._apply_prims(b, M, net, prims, M.net_rule(net)["width"], via)
        return {"ripped": dict(cut)}
    return cut, H._edit(board, out, mutate, "lay", net, path, constraints)


def _rip_lay(board, out, net, constraints, via, M, reasons, opens0=None):
    """k15 (sealed pour islands): a via site / pad stub refused only by unlocked tracks of other (non-hard) nets is laid after those
    tracks are ripped (fewest rips first). -> the accepted result with `ripped` {net: tracks} (the caller re-lays a ripped net that
    opened; the close guard judges the whole), or None."""
    from . import constraints as CN
    cons = CN.load(constraints) if isinstance(constraints, str) else (constraints or {})
    names = sorted({o.net for o in M.obs if o.net})
    from . import power as PW
    rails = {n for n, r in PW.plan(cons).items() if r and r.get("track_mm") is not None}
    cands = []
    for r in reasons:
        path = r.get("path") or (f"via {r['at'][0]:.4f},{r['at'][1]:.4f}" if r.get("at") else None)
        if not path or not str(r.get("reason", "")).startswith("probe"):
            continue
        pr = H.probe(board, net, path, constraints, via=via, M=M)
        ids = None if pr["ok"] else _rippable(M, net, pr["violations"], names, cons)
        if ids and H.probe_prims(M, net, H.G.parse_path(path, M.cu), None, via, skip_ids=ids)["ok"]:
            cands.append((len(ids), path, ids))
    nets_of = {o.id: o.net for o in M.obs if o.id}
    seen = set()                                      # plain copper first, a rail's copper last (fewest pieces within each)
    for _n, path, ids in sorted(cands, key=lambda c: (any(nets_of.get(i) in rails for i in c[2]), c[0])):
        key = frozenset(ids)
        if key in seen or len(seen) >= RIP_TRIES:
            continue
        seen.add(key)
        if any(nets_of.get(i) in rails for i in ids):   # a rail's copper: only a rip that opens nothing (one guarded edit on `board`)
            cut, result = _rip_lay_closed(board, out, net, path, ids, constraints, via)
        else:
            tmp = out + ".rip.kicad_pcb"
            cut = _strip_ids(board, tmp, ids)
            try:
                result = H.lay(tmp, net, path, out=out, constraints=constraints, via=via)
                m = _STILL.search(str(result.get("reason") or ""))
                if not result["accepted"] and m and opens0 is not None and int(m.group(1)) < opens0:
                    import shutil                        # the rip alone let the net's own pour reach the island: the stripped board is the repair
                    shutil.copyfile(tmp, out)
                    path, result = "rip only", {"accepted": True}
            finally:
                for f in (tmp, os.path.splitext(tmp)[0] + ".kicad_pro"):
                    if os.path.exists(f):
                        os.remove(f)
        if result["accepted"]:
            return {"accepted": True, "action": "rip" if path == "rip only" else "rip+" + ("via" if path.startswith("via") else "stub+via"), "net": net, "path": path,
                    "via": list(via), "out": out, "ripped": cut, "measure": result.get("measure", {})}
        reasons.append({"path": path, "rip": sorted(cut), "reason": result.get("reason")})
    return None


def cmd(a):
    import json
    out = a.out or os.path.splitext(a.board)[0] + ".pour-island.kicad_pcb"
    if os.path.abspath(out) == os.path.abspath(a.board):
        raise SystemExit("pour-island: --out must differ from the input board")
    at = tuple(float(v) for v in a.at.split(",")) if a.at else None
    result = repair(a.board, out, a.net, a.constraints, at)
    if a.report:
        json.dump(result, open(a.report, "w"), indent=1)
    print(json.dumps(result, indent=1) if a.json else
          (f"pour-island: via placed on {a.net} at {result['at']} -> {out}" if result["accepted"] else
           f"pour-island: no guarded repair for {a.net}: {result['reason']}"))
    return 0 if result["accepted"] else 1


def add_parser(sub):
    p = sub.add_parser("pour-island", help="join a same-net pour island with one guarded stitching via")
    p.add_argument("board")
    p.add_argument("--net", required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--at", default=None, help="optional island-centre hint X,Y mm")
    p.add_argument("--constraints", default=None)
    p.add_argument("--report", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd)
