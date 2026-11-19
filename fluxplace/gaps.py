"""Gap loop: classify the DRC gaps of a board, dispatch each kind to its repair rungs, guard every candidate, and log
each try to the attempts file. Phase 1 covers the open, DRC and via kinds (L0-L1) and uses a local guard `_accept`
that phase 2 replaces with `drcguard.accept`. Design: docs/FLOW-REDESIGN.md sections 2.4-2.7 and 2.12.
"""
import glob
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter

from . import audit, calllog, constraints as C, critroute, drcfull, drcguard, joingaps, krtchain, versions

KIND_OF_TYPE = {"shorting_items": "short", "solder_mask_bridge": "short",
                "clearance": "clearance", "hole_clearance": "clearance", "hole_to_hole": "clearance",
                "track_dangling": "dangling", "via_dangling": "dangling",
                "track_not_centered_on_via": "via-offcentre"}
ORDER = ["short", "open-gap", "open-pour", "open-free", "clearance", "dangling", "via-offcentre"]
CLASS_RANK = {"hs-pair": 0, "rf": 1, "power": 2, "pair": 3, "plain": 4}
_NET = re.compile(r"\[([^\]]*)\]")


def measure(board, kicad_cli="kicad-cli"):
    """The plain drcguard.measure result (callers set m["gaps"] themselves)."""
    return drcguard.measure(board, kicad_cli)


def _nets_of(items):
    return sorted({n for it in items for n in _NET.findall(str(it.get("description") or ""))})


def _pos(it):
    p = it.get("pos")
    return (p["x"], p["y"]) if isinstance(p, dict) and "x" in p and "y" in p else None


def _class(nets, rf, pairs):
    if any(n in rf for n in nets):
        return "rf"
    if any(n in pairs for n in nets):
        return "pair"
    return "plain"


def classify(d, cons=None):
    """DRC dict -> sorted gap list. Gap: id, kind, class, nets, at, layer, evidence."""
    cons = cons or {}
    rf = set((cons.get("rf") or {}).get("nets") or [])
    unc = d.get("unconnected_items") or []
    vs = d.get("violations") or []
    names = set()
    for x in unc + vs:
        names.update(_nets_of(x.get("items") or []))
    try:
        pairs = set(krtchain.pair_nets(cons, sorted(names), None))
    except Exception:
        pairs = set()
    gaps, seen = [], Counter()

    def add(base_id, g):
        seen[base_id] += 1
        g["id"] = base_id if seen[base_id] == 1 else f"{base_id}#{seen[base_id]}"
        gaps.append(g)

    for u in unc:
        items = u.get("items") or []
        descs = [str(i.get("description") or "") for i in items]
        nets = _nets_of(items)
        # open-sealed would need closure.run; phase 1 leaves it out, so the open-free rungs end with the sealed ones
        if any(drcguard._ZONE_DESC.match(x) for x in descs):
            kind = "open-pour"
        elif len(items) >= 2 and _pos(items[0]) and _pos(items[1]) \
                and math.hypot(_pos(items[0])[0] - _pos(items[1])[0],
                               _pos(items[0])[1] - _pos(items[1])[1]) <= joingaps.GAP:
            kind = "open-gap"
        else:
            kind = "open-free"
        a, b = (sorted(drcguard._DECIMAL.sub("#", x) for x in descs[:2]) + ["", ""])[:2]
        base_id = f"{kind}:{drcfull.net_of(u) or ''}:{a}:{b}"
        p0 = _pos(items[0]) if items else None
        add(base_id, {"kind": kind, "class": _class(nets, rf, pairs), "nets": nets,
                      "at": [round(p0[0], 2), round(p0[1], 2)] if p0 else None, "layer": None,
                      "evidence": str(u.get("description") or "")[:120]})
    for v in vs:
        items = v.get("items") or []
        kind = KIND_OF_TYPE.get(v.get("type"), "other")
        nets = _nets_of(items)
        p0 = _pos(items[0]) if items else None
        add(kind + ":" + json.dumps(drcguard.key_of(v)),
            {"kind": kind, "class": _class(nets, rf, pairs), "nets": nets,
             "at": [round(p0[0], 2), round(p0[1], 2)] if p0 else None, "layer": None,
             "evidence": str(v.get("description") or "")[:120]})

    def key(g):
        k = ORDER.index(g["kind"]) if g["kind"] in ORDER else len(ORDER)
        return (k, CLASS_RANK[g["class"]], g["id"])

    gaps.sort(key=key)
    return gaps


def score(m, gaps):
    """R4 vector: [hs_pair, rf, shorts, opens, pair, errors, quality]. hs_pair is 0 until phase 3."""
    def n(pred):
        return sum(1 for g in gaps if pred(g))
    return [0, n(lambda g: g["class"] == "rf"), m["shorts"], m["opens"],
            n(lambda g: g["class"] == "pair"), m["errors"],
            n(lambda g: g["kind"] in ("dangling", "via-offcentre", "other"))]


_SNAP = {}


def _snapshot(path):
    st = os.stat(path)
    key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    if key not in _SNAP:
        _SNAP[key] = audit.snapshot(path)
        while len(_SNAP) > 4:
            _SNAP.pop(next(iter(_SNAP)))
    return _SNAP[key]


def _accept(base_m, base_board, cand_m, cand_board, target_ids, cand_gaps, kind=None, protect=()):
    """Repair guard: drcguard.accept (mode repair). base_m["gaps"] supplies the base score (R4)."""
    cand_m["gap_ids"] = {g["id"] for g in cand_gaps}
    return drcguard.accept(base_m, cand_m, "repair", target_ids, kind, base_board, cand_board,
                           score=lambda m: score(m, cand_gaps if m is cand_m else (m.get("gaps") or [])),
                           snap=_snapshot, protect=protect)


# ---- rungs: fn(base, out, gaps, ctx) -> brief summary dict; writes `out` only when it has a candidate ----

def _rm(p):
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.lexists(p):
        os.remove(p)


def _copy(src, dst):
    shutil.copy2(src, dst + ".tmp")
    os.replace(dst + ".tmp", dst)


def _sidecars(src, dst):
    s, d = os.path.splitext(src)[0], os.path.splitext(dst)[0]
    for ext in (".kicad_pro", ".kicad_dru"):
        if os.path.exists(s + ext):
            _copy(s + ext, d + ext)


def _brief(r):
    """At most 6 scalar keys, no paths."""
    if not isinstance(r, dict):
        return {}
    out = {}
    for k, v in r.items():
        if len(out) >= 6:
            break
        if isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 40 and "/" not in v
                                                  and "\\" not in v):
            out[k] = v
    return out


def _nets(gaps):
    return sorted({n for g in gaps for n in g["nets"]})


def _clean_board(p):
    """Remove a board file, its sidecars and any p-stem.* / p.* leftovers."""
    stem = os.path.splitext(p)[0]
    for q in set(glob.glob(glob.escape(stem) + ".*") + glob.glob(glob.escape(p) + ".*") + [p]):
        _rm(q)


def _track_len_by_layer(board):
    """{layer name: total track + arc length, mm} over the board's tracks (vias excluded)."""
    import pcbnew
    b = pcbnew.LoadBoard(board)
    out = {}
    for t in b.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            continue
        name = b.GetLayerName(t.GetLayer())
        out[name] = out.get(name, 0.0) + t.GetLength() / 1e6
    return out


def copper_layers(board):
    import pcbnew
    b = pcbnew.LoadBoard(board)
    return [b.GetLayerName(lid) for lid in b.GetEnabledLayers().CuStack()]


def signal_layers(board):
    """Copper layers that carry at least 5 % of the board's track length. A plane layer carries almost none,
    and this rule does not depend on zones: boards whose layers are all full pours still resolve. If no layer
    passes (no tracks, or too many layers to pass), every copper layer counts as signal."""
    cu = copper_layers(board)
    lens = _track_len_by_layer(board)
    total = sum(lens.values())
    if total <= 0:
        return cu
    sig = [n for n in cu if lens.get(n, 0.0) >= 0.05 * total]
    return sig or cu


def _r_joingaps(base, out, gaps, ctx):
    from . import joingaps as J
    return _brief(J.run(base, out, nets=_nets(gaps)))


def _r_pourisland(base, out, gaps, ctx):
    from . import pourisland as P
    cur, wrote, tmps = base, False, []
    try:
        for i, net in enumerate(_nets(gaps)):
            tmp = os.path.join(ctx["work"], f"pi-{i}.kicad_pcb")
            tmps.append(tmp)
            _rm(tmp)
            _sidecars(cur, tmp)
            P.repair(cur, tmp, net, constraints=ctx["cons_path"])
            if os.path.exists(tmp):
                cur, wrote = tmp, True
        if wrote:
            _copy(cur, out)
            _sidecars(cur, out)
    finally:
        for t in tmps:
            _clean_board(t)
    return {"nets": len(_nets(gaps)), "wrote": wrote}


def _r_patch(base, out, gaps, ctx):
    from . import patch as PT
    return _brief(PT.patch_board(base, out, rip=False, cons=ctx["cons"], log=ctx["log"]))


def _r_patch_rip(base, out, gaps, ctx):
    from . import patch as PT
    return _brief(PT.patch_board(base, out, rip=True, cons=ctx["cons"], log=ctx["log"]))


def _r_handrepair(base, out, gaps, ctx):
    from . import handroute as HR, handrepair as HP
    w = os.path.join(ctx["work"], "hr-work.kicad_pcb")
    try:
        layers = signal_layers(base)
        if not layers:
            return {"status": "no signal layers"}
        _clean_board(w)
        HR.session_begin(base, w, ctx["kicad_cli"])
        r = HP.repair(w, out, constraints=ctx["cons_path"], budget=min(900, ctx["budget_s"]), layers=layers)
    finally:
        _clean_board(w)
    return _brief(r)


def _r_region(base, out, gaps, ctx):
    from . import repairregion as RR
    planes = tuple(sorted(set(copper_layers(base)) - set(signal_layers(base))))
    return _brief(RR.repair(base, out, nets=[re.escape(n) for n in _nets(gaps)], planes=planes,
                            constraints=ctx["cons_path"], kicad_cli=ctx["kicad_cli"], log=ctx["log"]))


def _krt_cmd(base, out, nets, ctx, layers=None):
    """The KRT route command for `nets` (the pipeline's route command, the proven router). None when KRT is not found."""
    import types
    from . import routerloc as RL, routerules as RR, pipeline as P, krtchain as KC
    try:
        rl = RL.resolve()
    except RuntimeError:
        return None
    R = RR.rules(ctx["cons"], base, default=RR.DEFAULT_SIZE)
    a = types.SimpleNamespace(router_py=rl["py"], router_dir=rl["dir"], layers=layers or signal_layers(base),
                              track=None, clearance=None)
    rflags = RR.flags(R, rl["dir"], ctx["work"])
    return P._route_cmd(a, base, out, [KC.glob_escape(n) for n in nets], R=R, rflags=rflags)


def _r_krt(base, out, gaps, ctx):
    """KRT single-net re-route (the proven router) on the open nets. Passes --json-out; ctx["krt_last"] names the summary."""
    cmd = _krt_cmd(base, out, _nets(gaps), ctx)
    if cmd is None:
        return {"status": "krt not found"}
    js = out + ".krt.json"
    i = cmd.index("--nets")
    cmd[i:i] = ["--json-out", js]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=min(900, ctx["budget_s"]),
                              cwd=ctx["work"])
    except subprocess.TimeoutExpired:
        _rm(out)
        return {"status": "krt timeout"}
    res = {"status": "ran", "rc": proc.returncode, "failed": 0, "open": 0}
    if os.path.exists(js):
        ctx["krt_last"] = js
        try:
            with open(js) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            d = {}
        d = d if isinstance(d, dict) else {}
        res["failed"] = len(d.get("failed_single") or [])
        res["open"] = len(d.get("open_single") or [])
    return res


def _r_drcfix(base, out, gaps, ctx):
    from . import drcfix as DF
    return _brief(DF.loop(base, out, kicad_cli=ctx["kicad_cli"], cons=ctx["cons"], log=ctx["log"]))


def _r_launder(base, out, gaps, ctx):
    from . import launder as LA
    return _brief(LA.launder_board(base, out, kicad_cli=ctx["kicad_cli"], dangling=True, log=ctx["log"]))


def _r_snapiter(base, out, gaps, ctx):
    from . import snapiter as SI
    return _brief(SI.run(base, out, log=ctx["log"]))


PACKET_BYTES = 4096
AI_BUDGET = 2                                   # action files applied per gap id per work dir
_HEX_ID = re.compile(r"^[0-9a-fA-F]{8,}$")
_PACKET_ACTIONS = ("rip {net, ids}; nudge {ref, dx, dy, rot}; route {nets, layers}; give_up {why}. "
                   "No copper coordinates.")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _ai_action(x, copper):
    """The validated action, or None (dropped)."""
    if not isinstance(x, dict):
        return None
    do = x.get("do")
    if do == "rip":
        ids = x.get("ids")
        if (isinstance(x.get("net"), str) and x["net"] and isinstance(ids, list) and 0 < len(ids) <= 12
                and all(isinstance(i, str) and _HEX_ID.match(i) for i in ids)):
            return {"do": "rip", "net": x["net"], "ids": list(ids)}
    elif do == "nudge":
        if (isinstance(x.get("ref"), str) and x["ref"] and all(_num(x.get(k)) for k in ("dx", "dy", "rot"))
                and abs(x["dx"]) <= 1.0 and abs(x["dy"]) <= 1.0):
            return {"do": "nudge", "ref": x["ref"], "dx": x["dx"], "dy": x["dy"], "rot": x["rot"]}
    elif do == "route":
        nets, layers = x.get("nets"), x.get("layers")
        if (isinstance(nets, list) and 0 < len(nets) <= 6 and all(isinstance(n, str) and n for n in nets)
                and (layers is None or (isinstance(layers, list) and all(l in copper for l in layers)))):
            return {"do": "route", "nets": list(nets), "layers": list(layers) if layers else None}
    elif do == "give_up":
        return {"do": "give_up", "why": str(x.get("why") or "")[:200]}
    return None


def load_ai_action(path, base):
    """{"actions", "tokens", "why"} from an AI action file. Invalid actions are dropped with a log line. Never raises."""
    out = {"actions": [], "tokens": 0, "why": ""}
    try:
        with open(path) as fh:
            a = json.load(fh)
        if not isinstance(a, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        print(f"ai: unreadable action file {os.path.basename(path)}: {e}", file=sys.stderr)
        return out
    try:
        copper = signal_layers(base)   # routes go on signal layers only (planes stay planes)
    except Exception:
        copper = []
    if _num(a.get("tokens")):
        out["tokens"] = int(a["tokens"])
    out["why"] = str(a.get("why") or "")[:200]
    for x in a.get("actions") if isinstance(a.get("actions"), list) else []:
        v = _ai_action(x, copper)
        if v is None:
            print(f"ai: dropped action {str(x)[:120]}", file=sys.stderr)
        else:
            out["actions"].append(v)
    return out


def _xy(p):
    return (p[0], p[1]) if isinstance(p, (list, tuple)) and len(p) >= 2 else None


def _near(items, mid, pts, n=20):
    """The `n` items nearest `mid` (distance of their points named by `pts`), slimmed and rounded to 0.01."""
    def dist(it):
        d = [math.hypot(q[0] - mid[0], q[1] - mid[1]) for q in (_xy(it.get(k)) for k in pts) if q]
        return min(d) if d else float("inf")
    out = []
    for it in sorted(items, key=dist)[:n]:
        s = {k: it.get(k) for k in ("id", "net", "layer", "locked")}
        for k in ("a", "b", "pos"):
            q = _xy(it.get(k))
            if q:
                s[k] = [round(q[0], 2), round(q[1], 2)]
        out.append(s)
    return out


def _on_net(e, net):
    if not net:
        return False
    return e.get("net") == net if isinstance(e, dict) else net in str(e)


def _clip_packet(p):
    """Drop krt, then halve the window's tracks and vias until the packet fits PACKET_BYTES."""
    def size():
        return len(json.dumps(p).encode())
    if size() > PACKET_BYTES:
        p["krt"] = {"failed": [], "open": []}
    while size() > PACKET_BYTES and p["window"] and (p["window"]["tracks"] or p["window"]["vias"]):
        for k in ("tracks", "vias"):
            p["window"][k] = p["window"][k][: len(p["window"][k]) // 2]
    return p


def gap_packet(base, gap, ctx):
    """Repair packet for one gap (<= PACKET_BYTES): its first open connection of the net, the copper window around
    it, the obstacles that seal it, the KRT failures on the net, and the attempts already made on this gap."""
    from . import handroute as HR
    cp = ctx.get("cons_path")
    net = gap.get("net") or (gap.get("nets") or [None])[0]
    i = None
    conns = []
    if net:
        conns = HR.open_nets(base, constraints=cp)["connections"]
        i = next((k for k, r in enumerate(conns) if r.get("net") == net), None)
    conn = win = None
    sealed = []
    if i is not None:
        r = conns[i]
        ends = lambda p: {k: p.get(k) for k in ("x", "y", "layer", "pin", "id")}
        conn = {"i": i, "a": ends(r["a"]), "b": ends(r["b"]), "straight_mm": r.get("straight_mm"),
                "rules": r.get("rules")}
        W = HR.window(base, conn=i, constraints=cp, margin=2.0)
        mid = ((r["a"]["x"] + r["b"]["x"]) / 2, (r["a"]["y"] + r["b"]["y"]) / 2)
        win = {"box": W.get("window"), "layers": signal_layers(base),
               "tracks": _near(W.get("tracks") or [], mid, ("a", "b")),
               "vias": _near(W.get("vias") or [], mid, ("pos",)),
               "via_sites": [[round(s[0], 2), round(s[1], 2)]
                             for s in ((W.get("via_sites") or {}).get("sites") or [])[:10]]}
        lim = (HR.suggest(base, net, conn=i, constraints=cp).get("sealed_by") or {}).get("limiting_obstacles") or []
        sealed = [{k: e.get(k) for k in ("id", "net", "layer", "locked", "border_cells")} for e in lim[:8]]
    krt = {"failed": [], "open": []}
    js = ctx.get("krt_last")
    if js and os.path.exists(js):
        try:
            with open(js) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            d = {}
        d = d if isinstance(d, dict) else {}
        for key, dst in (("failed_single", "failed"), ("open_single", "open")):
            krt[dst] = [e for e in (d.get(key) or []) if _on_net(e, net)][:5]
    tried = []
    try:
        with open(ctx.get("attempts")) as fh:
            rows = fh.readlines()
    except (OSError, TypeError):
        rows = []
    for ln in rows:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict) and (rec.get("gap") or {}).get("id") == gap["id"]:
            tried.append({"method": rec.get("method"), "result": rec.get("result"),
                          "why": str(rec.get("why") or "")[:120]})
    p = {"v": 1, "gap": {"id": gap["id"], "kind": gap["kind"], "class": gap.get("class"), "net": net},
         "conn": conn, "window": win, "sealed_by": sealed, "krt": krt, "tried": tried[-6:],
         "actions": _PACKET_ACTIONS}
    return _clip_packet(p)


def _route_net(w, net, layers, ctx):
    """KRT re-route of `net` on the session board, then suggest + lay for each of its open connections."""
    from . import handroute as HR
    dst = w + ".krt.kicad_pcb"
    _clean_board(dst)
    cmd = _krt_cmd(w, dst, [net], ctx, layers)
    if cmd is not None:
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=min(900, ctx["budget_s"]), cwd=ctx["work"])
        except subprocess.TimeoutExpired:
            pass
        if os.path.exists(dst):
            shutil.copy(dst, w)
    key = lambda r: (r["a"]["x"], r["a"]["y"], r["b"]["x"], r["b"]["y"])
    tried = set()
    for _ in range(50):
        conns = HR.open_nets(w)["connections"]
        todo = [(i, r) for i, r in enumerate(conns) if r.get("net") == net and key(r) not in tried]
        if not todo:
            break
        i, r = todo[0]
        tried.add(key(r))
        sug = HR.suggest(w, net, conn=i, layers=layers)
        if sug.get("found") and sug.get("path"):
            HR.lay(w, net, sug["path"], constraints=ctx["cons_path"])


def _apply_ai(base, out, a, ctx, sid):
    """One transaction on copies: nudges, then a session with rips and routes, then session_commit (the guard).
    Returns (status, commit, why). Temps are removed in every case."""
    from . import handroute as HR, routediag as RD, legality as LGL, routable as RB, boardwrite as BW
    cons, cp, ai_dir = ctx["cons"], ctx["cons_path"], ctx["ai_dir"]
    acts = a["actions"]
    give = next((x for x in acts if x["do"] == "give_up"), None)
    if give:
        return "give_up", False, give["why"]
    cur = base
    w = os.path.join(ai_dir, sid + ".work.kicad_pcb")
    tmps = [w]
    try:
        fixed = set(RB._fixed_set(RD.load(base), cons))
        for i, x in enumerate(v for v in acts if v["do"] == "nudge"):
            part = RD.load(cur)["parts"].get(x["ref"])
            if part is None or len(part["pads"]) > 3 or x["ref"] in fixed or part.get("locked"):
                print(f"ai: nudge {x['ref']} refused (fixed, locked, or more than 3 pads)", file=sys.stderr)
                continue
            px, py = part["pos"]
            tmp = os.path.join(ai_dir, f"{sid}.nudge{i}.kicad_pcb")
            tmps.append(tmp)
            _clean_board(tmp)
            _sidecars(cur, tmp)
            if not BW.move_file(cur, tmp, {x["ref"]: (px + x["dx"], py + x["dy"], part.get("angle", 0) + x["rot"])}):
                continue
            if LGL.Legal(RD.load(tmp), cons).part_issues(x["ref"]):
                print(f"ai: nudge {x['ref']} refused (courtyard or outline issue)", file=sys.stderr)
                continue
            cur = tmp
        _clean_board(w)
        _sidecars(cur, w)
        HR.session_begin(cur, w)
        for x in acts:
            if x["do"] == "rip":
                HR.shove(w, x["net"], ids=x["ids"], op="unlay", constraints=cp)
        for x in acts:
            if x["do"] == "route":
                for net in x["nets"]:
                    _route_net(w, net, x["layers"] or signal_layers(w), ctx)
        r = HR.session_commit(w, out, constraints=cp)
        return "applied", bool(r.get("accepted")), str((r.get("reasons") or [""])[0])
    finally:
        for t in tmps:
            _clean_board(t)


def ai_key(gap):
    """Stable key of a gap for AI packet/action files and the AI budget: kind and first net."""
    return f"{gap['kind']}:{(gap.get('nets') or ['?'])[0]}"


def _r_ai(base, out, gaps, ctx):
    """AI rung on the first gap. No action file: write the gap packet and stop (status packet). An action file:
    apply it as one transaction; close guards the board like any other rung."""
    if ctx.get("ai_mode") == "off":
        return {"status": "ai off"}
    gap = gaps[0]
    used = ctx["ai_used"]
    key = ai_key(gap)          # kind + net: DRC lists an open's two items in either order, so the full id is not stable
    if used.get(key, 0) >= AI_BUDGET:
        return {"status": "ai budget"}
    sid = critroute._safe(key)[:80]
    os.makedirs(ctx["ai_dir"], exist_ok=True)
    act = os.path.join(ctx["ai_dir"], sid + ".action.json")
    if not os.path.exists(act):
        with open(os.path.join(ctx["ai_dir"], sid + ".packet.json"), "w") as fh:
            fh.write(json.dumps(gap_packet(base, gap, ctx)))
        return {"status": "packet"}
    a = load_ai_action(act, base)
    os.replace(act, os.path.join(ctx["ai_dir"], sid + ".action.used.json"))
    used[key] = used.get(key, 0) + 1
    status, commit, why = _apply_ai(base, out, a, ctx, sid)
    return {"status": status, "commit": commit, "why": why, "tokens": a["tokens"]}


RUNGS = {"joingaps": _r_joingaps, "pourisland": _r_pourisland, "patch": _r_patch, "patch_rip": _r_patch_rip,
         "handrepair": _r_handrepair, "krt": _r_krt, "region": _r_region, "drcfix": _r_drcfix, "launder": _r_launder,
         "snapiter": _r_snapiter, "ai": _r_ai}

# pour-tie, planetie, pair and RF rungs join in phase 3 (FLOW-REDESIGN 5)
DISPATCH = {
    "short": ["drcfix"],
    "open-gap": ["joingaps", "patch"],
    "open-pour": ["pourisland", "region", "ai"],
    "open-free": ["patch", "handrepair", "krt", "region", "patch_rip", "ai"],
    "clearance": ["drcfix", "region"],
    "dangling": ["launder"],
    "via-offcentre": ["snapiter"],
}
LEVEL = {"region": "L1", "patch_rip": "L1", "ai": "L1", "krt": "L0"}  # every other rung is L0


def _tool():
    try:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _write_json(path, obj):
    with open(path + ".tmp", "w") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(path + ".tmp", path)


def _vitals(m):
    return {"opens": m["opens"], "shorts": m["shorts"], "errors": m["errors"], "score": score(m, m["gaps"])}


def _cons_hash(path):
    if not path or not os.path.exists(path):
        return ""
    return hashlib.sha1(open(path, "rb").read()).hexdigest()


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def close(board, work, cons_path=None, cons=None, kicad_cli="kicad-cli", budget_min=None, max_rounds=40,
          attempts=None, runs=None, log=print):
    """Gap loop on a copy of `board` in `work`. Returns the stop dict plus "best"; writes work/best.kicad_pcb,
    work/gaps.json, work/stop.json, work/ledger-rows.md."""
    os.makedirs(work, exist_ok=True)
    best = os.path.join(work, "best.kicad_pcb")
    _copy(board, best)
    _sidecars(board, best)
    cons = cons or C.load(cons_path)
    budget_min = budget_min or (cons.get("flow") or {}).get("budget_min", 120)
    explicit = attempts is not None
    attempts = attempts or calllog.attempts_path(board)
    run_id = "r-" + time.strftime("%Y%m%d-%H%M%S")
    tool = _tool()
    cons_hash = _cons_hash(cons_path)
    t0 = time.time()
    budget_s = budget_min * 60
    m = dict(measure(best, kicad_cli))
    m["gaps"] = classify(m["drc"], cons)
    protect = drcguard.protected_nets(cons, drcguard.snap_nets(_snapshot(best)))
    start = _vitals(m)
    board_fp0 = versions.copper_fingerprint(best)
    if runs is None and calllog.enabled():
        runs = os.path.join(_repo_root(), "docs", "corpus", "runs.jsonl")
    tries = []

    state_path = os.path.join(work, "gaps.json")
    memo = set()
    ai_used = {}
    try:
        with open(state_path) as fh:
            st = json.load(fh)
        ai_used = {k: int(v) for k, v in (st.get("ai_used") or {}).items()}
        if st.get("tool") == tool and st.get("cons_hash") == cons_hash:
            memo = {tuple(x) for x in st.get("memo", [])}
    except (OSError, ValueError, AttributeError):
        pass

    def save(r):
        _write_json(state_path, {"gaps": m["gaps"], "memo": sorted(list(k) for k in memo), "round": r,
                                 "tool": tool, "cons_hash": cons_hash, "ai_used": ai_used})

    stop, level, n_att, last = None, "L0", 0, {}
    n_packets, krt_state = 0, {}
    ai_mode = (cons.get("ai") or {}).get("mode", "packet")

    def attempt(r, kind, rung, targets):
        """One try. Returns True when the candidate was accepted (m is then the new base)."""
        nonlocal m, n_att, level, n_packets
        nets = _nets(targets)
        fp = versions.copper_fingerprint(best)
        key = (kind, rung, fp)
        vec0, t_try = score(m, m["gaps"]), time.time()
        out = os.path.join(work, "close", f"r{r}-{kind}-{rung}.kicad_pcb")
        after, runs, why, summ = None, 0, "", {}
        if key in memo:
            result, why = "skipped", "memo"
        else:
            try:
                os.makedirs(os.path.dirname(out), exist_ok=True)
                _clean_board(out)
                _sidecars(best, out)
                ctx = {"cons": cons, "cons_path": cons_path, "kicad_cli": kicad_cli, "work": work,
                       "budget_s": max(0.0, budget_s - (time.time() - t0)), "log": log, "attempts": attempts,
                       "ai_dir": os.path.join(work, "ai"), "ai_used": ai_used, "ai_mode": ai_mode,
                       "krt_last": krt_state.get("last")}
                summ = RUNGS[rung](best, out, targets, ctx) or {}
                if ctx.get("krt_last"):
                    krt_state["last"] = ctx["krt_last"]
                if not os.path.exists(out) and summ.get("status") == "packet":
                    result, why = "packet", "action file pending"
                elif not os.path.exists(out):
                    result = "refused"
                    why_ai = summ.get("why") if rung == "ai" else None
                    why = str(why_ai or summ.get("status") or summ.get("reason") or "no board written")[:120]
                elif versions.copper_fingerprint(out) == fp:
                    result, why = "refused", "no change"
                else:
                    cm = dict(measure(out, kicad_cli))
                    runs = 1
                    cm["gaps"] = classify(cm["drc"], cons)
                    ok, why = _accept(m, best, cm, out, [g["id"] for g in targets], cm["gaps"], kind, protect)
                    if ok:
                        _copy(out, best)
                        m = cm
                        _sidecars(out, best)
                        cur_ids = {g["id"] for g in m["gaps"]}
                        result = "closed" if all(g["id"] not in cur_ids for g in targets) else "partial"
                        after = score(m, m["gaps"])
                        why = json.dumps(summ)[:120]
                        if rung in LEVEL:
                            level = max(level, LEVEL[rung])
                    else:
                        result = "refused"
            except (Exception, SystemExit) as e:
                result, why = "error", f"{type(e).__name__}: {e}"[:120]
            finally:
                _clean_board(out)
        if result == "packet":
            n_packets += 1
        if result == "refused":
            memo.add(key)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "run": run_id,
               "board_fp": fp[:8] if len(fp) > 8 else fp, "tool": tool, "stage": "close",
               "level": LEVEL.get(rung, "L0"),
               "gap": {"id": targets[0]["id"], "kind": kind, "class": targets[0]["class"], "n": len(targets)},
               "method": rung, "tier": {"scope": "board", "effort": 1}, "in": {"nets": len(nets)},
               "result": result, "why": why, "score": [vec0, after], "s": round(time.time() - t_try, 1),
               "drc_runs": runs, "precheck": None,
               "ai_tokens": int(summ.get("tokens") or 0) if rung == "ai" else 0}
        if explicit or calllog.enabled():
            calllog.append(rec, attempts)
        tries.append({"result": result, "method": rung, "kind": kind, "why": why, "s": rec["s"]})
        n_att += 1
        last[kind] = {"method": rung, "why": why}
        return result in ("closed", "partial")

    for r in range(max_rounds):
        if not any(g["kind"] in DISPATCH for g in m["gaps"]):
            stop = "clean"
            break
        hit = False
        packets_before = n_packets
        for kind in ORDER:
            targets = [g for g in m["gaps"] if g["kind"] == kind]
            if not targets or kind not in DISPATCH:
                continue
            for rung in DISPATCH[kind]:
                if time.time() - t0 > budget_s:
                    stop = "budget"
                    break
                accepted = attempt(r, kind, rung, targets)
                save(r)
                if accepted:
                    hit = True
                    break
            if stop:
                break
        if stop:
            break
        if not hit:
            stop = "packets" if n_packets > packets_before else "flat"
            break
    else:
        stop = "rounds"

    left = []
    for g in m["gaps"]:
        l = last.get(g["kind"], {})
        left.append({"id": g["id"], "kind": g["kind"], "class": g["class"],
                     "last_method": l.get("method"), "why": l.get("why")})
    res = {"stop": stop, "exit": {"clean": 0, "packets": 5}.get(stop, 2), "level": level,
           "minutes": round((time.time() - t0) / 60, 1), "start": start, "end": _vitals(m),
           "attempts": n_att, "left": left}
    _write_json(os.path.join(work, "stop.json"), res)
    if runs:
        ok = [t for t in tries if t["result"] in ("closed", "partial")]
        refused = Counter(t["why"] for t in tries if t["result"] == "refused")
        methods = Counter(t["method"] for t in ok)
        os.makedirs(os.path.dirname(runs), exist_ok=True)
        calllog.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "run": run_id, "tool": tool,
                        "board": os.path.basename(board), "board_fp": board_fp0[:8] if len(board_fp0) > 8
                        else board_fp0, "stage": "close", "variant": None, "start": start,
                        "end": _vitals(m), "stop": stop, "minutes": res["minutes"], "attempts": n_att,
                        "closed": len(ok), "methods_closed": dict(methods),
                        "top_refusals": [[w, n] for w, n in refused.most_common(3)],
                        "full_pass": m["opens"] == 0 and m["shorts"] == 0 and m["errors"] == 0,
                        "ai_tokens": 0}, runs)
    date, rows = time.strftime("%Y-%m-%d"), []
    for kind in ORDER + ["other"]:
        n = sum(1 for g in m["gaps"] if g["kind"] == kind)
        if n:
            why = last.get(kind, {}).get("why") or ("not dispatched" if kind not in DISPATCH else "not tried")
            rows.append(f"| {date} | close | {kind} x{n} left | {why} | open |")
    with open(os.path.join(work, "ledger-rows.md"), "w") as fh:
        fh.write("\n".join(rows) + ("\n" if rows else ""))
    return dict(res, best=best)


def report(paths, runs=None):
    """Rows per (method, kind) from attempts.jsonl files. close% = (closed + partial) over tries that were not
    skipped; skipped tries are counted but do not enter the denominator. s/close = seconds of the non-skipped
    tries per success. top-why = most frequent why among non-skipped tries."""
    cells = {}
    for path in paths:
        with open(path) as fh:
            for line in fh:
                if not line.strip():
                    continue
                r = json.loads(line)
                cell = cells.setdefault((r["method"], r["gap"]["kind"]), {"r": Counter(), "s": 0.0, "why": Counter()})
                cell["r"][r["result"]] += 1
                if r["result"] != "skipped":
                    cell["s"] += float(r.get("s") or 0)
                    cell["why"][str(r.get("why") or "")[:40]] += 1
    rows = []
    for (method, kind), c in cells.items():
        r = c["r"]
        tried = sum(r.values()) - r["skipped"]
        won = r["closed"] + r["partial"]
        rows.append({"method": method, "kind": kind, "tries": sum(r.values()), "closed": r["closed"],
                     "partial": r["partial"], "refused": r["refused"], "error": r["error"],
                     "skipped": r["skipped"], "close_pct": round(100.0 * won / tried, 1) if tried else 0.0,
                     "s_per_close": round(c["s"] / won, 1) if won else None,
                     "top_why": c["why"].most_common(1)[0][0] if c["why"] else ""})
    rows.sort(key=lambda x: (-x["close_pct"], -x["tries"], x["method"], x["kind"]))
    return rows


def runs_summary(path):
    """(runs, full passes) from a runs.jsonl file."""
    n = k = 0
    with open(path) as fh:
        for line in fh:
            if line.strip():
                n += 1
                k += bool(json.loads(line).get("full_pass"))
    return n, k


def print_report(rows, runs=None):
    head = f"{'method':<12}{'kind':<14}{'tries':>6}{'closed':>8}{'partial':>8}{'refused':>8}{'error':>6}" \
           f"{'skipped':>8}{'close%':>8}{'s/close':>9}  top-why"
    print(head)
    for x in rows:
        sc = "-" if x["s_per_close"] is None else f"{x['s_per_close']:.1f}"
        print(f"{x['method']:<12}{x['kind']:<14}{x['tries']:>6}{x['closed']:>8}{x['partial']:>8}"
              f"{x['refused']:>8}{x['error']:>6}{x['skipped']:>8}{x['close_pct']:>7.1f}%{sc:>9}  {x['top_why'][:40]}")
    if runs:
        n, k = runs_summary(runs)
        print(f"runs: {n}, full passes: {k}")


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m fluxplace.gaps")
    ap.add_argument("board", nargs="?")
    ap.add_argument("--work")
    ap.add_argument("--report", nargs="+", metavar="ATTEMPTS")
    ap.add_argument("--runs", metavar="RUNS_JSONL")
    ap.add_argument("--constraints")
    ap.add_argument("--classify-only", action="store_true")
    ap.add_argument("--kicad-cli", default="kicad-cli")
    a = ap.parse_args(argv)
    t0 = time.time()
    if a.report:
        print_report(report(a.report, a.runs), a.runs)
        return 0
    if not a.board or not a.work:
        ap.error("board and --work are required unless --report is given")
    if a.classify_only:
        gaps = classify(measure(a.board, a.kicad_cli)["drc"], C.load(a.constraints))
        for (k, c), n in sorted(Counter((g["kind"], g["class"]) for g in gaps).items()):
            print(f"{k} {c} {n}")
        os.makedirs(a.work, exist_ok=True)
        _write_json(os.path.join(a.work, "gaps.json"), {"gaps": gaps})
        print(f"seconds {time.time() - t0:.1f}")
        return 0
    res = close(a.board, a.work, cons_path=a.constraints, kicad_cli=a.kicad_cli, log=lambda m: None)
    print(f"close: opens {res['start']['opens']}->{res['end']['opens']}, shorts {res['start']['shorts']}->"
          f"{res['end']['shorts']}, errors {res['start']['errors']}->{res['end']['errors']}, "
          f"attempts {res['attempts']}, stop {res['stop']}, {res['minutes']} min, best {res['best']}")
    return res["exit"]


if __name__ == "__main__":
    sys.exit(_main())
