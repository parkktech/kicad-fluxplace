"""Constraint ingest — the 'Constraints' stage: a hand-written TOML file that
overrides the pipeline's inferences with the engineer's numbers. Everything is
optional; anything not stated keeps the heuristic default. Schema:

    [power."+5V"]           # per power net (quote names with symbols)
    max_current_ma = 4000   # -> routed width via a 1oz outer-layer rule
    pour = true             # rail rides a plane/pour, not a fat trace

    [pairs.PCIE_TX]         # diff-pair family, matched by net-name prefix
    impedance_diff = 85     # recorded for the stackup/report (not yet enforced)
    skew_mm = 0.1           # SI-lite intra-pair skew limit for these nets

    [channel.PCIE_TX]       # budget from the parts at each end (fluxplace/channel.py): every key optional, `source` required
    loss_db_max = 6.0       # also loss_db_target, freq_ghz, skew_ps_max/_target, length_mm_max/_target, stub_mm_max, via_db, connector_db
    source = "CM5 datasheet p. N"
    [[dcdrop]]              # net, current_a, drop_mv_max, drop_mv_target, source; from/to (source and load groups "REF" or "REF.PAD"),
                            # temp_c, to_a = {REF = amps} (per-load currents with a datasheet source; else every load carries current_a)

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
    width_mm = 0.2          # declared RF geometry: a width, or {"F.Cu" = 0.2, "In3.Cu" = 0.15}; width_tol_mm = 0.02
                            # (else the impedance computed from the stackup is graded; `[nets.X] width_mm` and the map's rf_geometry win)

    [review]                # waive = ["UNROUTED:^\\+3V3$"] waives one open net; open_gate = false switches UNROUTED/STALE_FILL off
    [closure]               # window_mm radius_mm step_mm nudge_max_mm max_trials track_mm clearance_mm via_mm (docs/commands/closure.md)
    [escape]                # near_mm 2.0, lane_mm 1.5, window_mm 4.0, radius_mm 2.5, enabled, power_pins (escape reservation, escres.py)
    [route]                 # power_corridor_max_mm (longest planned power corridor), power_crowding (current crowding factor, default 1.25), track_mm / clearance_mm = PREFERRED routing sizes (CLI > these > 0.2; lifted to min_track_mm / min_clearance_mm); same_net_via_spacing = "freerouting"|"hole" (see docs/commands/route.md); tune_rounds = 3, tune_pairs = 8: auto-tune of pairs failing only on skew at the end of the route stage

    [floorplan]             # w_power = 1.0 (weight of the power source-to-load reach term; planned, floorplan2); block_weight breakout_weight stay waive_fail
    [[rule]]                # selector rules over the same keys (fluxplace/rules.py, docs/commands/rules.md)
    select = { net = "^USB_.*" }   # ref | net | netclass | type | layer | prop | pair (AND)
    set = { width_mm = 0.15 }      # width_mm gap_mm clearance_mm skew_mm impedance_diff straight_copper max_vias pour

    [power."+5V"]           # (in addition to the ampacity fields above)
    support = {C117 = "J200.70"}   # which pin a rail-only support part serves (powerbudget.supports; decided, never guessed)
    dcdrop_exempt = "R11 10 ohm sense resistor in series: the drop is the design"   # a rail with a current and NO [[dcdrop]] row by design
                                   # (ready lists it as INFO POWER_PLAN_EXEMPT; without the reason, or the row, ready FAILs: D71dd)
    holdup_ms = 20          # ride-through the spec promises ...
    nominal_v = 5.0         # ... from this rail voltage ...
    min_v = 4.75            # ... down to the load's minimum ...
    load_a = 1.0            # ... at this current. Checked against bulk C.

    [sourcing]              # where the board will be built
    china = true            # grade every MPN on LCSC/JLCPCB stock too (CN_* findings)
    cn_need = 100           # units the assembly house must be able to pull
    [sourcing.cn_alias]     # our MPN -> the string LCSC catalogues it under
    "G6K-2F-Y DC5" = "G6K-2F-Y-5V"

    [docs]                  # documentation is a build requirement
    project_libs = ["hardware/lib/footprints/utv-comms.pretty"]  # footprints
                            # drawn here need a `landpattern` citation in the spec
    datasheets = "docs/datasheets"   # every MPN's PDF lives here, with datasheets.json
    strict = true           # missing MPN / datasheet / pin evidence = FAIL, not WARN

    [protection]
    tvs_ref = "D1"
    downstream_max_v = 40   # rating of the first device behind the TVS
    clamp_v = 38.9          # optional; distributor data fills it when absent
    min_margin_pct = 15

    [parts]                 # part-entry gate (docs/commands/parts.md)
    generic_passives = false  # true: passives by value/tolerance/package are acceptable
    pinmap_exempt = ["J*"]; passive_datasheet = "warn"; package = "required"; enforce = true; waive = ["CODE:REGEX"]

    [stackup]               # readiness gate (docs/commands/ready.md): impedance is COMPUTED from this
    planes = ["In1.Cu"]     # reference layers; [[stackup.layers]] name/type/thickness/epsilon_r, or profile = "name"
    [ready]
    min_feature_mm = 0.127  # smallest width/gap the computed geometry may use

    [fab]                   # what the quoted fab process can actually build
    blind_vias = false      # default false: a blind/buried/micro via on the
    buried_vias = false     # board is a VIA_TYPE review FAIL unless the
    microvias = false       # matching flag here says the process supports it
    gerber_check = "warn"   # "fail": a FAIL-level GERBER_* finding (or a check
                            # error, GERBER_CHECK_ERROR) blocks fab and deliver
                            # with exit 1. "warn" (default): findings print
                            # prominently, exit code unchanged. "off": skip.
    profile = "pcbway-standard"  # "<vendor>-<tier>" (fabcaps: fluxplace fab-profiles) —
                            # with the board's layer count it SELECTS the
                            # manufacturer's hard limits; unknown name = error.
                            # A LIST (profile = ["pcbway-standard", "jlcpcb-standard"])
                            # = a board that must be buildable at every listed
                            # fab: each hard limit is the strictest of the rows,
                            # review and fab-advise name which fab sets it
    min_track_mm = 0.1      # narrowest copper the quoted fab will accept —
    min_clearance_mm = 0.1  # PCBWay rejected a 0.09 mm UTV V1.5 GND stub at
    min_drill_mm = 0.2      # upload ("minimum trace width ... no less than
    min_annular_mm = 0.1    # 0.1mm") that DRC/fab never caught because the
    min_via_dia_mm = 0.4    # board's own design-rule minimum (0.088 mm) was
    min_silk_width_mm = 0.15 # looser than the fab's real floor (D-fabcap).
                            # review.check_fab_capability() grades every
                            # track/arc/via/PTH pad against these; repair,
                            # patch, bridge and drc-fix clamp any width/via/
                            # drill they create or narrow up to these floors.

Width rule: conservative 1oz external copper, ~0.5 mm per amp with a 0.3 mm
floor — wide enough for a 10C rise at the stated current, narrow enough not to
eat the board. The engineer can always state width_mm directly instead.
"""
import re
import tomllib


def load(path):
    """Parse the TOML; returns the raw dict ({} for None path)."""
    if not path:
        return {}
    from . import placerules as PR
    with open(path, "rb") as f:
        return PR.expand(tomllib.load(f))          # [placement.module.*] with an explicit outline -> keep-out / no_tht


def power_width_mm(cons, net, default_mm):
    """Routed trace width for a power net: explicit width_mm wins, else the current-derived width (power.py: IPC-2221,
    the fab row's copper weights, dT), else the caller's heuristic default."""
    from . import power as PW
    if not (cons or {}).get("power", {}).get(net):
        return default_mm
    return PW.track_width_mm(cons, net, default_mm)


_FAB_DEFAULTS = {
    "min_track_mm": 0.1, "min_clearance_mm": 0.1, "min_drill_mm": 0.2,
    "min_annular_mm": 0.1, "min_via_dia_mm": 0.4, "min_silk_width_mm": 0.15,
    # acid trap: junction angle below this (deg) is an ACID_TRAP; the profile
    # does not state one, so the default is 60 (acidtrap.DEFAULT_ACID_DEG)
    "acid_trap_deg": 60.0,
}


def fab_profile(cons, layers=None, board=None):
    """The [fab] capability floor, defaults filled in. `profile = "<vendor>-<tier>"` selects a manufacturer row
    (fabcaps, by the board's copper `layers`, or read from `board`) and those numbers are HARD limits: an explicit
    [fab] min_* is honoured only when it is STRICTER than the profile. An unknown profile name raises
    fabcaps.FabProfileError (it used to fall back silently). Extra keys: `hard` (limits dict), `vendor`, `tier`,
    `layers`, `unverified`."""
    from . import fabcaps as FC
    fab = dict((cons or {}).get("fab", {}))
    out = dict(_FAB_DEFAULTS)
    name = fab.get("profile")
    res = None
    if name:
        if layers is None and board:
            layers = FC.board_layer_count(board)
        if layers is None:
            sl = ((cons or {}).get("stackup") or {}).get("layers") or []
            n = sum(1 for l in sl if isinstance(l, dict) and str(l.get("type", "copper")) == "copper")
            layers = n or None
        res = FC.limits(name, layers)                   # a list of profiles = the strictest of all of them
        out.update(res["floors"])
    explicit = {}
    for k in _FAB_DEFAULTS:
        if k in fab:
            v = float(fab[k])
            if res and k in res["floors"] and v > out[k] + 1e-9:
                explicit[k] = True
            out[k] = max(v, out[k]) if (res and k in res["floors"]) else v
    out["profile"] = res["name"] if res else name
    out["profiles"] = res["profiles"] if res else []
    out["provenance"] = {k: ("[fab] explicit value" if explicit.get(k) else " = ".join(v))
                         for k, v in res["floor_prov"].items()} if res else {}
    out["hard_provenance"] = {k: " = ".join(v) for k, v in res["provenance"].items()} if res else {}
    out["hard"] = res["limits"] if res else {}
    out["vendor"], out["tier"] = (res["vendor"], res["tier"]) if res else (None, None)
    out["layers"] = res["layers"] if res else layers
    out["unverified"] = res["unverified"] if res else []
    for k in ("blind_vias", "buried_vias", "microvias"):
        out[k] = bool(fab.get(k, False))
    return out


def _clamp_floor(value_mm, cons, key, log=None, what=""):
    """Raise `value_mm` up to the [fab] floor named by `key` when
    constraints are loaded; otherwise pass it through unchanged (no
    constraints = no opinion on what the fab can build). Logs the raise."""
    if not cons or value_mm is None:
        return value_mm
    floor = fab_profile(cons)[key]
    if value_mm < floor:
        if log:
            log(f"    fab-capability clamp: {what + ' ' if what else ''}"
                f"{value_mm:g} mm -> {floor:g} mm ({key})")
        return floor
    return value_mm


def clamp_track_mm(width_mm, cons, log=None, what=""):
    """Clamp a track/arc width a repair/patch/route pass is about to WRITE
    up to [fab] min_track_mm. Born from D-fabcap: PCBWay's upload audit
    rejected two 0.09 mm GND stubs that neither DRC nor review had flagged
    because the board's own design-rule minimum (0.088 mm) was looser than
    the fab's real floor."""
    return _clamp_floor(width_mm, cons, "min_track_mm", log, what)


def clamp_via_mm(dia_mm, cons, log=None, what=""):
    """Clamp a via/pad copper diameter up to [fab] min_via_dia_mm."""
    return _clamp_floor(dia_mm, cons, "min_via_dia_mm", log, what)


def clamp_drill_mm(drill_mm, cons, log=None, what=""):
    """Clamp a via/PTH drill up to [fab] min_drill_mm."""
    return _clamp_floor(drill_mm, cons, "min_drill_mm", log, what)


def not_pairs(cons):
    """Nets the owner declared plain signals although their names look like a pair (top-level `not_pairs = [...]`)."""
    return set((cons or {}).get("not_pairs") or [])


def pour_nets(cons):
    """Power nets the engineer says ride a plane/pour — the router should not
    also draw them as fat traces."""
    from . import power as PW
    return {n for n, p in cons.get("power", {}).items() if isinstance(p, dict) and (p.get("pour") or PW.is_pour(cons, n))}


SLOW_PAIR_SKEW_MM = 10.0      # audio-rate differential nets: a skew limit in the mm range has no meaning


def _skew_entry(d, mm_key, ps_key):
    """(value, unit, key) of the mm or _ps spelling of a skew limit in d, else None."""
    if mm_key in d:
        return float(d[mm_key]), "mm", mm_key
    if ps_key in d:
        return float(d[ps_key]), "ps", ps_key
    return None


def _to_mm(val, unit, key, ps_per_mm, master):
    """A ps limit in mm via the layer delay of `master` (a float, or callable(master) -> ps/mm).
    No delay known -> ValueError: a ps limit is never converted by a guessed delay."""
    if unit == "mm":
        return val
    pp = ps_per_mm(master) if callable(ps_per_mm) else ps_per_mm
    if not pp:
        raise ValueError(f"{key} is in ps but the layer delay of {master} is unknown")
    return val / pp


def skew_limit_source(cons, master, ps_per_mm=None):
    """(limit mm, where it comes from) for the pair whose master net is `master`. Resolved through
    rules.resolve: the longest matching [pairs.FAMILY] skew_mm, or a `[[rule]]` that sets skew_mm; else the
    longest matching [pairs.FAMILY] skew_ps; else `[si] skew_mm_by_kind` (or skew_ps_by_kind) for the pair's
    kind (netclass.pair_kind), else low-speed (audio) pairs get `[si] audio_skew_mm` (or audio_skew_ps, 10 mm),
    everything else `[si] default_skew_mm` (or default_skew_ps, 1 mm). ps values convert with `ps_per_mm`
    (float, or callable(master) giving the ps/mm on the layers the net runs on)."""
    from . import rules as RU
    # one precedence: every skew entry (legacy [pairs.F] skew_mm / skew_ps, explicit [[rule]] skew_mm / skew_ps)
    # is a rule; the best-ranked hit wins (explicit rule / longest pair family), whatever its unit.
    rs = RU.load(cons)
    obj = {"net": master}
    cands = []
    for key in ("skew_mm", "skew_ps"):
        hits = rs.cascade(obj, key)
        if hits:
            r, v = hits[0]
            cands.append((r.rank(), key, float(v), r))
    if cands:
        _rk, key, v, r = max(cands, key=lambda c: c[0])
        unit = "mm" if key == "skew_mm" else "ps"
        return _to_mm(v, unit, f"{r.label} {key}", ps_per_mm, master), f"{r.label} {key}"
    si = cons.get("si", {}) or {}
    from . import netclass as NC
    kind = NC.pair_kind(master)
    by = si.get("skew_mm_by_kind") or {}
    if kind in by:
        return float(by[kind]), f"[si] skew_mm_by_kind.{kind}"
    by_ps = si.get("skew_ps_by_kind") or {}
    if kind in by_ps:
        return _to_mm(float(by_ps[kind]), "ps", f"[si] skew_ps_by_kind.{kind}", ps_per_mm, master), \
            f"[si] skew_ps_by_kind.{kind}"
    if kind == "audio":
        e = _skew_entry(si, "audio_skew_mm", "audio_skew_ps")
        if e:
            return _to_mm(e[0], e[1], f"[si] {e[2]}", ps_per_mm, master), f"[si] {e[2]} (low-speed pair)"
        return SLOW_PAIR_SKEW_MM, "[si] audio_skew_mm (low-speed pair)"
    e = _skew_entry(si, "default_skew_mm", "default_skew_ps")
    if e:
        return _to_mm(e[0], e[1], f"[si] {e[2]}", ps_per_mm, master), f"[si] {e[2]}"
    return 1.0, "[si] default_skew_mm"


def skew_max_source(cons, master, ps_per_mm=None):
    """(maximum mm, where it comes from): the skew a pair FAILS past (D71cv).  `skew_max_mm` / `skew_max_ps` resolve with the same
    precedence as `skew_mm` / `skew_ps` (skew_limit_source, which is the TARGET); without one the maximum is the target, so a board
    that states no maximum behaves as before.  A maximum below the target is raised to it."""
    from . import rules as RU
    tgt, tsrc = skew_limit_source(cons, master, ps_per_mm)
    rs = RU.load(cons)
    cands = []
    for key in ("skew_max_mm", "skew_max_ps"):
        hits = rs.cascade({"net": master}, key)
        if hits:
            r, v = hits[0]
            cands.append((r.rank(), key, float(v), r))
    if not cands:
        return tgt, tsrc
    _rk, key, v, r = max(cands, key=lambda c: c[0])
    mx = _to_mm(v, "mm" if key == "skew_max_mm" else "ps", f"{r.label} {key}", ps_per_mm, master)
    return max(mx, tgt), f"{r.label} {key}"


def skew_max_checked(cons, master, ps_per_mm=None):
    try:
        return skew_max_source(cons, master, ps_per_mm)
    except ValueError as e:
        return None, f"PAIR_DELAY_UNKNOWN ({e})"


STUB_MAX_DEFAULT = {"usb2": 3.5, "usb3": 3.5}       # Type-C R2.0 Table 3-4: a spur on a joined connector row; other families: not checked


def stub_max_mm(cons, master):
    """Longest branch stub (mm) a pair with more than two terminals may have, or None (not checked): `[pairs.F] stub_max_mm` (same
    cascade as skew_mm), else `[si] stub_max_mm_by_kind.<kind>`, else the built-in USB value."""
    from . import rules as RU, netclass as NC
    v, _ = RU.load(cons).resolve({"net": master}, "stub_max_mm")
    if v is not None:
        return float(v)
    kind = NC.pair_kind(master)
    by = (cons.get("si", {}) or {}).get("stub_max_mm_by_kind") or {}
    if kind in by:
        return float(by[kind])
    return STUB_MAX_DEFAULT.get(kind)


def skew_limit_checked(cons, master, ps_per_mm=None):
    """(limit mm or None, source). None = a ps limit with no known layer delay: report PAIR_DELAY_UNKNOWN and
    do not grade the pair. Never raises for that case."""
    try:
        return skew_limit_source(cons, master, ps_per_mm)
    except ValueError as e:
        return None, f"PAIR_DELAY_UNKNOWN ({e})"


def skew_limit_mm(cons, master, ps_per_mm=None):
    """SI-lite intra-pair skew limit (mm): see skew_limit_source."""
    return skew_limit_source(cons, master, ps_per_mm)[0]


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


def _rule_netclasses(board, cons, classes, patterns, log):
    """One RULE_<name> class per `[[rule]]` that wins width_mm or clearance_mm on a board net, pattern = the
    net name. Legacy-block winners are not emitted (their classes already exist above), so a file without
    `[[rule]]` entries writes exactly what it always did. Returns the number of classes added."""
    if not board or not any(isinstance(r, dict) for r in (cons or {}).get("rule") or []):
        return 0
    from . import rules as RU
    try:
        rs = RU.load(cons)
        nets, _fps = RU.board_objects(board)
    except (RU.RuleError, OSError, RuntimeError, ImportError) as e:
        log(f"  rule netclasses skipped ({e})")
        return 0
    # Group by the full set of resolved values (width, clearance, ...) so every net
    # gets exactly what the rules resolved for it, no more and no less.
    groups = {}
    for n in nets:
        obj = {"net": n["net"], "netclass": n.get("netclass")}
        win = {}
        for key in ("width_mm", "clearance_mm"):
            hits = rs.cascade(obj, key)
            if hits and not hits[0][0].legacy:
                win[key] = (hits[0][0], hits[0][1])
        if win:
            vals = {k: v for k, (_r, v) in win.items()}
            gkey = tuple(sorted(vals.items()))
            first = win.get("width_mm", next(iter(win.values())))[0]
            groups.setdefault(gkey, (first, vals, []))[2].append(n["net"])
    used = set()
    for rule, vals, names in groups.values():
        base = "RULE_" + re.sub(r"\W+", "_", rule.name or f"r{rule.order}").strip("_")
        name, i = base, 2
        while name in used:
            name, i = f"{base}_{i}", i + 1
        used.add(name)
        cls = {"name": name}
        if "clearance_mm" in vals:
            cls["clearance"] = float(vals["clearance_mm"])
        if "width_mm" in vals:
            cls["track_width"] = float(vals["width_mm"])
        classes.append(cls)
        patterns += [{"netclass": name, "pattern": nm} for nm in sorted(names)]
    return len(groups)


MANAGED_KEY = "fluxplace_managed"       # the explicit marker of a class fluxplace owns (never a name prefix: a user class named PWR_* is the user's)
GEOM_TOL_MM = 0.005


def _eff_class(classes, patterns, net):
    """The class that applies to `net`: the first netclass pattern that matches it (KiCad wildcard), else None (Default)."""
    import fnmatch
    for p in patterns:
        if fnmatch.fnmatchcase(net, p.get("pattern", "")):
            return next((c for c in classes if c.get("name") == p.get("netclass")), None)
    return None


def pair_legs_of(cons, board):
    """{leg net: family} for the classified pair legs of `board` (graph.diff_pairs / family tails: budget.pair_legs) under their longest
    [pairs.F] family.  {} when the board cannot be read."""
    from . import budget as B
    try:
        from . import routediag as RD
        names = sorted({q["net"] for q in RD.load(board)["pads"] if q["net"]})
    except Exception:                                                       # noqa: BLE001 - no board: no legs
        return {}
    fams = (cons or {}).get("pairs") or {}
    legs = B.pair_legs(names, cons)
    return {n: f for n in sorted(legs) for f in [B.family_of(fams, n, legs)] if f}


def _expected_geometry(cons, group, board):
    from . import zgeom as ZG
    z = int((cons.get("pairs", {}).get(group) or {}).get("impedance_diff", 100))
    og = ZG.outer_geometry(cons, group, board=board) if (cons.get("pairs", {}).get(group) or {}).get("impedance_diff") else None
    w, g = og or PAIR_GEOM_BY_Z.get(z, PAIR_GEOM_BY_Z[100])                # computed from the stackup; the constant is the fallback
    return z, w, g


def _build_netclasses(d, cons, log, board):
    """(classes, patterns, n_rule): the project's classes with the fluxplace-managed ones rebuilt.  User classes are kept whole (never
    deleted, never edited); managed classes set only width / gap (pairs, from the stackup) and width (rails), never clearance or via sizes,
    so DRC on unchanged copper cannot move.  A pair leg or rail a user class already covers is left to that class (netclass_mismatch
    reports it); the rest get an exact-name pattern (the old `*FAMILY_*` pattern doubled the family's trailing `_` and matched nothing)."""
    ns = d.setdefault("net_settings", {})
    old = ns.get("classes", [])
    drop = {c.get("name") for c in old if c.get(MANAGED_KEY)}
    classes = [c for c in old if not c.get(MANAGED_KEY)]
    patterns = [p for p in ns.get("netclass_patterns", []) if p.get("netclass") not in drop]
    taken = {c.get("name") for c in classes}

    def uniq(name):
        while name in taken:
            name += "_fx"
        taken.add(name)
        return name
    legs = pair_legs_of(cons, board) if board else {}
    for group in sorted((cons or {}).get("pairs", {})):
        nets = [n for n, f in legs.items() if f == group and _eff_class(classes, patterns, n) is None]
        if not nets:
            continue
        z, w, g = _expected_geometry(cons, group, board)
        name = uniq(f"DP_{group.rstrip('_')}_{z}R")
        classes.append({"name": name, "track_width": w, "diff_pair_width": w, "diff_pair_gap": g, MANAGED_KEY: True})
        patterns += [{"netclass": name, "pattern": n} for n in nets]
    for net, pw in sorted((cons or {}).get("power", {}).items()):
        ma = pw.get("max_current_ma")
        if not ma or pw.get("pour") or _eff_class(classes, patterns, net) is not None:
            continue
        name = uniq(f"PWR_{net.strip('+').replace('/', '_')}")
        classes.append({"name": name, "track_width": rail_width_mm(ma), MANAGED_KEY: True})
        patterns.append({"netclass": name, "pattern": net})
    n0 = len(classes)
    n_rule = _rule_netclasses(board, cons, classes, patterns, log)
    for c in classes[n0:]:
        c[MANAGED_KEY] = True                                               # the owner's [[rule]] results are written by us and rebuilt by us
    return classes, patterns, n_rule


def inject_netclasses(pro_path, cons, log=print, board=None):
    """Write the engineering constraints INTO the project file as KiCad net classes, so any ECAD parser (Quilter's 'ECAD PARSED
    CONSTRAINTS', the KiCad router, a human) sees the intent instead of guessing defaults.  Only classes carrying MANAGED_KEY are ours:
    they are rebuilt, everything else in the file is kept as it was.  Call it on a project file the caller owns (a stage's copy)."""
    import json
    d = json.load(open(pro_path))
    classes, patterns, n_rule = _build_netclasses(d, cons, log, board)
    ns = d.setdefault("net_settings", {})
    ns["classes"] = classes
    ns["netclass_patterns"] = patterns
    json.dump(d, open(pro_path, "w"), indent=2)
    log(f"  netclasses written: {sum(1 for c in classes if c.get(MANAGED_KEY))} managed "
        f"({sum(1 for c in classes if c.get('name', '').startswith('DP_') and c.get(MANAGED_KEY))} pair, "
        f"{sum(1 for c in classes if c.get('name', '').startswith('PWR_') and c.get(MANAGED_KEY))} rail, {n_rule} rule classes)")
    return len(classes)


def netclass_mismatch(pro_path, cons, board=None):
    """NETCLASS_MISMATCH rows, [] when the project carries the budgets.  Writes nothing.
      managed ...   a class fluxplace owns differs from what the constraints imply (missing, extra, other width / gap)
      foreign ...   the class that actually applies to a pair leg (first matching pattern, a user class) has another track width,
                    diff-pair width or gap than the stackup gives for the family: the router would draw the pair off its impedance."""
    import json
    d = json.load(open(pro_path))
    want_c, want_p, _ = _build_netclasses(json.loads(json.dumps(d)), cons, lambda *a, **k: None, board)
    ns = d.get("net_settings", {})
    have_c = {c.get("name"): c for c in ns.get("classes", []) if c.get(MANAGED_KEY)}
    want_m = {c["name"]: c for c in want_c if c.get(MANAGED_KEY)}
    out = [f"managed {n}" for n in sorted(set(have_c) | set(want_m))
           if n not in have_c or n not in want_m or any(have_c[n].get(k) != v for k, v in want_m[n].items())]
    key = lambda p: (p.get("netclass"), p.get("pattern"))                  # noqa: E731
    hp = {key(p) for p in ns.get("netclass_patterns", []) if p.get("netclass") in have_c}
    wp = {key(p) for p in want_p if p.get("netclass") in want_m}
    out += [f"managed pattern {a}={b}" for a, b in sorted(hp ^ wp)]
    classes, patterns = ns.get("classes", []), ns.get("netclass_patterns", [])
    for net, group in sorted(pair_legs_of(cons, board).items()) if board else []:
        c = _eff_class(classes, patterns, net)
        if c is None or c.get(MANAGED_KEY):
            continue
        z, w, g = _expected_geometry(cons, group, board)
        got = {"diff_pair_width": c.get("diff_pair_width", c.get("track_width")), "diff_pair_gap": c.get("diff_pair_gap")}
        bad = [f"{k} {v} != {e:g}" for k, v, e in (("width", got["diff_pair_width"], w), ("gap", got["diff_pair_gap"], g))
               if v is not None and abs(float(v) - e) > GEOM_TOL_MM]
        if bad:
            out.append(f"foreign {net} class {c.get('name')}: {', '.join(bad)} (stackup, {z} ohm)")
    return out


# ---------------------------------------------------------------------------
# Board coordinates in the constraints — ONE registry
# ---------------------------------------------------------------------------
# Every constraints key whose value is a position on the board (mm, board
# frame) is listed here, and nowhere else. `fit-sheet` (sheet.rewrite_constraints)
# shifts exactly these; `coord_items` reads them from a parsed file for the
# before/after self-check; `unregistered_coords` finds coordinate-looking keys
# the registry does not know, so a new coordinate key cannot ship without being
# shifted (the shift refuses to run). Path segments are table / key names (list
# indexes are not segments); `*` matches one segment.
#   kind "xy"  : the numbers alternate x, y, x, y ... (a point, a rect, a polygon)
#   kind "xy2" : only the first two numbers are a point (a `[lock.at]` row's third item is the side)
# Keys that look like coordinates but are not (zoning, [plan.neck], [silk.title]) carry
# none: they take refs, globs, or distances.
COORD_KEYS = (
    ("lock.at.*", "xy2", "[lock.at] REF = [x, y, side]"),
    ("keepout.rect", "xy", "[[keepout]] rect = [x0, y0, x1, y1]"),
    ("placement.keepout.*.rect", "xy", "[placement.keepout.NAME] rect"),
    ("placement.region.*.rect", "xy", "[placement.region.NAME] rect"),
    ("placement.no_tht.*.rect", "xy", "[placement.no_tht.NAME] rect"),
    ("placement.height_region.rect", "xy", "[[placement.height_region]] rect"),
    ("placement.group.*.centre", "xy", "[placement.group.NAME] centre = [x, y]"),
    ("intent.region.rect", "xy", "[[intent.region]] rect = [x0, y0, x1, y1]"),
    ("plan.pour.region", "xy", "[[plan.pour]] region = [x0, y0, x1, y1] | [[x, y], ...]"),
    ("thermal.*.rect", "xy", "[thermal.NAME] rect = [x0, y0, x1, y1]"),
    ("thermal.*.polygon", "xy", "[thermal.NAME] polygon = [[x, y], ...]"),
)

# key names that mean "a position" when they hold numbers; anything with one of
# these names that the registry does not cover is refused by the shift.
COORD_NAME_HINTS = frozenset({
    "rect", "centre", "center", "at", "pos", "position", "poly", "polygon", "pts", "points",
    "xy", "origin", "bbox", "box", "region", "coords", "corner", "corners", "outline", "area"})


def coord_kind(path):
    """The registry kind ("xy" | "xy2") of a key path (tuple of segments), or None."""
    for pat, kind, _doc in COORD_KEYS:
        ps = pat.split(".")
        if len(ps) == len(path) and all(p == "*" or p == s for p, s in zip(ps, path)):
            return kind
    return None


def _numbers(v):
    """Every number under a parsed value, document order (bools are not numbers)."""
    if isinstance(v, bool):
        return []
    if isinstance(v, (int, float)):
        return [float(v)]
    if isinstance(v, (list, tuple)):
        return [n for x in v for n in _numbers(x)]
    return []


def _walk(node, path=()):
    """(path, value) for every leaf-ish entry of a parsed TOML dict; lists of tables are walked
    without an index segment; a list of numbers/lists is one value."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, path + (str(k),))
    elif isinstance(node, list) and node and all(isinstance(x, dict) for x in node):
        for x in node:
            yield from _walk(x, path)
    else:
        yield path, node


def coord_items(cons):
    """[(dotted path, (x, y))] for every registered coordinate in a parsed constraints dict
    (one entry per point), in file order. The self-check compares two of these."""
    out = []
    for path, v in _walk(cons or {}):
        kind = coord_kind(path)
        if not kind:
            continue
        nums = _numbers(v)
        if kind == "xy2":
            nums = nums[:2]
        out += [(".".join(path), (nums[i], nums[i + 1])) for i in range(0, len(nums) - 1, 2)]
    return out


def unregistered_coords(cons):
    """[dotted path] of keys named like a coordinate that hold numbers but are not in COORD_KEYS."""
    out = []
    for path, v in _walk(cons or {}):
        if path and path[-1].lower() in COORD_NAME_HINTS and _numbers(v) and not coord_kind(path):
            out.append(".".join(path))
    return out
