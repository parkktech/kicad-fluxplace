"""Guarded same-net pour-island repair: one stitching via at a legal site, never an unverified island deletion."""
import math
import os

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
    """Via sites inside the untied pour islands of `net`: a fill outline that holds a pad of the net but no via and no
    through-hole pad of it has no path to the other layers. KiCad reports such an open at the zone's anchor corner, not
    at the island, so the sites are taken from the fill itself: grid points inside the outline, nearest its pads first.
    -> [(x, y)] in mm."""
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
                out += [(pcbnew.ToMM(x), pcbnew.ToMM(y)) for _d, x, y in sorted(pts)[:per_island]]
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
    return {"accepted": False, "net": net, "reason": "no legal guarded stitching-via site joins the island", "attempts": reasons}


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
