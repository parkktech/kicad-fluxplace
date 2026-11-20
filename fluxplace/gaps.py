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
MASK_POLY = ("Polygon on F.Mask", "Polygon on B.Mask")
MASK_OPENING_EVIDENCE = "board-level mask opening exposes copper of several nets: cut mask openings after routing"
ORDER = ["short", "mask-opening", "power-width", "power-vias", "pair-open", "pair-asym", "pair-skew", "open-gap",
         "open-pour", "open-free", "clearance", "dangling", "via-offcentre", "pin-escape"]
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


def _pair_audit(board, cons, opn=None):
    """(B, cls, rows) of the routing pair audit, built as review._pairs_from_audit builds it. `opn`: open-net set
    (classify passes the DRC unconnected set); None -> routeconn.open_by_net."""
    from . import routediag as RD, netclass as NC, qualaudit as QA, routeconn as RC
    B = RD.load(board)
    cls = NC.classify(B, cons or {})
    if opn is None:
        try:
            opn = set(RC.open_by_net(board))
        except Exception:
            opn = set(RC.text_open(B))
    return B, cls, QA.pair_rows(B, cls, cons or {}, opn)


SEV = {"pair-open": 3, "pair-asym": 2, "pair-skew": 1}     # severity of an incomplete pair (score sums it)


def review_gaps(board, cons, opn=None):
    """Review findings as gap rows (classify's shape, plus "base" = id base the caller assigns) -> (rows, failed).
    `failed` names the sources that raised (power, pairs, pin-escape): their rows are missing, never clean.
    `opn`: open nets; pin-escape grades only their pads (None -> every pad)."""
    from . import power, boardfile, review, preroute, routediag
    cons = cons or {}
    not_p = C.not_pairs(cons)
    out, failed = [], []
    try:
        for f in power.check(boardfile.facts_from_file(board), cons):
            if f.get("code") not in ("POWER_WIDTH", "POWER_VIAS"):
                continue
            net = (f.get("refs") or [""])[0]
            kind = "power-width" if f["code"] == "POWER_WIDTH" else "power-vias"
            out.append({"base": f"{kind}:{net}", "kind": kind, "class": "power", "nets": [net], "at": None,
                        "layer": None, "evidence": str(f.get("msg") or "")[:120], "refs": list(f.get("refs") or []),
                        "mag": round(float(f.get("value") or 0))})
    except Exception:
        failed.append("power")
    B, rows = None, []
    try:
        B, _cls, rows = _pair_audit(board, cons, opn)
    except Exception:
        failed.append("pairs")
    for r in rows:
        p, n, st, causes = r["p"], r["n"], r["status"], r.get("causes") or []
        if p in not_p or n in not_p:
            continue
        if st in ("half", "open"):
            kind = "pair-open"
        elif st == "mismatch" and any(c in ("layers", "vias") for c in causes):
            kind = "pair-asym"
        elif st == "mismatch" and causes == ["skew"]:
            kind = "pair-skew"
        else:
            continue                         # ok / unknown rows are not graded
        out.append({"base": f"pair:{p}/{n}", "kind": kind, "sev": SEV[kind],
                    "class": "hs-pair" if review.pair_gated(cons, p) else "pair", "nets": [p, n], "at": None,
                    "layer": None, "evidence": str(r.get("why") or "")[:120], "refs": [p, n]})
    partners = {x: y for r in rows for x, y in ((r["p"], r["n"]), (r["n"], r["p"]))}
    try:
        for f in preroute.pin_escape_blocked(B or routediag.load(board), only_nets=opn, partners=partners):
            if f.get("code") != "PIN_ESCAPE_BLOCKED":
                continue
            ref = (f.get("refs") or [""])[0]
            out.append({"base": f"pin-escape:{ref}", "kind": "pin-escape", "class": "plain", "nets": [], "at": None,
                        "layer": None, "evidence": str(f.get("msg") or "")[:120], "refs": list(f.get("refs") or [])})
    except Exception:
        failed.append("pin-escape")
    return out, failed


def _protect(board, cons):
    """R5 set: rf nets and every pair leg (drcguard.protected_nets of the board's nets)."""
    return drcguard.protected_nets(cons, drcguard.snap_nets(_snapshot(board)))


def relax_nets(gaps):
    """Legs of every incomplete pair (pair-open/asym/skew gap): the only pair legs an R5 check may relax."""
    return {n for g in gaps if g["kind"] in ("pair-open", "pair-asym", "pair-skew") for n in g["nets"]}


def _r5_set(kind, targets, protect, relax):
    """R5 set handed to the guard: a pair-kind try may relax the legs of its own target pairs; other kinds keep all."""
    if kind is None or not kind.startswith("pair-"):
        return protect
    return protect - (relax & set(_nets(targets)))


def _classify_into(mm, cons, board):
    """Set mm["gaps"] (with board review gaps) and mm["review_failed"] (the review sources that raised)."""
    failed = []
    mm["gaps"] = classify(mm["drc"], cons, board=board, failed_out=failed)
    mm["review_failed"] = failed


def classify(d, cons=None, board=None, failed_out=None):
    """DRC dict -> sorted gap list. Gap: id, kind, class, nets, at, layer, evidence.
    `board`: the board the DRC dict was measured on; when given, review gaps are added too (failed_out, a list,
    receives the names of the review sources that raised)."""
    cons = cons or {}
    rf = set((cons.get("rf") or {}).get("nets") or [])
    unc = d.get("unconnected_items") or []
    vs = d.get("violations") or []
    opn = set().union(*(_nets_of(u.get("items") or []) for u in unc)) if unc else set()
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
        evidence = str(v.get("description") or "")[:120]
        if v.get("type") == "solder_mask_bridge" and any(
                str(i.get("description") or "").startswith(MASK_POLY) for i in items):
            kind, evidence = "mask-opening", MASK_OPENING_EVIDENCE
        nets = _nets_of(items)
        p0 = _pos(items[0]) if items else None
        add(kind + ":" + json.dumps(drcguard.key_of(v)),
            {"kind": kind, "class": _class(nets, rf, pairs), "nets": nets,
             "at": [round(p0[0], 2), round(p0[1], 2)] if p0 else None, "layer": None,
             "evidence": evidence})

    def key(g):
        k = ORDER.index(g["kind"]) if g["kind"] in ORDER else len(ORDER)
        return (k, CLASS_RANK[g["class"]], g["id"])

    if board is not None:
        rg, failed = review_gaps(board, cons, opn=opn)
        if failed_out is not None:
            failed_out.extend(failed)
        for g in rg:
            add(g.pop("base"), g)
    gaps.sort(key=key)
    return gaps


def score(m, gaps):
    """Vector: [hs_pair, rf, shorts, opens, power, power_mag, pair, pin_escape, errors, quality] (R4 plus D71cj a terms).
    hs_pair / pair: SUM of sev over pair-open|asym|skew gaps of a gated / plain pair; power: power-width|power-vias
    count; power_mag: their summed mag (narrow mm, missing vias); rf: any rf gap."""
    def n(pred):
        return sum(1 for g in gaps if pred(g))

    def sev(pred):
        return sum(g.get("sev", 0) for g in gaps if pred(g))
    return [sev(lambda g: g["class"] == "hs-pair" and g["kind"].startswith("pair-")),
            n(lambda g: g["class"] == "rf"), m["shorts"], m["opens"],
            n(lambda g: g["kind"] in ("power-width", "power-vias")),
            sum(g.get("mag", 0) for g in gaps if g["kind"] in ("power-width", "power-vias")),
            sev(lambda g: g["class"] == "pair" and g["kind"].startswith("pair-")),
            n(lambda g: g["kind"] == "pin-escape"), m["errors"],
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


# R2 cleanup: a candidate refused only for new mechanical DRC keys is cleaned by these rungs (in this order)
CLEANUP = {"via-offcentre": "snapiter", "dangling": "launder"}


def _new_kinds(base_m, cand_m, kind):
    """Kinds of every DRC key type the candidate adds over the base (drcguard.accept's R2 set, all keys, not the first)."""
    new = drcguard.new_keys(base_m.get("keys", base_m.get("err_keys")), cand_m.get("keys", cand_m.get("err_keys")))
    if kind is not None:
        new = Counter({k: v for k, v in new.items() if KIND_OF_TYPE.get(k[0]) != kind})
    return {KIND_OF_TYPE.get(k[0], "other") for k in new}


def _cleanup_rungs(base_m, cand_m, kind):
    """Cleanup rung names for an R2 refusal, [] unless every new key is a mechanical kind."""
    kinds = _new_kinds(base_m, cand_m, kind)
    if not kinds or not kinds <= set(CLEANUP):
        return []
    return [CLEANUP[k] for k in CLEANUP if k in kinds]


def _accept(base_m, base_board, cand_m, cand_board, target_ids, cand_gaps, kind=None, protect=()):
    """Repair guard: drcguard.accept (mode repair). base_m["gaps"] supplies the base score (R4).
    A candidate that failed a review source the base did not is refused; a pair target whose sev dropped counts as
    progress (it is removed from the target list)."""
    new_failed = sorted(set(cand_m.get("review_failed") or []) - set(base_m.get("review_failed") or []))
    if new_failed:
        return False, ("review source failed: " + ", ".join(new_failed))[:120]
    base_sev = {g["id"]: g["sev"] for g in base_m.get("gaps") or [] if "sev" in g}
    cand_sev = {g["id"]: g["sev"] for g in cand_gaps if "sev" in g}
    target_ids = [t for t in target_ids if not (t in base_sev and t in cand_sev and cand_sev[t] < base_sev[t])]
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


KRT_WINDOW_MM = 1.5                             # margin of the gap window whose nets KRT may rip and re-route
KRT_MAX = 8                                     # gaps per _r_krt call


def _krt_open(board, cp):
    """Names of the nets with an open connection on `board`."""
    from . import handroute as HR
    return {r.get("net") for r in HR.open_nets(board, constraints=cp)["connections"]}


def _krt_rip(board, net, cp, skip):
    """Nets with unlocked copper inside the window of `net`'s first open connection, `skip` removed. None: not open."""
    from . import handroute as HR
    conns = HR.open_nets(board, constraints=cp)["connections"]
    i = next((n for n, r in enumerate(conns) if r.get("net") == net), None)
    if i is None:
        return None
    W = HR.window(board, conn=i, constraints=cp, margin=KRT_WINDOW_MM)
    return sorted({t.get("net") for t in (W.get("tracks") or []) + (W.get("vias") or [])
                   if t.get("net") and not t.get("locked")} - set(skip))


def _zone_nets(board):
    try:
        import pcbnew
        return {z.GetNetname() for z in pcbnew.LoadBoard(board).Zones() if z.GetNetname()}
    except Exception:
        return set()


def _r_krt(base, out, gaps, ctx):
    """KRT (the proven router) on the open nets, one net per call, each call on the board the previous one left.
    The nets with unlocked copper inside the gap window (zone nets and ctx["protect"] excluded) are passed as
    `--rip-existing-nets`: KRT may rip them when they block the target and re-routes or restores each one itself
    (custody), and its improvement gate reverts a run that opens another net. A net's result is kept only when the
    net is closed and no other net has opened. Measured on W14: SIM_RST closed with its 4 window nets as candidates;
    with every net as a candidate a 47 mm net was ripped and lost; without candidates the same net was `no change`.
    Passes --json-out; ctx["krt_last"] names the last summary."""
    from . import krtchain as KC
    cp, protect = ctx.get("cons_path"), set(ctx.get("protect") or ())
    if _krt_cmd(base, out, ["x"], ctx) is None:
        return {"status": "krt not found"}
    targets = [n for n in dict.fromkeys(_nets(gaps)) if n not in protect][:KRT_MAX]   # a pair leg is never routed alone
    if not targets:
        return {"status": "no plain net"}
    skip = protect | _zone_nets(base) | set(targets)
    res = {"status": "ran", "rc": 0, "failed": 0, "open": 0, "closed": 0, "ripped": 0}
    cur, tmps, t0 = base, [], time.time()
    try:
        for k, net in enumerate(targets):
            if time.time() - t0 > ctx["budget_s"]:            # out of budget: what is closed so far is written
                res["status"] = "budget"
                break
            before = _krt_open(cur, cp)
            rip = _krt_rip(cur, net, cp, skip)
            if rip is None:
                continue
            nxt, js = f"{out}.n{k}.kicad_pcb", f"{out}.krt{k}.json"     # js outlives the temp board (AI packet)
            tmps.append(nxt)
            _clean_board(nxt)
            _sidecars(cur, nxt)
            cmd = _krt_cmd(cur, nxt, [net], ctx)
            at = cmd.index("--nets")
            cmd[at:at] = ["--json-out", js] + (["--rip-existing-nets", *[KC.glob_escape(n) for n in rip]] if rip else [])
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=min(900, ctx["budget_s"]),
                                      cwd=ctx["work"])
            except subprocess.TimeoutExpired:
                res["status"] = "krt timeout"
                break
            res["rc"] = proc.returncode
            if os.path.exists(js):
                ctx["krt_last"] = js
                try:
                    with open(js) as fh:
                        d = json.load(fh)
                except (OSError, ValueError):
                    d = {}
                d = d if isinstance(d, dict) else {}
                res["failed"] += len(d.get("failed_single") or [])
                res["open"] += len(d.get("open_single") or [])
                res["ripped"] += len(d.get("preexisting_rips") or [])
            if not os.path.exists(nxt):
                continue
            after = _krt_open(nxt, cp)
            if net in after or (after - before):
                continue                                      # not closed, or a trade: the previous board stands
            cur = nxt
            res["closed"] += 1
        if cur != base:
            _copy(cur, out)
            _sidecars(cur, out)
    finally:
        for t in tmps:
            _clean_board(t)
    return res


def _r_drcfix(base, out, gaps, ctx):
    from . import drcfix as DF
    return _brief(DF.loop(base, out, kicad_cli=ctx["kicad_cli"], cons=ctx["cons"], log=ctx["log"]))


def _r_launder(base, out, gaps, ctx):
    from . import launder as LA
    return _brief(LA.launder_board(base, out, kicad_cli=ctx["kicad_cli"], dangling=True, log=ctx["log"]))


def _r_prune(base, out, gaps, ctx):
    """Delete copper that connects nothing (launder.dead_copper: padless islands and free-end stubs, from KiCad's
    connectivity). An open whose far side is a padless island closes; the stubs it removes free room for the routing
    rungs that follow."""
    from . import launder as LA
    r = LA.prune_board(base, out, set(ctx.get("protect") or ()))
    return {"status": "pruned" if r["removed"] else "nothing dead", "removed": r["removed"], "nets": len(r["nets"])}


def _r_snapiter(base, out, gaps, ctx):
    from . import snapiter as SI
    return _brief(SI.run(base, out, log=ctx["log"]))


PACKET_BYTES = 4096
AI_BUDGET = 2                                   # action files applied per gap id per work dir
_HEX_ID = re.compile(r"^[0-9a-fA-F]{8,}$")
_PACKET_ACTIONS = ("rip {net, ids}; rip_locked {net, ids} (plain locked copper only; pairs/rf refused); "
                   "nudge {ref, dx, dy, rot}; route {nets, layers}; give_up {why}. No copper coordinates.")
AI_PACKETS = 6                                  # packets written per _r_ai call


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
    elif do == "rip_locked":
        ids = x.get("ids")
        if (isinstance(x.get("net"), str) and x["net"] and isinstance(ids, list) and 0 < len(ids) <= 12
                and all(isinstance(i, str) and _HEX_ID.match(i) for i in ids)):
            return {"do": "rip_locked", "net": x["net"], "ids": list(ids)}
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


def load_ai_action(path, base, protect=()):
    """{"actions", "tokens", "why"} from an AI action file. Invalid actions are dropped with a log line. Never raises.
    `protect` (drcguard.protected_nets: rf nets and pair legs) may not be rip_locked."""
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
        if v is not None and v["do"] == "rip_locked" and v["net"] in protect:
            v = None
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
            elif x["do"] == "rip_locked":
                # session-only opt-in: needs explicit ids, no window, no out (handroute.shove)
                HR.shove(w, x["net"], ids=x["ids"], op="unlay", constraints=cp, allow_locked=True)
        routes = [dict(x, nets=list(x["nets"])) for x in acts if x["do"] == "route"]
        routed = {n for r in routes for n in r["nets"]}
        ripped = [n for n in dict.fromkeys(x["net"] for x in acts if x["do"] == "rip_locked") if n not in routed]
        if ripped:                                  # a locked rip must be re-routed in this transaction
            if routes:
                routes[-1]["nets"] += ripped
            else:
                routes.append({"do": "route", "nets": ripped, "layers": None})
        for r in routes:
            for net in r["nets"]:
                _route_net(w, net, r["layers"] or signal_layers(w), ctx)
        r = HR.session_commit(w, out, constraints=cp)
        return "applied", bool(r.get("accepted")), str((r.get("reasons") or [""])[0])
    finally:
        for t in tmps:
            _clean_board(t)


def ctx_dir_ai(work):
    return os.path.join(work, "ai")


def _ai_pending(ai_dir, gaps):
    """True when an unapplied action file exists for one of `gaps`."""
    return any(os.path.exists(os.path.join(ai_dir, critroute._safe(ai_key(g))[:80] + ".action.json")) for g in gaps)


def ai_key(gap):
    """Stable key of a gap for AI packet/action files and the AI budget: kind and first net."""
    return f"{gap['kind']}:{(gap.get('nets') or ['?'])[0]}"


def _r_ai(base, out, gaps, ctx):
    """AI rung over the gaps of one kind. The first gap (list order) with an action file and budget left is applied
    as one transaction (status applied, gap_id names it). Otherwise a packet is written for each gap with budget left
    (at most AI_PACKETS per call): status packet, n = count. close guards the board like any other rung."""
    if ctx.get("ai_mode") == "off":
        return {"status": "ai off"}
    used = ctx["ai_used"]
    os.makedirs(ctx["ai_dir"], exist_ok=True)
    todo, seen = [], set()
    for g in gaps:                 # one entry per AI key: DRC can list two gaps of one net under one key
        key = ai_key(g)
        if key not in seen and used.get(key, 0) < AI_BUDGET:
            seen.add(key)
            todo.append((g, key, critroute._safe(key)[:80]))
    if not todo:
        return {"status": "ai budget"}
    for gap, key, sid in todo:
        act = os.path.join(ctx["ai_dir"], sid + ".action.json")
        if not os.path.exists(act):
            continue
        a = load_ai_action(act, base, ctx.get("protect", ()))
        os.replace(act, os.path.join(ctx["ai_dir"], sid + ".action.used.json"))
        used[key] = used.get(key, 0) + 1
        status, commit, why = _apply_ai(base, out, a, ctx, sid)
        return {"status": status, "commit": commit, "why": why, "tokens": a["tokens"], "gap_id": gap["id"]}
    n = 0
    for gap, key, sid in todo[:AI_PACKETS]:
        with open(os.path.join(ctx["ai_dir"], sid + ".packet.json"), "w") as fh:
            fh.write(json.dumps(gap_packet(base, gap, ctx)))
        n += 1
    return {"status": "packet", "n": n}


def _rip_window(src, dst, box, nets):
    """Copy src to dst without the unlocked tracks and vias of `nets` lying in box [x0, y0, x1, y1] (mm). A track
    counts when both ends are inside, a via when its centre is. Returns the number of items removed."""
    import pcbnew
    b = pcbnew.LoadBoard(src)
    x0, y0, x1, y1 = box
    inside = lambda p: x0 <= pcbnew.ToMM(p.x) <= x1 and y0 <= pcbnew.ToMM(p.y) <= y1
    n = 0
    for t in list(b.GetTracks()):
        if t.GetNetname() not in nets or t.IsLocked():
            continue
        if t.GetClass() == "PCB_VIA":
            hit = inside(t.GetPosition())
        else:
            hit = inside(t.GetStart()) and inside(t.GetEnd())
        if hit:
            b.Delete(t)
            n += 1
    pcbnew.SaveBoard(dst, b)
    _sidecars(src, dst)
    return n


KRT_REGION_MAX = 6                      # gaps per krt_region call
KRT_REGION_MARGINS = (1.5, 3.0)         # window margin (mm) tried in order


def _r_krt_region(base, out, gaps, ctx):
    """Rip the unlocked, unprotected copper around each open gap (one pcbnew pass per window on copies), then run the
    KRT re-route with the gap nets plus the ripped nets on the cleared board. The 3.0 mm window is tried when the 1.5 mm
    candidate still leaves a gap open. Output = KRT's board; close measures and guards it."""
    from . import handroute as HR
    cp, protect = ctx.get("cons_path"), set(ctx.get("protect") or ())
    targets = []
    for g in gaps[:KRT_REGION_MAX]:
        net = (g.get("nets") or [None])[0]
        if net:
            targets.append(net)
    if not targets:
        return {"status": "no net"}
    _sidecars(base, out)
    rip = out + ".rip.kicad_pcb"
    last = {"status": "no conn"}
    for margin in KRT_REGION_MARGINS:
        boxes, ripped = [], set()
        for net in targets:
            conns = HR.open_nets(base, constraints=cp)["connections"]
            i = next((k for k, r in enumerate(conns) if r.get("net") == net), None)
            if i is None:
                continue
            W = HR.window(base, conn=i, constraints=cp, margin=margin)
            boxes.append(W["window"])
            for t in (W.get("tracks") or []) + (W.get("vias") or []):
                n = t.get("net")
                if n and not t.get("locked") and n not in protect and n not in targets:
                    ripped.add(n)
        if not boxes:
            return {"status": "no conn"}
        nets = set(targets) | ripped
        cur = base
        try:
            for k, box in enumerate(boxes):
                nxt = f"{out}.ch{k}.kicad_pcb"
                _clean_board(nxt)
                _rip_window(cur, nxt, box, nets)
                if cur != base:
                    _clean_board(cur)
                cur = nxt
            _clean_board(rip)
            if cur != rip:
                shutil.copy(cur, rip)
                _sidecars(cur, rip)
            cmd = _krt_cmd(rip, out, sorted(nets), ctx, signal_layers(base))
            if cmd is None:
                return {"status": "krt not found"}
            _rm(out)
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=min(900, ctx["budget_s"]), cwd=ctx["work"])
            except subprocess.TimeoutExpired:
                _rm(out)
                return {"status": "krt timeout", "margin": margin}
        finally:
            _clean_board(rip)
            for k in range(len(boxes)):
                _clean_board(f"{out}.ch{k}.kicad_pcb")
        last = {"status": "ran", "nets": len(ripped), "margin": margin}
        if not os.path.exists(out):
            last = {"status": "krt no output", "margin": margin}
            continue
        still = {r.get("net") for r in HR.open_nets(out, constraints=cp)["connections"]}
        if not (set(targets) & still):
            break
    return last


RUNGS = {"joingaps": _r_joingaps, "pourisland": _r_pourisland, "patch": _r_patch, "patch_rip": _r_patch_rip,
         "handrepair": _r_handrepair, "krt": _r_krt, "region": _r_region, "drcfix": _r_drcfix, "launder": _r_launder,
         "snapiter": _r_snapiter, "krt_region": _r_krt_region, "ai": _r_ai, "prune": _r_prune}

# pour-tie, planetie, pair and RF rungs join in phase 3 (FLOW-REDESIGN 5)
DISPATCH = {
    "short": ["drcfix"],
    "open-gap": ["joingaps", "patch"],
    "open-pour": ["pourisland", "region", "ai"],
    "open-free": ["prune", "patch", "handrepair", "krt", "krt_region", "region", "patch_rip", "ai"],
    "clearance": ["drcfix", "region"],
    "dangling": ["prune", "launder"],
    "via-offcentre": ["snapiter"],
}
LEVEL = {"region": "L1", "patch_rip": "L1", "ai": "L1", "krt_region": "L1", "krt": "L0"}  # every other rung is L0


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


# ---- out-of-process rungs: a segfault in pcbnew must not kill the close run ----

_CTX_JSON_KEYS = ("cons_path", "kicad_cli", "work", "budget_s", "krt_last", "ai_mode", "ai_dir")
JOB_OUT_KRT = "_krt_last"


def _call_rung(name, base, out, gaps, ctx):
    """Run one rung. `ai` and ctx["inproc"] runs stay in-process; every other rung runs in a child process
    (`python -m fluxplace.gaps --rung-job JOB`) so a native crash is a RuntimeError, not a dead close run."""
    if name == "ai" or ctx.get("inproc"):
        return RUNGS[name](base, out, gaps, ctx)
    job, jout = out + ".job.json", out + ".job.out.json"
    try:
        with open(job, "w") as fh:
            json.dump({"name": name, "base": base, "out": out, "gaps": gaps,
                       "ctx": {k: ctx.get(k) for k in _CTX_JSON_KEYS}}, fh, default=str)
        try:
            p = subprocess.run([sys.executable, "-m", "fluxplace.gaps", "--rung-job", job], capture_output=True,
                               text=True, timeout=ctx["budget_s"] + 120, cwd=_repo_root())
        except subprocess.TimeoutExpired:
            raise RuntimeError("rung timeout")
        if not os.path.exists(jout):
            if p.returncode < 0:
                raise RuntimeError(f"rung crashed (signal {-p.returncode})")
            if p.returncode > 0:
                raise RuntimeError(f"rung exit {p.returncode}")
            raise RuntimeError("rung wrote no result")
        with open(jout) as fh:
            res = json.load(fh)
        krt = res.pop(JOB_OUT_KRT, None)
        if krt:
            ctx["krt_last"] = krt
        return res
    finally:
        _rm(job)
        _rm(jout)


def _rung_job(job_path):
    """Child entry (`--rung-job`): run one rung from its job file and write the summary to JOB.out.json."""
    with open(job_path) as fh:
        j = json.load(fh)
    cj = j["ctx"]
    cons = C.load(cj.get("cons_path")) if cj.get("cons_path") else {}
    ctx = dict(cj, cons=cons, log=lambda m: print(m, file=sys.stderr), inproc=True)
    summ = RUNGS[j["name"]](j["base"], j["out"], j["gaps"], ctx) or {}
    summ = dict(summ, **{JOB_OUT_KRT: ctx.get("krt_last")})
    with open(j["out"] + ".job.out.json", "w") as fh:
        json.dump(summ, fh, default=str)
    return 0


def close(board, work, cons_path=None, cons=None, kicad_cli="kicad-cli", budget_min=None, max_rounds=40,
          attempts=None, runs=None, log=print, inproc=False):
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
    _classify_into(m, cons, best)
    protect = _protect(best, cons)
    relax = relax_nets(m["gaps"])
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
        nonlocal m, n_att, level, n_packets, protect, relax
        nets = _nets(targets)
        fp = versions.copper_fingerprint(best)
        key = (kind, rung, fp)
        vec0, t_try = score(m, m["gaps"]), time.time()
        out = os.path.join(work, "close", f"r{r}-{kind}-{rung}.kicad_pcb")
        after, runs, why, summ, cleanup = None, 0, "", {}, []
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
                       "krt_last": krt_state.get("last"), "protect": protect}
                ctx["inproc"] = inproc
                summ = _call_rung(rung, best, out, targets, ctx) or {}
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
                    _classify_into(cm, cons, out)
                    final = out
                    ok, why = _accept(m, best, cm, out, [g["id"] for g in targets], cm["gaps"], kind,
                                      _r5_set(kind, targets, protect, relax))
                    names = _cleanup_rungs(m, cm, kind) if (not ok and why.startswith("R2 ")) else []
                    if names:
                        # cleanup: one pass per rung on the candidate; a rung that writes nothing keeps the file
                        for name in names:
                            nxt = f"{out}.{name}.kicad_pcb"
                            _clean_board(nxt)
                            _sidecars(final, nxt)
                            try:
                                _call_rung(name, final, nxt, [], ctx)
                            except Exception:
                                pass
                            cleanup.append(name)
                            if os.path.exists(nxt):
                                final = nxt
                        cm = dict(measure(final, kicad_cli))
                        runs += 1
                        _classify_into(cm, cons, final)
                        ok, why = _accept(m, best, cm, final, [g["id"] for g in targets], cm["gaps"], kind,
                                          _r5_set(kind, targets, protect, relax))
                    if ok:
                        _copy(final, best)
                        m = cm
                        protect = _protect(best, cons)
                        relax = relax_nets(m["gaps"])
                        _sidecars(final, best)
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
               "drc_runs": runs, "precheck": None, "cleanup": cleanup,
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
                extra = len(targets) if rung == "ai" else 0   # every pending AI action gets its try this round
                while (extra and not accepted and _ai_pending(ctx_dir_ai(work), targets)
                       and time.time() - t0 <= budget_s):
                    extra -= 1
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
    ap.add_argument("--rung-job", help=argparse.SUPPRESS)
    ap.add_argument("board", nargs="?")
    ap.add_argument("--work")
    ap.add_argument("--report", nargs="+", metavar="ATTEMPTS")
    ap.add_argument("--runs", metavar="RUNS_JSONL")
    ap.add_argument("--constraints")
    ap.add_argument("--classify-only", action="store_true")
    ap.add_argument("--kicad-cli", default="kicad-cli")
    a = ap.parse_args(argv)
    if a.rung_job:
        return _rung_job(a.rung_job)
    t0 = time.time()
    if a.report:
        print_report(report(a.report, a.runs), a.runs)
        return 0
    if not a.board or not a.work:
        ap.error("board and --work are required unless --report is given")
    if a.classify_only:
        gaps = classify(measure(a.board, a.kicad_cli)["drc"], C.load(a.constraints), board=a.board)
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
