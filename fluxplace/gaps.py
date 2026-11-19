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

from . import audit, calllog, constraints as C, drcfull, drcguard, joingaps, krtchain, versions

KIND_OF_TYPE = {"shorting_items": "short", "solder_mask_bridge": "short",
                "clearance": "clearance", "hole_clearance": "clearance", "hole_to_hole": "clearance",
                "track_dangling": "dangling", "via_dangling": "dangling",
                "track_not_centered_on_via": "via-offcentre"}
ORDER = ["short", "open-gap", "open-pour", "open-free", "clearance", "dangling", "via-offcentre"]
CLASS_RANK = {"hs-pair": 0, "rf": 1, "power": 2, "pair": 3, "plain": 4}
_NET = re.compile(r"\[([^\]]*)\]")
_CACHE = {}


def measure(board, kicad_cli="kicad-cli"):
    """One DRC per board (cached on abspath, mtime, size; last 4 kept). `errors` excludes unconnected items."""
    p = os.path.abspath(board)
    st = os.stat(p)
    key = (p, st.st_mtime_ns, st.st_size)
    if key in _CACHE:
        return _CACHE[key]
    d = drcguard.drc_json(p, kicad_cli)
    vs = d.get("violations") or []
    err = [v for v in vs if v.get("severity") == "error"]
    m = {"drc": d, "opens": len(d.get("unconnected_items") or []),
         "shorts": sum(1 for v in vs if v.get("type") == "shorting_items"),
         "errors": len(err), "err_keys": Counter(drcguard.key_of(v) for v in err)}
    _CACHE[key] = m
    while len(_CACHE) > 4:
        _CACHE.pop(next(iter(_CACHE)))
    return m


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


def _accept(base_m, base_board, cand_m, cand_board, target_ids, cand_gaps):
    """Phase 1 guard (mode repair). The first failing rule gives `why`. base_m["gaps"] supplies the base score (R4)."""
    diff = audit.connectivity_diff(_snapshot(base_board), audit.snapshot(cand_board))
    if not diff["ok"]:
        return False, ("R0 " + (diff["violations"][0] if diff.get("violations") else "connectivity"))[:100]
    if cand_m["shorts"] > base_m["shorts"]:
        return False, "R1 shorts up"
    new = drcguard.new_keys(base_m["err_keys"], cand_m["err_keys"])
    if new:
        return False, f"R2 new DRC key {next(iter(new))[0]}"[:120]
    if not score(cand_m, cand_gaps) < score(base_m, base_m.get("gaps") or []):
        return False, "R4 score not lower"
    cand_ids = {g["id"] for g in cand_gaps}
    if not any(t not in cand_ids for t in target_ids):
        return False, "target gap still there"
    return True, ""


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


def _layers(board):
    """(copper layer names, names of layers a full-board zone covers). Rule areas are skipped."""
    import pcbnew
    b = pcbnew.LoadBoard(board)
    cu = [b.GetLayerName(lid) for lid in b.GetEnabledLayers().CuStack()]
    edge = b.GetBoardEdgesBoundingBox().GetArea()
    full = set()
    for z in b.Zones():
        if z.GetIsRuleArea() or not edge or z.GetBoundingBox().GetArea() < 0.8 * edge:
            continue
        full.update(b.GetLayerName(lid) for lid in z.GetLayerSet().Seq())
    return cu, full


def copper_layers(board):
    return _layers(board)[0]


def signal_layers(board):
    """Copper layers minus every layer that a full-board zone covers. No layer names in code."""
    cu, full = _layers(board)
    return [n for n in cu if n not in full]


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


def _r_drcfix(base, out, gaps, ctx):
    from . import drcfix as DF
    return _brief(DF.loop(base, out, kicad_cli=ctx["kicad_cli"], cons=ctx["cons"], log=ctx["log"]))


def _r_launder(base, out, gaps, ctx):
    from . import launder as LA
    return _brief(LA.launder_board(base, out, kicad_cli=ctx["kicad_cli"], dangling=True, log=ctx["log"]))


def _r_snapiter(base, out, gaps, ctx):
    from . import snapiter as SI
    return _brief(SI.run(base, out, log=ctx["log"]))


RUNGS = {"joingaps": _r_joingaps, "pourisland": _r_pourisland, "patch": _r_patch, "patch_rip": _r_patch_rip,
         "handrepair": _r_handrepair, "region": _r_region, "drcfix": _r_drcfix, "launder": _r_launder,
         "snapiter": _r_snapiter}

# pour-tie, planetie, pair and RF rungs join in phase 3 (FLOW-REDESIGN 5)
DISPATCH = {
    "short": ["drcfix"],
    "open-gap": ["joingaps", "patch"],
    "open-pour": ["pourisland", "region"],
    "open-free": ["patch", "handrepair", "region", "patch_rip"],
    "clearance": ["drcfix", "region"],
    "dangling": ["launder"],
    "via-offcentre": ["snapiter"],
}
LEVEL = {"region": "L1", "patch_rip": "L1"}  # every other rung is L0


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


def close(board, work, cons_path=None, cons=None, kicad_cli="kicad-cli", budget_min=None, max_rounds=40,
          attempts=None, log=print):
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
    start = _vitals(m)

    state_path = os.path.join(work, "gaps.json")
    memo = set()
    try:
        with open(state_path) as fh:
            st = json.load(fh)
        if st.get("tool") == tool and st.get("cons_hash") == cons_hash:
            memo = {tuple(x) for x in st.get("memo", [])}
    except (OSError, ValueError, AttributeError):
        pass

    def save(r):
        _write_json(state_path, {"gaps": m["gaps"], "memo": sorted(list(k) for k in memo), "round": r,
                                 "tool": tool, "cons_hash": cons_hash})

    stop, level, n_att, last = None, "L0", 0, {}

    def attempt(r, kind, rung, targets):
        """One try. Returns True when the candidate was accepted (m is then the new base)."""
        nonlocal m, n_att, level
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
                       "budget_s": max(0.0, budget_s - (time.time() - t0)), "log": log}
                summ = RUNGS[rung](best, out, targets, ctx) or {}
                if not os.path.exists(out):
                    result = "refused"
                    why = str(summ.get("status") or summ.get("reason") or "no board written")[:120]
                elif versions.copper_fingerprint(out) == fp:
                    result, why = "refused", "no change"
                else:
                    cm = dict(measure(out, kicad_cli))
                    runs = 1
                    cm["gaps"] = classify(cm["drc"], cons)
                    ok, why = _accept(m, best, cm, out, [g["id"] for g in targets], cm["gaps"])
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
        if result == "refused":
            memo.add(key)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "run": run_id,
               "board_fp": fp[:8] if len(fp) > 8 else fp, "tool": tool, "stage": "close",
               "level": LEVEL.get(rung, "L0"),
               "gap": {"id": targets[0]["id"], "kind": kind, "class": targets[0]["class"], "n": len(targets)},
               "method": rung, "tier": {"scope": "board", "effort": 1}, "in": {"nets": len(nets)},
               "result": result, "why": why, "score": [vec0, after], "s": round(time.time() - t_try, 1),
               "drc_runs": runs, "precheck": None, "ai_tokens": 0}
        if explicit or calllog.enabled():
            calllog.append(rec, attempts)
        n_att += 1
        last[kind] = {"method": rung, "why": why}
        return result in ("closed", "partial")

    for r in range(max_rounds):
        if not any(g["kind"] in DISPATCH for g in m["gaps"]):
            stop = "clean"
            break
        hit = False
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
            stop = "flat"
            break
    else:
        stop = "rounds"

    left = []
    for g in m["gaps"]:
        l = last.get(g["kind"], {})
        left.append({"id": g["id"], "kind": g["kind"], "class": g["class"],
                     "last_method": l.get("method"), "why": l.get("why")})
    res = {"stop": stop, "exit": 0 if stop == "clean" else 2, "level": level,
           "minutes": round((time.time() - t0) / 60, 1), "start": start, "end": _vitals(m),
           "attempts": n_att, "left": left}
    _write_json(os.path.join(work, "stop.json"), res)
    date, rows = time.strftime("%Y-%m-%d"), []
    for kind in ORDER + ["other"]:
        n = sum(1 for g in m["gaps"] if g["kind"] == kind)
        if n:
            why = last.get(kind, {}).get("why") or ("not dispatched" if kind not in DISPATCH else "not tried")
            rows.append(f"| {date} | close | {kind} x{n} left | {why} | open |")
    with open(os.path.join(work, "ledger-rows.md"), "w") as fh:
        fh.write("\n".join(rows) + ("\n" if rows else ""))
    return dict(res, best=best)


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m fluxplace.gaps")
    ap.add_argument("board")
    ap.add_argument("--work", required=True)
    ap.add_argument("--constraints")
    ap.add_argument("--classify-only", action="store_true")
    ap.add_argument("--kicad-cli", default="kicad-cli")
    a = ap.parse_args(argv)
    t0 = time.time()
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
