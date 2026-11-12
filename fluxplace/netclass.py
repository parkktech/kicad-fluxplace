"""Net classes for placement and the pre-route gate. Pure text (routediag.load).

Every net gets one class, a weight (pull strength in the placer) and a length
budget in mm (the pre-route gate's NET_OVER_BUDGET limit). Order of decision:

  rf  >  pair  >  switch_node  >  clock  >  rail  >  bypass  >  signal

Defaults (mm / weight), overridable in constraints TOML:

    class        budget  weight  meaning
    switch_node     6      10    regulator SW pin to its inductor
    bypass          3       8    IC supply/ref pin to a cap whose other pad is ground
    rf             15       6    [rf] nets, or an RF-named net
    clock          20       5    CLK/XTAL/OSC-named or crystal/oscillator nets
                                 (crystal: 8, a net with a Y/X part and <= 3 pads)
    pair           30       4    [pairs.*] prefix, _P/_N, DP/DM naming; judged per net
    signal         30       1    everything else
    rail          none      0    ground, [power.*] nets, and nets of >= 8 pads

    [placement.budget]   switch_node = 5          # per class
    [placement.weight]   pair = 6
    [placement.net.PCIE_TX_P]  budget = 45  weight = 8  class = "pair"

Multi-drop nets are judged on MST length with budget * (1 + 0.5 * (pads-2)).
"""
import math
import re
from collections import defaultdict

from . import graph as G
from . import routediag as RD

WEIGHT = {"switch_node": 10.0, "bypass": 8.0, "rf": 6.0, "clock": 5.0,
          "pair": 4.0, "signal": 1.0, "rail": 0.0}
BUDGET = {"switch_node": 6.0, "bypass": 3.0, "rf": 15.0, "clock": 20.0,
          "crystal": 8.0, "pair": 30.0, "signal": 30.0, "rail": None}
RAIL_PADS = 8
_RF_TOK = {"RF", "RFIN", "RFOUT", "COAX", "UFL", "LNA", "ANTENNA"}
_CLK = re.compile(r"(^|_)(X?TAL\d?|CLK|MCLK|REFCLK|OSC\d?|XO\d?|XIN|XOUT)(_|$|\d)", re.I)
_SWNAME = re.compile(r"(^|_)(SW|LX|PHASE|SWITCH)\d?($|_)", re.I)
_PINSW = re.compile(r"^(SW|LX|PH|PHASE|SWITCH|SW\d)$", re.I)
_IC = re.compile(r"^(U|IC)\d")


def is_ground(net):
    return bool(RD.GROUND.match(net or ""))


def is_ic(ref, part, min_pads=5):
    """An integrated circuit: U*/IC* reference with >= 5 distinct pads. Connectors,
    relays, transformers, inductors and passives are not ICs."""
    return part["n_pads"] >= min_pads and bool(_IC.match(ref))


def is_cap(ref, part):
    return bool(re.match(r"^C\d", ref)) and part["n_pads"] == 2


def pad_xy(p):
    return (p["x"], p["y"])


def mst_length(pts):
    """Prim MST length over (x, y) points."""
    n = len(pts)
    if n < 2:
        return 0.0
    d = [math.inf] * n
    d[0] = 0.0
    done, tot = [False] * n, 0.0
    for _ in range(n):
        i = min((k for k in range(n) if not done[k]), key=lambda k: d[k])
        done[i] = True
        tot += d[i] if d[i] != math.inf else 0.0
        for k in range(n):
            if not done[k]:
                dd = math.hypot(pts[i][0] - pts[k][0], pts[i][1] - pts[k][1])
                if dd < d[k]:
                    d[k] = dd
    return tot


def net_pads(B):
    out = defaultdict(list)
    for p in B["pads"]:
        if p["net"]:
            out[p["net"]].append(p)
    return out


def pin_names(spec):
    """{ref: {pad number: pin name}} from the spec's pinmaps."""
    out = {}
    for c in (spec or {}).get("components", []):
        pm = c.get("pinmap")
        if pm:
            out[c["ref"]] = {str(v): k for k, v in pm.items()}
    return out


def _pairs(names, cons):
    """{net: partner} for every net that is half of a differential pair."""
    m = G.diff_pairs(names)
    out = {}
    for slave, master in m.items():
        out[slave], out[master] = master, slave
    fams = list((cons or {}).get("pairs", {}))
    for n in names:
        if n in out or not any(n.startswith(f) for f in fams):
            continue
        for a, b in (("_P", "_N"), ("P", "N"), ("DP", "DM"), ("+", "-")):
            if n.endswith(a) and n[:-len(a)] + b in names:
                out[n] = n[:-len(a)] + b
                out[n[:-len(a)] + b] = n
    return out


def classify(B, cons=None, spec=None):
    """{net: {class, weight, budget, reason, partner, pads}} for nets with >= 2 pads."""
    cons = cons or {}
    pl = cons.get("placement", {}) or {}
    wt = dict(WEIGHT)
    wt.update({k: float(v) for k, v in (pl.get("weight") or {}).items()})
    bd = dict(BUDGET)
    bd.update({k: (None if v is None else float(v)) for k, v in (pl.get("budget") or {}).items()})
    over = pl.get("net") or {}
    pads = net_pads(B)
    parts = B["parts"]
    pins = pin_names(spec)
    rf_names = set((cons.get("rf") or {}).get("nets", []))
    pwr = set((cons.get("power") or {}))
    pair_of = _pairs(sorted(pads), cons)
    out = {}
    for net, ps in pads.items():
        if len(ps) < 2:
            continue
        refs = [p["ref"] for p in ps]
        ics = [p for p in ps if is_ic(p["ref"], parts[p["ref"]])]
        cls, why = "signal", ""
        toks = set(re.split(r"[_\-.]", net.upper()))
        if is_ground(net) or net in pwr:
            cls, why = "rail", "ground/[power] net"
        elif net in rf_names or (not rf_names and toks & _RF_TOK):
            cls, why = "rf", "[rf] net" if net in rf_names else "RF-named"
        elif net in pair_of:
            cls, why = "pair", f"pair with {pair_of[net]}"
        else:
            sw = False
            inds = [p for p in ps if re.match(r"^L\d", p["ref"]) and parts[p["ref"]]["n_pads"] == 2]
            if inds and ics:
                if _SWNAME.search(net) or any(_PINSW.match(pins.get(p["ref"], {}).get(p["num"], ""))
                                              for p in ics):
                    sw = True
            if sw:
                cls, why = "switch_node", "regulator SW pin to inductor"
            elif len(ps) >= RAIL_PADS:
                cls, why = "rail", f"{len(ps)} pads"
            elif _CLK.search(net) or (len(ps) <= 4 and any(re.match(r"^(Y|X|XO|OSC)\d", r) for r in refs)):
                cls = "clock"
                why = "crystal/oscillator" if any(re.match(r"^(Y|X|XO|OSC)\d", r) for r in refs) \
                    and len(ps) <= 3 else "clock-named"
            elif len(ics) == 1 and len(ps) <= 4 and any(
                    is_cap(p["ref"], parts[p["ref"]]) and any(
                        q["num"] != p["num"] and is_ground(q["net"]) for q in parts[p["ref"]]["pads"])
                    for p in ps):
                cls, why = "bypass", "IC pin to cap to ground"
        budget = bd.get("crystal") if why == "crystal/oscillator" else bd.get(cls)
        weight = wt[cls]
        o = over.get(net) or {}
        if "class" in o:
            cls = o["class"]
            weight, budget = wt.get(cls, 1.0), bd.get(cls)
        if "weight" in o:
            weight = float(o["weight"])
        if "budget" in o:
            budget = float(o["budget"])
        out[net] = {"class": cls, "weight": weight, "budget": budget, "reason": why,
                    "partner": pair_of.get(net), "pads": len(ps)}
    return out


def net_length(B, net, ps=None):
    ps = ps or net_pads(B)[net]
    return mst_length([pad_xy(p) for p in ps])


def effective_budget(info):
    b = info["budget"]
    if b is None:
        return None
    return b * (1 + 0.5 * max(0, info["pads"] - 2))


def format_text(cls):
    cnt = defaultdict(int)
    for v in cls.values():
        cnt[v["class"]] += 1
    return "net classes: " + ", ".join(f"{k} {cnt[k]}" for k in
                                       ("switch_node", "bypass", "rf", "clock", "pair", "signal", "rail") if cnt[k])
