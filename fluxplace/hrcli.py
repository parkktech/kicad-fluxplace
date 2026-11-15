"""CLI glue for the hand-routing toolkit (fluxplace/handroute.py). Every command prints a compact verdict and, with --json, the
full machine-readable record on stdout. Registered from cli.build_parser via register(sub)."""
import json
import sys


def _emit(a, data, text):
    if getattr(a, "json", False):
        print(json.dumps(data, indent=1, default=str))
    else:
        print(text() if callable(text) else text)


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
    _emit(a, R, lambda: (H._print_open(R) or ""))


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
    _emit(a, P, text)


def cmd_probe(a):
    from . import handroute as H
    R = H.probe(a.board, a.net, a.path, a.constraints, a.width, _via(a), a.any_angle)
    _emit(a, R, lambda: (H._print_probe(R) or ""))
    return 0 if R["ok"] else 1


def cmd_lay(a):
    from . import handroute as H
    r = H.lay(a.board, a.net, a.path, a.out, a.constraints, a.width, _via(a), a.any_angle, a.partial)
    r.pop("cand_unc_pairs", None)
    _emit(a, r, lambda: f"lay {a.net}: {'ACCEPTED' if r['accepted'] else 'REJECTED'}  " + (r.get("reason") or "") + "\n  " + "  ".join(f"{k}={v}" for k, v in r.get("measure", {}).items()))
    return 0 if r["accepted"] else 1


def cmd_shove(a, op="shove"):
    from . import handroute as H
    r = H.shove(a.board, a.net, a.out, [i for i in (a.ids or "").split(",") if i] or None, _box(a.window) if a.window else None, a.chain,
                a.constraints, op=op)
    r.pop("cand_unc_pairs", None)

    def text():
        o = [f"{op} {a.net}: {'ACCEPTED' if r['accepted'] else 'REJECTED'} {r.get('reason') or ''}"]
        for x in (r.get("info") or {}).get("removed", []):
            o.append(f"  removed {x['kind']} {x['id']} {x['layer']} {x['a']}->{x['b']}")
        for x in r.get("opened", []):
            o.append(f"  opened: {x['a'].get('pin') or x['a']['kind']}({x['a']['x']:.2f},{x['a']['y']:.2f}) <-> {x['b'].get('pin') or x['b']['kind']}({x['b']['x']:.2f},{x['b']['y']:.2f})")
        return "\n".join(o)
    _emit(a, r, text)
    return 0 if r["accepted"] else 1


def cmd_unlay(a):
    return cmd_shove(a, "unlay")


def cmd_suggest(a):
    from . import handroute as H
    sp = tuple(float(x) for x in a.src.split(",")) if a.src else None
    dp = tuple(float(x) for x in a.dst.split(",")) if a.dst else None
    r = H.suggest(a.board, a.net, sp, dp, a.conn, a.constraints, a.width, _via(a), a.pitch, a.margin, a.via_cost,
                  a.layers.split(",") if a.layers else None, relax=a.relax, relax_cost=a.relax_cost, explore=a.explore)
    _emit(a, r, lambda: ("suggest: none - " + r["reason"] + "\n  " + "\n  ".join(f"remove {[(x['net'], x['id']) for x in e['remove']]} -> found={e['then_found']} {e['path'] or ''}" for e in r["explore"]) if r.get("explore") is not None else f"suggest(relaxed): {r['path']}\n  blockers: " + ", ".join(f"{b['kind']} {b['id']} {b['net']} {b['layer']}x{b['cells']}" for b in r["blockers"]) if r.get("blockers") is not None else f"suggest: {r['path']}\n  length {r['length_mm']} mm, {r['vias']} via(s), probe OK" if r.get("found") else f"suggest: none - {r['reason']}"))
    return 0 if r.get("found") else 1


def cmd_session(a):
    from . import handroute as H
    act = a.action
    if act == "begin":
        if not a.work_out:
            raise SystemExit("session begin SRC --work WORK")
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
        r = H.session_commit(a.work, a.out, a.constraints)
        text = (f"commit: {'ACCEPTED -> ' + r['out'] if r['accepted'] else 'REJECTED: ' + '; '.join(r['reasons'])}\n"
                f"  unconnected {r['start']['unconnected']} -> {r['work']['unconnected']}  errors {H._fmt_errs(r['work']['errors'])}")
    else:
        raise SystemExit("session action: begin|undo|status|ledger|commit")
    _emit(a, r, text)
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

    p = sub.add_parser("open-nets", help="read-only: list open connections")
    common(p)
    p.set_defaults(fn=cmd_open_nets)

    p = sub.add_parser("window", help="read-only: region as data + PNG")
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

    p = sub.add_parser("probe", help="read-only: check a path")
    common(p)
    geom(p)
    p.add_argument("--net", required=True)
    p.add_argument("--path", required=True, help='"F.Cu: x,y x,y; via x,y; B.Cu: x,y x,y"')
    p.add_argument("--any-angle", action="store_true", help="skip the 45/90 check")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("lay", help="lay a probed path, guarded")
    common(p)
    geom(p)
    p.add_argument("--net", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--out", default=None, help="output board (omit inside a session: edits the session WORK board)")
    p.add_argument("--any-angle", action="store_true")
    p.add_argument("--partial", action="store_true", help="do not require the net's open count to fall")
    p.set_defaults(fn=cmd_lay)

    for name, h in (("shove", "remove unlocked copper"),
                    ("unlay", "remove copper by id")):
        p = sub.add_parser(name, help=h)
        common(p)
        p.add_argument("--net", required=True, help="net whose copper is removed")
        p.add_argument("--ids", default=None, help="comma list of track/via ids (8+ hex chars) from `window`")
        p.add_argument("--window", default=None, help="X,Y,W,H: all copper of the net touching it")
        p.add_argument("--chain", action="store_true", help="extend each track to its whole run (to pad / via / branch)")
        p.add_argument("--out", default=None)
        p.set_defaults(fn=cmd_shove if name == "shove" else cmd_unlay)

    p = sub.add_parser("suggest", help="grid-search a legal path")
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
    p.set_defaults(fn=cmd_suggest)

    p = sub.add_parser("session", help="hand-route session + commit")
    p.add_argument("action", choices=["begin", "undo", "status", "ledger", "commit"])
    p.add_argument("work", help="begin: the SOURCE board; others: the WORK board")
    p.add_argument("--work", dest="work_out", default=None, help="begin: the WORK board to create")
    p.add_argument("--out", default=None)
    p.add_argument("--constraints", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_session)
