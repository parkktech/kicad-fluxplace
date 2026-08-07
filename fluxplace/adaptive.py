"""Adaptive escape routing driver — the route-and-anneal loop.

The heart of "shoot a board in, get a routed board out." Routes the whole board at the
CONSERVATIVE rule (0.2 mm), then for the nets that won't close, steps ONLY those down the
fine-pitch ladder and re-routes — signal nets thin toward the fab floor, power/current
rails keep their width and are left for the escape halo / a bigger board to give room.
Repeats until the board closes or the floor (0.10 mm) is reached.

Router-agnostic: caller passes a `route_fn(in_board, out_board, nets, width, clearance)`
that routes `nets` keeping existing copper and returns the written board. `krt_route_fn`
wires KiCadRoutingTools (fast Rust A*, honors --clearance/--track-width/--grid-step and
per-net power widths); freerouting could be dropped in the same slot.

DRC (kicad-cli) is the ground truth for what's still unrouted each round.

MEASURED (RAZOR-01 dig, 300 parts, 6-layer):
  * 0.20mm bulk -> 89% routed (102 unrouted);  0.10mm -> 97% (28 unrouted).
    The residual 28 sit on U10 (LQFP-144) + J20 (2-row mezzanine) and DON'T close even
    at 0.10mm -> the last mile is escape-aware PLACEMENT (fanout halo/room) or via-in-pad
    fanout, NOT a finer rule. So the driver's step-down is the middle 8%, not the last 3%.
  * PERFORMANCE: KRT re-routing leftover nets THROUGH existing congested copper thrashes
    (A* fights the fill). Prefer route-fresh per rung — strip signals, keep GND planes,
    route ALL signal nets at the rung's width with the still-stuck ones on finer per-net
    clearance (--net-clearances) — which is how the fast 97% board was produced. A
    `route_fn` may implement either; the step-down/classify logic here is unchanged.
"""
import json
import os
import re
import subprocess

from . import escape as E


def drc_unrouted(board, kicad_cli="kicad-cli"):
    """Run kicad-cli DRC; return (parsed_report, set_of_unrouted_netnames)."""
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


def krt_route_fn(krt_py, krt_dir, layers, via_size=0.6, via_drill=0.3, grid=0.1,
                 power_nets=None, power_widths=None, timeout=1200):
    """Build a route_fn backed by KiCadRoutingTools. Power rails are passed through with
    their own widths so the step-down never necks them."""
    route_py = os.path.join(krt_dir, "py_router", "route.py")

    def fn(inb, outb, nets, width, clearance, log=print):
        cmd = [krt_py, route_py, inb, outb, "--keep-input-copper",
               "--layers", *layers, "--track-width", str(width),
               "--clearance", str(clearance), "--via-size", str(via_size),
               "--via-drill", str(via_drill), "--grid-step", str(grid),
               "--nets", *sorted(nets)]
        if power_nets and power_widths:
            cmd += ["--power-nets", *power_nets, "--power-nets-widths", *map(str, power_widths)]
        try:
            r = subprocess.run(cmd, cwd=krt_dir, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"    router hit {timeout}s cap on {len(nets)} nets — keeping prior board")
            return inb
        if not os.path.exists(outb):
            log(f"    ! router produced no board: {(r.stderr or r.stdout)[-200:]}")
            return inb
        return outb
    return fn


def route_adaptive(board, out, route_fn, graph, kicad_cli="kicad-cli",
                   start_mm=0.20, floor_mm=0.10, log=print):
    """Route-and-anneal. Returns (final_board, summary). `route_fn` closes nets keeping
    existing copper. Each round: DRC -> classify stalled nets -> step the SIGNAL ones
    down one ladder rung and re-route; stop when clean, or when only rails/geometry
    remain at the floor. The bulk board never leaves its conservative rule."""
    cur = board
    width = start_mm
    rounds = []
    # round 0: close everything we can at the conservative rule
    d, unrouted = drc_unrouted(cur, kicad_cli)
    unrouted.discard("GND")
    thin0 = classify(graph, d)["thin"]
    if thin0:
        cur = route_fn(cur, out, thin0, width, width, log=log)
        d, unrouted = drc_unrouted(cur, kicad_cli)
        unrouted.discard("GND")
    rounds.append({"width": width, "unrouted": len(unrouted)})
    log(f"[{width}mm] unrouted: {len(unrouted)}")

    while unrouted:
        cls = classify(graph, d)
        nxt = E.ladder_step(width)
        if nxt is None or not cls["thin"]:
            log(f"floor/ceiling: {len(unrouted)} nets remain "
                f"({len(cls['keep'])} are rails — need room, not thinning)")
            break
        width = nxt
        log(f"[{width}mm] step {len(cls['thin'])} stalled signal nets down")
        cur = route_fn(cur, out, cls["thin"], width, width, log=log)
        d, unrouted = drc_unrouted(cur, kicad_cli)
        unrouted.discard("GND")
        rounds.append({"width": width, "unrouted": len(unrouted)})
        log(f"[{width}mm] unrouted: {len(unrouted)}")

    # encode the local fine-pitch as a .kicad_dru so the fine tracks are DRC-legal
    parts = None
    summary = {"final_unrouted": len(unrouted), "rounds": rounds,
               "closed": len(unrouted) == 0, "board": cur}
    return cur, summary


def classify(graph, drc):
    """{thin: signal nets that may size down, keep: power/current rails}."""
    return E.classify_stalled_nets(graph, drc)
