"""Net classes for placement and the pre-route gate. Pure text (routediag.load).

Every net gets one class, a weight (pull strength in the placer) and a length
budget in mm (the pre-route gate's NET_OVER_BUDGET limit). Order of decision:

  rf  >  pair  >  switch_node  >  clock  >  rail  >  bypass  >  signal

Defaults (mm / weight) and the physical basis of each (BASIS below carries the same text):

    class        budget  weight  basis
    switch_node     6      10    loop inductance: ~1 nH/mm of narrow trace; 6 mm ~ 5 nH in the SW loop
    bypass          3       8    loop inductance: 3 mm ~ 2.5 nH, SRF of the cap + its trace stays > 100 MHz
    rf           17.7       6    insertion loss: 0.5 dB at 2.4 GHz on FR-4 microstrip
                                 (0.0118 dB/mm per GHz); [placement.rf] freq_ghz / loss_db
    clock          20       5    edge rate: ~0.7 x t_r / (6 t_pd) for a 1 ns edge, t_pd 6 ps/mm
                                 (crystal 8: ~0.1 pF/mm stray C vs a load-cap tolerance of ~1 pF;
                                 an active oscillator is a plain clock, not a crystal)
    pair usb2      100       4    USB 2.0 HS: spec's 5 in host-side trace, derated; skew 3 mm
    pair usb3      100       4    USB 3 5 Gb/s: ~1 dB/in at 2.5 GHz Nyquist, ~6 dB channel budget
    pair pcie      100       4    PCIe Gen2/3 x1: 8 GT/s ~1.2 dB/in at 4 GHz, ~8 dB budget
    pair eth       100       4    1000BASE-T PHY -> magnetics: vendor guidance <= 100 mm; skew 1 mm
    pair other      60       4    unknown speed: ~1.5 ns edge, reflection-critical length
    pair audio    none       4    analog audio pair: wavelength km, only noise pickup matters
    signal          60       1    digital edge >= 2.5 ns: t_r / (6 t_pd) ~ 62 mm before a stub needs termination
    slow          none       1    I2C / reset / interrupt / LED / key / status / CAN <= 1 Mb/s / analog audio:
                                 kHz-class, effectively unbounded (still pulled short by weight 1)
    rail          none       0    ground, [power.*] nets, power-named nets, nets of >= 8 pads: current
                                 capacity (width / pour), not length

    [placement.budget]   switch_node = 5          # per class (pair = N sets every pair kind)
    [placement.budget]   "pair:usb2" = 80         # one pair kind
    [placement.weight]   pair = 6
    [placement.net.PCIE_TX_P]  budget = 45  weight = 8  class = "pair"
    [placement.budget."USB_HUB_DP"] = {mm = 60, reason = "hub is across the board by the mechanical stack"}
                                                  # per-net override; the reason is printed by route-ready

Multi-drop nets are judged on MST length with budget * (1 + 0.5 * (parts-2)), parts = distinct references on the net.
"""
import math
import re
from collections import defaultdict

from . import graph as G
from . import routediag as RD

WEIGHT = {"switch_node": 10.0, "bypass": 8.0, "rf": 6.0, "clock": 5.0,
          "pair": 4.0, "signal": 1.0, "slow": 1.0, "rail": 0.0}
RF_DB_PER_MM_GHZ = 0.0118        # FR-4 microstrip, dielectric + conductor, 0.028 dB/mm at 2.4 GHz


def rf_budget_mm(freq_ghz=2.4, loss_db=0.5):
    return round(loss_db / (RF_DB_PER_MM_GHZ * freq_ghz), 1)


BUDGET = {"switch_node": 6.0, "bypass": 3.0, "rf": rf_budget_mm(), "clock": 20.0,
          "crystal": 8.0, "pair": 60.0, "pair:usb2": 100.0, "pair:usb3": 100.0, "pair:pcie": 100.0,
          "pair:eth": 100.0, "pair:audio": None, "signal": 60.0, "slow": None, "rail": None}
# defaults before 2026-11-11 round 2 (old -> new): rf 15 -> 17.7, pair 30 -> 100 (usb2/usb3/pcie/eth)
# or 60 (other) or none (analog audio), signal 30 -> 60, I2C/reset/interrupt/LED/key/status/CAN/analog
# audio signals 30 -> none ("slow" class), oscillators (active) 8 -> 20 (only crystals keep 8).
OLD_BUDGET = {"switch_node": 6.0, "bypass": 3.0, "rf": 15.0, "clock": 20.0, "crystal": 8.0,
              "pair": 30.0, "signal": 30.0, "rail": None}
BASIS = {
    "switch_node": "loop inductance (~1 nH/mm): 6 mm is ~5 nH in the switching loop",
    "bypass": "loop inductance: 3 mm is ~2.5 nH, keeps the cap's SRF above 100 MHz",
    "rf": "insertion loss on FR-4 microstrip, 0.0118 dB/mm/GHz: 0.5 dB at 2.4 GHz = 17.7 mm",
    "clock": "edge rate: ~0.7 t_r/(6 t_pd) with t_r 1 ns, t_pd 6 ps/mm; crystal 8 mm = ~0.8 pF stray C",
    "pair:usb2": "USB 2.0 HS spec: 5 in host-side trace, derated to 100 mm; intra-pair skew <= 3 mm",
    "pair:usb3": "USB 3 5 Gb/s: ~1 dB/in at 2.5 GHz, ~6 dB channel budget -> 100 mm",
    "pair:pcie": "PCIe Gen2/3 x1: ~1.2 dB/in at 4 GHz, ~8 dB budget -> 100 mm",
    "pair:eth": "1000BASE-T PHY to magnetics: vendor guidance <= 100 mm, skew <= 1 mm",
    "pair:audio": "analog audio pair: noise pickup, not length",
    "pair": "unknown speed pair: 60 mm (reflection-critical length for ~1.5 ns edges)",
    "signal": "digital edge >= 2.5 ns: t_r/(6 t_pd) ~ 62 mm before a stub needs termination",
    "slow": "I2C / reset / interrupt / LED / key / status / CAN <= 1 Mb/s / analog audio: unbounded",
    "rail": "current capacity (width or pour), not length",
}
SLOW_TOK = re.compile(r"^(I2C\d*|SDA\d*|SCL\d*|N?RST\w*|RESET\w*|N?INT\d*|N?IRQ\d*|LED\w*|KEY\w*|\w*BOOT|PPS|"
                      r"SENSE\w*|STATUS\w*|PWRFAIL|ALERT|FAULT|N?PG|PGOOD|WAKE|CAN\d*|PTT\w*)$")
AUDIO_TOK = re.compile(r"^(MIC\w*|SPK\w*|AUDIO|AUD|HP[LR]|INJ|ADC|DAC|MIX\w*|ATT|LINE\w*)$")
_RAILNAME = re.compile(r"^[+-]?\d+V\d*(_|$)|^[+-]?\d+V\d*$|^(VCC|VDD|VBAT|VBATT|VIN|VBUS|V12|V5|V3)\w*$|_(\d+V\d*|VCC|VDD)$", re.I)


def pair_kind(net):
    """usb2 | usb3 | pcie | eth | audio | other, by net name."""
    toks = [t for t in re.split(r"[_\-.]", net.upper()) if t]
    if any(t in ("PCIE", "PCI") for t in toks):
        return "pcie"
    if any(t in ("USB3", "SS", "SSRX", "SSTX") for t in toks):
        return "usb3"
    if any(t.startswith("ETH") or t in ("MDI", "LAN") for t in toks):
        return "eth"
    if any(t.startswith("USB") or t in ("DP", "DM") for t in toks):
        return "usb2"
    if any(AUDIO_TOK.match(t) for t in toks):
        return "audio"
    return "other"


FAST_TOK = {"SCK", "SCLK", "MOSI", "MISO", "I2S", "BCLK", "LRCLK", "LRCK", "SDIO", "QSPI", "SPI", "ANT", "FEED",
            "ANTENNA", "RF"}


def is_slow(net):
    toks = [t for t in re.split(r"[_\-.]", net.upper()) if t]
    if set(toks) & FAST_TOK:            # SPI / I2S clocks and data are MHz, antenna nets are RF
        return ""
    if any(SLOW_TOK.match(t) for t in toks):
        return "I2C/control/status"
    if any(AUDIO_TOK.match(t) for t in toks):
        return "analog audio"
    return ""
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


def _only_caps_besides(ps, ics, parts):
    """Every non-IC pad on the net is a capacitor whose other pad is ground."""
    rest = [p for p in ps if p not in ics]
    return bool(rest) and all(
        is_cap(p["ref"], parts[p["ref"]]) and any(
            q["num"] != p["num"] and is_ground(q["net"]) for q in parts[p["ref"]]["pads"])
        for p in rest)


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
    rfc = pl.get("rf") or {}
    if rfc:
        bd["rf"] = rf_budget_mm(float(rfc.get("freq_ghz", 2.4)), float(rfc.get("loss_db", 0.5)))
    user = pl.get("budget") or {}
    per_net_budget = {k: v for k, v in user.items() if isinstance(v, dict)}
    ub = {k: (None if v is None else float(v)) for k, v in user.items() if not isinstance(v, dict)}
    bd.update(ub)
    if "pair" in ub:                       # an explicit pair budget covers every pair kind
        for k in [k for k in bd if k.startswith("pair:") and k not in ub]:
            bd[k] = ub["pair"]
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
        cls, why, kind = "signal", "", ""
        toks = set(re.split(r"[_\-.]", net.upper()))
        if is_ground(net) or net in pwr:
            cls, why = "rail", "ground/[power] net"
        elif net in rf_names or (not rf_names and toks & _RF_TOK):
            cls, why = "rf", "[rf] net" if net in rf_names else "RF-named"
        elif net in pair_of:
            cls, why = "pair", f"pair with {pair_of[net]}"
            kind = pair_kind(net)
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
            elif len(ics) == 1 and len(ps) <= 4 and _only_caps_besides(ps, ics, parts):
                cls, why = "bypass", "IC pin to cap to ground"
            elif is_slow(net):
                cls, why = "slow", is_slow(net)
            elif _RAILNAME.search(net):
                cls, why = "rail", "power-named net"
        if why == "crystal/oscillator" and any(
                re.match(r"^(Y|X|XO|OSC)\d", r) and _active(parts[r]) for r in refs):
            why = "active oscillator"          # a driven clock, not a crystal: the clock budget
        budget = bd.get("crystal") if why == "crystal/oscillator" else (
            bd.get(f"pair:{kind}") if cls == "pair" else bd.get(cls))
        weight = wt[cls]
        o = over.get(net) or {}
        if "class" in o:
            cls = o["class"]
            weight, budget = wt.get(cls, 1.0), bd.get(cls)
        if "weight" in o:
            weight = float(o["weight"])
        if "budget" in o:
            budget = float(o["budget"])
        reason = ""
        if net in per_net_budget:
            pb = per_net_budget[net]
            budget, reason = (None if pb.get("mm") is None else float(pb["mm"])), str(pb.get("reason", ""))
        out[net] = {"class": cls, "weight": weight, "budget": budget, "reason": why,
                    "partner": pair_of.get(net), "pads": len(ps), "drops": len(set(refs)),
                    "kind": kind, "override": net in per_net_budget or "budget" in o,
                    "override_reason": reason or str(o.get("reason", ""))}
    return out


def _active(part):
    """An oscillator module (>= 3 pads incl. a power pin), not a bare crystal."""
    return part["n_pads"] >= 3 and any(
        p["net"] and not is_ground(p["net"]) and _RAILNAME.search(p["net"]) for p in part["pads"])


def net_length(B, net, ps=None):
    ps = ps or net_pads(B)[net]
    return mst_length([pad_xy(p) for p in ps])


def effective_budget(info):
    b = info["budget"]
    if b is None:
        return None
    return b * (1 + 0.5 * max(0, info.get("drops", info["pads"]) - 2))


def format_text(cls):
    cnt = defaultdict(int)
    for v in cls.values():
        cnt[v["class"]] += 1
    return "net classes: " + ", ".join(f"{k} {cnt[k]}" for k in
                                       ("switch_node", "bypass", "rf", "clock", "pair", "signal", "slow", "rail") if cnt[k])
