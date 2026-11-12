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
    nets = {}
    for n, ps in pads.items():
        ids = [p.m_Uuid.AsString() for p in ps]
        seen, groups = set(), 0
        for p, u in zip(ps, ids):
            if u in seen:
                continue
            groups += 1
            seen.add(u)
            seen.update(q.m_Uuid.AsString() for q in c.GetConnectedItems(p) if q.Type() == pcbnew.PCB_PAD_T)
        if groups > 1:
            nets[n] = groups - 1
    print(json.dumps({"nets": nets, "unconnected": c.GetUnconnectedCount(False)}))


main()
