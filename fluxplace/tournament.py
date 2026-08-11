"""Placement tournament — the real autorouter as the fitness function.

The coarse gate is a fast proxy (seconds); freerouting is slow truth (minutes).
This module closes the loop the way serious flows do:

  1. Candidate placements across a parameter grid (fill, aspect, pad, jitter).
     The GATE filters: only overflow-0 survivors advance — never spend
     autorouter minutes on a placement the proxy already rejects.
  2. Each survivor becomes a real .kicad_pcb (project netclasses + GND/power
     planes poured) and freerouting attacks it, several JVMs in parallel.
  3. Fitness, lexicographic: unrouted (asc) -> vias (asc) -> wire length (asc).
     The winner is what a real router found cleanest.
  4. The winner's .ses imports straight back as copper (ImportSpecctraSES) —
     the tournament delivers a routed board.
  5. Calibration: gate rank vs freerouting rank across candidates shows whether
     the proxy predicts truth (tune Grid pitch/util when it doesn't).

Process model: the ORCHESTRATOR never imports pcbnew — every candidate is
placed/poured/exported in its own fresh interpreter (pcbnew's SWIG proxies
corrupt on repeated board loads in one process), then routed in its own JVM.
"""
import json
import os
import re
import subprocess
import sys
import time


CANDIDATES = [
    # (fill, aspect, pad, jitter_seed)  — deterministic, ordered
    (0.65, 1.35, 0.45, 0),      # shipped default
    (0.60, 1.35, 0.45, 0),
    (0.72, 1.35, 0.45, 0),
    (0.65, 1.00, 0.45, 0),
    (0.72, 1.00, 0.45, 0),
    (0.65, 1.75, 0.45, 0),
    (0.65, 1.35, 0.45, 1),
    (0.65, 1.35, 0.45, 2),
    (0.72, 1.35, 0.45, 1),
    (0.65, 1.35, 0.60, 0),
    (0.72, 1.35, 0.60, 0),
    (0.60, 1.00, 0.45, 0),
]

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def candidate_worker(board_path, workdir, idx, fill, aspect, pad, jitter):
    """Runs in a FRESH interpreter: place one candidate, write board + planes +
    DSN + metrics json. Never called from the orchestrator process."""
    sys.path.insert(0, _REPO)
    import pcbnew
    from fluxplace import kicad_io as IO, graph as G, topology as T, placement as P

    board = IO.load(board_path)
    parts, nets = IO.read_board(board)
    cg = G.build(parts, nets)
    topo = T.analyze(cg)
    t0 = time.time()
    pos, angles, rep = P.place_routed(parts, cg, topo, center=IO.board_center(board),
                                      pad=pad, fill=fill, aspect=aspect,
                                      jitter_seed=jitter)
    meta = dict(idx=idx, fill=fill, aspect=aspect, pad=pad, jitter=jitter,
                overflow=rep["overflow"], gate_wl=rep["wirelength"],
                pair_sep=rep.get("pair_sep"), hpwl=round(P.hpwl(parts, cg, pos)),
                place_secs=round(time.time() - t0, 1),
                routable=sorted(set(cg.signal_nets) | set(cg.power_traces)))
    if rep["overflow"] > 0:
        meta["status"] = "gate-rejected"
        json.dump(meta, open(os.path.join(workdir, f"cand_{idx}.json"), "w"))
        return

    _materialize(board, parts, pos, angles, workdir, idx, meta,
                 plane_nets=(("In1.Cu", "GND"), ("In2.Cu", "VIN_PROT")))


def _materialize(board, parts, pos, angles, workdir, idx, meta, plane_nets):
    """Shared tail: apply placement, shrinkwrap, pour planes, save, DSN."""
    import pcbnew
    from fluxplace import kicad_io as IO, placement as P

    IO.apply_orientations(board, angles)
    IO.apply_positions(board, pos, parts)
    xs0 = []; ys0 = []; xs1 = []; ys1 = []
    for ref, (x, y) in pos.items():
        w, h = P.eff_size(parts, ref, angles.get(ref, 0.0), 0.0)
        xs0.append(x - w / 2); ys0.append(y - h / 2)
        xs1.append(x + w / 2); ys1.append(y + h / 2)
    bx0, by0, bx1, by1 = min(xs0), min(ys0), max(xs1), max(ys1)
    IO.shrinkwrap_outline(board, bx0, by0, bx1, by1)

    # pour planes on the SAME live board — bounds from the shrinkwrap numbers we
    # already hold in mm (no fragile bbox round-trip through SWIG)
    mm = pcbnew.FromMM
    pts = [(mm(bx0 - 1.5), mm(by0 - 1.5)), (mm(bx1 + 1.5), mm(by0 - 1.5)),
           (mm(bx1 + 1.5), mm(by1 + 1.5)), (mm(bx0 - 1.5), mm(by1 + 1.5))]
    for layer_name, net_name in plane_nets:
        net = board.FindNet(net_name)
        if not net:
            continue
        z = pcbnew.ZONE(board)
        z.SetLayer(board.GetLayerID(layer_name))
        z.SetNetCode(net.GetNetCode())
        z.Outline().NewOutline()
        for x, y in pts:
            z.AppendCorner(pcbnew.VECTOR2I(int(x), int(y)), -1)
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
        z.SetMinThickness(pcbnew.FromMM(0.2))
        board.Add(z)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())

    cpcb = os.path.join(workdir, f"cand_{idx}.kicad_pcb")
    IO.save(board, cpcb)
    pro_src = os.path.splitext(board.GetFileName())[0] + ".kicad_pro"
    if os.path.exists(pro_src):
        open(os.path.join(workdir, f"cand_{idx}.kicad_pro"), "w").write(open(pro_src).read())
    # razor calibration lesson: with default 0.2/0.2 netclasses freerouting
    # proves DF40 escape impossible; 0.127/0.127 routes the same nets. Via
    # class 0.6/0.3 keeps JLC hole-to-copper honest during autoroute.
    try:
        for _name, nc in board.GetAllNetClasses().items():
            nc.SetClearance(pcbnew.FromMM(0.127))
            nc.SetTrackWidth(pcbnew.FromMM(0.15))
            nc.SetViaDiameter(pcbnew.FromMM(0.6))
            nc.SetViaDrill(pcbnew.FromMM(0.3))
    except Exception as e:
        print("netclass prep skipped:", e)
    ok = bool(pcbnew.ExportSpecctraDSN(board, os.path.join(workdir, f"cand_{idx}.dsn")))
    meta["status"] = "queued" if ok else "dsn-failed"
    json.dump(meta, open(os.path.join(workdir, f"cand_{idx}.json"), "w"))


def parse_compact_grid(spec):
    """'sx:sy[:gap[:pack]],...' -> [(sx, sy, gap, pack)]."""
    out = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        f = item.split(":")
        out.append((float(f[0]), float(f[1]),
                    float(f[2]) if len(f) > 2 else 0.45,
                    int(f[3]) if len(f) > 3 else 3))
    return out


def compact_candidate_worker(board_path, workdir, idx, sx, sy, gap, pack,
                             obstacle_specs, plane_nets):
    """Fresh interpreter: candidate = COMPACTED current placement (strips
    stale copper first), gate-scored like any placer candidate."""
    sys.path.insert(0, _REPO)
    import pcbnew  # noqa: F401 — registers SWIG wrappers before board surgery
    from fluxplace import kicad_io as IO, graph as G, placement as P
    from fluxplace import compact as CC, route as R

    os.makedirs(workdir, exist_ok=True)
    board = IO.load(board_path)
    _keep = []  # hold refs until process exit — SWIG GC of removed items corrupts
    for t in list(board.GetTracks()):
        _keep.append(t)
        board.Remove(t)
    for z in list(board.Zones()):
        _keep.append(z)
        board.Remove(z)
    compact_candidate_worker._keep = _keep
    parts, nets = IO.read_board(board)
    obstacles = CC.parse_obstacles(obstacle_specs or [], log=lambda *a: None)
    t0 = time.time()
    pos, st = CC.compact(parts, sx, sy, gap=gap, pack=pack,
                         obstacles=obstacles)
    cg = G.build(parts, nets)
    rep = R.score(parts, pos, cg)
    meta = dict(idx=idx, mode="compact", fill=sx, aspect=sy, pad=gap,
                jitter=pack, resid=st["resid"], overflow=rep["overflow"],
                gate_wl=rep["wirelength"], hpwl=round(P.hpwl(parts, cg, pos)),
                place_secs=round(time.time() - t0, 1),
                routable=sorted(set(cg.signal_nets) | set(cg.power_traces)))
    # compact grids are small and the gate over-penalizes elbow room
    # (tournament #1 calibration: truth rank != gate rank) — only reject
    # hopeless congestion; freerouting is the judge that counts
    if st["resid"] or rep["overflow"] > 30:
        meta["status"] = "overlaps" if st["resid"] else "gate-rejected"
        json.dump(meta, open(os.path.join(workdir, f"cand_{idx}.json"), "w"))
        return
    angles = {r: parts[r].get("angle0", 0.0) for r in parts}
    _materialize(board, parts, pos, angles, workdir, idx, meta,
                 plane_nets=tuple(plane_nets))


def _ses_metrics(ses_path, log_path, routable):
    """Parse a freerouting session + log into fitness numbers."""
    out = dict(unrouted=None, wires=0, vias=0, wl=0.0, routed_scope=0)
    if os.path.exists(log_path):
        last = None
        for m in re.finditer(r"pass #\d+ .*?\((\d+) unrouted\)",
                             open(log_path, errors="ignore").read()):
            last = int(m.group(1))
        out["unrouted"] = last
    if not os.path.exists(ses_path):
        return out
    txt = open(ses_path, errors="ignore").read()
    out["wires"] = txt.count("(wire")
    out["vias"] = txt.count("(via")
    ses_nets = set(re.findall(r'\(net\s+"?([^\s")]+)', txt))
    out["routed_scope"] = len({n for n in ses_nets if n in routable})
    wl = 0.0   # sum manhattan runs of each path's coordinate list (0.1 um units)
    for pm in re.finditer(r"\(path\s+\S+\s+\d+((?:\s+-?[\d.]+)+)\s*\)", txt):
        nums = [float(v) for v in pm.group(1).split()]
        pts = list(zip(nums[0::2], nums[1::2]))
        for a, b in zip(pts, pts[1:]):
            wl += abs(a[0] - b[0]) + abs(a[1] - b[1])
    out["wl"] = round(wl / 1e4)   # -> mm
    return out


def run(board_path, jar, workdir, passes=25, jobs=3, candidates=None, log=print,
        place_jobs=2, resume=False, oit=None, compact_grid=None, obstacles=(),
        plane_nets=None):
    """Full tournament. Orchestration only — no pcbnew in this process.
    `resume`: reuse existing cand_*.json / .ses; adopt orphaned live JVMs.
    `oit`: freerouting optimization-improvement threshold (%) — caps the silent
    post-routing optimizer phase that otherwise runs for an hour per candidate."""
    os.makedirs(workdir, exist_ok=True)
    cands = candidates or CANDIDATES
    if compact_grid:
        cands = list(compact_grid)
    plane_nets = tuple(plane_nets or (("In1.Cu", "GND"), ("In2.Cu", "VIN_PROT")))

    # ---- stage 1: place candidates in fresh interpreters (parallel) ----
    def spawn_place(i, c):
        if compact_grid:
            sx, sy, gp, pk = c
            code = (f"import sys; sys.path.insert(0, {_REPO!r}); "
                    f"from fluxplace.tournament import compact_candidate_worker; "
                    f"compact_candidate_worker({board_path!r}, {workdir!r}, {i}, "
                    f"{sx}, {sy}, {gp}, {pk}, {list(obstacles)!r}, {plane_nets!r})")
        else:
            fill, aspect, pad, jitter = c
            code = (f"import sys; sys.path.insert(0, {_REPO!r}); "
                    f"from fluxplace.tournament import candidate_worker; "
                    f"candidate_worker({board_path!r}, {workdir!r}, {i}, "
                    f"{fill}, {aspect}, {pad}, {jitter})")
        return subprocess.Popen([sys.executable, "-c", code],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=dict(os.environ))

    pending = [(i, c) for i, c in enumerate(cands)
               if not (resume and os.path.exists(os.path.join(workdir, f"cand_{i}.json")))]
    procs = []
    while pending or procs:
        while pending and len(procs) < place_jobs:
            i, c = pending.pop(0)
            procs.append((spawn_place(i, c), i))
        time.sleep(2)
        for pr, i in list(procs):
            if pr.poll() is not None:
                procs.remove((pr, i))
                mp = os.path.join(workdir, f"cand_{i}.json")
                if os.path.exists(mp):
                    m = json.load(open(mp))
                    log(f"cand {i} {m['fill']}/{m['aspect']}/{m['pad']}/j{m['jitter']}: "
                        f"{m['status']} (hpwl {m.get('hpwl')}, overflow {m['overflow']})")
                else:
                    log(f"cand {i}: worker died (no metrics json)")

    results = []
    for i in range(len(cands)):
        mp = os.path.join(workdir, f"cand_{i}.json")
        if os.path.exists(mp):
            results.append(json.load(open(mp)))

    # ---- stage 2: freerouting the survivors, bounded concurrency ----
    def finish(r):
        i = r["idx"]
        met = _ses_metrics(os.path.join(workdir, f"cand_{i}.ses"),
                           os.path.join(workdir, f"cand_{i}.frlog"),
                           set(r.get("routable", [])))
        r.update(met, status="routed")
        log(f"cand {i}: unrouted={met['unrouted']} vias={met['vias']} "
            f"wl={met['wl']}mm")

    queue, external = [], []
    for r in results:
        if r.get("status") != "queued":
            continue
        i = r["idx"]
        if resume and os.path.exists(os.path.join(workdir, f"cand_{i}.ses")):
            finish(r)                       # already routed in a prior run
        elif resume and subprocess.run(["pgrep", "-f", f"java .*cand_{i}\\.dsn"],
                                       capture_output=True).returncode == 0:
            external.append(r)              # adopt an orphaned live JVM
            log(f"cand {i}: adopting live external JVM")
        else:
            queue.append(r)

    running = []
    while queue or running or external:
        while queue and len(running) < jobs:
            r = queue.pop(0)
            i = r["idx"]
            # freerouting v2 keeps mutable global state (config, logs, locks)
            # in java.io.tmpdir/freerouting — concurrent JVMs sharing it die
            # silently. Give every job its own tmpdir.
            jtmp = os.path.join(workdir, f"jvmtmp_{i}")
            os.makedirs(jtmp, exist_ok=True)
            cmd = ["java", f"-Djava.io.tmpdir={jtmp}", "-jar", jar,
                   "-de", os.path.join(workdir, f"cand_{i}.dsn"),
                   "-do", os.path.join(workdir, f"cand_{i}.ses"),
                   "-mp", str(passes),
                   # v2 defaults to a short per-job timeout that silently
                   # abandons the run with no .ses — give real boards hours
                   "--router.job_timeout=02:00:00"]
            if oit is not None:
                cmd += ["-oit", str(oit)]
            proc = subprocess.Popen(
                cmd, stdout=open(os.path.join(workdir, f"cand_{i}.frlog"), "w"),
                stderr=subprocess.STDOUT)
            running.append((proc, r))
            log(f"cand {r['idx']}: freerouting started")
        time.sleep(5)
        for r in list(external):
            i = r["idx"]
            alive = subprocess.run(["pgrep", "-f", f"java .*cand_{i}\\.dsn"],
                                   capture_output=True).returncode == 0
            if not alive:
                external.remove(r)
                finish(r)
        for proc, r in list(running):
            if proc.poll() is None:
                continue
            running.remove((proc, r))
            finish(r)

    # ---- stage 3: fitness + winner + calibration snapshot ----
    routed = [r for r in results if r.get("status") == "routed"
              and r.get("unrouted") is not None and r.get("wires", 0) > 0]
    winner = None
    if routed:
        routed.sort(key=lambda r: (r["unrouted"], r["vias"], r["wl"]))
        winner = routed[0]
        log(f"WINNER: cand {winner['idx']} (fill {winner['fill']}, "
            f"aspect {winner['aspect']}, pad {winner['pad']}, jitter {winner['jitter']}) "
            f"— unrouted {winner['unrouted']}, vias {winner['vias']}, wl {winner['wl']}mm")
        gate_rank = sorted(routed, key=lambda r: r["gate_wl"])
        log("calibration (gate rank -> truth rank): " + ", ".join(
            f"c{r['idx']}:{gate_rank.index(r) + 1}->{routed.index(r) + 1}"
            for r in routed))
    json.dump([{k: v for k, v in r.items() if k not in ("routable",)}
               for r in results],
              open(os.path.join(workdir, "tournament.json"), "w"), indent=1)
    return results, winner


def import_winner(workdir, winner_idx, out_board=None):
    """Import the winning session's copper into the winning candidate board.
    Run in a fresh interpreter (subprocess) for the same SWIG-safety reason."""
    code = (f"import sys; sys.path.insert(0, {_REPO!r}); import pcbnew; "
            f"b = pcbnew.LoadBoard({os.path.join(workdir, f'cand_{winner_idx}.kicad_pcb')!r}); "
            f"ok = pcbnew.ImportSpecctraSES(b, {os.path.join(workdir, f'cand_{winner_idx}.ses')!r}); "
            f"out = {out_board!r} or "
            f"{os.path.join(workdir, f'cand_{winner_idx}_routed.kicad_pcb')!r}; "
            f"pcbnew.SaveBoard(out, b); print('OK' if ok else 'FAIL', out)")
    rc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        env=dict(os.environ))
    line = (rc.stdout.strip().splitlines() or ["FAIL"])[-1]
    return line.startswith("OK"), line.split(" ", 1)[-1]
