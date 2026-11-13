"""Runs under KiCad's python (/usr/bin/python3, has `pcbnew`), NOT the fluxplace venv:
    python3 conn_helper.py BOARD [--fill]
Real connectivity: optionally refills the zones, builds KiCad's connectivity and prints
JSON {"nets": {net: open_connections}, "unconnected": KiCad's own total}.  A net's open
connections = (clusters of its pads joined by copper/zones) - 1."""
import json
import sys

import pcbnew


def main():
    path, fill = sys.argv[1], "--fill" in sys.argv[2:]
    b = pcbnew.LoadBoard(path)
    if b is None:
        raise SystemExit("cannot load " + path)
    if fill and len(b.Zones()):
        pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    b.BuildConnectivity()
    c = b.GetConnectivity()
    c.RecalculateRatsnest()
    pads = {}
    for p in b.GetPads():
        if p.GetNetname():
            pads.setdefault(p.GetNetname(), []).append(p)
    nets, clusters = {}, {}
    mm = pcbnew.ToMM
    for n, ps in pads.items():
        ids = [p.m_Uuid.AsString() for p in ps]
        seen, groups, cl = set(), 0, []
        for p, u in zip(ps, ids):
            if u in seen:
                continue
            groups += 1
            seen.add(u)
            mem = {u}
            for q in c.GetConnectedItems(p):
                if q.Type() == pcbnew.PCB_PAD_T:
                    seen.add(q.m_Uuid.AsString())
                    mem.add(q.m_Uuid.AsString())
            cl.append([q for q, w in zip(ps, ids) if w in mem])
        if groups > 1:
            nets[n] = groups - 1
            clusters[n] = [[{"ref": q.GetParentFootprint().GetReference(), "num": q.GetNumber(),
                             "x": round(mm(q.GetPosition().x), 4), "y": round(mm(q.GetPosition().y), 4),
                             "layers": [b.GetLayerName(l) for l in q.GetLayerSet().CuStack()]} for q in g] for g in cl]
    print(json.dumps({"nets": nets, "unconnected": c.GetUnconnectedCount(False), "clusters": clusters}))


main()
