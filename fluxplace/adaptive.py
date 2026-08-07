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
        try:
            subprocess.run(cmd, cwd=krt_dir, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"    route-fresh hit {timeout}s cap")
        return outb if os.path.exists(outb) else placed
    return fn


def krt_fanout(krt_py, krt_dir, layers, track_w=0.1, clearance=0.1, via_size=0.45,
               via_drill=0.25, method="auto", timeout=600):
    """fanout_fn backed by KRT bga_fanout: generate escape vias (dogbone/underpad) for a
    fine-pitch component so its pins reach an inner layer where they can route."""
    fo_py = os.path.join(krt_dir, "py_router", "bga_fanout.py")

    def fn(board, outb, component, log=print):
        cmd = [krt_py, fo_py, board, "--output", outb, "--component", component,
               "--layers", *layers, "--track-width", str(track_w),
               "--clearance", str(clearance), "--via-size", str(via_size),
               "--via-drill", str(via_drill), "--escape-method", method]
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

    while unrouted:
        cls = classify(graph, d)
        nxt = E.ladder_step(width)
        if nxt is not None and cls["thin"]:
            width = nxt
            for n in cls["thin"]:
                fine[n] = width                      # this net may neck (signal only)
            log(f"[{width}mm] step {len(cls['thin'])} stalled signal nets down (net-aware)")
            cur = route_fresh(placed, routed, fine, log=log)
        elif fanout is not None:
            zones = [z for z in E.detect_escape_zones(parts, d, min_unrouted=min_unrouted)
                     if z["ref"] not in fanned_refs]
            if not zones:
                log(f"floor + no new fanout targets: {len(unrouted)} nets remain")
                break
            for z in zones:
                log(f"fanout: generating escape vias for {z['ref']} ({z['n']} stuck pads)")
                placed = fanout(placed, os.path.join(out, f"fan_{z['ref']}.kicad_pcb")
                                if os.path.isdir(out) else placed + f".fan_{z['ref']}",
                                z["ref"], log=log)
                fanned_refs.append(z["ref"])
            cur = route_fresh(placed, routed, fine, log=log)
        else:
            log(f"floor reached (no fanout): {len(unrouted)} nets remain — need via-in-pad")
            break
        d, unrouted = drc_unrouted(cur, kicad_cli); unrouted.discard("GND")
        rounds.append({"width": width, "unrouted": len(unrouted),
                       "fanned": list(fanned_refs)})
        log(f"[{width}mm] unrouted: {len(unrouted)}"
            + (f"  (fanned {fanned_refs})" if fanned_refs else ""))

    zones_left = E.detect_escape_zones(parts, d, min_unrouted=1) if unrouted else []
    return cur, {"final_unrouted": len(unrouted), "rounds": rounds,
                 "closed": len(unrouted) == 0, "fanned": fanned_refs,
                 "zones_left": [z["ref"] for z in zones_left], "board": cur}
