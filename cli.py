#!/usr/bin/env python3
"""fluxplace CLI — headless signal-flow placement for KiCad boards.

  analyze  : print the communication map (hub, branches, forks, lint)
  place    : re-place components by topology, save the board
  eval     : report placement quality (weighted wirelength, overlaps, board size)

Run with KiCad's python so pcbnew is importable, e.g.:
  PYTHONPATH=/usr/lib/python3/dist-packages /usr/bin/python3 cli.py place \
      --board board.kicad_pcb --strategy flux --rotate ortho --out out.kicad_pcb
"""
import argparse, re, sys, os
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


def _sourcing_preflight(a, stage="placement"):
    """Ask the distributors BEFORE committing parts to copper.

    Placement is the point of no return for a part choice: once a footprint is
    placed, routed and DRC'd, an unbuyable part costs a re-layout, not a
    re-order. Advisory by default (a lead-time part is a schedule call);
    --strict-sourcing aborts on NONE/RISK verdicts."""
    if getattr(a, "no_sourcing", False):
        return
    from fluxplace import sourcing as S
    try:
        blockers = S.preflight(a.board,
                               mpn_map=getattr(a, "mpn_map", None),
                               need=getattr(a, "sourcing_need", 10),
                               refresh=getattr(a, "sourcing_refresh", False),
                               both=getattr(a, "sourcing_both", False),
                               log=lambda m: print(m, flush=True))
    except Exception as e:                      # never let sourcing block work
        print(f"    sourcing pre-flight skipped: {e}", flush=True)
        return
    if blockers and getattr(a, "strict_sourcing", False):
        raise SystemExit(
            f"ABORT before {stage}: {len(blockers)} part(s) cannot be bought "
            f"({', '.join(blockers)}). Fix the BOM or drop --strict-sourcing.")


def cmd_sourcing(a):
    """Standalone: grade every MPN against live DigiKey + Mouser stock."""
    from fluxplace import sourcing as S
    path = a.mpn_map or S.find_map(a.board)
    if not path:
        raise SystemExit("no mpn_map.json found (pass --mpn-map)")
    by_mpn = S.load_map(path)
    print(f"sourcing: {len(by_mpn)} MPNs from {path}")
    report, counts = S.check(by_mpn, need=a.sourcing_need,
                             cache_dir=os.path.dirname(path),
                             refresh=a.sourcing_refresh, both=a.sourcing_both)
    blockers = S.summary(report, counts, a.sourcing_need, show_ok=a.show_ok)
    if a.json:
        json_mod = __import__("json")
        json_mod.dump(report, open(a.json, "w"), indent=1)
        print(f"wrote {a.json}")
    raise SystemExit(1 if blockers else 0)


def cmd_deliver(a):
    """Split a fab package for its two audiences: a CAM-only zip for the
    fab's engineer, plus loose readable docs for whoever places the order."""
    from fluxplace import fab as F, fabdoc
    docs = list(a.doc or [])
    if a.brief:
        docs.append(a.brief)
        if not a.no_docx:
            out = os.path.join(os.path.dirname(a.brief) or ".",
                               (a.docx_name or os.path.splitext(
                                   os.path.basename(a.brief))[0] + ".docx"))
            made = fabdoc.render(a.brief, out, a.title or a.name,
                                 a.subtitle or "PCB Fabrication Submission")
            if made:
                docs.append(made)
                print(f"    brief -> {made}")
            else:
                print("    python-docx unavailable — markdown brief only")
    if a.no_pcbway:
        F.deliver(a.fab_dir, a.out, a.name, docs=docs, extras=(a.bom or []),
                  log=lambda m: print(m, flush=True))
        return

    # PCBWay's assembly page takes FOUR separate uploads — gerbers, BOM,
    # centroid, assembly documents — so emit one file per field, numbered to
    # match the page. The centroid comes OUT of the zip to get there.
    from fluxplace import pcbway
    base = re.sub(r"-fab$", "", a.name)
    slots = pcbway.slot_names(base)
    extras = []
    boms = list(a.bom or [])
    facts = pcbway.collect(board=a.board, fab_dir=a.fab_dir, boms=boms,
                           sourcing=a.sourcing_json, quantity=a.quantity,
                           name=a.name)
    facts.update(slots)
    # the board's own BOM is upload #2; anything else stays under its own name
    board_bom = next((b for b in boms
                      if os.path.basename(b) in (facts.get("bom_files") or [])), None)
    for b in boms:
        extras.append((b, slots["slot_bom"]) if b == board_bom else b)
    F.deliver(a.fab_dir, a.out, os.path.splitext(slots["slot_gerbers"])[0],
              docs=docs, extras=extras, centroid_name=slots["slot_centroid"],
              log=lambda m: print(m, flush=True))
    notes = ""
    if a.assembly_notes and os.path.exists(a.assembly_notes):
        notes = open(a.assembly_notes, encoding="utf-8").read()
    pcbway.write(a.out, facts, zip_name=slots["slot_gerbers"],
                 docx=not a.no_docx, title=a.title, assembly_extra=notes)
    # spreadsheet twins of the two CSV uploads — an upload widget that refuses
    # one CSV will take the .xlsx, and mid-order is the wrong time to find out
    for slot in ("slot_bom", "slot_centroid"):
        src = os.path.join(a.out, slots[slot])
        if os.path.exists(src):
            alt = pcbway.to_xlsx(src, os.path.splitext(src)[0] + ".xlsx")
            if alt:
                print(f"    alt format -> {os.path.basename(alt)}")


def cmd_pcbway(a):
    """The PCBWay quote form, answered from the board file wherever the design
    already decides the answer."""
    from fluxplace import pcbway
    facts = pcbway.collect(board=a.board, fab_dir=a.fab_dir, boms=(a.bom or []),
                           sourcing=a.sourcing_json, quantity=a.quantity,
                           name=a.name)
    pcbway.write(a.out_dir, facts, zip_name=a.zip_name, docx=not a.no_docx)


def cmd_place(a):
    _sourcing_preflight(a, "placement")
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
    print(f"pin density: {pin_density(board):.1f}% "
          f"(pad copper / board area; Quilter refuses >20%)")
    if getattr(a, "prc", False):
        from fluxplace import comprehend as CM, prc as PR
        comp = CM.comprehend(CM.pads_from_board(board))
        angles = {r: parts[r].get("angle0", 0.0) for r in parts}
        rows, _, _ = PR.score(parts, pos, angles, comp)
        PR.summarize(rows, failed_only=getattr(a, "failed_only", False))


def pin_density(board):
    """Quilter's input-complexity metric: component pad area / board area x100.
    Their guidance: designs over 20% are not recommended for auto-layout —
    use this to compare density targets across boards on a common scale."""
    import pcbnew
    pad_area = 0.0
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            s = pad.GetSize()
            pad_area += (s.x / 1e6) * (s.y / 1e6)
    bb = board.GetBoardEdgesBoundingBox()
    w = bb.GetWidth() / 1e6
    h = bb.GetHeight() / 1e6
    if w <= 0 or h <= 0:
        return 0.0
    return 100.0 * pad_area / (w * h)


def cmd_comprehend(a):
    import json as _json
    from fluxplace import comprehend as CM, prc as PR
    board, parts, nets, IO = _load(a.board)
    comp = CM.comprehend(CM.pads_from_board(board))
    CM.summarize(comp)
    if a.prc:
        pos = {r: (parts[r]["x"], parts[r]["y"]) for r in parts}
        angles = {r: parts[r].get("angle0", 0.0) for r in parts}
        rows, _, _ = PR.score(parts, pos, angles, comp)
        PR.summarize(rows, failed_only=a.failed_only)
    if a.json:
        _json.dump(comp, open(a.json, "w"), indent=1)
        print(f"wrote: {a.json}")


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
    _sourcing_preflight(a, "the tournament")
    from fluxplace import tournament as TN
    jar = a.jar or os.environ.get("FREEROUTING_JAR")
    if not jar:
        # 2.3.0+ required: 2.2.4 dies silently on headless jobs (NEXT.md)
        cand = os.path.expanduser("~/tools/freerouting-2.3.0.jar")
        jar = cand if os.path.exists(cand) else None
    if not jar or not os.path.exists(jar):
        print("need --jar or $FREEROUTING_JAR (freerouting 2.3.0+; "
              "2.2.4 dies silently headless)")
        return
    grid = TN.parse_compact_grid(a.compact_grid) if a.compact_grid else None
    planes = None
    if a.plane_nets:
        planes = tuple(tuple(p.split("=", 1)) for p in a.plane_nets)
    results, winner = TN.run(a.board, jar, a.workdir, passes=a.passes, jobs=a.jobs,
                             resume=a.resume, oit=a.oit, compact_grid=grid,
                             obstacles=list(a.obstacle), plane_nets=planes,
                             profiles=a.profiles)
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
    if a.route_only:
        v += ["--route-only"]
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


def cmd_comprehend_intent(a):
    """Circuit comprehension, si.py-backed: infer the electrical-intent tables
    (power classes, diff pairs, bypass cap -> pin, crystal -> parent) and emit
    them as TOML for review. The reviewed numbers belong in constraints.toml,
    which always wins.

    This is the origin/main lineage of `comprehend`, kept alongside ours after
    the 2026-08-19 merge: it produces a different artifact (TOML intent tables
    from comprehend_si) than `comprehend` (JSON physics constraints + PRC
    grading), and both are used.
    """
    from fluxplace import comprehend_si as CO
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
    from fluxplace import profiles as PROF
    prof = PROF.get(a.profile)
    PROF.write_pro_limits(os.path.splitext(a.board)[0] + ".kicad_pro", prof)
    cons = CONS.load(a.constraints)
    nw = {n: CONS.power_width_mm(cons, n, 0.5)
          for n in (cons or {}).get("power", {})}
    res = PATCH.patch_board(a.board, a.out or a.board, kicad_cli=a.kicad_cli,
                            track_w=a.track, clearance=a.clearance,
                            via_mm=a.via, drill_mm=a.drill, cell=a.cell,
                            net_widths=nw, rip=not a.no_rip,
                            rip_r_mm=a.rip_radius, max_rip=a.max_rip,
                            checkpoint=a.checkpoint)
    print(f"patch: accepted={res['accepted']} patched={res['patched']} "
          f"failed={res['failed']}")


def cmd_launder(a):
    """Apply profile limits to the board's setup constraints, then delete
    the parasitic copper KiCad's own DRC names (dangling stubs, shorting
    stitch vias, vias against holes). Guarded per round."""
    import gc
    import pcbnew
    from fluxplace import launder as LAU, profiles as PROF
    prof = PROF.get(a.profile)
    b = pcbnew.LoadBoard(a.board)
    PROF.apply_board_limits(b, prof, pcbnew)
    out = a.out or a.board
    pcbnew.SaveBoard(out, b)
    del b
    gc.collect()
    PROF.write_pro_limits(os.path.splitext(out)[0] + ".kicad_pro", prof)
    print(f"rules: setup constraints set to [{a.profile}] "
          f"(track {prof['track_min']}, via {prof['via_dia_min']}/"
          f"{prof['via_drill_min']}, hole {prof['hole_clearance']}) "
          f"— board + project file")
    res = LAU.launder_board(out, out, kicad_cli=a.kicad_cli, prof=prof)
    print(f"launder: rounds={res['rounds']} removed={res['removed']} "
          f"violations {res['violations'][0]}->{res['violations'][1]} "
          f"unconnected {res['unconnected'][0]}->{res['unconnected'][1]}")


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
    _sourcing_preflight(a, "the auto pipeline")
    if getattr(a, "candidates", 1) > 1:
        return cmd_auto_candidates(a)
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
    try:
        r = subprocess.run(cmd, cwd=a.router_dir, capture_output=True, text=True,
                           timeout=a.route_timeout)
    except subprocess.TimeoutExpired:
        r = None
        print(f"    ! router timed out after {a.route_timeout}s — continuing "
              "with whatever it saved")
    ok = os.path.exists(routed)
    if not ok and r is not None:
        print("    router stderr:", (r.stderr or r.stdout or "")[-240:].replace("\n", " "))
    summ = ""
    for line in ((r.stdout if r else "") or "").splitlines():
        if line.startswith("JSON_SUMMARY:"):
            try:
                js = json.loads(line[13:])
                summ = (f"  {js.get('successful',0)} ok / {js.get('failed',0)} failed"
                        f", pairs {js.get('pad_pairs_connected','?')}/{js.get('pad_pairs_total','?')}")
            except Exception:
                pass
    # KRT's own summary is unreliable (observed: "1 ok, pairs 2/2" on a fully
    # connected board) — report the board's actual ratsnest instead.
    real = ""
    if ok:
        try:
            rb = pcbnew.LoadBoard(routed)
            rb.BuildConnectivity()
            real = f"  actual-unconnected={rb.GetConnectivity().GetUnconnectedCount(True)}"
        except Exception:
            pass
    print(f"[4/{NSTAGE}] routed {len(route_nets)} nets{summ}{real}  ({time.time()-t0:.0f}s)")
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
        try:
            subprocess.run(cmd, cwd=a.router_dir, capture_output=True, text=True,
                           timeout=a.route_timeout)
        except subprocess.TimeoutExpired:
            print(f"    ! plane-finalize route timed out after {a.route_timeout}s"
                  " — proceeding to DFM/fab with the main-route board")
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
    _sourcing_preflight(a, "compaction")
    from fluxplace import compact as C
    os.makedirs(a.out, exist_ok=True)
    board, parts, nets, IO = _load(a.board)
    if getattr(a, "quilter_contract", False):
        nl, nf = IO.quilter_contract(board, parts)
        print(f"    quilter contract: {nl} inside outline -> LOCKED, "
              f"{nf} outside -> free")
    # compact REROUTES: stale copper from the source board poisons planes
    # (pour sees existing zones -> 0 pours) and DRC (old tracks short the
    # new route). Strip tracks/vias/zones; refs held until exit (SWIG GC).
    # Quilter model: pours are deleted+regenerated UNLESS explicitly named
    # (--preserve-pour); --keep-copper preserves tracks/vias so the router
    # extends partial routes instead of starting over.
    preserve = set(getattr(a, "preserve_pour", []) or [])
    _keep = []
    if not getattr(a, "keep_copper", False):
        for t in list(board.GetTracks()):
            _keep.append(t)
            board.Remove(t)
    kept_pours = 0
    for z in list(board.Zones()):
        try:
            zname = z.GetZoneName() or ""
            rule_area = z.GetIsRuleArea()
        except Exception:
            zname, rule_area = "", False
        if rule_area:
            continue                     # rule areas are constraints, not copper
        if zname and zname in preserve:
            kept_pours += 1
            continue                     # named + registered -> preserved
        _keep.append(z)
        board.Remove(z)
    cmd_compact._keep = _keep
    if _keep:
        print(f"    stripped {len(_keep)} stale tracks/vias/zones from source"
              + (f" (preserved {kept_pours} named pours)" if kept_pours else ""))
    obstacles = C.parse_obstacles(a.obstacle)

    # Rule Areas (Quilter's KiCad convention): keepout-flagged -> obstacle;
    # named + no keepout items -> hard placement region
    regions = []
    if a.rule_areas:
        ra_regions, ra_keepouts = IO.read_rule_areas(board)
        obstacles += ra_keepouts
        members_spec = {}
        for spec in a.region or []:
            name, _, refs = spec.partition("=")
            members_spec[name] = ("auto" if refs in ("", "auto")
                                  else [r.strip() for r in refs.split(",")])
        regions = C.assign_regions(parts, ra_regions, members_spec)
        for rg in regions:
            print(f"    region '{rg['name']}' [{rg['side']}]: "
                  f"{len(rg['members'])} members")
        if ra_keepouts:
            print(f"    {len(ra_keepouts)} keepout rule areas -> obstacles")

    # side exploration: flip chosen small parts to the back, judged by the
    # router downstream — never assumed good
    if a.flip and a.flip != "none":
        from fluxplace import comprehend as CM
        comp = CM.comprehend(CM.pads_from_board(board))
        flips = C.pick_flips(parts, a.flip, obstacles=obstacles, comp=comp)
        n = IO.flip_footprints(board, set(flips))
        print(f"    side exploration [{a.flip}]: flipped {n} parts to back")
        parts, nets = IO.read_board(board)   # sides/pins changed — re-read

    anchor = None
    if a.anchor:
        if a.anchor not in parts:
            raise SystemExit(f"--anchor {a.anchor}: no such ref")
        anchor = (parts[a.anchor]["x"], parts[a.anchor]["y"])

    bounds = None
    if a.outline:
        try:
            ow, oh = [float(v) for v in a.outline.split(":")]
        except ValueError:
            raise SystemExit(f"--outline {a.outline}: want W:H in mm")
        # hard outline centered on the anchor (locked centroid by default)
        if anchor is None:
            lk = [p for p in parts.values() if p.get("locked")]
            src = lk if lk else list(parts.values())
            anchor_c = (sum(p["x"] for p in src) / len(src),
                        sum(p["y"] for p in src) / len(src))
        else:
            anchor_c = anchor
        bounds = (anchor_c[0] - ow / 2, anchor_c[1] - oh / 2,
                  anchor_c[0] + ow / 2, anchor_c[1] + oh / 2)
        print(f"    hard outline: {ow:.0f}x{oh:.0f} mm centered "
              f"({anchor_c[0]:.1f}, {anchor_c[1]:.1f})")

    camap = C.cluster_anchor_map(parts) if a.cluster_anchors else None
    if camap:
        print(f"    cluster anchors: {len(camap)} parts stick to their "
              f"cluster's locked centroid")

    if not a.no_prc_seed:
        from fluxplace import comprehend as CM
        comp = CM.comprehend(CM.pads_from_board(board))
        n = C.constraint_seed(parts, comp, obstacles=obstacles)
        if n:
            print(f"    PRC seed: walked {n} constraint members next to "
                  f"their anchor (hot loops, pair elements, crystals)")

    t0 = time.time()
    pos, st = C.compact(parts, a.sx, a.sy, anchor=anchor, gap=a.gap,
                        pack=a.pack, obstacles=obstacles,
                        tht_bands=a.tht_bands, regions=regions,
                        bounds=bounds, cluster_anchors=camap)
    IO.apply_positions(board, pos, parts, skip_locked=True)
    x0, y0, x1, y1 = st["extent"]
    print(f"[1/6] compacted sx={a.sx} sy={a.sy}: {st['nudges']} nudges/"
          f"{st['iters']} iters, {st['hard']} hard-relocated, "
          f"{st['resid']} residual overlaps, extent {x1-x0:.1f}x{y1-y0:.1f} mm"
          f"  ({time.time()-t0:.0f}s)")
    if st.get("outside"):
        # Quilter model: hard constraints fail LOUDLY, with diagnostics —
        # never a silently-spread board
        raise SystemExit(
            f"compact: {st['outside']} parts could not satisfy their hard "
            f"region/outline constraints. The constraint set is infeasible "
            f"at this density — grow --outline, loosen --sx/--sy, or free "
            f"some region members.")
    if st["resid"] and not a.allow_overlaps:
        raise SystemExit(
            f"compact: {st['resid']} residual overlaps after convergence — "
            "NOT routing a physically broken placement (relax --sx/--sy/--gap, "
            "or pass --allow-overlaps to proceed anyway)")
    if st["resid"]:
        print("    ! residual overlaps — proceeding under --allow-overlaps")
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


def cmd_models(a):
    """Fetch real manufacturer STEP models (DigiKey /media, Mouser assist)
    for footprints with missing/broken 3D models, wire them in, save."""
    import json as _json
    from fluxplace import models as M
    from fluxplace import kicad_io as IO
    board = IO.load(a.board)
    if a.audit_only:
        todo = M.audit_board(board)
        for fp, why in todo:
            print(f"  {fp.GetReference():6s} {why:12s} "
                  f"{str(fp.GetFPID().GetLibItemName())[:50]}")
        print(f"models: {len(todo)} footprints need a model")
        return
    mpn_map = {}
    if a.map:
        mpn_map.update(_json.load(open(a.map)))
    for spec in a.mpn or []:
        ref, _, mpn = spec.partition("=")
        mpn_map[ref] = mpn
    rep = M.sync(board, mpn_map, a.models_dir, path_prefix=a.path_prefix,
                 force=getattr(a, "force", False))
    board.Save(a.out or a.board)
    print(f"models: {len(rep['fetched'])} fetched, {len(rep['cached'])} cached, "
          f"{len(rep['failed'])} failed, {len(rep['skipped'])} unmapped "
          f"-> {a.out or a.board}")


def cmd_intake(a):
    """Interactive design intake: connectors (on-board type, edge vs remote,
    panel default), mounting holes. Writes design_intent.json; optionally
    applies corner mounting holes to a board that already has its outline."""
    import json as _json
    from fluxplace import intake as IN
    answers = _json.load(open(a.answers)) if a.answers else None
    intent = IN.run(answers=answers)
    with open(a.out, "w") as fh:
        _json.dump(intent, fh, indent=1)
    print(f"intent -> {a.out} ({len(intent['interfaces'])} interfaces, "
          f"mounting={intent['mounting']})")
    if a.apply_board:
        from fluxplace import kicad_io as IO
        board = IO.load(a.apply_board)
        refs = IN.apply_mounting(board, intent)
        if refs:
            IO.save(board, a.apply_board)
            print(f"applied {len(refs)} mounting holes -> {a.apply_board}")


# --------------------------------------------------------------------------- doctor
def cmd_doctor(a):
    """Preflight the whole suite and optionally install what pip can fix.

    This exists because fluxplace drives KiCad, a router, an office suite and
    two distributor APIs, and for its first 67 commits it declared none of that.
    A user's first run should tell them what is missing, not die inside stage 4.
    """
    from fluxplace import deps
    res = deps.check_all()
    if a.json:
        import json as _json
        print(_json.dumps(res, indent=2))
    else:
        print(deps.report(res, show_ok=not a.problems_only))
    if a.install:
        pips = deps.pip_installable(res)
        if pips:
            deps.install(pips)
            print()
            print(deps.report(deps.check_all(), show_ok=False))
    elif a.interactive:
        deps.prompt_and_install()
    # exit non-zero only when the suite genuinely cannot run
    import sys as _sys
    if deps.blocking(deps.check_all()):
        _sys.exit(1)


def _require(*tiers):
    """Guard for commands with hard prerequisites: fail with a readable preflight
    instead of an ImportError three stages into a pipeline."""
    from fluxplace import deps
    miss = deps.missing(tiers=tiers or None)
    if miss:
        print("fluxplace: missing requirements for this command\n")
        for m in miss:
            print("  MISS %-30s %s" % (m["label"], m["detail"]))
            if m["hint"]:
                print("       %s" % m["hint"])
        print("\nRun `fluxplace doctor --install` to fix what pip can.")
        import sys as _sys
        _sys.exit(1)


# ------------------------------------------------------------------- audit
def cmd_drc_scope(a):
    """What a DRC result actually examined.

    Born from an outside review: a board signed off as "0 violations at all
    severities" had 13 of 62 rules set to ignore, including solder-mask bridging
    and annular width. A rule set to ignore is not reported at ANY severity, so
    the clean result was true and narrow at once — and nothing said so.
    """
    from fluxplace import audit
    import json as _json
    res = audit.drc_scope(a.board, a.report)
    if a.full:
        res["full_run"] = audit.drc_full(a.board, kicad_cli=a.kicad_cli,
                                         out_path=a.out)
    if a.json:
        print(_json.dumps(res, indent=2)); return

    print("DRC scope for %s" % os.path.basename(res["board"]))
    print("=" * 70)
    print("  rules total   : %s" % res["rules_total"])
    print("  evaluated     : %s" % res["rules_active"])
    print("  IGNORED       : %s" % res["rules_ignored"])
    if res["ignored"]:
        for k in res["ignored"]:
            print("      - %s" % k)
    if res["fab_critical_ignored"]:
        print()
        print("  FAB-CRITICAL checks that are switched off:")
        for c in res["fab_critical_ignored"]:
            print("      %s" % c["check"])
            print("          %s" % c["why_it_matters"])
    if "report" in res and "violations" in res.get("report", {}):
        r = res["report"]
        print()
        print("  report %s: %s violations, %s unconnected"
              % (os.path.basename(r["path"]), r["violations"], r["unconnected"]))
    print()
    print("  VERDICT: %s" % res["verdict"])
    if "full_run" in res:
        f = res["full_run"]
        print()
        print("  Full re-run with every check enabled:")
        if "error" in f:
            print("      error: %s" % f["error"])
        else:
            print("      enabled %d previously-ignored check(s)"
                  % len(f["enabled_for_this_run"]))
            print("      %s violations, %s unconnected"
                  % (f["violations"], f["unconnected"]))
            if f["newly_surfaced"]:
                print("      NEWLY SURFACED by the previously-ignored checks:")
                for k, n in sorted(f["newly_surfaced"].items()):
                    print("          %-32s %d" % (k, n))
            else:
                print("      nothing new surfaced — the clean result holds at full scope")


def cmd_netlist(a):
    """Read connectivity back out of the board.

    For a board generated from a netlist spec there is no .kicad_sch at all;
    this is the only connectivity document that exists. Even with a schematic,
    this is what the copper says.
    """
    from fluxplace import audit
    import json as _json
    if getattr(a, "summary", False):
        print(_json.dumps(audit.netlist_summary(a.board), indent=2)); return
    res = audit.netlist(a.board, fmt="json" if a.json else "text")
    text = _json.dumps(res, indent=2) if a.json else res
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text + "\n")
        print("wrote %s" % a.out)
    else:
        print(text)


def cmd_stackup(a):
    """Report the layer stack, plane assignment and netclass geometry — and say
    plainly whether controlled impedance can be verified from these files."""
    from fluxplace import audit
    import json as _json
    res = audit.stackup(a.board)
    if a.json:
        print(_json.dumps(res, indent=2)); return
    print("Stackup for %s" % os.path.basename(res["board"]))
    print("=" * 70)
    print("  copper layers    : %d" % res["copper_layers"])
    for l in res["layers"]:
        planes = res["plane_layers"].get(l["name"])
        note = ("  <- pour: %s" % ", ".join(planes)) if planes else ""
        print("      %-8s%s" % (l["name"], note))
    print("  board thickness  : %s mm" % res["board_thickness_mm"])
    print("  stackup defined  : %s" % res["stackup_defined"])
    print("  dielectric/Er    : %s" % res["dielectric_defined"])
    if res["netclasses"]:
        print()
        print("  netclasses:")
        for c in res["netclasses"]:
            print("      %-12s track=%s clr=%s dp=%s/%s"
                  % (c["name"], c["track_width"], c["clearance"],
                     c["diff_pair_width"], c["diff_pair_gap"]))
    for pat in res["netclass_patterns"]:
        print("      pattern %s -> %s" % (pat.get("pattern"), pat.get("netclass")))
    print()
    print("  IMPEDANCE VERIFIABLE: %s" % ("yes" if res["impedance_verifiable"] else "NO"))
    for line in _wrap_text(res["impedance_note"], 66):
        print("      %s" % line)


def _wrap_text(text, width):
    words, line, lines = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            lines.append(line); line = w
        else:
            line = (line + " " + w).strip()
    if line:
        lines.append(line)
    return lines


def cmd_stackup_apply(a):
    """Define the board's stackup from a named fab profile, and/or solve the
    trace geometry the stackup implies."""
    from fluxplace import stackup as ST
    import json as _json

    if a.list_profiles:
        for n in ST.profile_names():
            p = ST.PROFILES[n]
            print("%-18s %-8s %5.2f mm  %s"
                  % (n, p["vendor"], ST.total_thickness(n), p["description"]))
        return

    if not a.profile:
        print("need --profile (or --list-profiles). Available: %s"
              % ", ".join(ST.profile_names()))
        return

    res = {"profile": a.profile}
    if a.apply:
        res["apply"] = ST.apply_to_board(a.board, a.profile,
                                         backup=not a.no_backup, replace=a.replace)
    if a.traces or a.nets:
        res["traces"] = ST.check_traces(
            a.board, a.profile,
            nets=[n.strip() for n in a.nets.split(",")] if a.nets else None,
            target_z=a.target_se, tolerance=a.tolerance)
    res["check"] = ST.check_netclasses(a.board, a.profile,
                                       target_se=a.target_se,
                                       target_diff=a.target_diff,
                                       tolerance=a.tolerance)
    if a.json:
        print(_json.dumps(res, indent=2)); return

    ck = res["check"]
    print("Stackup: %s" % a.profile)
    print("  %s" % ST.PROFILES[a.profile]["description"])
    print("  total thickness      : %.3f mm" % ST.total_thickness(a.profile))
    print("  outer dielectric     : %.4f mm, Er %.2f"
          % (ck["dielectric_height_mm"], ck["epsilon_r"]))
    print("  outer copper         : %.4f mm" % ck["copper_thickness_mm"])
    if "apply" in res:
        ap = res["apply"]
        if ap.get("changed"):
            print("  WRITTEN to the board (backup: %s)" % ap.get("backup"))
        else:
            print("  NOT written: %s" % ap.get("reason"))
    print()
    print("Geometry this stackup implies:")
    r = ck["recommended"]
    print("  %.0f ohm single-ended : %s mm track"
          % (ck["target_single_ended"], r["single_ended_width_mm"]))
    print("  %.0f ohm differential : %s mm track / %s mm gap"
          % (ck["target_differential"], r["diff_pair_width_mm"], r["diff_pair_gap_mm"]))
    print()
    print("Netclasses as they stand (assumed outer-layer microstrip):")
    for row in ck["netclasses"]:
        print("  %s" % row["netclass"])
        if row.get("single_ended_z") is not None:
            flag = "OK " if row.get("single_ended_ok") else "OFF"
            print("      track %s mm -> %5.1f ohm  (%+.0f%% vs %.0f)  [%s]"
                  % (row["track_width"], row["single_ended_z"],
                     row["single_ended_error_pct"], ck["target_single_ended"], flag))
        if row.get("differential_z") is not None:
            flag = "OK " if row.get("differential_ok") else "OFF"
            print("      diff %s/%s mm -> %5.1f ohm  (%+.0f%% vs %.0f)  [%s]"
                  % (row["diff_pair_width"], row["diff_pair_gap"],
                     row["differential_z"], row["differential_error_pct"],
                     ck["target_differential"], flag))
    if "traces" in res:
        tr = res["traces"]
        print()
        print("Routed copper on RF-named nets (target %.0f ohm, needs %s mm):"
              % (tr["target_z"], tr["required_width_mm"]))
        for r in tr["nets"]:
            flag = "OFF" if (r["worst_error_pct"] or 0) > tr["tolerance_pct"] else "OK "
            extra = []
            if r["vias"]:
                extra.append("%d via%s" % (r["vias"], "" if r["vias"] == 1 else "s"))
            if r["crosses_inner_layers"]:
                extra.append("crosses inner layers")
            print("  [%s] %-13s %6.1f mm  %s%s"
                  % (flag, r["net"], r["total_length_mm"],
                     "/".join(str(w["width_mm"]) for w in r["widths"]) + " mm",
                     ("  (" + ", ".join(extra) + ")") if extra else ""))
            for w in r["widths"]:
                if w["microstrip_z"] is not None:
                    print("           %.4f mm -> %5.1f ohm (%+.0f%%)"
                          % (w["width_mm"], w["microstrip_z"], w["error_pct"]))
        print()
        print("  %s" % tr["verdict"])
    print()
    for line in _wrap_text(ck["caveat"], 66):
        print("  %s" % line)


def cmd_schematic(a):
    """Generate a .kicad_sch from the netlist spec (or from the copper), then
    prove it by exporting its netlist and diffing against the source."""
    from fluxplace import schematic as SC
    import json as _json
    res = SC.generate(a.spec or a.board, a.out, title=a.title,
                      from_copper=bool(a.board and not a.spec))
    print("wrote %s" % res["path"])
    print("  %d components, %d nets, %d pins, %.0f x %.0f mm, %d column(s)"
          % (res["components"], res["nets"], res["pins"],
             res["sheet_mm"][0], res["sheet_mm"][1], res["columns"]))
    if a.spec and not a.no_verify:
        v = SC.verify(a.out, a.spec, kicad_cli=a.kicad_cli)
        print()
        print("  VERIFY: %s" % v.get("verdict", v.get("error")))
        if v.get("ok"):
            print("    %d nets / %d pins in both" % (v["nets_in_spec"], v["pins_in_spec"]))
        else:
            for k in ("missing_nets", "extra_nets"):
                if v.get(k): print("    %s: %s" % (k, v[k][:8]))
            for d in v.get("differing_nets", [])[:5]: print("    DIFF %s" % d)
        if a.json: print(_json.dumps(v, indent=2))


def build_parser():
    """Build the full argument parser.

    Split out of main() so the MCP server can introspect the REAL parser rather
    than maintain a parallel description of the commands. One source of truth:
    add a flag here and the MCP tool schema gains it automatically.
    """
    ap = argparse.ArgumentParser(prog="fluxplace")
    ap.add_argument("--big-fanout", type=int, default=12,
                    help="signal nets with >= this many nodes are treated as planes")
    ap.add_argument("--hub", default=None, help="force a specific ref as the hub")
    ap.add_argument("--mpn-map", default=None,
                    help="ref->MPN json for the sourcing pre-flight "
                         "(auto-discovered next to the board if omitted)")
    ap.add_argument("--sourcing-need", type=int, default=10,
                    help="units that must be in stock to pass (default 10)")
    ap.add_argument("--strict-sourcing", action="store_true",
                    help="ABORT placement when a part is unbuyable (NONE/RISK)")
    ap.add_argument("--no-sourcing", action="store_true",
                    help="skip the pre-flight entirely")
    ap.add_argument("--sourcing-refresh", action="store_true",
                    help="ignore the 24h stock cache")
    ap.add_argument("--sourcing-both", action="store_true",
                    help="always query BOTH distributors (default: Mouser is "
                         "only asked when DigiKey has not already settled the "
                         "part — saves Mouser's ~30 calls/min quota)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("deliver",
                        help="split a fab package: CAM-only zip + loose docs "
                             "for the person ordering")
    pd.add_argument("--fab-dir", required=True, help="an emit() output dir")
    pd.add_argument("--out", required=True, help="delivery folder to create")
    pd.add_argument("--name", required=True, help="zip basename")
    pd.add_argument("--brief", help="submission brief .md (a .docx is "
                                    "generated beside it)")
    pd.add_argument("--docx-name", help="override the generated .docx name")
    pd.add_argument("--title"); pd.add_argument("--subtitle")
    pd.add_argument("--no-docx", action="store_true")
    pd.add_argument("--doc", action="append",
                    help="extra human-facing file, repeatable (README etc.)")
    pd.add_argument("--bom", action="append",
                    help="BOM/commercial file kept OUT of the zip, repeatable")
    pd.add_argument("--board", help="the .kicad_pcb, so the PCBWay order "
                                    "worksheet can be filled from the design")
    pd.add_argument("--quantity", type=int, help="board qty, pre-filled on the "
                                                 "worksheet")
    pd.add_argument("--sourcing-json", help="a `fluxplace sourcing --json` "
                                            "report; its non-OK parts become "
                                            "the consign list")
    pd.add_argument("--no-pcbway", action="store_true",
                    help="skip the PCBWay four-slot layout and worksheet; emit "
                         "a single CAM zip the old way")
    pd.add_argument("--assembly-notes", help="markdown file appended verbatim "
                                             "to the assembly-instructions doc "
                                             "(upload slot 4)")
    pd.set_defaults(fn=cmd_deliver)

    pw = sub.add_parser("pcbway",
                        help="PCBWay order worksheet: every field on the quote "
                             "form, pre-filled from the board")
    pw.add_argument("--board", help=".kicad_pcb to measure")
    pw.add_argument("--fab-dir", help="an emit() output dir (for place/pos.csv)")
    pw.add_argument("--bom", action="append", help="assembly BOM csv, repeatable")
    pw.add_argument("--sourcing-json")
    pw.add_argument("--quantity", type=int)
    pw.add_argument("--name", help="worksheet name (default: the board's)")
    pw.add_argument("--zip-name", help="the CAM zip the buyer uploads")
    pw.add_argument("--out-dir", required=True)
    pw.add_argument("--no-docx", action="store_true")
    pw.set_defaults(fn=cmd_pcbway)

    ps = sub.add_parser("sourcing",
                        help="grade every MPN against live DigiKey+Mouser stock")
    ps.add_argument("--board", required=True)
    ps.add_argument("--json", help="write the full report here")
    ps.add_argument("--show-ok", action="store_true",
                    help="list every part, not just the problems")
    ps.set_defaults(fn=cmd_sourcing)

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
    pt.add_argument("--compact-grid", default=None,
                    help="seed candidates by COMPACTING the current placement: "
                         "'sx:sy[:gap[:pack[:flip]]],...' (replaces the placer "
                         "grid; flip = none|decaps|passives side exploration)")
    pt.add_argument("--profiles", nargs="+", default=None,
                    help="compile-target rule profiles to sweep "
                         "(jlc-fine jlc-std osh-6mil); default jlc-fine")
    pt.add_argument("--obstacle", action="append", default=[],
                    help="keep-out rect X:Y:W:H[:F|B] for compact candidates")
    pt.add_argument("--plane-nets", nargs="+", default=None,
                    help="candidate plane pours as Layer=Net pairs, e.g. "
                         "In1.Cu=GND In2.Cu=+5V (default: GND + VIN_PROT)")
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

    pci = sub.add_parser("comprehend-intent",
                         help="infer electrical intent as TOML: pairs, bypass, "
                              "crystals, power (si.py-backed)")
    pci.add_argument("--board", required=True)
    pci.add_argument("--out", default=None, help="write TOML here (default: stdout)")
    pci.set_defaults(fn=cmd_comprehend_intent)

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
    ppa.add_argument("--via", type=float, default=0.6)
    ppa.add_argument("--drill", type=float, default=0.3)
    ppa.add_argument("--cell", type=float, default=0.25)
    ppa.add_argument("--no-rip", action="store_true",
                     help="disable regional rip-up-and-reroute at walled "
                          "islands")
    ppa.add_argument("--rip-radius", type=float, default=3.0,
                     help="halo (mm) of foreign copper freed along the "
                          "blocked corridor")
    ppa.add_argument("--max-rip", type=int, default=150)
    ppa.add_argument("--checkpoint", type=int, default=8,
                     help="guard-accept every N nets (crash loses one "
                          "chunk, not the run)")
    ppa.add_argument("--constraints", default=None)
    ppa.add_argument("--profile", default="jlcpcb-advanced",
                     help="fab profile stamped into the working "
                          ".kicad_pro so guard DRC judges at the "
                          "process floor")
    ppa.add_argument("--kicad-cli", default="kicad-cli")
    ppa.set_defaults(fn=cmd_patch)

    pla = sub.add_parser("launder",
                         help="set board constraints to the fab profile "
                              "floor + delete DRC-named parasitic copper")
    pla.add_argument("--board", required=True)
    pla.add_argument("--out", default=None)
    pla.add_argument("--profile", default="jlcpcb-advanced")
    pla.add_argument("--kicad-cli", default="kicad-cli")
    pla.set_defaults(fn=cmd_launder)

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
    pau.add_argument("--route-only", action="store_true",
                     help="keep the existing placement (RF-board mode): "
                          "route + patch + fab only")
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
    pco.add_argument("--allow-overlaps", action="store_true",
                     help="route even if legalization left overlaps (default: abort)")
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
    pco.add_argument("--rule-areas", action="store_true",
                     help="read board Rule Areas (Quilter convention: named + "
                          "no keepout items = placement REGION, keepout items "
                          "checked = obstacle)")
    pco.add_argument("--region", action="append", default=[],
                     help="region membership NAME=REF1,REF2 or NAME=auto "
                          "(parts inside the area), repeatable; needs "
                          "--rule-areas")
    pco.add_argument("--outline", default=None,
                     help="HARD outline W:H mm centered on the anchor — fails "
                          "loudly if infeasible instead of spreading")
    pco.add_argument("--cluster-anchors", action="store_true",
                     help="parts stick to their schematic-sheet cluster's "
                          "locked centroid instead of the global anchor")
    pco.add_argument("--flip", choices=["none", "decaps", "passives"],
                     default="none",
                     help="side exploration: move small parts to the BACK "
                          "before compacting (decaps = bypass caps only)")
    pco.add_argument("--quilter-contract", action="store_true",
                     help="Quilter I/O contract: parts inside the outline = "
                          "locked, outside = free to place")
    pco.add_argument("--preserve-pour", action="append", default=[],
                     help="zone NAME to preserve instead of strip+regenerate "
                          "(Quilter preserved-pours table), repeatable")
    pco.add_argument("--keep-copper", action="store_true",
                     help="keep existing tracks/vias (router extends partial "
                          "routes instead of starting over)")
    pco.add_argument("--no-prc-seed", action="store_true",
                     help="skip the physics pre-seed (hot-loop members, pair "
                          "elements, crystal clusters walked to their anchor)")
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

    pmo = sub.add_parser("models",
                         help="fetch real vendor STEP models (DigiKey/Mouser "
                              "APIs) for footprints missing 3D models")
    pmo.add_argument("--board", required=True)
    pmo.add_argument("--out", default=None, help="save here (default: in place)")
    pmo.add_argument("--models-dir", default="3dmodels",
                     help="directory to store fetched .step files")
    pmo.add_argument("--map", default=None,
                     help="JSON file {ref: MPN} for parts needing models")
    pmo.add_argument("--mpn", action="append", default=[],
                     help="inline mapping REF=MPN, repeatable")
    pmo.add_argument("--path-prefix", default=None,
                     help="write model paths as PREFIX/name.step (e.g. "
                          "'${KIPRJMOD}/../lib/3dmodels') instead of absolute")
    pmo.add_argument("--audit-only", action="store_true",
                     help="just list footprints with missing/broken models")
    pmo.add_argument("--force", action="store_true",
                     help="every mapped ref gets an authoritative fetch and "
                          "the REAL model replaces any attached stand-in")
    pmo.set_defaults(fn=cmd_models)

    pin = sub.add_parser("intake",
                         help="design interview: connectors (edge/remote + "
                              "panel defaults), mounting -> design_intent.json")
    pin.add_argument("--out", default="design_intent.json")
    pin.add_argument("--answers", default=None,
                     help="JSON with pre-filled answers (non-interactive)")
    pin.add_argument("--apply-board", default=None,
                     help="board to add corner mounting holes to now")
    pin.set_defaults(fn=cmd_intake)

    pc = sub.add_parser("calibrate",
                        help="ground-truth the gate vs freerouting (DSN export / .ses parse)")
    pc.add_argument("--board", required=True)
    pc.add_argument("--jar", default=None, help="path to freerouting.jar")
    pc.add_argument("--ses", default=None, help="parse an existing .ses instead of running")
    pc.add_argument("--passes", type=int, default=20)
    pc.set_defaults(fn=cmd_calibrate)

    pe = sub.add_parser("eval"); pe.add_argument("--board", required=True)
    pe.add_argument("--pad", type=float, default=0.5)
    pe.add_argument("--prc", action="store_true",
                    help="grade placement physics rule checks too")
    pe.add_argument("--failed-only", action="store_true")
    pe.set_defaults(fn=cmd_eval)

    pco = sub.add_parser("comprehend",
                         help="auto-detect physics constraints (power nets, "
                              "diff pairs, bypass caps, crystals, converters)")
    pco.add_argument("--board", required=True)
    pco.add_argument("--json", default=None, help="write constraints JSON here")
    pco.add_argument("--prc", action="store_true",
                     help="also grade the current placement against them")
    pco.add_argument("--failed-only", action="store_true")
    pco.set_defaults(fn=cmd_comprehend)

    pdoc = sub.add_parser("doctor",
                          help="preflight: check KiCad, python deps, router, "
                               "distributor keys and the wider suite")
    pdoc.add_argument("--install", action="store_true",
                      help="pip install the missing packages we can fix")
    pdoc.add_argument("--interactive", action="store_true",
                      help="ask before installing")
    pdoc.add_argument("--problems-only", action="store_true",
                      help="hide the checks that already pass")
    pdoc.add_argument("--json", action="store_true", help="machine-readable output")
    pdoc.set_defaults(fn=cmd_doctor)

    pds = sub.add_parser("drc-scope",
                         help="what a DRC result actually examined — which checks "
                              "are switched off, and what a full-scope re-run finds")
    pds.add_argument("--board", required=True)
    pds.add_argument("--report", default=None,
                     help="an existing kicad-cli DRC json to describe")
    pds.add_argument("--full", action="store_true",
                     help="re-run DRC with EVERY check enabled (on a temp copy)")
    pds.add_argument("--out", default=None, help="write the full-run report here")
    pds.add_argument("--kicad-cli", default="kicad-cli")
    pds.add_argument("--json", action="store_true")
    pds.set_defaults(fn=cmd_drc_scope)

    pnl = sub.add_parser("netlist",
                         help="read the connection list back out of the board "
                              "(the netlist a board with no schematic still has)")
    pnl.add_argument("--board", required=True)
    pnl.add_argument("--out", default=None, help="write here instead of stdout")
    pnl.add_argument("--summary", action="store_true",
                     help="counts, biggest nets and single-pad nets only — the "
                          "cheap answer to 'what is on this board'")
    pnl.add_argument("--json", action="store_true")
    pnl.set_defaults(fn=cmd_netlist)

    pst = sub.add_parser("stackup",
                         help="layer stack, plane assignment, netclass geometry, "
                              "and whether impedance is verifiable at all")
    pst.add_argument("--board", required=True)
    pst.add_argument("--json", action="store_true")
    pst.set_defaults(fn=cmd_stackup)

    psa = sub.add_parser("stackup-define",
                         help="define the board stackup from a fab profile and "
                              "solve the trace geometry it implies")
    psa.add_argument("--board", required=True)
    psa.add_argument("--profile", default=None, help="fab stackup profile name")
    psa.add_argument("--list-profiles", action="store_true")
    psa.add_argument("--apply", action="store_true",
                     help="write the stackup into the .kicad_pcb")
    psa.add_argument("--no-backup", action="store_true")
    psa.add_argument("--replace", action="store_true",
                     help="replace an existing stackup instead of refusing")
    psa.add_argument("--target-se", type=float, default=50.0,
                     help="single-ended impedance target in ohms (default 50)")
    psa.add_argument("--target-diff", type=float, default=100.0,
                     help="differential impedance target in ohms (default 100)")
    psa.add_argument("--tolerance", type=float, default=10.0,
                     help="percent tolerance before a netclass is flagged")
    psa.add_argument("--traces", action="store_true",
                     help="also grade the ACTUAL routed copper on RF-named nets")
    psa.add_argument("--nets", default=None,
                     help="comma-separated nets to grade instead of auto-detecting")
    psa.add_argument("--json", action="store_true")
    psa.set_defaults(fn=cmd_stackup_apply)

    psc = sub.add_parser("schematic",
                         help="generate a .kicad_sch from a netlist spec (or a "
                              "board) and verify it against its source")
    psc.add_argument("--spec", default=None, help="spec JSON (preferred: it is the source of truth)")
    psc.add_argument("--board", default=None, help="or derive from the routed copper")
    psc.add_argument("--out", required=True)
    psc.add_argument("--title", default=None)
    psc.add_argument("--no-verify", action="store_true")
    psc.add_argument("--kicad-cli", default="kicad-cli")
    psc.add_argument("--json", action="store_true")
    psc.set_defaults(fn=cmd_schematic)

    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
