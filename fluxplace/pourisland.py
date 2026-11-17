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
    reasons = []
    for _radius, cx, cy in sites[:max_sites]:
        pos = pcbnew.VECTOR2I(pcbnew.FromMM(cx), pcbnew.FromMM(cy))
        hit_layers = {lid for lid in {lid for z in pours for lid in z.GetLayerSet().Seq()}
                      if PTF.fill_hit(b, net, lid, pcbnew.FromMM(cx), pcbnew.FromMM(cy))}
        pads = [p for f in b.GetFootprints() for p in f.Pads() if p.GetNetname() == net]
        hit_layers.update(lid for p in pads for lid in p.GetLayerSet().Seq() if p.HitTest(pos, int(via[0] * 1e6 / 2)))
        if len(hit_layers) < 2:
            continue
        path = f"via {cx:.4f},{cy:.4f}"
        probe = H.probe(board, net, path, constraints, via=via)
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
