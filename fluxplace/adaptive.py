"""Adaptive escape routing driver — route-fresh-per-rung + fanout-aware.

The universal "finish the board itself" loop, any board:

  1. Route ALL signal nets FRESH at the conservative rule (rip any prior signal copper,
     keep poured GND/power planes). Route-fresh — not patch-through-congestion — is what
     keeps a real router from thrashing (measured: incremental leftover routing stalls).
  2. DRC -> which nets are still open.
  3. Step the stalled SIGNAL nets down the fine-pitch ladder and re-route FRESH, giving
     just those nets a finer PER-NET clearance (KRT --net-clearances). Power/current rails
     keep their ampacity width and are never necked. Accumulate across rungs.
  4. At the floor, any net that STILL won't close is geometry (e.g. a 2-row mezzanine
     inner row): generate via-in-pad/dogbone FANOUT for that part (KRT bga_fanout) and
     route fresh again. Nets that then close were fanout-limited, not rule-limited.
  5. Stop when clean, or when a zone can't be closed even with fanout — reported by
     part, never handed back as "route it yourself".

Router-agnostic: `route_fresh_fn(placed, out, fine_clearances)->board` and
`fanout_fn(board, out, component)->board` are the two pluggable slots; KRT wirings below.
DRC (kicad-cli) is the ground truth each round.
"""
import json
import os
import re
import subprocess

from . import escape as E


def drc_unrouted(board, kicad_cli="kicad-cli"):
    """Run kicad-cli DRC; return (report, set_of_unrouted_netnames)."""
    out = board + ".drc.json"
    subprocess.run([kicad_cli, "pcb", "drc", "--format", "json", "--severity-error",
                    "--output", out, board], capture_output=True, text=True)
    d = json.load(open(out))
    nets = set()
    for u in d.get("unconnected_items", []):
        for it in u.get("items", []):
            m = re.search(r"\[([^\]]+)\]", it.get("description", ""))
            if m and m.group(1):
                nets.add(m.group(1))
    return d, nets


def classify(graph, drc):
    """{thin: signal nets that may size down, keep: power/current rails}."""
    return E.classify_stalled_nets(graph, drc)


def krt_route_fresh(krt_py, krt_dir, layers, base_w=0.2, base_c=0.2, via_size=0.6,
                    via_drill=0.3, grid=0.1, power_nets=None, power_widths=None, timeout=1200):
    """route_fresh_fn backed by KiCadRoutingTools: rip prior signal copper, keep planes,
    route ALL nets at (base_w, base_c); `fine` = {net: clearance} necks only those nets."""
    route_py = os.path.join(krt_dir, "py_router", "route.py")

    def fn(placed, outb, fine, log=print):
        cmd = [krt_py, route_py, placed, outb, "--keep-input-copper",
               "--rip-existing-nets", "*", "--layers", *layers,
               "--track-width", str(base_w), "--clearance", str(base_c),
               "--via-size", str(via_size), "--via-drill", str(via_drill),
               "--grid-step", str(grid)]
        if fine:
            fpath = outb + ".netclr.json"          # KRT --net-clearances takes a FILE path
            json.dump(fine, open(fpath, "w"))
            cmd += ["--net-clearances", fpath]
        if power_nets and power_widths:
            cmd += ["--power-nets", *power_nets, "--power-nets-widths", *map(str, power_widths)]
        fn.timed_out = False       # per-call: did THIS rung get truncated?
        try:
            subprocess.run(cmd, cwd=krt_dir, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            fn.timed_out = fn.any_timeout = True
            log(f"    route-fresh hit {timeout}s cap")
        return outb if os.path.exists(outb) else placed
    fn.timed_out = fn.any_timeout = False
    return fn


def pick_best(rows):
    """Population-search selection: rows = [(name, unrouted, violations)] ->
    index of the winner. Fewest unrouted nets first, DRC violations break ties,
    earliest candidate breaks the rest (candidate 0 = the un-jittered base, so
    ties prefer the least-perturbed placement)."""
    return min(range(len(rows)), key=lambda i: (rows[i][1], rows[i][2], i))


def krt_route_diff(krt_py, krt_dir, layers, pairs, track_w=0.2, clearance=0.2,
                   gap=0.15, via_size=0.45, via_drill=0.25, timeout=900):
    """Pairs-first pre-route via KRT route_diff.py: P and N routed TOGETHER at a
    fixed gap, so coupling is by construction, not luck (the 'uncoupled spacing'
    physics failure is a single-ended router treating the pair as two unrelated
    nets). route.py treats routed diff pairs as protected copper, so the bulk
    route-fresh afterwards builds around them. Returns fn(board, outb) -> board;
    on failure the input board passes through untouched."""
    rd_py = os.path.join(krt_dir, "py_router", "route_diff.py")
    netnames = sorted(set(pairs) | set(pairs.values()))

    def fn(board, outb, log=print):
        if not netnames:
            return board
        cmd = [krt_py, rd_py, board, "--output", outb,
               "--nets", *netnames, "--layers", *layers,
               "--track-width", str(track_w), "--clearance", str(clearance),
               "--diff-pair-gap", str(gap), "--via-size", str(via_size),
               "--via-drill", str(via_drill), "--keep-input-copper"]
        try:
            subprocess.run(cmd, cwd=krt_dir, capture_output=True, text=True,
                           timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"    diff-pair pre-route hit {timeout}s cap")
        return outb if os.path.exists(outb) else board
    return fn


def krt_length_match(krt_py, krt_dir, layers, pairs, base_w=0.2, base_c=0.2,
                     tolerance=0.5, via_size=0.6, via_drill=0.3, grid=0.1,
                     timeout=1200):
    """Skew repair: force-reroute the (still-unlocked) diff pairs as LENGTH-MATCHED
    groups with meanders. KRT's --force-reroute restores the original copper if a
    net fails, and never touches KiCad-locked copper — so coupled pairs from the
    pairs-first stage keep their geometry and only the bulk-routed stragglers get
    matched. This is the stage that turns millimetre skews into the 0.1mm class."""
    route_py = os.path.join(krt_dir, "py_router", "route.py")
    netnames = sorted(set(pairs) | set(pairs.values()))

    def fn(board, outb, log=print):
        if not netnames:
            return board
        cmd = [krt_py, route_py, board, outb, "--keep-input-copper",
               "--force-reroute", "--nets", *netnames,
               "--layers", *layers, "--track-width", str(base_w),
               "--clearance", str(base_c), "--via-size", str(via_size),
               "--via-drill", str(via_drill), "--grid-step", str(grid),
               "--length-match-tolerance", str(tolerance)]
        for s, m in sorted(pairs.items()):
            cmd += ["--length-match-group", m, s]
        try:
            subprocess.run(cmd, cwd=krt_dir, capture_output=True, text=True,
                           timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"    length-match hit {timeout}s cap")
        return outb if os.path.exists(outb) else board
    return fn


def freerouting_finish(jar, passes=6, timeout=1800):
    """Last-mile finisher: freerouting is ~two orders slower than KRT but
    completion-strong — on a board that is already 97%+ routed it only has the
    hard residue to negotiate. DSN out -> jar -> SES back in via pcbnew.
    Passthrough (returns the input path) on any failure; caller keep-bests."""
    def fn(src, outb, log=print):
        import pcbnew
        from . import kicad_io as IO
        b = pcbnew.LoadBoard(src)
        dsn, ses = outb + ".dsn", outb + ".ses"
        if not IO.export_dsn(b, dsn):
            log("    finisher: DSN export failed — skipped")
            return src
        # HEADLESS, always: without this the jar opens a Swing window through
        # WSLg on the user's desktop (measured). -Xss256m: freerouting's DSN
        # parser recurses per polygon vertex — big GND pours overflow the
        # default JVM stack (java.lang.StackOverflowError on load, measured).
        env = {k: v for k, v in os.environ.items()
               if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        try:
            r = subprocess.run(["java", "-Djava.awt.headless=true", "-Xss256m",
                                "-jar", jar, "-de", dsn, "-do", ses,
                                "-mp", str(passes)], capture_output=True,
                               timeout=timeout, env=env, text=True)
            if r.returncode != 0:
                log("    finisher: jar exited "
                    f"{r.returncode}: {(r.stderr or r.stdout or '').strip()[:160]}")
        except subprocess.TimeoutExpired:
            log(f"    finisher hit {timeout}s cap")
        except FileNotFoundError:
            log("    finisher: java not available — skipped")
            return src
        if not os.path.exists(ses):
            log("    finisher: no session produced — skipped")
            return src
        try:
            if not pcbnew.ImportSpecctraSES(b, ses):
                log("    finisher: SES import rejected — skipped")
                return src
        except Exception as e:
            log(f"    finisher: SES import failed ({e}) — skipped")
            return src
        pcbnew.SaveBoard(outb, b)
        return outb
    return fn


def krt_fanout(krt_py, krt_dir, layers, track_w=0.1, clearance=0.1, via_size=0.45,
               via_drill=0.25, method="auto", timeout=600):
    """fanout_fn backed by KRT bga_fanout: generate escape vias (dogbone/underpad) for a
    fine-pitch component so its pins reach an inner layer where they can route."""
    fo_py = os.path.join(krt_dir, "py_router", "bga_fanout.py")

    def fn(board, outb, component, nets=None, log=print):
        cmd = [krt_py, fo_py, board, "--output", outb, "--component", component,
               "--layers", *layers, "--track-width", str(track_w),
               "--clearance", str(clearance), "--via-size", str(via_size),
               "--via-drill", str(via_drill), "--escape-method", method]
        if nets:
            # fan out ONLY the stuck nets: blanket fanout of every pin plants a via
            # picket fence the next route-fresh must dodge (measured on CM5: fanout
            # made unrouted WORSE and keep-best had to throw the round away)
            cmd += ["--nets", *nets]
        try:
            subprocess.run(cmd, cwd=krt_dir, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"    fanout hit {timeout}s cap on {component}")
        return outb if os.path.exists(outb) else board
    return fn


def route_adaptive(placed, out, route_fresh, graph, parts, kicad_cli="kicad-cli",
                   start_mm=0.20, floor_mm=0.10, fanout=None, min_unrouted=5, log=print):
    """The universal finisher. `route_fresh(src, out, fine)->board`, optional
    `fanout(board, out, ref)->board`. Returns (board, summary). Route-fresh every rung
    (from the current `placed`, which fanout may augment), accumulating stuck signal nets
    into finer per-net clearance; fanout the geometric residue at the floor."""
    routed = os.path.join(out, "routed.kicad_pcb") if os.path.isdir(out) else out
    fine = {}
    width = start_mm
    rounds = []
    fanned_refs = []

    cur = route_fresh(placed, routed, fine, log=log)
    d, unrouted = drc_unrouted(cur, kicad_cli); unrouted.discard("GND")
    rounds.append({"width": width, "unrouted": len(unrouted)})
    log(f"[{width}mm] unrouted: {len(unrouted)}")
    import shutil
    best_board = cur + ".best"; shutil.copy(cur, best_board); best_n = len(unrouted)

    force_fanout = False       # a worsened step-down skips the rest of the ladder
    while unrouted:
        cls = classify(graph, d)
        nxt = E.ladder_step(width)
        # FANOUT-PRIORITY: when the residue is CONCENTRATED at a few fine-pitch
        # parts (>=60% of stuck endpoints at the top-3 zones), escape vias are the
        # mechanism that fits the failure — give fanout the router time before
        # walking the clearance ladder (measured on dig: two ladder rungs bought
        # 69->69->72 = nothing; the fanout rung bought 69->33). The ladder still
        # runs for spread residue and for whatever fanout leaves behind.
        zones = ([z for z in E.detect_escape_zones(parts, d, min_unrouted=min_unrouted)
                  if z["ref"] not in fanned_refs] if fanout is not None else [])
        all_ep = sum(z["n"] for z in E.detect_escape_zones(parts, d, min_unrouted=1))
        zone_ep = sum(z["n"] for z in zones[:3])
        concentrated = all_ep > 0 and zone_ep >= 0.6 * all_ep
        if zones and (concentrated or force_fanout or nxt is None or not cls["thin"]):
            last = "fanout"
            for z in zones:
                stuck = sorted(unrouted & set(parts.get(z["ref"], {}).get("pins", {})))
                log(f"fanout: escape vias for {z['ref']} ({z['n']} stuck pads, "
                    f"{len(stuck)} stuck nets targeted)")
                placed = fanout(placed, os.path.join(out, f"fan_{z['ref']}.kicad_pcb")
                                if os.path.isdir(out) else placed + f".fan_{z['ref']}",
                                z["ref"], nets=stuck or None, log=log)
                fanned_refs.append(z["ref"])
            cur = route_fresh(placed, routed, fine, log=log)
        elif not force_fanout and nxt is not None and cls["thin"]:
            last = "step"
            width = nxt
            for n in cls["thin"]:
                fine[n] = width                      # this net may neck (signal only)
            log(f"[{width}mm] step {len(cls['thin'])} stalled signal nets down (net-aware)")
            cur = route_fresh(placed, routed, fine, log=log)
        elif fanout is not None and not zones:
            log(f"floor + no new fanout targets: {len(unrouted)} nets remain")
            break
        else:
            log(f"floor reached (no fanout): {len(unrouted)} nets remain — need via-in-pad")
            break
        d, unrouted = drc_unrouted(cur, kicad_cli); unrouted.discard("GND")
        rounds.append({"width": width, "unrouted": len(unrouted),
                       "fanned": list(fanned_refs)})
        log(f"[{width}mm] unrouted: {len(unrouted)}"
            + (f"  (fanned {fanned_refs})" if fanned_refs else ""))
        # KEEP THE BEST: never accept a round (step-down OR fanout) that made it worse.
        if len(unrouted) < best_n:
            shutil.copy(cur, best_board); best_n = len(unrouted)
        elif len(unrouted) > best_n:
            if last == "step" and fanout is not None:
                # a worsened step-down must not silence FANOUT — the residue that
                # stalls the ladder is usually escape-shaped (dig: J20+U10 held 76%
                # of stuck endpoints and fanout never got its turn). Revert to the
                # best board's state and give fanout one shot at its zones.
                log(f"step-down worsened ({best_n}->{len(unrouted)}) — "
                    f"reverting to best, trying fanout")
                force_fanout = True
                shutil.copy(best_board, cur)
                d, unrouted = drc_unrouted(cur, kicad_cli); unrouted.discard("GND")
                continue
            log(f"round worsened ({best_n}->{len(unrouted)}) — reverting to best, stop")
            break

    shutil.copy(best_board, cur); best_d, best_un = drc_unrouted(cur, kicad_cli)
    best_un.discard("GND")
    zones_left = E.detect_escape_zones(parts, best_d, min_unrouted=1) if best_un else []
    zl = [z["ref"] for z in zones_left]
    # DIAGNOSIS — tell the user WHY, and what to do:
    base_n = rounds[0]["unrouted"]
    improved = best_n < base_n
    timed = getattr(route_fresh, "any_timeout", False)
    total_ep = sum(z["n"] for z in zones_left)
    conc = (sum(z["n"] for z in zones_left[:3]) / total_ep) if total_ep else 0.0
    if not best_un:
        diag = "CLOSED 100%"
    elif timed and not improved:
        # a truncated rung proves nothing about clearance vs capacity (measured on
        # dig: every rung hit the cap and the verdict flipped with more time)
        diag = (f"TIME-CAPPED: {len(best_un)} nets remain but the router hit its time "
                f"cap on a rung — raise --route-timeout before trusting a verdict"
                + (f"; unrouted concentrate at {zl[:4]}" if conc >= 0.6 else ""))
    elif not improved and not fanned_refs and conc < 0.6:
        # tightening clearance didn't help and the residue is SPREAD -> the
        # constraint is routing CAPACITY, not fine-pitch escape.
        diag = (f"LAYER-LIMITED: {len(best_un)} nets remain and the step-down did not help "
                f"(clearance isn't the bottleneck) — this board needs more SIGNAL LAYERS")
    else:
        # improved, or the residue clusters at a few fine-pitch parts (>=60% of
        # stuck endpoints at the top 3) — that's an escape problem wherever the
        # step-down stalled
        diag = (f"ESCAPE-LIMITED: {len(best_un)} nets at fine-pitch parts {zl[:8]} — "
                f"generate via-in-pad fanout there (step-down closed the rest)")
    log("DIAGNOSIS: " + diag)
    return cur, {"final_unrouted": len(best_un), "rounds": rounds,
                 "closed": not best_un, "fanned": fanned_refs, "diagnosis": diag,
                 "zones_left": zl, "board": cur}
