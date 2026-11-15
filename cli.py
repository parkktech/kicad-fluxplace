#!/usr/bin/env python3
"""fluxplace CLI — headless signal-flow placement for KiCad boards.

  analyze  : print the communication map (hub, branches, forks, lint)
  place    : re-place components by topology, save the board
  eval     : report placement quality (weighted wirelength, overlaps, board size)

Run with KiCad's python so pcbnew is importable, e.g.:
  PYTHONPATH=/usr/lib/python3/dist-packages /usr/bin/python3 cli.py place \
      --board board.kicad_pcb --strategy flux --rotate ortho --out out.kicad_pcb
"""
import argparse, re, sys, os, subprocess
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
    qty = a.qty
    if qty is None and a.constraints:
        from fluxplace import constraints as C
        qty = (C.load(a.constraints).get("bom") or {}).get("quantity_target")
    if qty is not None and int(qty) < 1:
        raise SystemExit("quantity must be >= 1")
    qty = int(qty) if qty is not None else None
    report, counts = S.check(by_mpn, need=a.sourcing_need,
                             cache_dir=os.path.dirname(path),
                             refresh=a.sourcing_refresh, both=a.sourcing_both,
                             qty=qty)
    totals = None
    if qty is not None:
        totals = S.quantity_table(report, qty)
        blockers = [m for m, r in report.items() if r["verdict"] in S.BLOCKERS]
        if blockers:
            print("    SOURCING BLOCKERS (not buyable / EOL): " + ", ".join(blockers))
    else:
        blockers = S.summary(report, counts, a.sourcing_need, show_ok=a.show_ok)
    if a.china:
        from fluxplace import lcsc as L
        res = L.check(list(by_mpn), need=a.cn_need,
                      cache_path=os.path.join(os.path.dirname(path), ".lcsc_cache.json"),
                      refresh=a.sourcing_refresh)
        order = {"CN_ABSENT": 0, "CN_NONE": 1, "CN_LOW": 2, "ERR": 3, "CN_OK": 4}
        for mpn, r in sorted(res.items(), key=lambda kv: (order.get(kv[1]["grade"], 9), kv[0])):
            row = r["row"] or {}
            if r["grade"] == "CN_OK" and not a.show_ok:
                continue
            print(f"    {r['grade']:9s} {mpn:26s} {str(row.get('lcsc') or '-'):10s} "
                  f"stock={row.get('stock', '-')!s:>7} "
                  f"{'basic' if row.get('basic') else 'ext':5s}   [{' '.join(by_mpn[mpn])}]")
        from collections import Counter
        c = Counter(r["grade"] for r in res.values())
        print("    china: " + "  ".join(f"{k}={v}" for k, v in sorted(c.items()))
              + f"  (need >= {a.cn_need} at LCSC)")
        blockers += [m for m, r in res.items() if r["grade"] in ("CN_ABSENT", "CN_NONE")]
    if a.json:
        json_mod = __import__("json")
        # quantity mode wraps the lines so the totals travel with them;
        # legacy mode writes the bare {mpn: line} map that review reads
        body = (report if qty is None else
                {"quantity": qty, "totals": totals, "lines": report})
        json_mod.dump(body, open(a.json, "w"), indent=1)
        print(f"wrote {a.json}")
    raise SystemExit(1 if blockers else 0)


def _deliver_xcheck(a, bom):
    """FAB_BOM_POS_MISMATCH before anything ships: the BOM and the .pos in the fab
    package must match the board. Exits 1 on any FAIL. bom=None checks pos only."""
    from fluxplace import fabxcheck as X
    if not a.board:
        print("    ! cross-check skipped: no --board", flush=True)
        return
    pos = os.path.join(a.fab_dir, "place", "pos.csv")
    msgs = []
    if not os.path.isfile(pos):
        msgs.append(f"{X.CODE}: no position file in {a.fab_dir}/place")
    else:
        msgs += X.check(a.board, pos=pos)
    if bom:
        msgs += X.check(a.board, bom=bom)
    if not bom:
        print("    ! BOM cross-check skipped: no board BOM among --bom", flush=True)
    for m in dict.fromkeys(msgs):
        print("FAIL " + m, flush=True)
    if msgs:
        raise SystemExit(1)
    print(f"    {X.CODE}=PASS (pos{' + BOM' if bom else ''})", flush=True)


def cmd_deliver(a):
    """Split a fab package for its two audiences: a CAM-only zip for the
    fab's engineer, plus loose readable docs for whoever places the order."""
    from fluxplace import fab as F, fabdoc, questions as QS
    QS.refuse_assumed(a.board, "deliver")
    _review_gate(a, stage="deliver")
    _open_gate(a)
    from fluxplace import constraints as C, gerbercheck as GC
    gerber = GC.run_stage(a.fab_dir, a.board, cons=C.load(a.constraints),
                          log=lambda m: print(m, flush=True))
    if gerber["blocking"]:
        print("deliver: gerber check FAILED with [fab] gerber_check = fail; nothing delivered")
        raise SystemExit(1)
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
    from fluxplace import constraints as C
    inc = tuple(C.load(a.constraints).get("deliver", {}).get("include_formats", ()))
    if a.no_pcbway:
        _deliver_xcheck(a, bom=(a.bom or [None])[0])
        F.deliver(a.fab_dir, a.out, a.name, docs=docs, extras=(a.bom or []),
                  log=lambda m: print(m, flush=True), include_formats=inc)
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
    _deliver_xcheck(a, bom=board_bom)
    F.deliver(a.fab_dir, a.out, os.path.splitext(slots["slot_gerbers"])[0],
              docs=docs, extras=extras, centroid_name=slots["slot_centroid"],
              log=lambda m: print(m, flush=True), include_formats=inc)
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
    from fluxplace import pcbway, titleblock as TB
    bad = [f for f in TB.findings(TB.read(a.board), a.board, stage="pcbway") if f["level"] == "FAIL"]
    if bad:
        raise SystemExit("ABORT before pcbway: " + "; ".join(f"{f['code']}: {f['msg']}" for f in bad))
    _open_gate(a)
    facts = pcbway.collect(board=a.board, fab_dir=a.fab_dir, boms=(a.bom or []),
                           sourcing=a.sourcing_json, quantity=a.quantity,
                           name=a.name)
    pcbway.write(a.out_dir, facts, zip_name=a.zip_name, docx=not a.no_docx)


def _routable_place(a, board_in, board_out):
    """Connection-driven placement on the text model; returns (moves, before, after)."""
    from fluxplace import routediag as RD, routable as RB, boardwrite as BW, preroute as PR
    cons, spec = _text_ctx(a)
    B0 = RD.load(board_in)
    f0, c0 = PR.run(B0, cons, spec)
    from fluxplace import intent as IT              # --- intent: hard items seated and held before the placer runs
    src_in, pre_moves = board_in, {}
    if IT.section(cons):
        import tempfile
        board_in, cons, seated = IT.prepare(board_in, cons, spec, tempfile.mkdtemp(prefix="intent-"),
                                            log=lambda m: print("  " + m, flush=True))
        pre_moves = seated.moves
        a._intent_cons = cons
    B = RD.load(board_in)
    try:
        if getattr(a, "objective", None) == "plan" or (cons.get("placement") or {}).get("objective") == "plan":
            from fluxplace import planplace as PP
            mv, st = PP.plan_place(B, cons, spec, seed=a.seed, log=lambda m: print("  " + m, flush=True),
                                   time_limit=a.time_limit)
            a._plan_stats = st
            print(f"  plan objective: PLAN_SCORE {st['score_before']:.1f} -> {st['score_after']:.1f}, kept {st['kept']}", flush=True)
            flips = st["flips"]
        elif getattr(a, "refine", False):
            from fluxplace import refine as RF
            mv, st = RF.refine(B, cons, spec, seed=a.seed, log=lambda m: print("  " + m, flush=True),
                               time_limit=a.time_limit)
            print(f"  refine: score {st['score_before']:.0f} -> {st['score_after']:.0f}, kept {st['kept']}", flush=True)
            flips = st["flips"]
        else:
            mv = RB.place(B, cons, spec, log=lambda m: print("  " + m, flush=True))
            flips = []
    except Exception as e:
        if e.__class__.__name__ == "PlacementError":
            raise SystemExit(f"place [routable] REFUSED: {e}")
        raise
    # flipped parts: write the pre-flip pose as text, then let pcbnew do the flip (text cannot mirror)
    BW.move_file(board_in, board_out, {r: ((x, y, (180.0 - ang) % 360.0) if r in flips else (x, y, ang))
                                       for r, (x, y, ang) in mv.items()}, post=not flips)
    if flips:
        from fluxplace import flip as FL
        pre = RD.load(board_out)
        facts = FL.flip_file(board_out, board_out, flips)
        bad = FL.verify(pre, board_out, flips, facts)
        print(f"  flipped {len(flips)} part(s) with pcbnew: {', '.join(flips[:12])}; "
              f"model vs pcbnew mismatches: {len(bad)}" + (f" ({bad[:3]})" if bad else ""))
    if getattr(a, "strip_stale", False):
        from fluxplace import placechecks as PCK
        text, n = PCK.strip_stale_text(open(board_out, errors="surrogateescape").read(), RD.load(board_out))
        open(board_out, "w", errors="surrogateescape").write(text)
        print(f"  --strip-stale: removed {n} stale track/via item(s)")
    f1, c1 = PR.run(RD.load(board_out), cons, spec)
    if getattr(a, "_plan_stats", None):
        from fluxplace import plancli
        plancli.after_plan_place(a, board_in, board_out, cons, spec, a._plan_stats)
    return mv, (f0, c0), (f1, c1)


def _routable_report(mv, before, after, out):
    from fluxplace import preroute as PR
    print(f"routable: moved/turned {len(mv)} part(s); saved {out}")
    print("BEFORE"); print(PR.format_text(*before))
    print("AFTER"); print(PR.format_text(*after))
    print("note: existing copper is not moved with the parts; place on an unrouted board")


def _engine_place(a):
    """Placement engine hook (docs/CYPRESS.md): --engine, else `[placement] engine`. True when an external engine
    wrote the board; False -> the built-in placer runs (also when the engine is not installed)."""
    from fluxplace import placer_cypress as PC
    cons, spec = _text_ctx(a)
    out = a.out or a.board
    if PC.engine_of(cons, getattr(a, "engine", None)) != "cypress":
        PC.builtin_record(out, a.strategy)
        return False
    try:
        rep = PC.place_file(a.board, out, cons, spec, home=a.cypress_home, python=a.cypress_python,
                            log=lambda m: print("  " + m, flush=True))
    except Exception as e:
        if e.__class__.__name__ == "PlacementError":
            raise SystemExit(f"place [cypress] REFUSED: {e}")
        raise
    if isinstance(rep, str):
        PC.builtin_record(out, a.strategy, fallback=rep)
        return False
    print(PC.format_report(rep))
    print(f"cypress: moved/turned {rep['moved']} part(s); saved {out}")
    return True


def cmd_place(a):
    if getattr(a, "spec", None):
        from fluxplace import parts as _PT, constraints as _C
        _PT.autogate(a.spec, _C.load(getattr(a, "constraints", None)), a.board, "place")
    if _engine_place(a):
        return
    if a.strategy == "routable":
        out = a.out or a.board
        mv, b, c = _routable_place(a, a.board, out)
        _routable_report(mv, b, c, out)
        if not getattr(a, "_plan_stats", None) and not getattr(a, "refine", False):     # --- wire length (wlrefine.py)
            from fluxplace import wlrefine as WL
            WL.stage_step(out, getattr(a, "_intent_cons", None) or _text_ctx(a)[0], _text_ctx(a)[1])
        if getattr(a, "_intent_cons", None):        # --- INTENT gate: unmet hard intent fails the placement
            from fluxplace import intent as IT
            if IT.gate(out, a._intent_cons, _text_ctx(a)[1]):
                raise SystemExit(1)
        return
    _sourcing_preflight(a, "placement")
    board, parts, nets, IO = _load(a.board)
    cg = G.build(parts, nets, a.big_fanout)
    topo = T.analyze(cg, prefer_hub=a.hub)
    center = IO.board_center(board) if a.center_board else (0.0, 0.0)
    if not a.center_board and any(p.get("locked") for p in parts.values()):
        # locked parts keep their real coordinates, so a strategy seeded at (0, 0)
        # was assembling the rest of the board far from them
        center = IO.board_center(board)
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
    try:        # hard guarantee: never write a part outside the outline / a board that exploded
        P.check_extent(parts, {r: v for r, v in pos.items() if not parts[r].get("locked") or a.move_locked},
                       rot, IO.outline_bbox(board), redraw=not a.no_outline, max_growth=a.max_growth)
    except P.PlacementError as e:
        raise SystemExit(f"place [{a.strategy}] REFUSED: {e}")
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
    frep = _fit_sheet(a, board)
    IO.save(board, out)
    _fit_paper(frep, out)
    ov = P.count_overlaps(parts, pos, 0.2, angles=rot)
    print(f"placed {len(pos)} parts [{a.strategy}, rotate={a.rotate}]  "
          f"HPWL {before:.0f} -> {after:.0f} mm ({100*(before-after)/before:+.0f}%)  "
          f"overlaps={ov}" + (f"  board={dims[0]:.0f}x{dims[1]:.0f}mm" if dims else ""))
    print(f"saved: {out}")


# ---- fit-sheet (fluxplace/sheet.py) ----------------------------------------
_HINT_CMDS = {"finish": "out", "repair": "out", "tune": "out", "drc-fix": "out",
              "patch": "out", "sync-nets": "out", "replace-footprint": "out"}


def _fit_sheet(a, board, cons_path=None, label="fit-sheet"):
    """Translate a freshly built board onto the drawing sheet (in memory; the
    caller saves, then calls _fit_paper on every file it saved). None when
    --no-fit-sheet or nothing to do."""
    if getattr(a, "no_fit_sheet", False):
        return None
    from fluxplace import sheet as SH
    try:
        rep = SH.fit(board)
    except Exception as e:                      # never lose a build to the fit
        print(f"    ! {label} skipped: {e}")
        return None
    print("    " + SH.summary_line(rep, label))
    if rep["moved"] and cons_path and os.path.exists(cons_path):
        n = SH.shift_text(open(cons_path).read(), 0, 0)[1]
        if n:
            dx, dy = rep["offset"]
            print(f"    {label}: {cons_path} has {n} absolute coordinate row(s) "
                  f"(`[lock.at]`, keep-outs, group centres ...) — NOT rewritten; shift them with "
                  f"`fluxplace fit-sheet --board <x> --constraints {cons_path} "
                  f"--shift {dx:g},{dy:g}`")
    return rep


def _fit_paper(rep, *paths):
    """Write the chosen paper into saved board files (pcbnew 10 cannot)."""
    if rep and rep.get("paper_changed"):
        from fluxplace import sheet as SH
        for p in paths:
            if p and os.path.exists(p):
                SH.set_paper_text(p, rep["paper"])


def _sheet_hint(path):
    """One line when a board a copper-only command just wrote is off the page."""
    try:
        from fluxplace import sheet as SH
        if SH.off_sheet_file(path):
            print("board is off the sheet \u2014 run `fluxplace fit-sheet`")
    except Exception:
        pass


def cmd_fit_sheet(a):
    from fluxplace import sheet as SH
    if a.shift:                                  # constraints only, board untouched
        if not a.constraints:
            raise SystemExit("--shift needs --constraints")
        off = tuple(float(v) for v in a.shift.split(","))
        try:
            new, n = SH.rewrite_constraints(open(a.constraints).read(), *off)
        except SH.ConstraintsShiftError as e:
            raise SystemExit(f"fit-sheet: NOTHING WRITTEN: {e}")
        if off != (0, 0) and not a.dry_run:
            open(a.constraints, "w").write(new)
        print(f"constraints: {n} coordinate key(s) in {a.constraints} "
              f"{'would shift' if a.dry_run else 'shifted'} by {off}; self-check passed")
        return
    try:
        rep = SH.fit_file(a.board, out=a.out, paper=a.paper, dry_run=a.dry_run,
                          refill=a.refill, constraints=a.constraints)
    except SH.ConstraintsShiftError as e:
        raise SystemExit(f"fit-sheet: NOTHING WRITTEN: {e}")
    print("\n".join(SH.summary(rep, dry_run=a.dry_run)))
    if a.constraints and rep.get("constraints_text") is not None:
        print(f"constraints: {rep['constraints_rows']} coordinate key(s) in {a.constraints} "
              f"{'would shift' if a.dry_run else 'shifted'}; self-check passed")
    _fill_tb(a, rep)


def _fill_tb(a, rep=None):
    """fit-sheet / title-block summary line; `--fill-title-block` fills it (see titleblock.py)."""
    import json as _json
    from fluxplace import constraints as C, titleblock as TB, questions as Q
    path = getattr(a, "out", None) if (rep is not None and getattr(a, "out", None)
                                       and os.path.exists(a.out)) else a.board
    cons = C.load(getattr(a, "constraints", None))
    spec = _json.load(open(a.spec)) if getattr(a, "spec", None) else None
    if getattr(a, "fill_title_block", False) and not getattr(a, "dry_run", False):
        S = Q.Session(path, cons, Q.parse_answers(getattr(a, "answer", None)),
                      bool(getattr(a, "assume_defaults", False)), "title-block")
        r = TB.fill(path, None, cons, spec, session=S)
        print(f"title block filled: {r['after']['title']!r} rev {r['after']['rev']!r} "
              f"date {r['after']['date']!r} company {r['after']['company']!r}"
              + (f"; not filled: {', '.join(r['missing'])}" if r["missing"] else ""))
    print(TB.summary_line(path, cons, spec))


def cmd_title_block(a):
    """Fill the board's drawing-sheet title block (revision, date, company, title)."""
    a.fill_title_block = True
    if a.out:
        import shutil
        shutil.copyfile(a.board, a.out)
        a.board, a.out = a.out, None
    _fill_tb(a)


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
    _route_gate(a, a.board, "route")
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
    if getattr(a, "pre_place", None) == "routable":
        os.makedirs(a.workdir, exist_ok=True)
        pre = os.path.join(a.workdir, "pre_routable.kicad_pcb")
        mv, b0, b1 = _routable_place(a, a.board, pre)
        _routable_report(mv, b0, b1, pre)
        a.board = pre
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
        if ok and not a.no_fit_sheet:
            from fluxplace import sheet as SH
            try:
                print("    " + SH.summary_line(SH.fit_file(out, refill=False), "fit-sheet"))
            except Exception as e:
                print(f"    ! fit-sheet skipped: {e}")


def cmd_fab(a):
    """Emit a build-quality manufacturing package (gerbers/drill/place/DRC) for review."""
    from fluxplace import fab, questions as QS, constraints as C, gerbercheck as GC
    if a.check_only:
        r = GC.check_package(a.check_only, board=a.board, cons=C.load(a.constraints))
        print(GC.format_report(r))
        raise SystemExit(1 if r["status"] == "FAIL" else 0)
    if not (a.board and a.out):
        raise SystemExit("fab: --board and --out are required (or --check-only FAB_DIR)")
    QS.refuse_assumed(a.board, "fab")
    _review_gate(a, stage="fab")
    _open_gate(a)
    from fluxplace import constraints as C
    cons = C.load(a.constraints)
    res = fab.emit(a.board, a.out, kicad_cli=a.kicad_cli, formats=cons.get("fab", {}).get("formats"), cons=cons)
    print(f"DRC {res['drc']}; package at {res['out']}")
    if res["xcheck"]:
        raise SystemExit(1)
    if res["gerber_blocking"]:
        print("fab: gerber check FAILED with [fab] gerber_check = fail; see gerbercheck.txt")
        raise SystemExit(1)
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
          "--pad", str(a.pad), "--route-timeout", str(a.route_timeout or 900),
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
    if a.no_fit_sheet:
        v += ["--no-fit-sheet"]
    if a.route_only:
        v += ["--route-only"]
    if a.no_pairs:
        v += ["--no-pairs"]
    if a.bypass_csv:
        v += ["--bypass-csv", a.bypass_csv]
    v += ["--force"]          # the candidate run picks the best routed result; gating happens per child as a report
    if getattr(a, "spec", None):
        v += ["--spec", a.spec]
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
    from fluxplace import constraints as _C
    _cons = _C.load(a.constraints) if getattr(a, "constraints", None) else None
    res = fab.emit(os.path.join(a.out, "routed.kicad_pcb"),
                   os.path.join(a.out, "fab"), kicad_cli=a.kicad_cli,
                   formats=(_cons or {}).get("fab", {}).get("formats"), cons=_cons)
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
                            checkpoint=a.checkpoint, cons=cons)
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
    from fluxplace import model_verify as MV
    project_libs = []
    if a.constraints:
        from fluxplace import constraints as CONS
        cons = CONS.load(a.constraints)
        board_dir = os.path.dirname(os.path.abspath(a.board))
        for d in cons.get("docs", {}).get("project_libs", []):
            # same fallback precedence check_landpattern uses for
            # project_libs: as given (relative to CWD, the normal case
            # when run from the repo root) or relative to the board.
            project_libs.append(d)
            if not os.path.isabs(d):
                project_libs.append(os.path.join(board_dir, "..", "..", d))
    resolve = lambda p: IO._resolve_model_path(p, board)
    finds = MV.verify_board(board, resolve, fix=a.fix, tol=a.tol, project_libs=project_libs)
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
    if getattr(a, "pre_place", None) == "routable":
        pre = os.path.join(a.out, "pre_routable.kicad_pcb")
        mv, b0, b1 = _routable_place(a, a.board, pre)
        _routable_report(mv, b0, b1, pre)
        board, parts, nets, IO = _load(pre)         # the routable placement IS the placement
        pos = {r: (p["x"], p["y"]) for r, p in parts.items()}
        rot = {r: p.get("angle0", 0.0) for r, p in parts.items()}
        rep = {"overflow": 0.0}
        for ref in phantoms:
            parts.pop(ref, None)
        phantoms = []
        nudged = 0
        print(f"[1/{NSTAGE}] placed by routable ({len(mv)} parts moved)  ({time.time()-t0:.0f}s)")
        _stages_outline_to_fab(a, board, parts, nets, IO, pos, rot, label="AUTO")
        return
    pos, rot, rep = P.place_routed(parts, cg, topo, center=IO.board_center(board), pad=a.pad)
    try:
        P.check_extent(parts, {r: v for r, v in pos.items() if not parts[r].get("locked")}, rot,
                       IO.outline_bbox(board), redraw=True, max_growth=getattr(a, "max_growth", 6.0))
    except P.PlacementError as e:
        raise SystemExit(f"auto REFUSED after placement: {e}")
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
    import os, subprocess, time, json, shutil
    from fluxplace import fab, planes, adaptive
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
    frep = _fit_sheet(a, board, cons_path=getattr(a, "constraints", None))

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
    _fit_paper(frep, placed)
    _route_gate(a, placed, "routing")

    # ---- [4] ROUTE — explicit net list (KRT routes NOTHING without --nets), --------
    # power nets first and wide, plane nets excluded (they are pours, not traces).
    # Stage 7: per-stage wall-clock limit; escape-first net order; on timeout or failed
    # nets, stop retrying and run route-diagnose (optionally re-place + re-route).
    from fluxplace import stall as ST, routediag as RD, netclass as NC
    cons4, spec4 = _text_ctx(a)
    tmo = ST.stage_timeout(a.route_timeout, cons4)
    t0 = time.time()
    route_nets = [n for n in nets if n not in plane_nets]
    pw = _classify_power(nets, plane_nets, a.big_fanout)

    # Batch routing with checkpoints (fluxplace/batchroute.py): never time-box a whole-board router call
    # (py_router writes its output only when a whole pass incl. its retry phase finishes).
    from fluxplace import batchroute as BR, routestatus as RS
    from fluxplace import routerules as RR, routepar as RP
    S4 = BR.settings(cons4)
    R4 = RR.rules(cons4, placed, a.track, a.clearance)       # project floors, grid, no vias in pads
    rflags4 = RR.flags(R4, a.router_dir, a.out)
    conn4 = BR.default_conn(S4)

    def build(src_in, dst, only, limited):
        cmd = [a.router_py, os.path.join(a.router_dir, "py_router", "route.py"),
               src_in, "--output", dst, "--keep-input-copper", "--layers", *a.layers,
               *RR.size_args(R4), *adaptive.krt_extra(a.router_dir), *rflags4]
        if limited:
            cmd += ["--max-ripup", str(S4["max_ripup"])]
        cmd += ["--nets", *only]
        pwn = {k: v for k, v in pw.items() if k in only}
        if pwn:
            cmd += ["--power-nets", *pwn.keys(), "--power-nets-widths", *[str(w) for w in pwn.values()]]
        return cmd

    extra4 = {}
    spath4 = RS.status_path_for(routed)
    run4 = BR.make_runner(build, a.router_dir, extra4, spath4, S4)
    first = []
    Bp = RD.load(placed)
    cls4 = NC.classify(Bp, cons4, spec4)
    if not getattr(a, "no_escape_first", False):
        first, _rest = ST.escape_first(Bp, cls4)
        first = [n for n in first if n in route_nets]
        if first:
            print(f"    escape-first: {len(first)} fine-pitch signal net(s) lead the batches")
    open4 = conn4(placed)                                    # open connections, not "nets without copper"
    todo4 = [n for n in route_nets if n in open4]
    if R4["mode"] == "parallel":
        br = RP.route_parallel(placed, a.out, todo4, build, run4, cons=cons4, spec=spec4, B=Bp, cls=cls4,
                               first=first, power=list(pw), rules=R4, status_path=spath4, extra=extra4,
                               log=lambda m: print(m, flush=True), conn=conn4, cwd=a.router_dir)
    else:
        br = BR.route_batches(placed, a.out, todo4, run4, cons=cons4, spec=spec4, B=Bp, cls=cls4, first=first,
                              power=list(pw), status_path=spath4, extra=extra4,
                              log=lambda m: print(m, flush=True), rules=R4, conn=conn4)
    print("    " + BR.format_result(br))
    shutil.copy(br["board"], routed)
    for ext in (".kicad_pro",):
        sc = os.path.splitext(br["board"])[0] + ext
        if os.path.exists(sc):
            shutil.copy(sc, os.path.splitext(routed)[0] + ext)
    timed_out = bool(br["timeouts"]) and bool(br["leftovers"])
    r = None
    ok = os.path.exists(routed)
    _fit_paper(frep, routed)
    if not ok and r is not None:
        print("    router stderr:", (r.stderr or r.stdout or "")[-240:].replace("\n", " "))
    summ = adaptive.krt_summary((r.stdout if r else "") or "")
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
    if ok:
        failed = sorted(conn4(routed))
        if failed or timed_out:
            kk = [0]

            def again(b_in, b_out, only, timeout):
                kk[0] += 1
                r2 = BR.route_batches(b_in, os.path.join(a.out, f"stall-batches-{kk[0]}"), only, run4,
                                      cons=cons4, spec=spec4, total_budget=timeout, status_path=spath4,
                                      extra=extra4, log=lambda m: print(m, flush=True), rules=R4, conn=conn4)
                shutil.copy(r2["board"], b_out)
                return bool(r2["timeouts"]) and bool(r2["leftovers"])
            out = ST.stall_loop(routed, a.out, again, cons4, spec4,
                                rounds=getattr(a, "stall_rounds", 3),
                                replace=getattr(a, "replace_on_stall", False),
                                plane_nets=plane_nets, timeout=tmo, failed0=failed,
                                timed_out0=timed_out, log=lambda m: print(m, flush=True))
            print(f"    stall: {out['rounds']} round(s), {len(out['failed'])} net(s) still unrouted, "
                  f"{len(out['moved'])} part(s) moved: {', '.join(sorted(out['moved'])) or 'none'}")
            if out["board"] != routed:
                import shutil as _sh
                _sh.copy(out["board"], routed)
    src = routed if ok else placed

    # ---- [5] PLANE FINALIZE + DFM — a pour alone connects nothing the fill can't ---
    # physically reach (fine-pitch rows): one router pass ON the plane nets adds the
    # stub+via escapes. Then clamp sub-fab vias, embed rules, refill, sync .kicad_pro.
    if plane_nets and ok:
        cmd = [a.router_py, os.path.join(a.router_dir, "py_router", "route.py"),
               routed, "--output", finald, "--keep-input-copper", "--layers", *a.layers,
               *RR.size_args(dict(R4, clearance=max(R4["clearance"], 0.12))),
               *adaptive.krt_extra(a.router_dir), *rflags4, "--nets", *plane_nets,
               "--max-ripup", "60"]
        try:
            from fluxplace import routestatus as RS
            RS.run_logged(cmd, cwd=a.router_dir, timeout=tmo, out=finald, stage="plane-finalize")
        except subprocess.TimeoutExpired:
            print(f"    ! plane-finalize route timed out after {tmo}s"
                  " — proceeding to DFM/fab with the main-route board")
        src = finald if os.path.exists(finald) else routed
    _fit_paper(frep, src)
    b2 = pcbnew.LoadBoard(src)
    clamped, shrunk, grazing = planes.finalize_dfm(b2)
    planes.refill(b2)
    b2.Save(src)
    _fit_paper(frep, src)
    pro = os.path.splitext(src)[0] + ".kicad_pro"
    planes.sync_project_rules(pro, netclass_clearance=min(a.clearance, 0.13) * 0.75)
    print(f"[5/{NSTAGE}] plane finalize + DFM: {clamped} vias clamped, {shrunk} graze-shrunk,"
          f" zones filled, rules embedded (board + {os.path.basename(pro)})")
    for (gx, gy, gap) in grazing:
        print(f"    ! via at ({gx:.2f},{gy:.2f}) still grazes a pad ({gap} mm air) — review")

    # ---- [6] FAB -------------------------------------------------------------------
    from fluxplace import constraints as _C
    _cons = _C.load(getattr(a, "constraints", None)) if getattr(a, "constraints", None) else None
    res = fab.emit(src, os.path.join(a.out, "fab"), kicad_cli=a.kicad_cli,
                   formats=(_cons or {}).get("fab", {}).get("formats"), cons=_cons)
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
    if not a.board and not a.spec:
        raise SystemExit("lint: give --board and/or --spec")
    from fluxplace import ercnet as EN
    findings, pads, facts, spec, cons = [], None, None, None, {}
    if getattr(a, "constraints", None):
        from fluxplace import constraints as C
        cons = C.load(a.constraints)
    if a.board:
        from fluxplace import kicad_io as IO
        board = IO.load(a.board)
        pads = L.pads_from_board(board)
        findings += L.run(pads, waivers=a.waive)
        facts = {"parts": {}}
        for fp in board.GetFootprints():
            facts["parts"][fp.GetReference()] = {"pad_abs": {
                pd.GetNumber(): (pcbnew_mm(pd.GetPosition().x), pcbnew_mm(pd.GetPosition().y))
                for pd in fp.Pads() if pd.GetNumber()}}
    if a.spec:                                   # control-pin default levels from the spec netlist
        with open(a.spec, encoding="utf-8") as fh:
            spec = _json.load(fh)
        st = L.sch_pin_types(a.sch) if getattr(a, "sch", None) else None
        findings += L.control_pins(spec, cons, st, waivers=a.waive)
    # schematic-level checks: the spec's netlist (pin names, values) + placed pad positions when a board is given
    if not a.board and not (spec and spec.get("nets")):
        raise SystemExit("lint: spec has no nets; give --board (the placed pads) to lint")
    erc = EN.run(spec if spec and spec.get("nets") else pads, cons, board_facts=facts)
    findings += EN.waive(erc, a.waive)
    n = L.summarize(findings)
    if a.json:
        with open(a.json, "w") as fh:
            _json.dump(findings, fh, indent=1)
        print(f"lint: wrote {a.json}")
    if a.fail_on == "warning" and (n["error"] or n["warning"]):
        raise SystemExit(1)
    if a.fail_on == "error" and n["error"]:
        raise SystemExit(1)


def pcbnew_mm(nm):
    return round(nm / 1e6, 4)


def _open_gate(a):
    """fab / deliver / pcbway refuse while a connection is open or the saved fills are stale (not skipped by --no-review)."""
    from fluxplace import opengate as OG, constraints as C
    board = getattr(a, "board", None)
    if board and os.path.isfile(board):
        OG.refuse(board, C.load(getattr(a, "constraints", None)), getattr(a, "waive", None))


def _review_gate(a, stage="review", hard=True):
    """The design-review gate: spec rules, diff pairs, per-layer RF impedance,
    distributor package/pin/temperature data, pinmap vs the official symbol,
    spec/board sync, hold-up and TVS margins. Runs before anything leaves for
    a fab. FAIL aborts unless --no-review; returns the findings."""
    import json as _json
    from fluxplace import review as R
    from fluxplace import constraints as C
    if getattr(a, "no_review", False):
        print(f"    review gate SKIPPED (--no-review) before {stage}", flush=True)
        return []
    board = getattr(a, "board", None)
    if not board:
        print(f"    review gate: no --board given, nothing to review before {stage}")
        return []
    facts = R.facts_from_board(board, mpn_map=getattr(a, "mpn_map", None))
    facts.setdefault("board_path", board)
    spec = None
    spec_path = getattr(a, "spec", None)
    if spec_path:
        spec = _json.load(open(spec_path))
    cons = C.load(getattr(a, "constraints", None))
    partdata = {}
    if not getattr(a, "no_api", False) and facts["mpn_map"]:
        from fluxplace import partdata as PD
        cache_dir = os.path.dirname(facts.get("mpn_map_path") or board) or "."
        partdata = PD.fetch(sorted(set(facts["mpn_map"].values())),
                            cache_dir=cache_dir,
                            refresh=getattr(a, "refresh", False),
                            log=lambda m: print(m, flush=True))
    idx = R.lib_index()
    waivers = list(getattr(a, "waive", None) or ()) + list(
        (cons.get("review", {}) or {}).get("waive", []))
    ds_dir = getattr(a, "datasheets", None) or (cons.get("docs", {}) or {}).get("datasheets")
    if ds_dir and not os.path.isabs(ds_dir):
        base = os.path.dirname(os.path.abspath(getattr(a, "constraints", None) or board))
        cand = [os.path.join(base, ds_dir), os.path.join(base, "..", ds_dir),
                os.path.join(base, "..", "..", ds_dir)]
        ds_dir = next((os.path.normpath(c) for c in cand if os.path.isdir(c)),
                      os.path.normpath(cand[0]))
    findings = R.run(facts, spec=spec, cons=cons, partdata=partdata, idx=idx,
                     waivers=waivers, ds_dir=ds_dir, stage=stage)
    print(f"REVIEW ({stage}) — {board}")
    n = R.summarize(findings, log=lambda m: print(m, flush=True))
    if getattr(a, "json", None) and stage == "review":
        with open(a.json, "w") as fh:
            _json.dump(findings, fh, indent=1)
        print(f"review: wrote {a.json}")
    if hard and n["fail"]:
        raise SystemExit(f"ABORT before {stage}: {n['fail']} review FAIL(s). "
                         f"Fix them, waive them (--waive CODE:REGEX) or pass "
                         f"--no-review to ship anyway.")
    return findings


def _bridge_guarded(path, spec, a, log, cons=None):
    """repair.bridge on a copy; keep it only if DRC violations do not rise
    and the unconnected count falls."""
    import shutil
    from fluxplace import repair as RP, tune as TU
    from fluxplace import kicad_io as IO
    ref, _, num = spec.partition(":")
    tmp = os.path.splitext(path)[0] + "-bridge.kicad_pcb"
    shutil.copy(path, tmp)
    for ext in (".kicad_pro", ".kicad_dru"):   # DRC judges with the project's rules
        side = os.path.splitext(path)[0] + ext
        if os.path.exists(side):
            shutil.copy(side, os.path.splitext(tmp)[0] + ext)
    board = IO.load(tmp)
    added = RP.bridge(board, ref, num, layers=a.bridge_layers or None, cell=a.cell,
                      width_mm=a.track, clearance_mm=a.clearance, via_mm=a.via,
                      drill_mm=a.drill, cons=cons, log=log)
    if not added:
        os.unlink(tmp)
        return False
    IO.save(board, tmp)
    v0, u0 = TU.drc_counts(path, a.kicad_cli)
    v1, u1 = TU.drc_counts(tmp, a.kicad_cli)
    ok = v1 <= v0 and u1 < u0
    print(f"bridge {spec}: DRC {v0}/{u0} -> {v1}/{u1} {'kept' if ok else 'REVERTED'}")
    if ok:
        shutil.move(tmp, path)
        # the new copper cut the pours: save the file with fills the fab
        # gate will judge (a stale fill read as 19 zone-clearance hits on V1.5)
        subprocess.run([a.kicad_cli, "pcb", "drc", "--refill-zones", "--save-board",
                        "--format", "json", "-o", tmp + ".drc.json", path],
                       capture_output=True, text=True)
        for junk in (tmp + ".drc.json",):
            if os.path.exists(junk):
                os.unlink(junk)
    else:
        os.unlink(tmp)
    for ext in (".kicad_pro", ".kicad_dru", ".kicad_prl"):
        side = os.path.splitext(tmp)[0] + ext
        if os.path.exists(side):
            os.unlink(side)
    return ok


def cmd_repair(a):
    """Copper repairs the review gate asks for: pair loop removal, per-layer
    RF widths, pin remaps with stub rip + GND vias, twin-pad nets, silkscreen
    text. Saves to --out (default: in place) and can run the last-mile patcher
    for anything a remap left unrouted."""
    import json as _json
    from fluxplace import repair as RP, review as R, constraints as C
    from fluxplace import kicad_io as IO
    board = IO.load(a.board)
    log = lambda m: print(m, flush=True)
    cons = C.load(a.constraints)   # [fab] floors every width/via/drill below
    did = []
    if a.fix_pads:
        n = RP.fix_dup_pads(board, log=log)
        did.append(f"twin pads netted: {n}")
    if a.pairs is not None:
        prefixes = a.pairs or ["ETH_"]
        tot = []
        for pfx in prefixes:
            tot += RP.redundant_copper(board, prefix=pfx, log=log)
        did.append(f"redundant copper removed on {len(tot)} net(s)")
    if a.remap:
        mapping = _json.load(open(a.remap))
        rep = RP.remap_pins(board, mapping, rip=not a.no_rip, cons=cons, log=log)
        did.append(f"remapped {len(rep['changed'])} pad(s), ripped {rep['ripped']} "
                   f"items, {rep['vias']} GND via(s), {len(rep['unrouted'])} to route")
    if a.rf:
        rf = cons.get("rf", {})
        facts = R.facts_from_board(a.board)
        nets = set(rf.get("nets", [])) or {n for n in facts["net_pads"] if R.is_rf(n)}
        planes = a.planes or facts["plane_layers"]
        ch = RP.rf_widths(board, facts["stackup"], planes, nets,
                          target_z=float(rf.get("target_z", 50.0)), cons=cons, log=log)
        did.append(f"RF segments re-widthed: {len(ch)} on {sorted(nets)}")
    for ref in (a.clear_under or []):
        n, nets = RP.clear_under(board, ref, log=log)
        did.append(f"cleared {n} item(s) under {ref}")
    for spec in (a.stitch or []):
        ref, _, num = spec.partition(":")
        RP.stitch(board, ref, num, max_mm=a.stitch_max, cons=cons, log=log)
        did.append(f"stitch {spec}")
    if a.text:
        at = None
        if a.text_at:
            at = tuple(float(v) for v in a.text_at.split(","))
        RP.add_text(board, a.text, layer=a.text_layer, at=at, size_mm=a.text_size,
                    prefer=a.text_prefer, log=log)
        did.append(f"text added: {a.text!r}")
    out = a.out or a.board
    IO.save(board, out)
    print(f"repair: {'; '.join(did) or 'nothing requested'} -> {out}")
    for spec in (a.bridge or []):
        _bridge_guarded(out, spec, a, log, cons=cons)
    if a.patch:
        from fluxplace import patch as PATCH
        res = PATCH.patch_board(out, out, kicad_cli=a.kicad_cli,
                                track_w=a.track, clearance=a.clearance,
                                via_mm=a.via, drill_mm=a.drill, cell=a.cell,
                                cons=cons, log=log)
        print(f"patch: accepted={res['accepted']} patched={res['patched']} "
              f"failed={res['failed']}")


def _legacy_finish(a):
    """`finish --nets` with no scope flag: the original behaviour, unchanged (the pipeline's per-net finish
    relies on it). The named nets' existing copper is removed and re-routed; DRC-guarded: kept if violations
    do not rise and the unconnected count falls."""
    import shutil
    from fluxplace import finish as FN, tune as TU
    out = a.out or a.board
    tmp = out + ".finish.kicad_pcb"
    base = TU.drc_counts(a.board, a.kicad_cli)
    print(f"  baseline DRC: {base[0]} violations, {base[1]} unconnected")
    res = FN.route_nets(a.board, tmp, a.nets, planes=a.planes, jar=a.jar, timeout=a.timeout,
                        passes=a.passes, log=lambda m: print(m, flush=True))
    if not res:
        raise SystemExit("finish: nothing produced")
    for ext in (".kicad_pro", ".kicad_dru"):
        side = os.path.splitext(a.board)[0] + ext
        if os.path.exists(side):
            shutil.copy(side, os.path.splitext(tmp)[0] + ext)
    v, u = TU.drc_counts(tmp, a.kicad_cli)
    if v <= base[0] and u < base[1]:
        shutil.move(tmp, out)
        print(f"  project file: {_project_beside(a.board, out)} ({os.path.splitext(out)[0]}.kicad_pro)")
        print(f"  finish KEPT: {v} violations, {u} unconnected -> {out}")
    else:
        print(f"  finish REJECTED by DRC: {v} violations, {u} unconnected "
              f"(was {base[0]} / {base[1]}); scratch left at {tmp}")
        raise SystemExit(1)


def _project_beside(board, out):
    """The .kicad_pro of the input, copied next to the output under the output's own name (so it opens with
    its rules). An existing <out>.kicad_pro is never overwritten (it may carry hand edits). Returns
    "wrote", "kept existing", or "no source" and the caller prints it."""
    import shutil
    side = os.path.splitext(board)[0] + ".kicad_pro"
    dst = os.path.splitext(out)[0] + ".kicad_pro"
    if not os.path.exists(side) or os.path.abspath(side) == os.path.abspath(dst):
        return "no source"
    if os.path.exists(dst):
        return "kept existing"
    shutil.copy(side, dst)
    return "wrote"


def cmd_finish(a):
    """`finish`. With a scope flag (--refs/--area/--area-of/--selection) it routes that scope and copies back
    ONLY its new copper, protection proven on the files (region.run). With --nets alone it is the legacy path."""
    if not any((a.refs, a.area, a.area_of, a.selection)):
        if not a.nets:
            raise SystemExit("finish: give --nets (legacy) or --refs / --area / --area-of / --selection")
        return _legacy_finish(a)
    import shutil
    from fluxplace import finish as FN, tune as TU, region as RG, routediag as RD
    out = a.out or a.board
    tmp = out + ".finish.kicad_pcb"
    scope = RG.scope_from_args(RD.load(a.board), a.nets, a.refs.split(",") if a.refs else None, a.area,
                               a.area_of.split(",") if a.area_of else None, a.margin, a.selection)
    print(f"  scope: {scope['label']}")
    base = TU.drc_counts(a.board, a.kicad_cli)
    print(f"  baseline DRC: {base[0]} violations, {base[1]} unconnected")
    res = RG.run(a.board, tmp, scope, planes=a.planes, jar=a.jar, timeout=a.timeout,
                 passes=a.passes, rip=a.rip, log=lambda m: print(m, flush=True))
    ck = res                                    # run() returns the proof fields next to the counts
    if not ck["ok"]:
        bad = os.path.splitext(out)[0] + ".rejected.kicad_pcb"
        shutil.move(tmp, bad)
        for v in ck["violations"][:40]:
            print("  protect: " + v)
        print(f"  finish FAILED protection check (hash {ck['hash_before']} != {ck['hash_after']}); "
              f"output moved to {bad}; input untouched")
        raise SystemExit(1)
    print(f"  connections in scope: {sum(res['in_scope_before'].values())} open before, "
          f"{sum(res['in_scope_after'].values())} still open")
    print(f"  copper added: {res['added_tracks']} tracks, {res['added_vias']} vias, {res['added_mm']} mm "
          f"on {res['nets_kept']}")
    if res["nets_dropped"]:
        print(f"  dropped (route left the area or out of scope): {res['nets_dropped']}")
    if res["diagnose"] and res["diagnose"].get("nets"):
        for n in res["diagnose"]["nets"]:
            print(f"  left open {n['net']}: " + "; ".join(str(c) for c in n["causes"][:2]))
    print(f"  protection: OK (hash {ck['hash_before']}, {len(ck['removed'])} removed, {sum(ck['added'].values())} added)")
    cov = ", ".join(f"{k} {n}" for k, n in sorted(ck.get("covered", {}).items()))
    print(f"  protected object classes: {cov or 'none'} (arc midpoints, via layer spans, keepouts, pad positions included)")
    for ext in (".kicad_pro", ".kicad_dru"):
        side = os.path.splitext(a.board)[0] + ext
        if os.path.exists(side):
            shutil.copy(side, os.path.splitext(tmp)[0] + ext)
    v, u = TU.drc_counts(tmp, a.kicad_cli)
    if v <= base[0] and u <= base[1]:
        shutil.move(tmp, out)
        print(f"  project file: {_project_beside(a.board, out)} ({os.path.splitext(out)[0]}.kicad_pro)")
        print(f"  finish KEPT: {v} violations, {u} unconnected -> {out}")
    else:
        print(f"  finish REJECTED by DRC: {v} violations, {u} unconnected "
              f"(was {base[0]} / {base[1]}); scratch left at {tmp}")
        raise SystemExit(1)


def cmd_drcfix(a):
    """Fix the DRC noise repairs leave behind (shorts from a bigger land
    pattern, RF clearance after re-widthing, dangling/uncentered tracks,
    silkscreen reference collisions), looping DRC until it stops falling."""
    from fluxplace import drcfix as DF, constraints as C
    cons = C.load(a.constraints)   # [fab] floors the RF neck-fallback width
    out = a.out or a.board
    hist, d = DF.loop(a.board, out, kicad_cli=a.kicad_cli,
                      rounds=a.rounds, islands=a.island_vias, cons=cons,
                      log=lambda m: print(m, flush=True))
    if a.acid_traps:
        from fluxplace import acidtrap as AT
        if AT.fix_junctions(out, kicad_cli=a.kicad_cli,
                            min_deg=C.fab_profile(cons)["acid_trap_deg"],
                            log=lambda m: print(m, flush=True)):
            hist, d = DF.loop(out, out, kicad_cli=a.kicad_cli, rounds=1, cons=cons,
                              log=lambda m: print(m, flush=True))
    import collections
    c = collections.Counter(v.get("type") for v in d.get("violations", []))
    if c:
        print("  remaining: " + ", ".join(f"{k} x{n}" for k, n in c.most_common()))
    for u in d.get("unconnected_items", [])[:12]:
        print("  unconnected: " + " <-> ".join(i.get("description", "")[:50]
                                              for i in u.get("items", [])))


def cmd_tune(a):
    """DRC-guarded differential-pair length tuning: hairpin shortcuts on the
    long side, serpentine meanders on the short side, every step accepted
    only if kicad-cli DRC does not get worse. --match NAME|all instead tunes
    [match.*] groups (bumps on the short members, see fluxplace/lengthmatch.py)."""
    from fluxplace import tune as TU, constraints as C, graph as G
    from fluxplace import kicad_io as IO
    if a.match:
        return _tune_match(a)
    board = IO.load(a.board)
    names = sorted({t.GetNetname() for t in board.GetTracks() if t.GetNetname()})
    pairs = G.diff_pairs(names)
    if a.prefix:
        pairs = {s: m for s, m in pairs.items() if m.startswith(tuple(a.prefix))}
    cons = C.load(a.constraints)
    from fluxplace.pairs import guard_limit      # ps limit with no known delay: not graded, never raises
    lim = lambda master: guard_limit(cons, master)
    rep = TU.tune_pairs(a.board, a.out or a.board, pairs, lim, kicad_cli=a.kicad_cli,
                        amp_mm=a.amplitude, max_rounds=a.rounds,
                        log=lambda m: print(m, flush=True))
    from fluxplace import opengate as OG
    OG.refill(a.out or a.board)                      # a routed board is saved with fresh zone fills
    for master, slave, lm, ls in rep:
        flag = "" if abs(lm - ls) <= lim(master) else "   <-- still over"
        print(f"  {master}/{slave}: P {lm:.1f} / N {ls:.1f} mm, skew {lm - ls:+.2f}{flag}")


def _tune_match(a):
    """tune --match: lengthen the short members of [match.*] groups, one DRC-guarded bump at a time."""
    from fluxplace import tune as TU, constraints as C, lengthmatch as LM, review as RV
    from fluxplace import opengate as OG
    cons = C.load(a.constraints)
    groups = LM.groups(cons)
    if a.match != "all":
        groups = [g for g in groups if g["name"] == a.match]
        if not groups:
            print(f"no [match.{a.match}] group in {a.constraints}")
            return 1
    facts = RV.facts_from_board(a.board)
    stackup, planes = facts.get("stackup") or [], facts.get("plane_layers") or []
    import pcbnew
    from fluxplace import repair as RP
    board0 = pcbnew.LoadBoard(a.board)
    targets = {}
    for g in groups:
        mem = LM.members(g, list(facts["net_pads"]))
        if not mem:
            print(f"  [match.{g['name']}] matched no net")
            continue
        lens = LM.lengths_from_board(board0, set(mem) | ({g["reference"]} if g["reference"] else set()),
                                     stackup, planes, g["include_vias"])
        unit = g["unit"]
        key = (lambda v: v["ps"]) if unit == "ps" else (lambda v: v["mm"])
        if g["target"] is not None:
            tgt = g["target"]
        elif g["reference"]:
            tgt = key(lens[g["reference"]])
        else:
            tgt = max(key(lens[n]) for n in mem)
        targets[g["name"]] = (tgt, g["tol"])
    groups = [g for g in groups if g["name"] in targets]

    def net_lengths(board, g):
        mem = LM.members(g, list(facts["net_pads"]))
        return LM.lengths_from_board(board, mem, stackup, planes, g["include_vias"])

    from fluxplace import rules as RU
    rules = RU.load(cons)
    clr = lambda net: float(rules.resolve({"net": net}, "clearance_mm")[0] or 0.127)
    rep = TU.tune_match(a.board, a.out or a.board, groups, net_lengths, targets,
                        kicad_cli=a.kicad_cli, max_rounds=a.rounds, clearance_of=clr,
                        log=lambda m: print(m, flush=True))
    OG.refill(a.out or a.board)
    bad = [r for r in rep if r["status"] != "ok"]
    for r in rep:
        print(f"  {r['group']}/{r['net']}: {r.get('before')} -> {r.get('after')} {r['unit']} [{r['basis']}] "
              f"(target {r['target']:.2f} +/- {r['tol']:g}) {r['status']} {r.get('note', '')}")
    return 1 if bad else 0


def cmd_datasheets(a):
    """Fetch every MPN's datasheet into the project (DigiKey/Mouser APIs),
    record it in datasheets.json with a hash, and say which ones need a
    browser. With --adopt, register PDFs dropped in by hand."""
    import json as _json
    from fluxplace import partdocs as PD2, sourcing as S
    mpns = []
    if a.mpn_map:
        m = _json.load(open(a.mpn_map))
        mpns = [v for k, v in m.items() if not k.startswith("_") and isinstance(v, str) and v]
    elif a.board:
        path = S.find_map(a.board)
        if path:
            m = _json.load(open(path))
            mpns = [v for k, v in m.items() if not k.startswith("_") and isinstance(v, str) and v]
    mpns += list(a.mpn or [])
    if a.adopt:
        files = dict(x.split("=", 1) for x in a.adopt)
        PD2.adopt(a.out, files, log=lambda m: print(m, flush=True))
    urls = dict(x.split("=", 1) for x in (a.url or []))
    res = PD2.fetch(mpns, a.out, refresh=a.refresh, urls=urls,
                    log=lambda m: print(m, flush=True))
    have = sum(1 for v in res.values() if v)
    miss = sorted(k for k, v in res.items() if not v)
    print(f"datasheets: {have}/{len(res)} on disk in {a.out}")
    if miss:
        print("  need a browser (drop the PDF in and run --adopt MPN=file.pdf): "
              + ", ".join(miss))
        raise SystemExit(1 if a.strict else 0)


def cmd_speccheck(a):
    """Documentation gate on a netlist spec: MPN, datasheet on disk, pinmap
    evidence on the cited page. Exit 1 on FAIL."""
    import json as _json
    from fluxplace import partdocs as PD2
    spec = _json.load(open(a.spec))
    mpn_map = {}
    if a.mpn_map:
        mpn_map = {k: v for k, v in _json.load(open(a.mpn_map)).items()
                   if not k.startswith("_") and isinstance(v, str)}
    if a.cite:
        spec, rep = PD2.cite(spec, a.datasheets, mpn_map=mpn_map, only_missing=not a.recite,
                             log=lambda m: print(m, flush=True))
        if a.write:
            with open(a.spec, "w") as fh:
                _json.dump(spec, fh, indent=1)
            print(f"spec-check: pinmap_source written for "
                  f"{sum(1 for r in rep if r[2] >= 0.8)} part(s) -> {a.spec}")
    finds = PD2.spec_check(spec, a.datasheets, mpn_map=mpn_map, strict=not a.lenient)
    n = {"FAIL": 0, "WARN": 0, "INFO": 0}
    for level, code, ref, msg in finds:
        n[level] += 1
        print(f"  {level:4s} {code:24s} {msg}")
    print(f"spec-check: {n['FAIL']} FAIL, {n['WARN']} WARN, {n['INFO']} INFO")
    if n["FAIL"]:
        raise SystemExit(1)


def cmd_lock(a):
    from fluxplace import review as R
    facts = R.facts_from_board(a.board)
    print(R.lock_toml(facts, [r.strip() for r in a.refs.split(",") if r.strip()]), end="")


def cmd_review(a):
    """Standalone design review; exit 1 per --fail-on."""
    from fluxplace import review as R
    findings = _review_gate(a, stage="review", hard=False)
    n = {"fail": sum(f["level"] == "FAIL" for f in findings),
         "warning": sum(f["level"] == "WARN" for f in findings)}
    if a.fail_on == "warning" and (n["fail"] or n["warning"]):
        raise SystemExit(1)
    if a.fail_on == "error" and n["fail"]:
        raise SystemExit(1)


def cmd_bomopt(a):
    """Part-count report; exit 1 only on a FAIL budget finding."""
    import json as _json
    from fluxplace import bomopt as BO
    from fluxplace import constraints as C
    spec = _json.load(open(a.spec))
    mpn = _json.load(open(a.mpn_map)) if a.mpn_map else None
    res = BO.analyze(spec.get("components", []), spec.get("nets", {}), mpn,
                     C.load(a.constraints))
    s = res["summary"]
    print(f"BOM-OPT — {a.spec}")
    print(f"  total parts      {s['total_parts']}  (fitted {s['fitted_parts']}, "
          f"DNP {s['dnp_parts']})")
    print(f"  unique parts     {s['unique_parts']}  (MPNs {s['unique_mpns']}, "
          f"passive values {s['unique_values']}, single-use {s['single_use_mpns']})")
    print("  by kind          " + "  ".join(f"{k}={v}" for k, v in s["by_kind"].items() if v))
    print(f"  passive ratio    {s['passive_ratio']:.0%}")
    place = lines = 0
    for code in sorted({f["code"] for f in res["findings"]}):
        group = [f for f in res["findings"] if f["code"] == code]
        print(f"\n{code} ({len(group)})")
        for f in group:
            print(f"  {f['level']:4s} {f['msg']}")
            sv = f.get("saves") or {}
            place += sv.get("placements", 0)
            lines += sv.get("lines", 0)
    print(f"\nPotential reduction: {place} placements, {lines} line items "
          f"(candidates — review each)")
    if a.json:
        with open(a.json, "w") as fh:
            _json.dump(res, fh, indent=1)
        print(f"bom-opt: wrote {a.json}")
    if any(f["level"] == "FAIL" for f in res["findings"]):
        raise SystemExit(1)


def cmd_rules(a):
    """Selector-rule cascade: effective value per key with every matching rule (winner marked), or the rule table."""
    import json as _json
    from fluxplace import constraints as C
    from fluxplace import rules as RU
    cons = C.load(a.constraints)
    try:
        rs = RU.load(cons)
    except RU.RuleError as e:
        print(f"{e.code}: {e}")
        raise SystemExit(1)
    nets, fps = RU.board_objects(a.board)
    if a.net or a.ref:
        if a.net:
            obj = next((n for n in nets if n["net"] == a.net), None)
            if obj is None:
                print(f"RULE_MATCHES_NOTHING: no net '{a.net}' on {a.board} (cascade uses name only)")
                obj = {"net": a.net}
        else:
            obj = next((f for f in fps if f["ref"] == a.ref), None)
            if obj is None:
                print(f"RULE_MATCHES_NOTHING: no footprint '{a.ref}' on {a.board}")
                obj = {"ref": a.ref}
        keys = sorted({k for r in rs if RU.matches(r, obj) for k in r.set})
        out = {"object": {k: v for k, v in obj.items() if k != "props"}, "keys": {}}
        for k in keys:
            hits = rs.cascade(obj, k)
            out["keys"][k] = {"value": hits[0][1], "provenance": hits[0][0].label,
                              "cascade": [{"rule": r.label, "value": v, "level": r._level,
                                           "important": r.important, "won": i == 0}
                                          for i, (r, v) in enumerate(hits)]}
        if a.json:
            print(_json.dumps(out, indent=2))
            return
        print(f"RULES {'net' if a.net else 'ref'} {a.net or a.ref}")
        if not keys:
            print("  no rule sets a key for this object")
        for k, v in out["keys"].items():
            print(f"  {k} = {v['value']}   <- {v['provenance']}")
            for c in v["cascade"]:
                tag = "WIN " if c["won"] else "    "
                imp = " important" if c["important"] else ""
                print(f"      {tag}{c['rule']:<36} level {c['level']:>3}{imp}  -> {c['value']}")
        return
    # table of every rule: how many objects it matches, and what matches nothing
    elements = RU.board_element_objects(a.board)
    notes = []
    warn = RU.check_matches(rs, nets=[n["net"] for n in nets], refs=[f["ref"] for f in fps],
                            objects=elements, notes=notes)
    rows = []
    for r in rs:
        objs = nets + fps
        n_match = sum(1 for o in elements if RU.matches(r, o)) if r.select else len(objs)
        matched = RU.matched_nets(r, nets) if r.select else [n["net"] for n in nets]
        rows.append({"rule": r.label, "select": r.describe_select(), "set": r.set, "important": r.important,
                     "matches": n_match, "nets": matched, "legacy": r.legacy})
    if a.json:
        print(_json.dumps({"rules": rows, "warnings": [m for _r, m in warn], "notes": notes}, indent=2))
        return
    print(f"RULES {a.board} — {len(rows)} rule(s), {len(nets)} nets, {len(fps)} footprints, "
          f"{len(elements)} board objects")
    for row in rows:
        imp = " important" if row["important"] else ""
        src = " (legacy)" if row["legacy"] else ""
        print(f"  {row['rule']:<36} matches {row['matches']:>4}  {row['select']}{src}{imp}")
        shown = ", ".join(row["nets"][:5]) + (f" (+{len(row['nets']) - 5} more)" if len(row["nets"]) > 5 else "")
        print(f"      nets ({len(row['nets'])}): {shown or '-'}")
        print(f"      set {row['set']}")
    for n in notes:
        print(f"RULE_NOT_CHECKED: {n}")
    for _r, m in warn:
        print(f"RULE_MATCHES_NOTHING: {m}")


def cmd_route_diagnose(a):
    """Read-only: why did these nets not route, and what to move. Exit 0."""
    import json as _json
    from fluxplace import routediag as RD
    B = RD.load(a.board, track=a.track, clearance=a.clearance)
    if a.nets:
        failed, src = [n for n in a.nets.split(",") if n], "--nets"
    elif a.log:
        st = RD.parse_logs(a.log)
        failed, src = sorted(n for n, s in st.items() if s == "FAILED"), "log"
    else:
        failed, src = RD.unrouted_nets(B), "board (2+ pads, no copper)"
    print(f"route-diagnose - {a.board}: {len(failed)} failed net(s) from {src}")
    cons, spec = _text_ctx(a)
    res = RD.diagnose(B, failed, cons or None, spec)
    print(RD.format_text(res))
    if a.json:
        with open(a.json, "w") as fh:
            _json.dump(res, fh, indent=1)
        print(f"route-diagnose: wrote {a.json}")


def _text_ctx(a):
    """(constraints dict, spec dict|None) from --constraints / --spec."""
    import json as _json
    from fluxplace import constraints as C
    spec = _json.load(open(a.spec)) if getattr(a, "spec", None) else None
    return C.load(getattr(a, "constraints", None)), spec


def cmd_clusters(a):
    """IC support clusters; --apply re-seats far support parts, ICs never move."""
    from fluxplace import routediag as RD, clusters as CL, boardwrite as BW
    cons, spec = _text_ctx(a)
    B = RD.load(a.board)
    cl, links = CL.build(B, cons, spec)
    far0 = CL.far_parts(cl)
    print(CL.format_text(cl, links))
    print(f"clusters: {len(cl)} IC(s), {sum(len(c['supports']) for c in cl)} support part(s), "
          f"{len(far0)} beyond limit")
    if not a.apply:
        return
    if not a.out:
        raise SystemExit("clusters --apply needs --out (the input board is never overwritten)")
    from fluxplace import legality as LGL, routable as RB
    x0, y0, x1, y1 = B["bbox"]
    L = LGL.Legal(B, cons, RB._fixed_set(B, cons))
    before = L.issues()
    fx = RB._fixed_set(B, cons)
    iss0, org = LGL.issue_set(B, cons, fx), LGL.origin_of(B)
    left = []
    moves = CL.reseat(B, cl, everything=a.all, guess=a.guess, bounds=(x0 + 0.2, y0 + 0.2, x1 - 0.2, y1 - 0.2),
                      log=print, legal=L, strict=not a.best_effort, left=left)
    rev, whole = LGL.self_audit(B, cons, fx, iss0, org, log=print)
    if rev:
        moves = LGL.moves_since(B, org)
    BW.move_file(a.board, a.out, moves)
    cl2, _ = CL.build(B, cons, spec)
    after = LGL.Legal(B, cons, RB._fixed_set(B, cons)).issues()
    new = {k: sorted(set(after[k]) - set(before[k])) for k in after}
    print(f"clusters --apply: moved {len(moves)} part(s); beyond limit {len(far0)} -> "
          f"{len(CL.far_parts(cl2))}; wrote {a.out}")
    for ref, why in left:
        print(f"  left in place: {ref}: {why}")
    print("legality (new violations vs the input): " + ", ".join(f"{k} {len(v)}" for k, v in new.items())
          + (" -- " + "; ".join(f"{k}: {', '.join(v[:6])}" for k, v in new.items() if v) if LGL.count(new) else ""))


def cmd_templates(a):
    """[placement.template.*] structure templates (tree / chain): place them, print the lengths."""
    from fluxplace import routediag as RD, templates as TP, legality as LGL, routable as RB, boardwrite as BW
    cons, spec = _text_ctx(a)
    B = RD.load(a.board)
    try:
        T = TP.load(B, cons)
    except ValueError as e:
        raise SystemExit(f"templates: {e}")
    if not T:
        raise SystemExit("templates: no [placement.template.*] in the constraints")
    fixed = RB._fixed_set(B, cons)
    L = LGL.Legal(B, cons, fixed)
    before = L.issues()
    iss0, org = LGL.issue_set(B, cons, fixed), LGL.origin_of(B)
    moves = {}
    for t in T:
        mv, rep = TP.place(B, cons, spec, t, fixed=fixed, legal=L)
        moves.update(mv)
        print(TP.format_report(rep))
    rev, whole = LGL.self_audit(B, cons, fixed, iss0, org, log=print)
    if rev:
        moves = LGL.moves_since(B, org)
    after = LGL.Legal(B, cons, fixed).issues()
    new = {k: sorted(set(after[k]) - set(before[k])) for k in after}
    print("legality (new violations vs the input): " + ", ".join(f"{k} {len(v)}" for k, v in new.items()))
    if a.out:
        BW.move_file(a.board, a.out, moves)
        print(f"templates: moved {len(moves)} part(s); wrote {a.out}")


def cmd_floorplan_check(a):
    """Area budget before placing: block demand vs the free area of its region/side."""
    import json as _json
    from fluxplace import routediag as RD, floorplan as FP
    cons, spec = _text_ctx(a)
    B = RD.load(a.board)
    f, rows = FP.check(B, cons, spec)
    print(FP.format_text(f, rows, a.board))
    if a.json:
        with open(a.json, "w") as fh:
            _json.dump({"rows": rows, "findings": f}, fh, indent=1)
    if any(x["level"] == "FAIL" for x in f):
        raise SystemExit(1)


def cmd_route_ready(a):
    """Pre-route gate scorecard; exit 1 when any FAIL-level check fails."""
    import json as _json
    from fluxplace import routediag as RD, preroute as PR
    cons, spec = _text_ctx(a)
    B = RD.load(a.board, track=getattr(a, "track", None), clearance=getattr(a, "clearance", None))
    if getattr(a, "collisions_only", False):
        from fluxplace import legality as LGL, review as R
        waivers = list(a.waive or []) + list((cons.get("review") or {}).get("waive", []))
        f = [x for x in LGL.collisions(B, cons) if not R._waived(x, waivers)]
        rel = {t.strip() for t in (a.relevance or "").split(",") if t.strip()}
        print(LGL.format_collisions([x for x in f if not rel or set(x["relevance"]) & rel], a.board))
        if a.json:
            with open(a.json, "w") as fh:
                _json.dump({"findings": f}, fh, indent=1)
        if any(x["level"] == "FAIL" for x in f):
            raise SystemExit(1)
        return
    f, card = PR.run(B, cons, spec, waivers=a.waive, plan=PR.use_plan(cons), collide=True)
    if getattr(a, "relevance", None):           # display filter only: the verdict still counts every finding
        keep = {t.strip() for t in a.relevance.split(",") if t.strip()}
        shown = [x for x in f if x["code"] != "COLLISION" or set(x.get("relevance", ())) & keep]
    else:
        shown = f
    print(PR.format_text(shown, card, a.board))
    if a.all:
        for x in shown:
            print(f"  {x['level']:4s} {x['code']:19s} {x['msg']}")
    if a.json:
        with open(a.json, "w") as fh:
            _json.dump({"scorecard": card, "findings": f}, fh, indent=1, default=sorted)
        print(f"route-ready: wrote {a.json}")
    if a.strip_stale:
        from fluxplace import placechecks as PCK
        out = a.out or a.board
        text, n = PCK.strip_stale_text(open(a.board, errors="surrogateescape").read(), B)
        open(out, "w", errors="surrogateescape").write(text)
        print(f"route-ready --strip-stale: removed {n} stale track/via item(s) -> {out}")
    if card["fail"]:
        raise SystemExit(1)


def cmd_anchor_check(a):
    """Per-anchor rotation table; --apply rotates the misoriented ones to their best option."""
    from fluxplace import routediag as RD, anchors as AN, boardwrite as BW
    cons, spec = _text_ctx(a)
    B = RD.load(a.board)
    res = AN.evaluate(B, cons, spec)
    tol = float((cons.get("preroute") or {}).get("anchor_tolerance_pct", 15.0))
    print(AN.format_text(res, tol))
    bad = [r for r in res if r["worse_pct"] > tol and r["best"]["delta"] % 360 != 0]
    print(f"anchor-check: {len(res)} anchor(s), {len(bad)} ANCHOR_MISORIENTED (> {tol:g}% worse than best)")
    if a.apply:
        if not a.out:
            raise SystemExit("anchor-check --apply needs --out")
        mv = AN.moves_for_best(B, res, cons, spec, tol)
        BW.move_file(a.board, a.out, mv)
        print(f"anchor-check --apply: rotated {', '.join(sorted(mv)) or 'nothing'}; wrote {a.out}")


def cmd_silk(a):
    """Silkscreen check; --fix re-fits the reference texts (never moves a footprint, track, via or zone)."""
    import json as _json
    from fluxplace import silk as SK
    cons, spec = _text_ctx(a)
    try:
        sc, cfg, before = SK.run_check(a.board, cons, a.side, spec=spec, title=a.title)
    except RuntimeError as e:
        raise SystemExit(f"silk: cannot run: {e}")
    order = {"FAIL": 0, "WARN": 1, "INFO": 2}
    shown, label = before, ""
    res = None
    if a.fix:
        out = a.board if a.in_place else (a.out or os.path.splitext(a.board)[0] + ".silk.kicad_pcb")
        res = SK.fit(sc, cfg, before, side=a.side)
        shown, _ = SK.apply_guarded(a.board, out, sc, cfg, before, res, a.side)
        label = out
    if a.json:
        print(_json.dumps({"board": label or a.board, "before": SK.counts(before), "after": SK.counts(shown),
                           "fit": None if res is None else {k: v for k, v in res.items() if k not in ("plan", "findings")},
                           "findings": [{k: v for k, v in f.items() if k not in ("tid",)} for f in shown]}, indent=1))
    else:
        for f in shown:
            print(f"  {f['level']:4s} {f['code']} {f['msg']}")
        if res is not None:
            b, c2 = SK.counts(before), SK.counts(shown)
            print("silk --fix: " + "  ".join(f"{k} {b.get(k, 0)}->{c2.get(k, 0)}" for k in SK.CODES if b.get(k) or c2.get(k)))
            for sd, ti in res["title"].items():
                print(f"  TITLE {sd}: '{ti['text']}' at ({ti['x']}, {ti['y']}) height {ti['h']:g} mm, "
                      f"{'rotated 90, ' if ti['angle'] else ''}clearance margin {ti['margin_mm']} mm")
            for r in res.get("reverted", []):
                print(f"  REVERTED {r}: the fit made it worse; left where it was")
            ug = SK.unfixable_graphics(shown)
            if ug:
                print("  NEEDS FOOTPRINT FIX (silk --fix cannot move footprint artwork): "
                      + ", ".join(f"{r} {c.replace('SILK_', '')}" for r, c in ug))
            for r in res["hidden"]:
                print(f"  HIDDEN {r}: no legal silkscreen spot; reference moved to the Fab layer")
            print(f"silk --fix: {len(res['moved'])} reference(s) re-placed ({len(res['shrunk'])} shrunk), "
                  f"{len(res['hidden'])} hidden on silk, {len(res['kept'])} left (no better spot); wrote {label}")
        n = {l: sum(f["level"] == l for f in shown) for l in order}
        print(f"silk: {n['FAIL']} FAIL, {n['WARN']} WARN, {n['INFO']} INFO"
              + ("" if res is None else " (after fix)"))
    if SK.fails(shown):
        raise SystemExit(1)


def cmd_magnetics(a):
    """LAN magnetics wiring vs the datasheet pin table declared in the spec; exit 1 on a FAIL."""
    import json as _json
    from fluxplace import review as R, magnetics as MG
    cons, spec = _text_ctx(a)
    if not spec:
        raise SystemExit("magnetics: --spec is required (parts declare their channels there)")
    facts = R.facts_from_board(a.board)
    findings = MG.check(facts["parts"], facts.get("net_pads") or {}, spec, cons)
    order = {"FAIL": 0, "WARN": 1, "INFO": 2}
    findings.sort(key=lambda f: (order[f["level"]], f["code"], f["msg"]))
    n = MG.summary(findings)
    chans = sum(len(c.get("channels") or []) for c in spec.get("components", []) if isinstance(c, dict))
    if a.json:
        print(_json.dumps({"board": a.board, "channels": chans, "counts": n, "findings": findings}, indent=1))
    else:
        for f in findings:
            print(f"  {f['level']:4s} {f['code']} {f['msg']}")
        print(f"magnetics: {n['FAIL']} FAIL, {n['WARN']} WARN, {n['INFO']} INFO ({chans} declared channel(s))")
    if n["FAIL"]:
        raise SystemExit(1)


def _route_gate(a, board_path, stage="routing"):
    """Refuse to start routing on a placement route-ready rejects, unless --force.
    Prints the scorecard either way."""
    from fluxplace import routediag as RD, preroute as PR
    cons, spec = _text_ctx(a)
    B = RD.load(board_path, track=getattr(a, "track", None), clearance=getattr(a, "clearance", None))
    f, card = PR.run(B, cons, spec, waivers=list(getattr(a, "waive", None) or ()), plan=PR.use_plan(cons), collide=True)
    print(PR.format_text(f, card, board_path), flush=True)
    if card["fail"]:
        if getattr(a, "force", False):
            print(f"    route-ready FAILED ({card['fail']}) - continuing to {stage} (--force)")
        else:
            raise SystemExit(f"ABORT before {stage}: route-ready failed ({card['fail']} FAIL). "
                             f"Fix the placement, waive with [review] waive, or pass --force.")
    return f, card


def _add_gate_args(p):
    p.add_argument("--spec", default=None, help="spec JSON (pin names) for the pre-route gate")
    p.add_argument("--force", action="store_true",
                   help="route even though route-ready failed (scorecard still printed)")
    if not any(o.dest == "constraints" for o in p._actions):
        p.add_argument("--constraints", default=None, help="constraints TOML ([placement], [preroute], [review] waive)")
    p.add_argument("--waive", action="append", default=[], help="pre-route CODE:REGEX, repeatable")


def _add_review_args(p, gate=False):
    p.add_argument("--spec", default=None,
                   help="netlist/board spec JSON (board size, layers, "
                        "components with pinmaps) to hold the copper against")
    p.add_argument("--constraints", default=None,
                   help="constraints TOML: [env], [nets], [rf], [power], "
                        "[protection] rules for the review gate")
    p.add_argument("--mpn-map", default=None,
                   help="ref->MPN map (auto-discovered when omitted)")
    p.add_argument("--no-api", action="store_true",
                   help="skip DigiKey/Mouser part data (offline review)")
    p.add_argument("--datasheets", default=None,
                   help="datasheet directory with datasheets.json (default: "
                        "[docs] datasheets in the constraints)")
    p.add_argument("--refresh", action="store_true",
                   help="ignore the 7-day part-data cache")
    p.add_argument("--waive", action="append", default=[],
                   help="suppress findings: CODE:REGEX, repeatable")
    if gate:
        p.add_argument("--no-review", action="store_true",
                       help="skip the design-review gate (ships known FAILs)")


def _models_lib_dir(a):
    board_dir = os.path.dirname(os.path.abspath(a.board))
    return a.lib_dir or os.path.normpath(
        os.path.join(board_dir, "..", "lib", "3dmodels"))


def _models_mpn_map(a, S, _json):
    """{ref: MPN}, from --mpn-map or auto-found beside the board (the same
    mpn_map.json sourcing/review use), skipping "_"-prefixed keys."""
    path = a.mpn_map or S.find_map(a.board)
    if not path:
        return {}, None
    raw = _json.load(open(path))
    return ({ref: mpn for ref, mpn in raw.items()
            if not ref.startswith("_") and isinstance(mpn, str) and mpn},
           path)


def _models_sources_json(a, mpn_map_path, lib_dir):
    if a.sources:
        return a.sources
    if mpn_map_path:
        beside = os.path.join(os.path.dirname(mpn_map_path),
                              "model_sources.json")
        if os.path.exists(beside):
            return beside
    return os.path.join(lib_dir, "model_sources.json")


def cmd_models(a):
    """Fetch real manufacturer STEP models (DigiKey /media, Mouser assist,
    or --fetch's EasyEDA/LCSC) for footprints with missing/broken 3D
    models, wire them in, save."""
    import json as _json
    from fluxplace import models as M
    from fluxplace import kicad_io as IO
    from fluxplace import sourcing as S

    if a.check:
        board = IO.load(a.board)
        todo = M.missing_models(board, board_path=a.board)
        for ref, fp_name, reason in todo:
            print(f"  {ref:6s} {reason:12s} {fp_name[:50]}")
        print(f"models --check: {len(todo)} footprints missing a model")
        sys.exit(1 if todo else 0)

    if a.fetch:
        lib_dir = _models_lib_dir(a)
        mpn_map, mpn_map_path = _models_mpn_map(a, S, _json)
        sources_json = _models_sources_json(a, mpn_map_path, lib_dir)
        rep = M.fetch_missing(a.board, mpn_map, lib_dir, sources_json)
        for ref, mpn, fname in rep["attached"]:
            print(f"  {ref:6s} attached   {mpn} -> {fname}")
        for ref, reason in rep["unresolved"]:
            print(f"  {ref:6s} unresolved {reason}")
        print(f"models --fetch: {len(rep['attached'])} attached, "
              f"{len(rep['unresolved'])} unresolved -> {sources_json}")
        sys.exit(1 if rep["unresolved"] else 0)

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
          f"mounting={intent['mounting']}, env={intent.get('environment')})")
    if a.constraints_out and intent.get("environment"):
        from fluxplace import constraints as C
        block = C.env_toml(intent["environment"])
        mode = "a" if os.path.exists(a.constraints_out) else "w"
        with open(a.constraints_out, mode) as fh:
            fh.write(("\n" if mode == "a" else "") + block)
        print(f"[env] -> {a.constraints_out}")
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
        from fluxplace import review as _R
        be = _R.board_backend()
        print("\nboard backend: %s — %s" % (be, (
            "pcbnew (SWIG) present: every command available"
            if be == "swig" else
            "no pcbnew module (KiCad 11, or the wrong interpreter): review, lock "
            "and the spec/keep-out/policy gates read the board file directly; "
            "commands that WRITE a board (place, repair, patch, finish, tune, "
            "planes) need KiCad <= 10 until the IPC port")))
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


def _list_stackup_templates(detail, as_json):
    """Named stackup templates (data/stackup_templates.json); --detail adds the
    standard impedance cases computed from each template's own layers."""
    import json as _json
    from fluxplace import stackup as ST, impedance as IM
    rows = []
    for n in ST.profile_names():
        p = ST.PROFILES[n]
        row = {"name": n, "kind": p.get("kind", "fab"), "vendor": p["vendor"],
               "layers": sum(1 for l in p["layers"] if l["type"] == "copper"),
               "thickness_mm": ST.total_thickness(n), "source": p.get("source", ""),
               "description": p["description"]}
        if detail:
            row["impedance"] = IM.template_report(n)
        rows.append(row)
    if as_json:
        print(_json.dumps(rows, indent=2)); return
    for r in rows:
        print("%-16s %-8s %dL %5.2f mm  %s" % (r["name"], r["kind"], r["layers"],
                                              r["thickness_mm"], r["description"]))
        print("    source: %s" % r["source"])
        if detail:
            i = r["impedance"]
            if not i["model"]:
                print("    impedance: no reference plane, not computed")
                continue
            se, d90, d100 = i["se50"], i["diff90"], i["diff100"]
            fmt = lambda o: ("%.3f mm" % o["width_mm"] + (" / gap %.3f" % o["gap_mm"] if o["gap_mm"] else "")
                             if o else "not reachable")
            print("    L1 (F.Cu, %s, h %.4f mm, Er %.2f): 50 ohm %s | 90 ohm diff %s | 100 ohm diff %s"
                  % (i["model"], i["h_mm"], i["er"], fmt(se), fmt(d90), fmt(d100)))
    print()
    print("Closed-form IPC-2141 estimates (+-10%): a fab field solver signs off controlled impedance.")


def cmd_stackup(a):
    """Report the layer stack, plane assignment and netclass geometry — and say
    plainly whether controlled impedance can be verified from these files."""
    import json as _json
    if a.list_templates:
        return _list_stackup_templates(a.detail, a.json)
    if not a.board:
        raise SystemExit("stackup: --board is required (or --list-templates)")
    from fluxplace import audit
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

    a.profile = ST.resolve_template(a.profile or a.template, a.constraints)
    if not a.profile:
        print("need --profile / --template (or --list-profiles). Available: %s"
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

    ck = res                                    # run() returns the proof fields next to the counts
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
    if a.spec:
        from fluxplace import parts as _PT, constraints as _C
        _PT.autogate(a.spec, _C.load(getattr(a, "constraints", None)), a.board, "schematic", a.datasheets, a.mpn_map)
    if a.spec and a.datasheets and not a.undocumented:
        from fluxplace import partdocs as PD2
        mpn_map = {}
        if a.mpn_map:
            mpn_map = {k: v for k, v in _json.load(open(a.mpn_map)).items()
                       if not k.startswith("_") and isinstance(v, str)}
        finds = PD2.spec_check(_json.load(open(a.spec)), a.datasheets, mpn_map=mpn_map)
        fails = [f for f in finds if f[0] == "FAIL"]
        for level, code, ref, msg in fails:
            print(f"  FAIL {code:24s} {msg}")
        if fails:
            raise SystemExit(f"schematic: refusing to generate — {len(fails)} undocumented "
                             f"part(s). Fetch datasheets / cite pinmap_source, or pass "
                             f"--undocumented to override.")
    if getattr(a, "layout", "blocks") == "blocks":
        return _schematic_blocks(a)
    res = SC.generate(a.spec or a.board, a.out, title=a.title,
                      from_copper=bool(a.board and not a.spec))
    print("wrote %s" % res["path"])
    print("  %d components, %d nets, %d pins, %.0f x %.0f mm, %d column(s)"
          % (res["components"], res["nets"], res["pins"],
             res["sheet_mm"][0], res["sheet_mm"][1], res["columns"]))
    if a.spec and a.no_verify:
        print()
        print("  VERIFY: NOT_VERIFIED (--no-verify: the netlist was not compared with the spec)")
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


def _schematic_blocks(a):
    """--layout blocks: hierarchical drafted schematic + netlist/ERC/legibility gate + renders (fluxplace.schblocks)."""
    import json as _json
    from fluxplace import schinv as SI, schblocks as SB, constraints as _C
    if not (a.spec or a.board):
        raise SystemExit("schematic: --spec or --board is required")
    cons = _C.load(getattr(a, "constraints", None))
    M = SI.load(spec=a.spec, board=None if a.spec else a.board)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    st = SB.generate(M, a.out, cons, title=a.title)
    stem = os.path.splitext(os.path.abspath(a.out))[0]
    names = [b["name"] for b in st["blocks"]]
    rdir = None if a.no_render else (a.render or stem + "-render")
    g = {"ok": True}
    if not a.no_verify:
        g = SB.gate(M, a.out, kicad_cli=a.kicad_cli, render_dir=rdir, sheet_names=names)
    elif rdir:
        g["render"] = SB.render(a.out, rdir, a.kicad_cli)
    nl = g.get("netlist", {})
    lg = g.get("legibility", {"counts": {}, "total": 0})
    er = g.get("erc", {})
    verdict, code = _schematic_verdict(a.no_verify, nl.get("ok"), lg.get("total", 0),
                                       getattr(a, "allow_unverified", False))
    rep = {"verdict": verdict, "generate": st, "gate": g}
    with open(stem + ".schematic.json", "w") as fh:
        _json.dump(rep, fh, indent=1, default=str)
    lines = ["schematic: %s - %d blocks, %d parts, %d nets, %d wires, %d local / %d global labels, %d joins (%d left as labels)"
             % (verdict, len(st["blocks"]), len(M["comps"]), len(M["nets"]), sum(b["wires"] for b in st["blocks"]),
                sum(b["labels_local"] for b in st["blocks"]), sum(b["labels_global"] for b in st["blocks"]),
                st["routed"], st["unrouted"])]
    lines.append("blocks: " + ", ".join("%s %d" % (b["name"], b["refs"]) for b in st["blocks"]))
    if not a.no_verify:
        lines.append("netlist: %s - %d/%d nets, %d/%d pins, %d/%d components, %d declared no-connects"
                     % (nl.get("verdict"), nl.get("nets_sch", 0), nl.get("nets_spec", 0), nl.get("pins_sch", 0),
                        nl.get("pins_spec", 0), nl.get("components_sch", 0), nl.get("components_spec", 0), nl.get("nc_declared", 0)))
        for k in ("error", "missing_nets", "extra_nets", "differing_nets", "split_names", "missing_components",
                  "extra_components", "differing_components"):
            if nl.get(k):
                lines.append("  %s: %s" % (k, str(nl[k])[:300]))
        lines.append("erc: %d - %s" % (er.get("total", 0), ", ".join("%s %d" % kv for kv in er.get("by_type", {}).items()) or "none"))
        lines.append("legibility: %d - %s" % (lg.get("total", 0), ", ".join("%s %d" % kv for kv in lg.get("counts", {}).items())))
        lines.append(g["verified"])
    if g.get("render"):
        r = g["render"]
        lines.append("render: %s (%d PNG pages, %d SVG)" % (os.path.dirname(r["pdf"] or r["svg_dir"]), len(r["pngs"]), len(r["svgs"])))
    lines.append("wrote %s (+%d sheets)" % (a.out, len(st["blocks"])))
    lines.append("full report: %s.schematic.json" % stem)
    with open(stem + ".schematic.txt", "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    if code:
        raise SystemExit(code)


def _schematic_verdict(no_verify, nl_ok, legibility_total, allow_unverified=False):
    """-> (verdict, exit code). --no-verify is NOT_VERIFIED, never PASS; it exits 0 only with --allow-unverified."""
    if no_verify:
        return "NOT_VERIFIED", (0 if allow_unverified else 1)
    if not nl_ok:
        return "FAIL", 1
    return ("WARN" if legibility_total else "PASS"), 0


def cmd_route_status(a):
    """One line from a router run's status file. Exit 0 running/done, 1 failed, 3 none."""
    from fluxplace import routestatus as RS
    sp = RS.resolve(a.target)
    st = RS.load(sp) if sp else None
    if not st:
        print(f"route-status: no status file for {a.target}")
        raise SystemExit(3)
    print(RS.one_line(st))
    if st["state"] in ("failed", "timeout", "dead"):
        raise SystemExit(1)


def cmd_route_wait(a):
    """Block until the router run is done/stalled/checkpointed; ONE summary line.
    Exit 0 condition met, 1 run failed, 2 timeout, 3 no status file."""
    from fluxplace import routestatus as RS
    sp = RS.resolve(a.target)
    if not sp:
        print(f"route-wait: no status file for {a.target} (is a route running?)")
        raise SystemExit(3)
    reason, st = RS.wait(sp, a.timeout, until=a.until, stall_s=a.stall, poll=a.poll)
    if st is None:
        print(f"route-wait: status file {sp} unreadable")
        raise SystemExit(3)
    line = f"route-wait: {reason} - {RS.one_line(st)}"
    if reason in ("done", "failed", "stall", "timeout") and not a.no_diagnose and st.get("out"):
        try:
            cons, spec = _text_ctx(a)
            dpath, nf = RS.diagnose_result(st, cons or None, spec)
            if dpath:
                line += f"  | unrouted {nf}; diagnosis {dpath}"
        except Exception as e:                      # a bad board must not hide the status
            line += f"  | diagnosis skipped ({type(e).__name__})"
    if st.get("out") and os.path.exists(st["out"]):
        line += f"  | result {st['out']}"
    print(line)
    raise SystemExit({"done": 0, "checkpoint": 0, "stall": 0, "failed": 1, "timeout": 2}.get(reason, 3))


def cmd_pipeline(a):
    """Staged pipeline with file handoffs (W/state.json). See docs/AGENT-PIPELINE.md."""
    from fluxplace import pipeline as PL
    rc = PL.run(a)
    if rc:
        raise SystemExit(rc)


def cmd_help_brief(a):
    """<= 15 lines per command from the live parser (all commands: one line each)."""
    from fluxplace import helpbrief as HB
    print(HB.main_text(a.command))


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
    _add_review_args(pd, gate=True)
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
    pw.add_argument("--constraints", help="TOML ([review] waive for UNROUTED)")
    pw.add_argument("--waive", action="append", help="CODE:REGEX (UNROUTED:^NET$)")
    pw.set_defaults(fn=cmd_pcbway)

    ps = sub.add_parser("sourcing",
                        help="grade every MPN against live DigiKey+Mouser stock")
    ps.add_argument("--board", required=True)
    ps.add_argument("--json", help="write the full report here")
    ps.add_argument("--show-ok", action="store_true",
                    help="list every part, not just the problems")
    ps.add_argument("--china", action="store_true",
                    help="also grade every MPN on LCSC/JLCPCB stock (China assembly)")
    ps.add_argument("--cn-need", type=int, default=100,
                    help="LCSC units required for CN_OK (default 100)")
    ps.add_argument("--qty", type=int, default=None,
                    help="boards to build: grade stock at refs x N, price each "
                         "line at N, pick the cheapest in-stock source "
                         "(default: [bom] quantity_target, else legacy mode)")
    ps.add_argument("--constraints", default=None,
                    help="constraints TOML (reads [bom] quantity_target)")
    ps.set_defaults(fn=cmd_sourcing)

    pa = sub.add_parser("analyze"); pa.add_argument("--board", required=True)
    pa.set_defaults(fn=cmd_analyze)

    pp = sub.add_parser("place")
    pp.add_argument("--board", required=True)
    pp.add_argument("--out", default=None)
    pp.add_argument("--strategy", choices=["radial", "flux", "pack", "quad", "build", "routable"], default="pack",
                    help="pack=hierarchical (organized+compact, default); flux=force-directed; radial; "
                         "routable=connection-driven (net classes, support clusters, anchor rotation, "
                         "chains, channel-aware legalise; text-only, keeps locked parts and anchors)")
    pp.add_argument("--constraints", default=None, help="constraints TOML ([placement], [lock], [keepout]) for routable")
    pp.add_argument("--refine", action="store_true",
                    help="with --strategy routable: only LOCAL moves from the input (nudge <= 3 mm, rotate 90, swap, "
                         "slide a support part), each kept only if the gate score improves and the result stays legal")
    pp.add_argument("--objective", choices=["default", "plan"], default="default",
                    help="routable: plan = local moves proposed from the plan-route findings, kept only when legal and "
                         "PLAN_SCORE does not get worse (also [placement] objective = \"plan\"); writes <out>.fluxmap.json")
    pp.add_argument("--seed", type=int, default=0, help="--refine: visiting order seed (deterministic)")
    pp.add_argument("--time-limit", type=float, default=None, help="--refine: seconds (default [placement] time_limit_s, 600)")
    pp.add_argument("--spec", default=None, help="spec JSON (pin names) for routable")
    pp.add_argument("--strip-stale", action="store_true",
                    help="routable: remove tracks/vias left dangling by the move")
    pp.add_argument("--rotate", choices=["none", "ortho", "fine"], default="ortho",
                    help="ortho=snap 0/90/180/270 (assembly-friendly); fine=any angle")
    pp.add_argument("--iters", type=int, default=260)
    pp.add_argument("--pad", type=float, default=0.8, help="courtyard margin mm")
    pp.add_argument("--margin", type=float, default=2.0, help="board edge margin mm")
    pp.add_argument("--no-outline", action="store_true", help="don't redraw Edge.Cuts")
    pp.add_argument("--max-growth", type=float, default=4.0,
                    help="refuse a result whose redrawn outline is more than this many times the "
                         "original board area (the pack > 1 m fault)")
    pp.add_argument("--center-board", action="store_true")
    pp.add_argument("--free-connectors", action="store_true")
    pp.add_argument("--move-locked", action="store_true")
    pp.add_argument("--seeds", type=int, default=1,
                    help="try N perturbed placements, keep the best routable one")
    pp.add_argument("--guides", action="store_true",
                    help="draw the global-route corridors on Eco1.User (build strategy)")
    pp.add_argument("--no-fit-sheet", action="store_true",
                    help="don't translate the board onto the drawing sheet")
    pp.add_argument("--engine", choices=["builtin", "cypress"], default=None,
                    help="placement engine (default [placement] engine, else builtin); cypress = NVIDIA Cypress on the GPU, "
                         "then the fluxplace legaliser; falls back to builtin when it is not installed (docs/CYPRESS.md)")
    pp.add_argument("--cypress-home", default=None, help="Cypress checkout (else $CYPRESS_HOME, else ~/tools/Cypress*)")
    pp.add_argument("--cypress-python", default=None, help="Python of the Cypress environment (else $CYPRESS_PYTHON)")
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
    _add_gate_args(pr)
    pr.set_defaults(fn=cmd_route)

    pfs = sub.add_parser("fit-sheet",
                         help="translate the whole board onto the drawing sheet "
                              "(centred above the title block; paper auto-picked)")
    pfs.add_argument("--board", required=True)
    pfs.add_argument("--out", default=None, help="write here instead of in place")
    pfs.add_argument("--paper", default="auto",
                     choices=["auto", "A5", "A4", "A3", "A2", "A1", "A0",
                              "A", "B", "C", "D"])
    pfs.add_argument("--constraints", default=None,
                     help="constraints TOML: [lock.at] and [[keepout]] rect "
                          "coordinates are shifted by the same offset, in place")
    pfs.add_argument("--shift", default=None, metavar="DX,DY",
                     help="with --constraints: only rewrite the constraints by this "
                          "offset (what a build command printed); board untouched")
    pfs.add_argument("--dry-run", action="store_true", help="plan only, write nothing")
    pfs.add_argument("--refill", action="store_true",
                     help="refill zones after the move (a translation does not need it)")
    pfs.add_argument("--fill-title-block", action="store_true",
                     help="also fill the drawing-sheet title block (title, revision, date, company)")
    pfs.add_argument("--spec", default=None, help="spec JSON (project name / version for the title block)")
    from fluxplace import questions as _Q
    _Q.add_args(pfs)
    pfs.set_defaults(fn=cmd_fit_sheet)

    ptb = sub.add_parser("title-block", help="fill the drawing-sheet title block (title, revision, date, company)")
    ptb.add_argument("board")
    ptb.add_argument("--out", default=None, help="write a copy here instead of in place")
    ptb.add_argument("--constraints", default=None)
    ptb.add_argument("--spec", default=None)
    _Q.add_args(ptb)
    ptb.set_defaults(fn=cmd_title_block)

    pt = sub.add_parser("tournament",
                        help="N placements -> gate filter -> freerouting judges -> winner")
    pt.add_argument("--board", required=True)
    pt.add_argument("--no-fit-sheet", action="store_true",
                    help="don't translate the imported winner onto the drawing sheet")
    pt.add_argument("--jar", default=None)
    pt.add_argument("--workdir", required=True)
    pt.add_argument("--passes", type=int, default=25)
    pt.add_argument("--jobs", type=int, default=3)
    pt.add_argument("--apply-winner", action="store_true")
    pt.add_argument("--pre-place", choices=["routable"], default=None,
                    help="place the input board with the connection-driven placer first")
    pt.add_argument("--constraints", default=None, help="constraints TOML for --pre-place routable")
    pt.add_argument("--spec", default=None, help="spec JSON for --pre-place routable")
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
    pf.add_argument("--board", default=None)
    pf.add_argument("--out", default=None)
    pf.add_argument("--check-only", metavar="FAB_DIR", default=None,
                    help="do not export: re-check an existing fab package's Gerbers "
                         "and drill files (gerbercheck); --constraints sets the [fab] floors")
    pf.add_argument("--kicad-cli", default="kicad-cli")
    pf.add_argument("--upload-out", default=None,
                    help="also assemble the ECAD upload set (board + pro + dru "
                         "+ schematics, no .kicad_prl) into this directory")
    pf.add_argument("--project-dir", default=None,
                    help="project dir for --upload-out (default: board's dir)")
    _add_review_args(pf, gate=True)
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
    pvm.add_argument("--constraints", default=None,
                     help="constraints TOML — [docs] project_libs is searched "
                          "(alongside KiCad's stock libraries) for the "
                          "LIBRARY-TRANSFORM check")
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
    pau.add_argument("--route-timeout", type=int, default=None, help="per-routing-stage wall-clock limit s (default [route] stage_timeout_s, 900)")
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
    pau.add_argument("--no-fit-sheet", action="store_true",
                     help="don't translate the board onto the drawing sheet "
                          "(also rewrites --constraints' absolute coordinates when it does)")
    _add_gate_args(pau)
    pau.add_argument("--pre-place", choices=["routable"], default=None,
                     help="place with the connection-driven placer (text, needs --constraints for "
                          "[placement]) instead of the route-aware builder")
    pau.add_argument("--replace-on-stall", action="store_true",
                     help="on a routing stall or failed nets: apply route-diagnose's move list to "
                          "unlocked parts, rip up only the copper of the nets that touch them and "
                          "re-route only those (<= --stall-rounds times)")
    pau.add_argument("--stall-rounds", type=int, default=3)
    pau.add_argument("--no-escape-first", action="store_true",
                     help="don't route fine-pitch signal nets before the rest")
    pau.add_argument("--max-growth", type=float, default=6.0,
                     help="refuse a placement whose redrawn outline is more than this many times "
                          "the original board area")
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
    pco.add_argument("--route-timeout", type=int, default=None, help="per-routing-stage wall-clock limit s (default [route] stage_timeout_s, 900)")
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
    pco.add_argument("--no-fit-sheet", action="store_true",
                     help="don't translate the board onto the drawing sheet")
    _add_gate_args(pco)
    pco.set_defaults(fn=cmd_compact)

    pli = sub.add_parser("lint",
                         help="design-completeness checks: missing power/IO "
                              "wiring, dead-end nets, non-latching connectors")
    pli.add_argument("--board", default=None)
    pli.add_argument("--spec", default=None, help="spec JSON: control-pin default levels and schematic checks (decoupling, I2C pull-ups, passive values)")
    pli.add_argument("--constraints", default=None, help="[lint] default_level / rails / control_pins regexes")
    pli.add_argument("--sch", default=None, help="schematic whose symbols give pin electrical types")
    pli.add_argument("--json", default=None, help="also write findings as JSON")
    pli.add_argument("--waive", action="append", default=[],
                     help="suppress findings: CODE:REGEX (matches msg or refs), "
                          "repeatable — e.g. dead-end-net:^ETH_ for a de-scoped "
                          "feature or unwired stubs on a split-board interconnect")
    pli.add_argument("--fail-on", choices=["never", "error", "warning"],
                     default="never", help="exit 1 at this severity (default never)")
    pli.set_defaults(fn=cmd_lint)

    pds = sub.add_parser("datasheets",
                         help="fetch every MPN's datasheet into the project "
                              "(DigiKey/Mouser), hash it into datasheets.json, "
                              "list the ones that need a browser")
    pds.add_argument("--out", required=True, help="datasheet directory")
    pds.add_argument("--board", default=None, help="board (mpn_map.json auto-found)")
    pds.add_argument("--mpn-map", default=None)
    pds.add_argument("--mpn", action="append", default=[], help="extra MPN, repeatable")
    pds.add_argument("--url", action="append", default=[], metavar="MPN=URL",
                     help="datasheet URL found by hand, repeatable")
    pds.add_argument("--adopt", action="append", default=[], metavar="MPN=FILE",
                     help="register a PDF already in --out, repeatable")
    pds.add_argument("--refresh", action="store_true")
    pds.add_argument("--strict", action="store_true", help="exit 1 if any are missing")
    pds.set_defaults(fn=cmd_datasheets)

    psc2 = sub.add_parser("spec-check",
                          help="documentation gate on a netlist spec: every "
                               "part has an MPN, its datasheet on disk, and a "
                               "pinmap the cited datasheet page backs")
    psc2.add_argument("--spec", required=True)
    psc2.add_argument("--datasheets", required=True)
    psc2.add_argument("--mpn-map", default=None)
    psc2.add_argument("--lenient", action="store_true", help="WARN instead of FAIL")
    psc2.add_argument("--cite", action="store_true",
                      help="find the datasheet page backing each pinmap and cite it")
    psc2.add_argument("--recite", action="store_true", help="re-cite even when cited")
    psc2.add_argument("--write", action="store_true", help="write citations into --spec")
    psc2.set_defaults(fn=cmd_speccheck)

    plk = sub.add_parser("lock",
                         help="print a [lock.at] constraints block for the "
                              "footprints matching --refs, from the board as it "
                              "stands — the bolt pattern a later revision must keep")
    plk.add_argument("--board", required=True)
    plk.add_argument("--refs", default="MH*,MK*,H*",
                     help="comma-separated reference globs (default MH*,MK*,H*)")
    plk.set_defaults(fn=cmd_lock)

    pbo = sub.add_parser("bom-opt",
                         help="part-count optimization: array, value-merge, "
                              "package, parallel and 0-ohm candidates plus the "
                              "[bom] budget (exit 1 on a FAIL budget)")
    pbo.add_argument("--spec", required=True, help="spec JSON (components, nets)")
    pbo.add_argument("--mpn-map", default=None, help="{ref: mpn} JSON")
    pbo.add_argument("--constraints", default=None, help="constraints TOML ([bom])")
    pbo.add_argument("--json", default=None, help="also write the result as JSON")
    pbo.set_defaults(fn=cmd_bomopt)
    pru = sub.add_parser("rules",
                         help="selector-rule cascade: effective per-net/footprint values with every "
                              "matching rule (winner marked); without --net/--ref the rule table")
    pru.add_argument("board", help="board .kicad_pcb")
    pru.add_argument("--constraints", default=None, help="constraints TOML ([[rule]] and per-object blocks)")
    pru.add_argument("--net", default=None, help="net name: show its cascade")
    pru.add_argument("--ref", default=None, help="footprint reference: show its cascade")
    pru.add_argument("--json", action="store_true", help="JSON output")
    pru.set_defaults(fn=cmd_rules)

    prd = sub.add_parser("route-diagnose",
                         help="read-only: explain why nets stayed unrouted and "
                              "which parts to move (never edits the board)")
    prd.add_argument("board")
    prd.add_argument("--log", action="append", default=[],
                     help="router log (repeatable); a net's last attempt decides")
    prd.add_argument("--nets", default=None, help="comma-separated failed nets")
    prd.add_argument("--json", default=None, help="also write the result as JSON")
    prd.add_argument("--track", type=float, default=None, help="default track width mm")
    prd.add_argument("--clearance", type=float, default=None, help="clearance mm")
    prd.add_argument("--constraints", default=None, help="constraints TOML (net classes, support ownership)")
    prd.add_argument("--spec", default=None, help="spec JSON (pin names)")
    prd.set_defaults(fn=cmd_route_diagnose)

    pac = sub.add_parser("anchor-check",
                         help="evaluate every allowed rotation of each connector/module/socket "
                              "and flag ANCHOR_MISORIENTED")
    pac.add_argument("board")
    pac.add_argument("--constraints", default=None)
    pac.add_argument("--spec", default=None)
    pac.add_argument("--apply", action="store_true", help="rotate misoriented anchors to their best option")
    pac.add_argument("--out", default=None)
    pac.set_defaults(fn=cmd_anchor_check)

    prr = sub.add_parser("route-ready",
                         help="pre-route gate: net budgets, support parts, anchors, pin escape, "
                              "keep-outs, congestion, ratsnest crossings; exit 1 on FAIL")
    prr.add_argument("board")
    prr.add_argument("--constraints", default=None)
    prr.add_argument("--spec", default=None)
    prr.add_argument("--json", default=None, help="write scorecard + findings as JSON")
    prr.add_argument("--all", action="store_true", help="list every finding")
    prr.add_argument("--waive", action="append", default=[], help="CODE:REGEX, repeatable")
    prr.add_argument("--strip-stale", action="store_true",
                     help="remove tracks/vias whose ends no longer land on a pad/track of their net")
    prr.add_argument("--out", default=None, help="with --strip-stale: output board (default: in place)")
    prr.add_argument("--track", type=float, default=None)
    prr.add_argument("--clearance", type=float, default=None)
    prr.add_argument("--collisions-only", action="store_true",
                     help="only the copper-collision gate (seconds): COLLISION findings by footprint pair")
    prr.add_argument("--relevance", default=None,
                     help="show only COLLISION findings with these tags (placement,routing,manufacturing,electrical); "
                          "display filter, the verdict counts all")
    prr.set_defaults(fn=cmd_route_ready)
    from fluxplace import plancli
    plancli.add_parsers(sub)              # map, plan-route, plan-compare
    from fluxplace import fancli
    fancli.add_parsers(sub)               # density, fanout, modules, floorplan (constructive flow)
    from fluxplace import partscli
    partscli.add_parsers(sub)             # parts, ready, pin-groups, part-diff
    from fluxplace import corpus as CP
    CP.add_parsers(sub)                   # corpus: multi-board regression scorecard

    pcl = sub.add_parser("clusters",
                         help="IC support clusters (which 2-pin parts belong to which IC); "
                              "--apply re-seats far support parts, ICs never move")
    pcl.add_argument("board")
    pcl.add_argument("--constraints", default=None)
    pcl.add_argument("--spec", default=None)
    pcl.add_argument("--apply", action="store_true")
    pcl.add_argument("--out", default=None)
    pcl.add_argument("--all", action="store_true", help="re-seat every support part, not only far ones")
    pcl.add_argument("--guess", action="store_true", help="also move parts whose owner is a guess")
    pcl.add_argument("--best-effort", action="store_true",
                     help="also accept a legal seat farther than the part's limit (default: leave such a part and report it)")
    pcl.set_defaults(fn=cmd_clusters)

    pfc = sub.add_parser("floorplan-check", help="area budget before placing: per block and side, courtyard "
                                                 "area + escape vs the free area of its region; REGION_OVERFULL")
    pfc.add_argument("board")
    pfc.add_argument("--constraints", default=None)
    pfc.add_argument("--spec", default=None)
    pfc.add_argument("--json", default=None)
    pfc.set_defaults(fn=cmd_floorplan_check)

    ptp = sub.add_parser("templates", help="place [placement.template.NAME] structure templates "
                                           "(kind = tree | chain) and report the net lengths achieved")
    ptp.add_argument("board")
    ptp.add_argument("--constraints", default=None)
    ptp.add_argument("--spec", default=None)
    ptp.add_argument("--out", default=None, help="write the board here (default: report only)")
    ptp.set_defaults(fn=cmd_templates)

    psk = sub.add_parser("silk", help="silkscreen check (off board, over pads/holes, overlaps, covered, too small, "
                                      "far, hidden refs); --fix re-fits the reference texts as a set")
    psk.add_argument("board")
    psk.add_argument("--constraints", default=None, help="constraints TOML; [silk] holds the limits")
    psk.add_argument("--fix", action="store_true", help="re-place reference texts, write --out, re-check")
    psk.add_argument("--out", default=None, help="with --fix: output board (default <board>.silk.kicad_pcb)")
    psk.add_argument("--in-place", action="store_true", help="with --fix: overwrite the input board")
    psk.add_argument("--side", choices=["F", "B", "both"], default="both")
    psk.add_argument("--spec", default=None, help="spec JSON (its version/revision feeds the title/version check)")
    psk.add_argument("--title", default=None, help="owned title text (literal); [silk.title] holds the template")
    psk.add_argument("--json", action="store_true", help="print machine-readable JSON instead of the compact report")
    psk.set_defaults(fn=cmd_silk)

    pmg = sub.add_parser("magnetics", help="LAN magnetics wiring check vs datasheet pins")
    pmg.add_argument("board")
    pmg.add_argument("--spec", default=None, help="spec JSON; parts declare `channels` + pinmap_source")
    pmg.add_argument("--constraints", default=None, help="constraints TOML; [magnetics] polarity/chip_ct/line_ct")
    pmg.add_argument("--json", action="store_true", help="print machine-readable JSON")
    pmg.set_defaults(fn=cmd_magnetics)

    prv = sub.add_parser("review",
                         help="design-review gate: spec net rules, diff-pair "
                              "skew, per-layer RF impedance, distributor "
                              "package/pin/temperature vs footprint, pinmap vs "
                              "the official symbol, spec/board sync, hold-up "
                              "and TVS margin")
    prv.add_argument("--board", required=True)
    _add_review_args(prv)
    prv.add_argument("--json", default=None, help="also write findings as JSON")
    prv.add_argument("--fail-on", choices=["never", "error", "warning"],
                     default="error", help="exit 1 at this severity (default error)")
    prv.set_defaults(fn=cmd_review)

    prp = sub.add_parser("repair",
                         help="copper repairs the review gate asks for: pair "
                              "loop removal, per-layer RF widths, pin remap "
                              "with stub rip, twin-pad nets, silkscreen text")
    prp.add_argument("--board", required=True)
    prp.add_argument("--out", default=None, help="default: overwrite --board")
    prp.add_argument("--fix-pads", action="store_true",
                     help="net the unnetted twin of a same-numbered pad")
    prp.add_argument("--pairs", nargs="*", default=None, metavar="PREFIX",
                     help="remove loops/stubs on 2-pin nets with these name "
                          "prefixes (default ETH_)")
    prp.add_argument("--remap", default=None,
                     help='JSON {"D7": {"1": "MIC_N", "2": "GND"}}: re-net pads, '
                          "rip old stubs, GND via beside pads that become GND")
    prp.add_argument("--no-rip", action="store_true")
    prp.add_argument("--rf", action="store_true",
                     help="re-width RF segments to the impedance their layer needs")
    prp.add_argument("--planes", nargs="*", default=None,
                     help="reference plane layers (default: detected)")
    prp.add_argument("--constraints", default=None, help="[rf] target/nets")
    prp.add_argument("--clear-under", action="append", default=[], metavar="REF",
                     help="rip other nets' copper colliding with REF's pads (after "
                          "a swap to a bigger land pattern), prune, leave for --patch")
    prp.add_argument("--stitch", action="append", default=[], metavar="REF:PAD",
                     help="join a pad to the nearest same-net copper on its "
                          "layer with one straight track (repeatable)")
    prp.add_argument("--stitch-max", type=float, default=3.0)
    prp.add_argument("--text", default=None, help="silkscreen text to add")
    prp.add_argument("--text-layer", default="F.SilkS")
    prp.add_argument("--text-size", type=float, default=1.0)
    prp.add_argument("--text-at", default=None, help="x,y mm (default: free spot)")
    prp.add_argument("--text-prefer", default="bottom-right",
                     choices=["bottom-right", "bottom-left", "top-left", "top-right"])
    prp.add_argument("--bridge", action="append", default=[], metavar="REF:PAD",
                     help="maze-route one unconnected pad to its net across layers "
                          "(DRC-guarded; for what --patch and finish cannot reach)")
    prp.add_argument("--bridge-layers", nargs="*", default=None,
                     help="copper layers the bridge may use (default: all)")
    prp.add_argument("--patch", action="store_true",
                     help="run the last-mile patcher afterwards for unrouted pads")
    prp.add_argument("--kicad-cli", default="kicad-cli")
    prp.add_argument("--track", type=float, default=0.2)
    prp.add_argument("--clearance", type=float, default=0.15)
    prp.add_argument("--via", type=float, default=0.45)
    prp.add_argument("--drill", type=float, default=0.25)
    prp.add_argument("--cell", type=float, default=0.25, help="patcher grid mm")
    prp.set_defaults(fn=cmd_repair)

    pfn = sub.add_parser("finish",
                         help="route a scope (nets, parts, area) with freerouting; copy back only "
                              "new in-scope copper, protection proven, DRC-guarded")
    pfn.add_argument("--board", required=True)
    pfn.add_argument("--out", default=None)
    pfn.add_argument("--nets", nargs="+", default=None,
                     help="net names or regexes (a token that is a net name is taken literally)")
    pfn.add_argument("--refs", default=None,
                     help="GLOB[,GLOB]: parts; the nets touching them, intersected with --nets")
    pfn.add_argument("--area", default=None,
                     help="X1,Y1,X2,Y2 mm: only connections with both pins inside; new copper stays inside")
    pfn.add_argument("--area-of", default=None, dest="area_of",
                     help="REF[,REF...] (globs): area = their courtyards' bounding box")
    pfn.add_argument("--margin", type=float, default=2.0, help="mm added around --area-of (default 2)")
    pfn.add_argument("--selection", default=None,
                     help='JSON {"nets":[..],"refs":[..],"area":[x1,y1,x2,y2]}; intersected with the rest')
    pfn.add_argument("--rip", action="store_true",
                     help="allow ripping in-scope nets' copper inside the area (nothing else is ever ripped)")
    pfn.add_argument("--planes", nargs="*", default=["In1.Cu", "In4.Cu"],
                     help="plane layers declared (type power) in the DSN")
    pfn.add_argument("--jar", default=None)
    pfn.add_argument("--passes", type=int, default=3)
    pfn.add_argument("--timeout", type=int, default=900,
                     help="freerouting wall-clock cap in seconds")
    pfn.add_argument("--kicad-cli", default="kicad-cli")
    pfn.set_defaults(fn=cmd_finish)

    pdf = sub.add_parser("drc-fix",
                         help="fix the DRC noise a repair leaves behind: rip "
                              "tracks shorting a swapped footprint, neck RF "
                              "segments at clearance pinches, snap/delete "
                              "stray track ends, move colliding reference text")
    pdf.add_argument("--board", required=True)
    pdf.add_argument("--out", default=None)
    pdf.add_argument("--rounds", type=int, default=3)
    pdf.add_argument("--island-vias", action="store_true",
                     help="DRC-guarded via into each outer-layer plane island "
                          "that has no via (kept only if DRC does not worsen)")
    pdf.add_argument("--acid-traps", action="store_true",
                     help="chamfer bare acute same-net track junctions (ACID_TRAP), "
                          "kept only if DRC violations and unconnected items do not rise")
    pdf.add_argument("--kicad-cli", default="kicad-cli")
    pdf.add_argument("--constraints", default=None,
                     help="[fab] min_track_mm floors the RF clearance-neck fallback")
    pdf.set_defaults(fn=cmd_drcfix)

    ptu = sub.add_parser("tune",
                         help="DRC-guarded diff-pair length tuning: hairpin "
                              "shortcuts and serpentine meanders until every "
                              "pair is within its [pairs] skew limit")
    ptu.add_argument("--board", required=True)
    ptu.add_argument("--out", default=None)
    ptu.add_argument("--prefix", nargs="*", default=None,
                     help="only pairs whose master net starts with these")
    ptu.add_argument("--constraints", default=None, help="[pairs.*] skew_mm limits")
    ptu.add_argument("--amplitude", type=float, default=0.6, help="meander amplitude mm")
    ptu.add_argument("--rounds", type=int, default=6)
    ptu.add_argument("--kicad-cli", default="kicad-cli")
    ptu.add_argument("--match", default=None, metavar="NAME|all",
                     help="tune [match.*] length groups instead of diff pairs")
    ptu.set_defaults(fn=cmd_tune)

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
    pmo.add_argument("--check", action="store_true",
                     help="list electrical footprints with a missing/broken "
                          "3D model (no network) and exit 1 if any")
    pmo.add_argument("--fetch", action="store_true",
                     help="pull missing 3D bodies from EasyEDA by LCSC code "
                          "(needs easyeda2kicad), attach with provenance, "
                          "exit 1 if anything stayed unresolved")
    pmo.add_argument("--lib-dir", default=None,
                     help="--fetch: where fetched STEP files land "
                          "(default: <board_dir>/../lib/3dmodels)")
    pmo.add_argument("--sources", default=None,
                     help="--fetch: provenance JSON to update (default: "
                          "model_sources.json beside mpn_map.json, else in "
                          "--lib-dir)")
    pmo.add_argument("--mpn-map", default=None,
                     help="--fetch: JSON file {ref: MPN} (default: "
                          "auto-found like other commands)")
    pmo.set_defaults(fn=cmd_models)

    pin = sub.add_parser("intake",
                         help="design interview: connectors (edge/remote + "
                              "panel defaults), mounting -> design_intent.json")
    pin.add_argument("--out", default="design_intent.json")
    pin.add_argument("--answers", default=None,
                     help="JSON with pre-filled answers (non-interactive)")
    pin.add_argument("--apply-board", default=None,
                     help="board to add corner mounting holes to now")
    pin.add_argument("--constraints-out", default=None,
                     help="write/append the [env] block to this constraints "
                          "TOML so the review gate derates against it")
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
    pst.add_argument("--board", default=None)
    pst.add_argument("--json", action="store_true")
    pst.add_argument("--list-templates", action="store_true",
                     help="list the named stackup templates (no board needed)")
    pst.add_argument("--detail", action="store_true",
                     help="with --list-templates: 50/90/100 ohm cases per template")
    pst.set_defaults(fn=cmd_stackup)

    psa = sub.add_parser("stackup-define",
                         help="define the board stackup from a fab profile and "
                              "solve the trace geometry it implies")
    psa.add_argument("--board", required=True)
    psa.add_argument("--profile", default=None, help="fab stackup profile name")
    psa.add_argument("--template", default=None,
                     help="named stackup template (same as --profile; see "
                          "`stackup --list-templates`)")
    psa.add_argument("--constraints", default=None,
                     help="constraints TOML; [fab] stackup_template is the default")
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
    psc.add_argument("--layout", choices=["blocks", "flat"], default="blocks",
                     help="blocks (default): one drafted sheet per functional block ([schematic] in the constraints); "
                          "flat: the old single net-label sheet")
    psc.add_argument("--title", default=None)
    psc.add_argument("--no-verify", action="store_true",
                     help="skip the netlist/ERC gate: verdict NOT_VERIFIED (block layout exits non-zero)")
    psc.add_argument("--allow-unverified", action="store_true",
                     help="with --no-verify, exit 0 anyway (the verdict stays NOT_VERIFIED)")
    psc.add_argument("--datasheets", default=None,
                     help="datasheet dir: refuse to generate from a spec whose "
                          "parts are not documented (spec-check)")
    psc.add_argument("--mpn-map", default=None)
    psc.add_argument("--constraints", default=None, help="constraints TOML ([parts] enforce stops on a part-entry FAIL)")
    psc.add_argument("--undocumented", action="store_true",
                     help="generate anyway (recorded in the output)")
    psc.add_argument("--kicad-cli", default="kicad-cli")
    psc.add_argument("--json", action="store_true")
    psc.add_argument("--render", default=None, help="blocks: render dir (default <out stem>-render/)")
    psc.add_argument("--no-render", action="store_true", help="blocks: skip PDF/PNG/SVG renders")
    psc.set_defaults(fn=cmd_schematic)

    prs = sub.add_parser("route-status",
                         help="one line from a router run's <out>.status.json (never tail the log)")
    prs.add_argument("target", help="board, router output, or .status.json")
    prs.set_defaults(fn=cmd_route_status)

    prw = sub.add_parser("route-wait",
                         help="block until the router is done/stalled/checkpointed; one summary "
                              "line + result/diagnosis path (one call instead of polling)")
    prw.add_argument("target", help="board, router output, or .status.json")
    prw.add_argument("--timeout", type=float, default=900, help="seconds to wait (default 900)")
    prw.add_argument("--until", choices=["done", "stall", "checkpoint"], default="done")
    prw.add_argument("--stall", type=float, default=None,
                     help="seconds without progress that count as a stall (default 600)")
    prw.add_argument("--poll", type=float, default=2.0)
    prw.add_argument("--no-diagnose", action="store_true", help="skip the read-only diagnosis on finish")
    prw.add_argument("--constraints", default=None)
    prw.add_argument("--spec", default=None)
    prw.set_defaults(fn=cmd_route_wait)

    from fluxplace import pipeline as PL
    ppl = sub.add_parser("pipeline",
                         help="staged pipeline, one stage per agent context, handing off via "
                              "WORKDIR/state.json (floorplan-check, anchor-check, place, route-ready, "
                              "route, tune, review)")
    ppl.add_argument("board", nargs="?", default=None, help="input board (first stage only)")
    ppl.add_argument("--workdir", required=True, help="state.json and W/<n>-<stage>/ outputs")
    ppl.add_argument("--constraints", default=None)
    ppl.add_argument("--spec", default=None)
    ppl.add_argument("--flow", choices=sorted(PL.FLOWS), default=None,
                     help="constructive = modules, floorplan, fanout (gate FANOUT_OK), plan-route, route, tune, silk, review")
    ppl.add_argument("--engine", choices=["builtin", "cypress"], default=None,
                     help="placement engine of the place / floorplan stage (default [placement] engine; docs/CYPRESS.md)")
    ppl.add_argument("--size", action="store_true", help="constructive flow: floorplan sizing loop (grow the outline until it passes)")
    ppl.add_argument("--from", dest="from_stage", choices=PL.ALL_STAGES, default=None)
    ppl.add_argument("--to", dest="to_stage", choices=PL.ALL_STAGES, default=None)
    ppl.add_argument("--status", action="store_true", help="print the stage table (<= 15 lines) and exit")
    ppl.add_argument("--waive", action="append", default=[], help="route-ready waiver CODE:REGEX")
    ppl.add_argument("--force", action="store_true", help="continue past a failing route-ready gate")
    ppl.add_argument("--api", action="store_true", help="review and the ready stage with live DigiKey/Mouser data")
    ppl.add_argument("--allow-incomplete", dest="allow_incomplete", action="store_true",
                     help="continue past a failing ready stage; every later output is stamped NOT READY, fab/deliver refuse")
    ppl.add_argument("--route-budget", type=float, default=1800, help="route stage wall-clock cap, s")
    ppl.add_argument("--route-timeout", type=float, default=900, help="per router call cap, s")
    ppl.add_argument("--stall-rounds", type=int, default=2)
    ppl.add_argument("--replace-on-stall", action="store_true", help="let the stall loop move parts")
    ppl.add_argument("--layers", nargs="+", default=["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"])
    ppl.add_argument("--track", type=float, default=0.2)
    ppl.add_argument("--clearance", type=float, default=0.2)
    ppl.add_argument("--router", choices=["krt", "freerouting", "auto"], default=None,
                     help="route stage: krt = KiCadRoutingTools, freerouting = whole-board Freerouting then krt on what is left, "
                          "auto = freerouting when a 2.5.0+ jar and Java are found (default [route] router, else krt)")
    ppl.add_argument("--freerouting-jar", default=None, help="Freerouting jar (else $FLUXPLACE_FREEROUTING_JAR, else newest ~/tools/freerouting*.jar)")
    ppl.add_argument("--java", default=None, help="Java runtime for Freerouting (else $FLUXPLACE_JAVA, $JAVA_HOME, ~/tools/jdk*, PATH)")
    ppl.add_argument("--freerouting-budget", type=float, default=None, help="Freerouting job timeout, s (default [route.freerouting] budget_s, else budget_frac of --route-budget)")
    ppl.add_argument("--freerouting-threads", type=int, default=None, help="Freerouting threads (default [route.freerouting] threads)")
    ppl.add_argument("--router-py", default=None, help="router python (else $FLUXPLACE_ROUTER_PY, else ~/tools/router-venv)")
    ppl.add_argument("--router-dir", default=None, help="router dir (else $FLUXPLACE_ROUTER_DIR, else newest ~/tools/KiCadRoutingTools*)")
    from fluxplace import questions as QS
    QS.add_args(ppl, json_flag=False)
    ppl.set_defaults(fn=cmd_pipeline)

    phb = sub.add_parser("help-brief",
                         help="at most 15 lines on one command (none: one line per command); "
                              "use instead of README/-h")
    phb.add_argument("command", nargs="?", default=None)
    phb.set_defaults(fn=cmd_help_brief)

    sub.choices["doctor"].add_argument(
        "--context", action="store_true",
        help="measure the agent-context footprint (MCP tools/list, AGENTS.md, README, command output) against budgets")

    from fluxplace import outputs as OUT
    OUT.add_flags(ap, sub)
    return ap


_SILK_HOOK_CMDS = ("place", "clusters", "templates", "anchor-check", "fit-sheet", "repair", "auto", "refine",
                   "route", "stall")


def _silk_hook(a):
    """Register the silkscreen summary/auto-fit on every board write that moves footprints (boardwrite.move_file,
    flip_file, stall rounds). `[silk] enabled = false` in the constraints turns it off."""
    from fluxplace import boardwrite as BW
    BW.set_post_write(None)
    if a.cmd not in _SILK_HOOK_CMDS:
        return
    try:
        from fluxplace import constraints as C
        cons = C.load(getattr(a, "constraints", None))
    except Exception:                                    # noqa: BLE001
        cons = {}
    from fluxplace import silk as SK
    BW.set_post_write(lambda path, refs: SK.after_write(path, refs, cons))


# commands that never write a board: no connectivity snapshot is taken for them
_GUARD_READ_ONLY = {"lint", "review", "route-status", "route-wait", "route-diagnose", "doctor", "help-brief",
                    "floorplan-check", "rules", "analyze", "comprehend"}


def _guard_ledger_on(a):
    """`[guard] ledger = false` in the constraints turns the *.ledger.json sidecar off (default on)."""
    try:
        from fluxplace import constraints as C
        return bool((C.load(getattr(a, "constraints", None)).get("guard") or {}).get("ledger", True))
    except Exception:                                    # noqa: BLE001 - an unreadable constraints file: default on
        return True


def _guard_begin(a):
    """Connectivity invariant (audit.snapshot): every command that writes a .kicad_pcb from an input board is
    checked pad -> net and footprint identity before / after. Off with FLUXPLACE_GUARD=0. Read-only commands
    (_GUARD_READ_ONLY) take no snapshot. An input the model cannot snapshot is not silently unguarded: it prints
    `guard: UNCHECKED - <reason>`, and a run that then writes a board fails."""
    import os as _os
    writes_board = isinstance(getattr(a, "out", None), str) and a.out.endswith(".kicad_pcb")
    if _os.environ.get("FLUXPLACE_GUARD", "1") == "0" or a.cmd == "pipeline" or \
            (a.cmd in _GUARD_READ_ONLY and not writes_board):
        return None
    src = getattr(a, "board", None)
    if not (isinstance(src, str) and src.endswith(".kicad_pcb") and _os.path.isfile(src)):
        return None
    out = getattr(a, "out", None)
    out = out if isinstance(out, str) and out.endswith(".kicad_pcb") else src
    st = lambda p: (_os.stat(p).st_mtime_ns, _os.stat(p).st_size) if _os.path.exists(p) else None
    from fluxplace import audit as AU
    pro = _os.path.splitext(src)[0] + ".kicad_pro"       # the rules of record: the source's project, for both snapshots
    base = {"src": src, "out": out, "stat": st(out), "st": st, "pro": pro,
            "orig": open(src, "rb").read() if _os.path.abspath(out) == _os.path.abspath(src) else None}
    try:
        base["snap"] = AU.snapshot(src, project=pro)
    except Exception as e:                               # noqa: BLE001 - recorded, not silently dropped
        base["unchecked"] = f"input board {src} cannot be snapshotted: {type(e).__name__}: {e}"
        _gp(f"guard: UNCHECKED - {base['unchecked']}")
    return base


def _gp(*x):
    """guard lines go to stderr: stdout may be a command's machine output (--json)."""
    import sys as _sys
    print(*x, file=_sys.stderr)


def _write_ledger(g, rec):
    """The *.ledger.json sidecar next to the output (`[guard] ledger = false` turns it off)."""
    import os as _os, json as _json
    lp = _os.path.splitext(g["out"])[0] + ".ledger.json"
    try:
        with open(lp, "w") as fh:
            _json.dump(rec, fh, indent=1)
        return lp
    except OSError:
        return None


def _guard_end(g, a, raised=False):
    """-> True when the written board passes the invariant (or nothing was written). `raised`: the command raised
    after it started; a board it wrote IN PLACE is restored to the input (a partial write is never kept)."""
    import os as _os, shutil as _sh
    if not g or not _os.path.exists(g["out"]) or g["st"](g["out"]) == g["stat"]:
        return True
    if raised:
        if g["orig"] is not None:
            with open(g["out"], "wb") as fh:
                fh.write(g["orig"])
            _gp(f"guard: {a.cmd} raised after writing {g['out']} in place - input board restored")
        else:
            _gp(f"guard: {a.cmd} raised; output {g['out']} may be partial")
        return False
    if g.get("unchecked"):                               # a board was written but its input was never snapshotted
        _gp(f"guard: UNCHECKED - {g['unchecked']}; output not verified")
        _write_ledger(g, {"command": a.cmd, "unchecked": g["unchecked"], "ok": False}) if _guard_ledger_on(a) else None
        return False
    from fluxplace import audit as AU
    try:
        after = AU.snapshot(g["out"], project=g.get("pro"))
    except Exception as e:                               # noqa: BLE001
        reason = f"output board {g['out']} cannot be snapshotted: {type(e).__name__}: {e}"
        _gp(f"guard: UNCHECKED - {reason}")
        _write_ledger(g, {"command": a.cmd, "unchecked": reason, "ok": False}) if _guard_ledger_on(a) else None
        return False
    d = AU.connectivity_diff(g["snap"], after, AU.EXPECT.get(a.cmd, ()))
    rec, line = AU.ledger(a.cmd, g["snap"], after, d)
    lp = _write_ledger(g, rec) if _guard_ledger_on(a) else None
    _gp("guard: " + line + (f"  ledger {lp}" if lp else ""))
    if d["ok"]:
        return True
    for v in d["violations"][:40]:
        _gp("  guard: " + v)
    bad = _os.path.splitext(g["out"])[0] + ".rejected.kicad_pcb"
    _sh.copy(g["out"], bad)
    if g["orig"] is not None:                            # written in place: put the input back
        with open(g["out"], "wb") as fh:
            fh.write(g["orig"])
    else:
        _os.remove(g["out"])
    _gp(f"guard: unintended connectivity change - result moved to {bad}"
          + ("; input restored" if g["orig"] is not None else "; no output written"))
    return False


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    from fluxplace import outputs as OUT
    if a.cmd == "doctor" and getattr(a, "context", False):
        from fluxplace import context_check as CC
        text, ok = CC.report()
        print(text)
        raise SystemExit(0 if ok else 1)
    _silk_hook(a)
    guard = _guard_begin(a)
    try:
        if a.cmd in OUT.COMPACT_CMDS:
            rc = OUT.run_compact(a.cmd, a, a.fn)    # compact-by-default wrapper (one place)
        else:
            rc = a.fn(a)
    except SystemExit as e:
        if not _guard_end(guard, a) and not e.code:
            raise SystemExit(1)
        raise
    except BaseException:
        # the command raised: the guard still runs (an in-place board is restored); the ORIGINAL error is
        # re-raised whatever the guard does
        try:
            _guard_end(guard, a, raised=True)
        except Exception as ge:                          # noqa: BLE001
            _gp(f"guard: end-check failed: {type(ge).__name__}: {ge}")
        raise
    if not _guard_end(guard, a):
        raise SystemExit(1)
    if a.cmd in _HINT_CMDS and not getattr(a, "dry_run", False):
        _sheet_hint(getattr(a, "out", None) or a.board)
    if type(rc) is int and rc:                  # a command that RETURNS its verdict code (fanout, pairs, snap-vias ...)
        raise SystemExit(rc)                    # exits with it; None / data return values stay exit 0


if __name__ == "__main__":
    main()
