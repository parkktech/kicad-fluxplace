"""Constraint ingest — the 'Constraints' stage: a hand-written TOML file that
overrides the pipeline's inferences with the engineer's numbers. Everything is
optional; anything not stated keeps the heuristic default. Schema:

    [power."+5V"]           # per power net (quote names with symbols)
    max_current_ma = 4000   # -> routed width via a 1oz outer-layer rule
    pour = true             # rail rides a plane/pour, not a fat trace

    [pairs.PCIE_TX]         # diff-pair family, matched by net-name prefix
    impedance_diff = 85     # recorded for the stackup/report (not yet enforced)
    skew_mm = 0.1           # SI-lite intra-pair skew limit for these nets

    [si]
    default_skew_mm = 1.0   # SI-lite limit for pairs with no family entry

    [env]                   # the PRODUCT environment — drives the review gate
    temp_min_c = -30        # every part must be rated at least this wide
    temp_max_c = 85
    vibration = "high"      # low | high  (vehicle / handheld = high)
    moisture = "outdoor"    # dry | humid | condensing | outdoor
    transient = "auto-12v"  # none | auto-12v | auto-24v (input protection class)

    [nets.PTT_THRU]         # per-net rules the reviewer holds the copper to
    straight_copper = ["J1A:1", "J2A:1"]   # ONLY these pads may be on the net
    max_vias = 0            # (RF nets) layer transitions allowed

    [rf]
    target_z = 50           # single-ended target for RF-named nets
    tolerance_pct = 10
    max_vias = 1
    nets = ["RF_GNSS"]      # optional: explicit list instead of name hints

    [power."+5V"]           # (in addition to the ampacity fields above)
    holdup_ms = 20          # ride-through the spec promises ...
    nominal_v = 5.0         # ... from this rail voltage ...
    min_v = 4.75            # ... down to the load's minimum ...
    load_a = 1.0            # ... at this current. Checked against bulk C.

    [protection]
    tvs_ref = "D1"
    downstream_max_v = 40   # rating of the first device behind the TVS
    clamp_v = 38.9          # optional; distributor data fills it when absent
    min_margin_pct = 15

Width rule: conservative 1oz external copper, ~0.5 mm per amp with a 0.3 mm
floor — wide enough for a 10C rise at the stated current, narrow enough not to
eat the board. The engineer can always state width_mm directly instead.
"""
import tomllib


def load(path):
    """Parse the TOML; returns the raw dict ({} for None path)."""
    if not path:
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def power_width_mm(cons, net, default_mm):
    """Routed trace width for a power net: explicit width_mm wins, else the
    ampacity rule on max_current_ma, else the caller's heuristic default."""
    p = cons.get("power", {}).get(net)
    if not p:
        return default_mm
    if "width_mm" in p:
        return float(p["width_mm"])
    if "max_current_ma" in p:
        return max(0.3, 0.5 * float(p["max_current_ma"]) / 1000.0)
    return default_mm


def pour_nets(cons):
    """Power nets the engineer says ride a plane/pour — the router should not
    also draw them as fat traces."""
    return {n for n, p in cons.get("power", {}).items() if p.get("pour")}


def skew_limit_mm(cons, master):
    """SI-lite intra-pair skew limit for the pair whose master net is `master`:
    the longest matching [pairs.*] prefix wins, else [si].default_skew_mm."""
    best = None
    for fam, p in cons.get("pairs", {}).items():
        if master.startswith(fam) and "skew_mm" in p:
            if best is None or len(fam) > best[0]:
                best = (len(fam), float(p["skew_mm"]))
    if best:
        return best[1]
    return float(cons.get("si", {}).get("default_skew_mm", 1.0))


def env_profile(cons):
    """The product environment, or None when nobody stated one. Numbers are
    floats; strings are lower-cased. A temperature range is the minimum a
    profile must carry — without it no part can be derated."""
    e = (cons or {}).get("env")
    if not e:
        return None
    if "temp_min_c" not in e or "temp_max_c" not in e:
        return None
    return {
        "temp_min_c": float(e["temp_min_c"]),
        "temp_max_c": float(e["temp_max_c"]),
        "vibration": str(e.get("vibration", "low")).lower(),
        "moisture": str(e.get("moisture", "dry")).lower(),
        "transient": str(e.get("transient", "none")).lower(),
    }


def env_toml(env):
    """Render an environment dict (intake output) as a [env] TOML block."""
    lines = ["[env]"]
    for k in ("temp_min_c", "temp_max_c"):
        if k in env:
            lines.append(f"{k} = {float(env[k]):g}")
    for k in ("vibration", "moisture", "transient", "location"):
        if env.get(k):
            lines.append(f'{k} = "{env[k]}"')
    return "\n".join(lines) + "\n"


# Differential geometry per impedance target on the JLC7628-class stackup
# (outer-layer coupled microstrip; 100R matches profiles.pair_geom, 85/90
# from JLCPCB's published impedance table — CALIBRATE against Simbeor
# numbers returned by external audits).
PAIR_GEOM_BY_Z = {100: (0.173, 0.107), 90: (0.201, 0.127), 85: (0.226, 0.127)}

_RAIL_W = ((3000, 1.5), (2500, 1.2), (1500, 0.8), (900, 0.5), (0, 0.3))


def rail_width_mm(ma):
    for floor, w in _RAIL_W:
        if ma >= floor:
            return w
    return 0.3


def inject_netclasses(pro_path, cons, log=print):
    """Write the engineering constraints INTO the project file as KiCad net
    classes, so any ECAD parser (Quilter's 'ECAD PARSED CONSTRAINTS', the
    KiCad router, a human) sees the intent instead of guessing defaults
    (measured: Quilter guessed 100R for every pair and 500mA for every rail
    because the .kicad_pro carried no classes). Pairs get diff_pair_width/
    gap for their impedance target; rails get ampacity track widths."""
    import json
    d = json.load(open(pro_path))
    ns = d.setdefault("net_settings", {})
    classes = [c for c in ns.get("classes", [])
               if not (c.get("name", "").startswith(("DP_", "PWR_")))]
    patterns = [p for p in ns.get("netclass_patterns", [])
                if not p.get("netclass", "").startswith(("DP_", "PWR_"))]
    for group, p in sorted((cons or {}).get("pairs", {}).items()):
        z = int(p.get("impedance_diff", 100))
        w, g = PAIR_GEOM_BY_Z.get(z, PAIR_GEOM_BY_Z[100])
        name = f"DP_{group}_{z}R"
        classes.append({
            "name": name, "clearance": 0.1,
            "track_width": w, "diff_pair_width": w, "diff_pair_gap": g,
            "via_diameter": 0.45, "via_drill": 0.25,
            "diff_pair_via_gap": 0.25,
        })
        patterns.append({"netclass": name, "pattern": f"*{group}_*"})
    for net, pw in sorted((cons or {}).get("power", {}).items()):
        ma = pw.get("max_current_ma")
        if not ma or pw.get("pour"):
            continue
        name = f"PWR_{net.strip('+').replace('/', '_')}"
        classes.append({
            "name": name, "clearance": 0.15,
            "track_width": rail_width_mm(ma),
            "via_diameter": 0.6, "via_drill": 0.3,
        })
        patterns.append({"netclass": name, "pattern": net})
    ns["classes"] = classes
    ns["netclass_patterns"] = patterns
    json.dump(d, open(pro_path, "w"), indent=2)
    log(f"  netclasses injected: {sum(1 for c in classes if c['name'].startswith('DP_'))} "
        f"pair classes, {sum(1 for c in classes if c['name'].startswith('PWR_'))} rail classes")
    return len(classes)
