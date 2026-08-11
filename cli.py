#!/usr/bin/env python3
"""fluxplace CLI — headless signal-flow placement for KiCad boards.

  analyze  : print the communication map (hub, branches, forks, lint)
  place    : re-place components by topology, save the board
  eval     : report placement quality (weighted wirelength, overlaps, board size)

Run with KiCad's python so pcbnew is importable, e.g.:
  PYTHONPATH=/usr/lib/python3/dist-packages /usr/bin/python3 cli.py place \
      --board board.kicad_pcb --strategy flux --rotate ortho --out out.kicad_pcb
"""
import argparse, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fluxplace import graph as G, topology as T, placement as P


def _load(board_path):
    from fluxplace import kicad_io as IO
    board = IO.load(board_path)
    parts, nets = IO.read_board(board)
    return board, parts, nets, IO


def cmd_analyze(a):
    _, parts, nets, _ = _load(a.board)
    cg = G.build(parts, nets, a.big_fanout)
    topo = T.analyze(cg, prefer_hub=a.hub)
    print(T.summary(topo))


def cmd_place(a):
    board, parts, nets, IO = _load(a.board)
    cg = G.build(parts, nets, a.big_fanout)
    topo = T.analyze(cg, prefer_hub=a.hub)
    center = IO.board_center(board) if a.center_board else (0.0, 0.0)
    rep = None
    if a.strategy == "build":
        from fluxplace import route as R
        pos, rot, rep = P.place_routed(parts, cg, topo, center=center, pad=a.pad,
                                       seeds=a.seeds)
        print(R.summary(rep))
    else:
        pos, rot = P.place(parts, cg, topo, strategy=a.strategy, rotate=a.rotate,
                           center=center, pad=a.pad, iters=a.iters)
    before = P.hpwl(parts, cg, {r: (parts[r]["x"], parts[r]["y"]) for r in parts})
    after = P.hpwl(parts, cg, pos)
    IO.apply_orientations(board, rot, skip_locked=not a.move_locked)   # rotate first
    IO.apply_positions(board, pos, parts, skip_locked=not a.move_locked)  # then center
    dims = None
    if not a.no_outline:
        xs0 = []; ys0 = []; xs1 = []; ys1 = []
        for r, (x, y) in pos.items():
            w, h = P.eff_size(parts, r, rot.get(r, 0.0), 0.0)
            xs0.append(x - w / 2); ys0.append(y - h / 2)
            xs1.append(x + w / 2); ys1.append(y + h / 2)
        dims = IO.shrinkwrap_outline(board, min(xs0), min(ys0), max(xs1), max(ys1),
                                     margin=a.margin)
    if rep is not None and getattr(a, "guides", False):
        n = IO.emit_route_guides(board, rep)
        print(f"route guides: {n} corridor segments on Eco1.User (group 'fluxplace-guides')")
    out = a.out or a.board
    IO.save(board, out)
    ov = P.count_overlaps(parts, pos, 0.2, angles=rot)
    print(f"placed {len(pos)} parts [{a.strategy}, rotate={a.rotate}]  "
          f"HPWL {before:.0f} -> {after:.0f} mm ({100*(before-after)/before:+.0f}%)  "
          f"overlaps={ov}" + (f"  board={dims[0]:.0f}x{dims[1]:.0f}mm" if dims else ""))
    print(f"saved: {out}")


def cmd_plan(a):
    from fluxplace import report
    _, parts, nets, _ = _load(a.board)
    cg = G.build(parts, nets, a.big_fanout)
    topo = T.analyze(cg, prefer_hub=a.hub)
    md = report.plan_markdown(parts, cg, topo, strategy=a.strategy, rotate=a.rotate)
    if a.out:
        open(a.out, "w").write(md)
        print(f"wrote plan: {a.out}")
    else:
        print(md)


def cmd_gather(a):
    from fluxplace import report
    import json
    _, parts, nets, _ = _load(a.board)
    cg = G.build(parts, nets, a.big_fanout)
    topo = T.analyze(cg, prefer_hub=a.hub)
    print(json.dumps(report.gather(parts, cg, topo), indent=2))


def cmd_eval(a):
    board, parts, nets, IO = _load(a.board)
    cg = G.build(parts, nets, a.big_fanout)
    pos = {r: (parts[r]["x"], parts[r]["y"]) for r in parts}
    xs = [parts[r]["x"] for r in parts]; ys = [parts[r]["y"] for r in parts]
    print(f"parts={len(parts)}  weighted HPWL={P.hpwl(parts, cg, pos):.0f} mm  "
          f"extent={max(xs)-min(xs):.0f} x {max(ys)-min(ys):.0f} mm")
    ov = P.count_overlaps(parts, pos, a.pad)
    print(f"overlapping pairs (pad {a.pad}mm): {ov}")


def cmd_route(a):
    from fluxplace import route as R
    board, parts, nets, IO = _load(a.board)
    cg = G.build(parts, nets, a.big_fanout)
    pos = {r: (parts[r]["x"], parts[r]["y"]) for r in parts}
    rep = R.score(parts, pos, cg)
    print(R.summary(rep))
    if a.guides:
        n = IO.emit_route_guides(board, rep)
        IO.save(board, a.out or a.board)
        print(f"route guides: {n} corridor segments on Eco1.User -> {a.out or a.board}")


def cmd_calibrate(a):
    """Ground-truth the gate against a real autorouter (freerouting).
    Exports Specctra DSN; runs freerouting if a jar is available (java -jar);
    or parses an existing .ses produced elsewhere. Reports agreement."""
    import os, subprocess, re
    from fluxplace import route as R
    board, parts, nets, IO = _load(a.board)
    cg = G.build(parts, nets, a.big_fanout)
    pos = {r: (parts[r]["x"], parts[r]["y"]) for r in parts}
    rep = R.score(parts, pos, cg)
    print("gate: " + ("ROUTABLE (overflow 0)" if rep["overflow"] == 0
                      else f"CONGESTED (overflow {rep['overflow']})"))

    ses = a.ses
    if not ses:
        dsn = os.path.splitext(a.board)[0] + ".dsn"
        if not IO.export_dsn(board, dsn):
            print("DSN export failed")
            return
        print(f"exported: {dsn}")
        jar = a.jar or os.environ.get("FREEROUTING_JAR")
        if not jar:
            for cand in (os.path.expanduser("~/freerouting.jar"),
                         "/opt/freerouting/freerouting.jar"):
                if os.path.exists(cand):
                    jar = cand
                    break
        if not jar:
            print("no freerouting jar found — run it elsewhere and re-invoke with --ses:\n"
                  "  java -jar freerouting.jar -de board.dsn -do board.ses -mp 20\n"
                  "  (https://github.com/freerouting/freerouting/releases)")
            return
        ses = os.path.splitext(a.board)[0] + ".ses"
        print(f"running freerouting ({jar})...")
        try:
            subprocess.run(["java", "-jar", jar, "-de", dsn, "-do", ses,
                            "-mp", str(a.passes)], timeout=1800, check=True,
                           capture_output=True)
        except Exception as e:
            print(f"freerouting failed: {e}")
            return

    txt = open(ses, errors="ignore").read()
    import re as _re
    ses_nets = set(_re.findall(r'\(net\s+"?([^\s")]+)', txt))
    vias = txt.count("(via")
    wires = txt.count("(wire")
    # compare on OUR scope only: freerouting had to route plane nets as traces
    # (no zones in the DSN), which the gate legitimately excludes
    routable = set(cg.signal_nets) | set(cg.power_traces)
    got = {n for n in ses_nets if n in routable or n.replace('"', "") in routable}
    missing = sorted(routable - got)
    print(f"freerouting session: {wires} wires, {vias} vias; "
          f"{len(got)}/{len(routable)} gate-scope nets routed "
          f"(+{len(ses_nets) - len(got)} plane/other nets)")
    if missing and len(missing) <= 12:
        print("gate-scope nets NOT in session: " + ", ".join(missing))
    if rep["overflow"] == 0 and len(got) >= len(routable) * 0.98:
        print("AGREEMENT: gate said routable, freerouting closed the gate scope — calibrated OK")
    elif rep["overflow"] > 0 and len(got) < len(routable):
        print("AGREEMENT: both see congestion — inspect gate hotspots for where")
    else:
        print("PARTIAL/DISAGREEMENT: raise --passes (freerouting may have stopped early), "
              "or tune Grid pitch/util/blockage; the gate should sit slightly "
              "conservative of freerouting")


def cmd_tournament(a):
    """Placement tournament with freerouting as the fitness function."""
    import os
    from fluxplace import tournament as TN
    jar = a.jar or os.environ.get("FREEROUTING_JAR")
    if not jar or not os.path.exists(jar):
        print("need --jar or $FREEROUTING_JAR (freerouting 2.2.4+)")
        return
    results, winner = TN.run(a.board, jar, a.workdir, passes=a.passes, jobs=a.jobs,
                             resume=a.resume, oit=a.oit)
    if winner and a.apply_winner:
        ok, out = TN.import_winner(a.workdir, winner["idx"])
        print(f"winner copper imported: {out} (ok={ok})")


def cmd_fab(a):
    """Emit a build-quality manufacturing package (gerbers/drill/place/DRC) for review."""
    from fluxplace import fab
    res = fab.emit(a.board, a.out, kicad_cli=a.kicad_cli)
    print(f"DRC {res['drc']}; package at {res['out']}")


_POWER_HEAVY = ("VBATT", "VBAT_IN", "VIN", "12V", "24V", "28V", "30V", "_SW")
_POWER_ANY = ("VCC", "VDD", "PWR", "5V", "3V3", "1V8", "VBUS", "AVDD", "RAIL")


def _classify_power(nets, plane_nets, big_fanout):
    """name/fanout heuristic -> {netname: trace_width_mm} for the router's
    route-power-first-and-wide discipline."""
    widths = {}
    for n, refs in nets.items():
        if n in plane_nets:
            continue
        u = n.upper()
        if any(k in u for k in _POWER_HEAVY):
            widths[n] = 1.0
        elif u.startswith("+") or any(k in u for k in _POWER_ANY):
            widths[n] = 0.8 if ("5V" in u or "VBUS" in u) else 0.5
        elif len(refs) >= big_fanout:
            widths[n] = 0.5
    return widths


def _legalize_bboxes(parts, pos, rot, max_pass=4, gap=0.3):
    """Post-placement insurance: nudge the smaller of any two overlapping part
    bboxes apart (builder collisions have slipped through on keepout-bearing
    footprints — an overlap here is a guaranteed short or courtyard DRC)."""
    import math
    moved = 0
    for _ in range(max_pass):
        dirty = False
        refs = list(pos)
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                r1, r2 = refs[i], refs[j]
                w1, h1 = P.eff_size(parts, r1, rot.get(r1, 0.0), gap)
                w2, h2 = P.eff_size(parts, r2, rot.get(r2, 0.0), gap)
                x1, y1 = pos[r1]; x2, y2 = pos[r2]
                ox = (w1 + w2) / 2 - abs(x1 - x2)
                oy = (h1 + h2) / 2 - abs(y1 - y2)
                if ox <= 0 or oy <= 0:
                    continue
                l1 = parts[r1].get("locked"); l2 = parts[r2].get("locked")
                if l1 and l2:
                    continue          # both are hard anchors — nothing to nudge
                if l1 or l2:
                    small = r2 if l1 else r1   # only the unlocked one may move
                else:
                    small = r1 if w1 * h1 <= w2 * h2 else r2
                other = r2 if small == r1 else r1
                sx, sy = pos[small]; bx, by = pos[other]
                if ox < oy:
                    sx += (ox + 0.05) * (1 if sx >= bx else -1)
                else:
                    sy += (oy + 0.05) * (1 if sy >= by else -1)
                pos[small] = (sx, sy)
                moved += 1; dirty = True
        if not dirty:
            break
    return moved


def cmd_auto(a):
    """The 'magic' endpoint: board in -> PLACE -> OUTLINE -> PLANES -> ROUTE ->
    PLANE-FINALIZE -> DFM -> FAB. Each stage logged; router pluggable (KRT)."""
    import os, subprocess, time, json
    from fluxplace import fab, planes
    import pcbnew
    os.makedirs(a.out, exist_ok=True)
    placed = os.path.join(a.out, "placed.kicad_pcb")
    routed = os.path.join(a.out, "routed.kicad_pcb")
    finald = os.path.join(a.out, "final.kicad_pcb")
    NSTAGE = 6

    # ---- [1] PLACE (route-aware, escape-aware) + overlap legalize ------------------
    board, parts, nets, IO = _load(a.board)
    # phantom obstacles: rigid module shadows / enclosure bosses the placer must
    # respect but that have no footprint (e.g. the body of a plug-on SoM whose
    # connectors ARE on the board). Locked phantom parts, stripped before write-back.
    phantoms = []
    for i, spec in enumerate(a.obstacle or []):
        fields = spec.split(":")
        try:
            ox, oy, ow, oh = [float(v) for v in fields[:4]]
        except ValueError:
            print(f"    ! bad --obstacle '{spec}' (want X:Y:W:H[:F|B] in mm) — skipped"); continue
        side = fields[4].upper() if len(fields) > 4 else "F"
        ref = f"__OBST{i}"
        parts[ref] = dict(value="obstacle", footprint="phantom", w=ow, h=oh,
                          x=ox, y=oy, off=(0.0, 0.0), locked=True, pins={},
                          pitch=999.0, drills=0, npads=0, side=side)
        phantoms.append(ref)
    cg = G.build(parts, nets, a.big_fanout); topo = T.analyze(cg, prefer_hub=a.hub)
    t0 = time.time()
    pos, rot, rep = P.place_routed(parts, cg, topo, center=IO.board_center(board), pad=a.pad)
    nudged = _legalize_bboxes(parts, pos, rot)
    for ref in phantoms:
        pos.pop(ref, None); rot.pop(ref, None); parts.pop(ref, None)
    IO.apply_orientations(board, rot, skip_locked=True)
    IO.apply_positions(board, pos, parts, skip_locked=True)
    print(f"[1/{NSTAGE}] placed {len(pos)} parts  gate-overflow={rep['overflow']:.0f}"
          f"  legalize-nudges={nudged}  ({time.time()-t0:.0f}s)")
    _stages_outline_to_fab(a, board, parts, nets, IO, pos, rot, label="AUTO")


def _stages_outline_to_fab(a, board, parts, nets, IO, pos, rot, label="AUTO"):
    """Stages [2]-[6] shared by `auto` and `compact`: OUTLINE -> PLANES ->
    ROUTE -> PLANE-FINALIZE/DFM -> FAB. Behavior identical to the original
    cmd_auto tail (extracted verbatim, v0.7.0)."""
    import os, subprocess, time, json
    from fluxplace import fab, planes
    import pcbnew
    placed = os.path.join(a.out, "placed.kicad_pcb")
    routed = os.path.join(a.out, "routed.kicad_pcb")
    finald = os.path.join(a.out, "final.kicad_pcb")
    NSTAGE = 6

    # ---- [2] OUTLINE — the board follows the parts. Skipping this strands the ------
    # placement outside Edge.Cuts and the router sees every pad as OFF-BOARD.
    xs0 = []; ys0 = []; xs1 = []; ys1 = []
    for r, (x, y) in pos.items():
        w, h = P.eff_size(parts, r, rot.get(r, 0.0), 0.0)
        xs0.append(x - w / 2); ys0.append(y - h / 2)
        xs1.append(x + w / 2); ys1.append(y + h / 2)
    dims = IO.shrinkwrap_outline(board, min(xs0), min(ys0), max(xs1), max(ys1),
                                 margin=a.margin)
    print(f"[2/{NSTAGE}] outline shrink-wrapped: {dims[0]:.1f} x {dims[1]:.1f} mm")

    # ---- [3] PLANES — pour plane nets on all routing layers + stitch grid ----------
    plane_nets = [] if a.no_planes else list(a.plane_nets)
    if plane_nets:
        layer_ids = [l for l in board.GetEnabledLayers().CuStack()]
        nz = planes.pour(board, plane_nets[0], layers=layer_ids)
        nv = planes.stitch(board, plane_nets[0])
        print(f"[3/{NSTAGE}] planes: {nz} pours + {nv} stitch vias ({plane_nets[0]}, solid pads)")
    else:
        print(f"[3/{NSTAGE}] planes: skipped (--no-planes)")
    IO.save(board, placed)

    # ---- [4] ROUTE — explicit net list (KRT routes NOTHING without --nets), --------
    # power nets first and wide, plane nets excluded (they are pours, not traces).
    t0 = time.time()
    route_nets = [n for n in nets if n not in plane_nets]
    pw = _classify_power(nets, plane_nets, a.big_fanout)
    cmd = [a.router_py, os.path.join(a.router_dir, "py_router", "route.py"),
           placed, "--output", routed, "--keep-input-copper", "--layers", *a.layers,
           "--track-width", str(a.track), "--clearance", str(a.clearance),
           "--via-size", "0.45", "--via-drill", "0.2", "--nets", *route_nets]
    if pw:
        cmd += ["--power-nets", *pw.keys(), "--power-nets-widths",
                *[str(w) for w in pw.values()]]
    r = subprocess.run(cmd, cwd=a.router_dir, capture_output=True, text=True,
                       timeout=a.route_timeout)
    ok = os.path.exists(routed)
    if not ok:
        print("    router stderr:", (r.stderr or r.stdout or "")[-240:].replace("\n", " "))
    summ = ""
    for line in (r.stdout or "").splitlines():
        if line.startswith("JSON_SUMMARY:"):
            try:
                js = json.loads(line[13:])
                summ = (f"  {js.get('successful',0)} ok / {js.get('failed',0)} failed"
                        f", pairs {js.get('pad_pairs_connected','?')}/{js.get('pad_pairs_total','?')}")
            except Exception:
                pass
    print(f"[4/{NSTAGE}] routed {len(route_nets)} nets{summ}  ({time.time()-t0:.0f}s)")
    src = routed if ok else placed

    # ---- [5] PLANE FINALIZE + DFM — a pour alone connects nothing the fill can't ---
    # physically reach (fine-pitch rows): one router pass ON the plane nets adds the
    # stub+via escapes. Then clamp sub-fab vias, embed rules, refill, sync .kicad_pro.
    if plane_nets and ok:
        cmd = [a.router_py, os.path.join(a.router_dir, "py_router", "route.py"),
               routed, "--output", finald, "--keep-input-copper", "--layers", *a.layers,
               "--track-width", str(a.track), "--clearance", str(max(a.clearance, 0.12)),
               "--via-size", "0.45", "--via-drill", "0.2", "--nets", *plane_nets,
               "--max-ripup", "60"]
        r = subprocess.run(cmd, cwd=a.router_dir, capture_output=True, text=True,
                           timeout=a.route_timeout)
        src = finald if os.path.exists(finald) else routed
    b2 = pcbnew.LoadBoard(src)
    clamped, shrunk, grazing = planes.finalize_dfm(b2)
    planes.refill(b2)
    b2.Save(src)
    pro = os.path.splitext(src)[0] + ".kicad_pro"
    planes.sync_project_rules(pro, netclass_clearance=min(a.clearance, 0.13) * 0.75)
    print(f"[5/{NSTAGE}] plane finalize + DFM: {clamped} vias clamped, {shrunk} graze-shrunk,"
          f" zones filled, rules embedded (board + {os.path.basename(pro)})")
    for (gx, gy, gap) in grazing:
        print(f"    ! via at ({gx:.2f},{gy:.2f}) still grazes a pad ({gap} mm air) — review")

    # ---- [6] FAB -------------------------------------------------------------------
    res = fab.emit(src, os.path.join(a.out, "fab"), kicad_cli=a.kicad_cli)
    verdict = res["drc"]
    print(f"[6/{NSTAGE}] fab package -> {res['out']}  DRC {verdict}"
          + (f" ({res['violations']} viol, {res['unconnected']} unrouted)"
             if res.get("violations") is not None else ""))
    print(f"{label} complete: {a.out}/fab  ({verdict})")


def cmd_compact(a):
    """Shrink a KNOWN-GOOD placement: scale unlocked parts toward the locked
    anchor, legalize + gravity-pack (fluxplace.compact), then the same
    OUTLINE -> PLANES -> ROUTE -> DFM -> FAB stages as `auto`. Use when the
    builder's density, not its arrangement, is the limiter."""
    import os, time
    from fluxplace import compact as C
    os.makedirs(a.out, exist_ok=True)
    board, parts, nets, IO = _load(a.board)
    obstacles = C.parse_obstacles(a.obstacle)
    anchor = None
    if a.anchor:
        if a.anchor not in parts:
            raise SystemExit(f"--anchor {a.anchor}: no such ref")
        anchor = (parts[a.anchor]["x"], parts[a.anchor]["y"])
    t0 = time.time()
    pos, st = C.compact(parts, a.sx, a.sy, anchor=anchor, gap=a.gap,
                        pack=a.pack, obstacles=obstacles,
                        tht_bands=a.tht_bands)
    IO.apply_positions(board, pos, parts, skip_locked=True)
    x0, y0, x1, y1 = st["extent"]
    print(f"[1/6] compacted sx={a.sx} sy={a.sy}: {st['nudges']} nudges/"
          f"{st['iters']} iters, {st['hard']} hard-relocated, "
          f"{st['resid']} residual overlaps, extent {x1-x0:.1f}x{y1-y0:.1f} mm"
          f"  ({time.time()-t0:.0f}s)")
    if st["resid"]:
        print("    ! residual overlaps — inspect before fab")
    rot = {r: parts[r].get("angle0", 0.0) for r in parts}  # eff_size identity
    # phantom entries so the shrink-wrapped outline covers the obstacle rects
    # (a plug-on module shadow must stay on-board even where no part reaches)
    for i, ob in enumerate(obstacles):
        ref = f"__OBST{i}"
        parts[ref] = dict(w=ob["w"], h=ob["h"], x=ob["x"], y=ob["y"],
                          off=(0.0, 0.0), angle0=0.0, locked=True, pins={})
        pos[ref] = (ob["x"], ob["y"])
        rot[ref] = 0.0
    _stages_outline_to_fab(a, board, parts, nets, IO, pos, rot, label="COMPACT")


def cmd_lint(a):
    """Design-completeness lint: missing power wiring, dead-end nets,
    unwired connectors, barrel-jack / friction-header connector smells."""
    import json as _json
    from fluxplace import lint as L
    from fluxplace import kicad_io as IO
    board = IO.load(a.board)
    findings = L.run(L.pads_from_board(board), waivers=a.waive)
    n = L.summarize(findings)
    if a.json:
        with open(a.json, "w") as fh:
            _json.dump(findings, fh, indent=1)
        print(f"lint: wrote {a.json}")
    if a.fail_on == "warning" and (n["error"] or n["warning"]):
        raise SystemExit(1)
    if a.fail_on == "error" and n["error"]:
        raise SystemExit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fluxplace")
    ap.add_argument("--big-fanout", type=int, default=12,
                    help="signal nets with >= this many nodes are treated as planes")
    ap.add_argument("--hub", default=None, help="force a specific ref as the hub")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("analyze"); pa.add_argument("--board", required=True)
    pa.set_defaults(fn=cmd_analyze)

    pp = sub.add_parser("place")
    pp.add_argument("--board", required=True)
    pp.add_argument("--out", default=None)
    pp.add_argument("--strategy", choices=["radial", "flux", "pack", "quad", "build"], default="pack",
                    help="pack=hierarchical (organized+compact, default); flux=force-directed; radial")
    pp.add_argument("--rotate", choices=["none", "ortho", "fine"], default="ortho",
                    help="ortho=snap 0/90/180/270 (assembly-friendly); fine=any angle")
    pp.add_argument("--iters", type=int, default=260)
    pp.add_argument("--pad", type=float, default=0.8, help="courtyard margin mm")
    pp.add_argument("--margin", type=float, default=2.0, help="board edge margin mm")
    pp.add_argument("--no-outline", action="store_true", help="don't redraw Edge.Cuts")
    pp.add_argument("--center-board", action="store_true")
    pp.add_argument("--free-connectors", action="store_true")
    pp.add_argument("--move-locked", action="store_true")
    pp.add_argument("--seeds", type=int, default=1,
                    help="try N perturbed placements, keep the best routable one")
    pp.add_argument("--guides", action="store_true",
                    help="draw the global-route corridors on Eco1.User (build strategy)")
    pp.set_defaults(fn=cmd_place)

    pl = sub.add_parser("plan", help="gather info + write a detailed placement plan")
    pl.add_argument("--board", required=True)
    pl.add_argument("--out", default=None, help="write markdown to a file (else stdout)")
    pl.add_argument("--strategy", choices=["radial", "flux", "pack", "quad", "build"], default="pack")
    pl.add_argument("--rotate", choices=["none", "ortho", "fine"], default="ortho")
    pl.set_defaults(fn=cmd_plan)

    pg = sub.add_parser("gather", help="dump structured board facts as JSON")
    pg.add_argument("--board", required=True)
    pg.set_defaults(fn=cmd_gather)

    pr = sub.add_parser("route", help="global-route the current placement, report congestion")
    pr.add_argument("--board", required=True)
    pr.add_argument("--out", default=None)
    pr.add_argument("--guides", action="store_true",
                    help="draw the corridors on Eco1.User and save the board")
    pr.set_defaults(fn=cmd_route)

    pt = sub.add_parser("tournament",
                        help="N placements -> gate filter -> freerouting judges -> winner")
    pt.add_argument("--board", required=True)
    pt.add_argument("--jar", default=None)
    pt.add_argument("--workdir", required=True)
    pt.add_argument("--passes", type=int, default=25)
    pt.add_argument("--jobs", type=int, default=3)
    pt.add_argument("--apply-winner", action="store_true")
    pt.add_argument("--resume", action="store_true",
                    help="reuse existing candidate boards/sessions; adopt live JVMs")
    pt.add_argument("--oit", type=float, default=None,
                    help="freerouting optimizer improvement threshold %% (caps the silent optimizer)")
    pt.set_defaults(fn=cmd_tournament)

    pf = sub.add_parser("fab", help="emit gerbers/drill/place/DRC package for review")
    pf.add_argument("--board", required=True)
    pf.add_argument("--out", required=True)
    pf.add_argument("--kicad-cli", default="kicad-cli")
    pf.set_defaults(fn=cmd_fab)

    pau = sub.add_parser("auto", help="board in -> place -> route -> fab package out")
    pau.add_argument("--board", required=True)
    pau.add_argument("--out", required=True)
    pau.add_argument("--pad", type=float, default=0.45)
    pau.add_argument("--layers", nargs="+", default=["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"])
    pau.add_argument("--track", type=float, default=0.2)
    pau.add_argument("--clearance", type=float, default=0.2)
    pau.add_argument("--router-py", default=os.path.expanduser("~/tools/router-venv/bin/python"))
    pau.add_argument("--router-dir", default=os.path.expanduser("~/tools/KiCadRoutingTools"))
    pau.add_argument("--route-timeout", type=int, default=1800)
    pau.add_argument("--kicad-cli", default="kicad-cli")
    pau.add_argument("--margin", type=float, default=2.0,
                     help="Edge.Cuts margin around the shrink-wrapped placement (mm)")
    pau.add_argument("--plane-nets", nargs="+", default=["GND"],
                     help="nets poured as planes + stitched, excluded from trace routing")
    pau.add_argument("--no-planes", action="store_true",
                     help="skip the plane pour/stitch stage entirely")
    pau.add_argument("--obstacle", action="append", default=[],
                     help="phantom keep-out rect X:Y:W:H (mm), repeatable — e.g. a "
                          "plug-on module body over its locked board connectors")
    pau.set_defaults(fn=cmd_auto)

    pco = sub.add_parser("compact",
                         help="shrink a known-good placement -> route -> fab "
                              "(scale toward locked anchor, legalize, pack)")
    pco.add_argument("--board", required=True)
    pco.add_argument("--out", required=True)
    pco.add_argument("--sx", type=float, required=True, help="X scale toward anchor (<1 shrinks)")
    pco.add_argument("--sy", type=float, required=True, help="Y scale toward anchor (<1 shrinks)")
    pco.add_argument("--gap", type=float, default=0.42, help="min bbox-to-bbox air (mm)")
    pco.add_argument("--pack", type=int, default=5, help="gravity-pack sweeps (0 = off)")
    pco.add_argument("--anchor", default=None,
                     help="ref to shrink toward (default: centroid of locked parts)")
    pco.add_argument("--tht-bands", action="store_true",
                     help="THT parts only above/below obstacle bands (frees X width)")
    pco.add_argument("--layers", nargs="+", default=["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"])
    pco.add_argument("--track", type=float, default=0.2)
    pco.add_argument("--clearance", type=float, default=0.2)
    pco.add_argument("--router-py", default=os.path.expanduser("~/tools/router-venv/bin/python"))
    pco.add_argument("--router-dir", default=os.path.expanduser("~/tools/KiCadRoutingTools"))
    pco.add_argument("--route-timeout", type=int, default=1800)
    pco.add_argument("--kicad-cli", default="kicad-cli")
    pco.add_argument("--margin", type=float, default=2.0,
                     help="Edge.Cuts margin around the shrink-wrapped placement (mm)")
    pco.add_argument("--plane-nets", nargs="+", default=["GND"])
    pco.add_argument("--no-planes", action="store_true")
    pco.add_argument("--obstacle", action="append", default=[],
                     help="phantom keep-out rect X:Y:W:H[:F|B] (mm), repeatable; "
                          "compaction keeps unlocked same-side + THT parts out "
                          "(module escape area must stay clear — see NEXT.md)")
    pco.set_defaults(fn=cmd_compact)

    pli = sub.add_parser("lint",
                         help="design-completeness checks: missing power/IO "
                              "wiring, dead-end nets, non-latching connectors")
    pli.add_argument("--board", required=True)
    pli.add_argument("--json", default=None, help="also write findings as JSON")
    pli.add_argument("--waive", action="append", default=[],
                     help="suppress findings: CODE:REGEX (matches msg or refs), "
                          "repeatable — e.g. dead-end-net:^ETH_ for a de-scoped "
                          "feature or unwired stubs on a split-board interconnect")
    pli.add_argument("--fail-on", choices=["never", "error", "warning"],
                     default="never", help="exit 1 at this severity (default never)")
    pli.set_defaults(fn=cmd_lint)

    pc = sub.add_parser("calibrate",
                        help="ground-truth the gate vs freerouting (DSN export / .ses parse)")
    pc.add_argument("--board", required=True)
    pc.add_argument("--jar", default=None, help="path to freerouting.jar")
    pc.add_argument("--ses", default=None, help="parse an existing .ses instead of running")
    pc.add_argument("--passes", type=int, default=20)
    pc.set_defaults(fn=cmd_calibrate)

    pe = sub.add_parser("eval"); pe.add_argument("--board", required=True)
    pe.add_argument("--pad", type=float, default=0.5)
    pe.set_defaults(fn=cmd_eval)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
