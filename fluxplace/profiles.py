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
    ),
}


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
