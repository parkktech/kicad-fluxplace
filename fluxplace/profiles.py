"""Fabricator constraint profiles — the 'Fabricator Constraints' stage.

A profile is the fab house's process floor: the finest copper the pipeline is
allowed to emit. It feeds three places:
  - auto: the step-down FLOOR (never neck a net below what the fab can make)
    and the router/fanout via geometry
  - the fab gate: scan the finished board's finest track/clearance/via against
    the profile and put the verdict in the MANIFEST (REVIEW if below floor)
  - the manifest: record WHICH process the package assumed, so the engineer
    orders the right service tier

Values are the published capabilities of the named service, conservative side.
"""

PROFILES = {
    # JLCPCB standard multilayer service (their 'standard' tier)
    "jlcpcb": dict(
        track_min=0.10, clearance_min=0.10,     # 3.5 mil class
        via_dia_min=0.45, via_drill_min=0.30,
        floor=0.10,                             # step-down never goes below
        route_via=(0.6, 0.3),                   # bulk routing via (dia, drill)
        fanout_via=(0.45, 0.30),                # escape via (dia, drill)
        edge_clearance=0.30,
        hole_clearance=0.20, hole_to_hole=0.25,
    ),
    # JLCPCB advanced tier: 3.5 mil copper (0.0889mm — routers emit the exact
    # mil value, so the min carries margin below 0.09), 0.25/0.15 vias
    "jlcpcb-advanced": dict(
        track_min=0.088, clearance_min=0.088,
        via_dia_min=0.25, via_drill_min=0.15,
        floor=0.09,
        route_via=(0.45, 0.25),
        fanout_via=(0.30, 0.15),
        edge_clearance=0.30,
        hole_clearance=0.20, hole_to_hole=0.25,
        # 100R differential geometry on the JLC 4-layer 7628 reference stackup
        # (Simbeor-computed: 0.173mm trace / 0.107mm gap)
        pair_geom=(0.173, 0.107),
    ),
    # generic conservative prototype fab (2-6 layer quick-turn anywhere)
    "proto": dict(
        track_min=0.15, clearance_min=0.15,
        via_dia_min=0.60, via_drill_min=0.30,
        floor=0.15,
        route_via=(0.6, 0.3),
        fanout_via=(0.6, 0.3),
        edge_clearance=0.50,
        hole_clearance=0.25, hole_to_hole=0.50,
    ),
}


def apply_board_limits(board, prof, pcbnew):
    """Write the profile's process floor INTO the board's setup constraints
    and the Default netclass clearance. Without this every sub-netclass
    track/via the pipeline legitimately emits is judged illegal — measured:
    ~1300 of dig's 1580 / CM5's 1646 violations were board-setup constraint
    classes (track_width, via_diameter, drill_out_of_range, annular_width,
    clearance at 0.2 netclass) on copper the profile explicitly allows."""
    ds = board.GetDesignSettings()
    ds.m_TrackMinWidth = int(prof["track_min"] * 1e6)
    ds.m_ViasMinSize = int(prof["via_dia_min"] * 1e6)
    ds.m_MinThroughDrill = int(prof["via_drill_min"] * 1e6)
    ds.m_ViasMinAnnularWidth = int(
        (prof["via_dia_min"] - prof["via_drill_min"]) / 2 * 1e6)
    ds.m_HoleClearance = int(prof["hole_clearance"] * 1e6)
    ds.m_HoleToHoleMin = int(prof["hole_to_hole"] * 1e6)
    ds.m_CopperEdgeClearance = int(prof["edge_clearance"] * 1e6)
    for _name, nc in board.GetAllNetClasses().items():
        if nc.GetClearance() > int(prof["clearance_min"] * 1e6):
            nc.SetClearance(int(prof["clearance_min"] * 1e6))
    return prof


def get(name):
    if name not in PROFILES:
        raise KeyError(f"unknown fab profile {name!r} — have {sorted(PROFILES)}")
    return dict(PROFILES[name])


def check_board(board_path, profile, pcbnew=None):
    """Scan the finished board's finest emitted copper against the profile floor.
    Returns [(level, code, msg)] — FAIL when something was drawn finer than the
    fab can manufacture (the package would come back scrapped or modified)."""
    if pcbnew is None:
        import pcbnew
    b = pcbnew.LoadBoard(board_path)
    p = profile
    out = []
    tmin = vmin = dmin = None
    for t in b.GetTracks():
        cls = t.GetClass()
        if cls == "PCB_VIA":
            w = t.GetWidth() / 1e6
            d = t.GetDrillValue() / 1e6
            vmin = w if vmin is None else min(vmin, w)
            dmin = d if dmin is None else min(dmin, d)
        elif cls == "PCB_TRACK":
            w = t.GetWidth() / 1e6
            tmin = w if tmin is None else min(tmin, w)
    if tmin is not None and tmin < p["track_min"] - 1e-6:
        out.append(("FAIL", "TRACK_BELOW_FAB",
                    f"finest track {tmin:.3f}mm < profile min {p['track_min']}mm"))
    if vmin is not None and vmin < p["via_dia_min"] - 1e-6:
        out.append(("FAIL", "VIA_BELOW_FAB",
                    f"finest via {vmin:.3f}mm < profile min {p['via_dia_min']}mm"))
    if dmin is not None and dmin < p["via_drill_min"] - 1e-6:
        out.append(("FAIL", "DRILL_BELOW_FAB",
                    f"finest drill {dmin:.3f}mm < profile min {p['via_drill_min']}mm"))
    summary = (f"finest emitted: track {tmin if tmin is not None else '-'} / "
               f"via {vmin if vmin is not None else '-'} / "
               f"drill {dmin if dmin is not None else '-'} (mm)")
    return out, summary


# Human ordering facts per profile: the service to buy and the stackup preset
# string external tools (Quilter, fab order forms) present. {n} = copper layers.
ORDER_INFO = {
    "jlcpcb": dict(
        service="JLCPCB standard multilayer (0.1mm / ~4mil class)",
        pick="JLCPCB {n}-Layer (with power plane) | 4 mil / 4 mil",
        stackup="JLC7628 reference stackup, 1.6mm"),
    "jlcpcb-advanced": dict(
        service="JLCPCB advanced / controlled impedance (3.5 mil class)",
        pick="JLCPCB {n}-Layer (with power plane) | 3.5 mil / 3.5 mil",
        stackup="JLC7628 reference stackup, 1.6mm (impedance-controlled order)"),
    "proto": dict(
        service="conservative quick-turn prototype (0.15mm / 6mil class)",
        pick="{n}-Layer | 6 mil / 6 mil",
        stackup="fab default stackup"),
}


def order_guidance(profile_name, copper_layers, signal_layers, size_mm, cons):
    """The 'what do I pick' block: printed when a board finishes preparing and
    appended to the fab MANIFEST — service tier, the stackup preset string an
    upload tool will show, impedance/skew answers, and rail currents, all from
    the profile + engineering constraints (never guessed at order time)."""
    info = ORDER_INFO.get(profile_name, ORDER_INFO["proto"])
    lines = ["", "ORDER / UPLOAD GUIDANCE — what to pick",
             f"  fab service : {info['service']}   [profile {profile_name}]",
             f"  board       : {copper_layers} copper layers "
             f"({signal_layers} signal + planes), "
             f"{size_mm[0]:.0f} x {size_mm[1]:.0f} mm",
             f"  stackup pick: \"{info['pick'].format(n=copper_layers)}\"",
             f"  stackup     : {info['stackup']}"]
    pairs = (cons or {}).get("pairs", {})
    if pairs:
        by = {}
        for name, p in sorted(pairs.items()):
            key = (p.get("impedance_diff"), p.get("skew_mm"))
            by.setdefault(key, []).append(name)
        for (z, skew), names in sorted(by.items(), key=lambda kv: kv[0][0] or 0):
            lines.append(f"  impedance   : {', '.join(names)} = {z} ohm diff"
                         + (f", skew {skew} mm" if skew else ""))
    power = (cons or {}).get("power", {})
    if power:
        rails = sorted(power.items(),
                       key=lambda kv: -(kv[1].get("max_current_ma") or 0))
        lines.append("  rail currents: " + ", ".join(
            f"{n} {p.get('max_current_ma')}mA" + (" (plane)" if p.get("pour") else "")
            for n, p in rails if p.get("max_current_ma")))
    lines.append("  upload set  : pcb + pro + sch only "
                 "(fab --upload-out; never .kicad_prl/.kicad_dru)")
    return "\n".join(lines)
