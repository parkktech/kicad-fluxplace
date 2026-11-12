"""CLI bodies for `map`, `plan-route`, `plan-compare` (and the `place --objective plan` entry).  cli.py only
registers the parsers (`add_parsers`) and calls these thin functions."""
import json
import os
import sys


def _ctx(a):
    from . import constraints as C
    spec = json.load(open(a.spec)) if getattr(a, "spec", None) else None
    return C.load(getattr(a, "constraints", None)), spec


def _top(a, default=10):
    return int(getattr(a, "top", None) or default)


def _emit(a, verdict_line, counts_line, worst, report_path):
    """Compact output: verdict line, counts, worst N, report path (`-q` = verdict only, `--full` = all)."""
    print(verdict_line)
    if getattr(a, "quiet", False):
        return
    if counts_line:
        print(counts_line)
    n = len(worst) if getattr(a, "full_output", False) else min(len(worst), _top(a))
    if worst:
        print(f"worst {n} of {len(worst)}:")
        for w in worst[:n]:
            print(w)
    if report_path:
        print(f"full report: {report_path}" + ("" if getattr(a, "full_output", False) else "   --full for everything"))


# ------------------------------------------------------------------ map
def cmd_map(a):
    from . import fluxmap as FM, routediag as RD
    path = a.map or (FM.default_path(a.board) if a.board else None)
    if a.show or a.render:
        if not path or not os.path.exists(path):
            raise SystemExit(f"map: no map at {path}; run `fluxplace map BOARD --constraints C` first")
        m = FM.load(path)
        if a.show:
            print(FM.show(m, a.show, ref=a.ref, net=a.net, group=a.group))
        if a.render:
            for f in FM.render(m, a.render):
                print(f"map: rendered {f}")
        return
    if not a.board:
        raise SystemExit("map: BOARD required")
    cons, spec = _ctx(a)
    B = RD.load(a.board)
    state = None
    if getattr(a, "state", False):
        from . import planroute as PL
        o, g, r = PL.open_state(B, a.board)
        state = {"open": o, "routed": r}
    m = FM.build(B, cons, spec, a.board, state=state)
    out = a.out or FM.default_path(a.board)
    n = FM.save(m, out)
    gs = ", ".join(f"{g['name']} {len(g['members'])}" for g in m["groups"][:6])
    print(f"map: {len(m['parts'])} parts, {len(m['nets'])} nets, {len(m['groups'])} groups, {len(m['relations'])} relations, "
          f"{len(m['board']['keepouts'])} keep-outs; wrote {out} ({n / 1024:.0f} kB)")
    print(f"map: read slices with `fluxplace map --show SECTION [--ref R|--net N|--group G] --map {out}`; "
          f"sections: {', '.join(FM.SECTIONS)}")


# ------------------------------------------------------------------ plan-route
def cmd_plan_route(a):
    from . import fluxmap as FM, routediag as RD, planroute as PL
    cons, spec = _ctx(a)
    B = RD.load(a.board)
    if getattr(a, "calibrate", None):
        return _calibrate(a, cons, spec)
    R = PL.run(B, cons, spec, ignore_routing=a.ignore_routing, cell=a.cell, board_path=a.board,
               open_method="pcbnew" if getattr(a, "pcbnew", False) else "text",
               log=(print if getattr(a, "verbose", False) else None))
    state = R["state"]
    m = FM.build(B, cons, spec, a.board, cls=R["cls"], state={"open": state["open"], "routed": state["routed"]})
    m.update(PL.plan_section(R))
    mp = a.map or FM.default_path(a.board)
    size = FM.save(m, mp)
    rep = os.path.splitext(a.board)[0] + ".plan-route"
    ranked = PL.rank(R["findings"])
    with open(rep + ".txt", "w") as fh:
        fh.write(PL.format_text(R, top=len(ranked), title=a.board) + "\n")
    with open(rep + ".json", "w") as fh:
        json.dump({"verdict": PL.verdict(R), "findings": ranked}, fh, indent=1, default=str)
    v = PL.verdict(R)
    if a.json:
        json.dump({"verdict": v, "findings": ranked}, open(a.json, "w"), indent=1, default=str)
    counts = "counts: " + (", ".join(f"{k} {c}" for k, c in sorted(v["counts"].items(), key=lambda kv: -kv[1])) or "none")
    comp = v["components"]
    line = (f"plan-route: {v['result']} (exit {1 if v['result'] == 'FAIL' else 0}) - PLAN_SCORE {v['score']:g} conf {v['confidence']} [{v['mode']}] "
            f"sealed {comp['sealed']}, no-via-site {comp['no_via_site']}, overflow {comp['overflow']:g}, "
            f"over-budget {comp['over_budget_mm']:g} mm; {v['nets_planned']} nets, {v['timing']['total_s']:g} s; map {mp} ({size / 1024:.0f} kB)")
    worst = [f"  {x['code']:20s} {x['msg']}\n      -> {x.get('suggestion', '')}" for x in ranked]
    _emit(a, line, counts, worst, rep + ".txt")
    if getattr(a, "variant", False):
        rows = PL.run_variants(B, cons, spec, a.ignore_routing, cell=a.cell, board_path=a.board, base=R)
        print(f"{'variant':10s} {'layers':>6s} {'score':>8s} {'conf':>5s} {'sealed':>6s} {'overflow':>8s} {'vias':>5s} {'copper_mm':>9s}")
        for r in rows:
            print(f"{r['variant']:10s} {r['layers']:6d} {r['score']:8.1f} {r['confidence']:5d} {r['sealed']:6d} {r['overflow']:8.1f} {r['vias']:5d} {r['copper_mm']:9.1f}")
    if getattr(a, "swaps", False):
        sw, base = PL.swap_candidates(B, cons, spec)
        print(f"swap proposals (schematic change, not applied; base PLAN_SCORE {base:g}): {len(sw)} evaluated")
        for r in sw[:10]:
            print(f"  {r['group']}: {r['a']} <-> {r['b']} ({r['nets'][0]} / {r['nets'][1]}) gain {r['gain']:+g}")
    if v["result"] == "FAIL":
        raise SystemExit(1)


def _calibrate(a, cons, spec):
    from . import fluxmap as FM, routediag as RD, planroute as PL
    B = RD.load(a.calibrate)
    c = PL.calibrate(B, cons, spec, a.calibrate)
    R = c.pop("result")
    mp = FM.default_path(a.calibrate)
    m = FM.build(B, cons, spec, a.calibrate, cls=R["cls"])
    m.update(PL.plan_section(R))
    m["calibration"] = {k: c[k] for k in ("hit", "miss", "false_alarm", "best", "planned")}
    m["calibration"]["errors"] = [{"net": n, "why": "open on the routed board, plan predicted clear"} for n in c["miss"]]
    FM.save(m, mp)
    print(f"calibrate {a.calibrate}: {len(c['actual_open'])} nets open; plan flagged {len(c['hit'])} (hit), missed {len(c['miss'])}, "
          f"flagged-but-routed {len(c['false_alarm'])} (of {c['planned']} planned); PLAN_SCORE {c['score']:g}")
    print(f"  missed (planner errors, logged in {mp}): {', '.join(c['miss'][:12])}")
    print("  capacity constants: share of real cell-directions over the modelled capacity")
    for u in (0.6, 0.8, 1.0):
        print("   util %.1f: " % u + "  ".join(f"jog {g['jog_frac']:.1f} {100 * g['over_share']:.1f}%" for g in c["grid"] if g["util"] == u))
    b = c["best"]
    print(f"  tightest constants holding 95 %: util {b['util']}, jog_frac {b['jog_frac']} ({100 * b['over_share']:.1f}% over)")
    if getattr(a, "write_constants", False):
        if not a.constraints:
            raise SystemExit("--write-constants needs --constraints")
        PL.write_constants(a.constraints, b)
        print(f"  wrote util/jog_frac to [plan] in {a.constraints}")


# ------------------------------------------------------------------ plan-compare
def cmd_plan_compare(a):
    from . import fluxmap as FM
    A, Bm = FM.load(a.map_a), FM.load(a.map_b)
    va, vb = A.get("verdict") or {}, Bm.get("verdict") or {}
    if not va or not vb:
        raise SystemExit("plan-compare: both maps need a verdict (run plan-route first)")
    rows = [("PLAN_SCORE", va["score"], vb["score"]), ("result", va["result"], vb["result"]), ("mode", va["mode"], vb["mode"])]
    for k in sorted(set(va["components"]) | set(vb["components"])):
        rows.append((k, va["components"].get(k, 0), vb["components"].get(k, 0)))
    for k in sorted(set(va["counts"]) | set(vb["counts"])):
        rows.append((k, va["counts"].get(k, 0), vb["counts"].get(k, 0)))
    na, nb = os.path.basename(a.map_a)[:34], os.path.basename(a.map_b)[:34]
    print(f"{'':24s} {na:>34s} {nb:>34s}   delta")
    for k, x, y in rows:
        d = ""
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            d = f"{y - x:+.1f}".rstrip("0").rstrip(".") if y != x else "="
        print(f"{k:24s} {x!s:>34s} {y!s:>34s}   {d}")
    better = vb["score"] < va["score"]
    print(f"plan-compare: {'B' if better else ('A' if va['score'] < vb['score'] else 'neither')} is better "
          f"(PLAN_SCORE {va['score']:g} vs {vb['score']:g})")
    pa = {(f["code"], f.get("part") or f.get("net") or tuple(f.get("at", ()))) for f in va.get("findings", [])}
    pb = {(f["code"], f.get("part") or f.get("net") or tuple(f.get("at", ()))) for f in vb.get("findings", [])}
    print(f"findings only in A: {len(pa - pb)}, only in B: {len(pb - pa)}, shared: {len(pa & pb)}")


def cmd_plan_options(a):
    from . import planopts as PO, routediag as RD, planroute as PL
    cons, spec = _ctx(a)
    out = a.out_dir or (os.path.splitext(a.board)[0] + ".plan-options")
    calib = None
    if a.calibrate_board:
        Bc = RD.load(a.calibrate_board)
        calib = PL.calibrate(Bc, cons, spec, a.calibrate_board, grid_scan=False)
        calib.pop("result", None)
        calib["placement_rate"], calib["placement_failed"], calib["placement_hit"] = PL.calibrate_placement(Bc, cons, spec)
    r = PO.search(a.board, cons, spec, out, max_grow=a.max_grow, workers=a.workers, place_evals=a.place_evals,
                  log=(print if a.verbose else (lambda m: None)), calib=calib)
    if a.json:
        json.dump({k: v for k, v in r.items() if k != "baseline"}, open(a.json, "w"), indent=1, default=str)
    if r.get("caveat"):
        print("WARNING " + r["caveat"])
    print(r["prompt"])
    if r.get("table"):
        print(r["table"])
    print(f"plan-options: {'fits as is' if r['fits'] else str(len(r['ok'])) + ' recommended option(s), ' + str(len(r['sub'])) + ' need an owner rule change, ' + str(len(r['miss'])) + ' fail'}; "
          f"{r['seconds']:g} s; candidates in {out}")


def after_plan_place(a, board_in, board_out, cons, spec, st):
    """`place --objective plan` epilogue: confirm with a full plan-route, write the map and the move list."""
    from . import fluxmap as FM, routediag as RD, planroute as PL
    B = RD.load(board_out)
    R = PL.run(B, cons, spec, ignore_routing=True)
    m = FM.build(B, cons, spec, board_out, cls=R["cls"])
    m.update(PL.plan_section(R))
    mp = FM.default_path(board_out)
    FM.save(m, mp)
    v = PL.verdict(R)
    print(f"plan: {'REVERTED to the input (the full plan after the moves was worse)' if st.get('reverted_to_input') else 'confirmed by a full plan-route'}: "
          f"PLAN_SCORE {st['score_before']:.1f} -> {v['score']:.1f} (incremental {st['score_after']:.1f}); sealed {st['components_before']['sealed']} -> {v['components']['sealed']}, "
          f"overflow {st['components_before']['overflow']:g} -> {v['components']['overflow']:g}; map {mp}")
    log = st.get("log", [])
    print(f"plan: {len(log)} move(s) kept; move list (ref, old -> new, reason):")
    for e in log[:40]:
        for r in e["refs"][:3]:
            fx, to = e["from"][r], e["to"][r]
            print(f"  {r:6s} ({fx[0]:.2f}, {fx[1]:.2f}, {fx[2]:.0f}) -> ({to[0]:.2f}, {to[1]:.2f}, {to[2]:.0f})  {e['reason']}"
                  f"  [{e['before']:.1f} -> {e['after']:.1f}]")
    with open(os.path.splitext(board_out)[0] + ".planmoves.json", "w") as fh:
        json.dump({"score_before": st["score_before"], "score_confirmed": v["score"], "moves": log}, fh, indent=1)


# ------------------------------------------------------------------ parsers
def add_parsers(sub):
    def common(p):
        p.add_argument("--full", dest="full_output", action="store_true", help="print everything")
        p.add_argument("-q", "--quiet", dest="quiet", action="store_true", help="verdict line only")
        p.add_argument("--top", dest="top", type=int, default=None, help="worst N findings (default 10)")

    pm = sub.add_parser("map", help="meta map: compact JSON of parts, groups, relations, nets, escapes, grid; "
                                    "--show/--render read it back")
    pm.add_argument("board", nargs="?", default=None)
    pm.add_argument("--constraints", default=None)
    pm.add_argument("--spec", default=None)
    pm.add_argument("--out", default=None, help="map file (default <board>.fluxmap.json)")
    pm.add_argument("--map", default=None, help="existing map for --show/--render")
    pm.add_argument("--state", action="store_true", help="include routed/open state (text connectivity)")
    pm.add_argument("--show", default=None, metavar="SECTION", help="print one slice of the map")
    pm.add_argument("--ref", default=None)
    pm.add_argument("--net", default=None)
    pm.add_argument("--group", default=None)
    pm.add_argument("--render", default=None, metavar="OUT.png|OUT.svg", help="draw sides F, B and inner-layer demand")
    pm.set_defaults(fn=cmd_map)

    pp = sub.add_parser("plan-route", help="escape planner + global router on the placement: PLAN_SCORE and findings; "
                                           "exit 1 on sealed pins or overflow")
    pp.add_argument("board")
    pp.add_argument("--constraints", default=None)
    pp.add_argument("--spec", default=None)
    pp.add_argument("--ignore-routing", action="store_true", help="judge the placement only: all tracks and vias absent")
    pp.add_argument("--cell", type=float, default=None, help="grid cell mm (default 2.0)")
    pp.add_argument("--map", default=None, help="map file to write (default <board>.fluxmap.json)")
    pp.add_argument("--json", default=None, help="also write verdict + findings here")
    pp.add_argument("--pcbnew", action="store_true", help="open/routed state from pcbnew connectivity (zones filled)")
    pp.add_argument("--verbose", action="store_true")
    pp.add_argument("--variant", action="store_true", help="also run alternative layer roles (plane3v3, 8layer) and print a table")
    pp.add_argument("--swaps", action="store_true", help="list `[swap.group.*]` pad swaps that lower PLAN_SCORE (schematic change)")
    pp.add_argument("--calibrate", default=None, metavar="ROUTED_BOARD",
                    help="predicted vs actual open nets and capacity constants on a routed board")
    pp.add_argument("--write-constants", action="store_true", help="with --calibrate: write util/jog_frac to [plan] in --constraints")
    common(pp)
    pp.set_defaults(fn=cmd_plan_route)

    po = sub.add_parser("plan-options", help="what-if recommender: the smallest board/rule change that makes plan-route pass")
    po.add_argument("board")
    po.add_argument("--constraints", default=None)
    po.add_argument("--spec", default=None)
    po.add_argument("--max-grow", type=int, default=6, help="largest edge growth tried, mm (default 6)")
    po.add_argument("--out-dir", default=None, help="candidate boards/maps/constraints (default <board>.plan-options/)")
    po.add_argument("--place-evals", type=int, default=120, help="plan evaluations of --objective plan placement per option (deterministic bound)")
    po.add_argument("--workers", type=int, default=None)
    po.add_argument("--calibrate-board", default=None, help="routed board used to judge whether the verdict is proven")
    po.add_argument("--json", default=None)
    po.add_argument("--verbose", action="store_true")
    po.set_defaults(fn=cmd_plan_options)

    pc = sub.add_parser("plan-compare", help="two maps' plan verdicts side by side")
    pc.add_argument("map_a")
    pc.add_argument("map_b")
    pc.set_defaults(fn=cmd_plan_compare)
