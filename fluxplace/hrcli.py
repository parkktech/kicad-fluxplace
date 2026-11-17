"""CLI glue for the hand-routing toolkit (fluxplace/handroute.py). Every command prints a compact verdict and, with --json, the
full machine-readable record on stdout. Registered from cli.build_parser via register(sub)."""
import importlib
import json
import multiprocessing
import os
import shutil
import sys
import tempfile


def _first(x):
    return x[0] if isinstance(x, (list, tuple)) and x else x


def _viol(v):
    """One violation dict -> short text, from whatever keys it has."""
    if not isinstance(v, dict):
        return str(v)[:120]
    s = str(v.get("kind", "violation"))
    if v.get("gap") is not None and v.get("need") is not None:
        s += f" {v['gap']} < {v['need']}"
    elif v.get("gap") is not None:
        s += f" gap {v['gap']}"
    w = v.get("with") or v.get("item")
    if w:
        s += f" vs {w}"
    at = v.get("at")
    if at is not None:
        s += " at " + (",".join(str(x) for x in at) if isinstance(at, (list, tuple)) else str(at))
    if v.get("clear_dir") and v.get("clear_shift_mm") is not None:
        dx, dy = v["clear_dir"]
        ax, ay = abs(dx), abs(dy)
        d = ("+x" if dx > 0 else "-x") if ax >= ay * 2 else ("+y" if dy > 0 else "-y") if ay >= ax * 2 else f"({dx:+.2f},{dy:+.2f})"
        s += f" move {v['clear_shift_mm']} mm toward {d}"
    return s


def _reason(r):
    """First reason of a result as text ('probe failed: [json]' is unpacked to its first violation)."""
    rs = r.get("reasons") if isinstance(r.get("reasons"), list) else None
    t = r.get("reason") or (rs[0] if rs else None)
    if not t:
        return None
    t = str(t).split(" | ")[0]
    if t.startswith("probe failed: "):
        try:
            t = "probe failed: " + _viol(_first(json.loads(t[14:])))
        except Exception:
            pass
    return t[:200]


def summary(kind, r):
    """One-line verdict + first reason for a probe/lay/shove/unlay/suggest/commit result dict (defensive about missing keys)."""
    try:
        net = r.get("net")
        if kind == "probe":
            if r.get("ok"):
                return f"probe ok{' ' + str(net) if net else ''}: no violations"
            vs = r.get("violations") or []
            return f"probe failed: {_viol(vs[0]) if vs else _reason(r) or 'unknown'}" + (f" (+{len(vs) - 1} more)" if len(vs) > 1 else "")
        if kind == "suggest":
            if r.get("found"):
                return (f"suggest found: {r.get('length_mm', '?')} mm, {r.get('vias', '?')} via(s)"
                        + (f"; path {r['path']}" if r.get("path") else "")
                        + (" (relaxed, blockers listed)" if r.get("blockers") else ""))
            return "suggest none: " + (_reason(r) or "no path")
        if "accepted" in r:
            ok = r["accepted"]
            head = f"{kind} {'accepted' if ok else 'rejected'}"
            return head + (f" -> {r['out']}" if ok and r.get("out") else "") + ("" if ok else f": {_reason(r) or 'no reason given'}")
        return f"{kind}: " + (_reason(r) or "done")
    except Exception as e:  # a summary must never break the command
        return f"{kind}: (summary unavailable: {type(e).__name__})"


def _emit(a, data, text, kind=None, rev=None):
    """Brief normally replaces other output; suggest with both flags keeps JSON on stdout and puts brief on stderr."""
    if kind and isinstance(data, dict):
        line = summary(kind, data)
        if getattr(a, "brief", False):
            both = kind == "suggest" and getattr(a, "json", False)
            print(line, file=sys.stderr if both else sys.stdout)
            if not both:
                return
        data = {"summary": line, **data}
    if rev and isinstance(data, dict) and getattr(a, "json", False):
        data = {**data, "board_rev": rev}
    if getattr(a, "json", False):
        print(json.dumps(data, indent=1, default=str))
    else:
        print(text() if callable(text) else text)


def _rev(a):
    from . import boardrev
    return boardrev.rev(a.board)


def _write_guard(a, *paths):
    """--expect-rev (stale plan) + KiCad-open lock check on the board(s) about to be written. Returns an exit code or None."""
    from . import boardlock, boardrev
    er = getattr(a, "expect_rev", None)
    if er:
        m = boardrev.check(a.board, er)
        if m:
            print(m, file=sys.stderr)
            return 2
    try:
        for p in paths:
            boardlock.guard(p, getattr(a, "force_open", False))
    except boardlock.BoardOpenError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return None


def _invoke(spec, args, kwargs, queue):
    try:
        module, name = spec.split(":", 1)
        fn = getattr(importlib.import_module(module), name)
        queue.put((True, fn(*args, **kwargs)))
    except Exception as e:
        queue.put((False, (type(e).__name__, str(e))))


def _edit_paths(board, out):
    target = out or board
    paths = [target, os.path.splitext(target)[0] + ".kicad_pro"]
    if out is None:
        paths.append(board + ".session")
    return paths


def _timed(spec, args, kwargs, timeout, paths):
    """Run a guarded edit in a child; on expiry restore every destination exactly and return no verdict."""
    if timeout is None or timeout <= 0:
        module, name = spec.split(":", 1)
        return getattr(importlib.import_module(module), name)(*args, **kwargs)
    with tempfile.TemporaryDirectory(prefix="fluxplace-timeout-") as td:
        saved = []
        for i, path in enumerate(paths):
            backup = os.path.join(td, str(i))
            exists = os.path.exists(path)
            if os.path.isdir(path):
                shutil.copytree(path, backup)
            elif exists:
                shutil.copy(path, backup)
            saved.append((path, backup, exists))
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.SimpleQueue()
        p = ctx.Process(target=_invoke, args=(spec, args, kwargs, queue))
        p.start()
        p.join(timeout)
        if p.is_alive():
            p.terminate()
            p.join(5)
            if p.is_alive():
                p.kill()
                p.join()
            for path, backup, exists in saved:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                elif os.path.exists(path):
                    os.remove(path)
                if exists:
                    if os.path.isdir(backup):
                        shutil.copytree(backup, path)
                    else:
                        shutil.copy(backup, path)
            return {"accepted": False, "timed_out": True,
                    "reason": f"no verdict: timed out after {timeout:g} s, nothing written"}
        if not queue.empty():
            ok, value = queue.get()
            if ok:
                return value
            raise RuntimeError(f"{value[0]}: {value[1]}")
        raise RuntimeError(f"guarded edit exited without a result (code {p.exitcode})")


def _via(a):
    if not getattr(a, "via", None):
        return None
    d, r = (float(x) for x in a.via.split("/"))
    return (d, r)


def _box(s):
    v = [float(x) for x in s.replace("x", ",").split(",")]
    if len(v) != 4:
        raise SystemExit("--window needs X,Y,W,H")
    return tuple(v)


def cmd_open_nets(a):
    from . import handroute as H
    R = H.open_nets(a.board, a.constraints)
    _emit(a, R, lambda: (H._print_open(R) or ""), rev=_rev(a))


def cmd_window(a):
    from . import handroute as H
    at = size = None
    if a.at:
        at = tuple(float(x) for x in a.at.split(","))
        size = tuple(float(x) for x in a.size.split(","))
        if len(at) != 2 or len(size) != 2:
            raise SystemExit("--at X,Y and --size W,H")
    elif not (a.net or a.conn is not None):
        raise SystemExit("window needs --at/--size, --net or --conn")
    prims = None
    if a.path:
        from . import hrgeom as G
        import pcbnew
        prims = G.parse_path(a.path, [pcbnew.LoadBoard(a.board).GetLayerName(i) for i in pcbnew.LoadBoard(a.board).GetEnabledLayers().CuStack()])
    W = H.window(a.board, at=at, size=size, net=a.net, conn=a.conn, layers=a.layers.split(",") if a.layers else None,
                 constraints=a.constraints, width=a.width, via=_via(a), pitch=a.pitch, margin=a.margin, png=a.png, svg=a.svg,
                 overlay=prims, free_net=a.for_net)
    P = H._window_public(W)

    def text():
        out = [f"window {P['window']} layers {P['layers']} for_net {P['for_net'] or '-'} track {P['track']} via {P['via']}",
               f"  pads {len(P['pads'])} tracks {len(P['tracks'])} vias {len(P['vias'])} zones {len(P['zones'])} keepouts {len(P['keepouts'])}  via sites {P['via_sites']['count']} cells"]
        for l, cs in P["free_corridors"].items():
            out.append(f"  {l}: " + ("; ".join(f"#{c['id']} {c['area_mm2']}mm2 max {c['max_track_mm']}mm" for c in cs[:4]) or "no free corridor"))
        if "connection_view" in P:
            out.append(f"  direct corridor layers: {P['connection_view']['direct_corridor_layers']}")
        out += [f"  image {i}" for i in P.get("images", [])]
        out.append("  (use --json for the full record)")
        return "\n".join(out)
    _emit(a, P, text, rev=_rev(a))


def cmd_probe(a):
    from . import handroute as H
    R = H.probe(a.board, a.net, a.path, a.constraints, a.width, _via(a), a.any_angle)
    _emit(a, R, lambda: (H._print_probe(R) or ""), "probe", _rev(a))
    return 0 if R["ok"] else 1


def cmd_lay(a):
    g = _write_guard(a, a.out or a.board)
    if g is not None:
        return g
    r = _timed("fluxplace.handroute:lay", (a.board, a.net, a.path, a.out, a.constraints, a.width, _via(a), a.any_angle, a.partial),
               {}, a.timeout, _edit_paths(a.board, a.out))
    r.pop("cand_unc_pairs", None)
    _emit(a, r, lambda: f"lay {a.net}: {'ACCEPTED' if r['accepted'] else 'REJECTED'}  " + (r.get("reason") or "") + "\n  " + "  ".join(f"{k}={v}" for k, v in r.get("measure", {}).items()), "lay")
    return 2 if r.get("timed_out") else 0 if r["accepted"] else 1


def cmd_copy_net(a):
    g = _write_guard(a, a.out or a.board)
    if g is not None:
        return g
    r = _timed("fluxplace.handroute:copy_net", (a.src, a.board, a.net, a.out, a.constraints), {}, a.timeout, _edit_paths(a.board, a.out))
    r.pop("cand_unc_pairs", None)
    _emit(a, r, lambda: f"copy-net {a.net}: {'ACCEPTED' if r['accepted'] else 'REJECTED'}  " + (r.get("reason") or "") + "\n  " + "  ".join(f"{k}={v}" for k, v in r.get("measure", {}).items()), "copy-net")
    return 2 if r.get("timed_out") else 0 if r["accepted"] else 1


def cmd_via_min(a):
    from . import viamin
    g = _write_guard(a, a.out)
    if g is not None:
        return g
    r = viamin.via_min(a.board, a.out, a.constraints, budget=a.budget)
    r["summary"] = (f"via-min: {r['changed']} raised to {r['floor_mm']} mm, {r['kept']} already ok, {r['rejected']} rejected by DRC, "
                    f"{r['not_reached']} not reached, {r['locked_skipped']} locked")
    if a.report:
        json.dump(r, open(a.report, "w"), indent=1, default=str)
    _emit(a, r, lambda: r["summary"], None, None)
    return 0


def cmd_repair(a):
    from . import handrepair as repair
    g = _write_guard(a, a.out)
    if g is not None:
        return g
    r = repair.repair(a.board, a.out, a.constraints, a.max_attempts, a.budget, a.layers, a.report)
    r["summary"] = f"repair: {r['closed']} closed, {r['blocked']} blocked, {r['pairs']} pair(s) skipped; commit {'accepted' if r['commit']['accepted'] else 'rejected: ' + '; '.join(r['commit']['reasons'][:1])}"
    _emit(a, r, lambda: r["summary"], None, None)
    return 0 if r["commit"]["accepted"] else 1


def cmd_shove(a, op="shove"):
    g = _write_guard(a, a.out or a.board)
    if g is not None:
        return g
    args = (a.board, a.net, a.out, [i for i in (a.ids or "").split(",") if i] or None, _box(a.window) if a.window else None,
            a.chain, a.constraints)
    r = _timed("fluxplace.handroute:shove", args, {"op": op}, a.timeout, _edit_paths(a.board, a.out))
    r.pop("cand_unc_pairs", None)

    def text():
        o = [f"{op} {a.net}: {'ACCEPTED' if r['accepted'] else 'REJECTED'} {r.get('reason') or ''}"]
        for x in (r.get("info") or {}).get("removed", []):
            o.append(f"  removed {x['kind']} {x['id']} {x['layer']} {x['a']}->{x['b']}")
        for x in r.get("opened", []):
            o.append(f"  opened: {x['a'].get('pin') or x['a']['kind']}({x['a']['x']:.2f},{x['a']['y']:.2f}) <-> {x['b'].get('pin') or x['b']['kind']}({x['b']['x']:.2f},{x['b']['y']:.2f})")
        return "\n".join(o)
    _emit(a, r, text, op)
    return 2 if r.get("timed_out") else 0 if r["accepted"] else 1


def cmd_unlay(a):
    return cmd_shove(a, "unlay")


def cmd_suggest(a):
    from . import handroute as H
    sp = tuple(float(x) for x in a.src.split(",")) if a.src else None
    dp = tuple(float(x) for x in a.dst.split(",")) if a.dst else None
    try:
        r = H.suggest(a.board, a.net, sp, dp, a.conn, a.constraints, a.width, _via(a), a.pitch, a.margin, a.via_cost,
                      a.layers.split(",") if a.layers else None, relax=a.relax, relax_cost=a.relax_cost, explore=a.explore)
    except Exception as e:
        r = {"found": False, "reason": f"internal error: {type(e).__name__}: {e}"}
    _emit(a, r, lambda: ("suggest: none - " + r["reason"] + "\n  " + "\n  ".join(f"remove {[(x['net'], x['id']) for x in e['remove']]} -> found={e['then_found']} {e['path'] or ''}" for e in r["explore"]) if r.get("explore") is not None else f"suggest(relaxed): {r['path']}\n  blockers: " + ", ".join(f"{b['kind']} {b['id']} {b['net']} {b['layer']}x{b['cells']}" for b in r["blockers"]) if r.get("blockers") is not None else f"suggest: {r['path']}\n  length {r['length_mm']} mm, {r['vias']} via(s), probe OK" if r.get("found") else f"suggest: none - {r['reason']}"), "suggest", _rev(a))
    return 0 if r.get("found") else 1


def cmd_session(a):
    from . import handroute as H
    act = a.action
    if act == "begin":
        if not a.work_out:
            raise SystemExit("session begin SRC --work WORK")
        g = _write_guard(a, a.work_out)
        if g is not None:
            return g
        r = H.session_begin(a.work, a.work_out)
        text = f"session begun: {r['work']}  unconnected {r['unconnected']}  errors {H._fmt_errs(r['errors'])}"
    elif act == "undo":
        r = H.session_undo(a.work)
        text = f"undo: {r}"
    elif act == "status":
        r = H.session_status(a.work)
        text = (f"session {r['work']}: edits {r['edits_applied']} attempts {r['attempts']} rejected {r['rejected']}\n"
                f"  start: unconnected {r['start']['unconnected']} errors {H._fmt_errs(r['start']['errors'])}\n"
                f"  now:   unconnected {r['now']['unconnected']} errors {H._fmt_errs(r['now']['errors'])}\n  ledger {r['ledger']}")
    elif act == "ledger":
        import os
        p = os.path.join(a.work + ".session", "ledger.txt")
        r = {"ledger": p}
        text = open(p).read() if os.path.exists(p) else "(empty)"
    elif act == "commit":
        if not a.out:
            raise SystemExit("session commit needs --out")
        g = _write_guard(a, a.out)
        if g is not None:
            return g
        r = H.session_commit(a.work, a.out, a.constraints)
        text = (f"commit: {'ACCEPTED -> ' + r['out'] if r['accepted'] else 'REJECTED: ' + '; '.join(r['reasons'])}\n"
                f"  unconnected {r['start']['unconnected']} -> {r['work']['unconnected']}  errors {H._fmt_errs(r['work']['errors'])}")
    else:
        raise SystemExit("session action: begin|undo|status|ledger|commit")
    _emit(a, r, text, "commit" if act == "commit" else None)
    return 0 if r.get("accepted", True) else 1


def register(sub):
    def common(p, board=True):
        if board:
            p.add_argument("board")
        p.add_argument("--constraints", default=None, help="constraints TOML: raises the floors ([route], [fab] profile)")
        p.add_argument("--json", action="store_true", help="full JSON on stdout")

    def geom(p):
        p.add_argument("--width", type=float, default=None, help="track width mm (default: net class; never below the floor)")
        p.add_argument("--via", default=None, help="via DIA/DRILL mm (default: net class)")

    def brief(p):
        p.add_argument("--brief", action="store_true", help="print only the one-line verdict (exit code unchanged)")

    def guards(p):
        p.add_argument("--expect-rev", default=None, help="board_rev from probe/suggest: refuse to write if the board changed since")
        p.add_argument("--force-open", action="store_true", help="write even if KiCad has the board open (lock file present)")
        p.add_argument("--timeout", type=float, default=600.0,
                       help="lay/shove/copy-net hard cap; expiry restores all destinations and exits 2 (default 600; 0 disables)")

    p = sub.add_parser("open-nets", help="list open nets")
    common(p)
    p.set_defaults(fn=cmd_open_nets)

    p = sub.add_parser("window", help="region data + PNG")
    common(p)
    geom(p)
    p.add_argument("--at", default=None, help="window centre X,Y mm")
    p.add_argument("--size", default="6,6", help="window W,H mm")
    p.add_argument("--net", default=None, help="frame this net's open connection")
    p.add_argument("--conn", type=int, default=None, help="frame open connection INDEX (open-nets order)")
    p.add_argument("--for-net", default=None, help="compute corridors/via sites as if routing this net")
    p.add_argument("--layers", default=None, help="comma list (F.Cu,In1.Cu,B.Cu); default all")
    p.add_argument("--pitch", type=float, default=0.05, help="free-space grid pitch mm")
    p.add_argument("--margin", type=float, default=1.5, help="mm around a framed connection")
    p.add_argument("--png", default=None, help="write a PNG render")
    p.add_argument("--svg", default=None, help="write an SVG render")
    p.add_argument("--path", default=None, help="overlay a proposed path on the render")
    p.set_defaults(fn=cmd_window)

    p = sub.add_parser("probe", help="check a path")
    common(p)
    geom(p)
    p.add_argument("--net", required=True)
    p.add_argument("--path", required=True, help='"F.Cu: x,y x,y; via x,y; B.Cu: x,y x,y"')
    p.add_argument("--any-angle", action="store_true", help="skip the 45/90 check")
    brief(p)
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("lay", help="guarded lay")
    common(p)
    geom(p)
    p.add_argument("--net", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--out", default=None, help="output board (omit inside a session: edits the session WORK board)")
    p.add_argument("--any-angle", action="store_true")
    p.add_argument("--partial", action="store_true", help="do not require the net's open count to fall")
    brief(p)
    guards(p)
    p.set_defaults(fn=cmd_lay)

    p = sub.add_parser("copy-net", help="restore a net")
    p.add_argument("src", help="board to copy the net's tracks and vias FROM")
    common(p)
    p.add_argument("--net", required=True)
    p.add_argument("--out", default=None, help="output board (omit inside a session: edits the session WORK board = DST)")
    brief(p)
    guards(p)
    p.set_defaults(fn=cmd_copy_net)

    p = sub.add_parser("via-min", help="raise vias")
    common(p)
    p.add_argument("--out", required=True)
    p.add_argument("--budget", type=float, default=None, help="total seconds (default: 3 per candidate, minimum 60); partial output exits 0")
    p.add_argument("--report", default=None, help="write the changed/kept/rejected/not-reached JSON report")
    guards(p)
    p.set_defaults(fn=cmd_via_min)

    p = sub.add_parser("hand-repair", help="close open nets")
    common(p)
    p.add_argument("--out", required=True, help="committed board (WORK is the session work board)")
    p.add_argument("--max-attempts", type=int, default=2, help="per connection")
    p.add_argument("--budget", type=float, default=900.0, help="total seconds")
    p.add_argument("--layers", default=None)
    p.add_argument("--report", default=None, help="JSON report path")
    guards(p)
    p.set_defaults(fn=cmd_repair)

    for name, h in (("shove", "remove copper"),
                    ("unlay", "remove copper by ID")):
        p = sub.add_parser(name, help=h)
        common(p)
        p.add_argument("--net", required=True, help="net whose copper is removed")
        p.add_argument("--ids", default=None, help="comma list of track/via ids (8+ hex chars) from `window`")
        p.add_argument("--window", default=None, help="X,Y,W,H: all copper of the net touching it")
        p.add_argument("--chain", action="store_true", help="extend each track to its whole run (to pad / via / branch)")
        p.add_argument("--out", default=None)
        brief(p)
        guards(p)
        p.set_defaults(fn=cmd_shove if name == "shove" else cmd_unlay)

    p = sub.add_parser("suggest", help="grid search")
    common(p)
    geom(p)
    p.add_argument("--net", default=None)
    p.add_argument("--conn", type=int, default=None, help="open-nets connection index")
    p.add_argument("--from", dest="src", default=None, help="X,Y near the source island")
    p.add_argument("--to", dest="dst", default=None, help="X,Y near the target island")
    p.add_argument("--layers", default=None)
    p.add_argument("--pitch", type=float, default=0.05)
    p.add_argument("--margin", type=float, default=2.0)
    p.add_argument("--via-cost", type=float, default=2.0, help="mm-equivalent cost of a via")
    p.add_argument("--relax-cost", type=float, default=1.5, help="mm-equivalent cost per grid cell crossing an unlocked foreign track/via (higher = fewer blockers)")
    p.add_argument("--explore", action="store_true", help="when no path: try removing each limiting UNLOCKED track/via (singles, then pairs) and report what would free it")
    p.add_argument("--relax", action="store_true", help="let the path cross UNLOCKED foreign tracks/vias at a cost and list them as blockers")
    brief(p)
    p.set_defaults(fn=cmd_suggest)

    p = sub.add_parser("session", help="hand-route")
    p.add_argument("action", choices=["begin", "undo", "status", "ledger", "commit"])
    p.add_argument("work", help="begin: the SOURCE board; others: the WORK board")
    p.add_argument("--work", dest="work_out", default=None, help="begin: the WORK board to create")
    p.add_argument("--out", default=None)
    p.add_argument("--constraints", default=None)
    p.add_argument("--json", action="store_true")
    brief(p)
    p.add_argument("--force-open", action="store_true", help="write even if KiCad has the board open (lock file present)")
    p.set_defaults(fn=cmd_session)
