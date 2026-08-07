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
                                       seeds=a.seeds,
                                       layers=len(IO.signal_layers(board)))
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


def _cand_argv(a, k, outdir):
    """argv for one child candidate: same auto config, jitter-seed k, no fab."""
    import sys
    v = [sys.executable, "-u", os.path.abspath(__file__),
         "--big-fanout", str(a.big_fanout)]
    if a.hub:
        v += ["--hub", a.hub]
    v += ["auto", "--board", a.board, "--out", outdir,
          "--pad", str(a.pad), "--route-timeout", str(a.route_timeout),
          "--floor", str(a.floor), "--router-py", a.router_py,
          "--router-dir", a.router_dir, "--kicad-cli", a.kicad_cli,
          "--candidates", "1", "--jitter-seed", str(k), "--no-fab"]
    if a.layers:
        v += ["--layers", *a.layers]
    if a.track is not None:
        v += ["--track", str(a.track)]
    if a.clearance is not None:
        v += ["--clearance", str(a.clearance)]
    if not a.finish:
        v += ["--no-finish"]
    if a.no_fanout:
        v += ["--no-fanout"]
    return v


def cmd_auto_candidates(a):
    """Population search (the Quilter lesson): the router is nondeterministic and
    gate proxies don't predict routed-%, so run N independent place->route
    candidates (candidate 0 = base placement, k>0 = jittered) with bounded
    parallelism, verify each with real DRC, and keep the best. The winner's board
    gets the fab package at --out; every candidate's artifacts stay in cand_k/."""
    import json, subprocess, time
    from fluxplace import adaptive as AD, fab
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    live, done = {}, {}
    todo = list(range(a.candidates))
    print(f"[candidates] {a.candidates} place->route candidates, {a.parallel} in parallel")
    while todo or live:
        while todo and len(live) < max(1, a.parallel):
            k = todo.pop(0)
            outdir = os.path.join(a.out, f"cand_{k}")
            os.makedirs(outdir, exist_ok=True)
            log = open(os.path.join(outdir, "log.txt"), "w")
            live[k] = (subprocess.Popen(_cand_argv(a, k, outdir), stdout=log,
                                        stderr=subprocess.STDOUT), log, time.time())
        time.sleep(5)
        for k in list(live):
            proc, log, ts = live[k]
            if proc.poll() is None:
                continue
            log.close()
            del live[k]
            done[k] = os.path.join(a.out, f"cand_{k}", "routed.kicad_pcb")
            print(f"  cand_{k}: exit {proc.returncode}  ({time.time()-ts:.0f}s)")

    rows = []
    for k in sorted(done):
        if not os.path.exists(done[k]):
            print(f"  cand_{k}: no routed board — dropped")
            continue
        d, un = AD.drc_unrouted(done[k], a.kicad_cli)
        un.discard("GND")
        rows.append((k, len(un), len(d.get("violations", []))))
        print(f"  cand_{k}: unrouted={len(un)}  violations={rows[-1][2]}")
    if not rows:
        print("CANDIDATES failed: no candidate produced a routed board")
        return
    win = rows[AD.pick_best([(f"cand_{k}", u, v) for k, u, v in rows])][0]
    print(f"[candidates] winner: cand_{win}  ({time.time()-t0:.0f}s total)")
    import shutil
    src_dir = os.path.join(a.out, f"cand_{win}")
    for f in ("placed.kicad_pcb", "routed.kicad_pcb", "routed.kicad_dru"):
        if os.path.exists(os.path.join(src_dir, f)):
            shutil.copy(os.path.join(src_dir, f), os.path.join(a.out, f))
    json.dump({"winner": win, "candidates": [
        {"cand": k, "unrouted": u, "violations": v} for k, u, v in rows]},
        open(os.path.join(a.out, "candidates.json"), "w"), indent=1)
    res = fab.emit(os.path.join(a.out, "routed.kicad_pcb"),
                   os.path.join(a.out, "fab"), kicad_cli=a.kicad_cli)
    print(f"AUTO complete: {a.out}/fab  ({res['drc']}, winner cand_{win})")


def cmd_auto(a):
    """The 'magic' endpoint: board in -> PLACE -> ROUTE -> FAB -> review-ready package.
    Each stage is logged with its verdict; the router is pluggable (KRT by default)."""
    import os, subprocess, time
    from fluxplace import fab
    if a.candidates > 1:
        return cmd_auto_candidates(a)
    os.makedirs(a.out, exist_ok=True)
    placed = os.path.join(a.out, "placed.kicad_pcb")
    routed = os.path.join(a.out, "routed.kicad_pcb")

    # ---- [1] PLACE (route-aware, escape-aware) ------------------------------------
    board, parts, nets, IO = _load(a.board)
    cg = G.build(parts, nets, a.big_fanout); topo = T.analyze(cg, prefer_hub=a.hub)
    # AUTO-DETECT so an end user needs no stackup/rule knowledge: signal layers = copper
    # minus poured planes; bulk track/clearance = the board's own default netclass.
    # Detected BEFORE placement — the gate's capacity model needs the real layer count.
    layers = a.layers or IO.signal_layers(board)
    dtrack, dclr = IO.default_rules(board)
    track = a.track if a.track is not None else dtrack
    clr = a.clearance if a.clearance is not None else dclr
    print(f"    auto-detected: signal-layers={layers}  bulk={track}/{clr}mm  floor={a.floor}mm")
    t0 = time.time()
    pos, rot, rep = P.place_routed(parts, cg, topo, center=IO.board_center(board),
                                   pad=a.pad, layers=len(layers),
                                   jitter_seed=a.jitter_seed)
    IO.apply_orientations(board, rot, skip_locked=True)
    IO.apply_positions(board, pos, parts, skip_locked=True)
    IO.save(board, placed)
    print(f"[1/3] placed {len(pos)} parts  gate-overflow={rep['overflow']:.0f}  ({time.time()-t0:.0f}s)")

    # ---- [2] ROUTE — route-fresh-per-rung + fanout-aware finisher (universal) --------
    from fluxplace import adaptive as AD, escape as ESC
    t0 = time.time()
    pw = {n: G.power_width(n) for n in getattr(cg, "power_traces", {})}
    route_fresh = AD.krt_route_fresh(a.router_py, a.router_dir, layers,
                                     base_w=track, base_c=clr,
                                     power_nets=list(pw) or None,
                                     power_widths=[max(track, w * track) for w in pw.values()] or None,
                                     timeout=a.route_timeout)
    # fanout needs a spare layer to escape TO: on a <3-signal-layer board every
    # through-via also punches the other routing layer, so blanket escape vias make
    # congestion worse (measured on CM5, 2 signal layers: fanout regressed unrouted)
    if a.no_fanout:
        fanout = None
    elif len(layers) < 3:
        fanout = None
        print("    fanout: OFF (<3 signal layers — escape vias would eat them)")
    else:
        fanout = AD.krt_fanout(a.router_py, a.router_dir, layers,
                               track_w=a.floor, clearance=a.floor)
    src, summ = AD.route_adaptive(placed, a.out, route_fresh, cg, parts,
                                  kicad_cli=a.kicad_cli, start_mm=clr,
                                  floor_mm=a.floor, fanout=(fanout if a.finish else None),
                                  log=lambda m: print("   " + m))
    # local fine-pitch .kicad_dru so the escape copper is DRC-legal (bulk stays 0.2mm)
    if os.path.exists(src):
        d, _u = AD.drc_unrouted(src, a.kicad_cli)
        zones = ESC.detect_escape_zones(parts, d, min_unrouted=1)
        open(os.path.splitext(src)[0] + ".kicad_dru", "w").write(ESC.dru_text(zones, a.floor, a.floor))
    ladder = [r["width"] for r in summ["rounds"]]
    print(f"[2/3] route+anneal via {os.path.basename(a.router_py)}: "
          f"{summ['diagnosis']}  ladder={ladder}  fanned={summ['fanned']}  ({time.time()-t0:.0f}s)")

    # ---- [3] FAB -------------------------------------------------------------------
    if a.no_fab:
        print(f"AUTO candidate complete: {a.out} (fab skipped — parent selects the winner)")
        return
    res = fab.emit(src, os.path.join(a.out, "fab"), kicad_cli=a.kicad_cli)
    verdict = res["drc"]
    print(f"[3/3] fab package -> {res['out']}  DRC {verdict}"
          + (f" ({res['violations']} viol, {res['unconnected']} unrouted)"
             if res.get("violations") is not None else ""))
    print(f"AUTO complete: {a.out}/fab  ({verdict})")


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
    pau.add_argument("--layers", nargs="+", default=None,
                     help="signal layers (default: auto-detect = copper minus poured planes)")
    pau.add_argument("--track", type=float, default=None,
                     help="bulk track width mm (default: board's default netclass)")
    pau.add_argument("--clearance", type=float, default=None,
                     help="bulk clearance mm (default: board's default netclass)")
    pau.add_argument("--router-py", default=os.path.expanduser("~/tools/router-venv/bin/python"))
    pau.add_argument("--router-dir", default=os.path.expanduser("~/tools/KiCadRoutingTools"))
    pau.add_argument("--route-timeout", type=int, default=1800)
    pau.add_argument("--floor", type=float, default=0.1, help="fine-pitch escape floor (mm)")
    pau.add_argument("--no-finish", dest="finish", action="store_false",
                     help="skip the adaptive step-down anneal (one route pass only)")
    pau.add_argument("--no-fanout", action="store_true",
                     help="don't generate via-in-pad fanout for geometric residue")
    pau.add_argument("--kicad-cli", default="kicad-cli")
    pau.add_argument("--candidates", type=int, default=1,
                     help="population search: N independent place->route candidates "
                          "(cand 0 = base, k>0 jittered), DRC-best wins the fab package")
    pau.add_argument("--parallel", type=int, default=3,
                     help="max candidates routing at once (each KRT is multi-threaded)")
    pau.add_argument("--jitter-seed", type=int, default=0,
                     help="placement jitter seed (used by candidate children)")
    pau.add_argument("--no-fab", action="store_true",
                     help="skip the fab package (candidate children do this)")
    pau.set_defaults(fn=cmd_auto, finish=True)

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
