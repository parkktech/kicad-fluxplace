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
from fluxplace import procs as _procs
import sys
import time
from collections import Counter

from . import audit, calllog, constraints as C, critroute, drcfull, drcguard, joingaps, krtchain, versions
from . import events as EV

KIND_OF_TYPE = {"shorting_items": "short", "solder_mask_bridge": "short",
                "clearance": "clearance", "hole_clearance": "clearance", "hole_to_hole": "clearance",
                "track_dangling": "dangling", "via_dangling": "dangling",
                "track_not_centered_on_via": "via-offcentre"}
MASK_POLY = ("Polygon on F.Mask", "Polygon on B.Mask")
MASK_OPENING_EVIDENCE = "board-level mask opening exposes copper of several nets: cut mask openings after routing"
ORDER = ["short", "power-width", "power-vias", "pair-open", "pair-asym", "pair-unmeasured", "pair-skew", "pair-stub", "pair-impedance", "open-gap",
         "open-pour", "open-free", "mask-opening", "clearance", "dangling", "via-offcentre", "pin-escape"]   # PHASE3I B6: mask after copper
CLASS_RANK = {"hs-pair": 0, "rf": 1, "power": 2, "pair": 3, "plain": 4}
_NET = re.compile(r"\[([^\]]*)\]")
# a path on a high-speed pair leg is allowed only as a stub join this short; the R6 gate still refuses any hs_pair worsening
HS_STUB_MM = 2.0


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


SEV = {"pair-open": 3, "pair-unmeasured": 3, "pair-asym": 2, "pair-skew": 1, "pair-stub": 1, "pair-impedance": 1}     # severity of an incomplete pair (score sums it)


def impedance_off_nets(board, cons):
    """net names with a PAIR_IMPEDANCE_OFF finding (routed copper of a pair off its target); empty set on any exception."""
    from . import review, boardfile
    try:
        facts = boardfile.facts_from_file(board)
        return {(f.get("refs") or [""])[0] for f in review.check_pair_geometry(facts, cons) if f.get("code") == "PAIR_IMPEDANCE_OFF"}
    except Exception:
        return set()


def review_gaps(board, cons, opn=None):
    """Review findings as gap rows (classify's shape, plus "base" = id base the caller assigns) -> (rows, failed).
    `failed` names the sources that raised (power, pairs, pin-escape): their rows are missing, never clean.
    `opn`: open nets; pin-escape grades only their pads (None -> every pad)."""
    from . import power, boardfile, review, preroute, routediag
    cons = cons or {}
    not_p = C.not_pairs(cons)
    out, failed = [], []
    try:
        for f in power.check(boardfile.facts_from_file(board), cons, power.dcdrop_status(board, cons), board=board):
            if f.get("code") not in ("POWER_WIDTH", "POWER_VIAS"):
                continue
            net = (f.get("refs") or [""])[0]
            kind = "power-width" if f["code"] == "POWER_WIDTH" else "power-vias"
            out.append({"base": f"{kind}:{net}", "kind": kind, "class": "power", "nets": [net], "at": None,
                        "layer": None, "evidence": str(f.get("msg") or "")[:120], "refs": list(f.get("refs") or []),
                        "mag": round(float(f.get("value") or 0), 2) if kind == "power-width" else round(float(f.get("value") or 0))})
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
        elif st == "mismatch" and "stub" in causes:
            kind = "pair-stub"               # a spur past stub_max_mm (alone or with skew): a gap, never silently clean
        elif st == "mismatch" and causes == ["skew"]:
            kind = "pair-skew"
        elif st == "unknown":
            kind = "pair-unmeasured"         # D71cx: an unmeasured pair (kicad-cli failed, MEASURE_DISAGREE, PAIR_DELAY_UNKNOWN) cannot pass
        else:
            continue                         # complete rows are not gaps
        out.append({"base": f"pair:{p}/{n}", "kind": kind, "sev": SEV[kind],
                    "class": "hs-pair" if review.pair_gated(cons, p) else "pair", "nets": [p, n], "at": None,
                    "layer": None, "evidence": str(r.get("why") or "")[:120], "refs": [p, n],
                    "skew": r.get("skew"), "limit": r.get("limit")})
    partners = {x: y for r in rows for x, y in ((r["p"], r["n"]), (r["n"], r["p"]))}
    try:
        from . import boardfile as BF, graph
        facts = BF.facts_from_file(board)
        mates = graph.diff_pairs(sorted(set(facts.get("net_tracks") or {}) | set(partners)), exclude=not_p)
        for f in review.check_pair_geometry(facts, cons):
            if f.get("code") != "PAIR_IMPEDANCE_OFF":
                continue
            net = (f.get("refs") or [""])[0]
            other = next((a for a, b in mates.items() if b == net), None) or next((b for a, b in mates.items() if a == net), None)
            nets = sorted({net, other} - {None})
            out.append({"base": f"pair-impedance:{net}", "kind": "pair-impedance", "sev": SEV["pair-impedance"],
                        "class": "hs-pair" if review.pair_gated(cons, nets[0]) else "pair", "nets": nets, "at": None,
                        "layer": None, "evidence": str(f.get("msg") or "")[:120], "refs": nets})
    except Exception:
        failed.append("pair-impedance")
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
    return {n for g in gaps if g["kind"] in PAIR_KINDS for n in g["nets"]}


def _release_nets(kind, targets, man):
    """Protected nets an accepted pair rung may release (an explicit, logged act): the nets of its target pair gaps. A pair-skew gap
    whose skew does not pass its maximum (D71cx: KiCad length / ps budget is the verdict) is no target and releases nothing."""
    if not (kind or "").startswith("pair-"):
        return set()
    out = set()
    for g in targets:
        if g["kind"] == "pair-skew" and not _skew_fails(g):
            continue
        out |= set(g.get("nets") or [])
    return out & set((man or {}).get("nets") or {})


def _skew_fails(g):
    """True when the pair-skew gap's measured skew is past its maximum (gap limit = the maximum); unknown numbers count as failing."""
    sk, lim = g.get("skew"), g.get("limit")
    return sk is None or lim is None or sk > lim


def _r5_set(kind, targets, protect, relax, fallback=()):
    """R5 set handed to the guard: a pair-kind try may relax the legs of its own target pairs; other kinds keep all.
    PHASE3F A5: an open-free try may route the fallback pair legs among its targets (they are not protected then)."""
    if kind == "open-free":
        return protect - (set(fallback) & set(_nets(targets)))
    if kind is None or not kind.startswith("pair-"):
        return protect
    return protect - (relax & set(_nets(targets)))


# PHASE3I B1: stage gates. Gap kinds that must be zero before the next constructive stage runs. Pair kinds count only
# for gated (high-speed) pairs; low-speed pairs are left to the router. [flow.gates] in constraints overrides per stage.
PAIR_KINDS = ("pair-open", "pair-asym", "pair-unmeasured", "pair-skew", "pair-stub", "pair-impedance")
GATE_DEFAULTS = {"pairs": ["pair-open", "pair-asym", "pair-unmeasured", "pair-skew", "pair-stub", "pair-impedance"],
                 "route": ["power-width", "power-vias"]}          # PHASE3I F2: no `power` stage exists; rails are laid by `route`


# Gap kinds a LATER stage of the flow owns: a gate that ends with only these left does not stop the flow (verdict warn, stop
# "deferred", the rows kept in state and stop.json); review stays the final hard gate. A kind is deferred only when one of its
# owner stages is still to run in this flow, so there is never an exit 5 without a driver.
GATE_DEFER = {"pair-skew": ("tune",), "pair-unmeasured": ("tune",), "pair-stub": ("tune",), "pair-impedance": ("tune",),
              "pair-open": ("critroute", "route", "close"), "pair-asym": ("route", "close")}


def deferred_left(left, later_stages):
    """The rows of `left` (gap rows) when every one is owned by a stage in `later_stages`, else None (nothing is deferred:
    shorts, powers and any kind without an owner keep the gate closed)."""
    later = set(later_stages or ())
    if left and all(any(o in later for o in GATE_DEFER.get(g["kind"], ())) for g in left):
        return list(left)
    return None


def gate_kinds(cons, stage):
    """Kinds gating `stage` ([] = no gate). cons [flow.gates] = {stage: [kind, ...]} overrides the defaults."""
    over = ((cons or {}).get("flow") or {}).get("gates") or {}
    kinds = over[stage] if stage in over else GATE_DEFAULTS.get(stage, [])
    return list(kinds or [])


def in_scope(g, kinds):
    """A gap is in a gate's scope when its kind is listed; pair kinds only for gated (hs-pair) pairs."""
    if kinds is None or g["kind"] not in kinds:
        return kinds is None
    return g["kind"] not in PAIR_KINDS or g.get("class") == "hs-pair"


def _classify_into(mm, cons, board):
    """Set mm["gaps"] (with board review gaps) and mm["review_failed"] (the review sources that raised)."""
    failed = []
    mm["gaps"] = classify(mm["drc"], cons, board=board, failed_out=failed)
    mm["review_failed"] = failed


def cluster_regions(gaps, pad_mm=2.0):
    """PHASE3I B2: open gaps -> regions with disjoint nets and non-touching boxes. A gap joins a region when it shares a
    net with it, or its point (padded by pad_mm) touches the region's box; merged boxes grow until nothing touches.
    -> [{"gaps": [id], "nets": [net], "box": [x0, y0, x1, y1] | None}]."""
    def touch(a, b):
        return a is not None and b is not None and a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]
    regs = []
    for g in gaps:
        at = g.get("at")
        box = [at[0] - pad_mm, at[1] - pad_mm, at[0] + pad_mm, at[1] + pad_mm] if at else None
        regs.append({"gaps": [g["id"]], "nets": set(g.get("nets") or []), "box": box})
    merged = True
    while merged:
        merged = False
        for i in range(len(regs)):
            for j in range(i + 1, len(regs)):
                a, b = regs[i], regs[j]
                if a["nets"] & b["nets"] or touch(a["box"], b["box"]):
                    a["gaps"] += b["gaps"]
                    a["nets"] |= b["nets"]
                    if b["box"] is not None:
                        a["box"] = b["box"] if a["box"] is None else [min(a["box"][0], b["box"][0]), min(a["box"][1], b["box"][1]),
                                                                       max(a["box"][2], b["box"][2]), max(a["box"][3], b["box"][3])]
                    del regs[j]
                    merged = True
                    break
            if merged:
                break
    return [{"gaps": r["gaps"], "nets": sorted(r["nets"]), "box": r["box"]} for r in regs]


def _scope_gaps(mm, kinds):
    """PHASE3I B1: keep only the gaps of a gate's scope (kinds None = every gap)."""
    if kinds is not None:
        mm["gaps"] = [g for g in mm["gaps"] if in_scope(g, kinds)]


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


def score_terms(m, gaps):
    """Named score terms, in score() order: hs_pair, rf, shorts, opens, power, power_mag, pair, pin_escape, errors, quality.
    hs_pair / pair: SUM of sev over pair-open|asym|skew gaps of a gated / plain pair; power: power-width|power-vias
    count; power_mag: their summed mag (narrow mm, missing vias); rf: any rf gap."""
    def n(pred):
        return sum(1 for g in gaps if pred(g))

    def sev(pred):
        return sum(g.get("sev", 0) for g in gaps if pred(g))
    return {"hs_pair": sev(lambda g: g["class"] == "hs-pair" and g["kind"].startswith("pair-")),
            "rf": n(lambda g: g["class"] == "rf"), "shorts": m["shorts"], "opens": m["opens"],
            "power": n(lambda g: g["kind"] in ("power-width", "power-vias")),
            "power_mag": sum(g.get("mag", 0) for g in gaps if g["kind"] in ("power-width", "power-vias")),
            "pair": sev(lambda g: g["class"] == "pair" and g["kind"].startswith("pair-")),
            "pin_escape": n(lambda g: g["kind"] == "pin-escape"), "errors": m["errors"],
            "quality": n(lambda g: g["kind"] in ("dangling", "via-offcentre", "other"))}


def score(m, gaps):
    """Lexicographic vector: score_terms() values in order (R4)."""
    return list(score_terms(m, gaps).values())


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


R6_TERMS = ("hs_pair", "rf", "power")


def _accept(base_m, base_board, cand_m, cand_board, target_ids, cand_gaps, kind=None, protect=()):
    """Repair guard: drcguard.accept (mode repair). base_m["gaps"] supplies the base score (R4).
    A candidate that failed a review source the base did not is refused; a pair target whose sev dropped counts as
    progress (it is removed from the target list)."""
    new_failed = sorted(set(cand_m.get("review_failed") or []) - set(base_m.get("review_failed") or []))
    if new_failed:
        return False, ("review source failed: " + ", ".join(new_failed))[:120]
    sb, sc = score_terms(base_m, base_m.get("gaps") or []), score_terms(cand_m, cand_gaps)
    for t in R6_TERMS:                                  # never-worse: score() is lexicographic, so no trade on these
        if sc[t] > sb[t]:
            return False, f"R6 {t} worse {sb[t]:g}->{sc[t]:g}"[:120]
    # PHASE3F A4: power_mag is judged per net; a rail may gain copper when that closes its own open
    bm, cm = _mag_by_net(base_m.get("gaps") or []), _mag_by_net(cand_gaps)
    bo, co = base_m.get("opens_by_net") or {}, cand_m.get("opens_by_net") or {}
    for n in sorted(set(bm) | set(cm)):
        if cm.get(n, 0) > bm.get(n, 0) and co.get(n, 0) >= bo.get(n, 0):
            return False, f"R6 power_mag worse {n} {bm.get(n, 0):g}->{cm.get(n, 0):g}"[:120]
    base_sev = {g["id"]: g["sev"] for g in base_m.get("gaps") or [] if "sev" in g}
    cand_sev = {g["id"]: g["sev"] for g in cand_gaps if "sev" in g}
    kept = [t for t in target_ids if not (t in base_sev and t in cand_sev and cand_sev[t] < base_sev[t])]
    # a power target whose mag dropped (narrow mm widened, vias added) is progress too: W15 close refused a widen that took +3V3
    # from 259 to 166 narrow mm as "target gap still there", so nothing was kept (R6 above already refuses any net whose mag rose)
    pk = ("power-width", "power-vias")
    base_mag = {g["id"]: g.get("mag", 0) for g in base_m.get("gaps") or [] if g["kind"] in pk}
    cand_mag = {g["id"]: g.get("mag", 0) for g in cand_gaps if g["kind"] in pk}
    kept = [t for t in kept if not (t in base_mag and t in cand_mag and cand_mag[t] < base_mag[t])]
    # PHASE3H A2: a target that lost sev, or whose net lost opens, is progress: no "target gap still there" refusal
    tnets = {n for g in base_m.get("gaps") or [] if g["id"] in target_ids for n in g.get("nets", [])}
    if len(kept) < len(target_ids) or any(co.get(n, 0) < bo.get(n, 0) for n in tnets):
        kept = []
    target_ids = kept
    cand_m["gap_ids"] = {g["id"] for g in cand_gaps}
    return drcguard.accept(base_m, cand_m, "repair", target_ids, kind, base_board, cand_board,
                           score=lambda m: score(m, cand_gaps if m is cand_m else (m.get("gaps") or [])),
                           snap=_snapshot, protect=protect)


def _mag_by_net(gs):
    """{net: summed power-width/power-vias mag} of a gap list (PHASE3F A4)."""
    out = {}
    for g in gs:
        if g["kind"] in ("power-width", "power-vias"):
            k = (g.get("nets") or [g["id"]])[0]
            out[k] = out.get(k, 0) + g.get("mag", 0)
    return out


class _RefillError(RuntimeError):
    """A candidate whose zones could not be refilled: refused as "refill failed", never accepted."""


def _remeasure(path, kicad_cli="kicad-cli"):
    """Refill every zone of `path` and save it (opengate.refill), then measure. Measures never see stale zone fills."""
    from . import opengate as OG
    try:
        OG.refill(path)
    except Exception as e:
        raise _RefillError("refill failed: " + str(e)[:80]) from e
    return dict(measure(path, kicad_cli))


# ---- rungs: fn(base, out, gaps, ctx) -> brief summary dict; writes `out` only when it has a candidate ----

def _rm(p):
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.lexists(p):
        os.remove(p)


def _copy(src, dst):
    _procs.atomic_copy(src, dst)


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


def _r_edgesliver(base, out, gaps, ctx):
    from . import edgesliver as ES
    cur, wrote, tmps = base, False, []
    try:
        for i, net in enumerate(_nets(gaps)):
            tmp = os.path.join(ctx["work"], f"es-{i}.kicad_pcb")
            tmps.append(tmp)
            _rm(tmp)
            _sidecars(cur, tmp)
            ES.repair(cur, tmp, net, constraints=ctx["cons_path"])
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


def _protect_levels(board, cons):
    """PHASE3I F6: {net: lock level} of every net of `board` (lockpolicy.level)."""
    from . import lockpolicy as LP, drcguard as DG, handroute as HR
    names = DG.snap_nets(HR._snap(board))
    return {n: LP.level(n, cons, names) for n in names}


def _r_handrepair(base, out, gaps, ctx):
    from . import handroute as HR, handrepair as HP, lockpolicy as LP, routeconn as RC
    w = os.path.join(ctx["work"], "hr-work.kicad_pcb")
    soft = sorted(n for n, lv in _protect_levels(base, ctx["cons"]).items() if lv == LP.SOFT)   # PHASE3I F6
    try:
        layers = signal_layers(base)
        if not layers:
            return {"status": "no signal layers"}
        _clean_board(w)
        HR.session_begin(base, w, ctx["kicad_cli"])
        r = HP.repair(w, out, constraints=ctx["cons_path"], budget=min(900, ctx["budget_s"]), layers=layers)
        if soft and os.path.exists(out):
            # a soft-locked net whose copper was ripped is written only when the same rung re-laid it (no new opens)
            ripped = [n for n in soft if _rows_count(out, n) < _rows_count(base, n)]
            opn_b, opn_o = RC.open_by_net(base), RC.open_by_net(out)
            lost = [n for n in ripped if opn_o.get(n, 0) > opn_b.get(n, 0)]
            if lost:
                os.remove(out)
                return {"status": "refused", "why": "soft-locked copper ripped and not re-laid: " + ", ".join(lost)[:120]}
    finally:
        _clean_board(w)
    return _brief(r)


def _rows_count(board, net):
    """track, arc and via items of `net` on `board` (cached board rows)."""
    rows = _board_rows(board)
    return sum(1 for t in rows["tracks"] if t[0] == net) + sum(1 for v in rows["vias"] if v[0] == net)


def _r_region(base, out, gaps, ctx):
    from . import repairregion as RR
    planes = tuple(sorted(set(copper_layers(base)) - set(signal_layers(base))))
    return _brief(RR.repair(base, out, nets=[re.escape(n) for n in _nets(gaps)], planes=planes,
                            constraints=ctx["cons_path"], kicad_cli=ctx["kicad_cli"], log=ctx["log"]))


def _rail_widths(base, cons, layers, track_mm):
    """PHASE3F A4: {rail net: width mm} (power.route_widths, zone nets excluded as in the pipeline); {} when unreadable."""
    from . import power as PWR
    try:                                                # layers = copper count (stackup weights), as krtchain passes it;
        return PWR.route_widths(cons, layers=len(copper_layers(base)), track_mm=track_mm, exclude=_plane_nets(base))
    except Exception:                                   # noqa: BLE001 - no rail widths is a worse route, not a failed rung
        return {}


def _plane_nets(board):
    """Nets with a zone on a plane (non-signal) copper layer. A rail that only has corridor pours on signal layers is
    still a rail: it keeps its width and is never a rip candidate (review fix: _zone_nets dropped every poured rail)."""
    try:
        import pcbnew
        b = pcbnew.LoadBoard(board)
        planes = [b.GetLayerID(n) for n in set(copper_layers(board)) - set(signal_layers(board))]
        return {z.GetNetname() for z in b.Zones() if z.GetNetname() and not z.GetIsRuleArea()
                and any(z.IsOnLayer(l) for l in planes)}
    except Exception:                                   # noqa: BLE001
        return set()


def _krt_cmd(base, out, nets, ctx, layers=None):
    """The KRT route command for `nets` (the pipeline's route command, the proven router). None when KRT is not found.
    Rails route at their [power] width (`--power-nets`), not at signal width (PHASE3F A4)."""
    import types
    from . import routerloc as RL, routerules as RR, pipeline as P, krtchain as KC
    try:
        rl = RL.resolve()
    except RuntimeError:
        return None
    R = RR.rules(ctx["cons"], base, default=RR.DEFAULT_SIZE)
    lyrs = layers or signal_layers(base)
    a = types.SimpleNamespace(router_py=rl["py"], router_dir=rl["dir"], layers=lyrs, track=None, clearance=None)
    rflags = RR.flags(R, rl["dir"], ctx["work"])
    pw = _rail_widths(base, ctx["cons"], lyrs, R["track"])
    return P._route_cmd(a, base, out, [KC.glob_escape(n) for n in nets], power_nets=pw, R=R, rflags=rflags)


RUNG_CAP_S = 900                                # PHASE3F A1: one rung gets at most this much of the close budget ([flow] rung_cap_min, minutes)


def _rung_cap(cons):
    return float((((cons or {}).get("flow") or {}).get("rung_cap_min", RUNG_CAP_S / 60))) * 60


SPLIT_MAX = 6                                   # PHASE3F A3: extra attempts per (kind, rung) per round for split halves
SPLIT_RUNGS = ("krt", "krt_region", "region", "pairgroup", "powerwiden", "relocate")   # the rungs whose candidate depends on the gap list
SPLIT_WHY = ("R1 ", "R2 ", "R4 ", "R5 ", "R6 ", "target gap still there")


def _guard_refusal(why):
    """True when a refusal came from the repair guard (a batch that a split may still close), not from a rung."""
    return str(why).startswith(SPLIT_WHY)


def _halves(gs):
    """PHASE3F A3: two halves of a gap batch; neighbours stay together (sorted by 5 mm cell, then id)."""
    o = sorted(gs, key=lambda g: (tuple(round(c / 5.0) for c in (g.get("at") or (0, 0))), g["id"]))
    k = len(o) // 2
    return [o[:k], o[k:]]
STALL_MIN = 30                                   # PHASE3I B3: a pass with no accept for [flow] stall_min stops "stalled"
WINDOW_MM = 3.0                                  # PHASE3I B3: copper window around a gap for its memo key
NO_GAIN_ROUNDS = 5                              # close() stops after this many rounds without a lower score ([flow] no_gain_rounds)
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


def _pair_fallback_legs(ctx):
    """PHASE3F A5: {leg: partner} for the non-gated pairs the pair builder refused (ctx["pair_fallback"], [p, n] each)."""
    out = {}
    for p, n in (ctx.get("pair_fallback") or []):
        out[p], out[n] = n, p
    return out


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
    fb = _pair_fallback_legs(ctx)           # PHASE3F A5: a refused non-gated pair is routed by KRT, both legs in one call
    targets = [n for n in dict.fromkeys(_nets(gaps)) if n not in protect or n in fb][:KRT_MAX]   # other pair legs: never alone
    if not targets:
        return {"status": "no plain net"}
    skip = protect | _zone_nets(base) | set(targets) | set(_rail_widths(base, ctx["cons"], signal_layers(base), 0.0))
    res = {"status": "ran", "rc": 0, "failed": 0, "open": 0, "closed": 0, "ripped": 0}
    cur, tmps, t0, done = base, [], time.time(), set()
    try:
        for k, net in enumerate(targets):
            if time.time() - t0 > ctx["budget_s"]:            # out of budget: what is closed so far is written
                res["status"] = "budget"
                break
            if net in done:
                continue
            legs = next((list(pr) for pr in ctx.get("pair_fallback") or [] if net in pr), [net])   # [p, n] order
            done |= set(legs)
            before = _krt_open(cur, cp)
            anchor = next((x for x in legs if x in before), net)  # the window is the open leg's
            rip = _krt_rip(cur, anchor, cp, skip)
            if rip is None:
                continue
            nxt, js = f"{out}.n{k}.kicad_pcb", f"{out}.krt{k}.json"     # js outlives the temp board (AI packet)
            tmps.append(nxt)
            _clean_board(nxt)
            _sidecars(cur, nxt)
            cmd = _krt_cmd(cur, nxt, legs, ctx)
            at = cmd.index("--nets")
            cmd[at:at] = ["--json-out", js] + (["--rip-existing-nets", *[KC.glob_escape(n) for n in rip]] if rip else [])
            try:
                proc = _procs.run(cmd, capture_output=True, text=True, timeout=min(900, ctx["budget_s"]),
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
            if set(legs) & after or (after - before):
                continue                                      # not closed (a pair: either leg), or a trade: previous stands
            cur = nxt
            res["closed"] += len(legs)
        if cur != base:
            _copy(cur, out)
            _sidecars(cur, out)
    finally:
        for t in tmps:
            _clean_board(t)
    return res


def _r_relocate(base, out, gaps, ctx):
    """Move a small support part next to the pin it serves (route-diagnose `moves`), re-lay its nets with KRT, keep what lowers the
    opens (relocate.py). The close loop's guard and the manifest commit judge the candidate like any rung's."""
    from . import relocate as RL
    return RL.relocate(base, out, gaps, ctx)


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


def _r_maskcut(base, out, gaps, ctx):
    """PHASE3F A7: the board-level mask openings re-cut after routing (thermal.recut); copper is not touched."""
    from . import thermal as TH
    r = TH.recut(base, ctx["cons"], out)
    return {"status": "ran" if os.path.exists(out) else "no change", "blocks": r["blocks"], "inferred": r["inferred"],
            "skipped": len(r["skipped"])}


def _r_powerwiden(base, out, gaps, ctx):
    """PHASE3G G4.1: narrow rail copper widened in place (power.widen); no output when nothing widened."""
    from . import power as PW
    r = PW.widen(base, ctx["cons"], out, nets=_nets(gaps), protect=set(ctx.get("protect") or ()))
    return {"status": "ran" if os.path.exists(out) else "no change", "widened_mm": r["widened_mm"], "left_mm": r["left_mm"],
            "skipped": r["skipped"], "necks": len(r.get("necks") or []),
            "unreached": {n: len(v) for n, v in (r.get("unreached") or {}).items()}}


def _r_railpour(base, out, gaps, ctx):
    """PHASE3G G4.1: rails that have a current figure but no zone get their corridor pour (power.pour_rails, guarded)."""
    from . import power as PW
    r = PW.pour_rails(base, ctx["cons"], out, layers=len(copper_layers(base)), nets=_nets(gaps))
    return {"status": "ran" if os.path.exists(out) else "no change", "kept": len(r.get("kept") or []), "dropped": bool(r.get("dropped"))}


def _r_powervias(base, out, gaps, ctx):
    """PHASE3G G4.2: rails short of their parallel vias get the missing vias beside existing ones (power.add_vias)."""
    from . import power as PW
    r = PW.add_vias(base, ctx["cons"], out, nets=_nets(gaps), cons_path=ctx.get("cons_path"))
    return {"status": "ran" if os.path.exists(out) else "no change", "added": sum(r["added"].values()), "refused": sum(r["refused"].values())}


def _r_snapiter(base, out, gaps, ctx):
    from . import snapiter as SI
    return _brief(SI.run(base, out, log=ctx["log"]))


PACKET_BYTES = 4096
AI_BUDGET = 4                                   # PHASE3F B5: action files applied per gap id per work dir ([ai] budget)
AI_MAX_ACTIONS = 8                              # PHASE3F B2 / PHASE3H A5: valid actions kept per action file
AI_ACTIONS_MAX = AI_MAX_ACTIONS
AI_ROUTE_NETS = 6                               # PHASE3H A5: most nets in one route action
AI_RIP_IDS = 12                                 # PHASE3H A5: most ids in one rip / rip_locked
AI_NUDGE_MM = 1.0                               # PHASE3H A5: largest |dx| or |dy| of a nudge
AI_PAIR_WPTS = 40                               # PHASE3H A5: most waypoints in one pair_path
FEEDBACK_BYTES = 1200                           # PHASE3F B4: ai_dir/<sid>.feedback.json size cap
_HEX_ID = re.compile(r"^[0-9a-fA-F]{8,}$")
_PACKET_ACTIONS = (
    'Envelope {"actions":[...],"why","tokens"}. Apply order: nudge -> rip -> path -> pair_path -> route. '
    "Route on signal layers only. "
    "rip {net, ids} (one net per rip, <= " + str(AI_RIP_IDS) + " hex ids); "
    "rip_locked {net, ids} (plain locked copper only, same id cap; pairs/rf refused); "
    "nudge {ref, dx, dy, rot} (needs rot; |dx|, |dy| <= " + str(AI_NUDGE_MM) + " mm; part <= 3 pads, not fixed, not locked); "
    "route {nets, layers} (<= " + str(AI_ROUTE_NETS) + " nets); "
    "path {net, path, lock} (AI-FINISH path syntax, <= 800 chars; hs-pair legs only as a stub join <= "
    + str(HS_STUB_MM) + " mm; a stitching via is path \"via x,y\"); "
    "pair_path {pair, layer, waypoints} (45/90 centre line, both legs laid, pair gaps only; "
    'waypoints [x, y] or [x, y, "Layer"], <= ' + str(AI_PAIR_WPTS) + ": a named layer other than the current one is a via change, "
    "<= 4, never on the last point; the start pad must be on the first layer; each run >= 1 mm; a change where the reference "
    "plane changes gets a return via, else return_via_missing); "
    "swap_pins {ref, pads, why} (proposal only, never applied); give_up {why}. "
    "Copper coordinates only via path / pair_path."
)
AI_PACKETS = 24                                 # PHASE3H A1: packets written per _r_ai call ([ai] packets overrides)


def ai_answer_required(cons):
    """[ai] answer = "optional" (default): a stage whose AI packets have no answer goes on with its defaults and records the count;
    "required": the stage stops with exit 5 until the packets are answered."""
    return str(((cons or {}).get("ai") or {}).get("answer", "optional")).lower() == "required"


def _ai_packets(cons):
    """PHASE3H A1: [ai] packets overrides AI_PACKETS (packets written per _r_ai call)."""
    return int(((cons or {}).get("ai") or {}).get("packets", AI_PACKETS))


def _ai_budget(cons):
    """PHASE3F B5: [ai] budget overrides AI_BUDGET (action files applied per gap id)."""
    return int(((cons or {}).get("ai") or {}).get("budget", AI_BUDGET))


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _ai_action(x, copper, hs_legs=(), open_legs=(), rip_mm=0.0):
    """(action, reason): the validated action, or (None, reason) when dropped (PHASE3H A4). `hs_legs`: legs of
    high-speed pairs (only pair_path may touch them; a path only as a stub join). PHASE3H B3: a leg that is open has no
    stub cap (R6 still gates). PHASE3H B9: `rip_mm` is the copper ripped on that net in the same file (added to the cap)."""
    if not isinstance(x, dict):
        return None, "not an object"
    do = x.get("do")
    if do in ("rip", "rip_locked"):
        ids = x.get("ids")
        if not (isinstance(x.get("net"), str) and x["net"]):
            return None, f"{do}: net"
        if not (isinstance(ids, list) and 0 < len(ids) <= AI_RIP_IDS
                and all(isinstance(i, str) and _HEX_ID.match(i) for i in ids)):
            return None, f"{do}: ids must be 1-{AI_RIP_IDS} hex ids"
        return {"do": do, "net": x["net"], "ids": list(ids)}, ""
    if do == "nudge":
        if not (isinstance(x.get("ref"), str) and x["ref"] and all(_num(x.get(k)) for k in ("dx", "dy", "rot"))):
            return None, "nudge: needs ref, dx, dy, rot"
        if abs(x["dx"]) > AI_NUDGE_MM or abs(x["dy"]) > AI_NUDGE_MM:
            return None, f"nudge: |dx|, |dy| > {AI_NUDGE_MM} mm"
        return {"do": "nudge", "ref": x["ref"], "dx": x["dx"], "dy": x["dy"], "rot": x["rot"]}, ""
    if do == "route":
        nets, layers = x.get("nets"), x.get("layers")
        if not (isinstance(nets, list) and nets and all(isinstance(n, str) and n for n in nets)):
            return None, "route: nets must be names"
        if len(nets) > AI_ROUTE_NETS:
            return None, f"route: nets > {AI_ROUTE_NETS}"
        if not (layers is None or (isinstance(layers, list) and all(l in copper for l in layers))):
            return None, "route: layers must be signal layers"
        return {"do": "route", "nets": list(nets), "layers": list(layers) if layers else None}, ""
    if do == "path":                              # PHASE3F B2
        from . import hrgeom as HG
        net, path = x.get("net"), x.get("path")
        if not (isinstance(net, str) and net and isinstance(path, str) and 0 < len(path) <= 800):
            return None, "path: net and path (<= 800 chars) needed"
        try:
            prims = HG.parse_path(path, copper)
        except Exception:
            return None, "path: unparseable"
        if net in hs_legs and net not in open_legs and sum(math.hypot(p[3][0] - p[2][0], p[3][1] - p[2][1])
                                                          for p in prims if p[0] == "seg") > HS_STUB_MM + rip_mm:
            return None, f"path: hs-pair leg longer than {HS_STUB_MM} mm"
        return {"do": "path", "net": net, "path": path, "lock": bool(x.get("lock"))}, ""
    if do == "pair_path":
        # PHASE3G G1b a: waypoints [x, y] or [x, y, "Layer"]; a named layer other than the current one is a layer change
        pair, layer, wps = x.get("pair"), x.get("layer"), x.get("waypoints")
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(n, str) and n for n in pair)
                and isinstance(layer, str) and layer in copper and isinstance(wps, list)
                and 2 <= len(wps) <= AI_PAIR_WPTS):
            return None, f"pair_path: pair, layer, or 2-{AI_PAIR_WPTS} waypoints"
        out = []
        for q in wps:
            if not (isinstance(q, list) and len(q) in (2, 3) and all(_num(c) for c in q[:2])):
                return None, "pair_path: waypoint must be [x, y] or [x, y, layer]"
            if len(q) == 3 and not (isinstance(q[2], str) and q[2] in copper):
                return None, "pair_path: waypoint layer not copper"
            out.append([q[0], q[1]] + ([q[2]] if len(q) == 3 else []))
        lays, cur = [], layer
        for q in out:
            cur = q[2] if len(q) == 3 else cur
            lays.append(cur)
        chg = [i for i in range(1, len(out)) if lays[i] != lays[i - 1]]
        if len(chg) > 4:                                # at most 4 layer changes per centre line
            return None, "pair_path: more than 4 layer changes"
        for s, e in zip([0] + chg, chg + [len(out) - 1]):   # each run: 45/90 segments, at least 1.0 mm long
            run = 0.0
            for a, b in zip(out[s:e + 1], out[s + 1:e + 1]):
                dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
                if (dx < 1e-6 and dy < 1e-6) or (min(dx, dy) > 1e-6 and abs(dx - dy) > 1e-6):
                    return None, "pair_path: segments must be 45/90"
                run += math.hypot(dx, dy)
            if run < 1.0:
                return None, "pair_path: run under 1.0 mm"
        return {"do": "pair_path", "pair": list(pair), "layer": layer, "waypoints": out}, ""
    if do == "swap_pins":
        pads = x.get("pads")
        if (isinstance(x.get("ref"), str) and x["ref"] and isinstance(pads, list) and len(pads) == 2
                and all(isinstance(q, str) and q for q in pads)):
            return {"do": "swap_pins", "ref": x["ref"], "pads": list(pads), "why": str(x.get("why") or "")[:200]}, ""
        return None, "swap_pins: ref and two pads needed"
    if do == "give_up":
        return {"do": "give_up", "why": str(x.get("why") or "")[:200]}, ""
    return None, f"unknown do {str(do)[:20]!r}"


def _rip_mm(base, net, ids, cons_path=None):
    """PHASE3H B9: copper length (mm) of the tracks named by `ids` on `net` (vias add none). 0.0 when unreadable."""
    from . import handroute as HR
    try:
        M = HR._model(base, cons_path, refill=False)
        return sum(math.hypot(o.g[2][0] - o.g[1][0], o.g[2][1] - o.g[1][1]) for o in M.obs
                   if o.kind == "track" and o.net == net and any(o.id.startswith(i) for i in ids))
    except Exception as e:                       # noqa: BLE001 - an unreadable board gives no extra stub length
        print(f"ai: rip length unavailable for {net}: {e}", file=sys.stderr)
        return 0.0


def load_ai_action(path, base, protect=(), hs_legs=(), open_legs=(), cons_path=None):
    """{"actions", "tokens", "why", "dropped"} from an AI action file. Invalid actions are dropped with a log line and
    listed in "dropped" as {do, reason} (PHASE3H A4). Never raises. `protect` (drcguard.protected_nets: rf nets and pair
    legs) may not be rip_locked. At most AI_MAX_ACTIONS are kept; the rest are dropped."""
    out = {"actions": [], "tokens": 0, "why": "", "dropped": []}
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
    raw = a.get("actions") if isinstance(a.get("actions"), list) else []
    rip_mm = {}                                  # PHASE3H B9: rips on a high-speed leg in this same file
    for x in raw:
        if isinstance(x, dict) and x.get("do") == "rip" and x.get("net") in hs_legs and isinstance(x.get("ids"), list):
            ids = [i for i in x["ids"] if isinstance(i, str)]
            rip_mm[x["net"]] = rip_mm.get(x["net"], 0.0) + _rip_mm(base, x["net"], ids, cons_path)
    for x in raw:
        net = x.get("net") if isinstance(x, dict) else None
        v, why = _ai_action(x, copper, hs_legs, open_legs, rip_mm.get(net, 0.0) if isinstance(net, str) else 0.0)
        if v is not None and v["do"] == "rip_locked" and v["net"] in protect:
            v, why = None, "rip_locked: protected net (pair or rf)"
        if v is not None and len(out["actions"]) >= AI_MAX_ACTIONS:
            v, why = None, f"over {AI_MAX_ACTIONS} actions"
        if v is None:
            print(f"ai: dropped action {str(x)[:120]}", file=sys.stderr)
            out["dropped"].append({"do": str(x.get("do") if isinstance(x, dict) else "")[:20], "reason": why[:120]})
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


def _pair_block(base, cons, gap):
    """PHASE3F B1: the pair's family, its per-layer (width, gap) from zgeom (the geometry the pairs stage lays) and the
    family skew limit. Empty geometry when the board has no stackup for the family. Never raises."""
    from . import zgeom as ZG
    pn = gap.get("nets") or []
    p, n = (pn + [None, None])[:2]
    fam = ZG.family_of(cons, p) if p else None
    geo = {}
    try:
        rows, planes, _src = ZG.resolve_stack(cons, base)
        cu = [r["name"] for r in rows if r.get("type") == "copper" and r.get("name")]
        sig = [l for l in cu if l not in set(planes)]
        if fam and rows:
            tab = ZG.family_table(cons, rows, planes, sig, fam, board=base)
            geo = {l: {"width": g["w"], "gap": g["gap"]} for l, g in tab.items() if g.get("gap")}
    except Exception as e:
        print(f"ai: pair geometry unavailable for {p}: {e}", file=sys.stderr)
    lim = gap.get("limit")                              # the audit's limit for this pair; else the family's
    try:                                                # D71cv: tune brings the pair down to the TARGET, not just inside the maximum
        from . import constraints as C_
        tl = C_.skew_limit_checked(cons or {}, p)[0]
        lim = tl if tl is not None else lim
    except Exception:                                   # noqa: BLE001
        pass
    if lim is None and fam:
        lim = ((cons or {}).get("pairs") or {}).get(fam, {}).get("skew_mm")
    legs = {}                                           # PHASE3H B2: each leg's routed length (None legs left out)
    try:
        from . import routediag as RD, pairaudit as PA
        B = RD.load(base)
        for net in (p, n):
            ln = PA.profile(B, net).get("length") if net else None
            if ln is not None:
                legs[net] = round(float(ln), 3)
    except Exception as e:                              # noqa: BLE001 - the block goes without the lengths
        print(f"ai: pair legs unavailable for {p}/{n}: {e}", file=sys.stderr)
    both = len(legs) == 2
    return {"p": p, "n": n, "class": gap.get("class"), "family": fam, "geometry": geo,
            "skew_mm": gap.get("skew"), "skew_limit_mm": lim,   # skew_mm: measured |P - N| (None when a leg is open)
            "legs": legs, "short_leg": min(legs, key=legs.get) if both else None,
            "add_mm": round(abs(legs[p] - legs[n]), 3) if both else None}


def _pair_ends(base, cons, gap, ai_dir, sid, cp):
    """PHASE3G G1: a pair gap with no open connection: the two terminals farthest apart framed as windows (PNG each),
    and both legs' layer sequence and length from the pair audit (pairaudit.profile). Never raises."""
    from . import routediag as RD, pairaudit as PA, pairs as PR, handroute as HR
    out = {"ends": [], "legs": {}}
    p, n = gap["nets"]
    try:
        B = RD.load(base)
        terms, _lone = PR.terminals(B, p, n, PR.params(cons)["pad_max"])
        if terms:
            a, b = max(((x, y) for x in terms for y in terms), key=lambda xy: math.hypot(xy[0]["c"][0] - xy[1]["c"][0],
                                                                                        xy[0]["c"][1] - xy[1]["c"][1]))
            ends = (a, b) if a is not b else (a,)
            for k, t in enumerate(ends):
                out["ends"].append({"name": t["name"], "at": [round(t["c"][0], 2), round(t["c"][1], 2)]})
                if ai_dir and sid:
                    out.setdefault("png", []).extend(_window_png(base, cp, ai_dir, sid, f"pairend{k}", at=t["c"], size=(4, 4))[1])
            # PHASE3H B2: the window at the end with more free room; it keeps the copper and vias of both legs
            views = [HR.window(base, at=t["c"], size=(4, 4), constraints=cp) for t in ends]
            room = [sum(len(v or []) for v in (W.get("free_corridors") or {}).values()) for W in views]
            W, legs = views[room.index(max(room))], {p, n}
            out["window"] = {"box": W.get("window"), "layers": signal_layers(base),
                             "tracks": [t for t in W.get("tracks") or [] if t.get("net") in legs],
                             "vias": [v for v in W.get("vias") or [] if v.get("net") in legs],
                             "via_sites": [[round(s[0], 2), round(s[1], 2)]
                                           for s in ((W.get("via_sites") or {}).get("sites") or [])[:10]]}
        for net in (p, n):
            pr = PA.profile(B, net)
            out["legs"][net] = {"layers": pr.get("layers"), "length": pr.get("length"), "vias": pr.get("vias")}
    except Exception as e:                               # noqa: BLE001 - the packet still goes without the ends
        print(f"ai: pair ends unavailable for {p}/{n}: {e}", file=sys.stderr)
    return out


def _window_png(base, cp, ai_dir, sid, tag, **kw):
    """HR.window with a PNG at ai_dir/<sid>.<tag>.png when both are given. A render failure is logged and the view is
    recomputed without the picture. Returns (window dict, [png paths])."""
    from . import handroute as HR
    png = os.path.join(ai_dir, f"{sid}.{tag}.png") if ai_dir and sid else None
    if png is None:
        return HR.window(base, constraints=cp, **kw), []
    try:
        W = HR.window(base, constraints=cp, png=png, **kw)
        return W, list(W.get("images") or [])
    except Exception as e:
        print(f"ai: {tag} picture failed for {sid}: {e}", file=sys.stderr)
        return HR.window(base, constraints=cp, **kw), []


def _island_sites(base, cp, fills, sites):
    """PHASE3H B4: the via sites lying in the fills of one island ([zone, layer, fill] entries), and the island's
    {layer, bbox}. Never raises: an unreadable board keeps every site and gives no island."""
    import numpy as np
    from . import handroute as HR, hrgeom as HG
    try:
        M = HR._model(base, cp, refill=False)
        polys = [M.zones[z]["fills"][l][f] for z, l, f in fills]
    except Exception as e:                       # noqa: BLE001
        print(f"ai: island fills unavailable: {e}", file=sys.stderr)
        return sites, None
    if not polys or not sites:
        return sites, None
    P = np.array([[s[0], s[1]] for s in sites])
    ins = np.zeros(len(sites), bool)
    for fp in polys:
        ins |= HG.in_fill(P, fp)
    bb = [min(f["bb"][0] for f in polys), min(f["bb"][1] for f in polys),
          max(f["bb"][2] for f in polys), max(f["bb"][3] for f in polys)]
    return [s for s, k in zip(sites, ins) if k], {"layer": fills[0][1], "bbox": [round(v, 3) for v in bb]}


def _write_topology(ai_dir, sid, gap_id):
    """PHASE3H B10: a pair-asym gap with no connection is a topology fault: a proposal row, not an agent packet."""
    os.makedirs(ai_dir, exist_ok=True)
    with open(os.path.join(ai_dir, sid + ".topology.json"), "w") as fh:
        json.dump({"gap_id": gap_id, "proposal": "topology", "why": "topology: legs end at different pads"}, fh)


def _opens(path):
    """PHASE3H B5: the unconnected count of `path` (one DRC); None when it cannot be read."""
    from . import handroute as HR
    try:
        return int(HR.summarize_drc(HR.drc_raw(path))["unconnected"])
    except Exception as e:                       # noqa: BLE001 - the feedback row then has no count
        print(f"ai: open count unavailable for {path}: {e}", file=sys.stderr)
        return None


def _feed(do, target, ok, why, before, after):
    """PHASE3H B5: one feedback row for an action: ok, why, and the unconnected count before -> after."""
    return {"do": do, "target": target, "ok": bool(ok), "why": str(why)[:120], "opens_before": before, "opens_after": after}


def gap_packet(base, gap, ctx, sid=None):
    """Repair packet v2 for one gap (<= PACKET_BYTES): its first open connection of the net, the copper window around
    it (PNG views: connection and the pad pocket with less free room), the direct corridors, the obstacles that seal it,
    the KRT failures on the net, the attempts already made on this gap and the feedback of the last applied action."""
    from . import handroute as HR
    cp = ctx.get("cons_path")
    cons = ctx.get("cons") or {}
    ai_dir = ctx.get("ai_dir")
    net = gap.get("net") or (gap.get("nets") or [None])[0]
    pair = str(gap.get("kind", "")).startswith("pair-") and len(gap.get("nets") or []) == 2
    want = set(gap.get("nets") or []) if pair else {net}     # PHASE3H B6: a pair gap takes its first open leg
    want.discard(None)
    i = None
    conns = []
    raw = None
    if want:
        try:                                        # ONE DRC for the whole packet: `i` indexes this list, and a second
            raw = HR.drc_raw(base)                  # DRC (refill) can order the connections differently
        except Exception:
            raw = None
        conns = HR.open_nets(base, constraints=cp, raw=raw)["connections"]
        i = next((k for k, r in enumerate(conns) if r.get("net") in want), None)
        if i is not None and pair:
            net = conns[i]["net"]
    conn = win = corr = None
    pngs = []
    sealed = []
    if i is not None:
        r = conns[i]
        ends = lambda p: {k: p.get(k) for k in ("x", "y", "layer", "pin", "id")}
        conn = {"i": i, "a": ends(r["a"]), "b": ends(r["b"]), "straight_mm": r.get("straight_mm"),
                "rules": r.get("rules")}
        if r.get("closest"):                        # the window frames the islands' closest approach, not the DRC ends
            conn["closest"] = {k: r["closest"].get(k) for k in ("gap_mm", "a", "b", "a_item", "b_item")}
        W, got = _window_png(base, cp, ai_dir, sid, "conn", conn=i, margin=2.0, raw=raw)
        pngs += got
        mid = ((r["a"]["x"] + r["b"]["x"]) / 2, (r["a"]["y"] + r["b"]["y"]) / 2)
        sites = (W.get("via_sites") or {}).get("sites") or []
        isl = None
        if r["a"].get("island_fills"):              # PHASE3H B4: a zone end: the via sites inside its island only
            sites, isl = _island_sites(base, cp, r["a"]["island_fills"], sites)
        win = {"box": W.get("window"), "layers": signal_layers(base),
               "tracks": _near(W.get("tracks") or [], mid, ("a", "b")),
               "vias": _near(W.get("vias") or [], mid, ("pos",)),
               "via_sites": [[round(s[0], 2), round(s[1], 2)] for s in sites[:10]]}
        if isl:
            win["island"] = isl
        cv = W.get("connection_view") or {}
        free = W.get("free_corridors") or {}
        sig = set(win["layers"])                    # planes are not corridors: signal layers only
        corr = {"direct": [l for l in cv.get("direct_corridor_layers") or [] if l in sig],
                "max_track_mm": {l: c[0]["max_track_mm"] for l, c in free.items() if c and l in sig}}
        room = {t: len((cv.get(t) or {}).get("free_component_by_layer") or {}) for t in ("a", "b")}
        side = "a" if room["a"] <= room["b"] else "b"         # the end with less free room (ties: a)
        end = r[side]
        if r.get("closest") and r["closest"].get(side):       # same place as the connection view
            end = {"x": r["closest"][side][0], "y": r["closest"][side][1]}
        if ai_dir and sid:
            try:
                pngs += _window_png(base, cp, ai_dir, sid, "pocket", at=(end["x"], end["y"]), size=(4, 4))[1]
            except Exception as e:
                print(f"ai: pocket view failed for {sid}: {e}", file=sys.stderr)
        lim = (HR.suggest(base, net, conn=i, constraints=cp, raw=raw).get("sealed_by") or {}).get("limiting_obstacles") or []
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
    fb = None
    if ai_dir and sid:
        try:
            with open(os.path.join(ai_dir, sid + ".feedback.json")) as fh:
                fb = json.load(fh)
        except (OSError, ValueError):
            fb = None
    p = {"v": 2, "gap": {"id": gap["id"], "kind": gap["kind"], "class": gap.get("class"), "net": net},
         "board": base, "conn": conn, "window": win, "corridors": corr, "png": pngs, "sealed_by": sealed,
         "krt": krt, "tried": tried[-6:], "feedback": fb, "actions": _PACKET_ACTIONS}
    if str(gap.get("kind", "")).startswith("pair-") and len(gap.get("nets") or []) == 2:
        p["pair"] = _pair_block(base, cons, gap)
        if i is None:                                # PHASE3G G1: both legs closed (asym / skew): frame the pair's end pads
            pe = _pair_ends(base, cons, gap, ai_dir, sid, cp)
            pngs += pe.pop("png", [])                # the packet's picture list (p["png"] is this same list)
            if pe.get("window"):                     # PHASE3H B2: the end's window (both legs' copper)
                p["window"] = pe.pop("window")
            p["pair"].update(pe)
    return _clip_packet(p)


def _route_net(w, net, layers, ctx):
    """KRT re-route of `net` on the session board, then suggest + lay for each of its open connections."""
    from . import handroute as HR
    dst = w + ".krt.kicad_pcb"
    _clean_board(dst)
    cmd = _krt_cmd(w, dst, [net], ctx, layers)
    if cmd is not None:
        try:
            _procs.run(cmd, capture_output=True, text=True, timeout=min(900, ctx["budget_s"]), cwd=ctx["work"])
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


def _viol_row(v):
    return {k: v[k] for k in ("kind", "net", "with", "layer", "need", "margin", "at") if k in v}


def _ai_path(w, x, cp):
    """PHASE3F B3: lay one agent path on the session. A refusal does not end the transaction; its probe goes to feedback."""
    from . import handroute as HR
    row = {"do": "path", "net": x["net"], "ok": False, "why": ""}
    try:
        res = HR.lay(w, x["net"], x["path"], constraints=cp, lock=bool(x.get("lock"))) or {}
        row["ok"] = bool(res.get("accepted"))           # lay reports a refusal in its result; it does not raise
        row["why"] = "" if row["ok"] else str(res.get("reason") or "refused")[:160]
    except Exception as e:
        row["why"] = str(e)[:160]
    if not row["ok"]:
        try:
            pr = HR.probe(w, x["net"], x["path"], constraints=cp)
            row["probe"] = [_viol_row(v) for v in (pr.get("violations") or [])[:3]]
        except Exception:
            row["probe"] = []
    return row


def _write_swap(ai_dir, sid, gap_id, x):
    """PHASE3F B3: a pin swap changes the netlist, so it is only written as a proposal (never applied by close)."""
    with open(os.path.join(ai_dir, sid + ".swap.json"), "w") as fh:
        json.dump({"gap_id": gap_id, "action": x, "note": "pin swap: needs the part spec and the schematic"}, fh)


def _feedback_note(ai_dir, sid, **kw):
    """PHASE3F B4: merge `kw` into ai_dir/<sid>.feedback.json (<= FEEDBACK_BYTES; oldest actions dropped first)."""
    path = os.path.join(ai_dir, sid + ".feedback.json")
    try:
        with open(path) as fh:
            d = json.load(fh)
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    d.update(kw)
    while len(json.dumps(d).encode()) > FEEDBACK_BYTES and d.get("actions"):
        d["actions"] = d["actions"][1:]
    if len(json.dumps(d).encode()) > FEEDBACK_BYTES:
        d = {k: str(v)[:200] for k, v in d.items()}
    os.makedirs(ai_dir, exist_ok=True)
    with open(path, "w") as fh:
        fh.write(json.dumps(d))


def _apply_ai(base, out, a, ctx, sid):
    """One transaction on copies: nudges, then a session with rips and routes, then session_commit (the guard).
    Returns (status, commit, why). Temps are removed in every case."""
    from . import handroute as HR, routediag as RD, legality as LGL, routable as RB, boardwrite as BW
    cons, cp, ai_dir = ctx["cons"], ctx["cons_path"], ctx["ai_dir"]
    acts = a["actions"]
    give = next((x for x in acts if x["do"] == "give_up"), None)
    if give:
        return "give_up", False, give["why"]
    cur, n_exec = base, 0                           # PHASE3H A9: n_exec counts the actions that ran
    w = os.path.join(ai_dir, sid + ".work.kicad_pcb")
    tmps = [w]
    feed = []                                       # PHASE3F B4 / PHASE3H B5: per action {do, target, ok, why, opens}
    try:
        fixed = set(RB._fixed_set(RD.load(base), cons))
        opens = _opens(cur)
        for i, x in enumerate(v for v in acts if v["do"] == "nudge"):
            part = RD.load(cur)["parts"].get(x["ref"])
            if part is None or len(part["pads"]) > 3 or x["ref"] in fixed or part.get("locked"):
                print(f"ai: nudge {x['ref']} refused (fixed, locked, or more than 3 pads)", file=sys.stderr)
                feed.append(_feed("nudge", x["ref"], False, "fixed, locked, or more than 3 pads", opens, opens))
                continue
            px, py = part["pos"]
            tmp = os.path.join(ai_dir, f"{sid}.nudge{i}.kicad_pcb")
            tmps.append(tmp)
            _clean_board(tmp)
            _sidecars(cur, tmp)
            if not BW.move_file(cur, tmp, {x["ref"]: (px + x["dx"], py + x["dy"], part.get("angle", 0) + x["rot"])}):
                feed.append(_feed("nudge", x["ref"], False, "move failed", opens, opens))
                continue
            if LGL.Legal(RD.load(tmp), cons).part_issues(x["ref"]):
                print(f"ai: nudge {x['ref']} refused (courtyard or outline issue)", file=sys.stderr)
                feed.append(_feed("nudge", x["ref"], False, "courtyard or outline issue", opens, opens))
                continue
            after = _opens(tmp)
            feed.append(_feed("nudge", x["ref"], True, "", opens, after))
            opens, cur = after, tmp
            n_exec += 1
        _clean_board(w)
        _sidecars(cur, w)
        HR.session_begin(cur, w)
        for x in acts:                              # PHASE3H B5: an unknown id is a refused rip (ok false), not an abort
            if x["do"] not in ("rip", "rip_locked"):
                continue
            try:
                if x["do"] == "rip":
                    HR.shove(w, x["net"], ids=x["ids"], op="unlay", constraints=cp)
                else:
                    # session-only opt-in: needs explicit ids, no window, no out (handroute.shove)
                    HR.shove(w, x["net"], ids=x["ids"], op="unlay", constraints=cp, allow_locked=True)
                ok, why = True, ""
            except ValueError as e:
                ok, why = False, str(e)
            after = _opens(w)
            feed.append(_feed(x["do"], x["net"], ok, why, opens, after))
            opens = after
        n_exec += sum(1 for f in feed if f["do"] in ("rip", "rip_locked") and f["ok"])
        for x in acts:                              # PHASE3F B3: paths after the rips, then pair centre lines
            if x["do"] == "path":
                feed.append(_ai_path(w, x, cp))
        for x in acts:
            if x["do"] == "pair_path":
                from . import pairsonly as PS
                res = PS.lay_centreline(w, None, x["pair"], x["layer"], x["waypoints"], cons)
                feed.append({"do": "pair_path", "ok": bool(res.get("laid")), "why": str(res.get("why") or "")[:160],
                             "at": res.get("at"), "blocker": res.get("blocker"), "stripped": res.get("stripped")})
            elif x["do"] == "swap_pins":
                _write_swap(ai_dir, sid, a.get("gap_id"), x)
                feed.append({"do": "swap_pins", "ok": False, "why": "proposal written, not applied"})
        routes = [dict(x, nets=list(x["nets"])) for x in acts if x["do"] == "route"]
        routed = {n for r in routes for n in r["nets"]}
        ripped = [n for n in dict.fromkeys(x["net"] for x in acts if x["do"] == "rip_locked") if n not in routed]
        if ripped:                                  # a locked rip must be re-routed in this transaction
            if routes:
                routes[-1]["nets"] += ripped
            else:
                routes.append({"do": "route", "nets": ripped, "layers": None})
        n_exec += len(routes)
        for r in routes:
            for net in r["nets"]:
                before = opens
                _route_net(w, net, r["layers"] or signal_layers(w), ctx)
                opens = _opens(w)
                ok = before is not None and opens is not None and opens < before
                feed.append(_feed("route", net, ok, "" if ok else "no open closed", before, opens))
        n_exec += sum(1 for f in feed if f["do"] in ("path", "pair_path"))
        r = HR.session_commit(w, out, constraints=cp, mode="edit")      # PHASE3H B1: close's _accept judges R2/R4/R6
        ok = bool(r.get("accepted"))
        reason = "; ".join(str(x) for x in (r.get("reasons") or [])[:3])    # PHASE3H B5: up to 3 reasons
        a["executed"] = n_exec
        _feedback_note(ai_dir, sid, actions=feed, commit=ok, commit_why=reason[:160])
        return "applied", ok, reason
    finally:
        for t in tmps:
            _clean_board(t)


def ctx_dir_ai(work):
    return os.path.join(work, "ai")


def _packet_rows(work, gaps, used, budget):
    """PHASE3F B6: the handoff list for the caller: one row per open AI-eligible gap that has a packet on disk."""
    ai_dir, rows = ctx_dir_ai(work), []
    for g in gaps:
        if "ai" not in DISPATCH.get(g["kind"], []):
            continue
        key = ai_key(g)
        sid = critroute._safe(key)[:80]
        pk = os.path.join(ai_dir, sid + ".packet.json")
        if not os.path.exists(pk):
            continue
        try:
            with open(pk) as fh:
                png = json.load(fh).get("png") or []
        except (OSError, ValueError):
            png = []
        rows.append({"gap_id": g["id"], "packet": pk, "png": png, "action": os.path.join(ai_dir, sid + ".action.json"),
                     "budget_left": max(0, budget - used.get(key, 0))})
    for r in _sessions_load(ai_dir):                          # PHASE3I F4: one row per open session region
        if r.get("status") == "open":
            rows.append({"kind": "session", "region": r["rid"], "brief": r["brief"], "session": r["session"]})
    return rows


def _proposal_rows(ai_dir):
    """PHASE3F B3/B6: swap_pins proposals written by the AI rung (never applied by close)."""
    out = []
    for f in sorted(glob.glob(os.path.join(ai_dir, "*.swap.json"))) + sorted(glob.glob(os.path.join(ai_dir, "*.topology.json"))):
        try:
            with open(f) as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            pass
    return out


def _ai_pending_names(ai_dir, gaps):
    """Sorted basenames of the unapplied answers for `gaps`: `<sid>.action.json`, and PHASE3I F4 `<rid>.done.json` of an
    open session region that holds one of the gaps."""
    names = set()
    for g in gaps:
        name = critroute._safe(ai_key(g))[:80] + ".action.json"
        if os.path.exists(os.path.join(ai_dir, name)):
            names.add(name)
    ids = {g["id"] for g in gaps}
    for r in _sessions_load(ai_dir):
        done = r.get("rid", "") + ".done.json"
        if r.get("status") == "open" and ids & set(r.get("gaps", [])) and os.path.exists(os.path.join(ai_dir, done)):
            names.add(done)
    return sorted(names)


_ROWS = {}                                      # PHASE3F/I: (abspath, mtime_ns, size) -> board rows, two entries


def _load_rows(board):
    """PHASE3I F3: ONE pcbnew load -> tracks/arcs, vias, pads and footprints (all nm), for the window hash."""
    import pcbnew
    b = pcbnew.LoadBoard(board)
    if b is None:
        raise ValueError("unreadable board")
    tracks, vias = [], []
    for t in b.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T:
            p = t.GetPosition()
            vias.append((t.GetNetname(), t.GetLayer(), p.x, p.y, t.GetWidth(), t.GetDrill()))
        else:
            bb, s0, s1 = t.GetBoundingBox(), t.GetStart(), t.GetEnd()
            tracks.append((t.GetNetname(), t.GetLayer(), s0.x, s0.y, s1.x, s1.y, t.GetWidth(),
                           bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()))
    pads, fps = [], []
    for fp in b.GetFootprints():
        p = fp.GetPosition()
        fps.append((fp.GetReference(), p.x, p.y, round(fp.GetOrientationDegrees(), 3), fp.GetLayer()))
        for pd in fp.Pads():
            q = pd.GetPosition()
            pads.append((pd.GetNetname(), fp.GetReference(), q.x, q.y))
    return {"tracks": tracks, "vias": vias, "pads": pads, "fps": fps}


def _board_rows(board):
    """cached `_load_rows` (two boards at a time, keyed by path, mtime and size)."""
    st = os.stat(board)
    key = (os.path.abspath(board), st.st_mtime_ns, st.st_size)
    if key not in _ROWS:
        if len(_ROWS) >= 2:
            _ROWS.pop(next(iter(_ROWS)))
        _ROWS[key] = _load_rows(board)
    return _ROWS[key]


def window_hash(board, gap, r=WINDOW_MM):
    """PHASE3I B3/F3: memo key part for one gap. The window is the bbox of the gap's net pads padded `r` mm (no pads: `at`
    +- r; neither: the whole-board fingerprint). Hash = every track/arc whose bbox meets the window, vias and footprints
    inside it. `gap` may be a gap dict or a bare `at` pair."""
    import pcbnew
    gap = gap if isinstance(gap, dict) else {"at": gap, "nets": []}
    try:
        rows = _board_rows(board)
    except Exception:                                # not a readable board: the file bytes are the key (conservative)
        return hashlib.sha1(open(board, "rb").read()).hexdigest()
    R = pcbnew.FromMM(r)
    nets = set(gap.get("nets") or [])
    box = None
    pts = [(x, y) for n, _ref, x, y in rows["pads"] if n in nets]
    if pts:
        box = (min(x for x, _ in pts) - R, min(y for _, y in pts) - R, max(x for x, _ in pts) + R, max(y for _, y in pts) + R)
    elif gap.get("at"):
        cx, cy = pcbnew.FromMM(gap["at"][0]), pcbnew.FromMM(gap["at"][1])
        box = (cx - R, cy - R, cx + R, cy + R)
    if box is None:
        return versions.copper_fingerprint(board)
    x0, y0, x1, y1 = box
    out = []
    for n, layer, sx, sy, ex, ey, w, l, t, rt, bt in rows["tracks"]:
        if rt >= x0 and l <= x1 and bt >= y0 and t <= y1:
            out.append(["trk", n, layer, sx, sy, ex, ey, w])
    for n, layer, x, y, w, d in rows["vias"]:
        if x0 <= x <= x1 and y0 <= y <= y1:
            out.append(["via", n, layer, x, y, w, d])
    for ref, x, y, rot, layer in rows["fps"]:
        if x0 <= x <= x1 and y0 <= y <= y1:
            out.append(["fp", ref, x, y, rot, layer])
    return hashlib.sha1(json.dumps(sorted(out, key=str)).encode()).hexdigest()


def _rank(kind, rungs, yields):
    """PHASE3I B3: rung order for a kind by measured yield in this work dir (accepts over tries, unmeasured neutral).
    `ai` (L1) moves ahead of the other L1 router rungs once an L0 rung has failed here at least once."""
    row = yields.get(kind, {})
    rate = {r: (row.get(r, [0, 0])[1] + 0.5) / (row.get(r, [0, 0])[0] + 1) for r in rungs}
    order = sorted(rungs, key=lambda r: -rate[r])
    failed_l0 = any(LEVEL.get(r, "L0") == "L0" and row.get(r, [0, 0])[0] > 0 and row[r][1] == 0 for r in rungs if r != "ai")
    if "ai" in order and failed_l0:
        l1 = [r for r in order if r != "ai" and LEVEL.get(r, "L0") == "L1"]
        if l1:
            order.remove("ai")
            order.insert(order.index(l1[0]), "ai")
    return order


def _ai_pending(ai_dir, gaps):
    """True when an unapplied action file exists for one of `gaps`."""
    return bool(_ai_pending_names(ai_dir, gaps))


# PHASE3G-H2: an hs-pair gap takes centre lines, rips, give-ups, a stub path and a swap proposal (B7); other classes keep all
AI_PAIR_HS_KINDS = ("pair_path", "rip", "give_up", "path", "swap_pins")


def _ai_filter_pair(gap, actions):
    """Actions a pair gap may apply. An hs-pair gap keeps AI_PAIR_HS_KINDS only; a low-speed "pair" gap (and any
    other class) keeps all validated actions. Dropped kinds are reported on stderr."""
    if gap.get("class") != "hs-pair":
        return list(actions)
    kept = []
    for x in actions:
        if x["do"] in AI_PAIR_HS_KINDS:
            kept.append(x)
        else:
            print(f"ai: hs-pair gap {gap['id']}: dropped {x['do']} action (pair_path, rip, give_up, path only)", file=sys.stderr)
    return kept


def ai_key(gap):
    """Stable key of a gap for AI packet/action files and the AI budget: kind and first net. PHASE3G G1: a pair gap is
    keyed by its pair (one packet per pair, whatever its kind)."""
    if str(gap["kind"]).startswith("pair-") and len(gap.get("nets") or []) == 2:
        return f"pair:{gap['nets'][0]}/{gap['nets'][1]}"
    return f"{gap['kind']}:{(gap.get('nets') or ['?'])[0]}"


def _consume(act):
    """PHASE3H A6: an answer file leaves its pending name as <sid>.action.used.<n>.json (n = next free)."""
    stem = act[:-len(".json")]
    n = 1
    while os.path.exists(f"{stem}.used.{n}.json"):
        n += 1
    os.replace(act, f"{stem}.used.{n}.json")


def _r_ai(base, out, gaps, ctx):
    """AI rung over the gaps of one kind. The first gap (least used first, PHASE3H A1) with an answer file and budget
    left is applied as one transaction (status applied, gap_id names it). Otherwise a packet is written for each gap
    with budget left (at most [ai] packets per call): status packet, n = count. An invalid answer (PHASE3H A4) changes
    nothing and is consumed uncharged (status invalid). close guards the board like any other rung."""
    if ctx.get("ai_mode") == "off":
        return {"status": "ai off"}
    if ctx.get("ai_mode") == "session":                        # PHASE3I F4 (B2): regions with briefs for an agent
        return _r_ai_session(base, out, gaps, ctx)
    used = ctx["ai_used"]
    budget = _ai_budget(ctx.get("cons"))                       # PHASE3F B5
    ai_dir = ctx["ai_dir"]
    os.makedirs(ai_dir, exist_ok=True)
    legs = set(ctx.get("pair_gap_legs") or ())                 # PHASE3H A1: an open on a live pair's leg is the pair's
    todo, seen = [], set()
    for g in gaps:                 # one entry per AI key: DRC can list two gaps of one net under one key
        key = ai_key(g)
        if key in seen or (str(g["kind"]).startswith("open-") and legs & set(g.get("nets") or [])):
            continue
        seen.add(key)
        sid = critroute._safe(key)[:80]
        if used.get(key, 0) < budget:
            todo.append((g, key, sid))
        elif os.path.exists(os.path.join(ai_dir, sid + ".action.json")):   # PHASE3H A9: say why the answer is ignored
            _feedback_note(ai_dir, sid, refused=f"ai budget {used.get(key, 0)}/{budget}")
    if not todo:
        return {"status": "ai budget"}
    todo.sort(key=lambda t: (used.get(t[1], 0),                          # PHASE3H A1: least used, then packet missing first
                             0 if not os.path.exists(os.path.join(ai_dir, t[2] + ".packet.json")) else 1))
    for gap, key, sid in todo:
        act = os.path.join(ai_dir, sid + ".action.json")
        if not os.path.exists(act):
            continue
        a = load_ai_action(act, base, ctx.get("protect", ()), hs_legs=ctx.get("hs_legs", ()),
                           open_legs=ctx.get("open_legs", ()), cons_path=ctx.get("cons_path"))
        a["gap_id"] = gap["id"]
        if a.get("dropped"):                                    # PHASE3H A4: nothing applied, nothing charged
            _feedback_note(ai_dir, sid, dropped=a["dropped"][:8], refused="action invalid")
            _consume(act)
            return {"status": "invalid", "commit": False, "why": "action invalid: " + a["dropped"][0]["reason"],
                    "tokens": 0, "gap_id": gap["id"]}
        a["actions"] = _ai_filter_pair(gap, a["actions"])   # PHASE3G G1 / H2: hs-pair gaps are restricted
        try:
            status, commit, why = _apply_ai(base, out, a, ctx, sid)
        except (KeyboardInterrupt, SystemExit):                 # PHASE3H A6: the answer stays pending
            _feedback_note(ai_dir, sid, refused="error: interrupted")
            raise
        except Exception as e:                                  # PHASE3H A6: consumed, uncharged, reason in feedback
            _feedback_note(ai_dir, sid, refused=f"error: {e}"[:160])
            _consume(act)
            return {"status": "error", "commit": False, "why": f"error: {e}"[:160], "tokens": a["tokens"],
                    "gap_id": gap["id"]}
        _consume(act)                                           # PHASE3H A6: charged only after _apply_ai returned
        if status == "applied" and a.get("executed"):           # PHASE3H A9: an applied answer that ran an action
            used[key] = used.get(key, 0) + 1
        return {"status": status, "commit": commit, "why": why, "tokens": a["tokens"], "gap_id": gap["id"]}
    n = 0
    for gap, key, sid in todo[:_ai_packets(ctx.get("cons"))]:
        pk = gap_packet(base, gap, ctx, sid=sid)
        if gap["kind"] == "pair-asym" and pk.get("conn") is None:      # PHASE3H B10: a proposal row, no packet
            _write_topology(ai_dir, sid, gap["id"])
            continue
        with open(os.path.join(ai_dir, sid + ".packet.json"), "w") as fh:
            fh.write(json.dumps(pk))
        n += 1
    return {"status": "packet", "n": n} if n else {"status": "no packet", "n": 0}


def _sessions_load(ai_dir):
    """PHASE3I F4: the regions of the session mode (ai/sessions.json); [] when there are none."""
    try:
        with open(os.path.join(ai_dir, "sessions.json")) as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _r_ai_session(base, out, gaps, ctx):
    """PHASE3I F4 (B2, brief phase): `[ai] mode = "session"`. The open gaps with AI budget left that are in no open region
    are clustered (cluster_regions); each region gets its own session board and a brief for an agent, which lays copper
    there and writes <rid>.done.json. Returns "packet" (close: result packet, exit 5); nothing is written to `out`."""
    from . import handroute as HR
    ai_dir = ctx["ai_dir"]
    os.makedirs(os.path.join(ai_dir, "sessions"), exist_ok=True)
    budget = _ai_budget(ctx.get("cons"))
    used = ctx["ai_used"]
    regs = _sessions_load(ai_dir)
    for reg in regs:                                          # PHASE3I F5 (merge phase): the oldest answered region first
        if reg.get("status") == "open" and os.path.exists(os.path.join(ai_dir, reg["rid"] + ".done.json")) \
                and any(g["id"] in reg.get("gaps", []) for g in gaps):
            return _r_ai_merge(base, out, gaps, ctx, regs, reg)
    busy = {gid for r in regs if r.get("status") == "open" for gid in r.get("gaps", [])}
    todo = [g for g in gaps if g["id"] not in busy and used.get(ai_key(g), 0) < budget]
    by_id = {g["id"]: g for g in todo}
    made = []
    for reg in cluster_regions(todo)[:_ai_packets(ctx.get("cons"))]:
        grp = [by_id[i] for i in reg["gaps"]]
        rid = critroute._safe("region-" + reg["gaps"][0])[:60]
        n = 1
        while os.path.exists(os.path.join(ai_dir, "sessions", f"{rid}-{n}.kicad_pcb")):
            n += 1
        sess = os.path.join(ai_dir, "sessions", f"{rid}-{n}.kicad_pcb")
        h = HR.session_begin(base, sess)
        packets, rejected = [], []
        for g in grp:
            sid = critroute._safe(ai_key(g))[:80]
            if len(packets) < 3:
                pk_path = os.path.join(ai_dir, sid + ".packet.json")
                with open(pk_path, "w") as fh:
                    fh.write(json.dumps(gap_packet(base, g, ctx, sid=sid)))
                packets.append(pk_path)
            try:
                with open(os.path.join(ai_dir, sid + ".feedback.json")) as fh:
                    refused = (json.load(fh) or {}).get("refused")
            except (OSError, ValueError, AttributeError):
                refused = None
            if refused:
                rejected.append({"gap": g["id"], "refused": refused})
        brief = os.path.join(ai_dir, rid + ".brief.json")
        _write_json(brief, {
            "v": 1, "region": rid, "session": sess, "ledger": os.path.join(h["session"], "ledger.txt"),
            "box": reg["box"], "nets": reg["nets"],
            "gaps": [{"id": g["id"], "kind": g["kind"], "class": g["class"], "nets": g.get("nets") or [], "at": g.get("at")}
                     for g in grp],
            "packets": packets, "rejected": rejected,
            "tactics": ["suggest", "lay", "lay --stitch", "shove (unlocked only)", "undo"],
            "done": f"write {rid}.done.json = {{\"ledger\": <path>}} when finished"})
        regs.append({"rid": rid, "status": "open", "gaps": reg["gaps"], "nets": reg["nets"], "box": reg["box"],
                     "session": sess, "brief": brief, "base_fp": versions.copper_fingerprint(base)})
        made.append(brief)
    _write_json(os.path.join(ai_dir, "sessions.json"), regs)
    return {"status": "packet", "n": len(made), "briefs": made} if made else {"status": "no packet", "n": 0}


def _r_ai_merge(base, out, gaps, ctx, regs, reg):
    """PHASE3I F5 (B2, merge phase): one answered region. Its session ledger is replayed onto ai/merge/<rid>/ from the
    board the region was briefed on (H.replay: every lay through the guard again). Nothing applied -> the region is
    refused, its gaps get the reasons, the done file is retired. Else `out` is the merged board (close's guard judges it
    like any rung; a guard refusal leaves base untouched and the gaps free for a new brief), the requeued rows are
    feedback notes, and the region's gaps are charged one AI try each."""
    from . import handroute as HR
    ai_dir, used = ctx["ai_dir"], ctx["ai_used"]
    done = os.path.join(ai_dir, reg["rid"] + ".done.json")
    grp = [g for g in gaps if g["id"] in reg["gaps"]]
    sids = {g["id"]: critroute._safe(ai_key(g))[:80] for g in grp}
    try:
        with open(done) as fh:
            ledger = json.load(fh)["ledger"]
        if not isinstance(ledger, str) or not os.path.isfile(ledger):
            raise ValueError("ledger path missing")
    except (OSError, ValueError, KeyError, TypeError) as e:
        # L1: an unreadable done file refuses its region (charged like any refused merge), never raises
        why = f"done file unreadable: {type(e).__name__}"
        for g in grp:
            used[ai_key(g)] = used.get(ai_key(g), 0) + 1
            _feedback_note(ai_dir, sids[g["id"]], refused=f"merge: {why}"[:160])
        reg["status"] = "refused"
        _consume(done)
        _write_json(os.path.join(ai_dir, "sessions.json"), regs)
        return {"status": "refused", "why": why}
    mdir = os.path.join(ai_dir, "merge", reg["rid"])
    shutil.rmtree(mdir, ignore_errors=True)               # H2: a reused merge dir never keeps stale m<N> files
    os.makedirs(mdir, exist_ok=True)
    HR._copy_board(base, os.path.join(mdir, "m0.kicad_pcb"))
    r = HR.replay(ledger, mdir, constraints=ctx.get("cons"))
    if not r["applied"]:
        why = r["requeue"][0]["why"] if r["requeue"] else "nothing to merge"
        for g in grp:
            _feedback_note(ai_dir, sids[g["id"]], refused=f"merge: {why}"[:160])
        reg["status"] = "refused"
        for g in grp:                                       # L2: a refused merge is charged one AI try per gap
            used[ai_key(g)] = used.get(ai_key(g), 0) + 1
        out_status = {"status": "refused", "why": str(why)[:160]}
    else:
        HR._copy_board(r["board"], out)
        reg["status"] = "merged"
        for row in r["requeue"]:
            for g in grp:
                if row["net"] in (g.get("nets") or []):
                    _feedback_note(ai_dir, sids[g["id"]], refused=str(row["why"])[:160])
        for g in grp:
            used[ai_key(g)] = used.get(ai_key(g), 0) + 1
        out_status = {"status": "applied", "applied": len(r["applied"]), "requeue": len(r["requeue"]),
                      "gap_id": grp[0]["id"] if grp else None}
    _consume(done)
    _write_json(os.path.join(ai_dir, "sessions.json"), regs)
    return out_status


def _r_tune(base, out, gaps, ctx):
    """PHASE3H B8: skew-only pairs: the shorter leg (slave) is meandered toward its partner (tune.tune_pairs, with the
    pair's skew limit). The candidate goes through close's guard; no output when nothing was tuned."""
    from . import tune as TU, pairaudit as PA_
    pairs, limits = {}, {}
    for g in gaps:
        if g["kind"] != "pair-skew" or len(g.get("nets") or []) != 2 or not _skew_fails(g):
            continue                                                     # a pair inside its maximum is not tuned (D71cx)
        blk = _pair_block(base, ctx["cons"], g)
        legs = blk.get("legs") or {}
        lim = blk.get("skew_limit_mm")
        if len(legs) != 2:
            continue
        slave = min(legs, key=legs.get)
        master = next(n for n in g["nets"] if n != slave)
        if lim is None and not PA_.ps_budget(ctx["cons"], master):      # a ps budget is graded in ps inside tune_pairs (D71cx)
            continue
        pairs[slave], limits[master] = master, float("inf") if lim is None else lim
    if not pairs:
        return {"status": "no pair"}
    res = {}
    TU.tune_pairs(base, out, pairs, limit_mm=lambda m: limits[m], budget=ctx["budget_s"], result=res, cons=ctx["cons"])
    return {"status": "ran" if os.path.exists(out) else "no change", "pairs": len(pairs), "stopped": res.get("stopped")}


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
    rails = set(_rail_widths(base, ctx["cons"], signal_layers(base), 0.0))     # PHASE3F A4: a rail is never ripped
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
                if n and not t.get("locked") and n not in protect and n not in targets and n not in rails:
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
                _procs.run(cmd, capture_output=True, text=True, timeout=min(900, ctx["budget_s"]), cwd=ctx["work"])
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


def _r_pairgroup(base, out, gaps, ctx):
    """PHASE3C C.3: the pairs of the target gaps re-laid per connector group (pairsonly.regroup). No candidate when
    nothing is laid: `out` is removed, so the attempt is 'no output' and the board stays."""
    from . import pairsonly as PO, routediag as RD, netclass as NC, pairaudit as PA
    want = {n for g in gaps if g["kind"].startswith("pair-") for n in g["nets"]}
    cons = ctx["cons"]
    B = RD.load(base)
    pairs = [p for p in PA.pair_list(NC.classify(B, cons, None)) if set(p) & want]
    if not pairs:
        return {"status": "no pair"}
    budget = min(PO.PER_PAIR_S * len(pairs), float(ctx["budget_s"]))     # PHASE3E 7: per pair, capped by the close budget left
    if budget <= 0:
        return {"status": "no budget", "pairs": len(pairs)}
    rep = PO.regroup(base, out, cons, None, pairs, log=ctx["log"], budget_s=budget)
    laid = sum(len(g["laid"]) for g in rep["groups"].values())
    if not laid:
        if os.path.exists(out):
            os.remove(out)
        return {"status": "nothing laid", "pairs": len(pairs)}
    return {"status": "ran", "laid": laid, "failed": len(rep["failed"]), "pairs": len(pairs)}


RUNGS = {"joingaps": _r_joingaps, "pourisland": _r_pourisland, "edgesliver": _r_edgesliver, "patch": _r_patch, "patch_rip": _r_patch_rip,
         "handrepair": _r_handrepair, "krt": _r_krt, "region": _r_region, "drcfix": _r_drcfix, "launder": _r_launder,
         "snapiter": _r_snapiter, "krt_region": _r_krt_region, "ai": _r_ai, "prune": _r_prune,
         "pairgroup": _r_pairgroup, "relocate": _r_relocate, "maskcut": _r_maskcut, "powerwiden": _r_powerwiden, "railpour": _r_railpour,
         "powervias": _r_powervias, "tune": _r_tune}

# pour-tie, planetie, pair and RF rungs join in phase 3 (FLOW-REDESIGN 5)
DISPATCH = {
    "short": ["drcfix"],
    "open-gap": ["joingaps", "patch"],
    "open-pour": ["edgesliver", "pourisland", "region", "ai"],     # PHASE3I B12: edge-band fill sliver = island removal
    "open-free": ["prune", "patch", "handrepair", "krt", "relocate", "krt_region", "region", "patch_rip", "ai"],   # relocate (L1, moves a part) after the L0 router
    "clearance": ["drcfix", "region"],
    "dangling": ["prune", "launder"],
    "via-offcentre": ["snapiter"],
    "mask-opening": ["maskcut"],            # PHASE3F A7: re-cut after routing; the close guard judges it
    "power-width": ["powerwiden", "railpour"],     # PHASE3G G4: widen in place, then pour, then (power-vias) vias
    "power-vias": ["powervias"],
    "pair-open": ["pairgroup", "ai"],     # PHASE3G G1: an agent centre line (pair_path) after the group re-lay
    "pair-asym": ["pairgroup", "ai"],
    "pair-skew": ["pairgroup", "tune", "ai"],
    "pair-impedance": ["pairgroup", "ai"],     # PHASE3I F2: a pair off its target impedance is re-laid as a group     # PHASE3H B8: tune_pairs (shorter leg meandered) before the agent
}
LEVEL = {"relocate": "L1", "region": "L1", "patch_rip": "L1", "ai": "L1", "krt_region": "L1", "krt": "L0", "pairgroup": "L1"}  # every other rung is L0


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

_CTX_JSON_KEYS = ("cons_path", "kicad_cli", "work", "budget_s", "krt_last", "ai_mode", "ai_dir", "protect", "pair_fallback")
JOB_OUT_KRT = "_krt_last"


def _call_rung(name, base, out, gaps, ctx):
    """Run one rung. `ai` and ctx["inproc"] runs stay in-process; every other rung runs in a child process
    (`python -m fluxplace.gaps --rung-job JOB`) so a native crash is a RuntimeError, not a dead close run."""
    if name == "ai" or ctx.get("inproc"):
        return RUNGS[name](base, out, gaps, ctx)
    job, jout = out + ".job.json", out + ".job.out.json"
    try:
        cj = {k: ctx.get(k) for k in _CTX_JSON_KEYS}
        if cj.get("protect") is not None:
            cj["protect"] = sorted(cj["protect"])          # a set is not JSON; the child rebuilds it
        with open(job, "w") as fh:
            json.dump({"name": name, "base": base, "out": out, "gaps": gaps, "ctx": cj}, fh, default=str)
        try:
            p = _procs.run([sys.executable, "-m", "fluxplace.gaps", "--rung-job", job], capture_output=True,
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
    if cj.get("protect") is not None:
        cj["protect"] = set(cj["protect"])
    cons = C.load(cj.get("cons_path")) if cj.get("cons_path") else {}
    ctx = dict(cj, cons=cons, log=lambda m: print(m, file=sys.stderr), inproc=True)
    summ = RUNGS[j["name"]](j["base"], j["out"], j["gaps"], ctx) or {}
    summ = dict(summ, **{JOB_OUT_KRT: ctx.get("krt_last")})
    with open(j["out"] + ".job.out.json", "w") as fh:
        json.dump(summ, fh, default=str)
    return 0


def gate_measure(board, work, cons_path=None, cons=None, kicad_cli="kicad-cli", kinds=None):
    """A5: a gate is a MEASURE, not a repair. One refill + DRC + gap classification on a TEMP copy of `board`; no rung runs and nothing
    but work/stop.json is written (a `best.kicad_pcb`, `gaps.json` and memo of an earlier `close` in `work` are never touched, and the
    input board is never refilled or saved).
    -> {"stop": "clean" | "open", "left": [gap rows in scope], "start": vitals, "review_failed": [...]}.  `open` also when a review source
    crashed (review_failed): a pair audit that raised is missing rows, never clean.  Exit 5 on `open` (pipeline)."""
    import tempfile
    os.makedirs(work, exist_ok=True)
    cons = cons or C.load(cons_path)
    with tempfile.TemporaryDirectory(prefix="fxgate-") as td:
        tmp = os.path.join(td, "m.kicad_pcb")
        _copy(board, tmp)
        _sidecars(board, tmp)
        try:
            m = _remeasure(tmp, kicad_cli)
        except _RefillError:
            m = dict(measure(tmp, kicad_cli))
        _classify_into(m, cons, tmp)
    left = [g for g in m["gaps"] if kinds is None or in_scope(g, kinds)]
    failed = list(m.get("review_failed") or [])
    stop = "open" if (left or failed) else "clean"
    rows = [{"id": g["id"], "kind": g["kind"], "class": g.get("class"), "nets": g.get("nets"), "at": g.get("at")} for g in left]
    res = {"stop": stop, "exit": 0 if stop == "clean" else 5, "measure_only": True, "start": _vitals(m), "left": rows,
           "review_failed": failed}
    _write_json(os.path.join(work, "stop.json"), res)
    return dict(res, best=board)


def close(board, work, cons_path=None, cons=None, kicad_cli="kicad-cli", budget_min=None, max_rounds=40,
          attempts=None, runs=None, log=print, inproc=False, kinds=None):
    """Gap loop on a copy of `board` in `work`. Returns the stop dict plus "best"; writes work/best.kicad_pcb,
    work/gaps.json, work/stop.json, work/ledger-rows.md."""
    os.makedirs(work, exist_ok=True)
    best = os.path.join(work, "best.kicad_pcb")
    _copy(board, best)
    _sidecars(board, best)
    from . import boardtext as BT                       # PHASE3I F9: item ids unique on the work copy (never the input)
    rc = BT.reuuid_collisions(best)
    if rc["collisions"]:
        log(f"close: {rc['collisions']} item-id collision(s), {rc['changed']} uuid(s) renewed on the work copy")
    cons = cons or C.load(cons_path)
    budget_min = budget_min or (cons.get("flow") or {}).get("budget_min", 120)
    explicit = attempts is not None
    attempts = attempts or calllog.attempts_path(board)
    run_id = "r-" + time.strftime("%Y%m%d-%H%M%S")
    tool = _tool()
    cons_hash = _cons_hash(cons_path)
    t0 = time.time()
    budget_s = budget_min * 60
    try:
        m = _remeasure(best, kicad_cli)
    except Exception as e:                               # PHASE3E 3: the initial measure must not kill close()
        log(f"initial refill failed ({str(e)[:80]}); measuring the copy unrefilled")
        m = dict(measure(best, kicad_cli))
    _classify_into(m, cons, best)
    protect = _protect(best, cons)
    from . import boardwrite as BW
    man = BW.fresh_manifest(board, log)                  # A3: the input board's protected copper (empty: nothing protected yet; stale: re-seeded)
    if man["nets"]:
        BW.seed(best, keep=man, locked=False)            # the work copy carries it, so a release is a logged edit of that file
    man0 = json.loads(json.dumps(man))                   # the input's manifest: the baseline the pipeline's commit point judges the final board against
    released = {}                                        # {net: reason} released by an accepted attempt (cumulative; handed to the commit point)
    snaps = []                                           # [(label, path, frozenset(released))] after each accepted attempt, newest last
    relax = relax_nets(m["gaps"])
    start = _vitals(m)
    board_fp0 = versions.copper_fingerprint(best)
    if runs is None and calllog.enabled():
        runs = os.path.join(_repo_root(), "docs", "corpus", "runs.jsonl")
    tries = []

    state_path = os.path.join(work, "gaps.json")
    memo, memo_err = set(), set()                        # PHASE3I F3: an error is remembered in this pass only
    ai_used = {}
    yields = {}
    stall_s = float((cons.get("flow") or {}).get("stall_min", STALL_MIN)) * 60
    last_acc = [t0]
    fallback = []                                        # PHASE3F A5: [p, n] of refused non-gated pairs (KRT legs)
    try:
        with open(state_path) as fh:
            st = json.load(fh)
        ai_used = {k: int(v) for k, v in (st.get("ai_used") or {}).items() if st.get("tool") == tool}   # PHASE3H A9
        fallback = [list(x) for x in st.get("fallback", [])]
        yields = {k: {r: list(v) for r, v in d.items()} for k, d in (st.get("yields") or {}).items()}
        if st.get("tool") == tool and st.get("cons_hash") == cons_hash:
            memo = {tuple(x) for x in st.get("memo", [])}
    except (OSError, ValueError, AttributeError):
        pass

    def vis(gs):                                         # PHASE3I F2: the gate's scope selects targets; the guard sees all gaps
        return gs if kinds is None else [g for g in gs if in_scope(g, kinds)]

    def save(r):
        _write_json(state_path, {"gaps": m["gaps"], "memo": sorted(list(k) for k in memo), "round": r,
                                 "tool": tool, "cons_hash": cons_hash, "ai_used": ai_used, "fallback": fallback,
                                 "yields": yields})

    stop, level, n_att, last = None, "L0", 0, {}
    n_packets, krt_state = 0, {}
    no_gain = int((cons.get("flow") or {}).get("no_gain_rounds", NO_GAIN_ROUNDS))
    stale = 0
    ai_mode = (cons.get("ai") or {}).get("mode", "packet")

    def attempt(r, kind, rung, targets, sub=False):
        """One try. Returns True when the candidate was accepted (m is then the new base)."""
        nonlocal m, n_att, level, n_packets, protect, relax, man
        nets = _nets(targets)
        fp = versions.copper_fingerprint(best)
        # PHASE3I B3: memo key = rung, kind, gap signature and the copper in each gap window (not the whole board)
        sigs = sorted(f"{g['kind']}|{','.join(sorted(g.get('nets') or []))}|{g.get('at')}" for g in targets)
        wins = sorted(window_hash(best, g) for g in targets)
        key = (rung, kind, hashlib.sha1(json.dumps([sigs, wins]).encode()).hexdigest())
        if sub:                                              # PHASE3F A3: a split half has its own memo key
            key = key + ("sub:" + hashlib.sha1(",".join(sorted(g["id"] for g in targets)).encode()).hexdigest(),)
        ai_names = _ai_pending_names(ctx_dir_ai(work), targets) if rung == "ai" else []
        if rung == "ai":                                     # PHASE3G-H2: an answer file refused once must not block the next one
            # PHASE3I B3: an answer is keyed by its content, so a rewritten answer with the same name is tried again
            key = key + ("ai:" + ",".join(f"{n}@{hashlib.sha1(open(os.path.join(ctx_dir_ai(work), n), 'rb').read()).hexdigest()[:12]}"
                                          for n in ai_names),)
        vec0, t_try = score(m, m["gaps"]), time.time()
        out = os.path.join(work, "close", f"r{r}-{kind}-{rung}.kicad_pcb")
        after, runs, why, summ, cleanup, cand = None, 0, "", {}, [], None
        if (key in memo or key in memo_err) and not ai_names:   # PHASE3H A8: a pending answer is always tried
            result, why = "skipped", "memo"
        else:
            try:
                os.makedirs(os.path.dirname(out), exist_ok=True)
                _clean_board(out)
                _sidecars(best, out)
                ctx = {"cons": cons, "cons_path": cons_path, "kicad_cli": kicad_cli, "work": work,
                       "budget_s": min(max(0.0, budget_s - (time.time() - t0)), _rung_cap(cons)),
                       "log": log, "attempts": attempts,
                       "ai_dir": os.path.join(work, "ai"), "ai_used": ai_used, "ai_mode": ai_mode,
                       "krt_last": krt_state.get("last"), "protect": protect, "pair_fallback": fallback,
                       "hs_legs": sorted({n for g in m["gaps"] if g.get("class") == "hs-pair" for n in g.get("nets", [])}),
                       "pair_gap_legs": sorted({n for g in m["gaps"] if str(g["kind"]).startswith("pair-")
                                                for n in g.get("nets", [])}),
                       "open_legs": sorted(n for n, c in (m.get("opens_by_net") or {}).items() if c)}   # PHASE3H B3
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
                    cm = _remeasure(out, kicad_cli)
                    runs = 1
                    _classify_into(cm, cons, out)
                    cand = score(cm, cm["gaps"])          # PHASE3F A2: what the candidate cost, accepted or not
                    final = out
                    ok, why = _accept(m, best, cm, out, [g["id"] for g in targets], cm["gaps"], kind,
                                      _r5_set(kind, targets, protect, relax, set(_pair_fallback_legs(ctx))))
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
                        cm = _remeasure(final, kicad_cli)
                        runs += 1
                        _classify_into(cm, cons, final)
                        cand = score(cm, cm["gaps"])
                        ok, why = _accept(m, best, cm, final, [g["id"] for g in targets], cm["gaps"], kind,
                                          _r5_set(kind, targets, protect, relax, set(_pair_fallback_legs(ctx))))
                    if ok and man0["nets"]:                 # A3: protected copper (boardwrite manifest) stays; a pair rung releases its target pairs
                        # least release: only the target pair nets this candidate re-laid (an untouched target pair is not released)
                        rel = _release_nets(kind, targets, man) & set(BW.changed(final, man))
                        # the same verify the final commit point runs: against the INPUT manifest with every release so far
                        v0 = BW.verify(best, final, release=set(released) | rel, manifest=man0)
                        if not v0["ok"]:
                            ok, why = False, ("input manifest: " + v0["why"])[:120]
                        else:
                            v = BW.commit(best, final, best, release=rel, manifest=man, reason=f"{kind} {rung}",
                                          complete={n: man["nets"][n].get("why", "kept") for n in rel if n in man["nets"]
                                                    and man["nets"][n].get("why") != "locked"})   # re-sealed: a released pair stays protected
                            if not v["written"]:
                                ok, why = False, v["why"][:120]
                            else:
                                man = BW.load_manifest(best)
                                for n in rel:
                                    released[n] = f"{kind} {rung} (attempt {n_att + 1})"
                    elif ok:
                        _copy(final, best)
                    if ok:
                        if man0["nets"]:
                            sp = os.path.join(work, "accepted", f"a{n_att + 1}.kicad_pcb")
                            os.makedirs(os.path.dirname(sp), exist_ok=True)
                            _copy(best, sp)
                            _sidecars(best, sp)
                            snaps.append((f"attempt {n_att + 1} {kind} {rung}", sp, frozenset(released)))
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
            except _RefillError as e:
                result, why = "refused", str(e)[:120]
            except (Exception, SystemExit) as e:
                result, why = "error", f"{type(e).__name__}: {e}"[:120]
            finally:
                _clean_board(out)
        if result == "packet":
            n_packets += 1
        if result not in ("skipped", "packet"):              # PHASE3I B3: per-kind, per-rung yield; F3 R17: answers only
            yl = yields.setdefault(kind, {}).setdefault(rung, [0, 0])
            yl[0] += 1
            if result in ("closed", "partial"):
                yl[1] += 1
                last_acc[0] = time.time()
        if rung == "ai" and result in ("closed", "partial") and summ.get("gap_id"):   # PHASE3H B5: an accept clears the refusal
            tg = next((g for g in targets if g["id"] == summ["gap_id"]), None)
            if tg is not None:
                _feedback_note(ctx_dir_ai(work), critroute._safe(ai_key(tg))[:80], refused=None)
        if rung == "ai" and result == "refused" and summ.get("gap_id"):   # PHASE3F B4: refusal reason for the next packet
            tg = next((g for g in targets if g["id"] == summ["gap_id"]), None)
            if tg is not None:
                _feedback_note(ctx_dir_ai(work), critroute._safe(ai_key(tg))[:80], refused=str(why)[:160])
        if result == "refused" and not ai_names:             # PHASE3H A3 / A8
            memo.add(key)
        if result == "error" and not ai_names:               # PHASE3I F3 R7: a crash or timeout is not a refusal
            memo_err.add(key)
        if rung == "pairgroup" and result == "refused":      # PHASE3F A5: a refused non-gated pair goes to the router
            for g in targets:
                if g["class"] == "pair" and len(g["nets"]) == 2 and g["nets"] not in fallback:
                    fallback.append(list(g["nets"]))
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "run": run_id,
               "board_fp": fp[:8] if len(fp) > 8 else fp, "tool": tool, "stage": "close",
               "level": LEVEL.get(rung, "L0"),
               "gap": {"id": summ.get("gap_id") or targets[0]["id"], "kind": kind, "class": targets[0]["class"], "n": len(targets)},
               "method": rung, "tier": {"scope": "board", "effort": 1}, "in": {"nets": len(nets)},
               "result": result, "why": why, "score": [vec0, after], "cand": cand,
               "s": round(time.time() - t_try, 1),
               "drc_runs": runs, "precheck": None, "cleanup": cleanup,
               "ai_tokens": int(summ.get("tokens") or 0) if rung == "ai" else 0}
        if explicit or calllog.enabled():
            calllog.append(rec, attempts)
        tries.append({"result": result, "method": rung, "kind": kind, "why": why, "s": rec["s"]})
        n_att += 1
        last[kind] = {"method": rung, "why": why}
        return result in ("closed", "partial")

    def sub_attempt(r, kind, rung, ts, left):
        """PHASE3F A3: a refused batch (guard reason) is tried as two halves; a refused half is split again while
        `left[0]` (SPLIT_MAX) and the close budget allow. Returns True when any sub-attempt was accepted."""
        got = False
        for h in _halves(ts):
            ids = {g["id"] for g in h}
            cur = [g for g in vis(m["gaps"]) if g["id"] in ids and g["kind"] == kind]   # re-read: an accepted half moved m
            if not cur or left[0] <= 0 or time.time() - t0 > budget_s:
                continue
            left[0] -= 1
            ok = attempt(r, kind, rung, cur, sub=True)
            got = ok or got
            if not ok and len(cur) > 1 and tries[-1]["result"] == "refused" and _guard_refusal(tries[-1]["why"]):
                got = sub_attempt(r, kind, rung, cur, left) or got
        return got

    def attempt_split(r, kind, rung, targets):
        """attempt(), then the split of a refused batch (PHASE3F A3)."""
        acc = attempt(r, kind, rung, targets)
        if rung not in SPLIT_RUNGS:                      # whole-board rungs and `ai` ignore the subset: a split repeats them
            return acc
        if acc or len(targets) < 2 or tries[-1]["result"] != "refused" or not _guard_refusal(tries[-1]["why"]):
            return acc
        return sub_attempt(r, kind, rung, targets, [SPLIT_MAX])

    # PHASE3H A3 / A7: answers first. Every pending AI answer gets its try before any non-ai rung; targets are re-read
    # after each accept. A kind whose answer was consumed without an accept is asked again, so its packet has the feedback.
    ai_refused = []
    for kind in ORDER:
        if "ai" not in DISPATCH.get(kind, []):
            continue
        for _ in range(len(_ai_pending_names(ctx_dir_ai(work), [g for g in vis(m["gaps"]) if g["kind"] == kind]))):
            tg = [g for g in vis(m["gaps"]) if g["kind"] == kind]
            if not _ai_pending(ctx_dir_ai(work), tg) or time.time() - t0 > budget_s:
                break
            if not attempt(0, kind, "ai", tg) and kind not in ai_refused:
                ai_refused.append(kind)
            save(0)
    for kind in ai_refused:
        tg = [g for g in vis(m["gaps"]) if g["kind"] == kind]
        if tg and time.time() - t0 <= budget_s:
            attempt(0, kind, "ai", tg)
            save(0)
    for r in range(max_rounds):
        if kinds is None and not any(g["kind"] in DISPATCH or g["kind"] == "pair-unmeasured" for g in m["gaps"]) or kinds is not None and not vis(m["gaps"]):
            stop = "clean"                               # a scoped gap with no rung keeps the gate open: stop "flat"
            break
        hit = False
        packets_before = n_packets
        round_vec0 = score(m, m["gaps"])                 # PHASE3E 9: the round's start, for the no-gain counter
        deferred = []
        for kind in ORDER:
            targets = [g for g in vis(m["gaps"]) if g["kind"] == kind]
            if not targets or kind not in DISPATCH:
                continue
            for rung in _rank(kind, DISPATCH[kind], yields):
                if rung == "ai" and not _ai_pending(ctx_dir_ai(work), targets):
                    deferred.append(kind)                    # a packet changes no board in this pass: it is written after every kind
                    continue                                 # had its own rungs (W15: 9 pair packets took 9 of 30 min, opens got none)
                if time.time() - t0 > budget_s:
                    stop = "budget"
                    break
                if time.time() - last_acc[0] > stall_s:      # PHASE3I B3: no accept for stall_min: stop, do not re-run
                    for k2 in ORDER:                         # PHASE3I F3 R8: every kind with gaps gets its ai packet first
                        tg2 = [g for g in vis(m["gaps"]) if g["kind"] == k2]
                        if tg2 and "ai" in DISPATCH.get(k2, []):
                            attempt(r, k2, "ai", tg2)
                            save(r)
                    stop = "packets" if n_packets > packets_before else "stalled"
                    break
                accepted = attempt_split(r, kind, rung, targets)
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
        for kind in deferred:                                # the packets of this round, kinds in ORDER
            if stop or hit:                                  # after an accept the next round re-reads the gaps first
                break
            if time.time() - t0 > budget_s:
                stop = "budget"
                break
            tg = [g for g in vis(m["gaps"]) if g["kind"] == kind]
            if tg:
                attempt(r, kind, "ai", tg)
                save(r)
        if stop:
            break
        if not hit:
            stop = "packets" if n_packets > packets_before else "flat"
            break
        vec = score(m, m["gaps"])
        # PHASE3E 9: a round is a gain only when a term other than `quality` (the last slot) went down; accepts that
        # only polish quality count as no gain, so slow polishing stops
        gain = any(v < v0 for v, v0 in zip(vec[:-1], round_vec0[:-1]))
        stale = 0 if gain else stale + 1
        EV.iteration("close", r + 1, max_rounds, {"score": [round(x, 3) for x in vec], "opens": m["opens"],
                                                  "shorts": m["shorts"]})
        if stale >= no_gain:
            stop = f"no gain {no_gain} rounds"
            break
    else:
        stop = "rounds"

    broke = None
    if man0["nets"]:                                     # the final board must pass the commit point's verify; else the newest accepted state that does
        vf = BW.verify(board, best, release=set(released), manifest=man0)
        if not vf["ok"]:
            log(f"close: final board fails protection ({vf['why'][:100]}); falling back to the newest accepted state that verifies")
            for i in range(len(snaps) - 1, -1, -1):
                label, sp, rl = snaps[i]
                if BW.verify(board, sp, release=set(rl), manifest=man0)["ok"]:
                    broke = snaps[i + 1][0] if i + 1 < len(snaps) else f"an edit after {label}"
                    _copy(sp, best)
                    _sidecars(sp, best)
                    released = {n: w for n, w in released.items() if n in rl}
                    break
            else:
                broke = snaps[0][0] if snaps else "input"
                _copy(board, best)
                _sidecars(board, best)
                released = {}
            log(f"close: kept {len(released)} release(s); protection broken by {broke}")
            m = _remeasure(best, kicad_cli)
            _classify_into(m, cons, best)
            if stop == "clean" and (vis(m["gaps"]) if kinds is not None else
                                    any(g["kind"] in DISPATCH or g["kind"] == "pair-unmeasured" for g in m["gaps"])):
                stop = "protection"                      # the board handed on is an earlier state: its gaps are back, it is not clean
        else:
            shutil.rmtree(os.path.join(work, "accepted"), ignore_errors=True)       # the final board verifies: the snapshots are not needed

    left = []
    for g in vis(m["gaps"]):
        l = last.get(g["kind"], {})
        left.append({"id": g["id"], "kind": g["kind"], "class": g["class"],
                     "last_method": l.get("method"), "why": l.get("why")})
    ignored = []                                  # PHASE3H A7: answers left unapplied, with the reason
    by_sid = {critroute._safe(ai_key(g))[:80]: g for g in m["gaps"]}
    for f in sorted(glob.glob(os.path.join(ctx_dir_ai(work), "*.action.json"))):
        g = by_sid.get(os.path.basename(f)[:-len(".action.json")])
        why_ig = ("no such gap" if g is None else "kind has no ai rung" if "ai" not in DISPATCH.get(g["kind"], [])
                  else "budget" if ai_used.get(ai_key(g), 0) >= _ai_budget(cons) else None)
        if why_ig:
            ignored.append({"file": f, "reason": why_ig})
    res = {"stop": stop, "exit": {"clean": 0, "packets": 5}.get(stop, 2), "level": level,
           "minutes": round((time.time() - t0) / 60, 1), "start": start, "end": _vitals(m),
           "attempts": n_att, "left": left,
           "packets": _packet_rows(work, vis(m["gaps"]), ai_used, _ai_budget(cons)),       # PHASE3F B6
           "proposals": _proposal_rows(ctx_dir_ai(work)), "ignored": ignored,
           "released": dict(released), "protection_broken_by": broke}
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
