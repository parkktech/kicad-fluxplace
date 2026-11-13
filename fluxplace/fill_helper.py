"""Runs under KiCad's python (/usr/bin/python3): python3 fill_helper.py IN [OUT]
Refills every zone and saves (OUT, default IN). Prints {"zones": n}."""
import json
import sys

import pcbnew

b = pcbnew.LoadBoard(sys.argv[1])
if b is None:
    raise SystemExit("cannot load " + sys.argv[1])
n = len(b.Zones())
if n:
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
b.Save(sys.argv[2] if len(sys.argv) > 2 else sys.argv[1])
print(json.dumps({"zones": n}))
