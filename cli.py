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
    if a.upload_out:
        fab.upload_package(a.board, a.upload_out, project_dir=a.project_dir)


def _cand_argv(a, k, outdir):
    """argv for one child candidate: same auto config, jitter-seed k, no fab."""
    import sys
    v = [sys.executable, "-u", os.path.abspath(__file__),
         "--big-fanout", str(a.big_fanout)]
    if a.hub:
        v += ["--hub", a.hub]
    v += ["auto", "--board", a.board, "--out", outdir,
          "--pad", str(a.pad), "--route-timeout", str(a.route_timeout),
          "--profile", a.profile, "--router-py", a.router_py,
          "--router-dir", a.router_dir, "--kicad-cli", a.kicad_cli,
          "--candidates", "1", "--jitter-seed", str(k), "--no-fab"]
    if a.floor is not None:
        v += ["--floor", str(a.floor)]
    if a.constraints:
        v += ["--constraints", a.constraints]
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
    if a.keep_outline:
        v += ["--keep-outline"]
    if a.no_patch:
        v += ["--no-patch"]
    if a.no_pairs:
        v += ["--no-pairs"]
    if a.bypass_csv:
        v += ["--bypass-csv", a.bypass_csv]
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
    from fluxplace import profiles as PROF
    fchecks, fsummary = PROF.check_board(os.path.join(a.out, "routed.kicad_pcb"),
                                         PROF.get(a.profile))
    print(f"    fab-profile [{a.profile}]: {fsummary}")
    for lvl, code, msg in fchecks:
        print(f"    fab-profile {lvl} {code}: {msg}")
    _order_guidance(a, os.path.join(a.out, "routed.kicad_pcb"),
                    os.path.join(a.out, "fab"))
    print(f"AUTO complete: {a.out}/fab  ({res['drc']}, winner cand_{win})")


def cmd_comprehend(a):
    """Circuit comprehension: infer the electrical-intent tables (power classes,
    diff pairs, bypass cap -> pin, crystal -> parent) and emit them for review.
    The reviewed numbers belong in constraints.toml, which always wins."""
    from fluxplace import comprehend as CO
    board, parts, nets, IO = _load(a.board)
    cg = G.build(parts, nets, a.big_fanout)
    comp = CO.comprehend(parts, nets, cg)
    txt = CO.to_toml(comp)
    if a.out:
        open(a.out, "w").write(txt)
        print(f"wrote {a.out}  ({len(comp['bypass'])} bypass, "
              f"{len(comp['pairs'])} pairs, {len(comp['crystals'])} crystals, "
              f"{len(comp['power'])} power planes)")
    else:
        print(txt)


def _export_netlist_xml(kicad_cli, sch):
    """Export the schematic netlist (kicadxml) to a temp file; returns
    (path, error). Caller unlinks the path."""
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
        xml = tf.name
    r = subprocess.run([kicad_cli, "sch", "export", "netlist",
                        "--format", "kicadxml", "--output", xml, sch],
                       capture_output=True, text=True)
    if r.returncode != 0:
        os.unlink(xml)
        return None, (r.stderr or "kicad-cli netlist export failed").strip()[:200]
    return xml, None


def cmd_preflight(a):
    """Upload-gate check: would a downstream parser (fab, assembly, another EDA
    tool) reject this board? Prints findings; exits 1 on any FAIL. With --sch,
    also cross-checks schematic pins against board pads (pin/pad parity). With
    --components, adds the per-footprint order-readiness audit (stand-ins,
    parity, courtyards, 3D models) — run it before layout AND before ordering."""
    board, parts, nets, IO = _load(a.board)
    if a.fix_out:
        fixed, stuck = IO.repair_pad_overlaps(board)
        IO.save(board, a.fix_out)
        print(f"repaired {len(fixed)} different-net pad overlaps -> {a.fix_out}"
              + (f"; {len(stuck)} pairs NEED A REAL FOOTPRINT: {stuck[:6]}" if stuck else ""))
    findings = list(IO.preflight(board))
    xml = None
    if a.sch:
        xml, err = _export_netlist_xml(a.kicad_cli, a.sch)
        if xml:
            for ref, miss in sorted(IO.pin_pad_parity(board, xml).items()):
                findings.append(("FAIL", "SCH_PIN_NO_PAD",
                                 f"{ref}: schematic pin(s) {miss} have no pad on the "
                                 f"footprint — 'pins not on the board' to a strict parser"))
            diffs = IO.pad_net_parity(board, xml)
            if diffs:
                import collections
                bynet = collections.Counter(t for _, _, _, t in diffs)
                findings.append(("FAIL", "PAD_NET_MISMATCH",
                                 f"{len(diffs)} pad(s) disagree with the schematic "
                                 f"netlist (top: {bynet.most_common(3)}) — routers "
                                 f"and DRC are working an incomplete net set; run "
                                 f"sync-nets"))
        else:
            findings.append(("WARN", "NETLIST_EXPORT_FAILED", err))
    if a.components:
        audit = IO.component_audit(board, xml)
        for lvl, ref, fpid, issue in audit:
            findings.append((lvl, "COMPONENT", f"{ref} [{fpid}]: {issue}"))
        if not audit:
            print("COMPONENT AUDIT clean — order-ready")
    if xml:
        os.unlink(xml)
    for lvl, code, msg in findings:
        print(f"{lvl}  {code}: {msg}")
    if not findings:
        print("PREFLIGHT clean")
    if any(lvl == "FAIL" for lvl, _, _ in findings):
        raise SystemExit(1)


def cmd_patch(a):
    """Standalone last-mile patch: close leftover unrouted nets + heal pours
    on a routed board (DRC-guarded; reverts routes that regress DRC)."""
    from fluxplace import patch as PATCH, constraints as CONS
    cons = CONS.load(a.constraints)
    nw = {n: CONS.power_width_mm(cons, n, 0.5)
          for n in (cons or {}).get("power", {})}
    res = PATCH.patch_board(a.board, a.out or a.board, kicad_cli=a.kicad_cli,
                            track_w=a.track, clearance=a.clearance,
                            net_widths=nw)
    print(f"patch: accepted={res['accepted']} patched={res['patched']} "
          f"failed={res['failed']}")


def cmd_verifymodels(a):
    """Verify every footprint's 3D model sits ON its pins (TH: pin shafts vs
    holes; SMD: body over the footprint). --fix solves and writes the
    correcting transform where possible."""
    board, parts, nets, IO = _load(a.board)
    from fluxplace import models as M
    resolve = lambda p: IO._resolve_model_path(p, board)
    finds = M.verify_board(board, resolve, fix=a.fix, tol=a.tol)
    for ref, msg in finds:
        print(f"{ref}: {msg}")
    if not finds:
        print("MODEL REGISTRATION clean — every model on its pins")
    if a.fix:
        IO.save(board, a.out or a.board)
        print(f"saved {a.out or a.board}")


def cmd_syncnets(a):
    """Make board pad nets agree with the schematic netlist (headless
    'Update PCB from Schematic', nets only). The netlist is truth."""
    board, parts, nets, IO = _load(a.board)
    xml, err = _export_netlist_xml(a.kicad_cli, a.sch)
    if not xml:
        raise SystemExit(f"netlist export failed: {err}")
    rep = IO.sync_pad_nets(board, xml)
    os.unlink(xml)
    IO.save(board, a.out or a.board)
    print(f"sync-nets: {rep['assigned']} pad(s) re-netted across "
          f"{len(rep['refs'])} component(s)"
          + (f"; created nets {rep['created_nets'][:8]}" if rep['created_nets'] else ""))
    for ref, n in sorted(rep["refs"].items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {ref}: {n}")


def cmd_replacefp(a):
    """Swap one reference's footprint for a real library footprint, preserving
    placement and the schematic link, re-assigning pad nets by pad number.
    With --sch the schematic netlist is the net truth (recovers nets the
    stand-in never had pads for). --rename maps vendor pad names onto
    schematic pin numbers, e.g. --rename MP1:S1,MP2:S2."""
    board, parts, nets, IO = _load(a.board)
    net_by_pin = None
    xml = None
    if a.sch:
        xml, err = _export_netlist_xml(a.kicad_cli, a.sch)
        if not xml:
            raise SystemExit(f"netlist export failed: {err}")
        net_by_pin = IO.netlist_pin_nets(xml, a.ref)
        os.unlink(xml)
        if not net_by_pin:
            print(f"note: {a.ref} not in schematic netlist; using old pad nets")
    renames = None
    if a.rename:
        renames = dict(pair.split(":", 1) for pair in a.rename.split(","))
    rep = IO.replace_footprint(board, a.ref, a.lib, a.name,
                               net_by_pin=net_by_pin, renames=renames)
    IO.save(board, a.out or a.board)
    print(f"{a.ref} -> {a.name}: {rep['assigned']} pads netted"
          + (f"; created nets {rep['created_nets']}" if rep['created_nets'] else ""))
    if rep["unnetted_pads"]:
        print(f"  unnetted pads (no schematic pin): {rep['unnetted_pads']}")
    if rep["pins_without_pads"]:
        print(f"  WARNING pins with no pad on the new footprint: "
              f"{rep['pins_without_pads']}")


def _order_guidance(a, routed, fab_dir):
    """Print (and append to the MANIFEST) the what-do-I-pick block: fab
    service tier, the stackup preset string upload tools show, impedance and
    rail-current answers from the engineering constraints."""
    from fluxplace import profiles as PROF, constraints as CONS, kicad_io as IO
    board = IO.load(routed)
    bb = board.GetBoardEdgesBoundingBox()
    g = PROF.order_guidance(a.profile, board.GetCopperLayerCount(),
                            len(IO.signal_layers(board)),
                            (bb.GetWidth() / 1e6, bb.GetHeight() / 1e6),
                            CONS.load(a.constraints))
    print(g)
    mf = os.path.join(fab_dir, "MANIFEST.txt")
    if os.path.exists(mf):
        with open(mf, "a") as f:
            f.write(g + "\n")


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

    # ---- [0] PREFLIGHT — parse-level sanity a downstream tool would reject --------
    from fluxplace import profiles as PROF, constraints as CONS
    prof = PROF.get(a.profile)
    cons = CONS.load(a.constraints)
    floor = a.floor if a.floor is not None else prof["floor"]
    board, parts, nets, IO = _load(a.board)
    findings = IO.preflight(board)
    for lvl, code, msg in findings:
        print(f"    preflight {lvl} {code}: {msg}")
    if any(lvl == "FAIL" for lvl, _, _ in findings):
        print("    (continuing — placement may resolve; the placed board is re-checked)")

    # ---- [1] PLACE (route-aware, escape-aware) ------------------------------------
    cg = G.build(parts, nets, a.big_fanout); topo = T.analyze(cg, prefer_hub=a.hub)
    # AUTO-DETECT so an end user needs no stackup/rule knowledge: signal layers = copper
    # minus poured planes; bulk track/clearance = the board's own default netclass.
    # Detected BEFORE placement — the gate's capacity model needs the real layer count.
    layers = a.layers or IO.signal_layers(board)
    dtrack, dclr = IO.default_rules(board)
    track = a.track if a.track is not None else dtrack
    clr = a.clearance if a.clearance is not None else dclr
    # PROFILE FLOOR IS ABSOLUTE: the package must be orderable at the chosen
    # service tier, so nothing the pipeline emits may be finer than the profile
    # (measured: the CM5 netclass carries 3.5mil bulk track — legal on
    # jlcpcb-advanced, a fab-gate FAIL under the standard profile it ran with)
    if track < prof["track_min"]:
        print(f"    profile clamp: board netclass track {track}mm < "
              f"{prof['track_min']}mm ({a.profile} min) — clamped to profile")
        track = prof["track_min"]
    if clr < prof["clearance_min"]:
        print(f"    profile clamp: board netclass clearance {clr}mm < "
              f"{prof['clearance_min']}mm ({a.profile} min) — clamped to profile")
        clr = prof["clearance_min"]
    print(f"    auto-detected: signal-layers={layers}  bulk={track}/{clr}mm  "
          f"floor={floor}mm (profile {a.profile})")
    # ATTACHMENTS: every decap and crystal cluster hugs its owner DURING
    # construction (comprehend inference), so adjacency is by construction
    from fluxplace import comprehend as CO
    comp = CO.comprehend(parts, nets, cg)
    att = {}
    csv_own = {}
    if a.bypass_csv:
        import csv as _csv
        from collections import Counter as _Ctr
        votes = {}
        for row in _csv.DictReader(open(a.bypass_csv)):
            votes.setdefault(row["capacitor"], _Ctr())[row["bypassed_component"]] += 1
        csv_own = {c: v.most_common(1)[0][0] for c, v in votes.items()
                   if c in parts and v.most_common(1)[0][0] in parts}
        for c, ic in csv_own.items():
            att.setdefault(ic, []).append(c)
        print(f"    bypass-csv: {len(csv_own)} cap ownerships from {a.bypass_csv}")
    for c, ic, rail, d in comp["bypass"]:
        if c not in csv_own:
            att.setdefault(ic, []).append(c)
    for cl in comp["crystals"]:
        att.setdefault(cl["parent"], []).extend([cl["crystal"]] + cl["load_caps"])
    natt = sum(len(v) for v in att.values())
    if natt:
        print(f"    attachments: {natt} decap/crystal parts hug {len(att)} owners")
    t0 = time.time()
    ob = board.GetBoardEdgesBoundingBox()
    ox0, oy0, ox1, oy1 = (v / 1e6 for v in (ob.GetLeft(), ob.GetTop(),
                                            ob.GetRight(), ob.GetBottom()))
    fixed_bounds = None
    if a.keep_outline:
        m = 0.5
        fixed_bounds = (ox0 + m, oy0 + m, ox1 - m, oy1 - m)
        print(f"    keep-outline: parts constrained to the source outline "
              f"{ox1 - ox0:.0f}x{oy1 - oy0:.0f}mm (grow-to-route disabled)")
    pos, rot, rep = P.place_routed(parts, cg, topo, center=IO.board_center(board),
                                   pad=a.pad, layers=len(layers),
                                   jitter_seed=a.jitter_seed, attachments=att,
                                   fixed_bounds=fixed_bounds)
    IO.apply_orientations(board, rot, skip_locked=True)
    IO.apply_positions(board, pos, parts, skip_locked=True)
    # OUTLINE CONTAINMENT: if the placement exceeds the source outline, regrow the
    # outline around the parts — otherwise every outside pad is off-board and the
    # fab package is garbage (the exact parse error other tools reject boards for).
    # With --keep-outline the outline is a mechanical given and NEVER regrows: an
    # overhang here is a loud FAIL, not a bigger board.
    ex0, ey0, ex1, ey1 = IO.parts_extent(parts, pos, rot, P.eff_size)
    if ex0 < ox0 or ey0 < oy0 or ex1 > ox1 or ey1 > oy1:
        if a.keep_outline:
            print(f"    FAIL KEEP_OUTLINE: parts extent "
                  f"{ex1 - ex0:.1f}x{ey1 - ey0:.1f}mm exceeds the fixed outline "
                  f"{ox1 - ox0:.0f}x{oy1 - oy0:.0f}mm — the design does not fit; "
                  f"review gate overflow / free board area")
        else:
            w, h = IO.shrinkwrap_outline(board, ex0, ey0, ex1, ey1)
            print(f"    outline regrown to {w:.0f}x{h:.0f}mm "
                  f"(placement exceeded the source outline)")
    IO.save(board, placed)
    print(f"[1/3] placed {len(pos)} parts  gate-overflow={rep['overflow']:.0f}  ({time.time()-t0:.0f}s)")

    # ---- [2] ROUTE — route-fresh-per-rung + fanout-aware finisher (universal) --------
    from fluxplace import adaptive as AD, escape as ESC
    t0 = time.time()
    pours = CONS.pour_nets(cons)
    pw = {n: G.power_width(n) for n in getattr(cg, "power_traces", {})
          if n not in pours}
    pwidths = [CONS.power_width_mm(cons, n, max(track, w * track))
               for n, w in pw.items()]
    if pours:
        print(f"    constraints: {sorted(pours)} ride pours (no fat trace)")
    # PAIRS FIRST: coupled pre-route of differential pairs (route_diff), so the
    # bulk router builds around their copper instead of splitting P from N
    from fluxplace.graph import diff_pairs as _dp
    dpairs0 = {s: m for s, m in _dp(
        {n: len(v) for n, v in cg.signal_nets.items()}).items()
        if m in cg.signal_nets}
    locked_pairnets = []
    if dpairs0 and not a.no_pairs:
        pg = prof.get("pair_geom")
        # RETRY loop: route_diff completion is nondeterministic (1-4 of 7 pairs
        # per draw on CM5). Each round locks newly-complete pairs PAIR-ATOMICALLY
        # (both sides or neither — locking half a pair bakes in asymmetry,
        # measured 27.7mm skew) and re-attempts only the stragglers.
        cur_b = placed
        for attempt in range(3):
            todo = {sl: m for sl, m in dpairs0.items()
                    if m not in locked_pairnets and sl not in locked_pairnets}
            if not todo:
                break
            pr = AD.krt_route_diff(a.router_py, a.router_dir, layers, todo,
                                   track_w=pg[0] if pg else track,
                                   gap=pg[1] if pg else max(0.15, prof["clearance_min"]),
                                   clearance=(max(0.1, prof["clearance_min"])
                                              if pg else clr),
                                   via_size=prof["route_via"][0],
                                   via_drill=prof["route_via"][1])
            nxt = pr(cur_b, os.path.join(a.out, f"placed_pairs{attempt}.kicad_pcb"),
                     log=lambda m: print("   " + m))
            if nxt == cur_b:
                break
            cur_b = nxt
            _d, _un = AD.drc_unrouted(cur_b, a.kicad_cli)
            new_locked = []
            for sl, m in sorted(todo.items()):
                if sl not in _un and m not in _un:
                    new_locked += [m, sl]
            if not new_locked:
                break
            IO.lock_net_copper(cur_b, new_locked)
            locked_pairnets += new_locked
        placed = cur_b
        failed = sorted((set(dpairs0) | set(dpairs0.values())) - set(locked_pairnets))
        print(f"    pairs-first: {len(dpairs0)} pairs, "
              f"{len(locked_pairnets) // 2} locked coupled after retries"
              + (f", left to bulk: {failed}" if failed else ""))
    route_fresh = AD.krt_route_fresh(a.router_py, a.router_dir, layers,
                                     base_w=track, base_c=clr,
                                     via_size=prof["route_via"][0],
                                     via_drill=prof["route_via"][1],
                                     power_nets=list(pw) or None,
                                     power_widths=pwidths or None,
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
                               track_w=floor, clearance=floor,
                               via_size=prof["fanout_via"][0],
                               via_drill=prof["fanout_via"][1])
    src, summ = AD.route_adaptive(placed, a.out, route_fresh, cg, parts,
                                  kicad_cli=a.kicad_cli, start_mm=clr,
                                  floor_mm=floor, fanout=(fanout if a.finish else None),
                                  log=lambda m: print("   " + m))
    # SKEW REPAIR: force-reroute the bulk-routed pairs as length-matched groups
    # (locked coupled pairs are untouchable by design). Guarded: accept only if
    # unrouted did not grow AND the worst skew actually improved.
    from fluxplace import si as SI
    import shutil as _sh
    if dpairs0 and not a.no_pairs and os.path.exists(src):
        unlocked = {sl: m for sl, m in dpairs0.items()
                    if m not in locked_pairnets and sl not in locked_pairnets}
        if unlocked:
            lmfn = AD.krt_length_match(a.router_py, a.router_dir, layers, unlocked,
                                       base_w=track, base_c=clr,
                                       via_size=prof["route_via"][0],
                                       via_drill=prof["route_via"][1])
            cand = lmfn(src, os.path.join(a.out, "routed_lm.kicad_pcb"),
                        log=lambda m: print("   " + m))
            if cand != src and os.path.exists(cand):
                _, u0 = AD.drc_unrouted(src, a.kicad_cli); u0.discard("GND")
                _, u1 = AD.drc_unrouted(cand, a.kicad_cli); u1.discard("GND")
                _, tb0 = SI.check_board(src, dpairs0)
                _, tb1 = SI.check_board(cand, dpairs0)
                w0 = max((r[4] for r in tb0), default=0.0)
                w1 = max((r[4] for r in tb1), default=0.0)
                if len(u1) <= len(u0) and w1 < w0 - 0.05:
                    _sh.copy(cand, src)
                    print(f"    length-match: worst skew {w0:.2f} -> {w1:.2f}mm (kept)")
                else:
                    print(f"    length-match: skew {w0:.2f} -> {w1:.2f}mm, "
                          f"unrouted {len(u0)} -> {len(u1)} — discarded")

    # RETURN VIAS: stitch GND next to pair vias (return current must change
    # reference planes where the signal does). DRC-guarded: revert on regression.
    if dpairs0 and not a.no_pairs and os.path.exists(src):
        import shutil as _sh2
        bak = src + ".prestitch"
        _sh2.copy(src, bak)
        d0v = len(AD.drc_unrouted(src, a.kicad_cli)[0].get("violations", []))
        nrv = IO.add_return_vias(src, set(dpairs0) | set(dpairs0.values()),
                                 via_mm=prof["route_via"][0],
                                 drill_mm=prof["route_via"][1])
        if nrv:
            d1v = len(AD.drc_unrouted(src, a.kicad_cli)[0].get("violations", []))
            if d1v > d0v:
                _sh2.copy(bak, src)
                print(f"    return-vias: {nrv} added but DRC {d0v}->{d1v} — reverted")
            else:
                print(f"    return-vias: {nrv} GND stitching vias added (DRC {d0v}->{d1v})")

    # LAST-MILE PATCH: incremental single-net router closes the leftover
    # unconstrained nets + refills pours (DRC-guarded, in-module revert)
    if not a.no_patch and os.path.exists(src):
        from fluxplace import patch as PATCH
        nw = {n: CONS.power_width_mm(cons, n, max(track, 0.5))
              for n in (cons or {}).get("power", {})}
        try:
            PATCH.patch_board(src, src, kicad_cli=a.kicad_cli,
                              track_w=track, clearance=clr,
                              via_mm=prof["route_via"][0],
                              drill_mm=prof["route_via"][1],
                              net_widths=nw,
                              log=lambda m: print("   " + m))
        except Exception as e:
            print(f"    patch: stage failed ({e}) — board unchanged")

    # FINISHER: freerouting on the residue (slow, completion-strong). Keep-best.
    jar = os.path.expanduser("~/tools/freerouting-2.2.4.jar")
    if not a.no_finisher and os.path.exists(jar) and os.path.exists(src):
        _, u0 = AD.drc_unrouted(src, a.kicad_cli); u0.discard("GND")
        if u0:
            print(f"    finisher: freerouting on the last {len(u0)} nets")
            fin = AD.freerouting_finish(jar)(src, os.path.join(a.out, "routed_fin.kicad_pcb"),
                                             log=lambda m: print("   " + m))
            if fin != src and os.path.exists(fin):
                _, u1 = AD.drc_unrouted(fin, a.kicad_cli); u1.discard("GND")
                if len(u1) < len(u0):
                    _sh.copy(fin, src)
                    print(f"    finisher: {len(u0)} -> {len(u1)} unrouted (kept)")
                else:
                    print(f"    finisher: {len(u1)} unrouted — discarded")

    # local fine-pitch .kicad_dru so the escape copper is DRC-legal (bulk stays 0.2mm)
    if os.path.exists(src):
        d, _u = AD.drc_unrouted(src, a.kicad_cli)
        zones = ESC.detect_escape_zones(parts, d, min_unrouted=1)
        open(os.path.splitext(src)[0] + ".kicad_dru", "w").write(ESC.dru_text(zones, floor, floor))
    ladder = [r["width"] for r in summ["rounds"]]
    print(f"[2/3] route+anneal via {os.path.basename(a.router_py)}: "
          f"{summ['diagnosis']}  ladder={ladder}  fanned={summ['fanned']}  ({time.time()-t0:.0f}s)")

    # ---- [3] FAB -------------------------------------------------------------------
    if a.no_fab:
        print(f"AUTO candidate complete: {a.out} (fab skipped — parent selects the winner)")
        return
    res = fab.emit(src, os.path.join(a.out, "fab"), kicad_cli=a.kicad_cli)
    verdict = res["drc"]
    fchecks, fsummary = PROF.check_board(src, prof)
    print(f"    fab-profile [{a.profile}]: {fsummary}")
    for lvl, code, msg in fchecks:
        print(f"    fab-profile {lvl} {code}: {msg}")
        verdict = "REVIEW"
    # SI-lite: diff-pair intra-pair skew on the routed copper (report-only)
    from fluxplace import si as SI
    from fluxplace.graph import diff_pairs
    dpairs = {s: m for s, m in diff_pairs(
        {n: len(v) for n, v in cg.signal_nets.items()}).items()
        if m in cg.signal_nets}
    schecks, stable = (SI.check_board(src, dpairs,
                                  warn_mm=lambda m: CONS.skew_limit_mm(cons, m))
                   if dpairs else ([], []))
    if dpairs:
        worst = max((r[4] for r in stable), default=0.0)
        print(f"    si-lite: {len(stable)} routed diff pairs, worst skew {worst:.2f}mm, "
              f"{len(schecks)} finding(s)")
        for lvl, code, msg in schecks:
            print(f"    si-lite {lvl} {code}: {msg}")
        import pcbnew as _pn
        _bd = _pn.LoadBoard(src)
        rvf, _rt = SI.return_via_findings(*SI.collect_vias(_bd, set(dpairs) | set(dpairs.values())))
        print(f"    si-lite: return-path vias — {len(rvf)} finding(s)")
        for lvl, code, msg in rvf[:6]:
            print(f"    si-lite {lvl} {code}: {msg}")
    # bypass proximity: a decap >10mm from its pin is inductively absent at HF.
    # Measured on the FINISHED board (read back) so placement moves are seen.
    fparts, fnets = IO.read_board(IO.load(src))
    bchecks, btable = SI.bypass_findings(fparts, fnets, set(cg.power_nets))
    if btable:
        bworst = max(r[3] for r in btable)
        print(f"    si-lite: {len(btable)} bypass caps, worst pin distance "
              f"{bworst:.1f}mm, {len(bchecks)} finding(s)")
        for lvl, code, msg in bchecks[:8]:
            print(f"    si-lite {lvl} {code}: {msg}")
        schecks = schecks + bchecks
    try:
        with open(os.path.join(a.out, "fab", "MANIFEST.txt"), "a") as mf:
            mf.write(f"\nfab profile: {a.profile}\n{fsummary}\n")
            for lvl, code, msg in fchecks:
                mf.write(f"{lvl} {code}: {msg}\n")
            if dpairs:
                mf.write(f"\nsi-lite diff-pair skew (P/N routed mm | skew):\n")
                for m, s, lm, ls, sk in stable:
                    mf.write(f"  {m} / {s}: {lm:.1f} / {ls:.1f} | {sk:.2f}\n")
                for lvl, code, msg in schecks:
                    mf.write(f"{lvl} {code}: {msg}\n")
    except OSError:
        pass
    print(f"[3/3] fab package -> {res['out']}  DRC {verdict}"
          + (f" ({res['violations']} viol, {res['unconnected']} unrouted)"
             if res.get("violations") is not None else ""))
    _order_guidance(a, src, os.path.join(a.out, "fab"))
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
    pf.add_argument("--upload-out", default=None,
                    help="also assemble the ECAD upload set (board + pro + dru "
                         "+ schematics, no .kicad_prl) into this directory")
    pf.add_argument("--project-dir", default=None,
                    help="project dir for --upload-out (default: board's dir)")
    pf.set_defaults(fn=cmd_fab)

    pco = sub.add_parser("comprehend",
                         help="infer electrical intent: pairs, bypass, crystals, power")
    pco.add_argument("--board", required=True)
    pco.add_argument("--out", default=None, help="write TOML here (default: stdout)")
    pco.set_defaults(fn=cmd_comprehend)

    ppre = sub.add_parser("preflight",
                          help="parse-level sanity: outline, pads on-board, pos-file parity")
    ppre.add_argument("--board", required=True)
    ppre.add_argument("--sch", default=None,
                      help="root schematic: also cross-check sch pins vs board pads")
    ppre.add_argument("--fix-out", default=None,
                      help="repair different-net pad overlaps (shrink toward pad "
                           "centres, pins unchanged) and write the board here")
    ppre.add_argument("--kicad-cli", default="kicad-cli")
    ppre.add_argument("--components", action="store_true",
                      help="per-footprint order-readiness audit: stand-ins, "
                           "pin parity, courtyards, 3D models")
    ppre.set_defaults(fn=cmd_preflight)

    ppa = sub.add_parser("patch",
                         help="close leftover unrouted nets on a routed board "
                              "(incremental single-net router, DRC-guarded)")
    ppa.add_argument("--board", required=True)
    ppa.add_argument("--out", default=None)
    ppa.add_argument("--track", type=float, default=0.2)
    ppa.add_argument("--clearance", type=float, default=0.2)
    ppa.add_argument("--constraints", default=None)
    ppa.add_argument("--kicad-cli", default="kicad-cli")
    ppa.set_defaults(fn=cmd_patch)

    pvm = sub.add_parser("verify-models",
                         help="verify 3D models sit ON their pins/footprints; "
                              "--fix solves + writes correcting transforms")
    pvm.add_argument("--board", required=True)
    pvm.add_argument("--fix", action="store_true")
    pvm.add_argument("--tol", type=float, default=0.6,
                     help="max hole-to-pin distance mm (default 0.6)")
    pvm.add_argument("--out", default=None, help="output board (default: in place)")
    pvm.set_defaults(fn=cmd_verifymodels)

    psn = sub.add_parser("sync-nets",
                         help="make board pad nets agree with the schematic "
                              "netlist (headless update-from-schematic, nets only)")
    psn.add_argument("--board", required=True)
    psn.add_argument("--sch", required=True)
    psn.add_argument("--out", default=None, help="output board (default: in place)")
    psn.add_argument("--kicad-cli", default="kicad-cli")
    psn.set_defaults(fn=cmd_syncnets)

    prf = sub.add_parser("replace-footprint",
                         help="swap a ref's footprint for a real library one, "
                              "keep placement + schematic link, re-net by pad number")
    prf.add_argument("--board", required=True)
    prf.add_argument("--ref", required=True)
    prf.add_argument("--lib", required=True, help="path to the .pretty directory")
    prf.add_argument("--name", required=True, help="footprint name inside the lib")
    prf.add_argument("--sch", default=None,
                     help="root schematic: use its netlist as the pad-net truth")
    prf.add_argument("--rename", default=None,
                     help="vendor-pad:schematic-pin pairs, e.g. MP1:S1,MP2:S2")
    prf.add_argument("--out", default=None, help="output board (default: in place)")
    prf.add_argument("--kicad-cli", default="kicad-cli")
    prf.set_defaults(fn=cmd_replacefp)

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
    pau.add_argument("--floor", type=float, default=None,
                     help="fine-pitch escape floor mm (default: the fab profile's floor)")
    pau.add_argument("--constraints", default=None,
                     help="TOML constraint file: per-net currents/widths/pours, "
                          "per-pair skew limits (see fluxplace/constraints.py)")
    pau.add_argument("--profile", default="jlcpcb",
                     help="fabricator constraint profile: " + ", ".join(sorted(
                         __import__("fluxplace.profiles", fromlist=["PROFILES"]).PROFILES)))
    pau.add_argument("--no-finish", dest="finish", action="store_false",
                     help="skip the adaptive step-down anneal (one route pass only)")
    pau.add_argument("--no-fanout", action="store_true",
                     help="don't generate via-in-pad fanout for geometric residue")
    pau.add_argument("--no-pairs", action="store_true",
                     help="skip the coupled diff-pair pre-route stage")
    pau.add_argument("--no-patch", action="store_true",
                     help="skip the last-mile single-net patch stage")
    pau.add_argument("--keep-outline", action="store_true",
                     help="the source Edge.Cuts outline is a mechanical given: "
                          "place inside it, never regrow (doesn't-fit = loud FAIL)")
    pau.add_argument("--bypass-csv", default=None,
                     help="cap->component ownership CSV (e.g. Quilter export); "
                          "drives attachments so placement optimizes what the "
                          "grader grades")
    pau.add_argument("--no-finisher", action="store_true",
                     help="skip the freerouting last-mile finisher")
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
