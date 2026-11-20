"""`pairs --only A,B`: re-route named differential pairs WHOLE on a routed board.

Per named pair: both legs' unlocked copper goes (a locked item refuses the pair, naming it), the pair is laid again with the
coupled pair layer (`pairs.plan`, the machinery of the pairs stage) with everything else on the board as fixed obstacles.
The result is accepted for a pair only when ALL of these hold on the candidate board:
  - the pair is connected and `pairaudit` grades it complete (same layer sequence, same via count, skew in limit),
  - no net other than the pair has more open connections than before (nets opened on purpose by --allow-shove excepted),
  - DRC shows no violation key that the board before did not have (`drcguard` multiset rule).
Anything else: the pair's original copper stays exactly as it was (`kept_original`, with the measured reason: the corridor that
is blocked and the obstacles that bind it). A pair already complete is not touched (`already_ok`): never worse than it started.

--unlock (off by default): temporarily unlock only the named pair's copper; failure restores its original locks exactly,
success requires locked replacement copper and records the unlocked item count.

--allow-shove (off by default): when the coupled lay is blocked, the unlocked copper of the NON-pair, non-RF, non-power,
non-clock neighbour nets the blocker report names is removed, the pair is laid, and the nets that were opened are reported
(they are NOT laid again: the hand-route tools do that). Off, the report only names the neighbours that block.

Bounded by a budget (`tune.Budget`): out of time or SIGTERM / SIGINT writes the best accepted board so far to --out and removes
every temp file; pairs not reached are `not_reached`."""
import fnmatch
import os
import re
import shutil

from . import pairfb as FB
from . import pairuncoupled as PU
from . import tune as TU

BREAKOUT_WHY = ("closest approach", "no clear break-out")      # the coupled centre line cannot reach a terminal: retry with break-outs

PER_PAIR_S = 120            # default budget = named pairs x this (a lay plus one DRC run, with a retry for --allow-shove)
SHOVABLE = ("signal", "slow", "bypass")
__all__ = ["resolve", "locked_items", "shove_nets", "breakout_ring", "reroute", "groups", "regroup", "format_text", "PER_PAIR_S"]


def breakout_ring(limit):
    """gate ring (mm) of the break-out retry: the coupled part starts about this far from each terminal, so the uncoupled leg
    stays within `limit` (the pad-to-gate allowance of about 1 mm is the rest). The measured result is checked against the limit."""
    return max(0.5, limit - 1.0)


def _bo_text(r):
    """per-end break-out lengths of a re-routed pair, for the report line: '; breakout U8: P 1.4 mm, N 1.1 mm (limit 5.0)'."""
    lim = r.get("breakout_limit")
    return "".join(f"; breakout {k}: P {v['P']} mm, N {v['N']} mm (limit {lim:.1f})" for k, v in (r.get("breakout") or {}).items()
                   if v["P"] or v["N"])


def resolve(names, pairs):
    """names: pair names as the pair audit prints them ("P/N"), a net name, a prefix or a glob -> ([(p, n)], [unmatched names])."""
    out, miss = [], []
    for name in names:
        name = name.strip()
        if not name:
            continue
        exact = [pr for pr in pairs if name in (f"{pr[0]}/{pr[1]}", f"{pr[1]}/{pr[0]}") or name in pr]
        hit = exact or [pr for pr in pairs if any(fnmatch.fnmatchcase(x, name) if re.search(r"[*?\[]", name) else x.startswith(name) for x in pr)]
        if not hit:
            miss.append(name)
        for pr in hit:
            if pr not in out:
                out.append(pr)
    return out, miss


def locked_items(text, nets, locked=True):
    """Copper descriptions of `nets` with the requested lock state."""
    from . import boardtext as BT
    want, out = set(nets), []
    for _i, _j, key, blk in BT.scan(text):
        net = BT.net_name(blk)
        if net in want and BT.is_locked(blk) == locked:
            out.append(f"via {net} ({key[1]},{key[2]})" if key[0] == "v" else
                       f"segment {net} {key[4]} ({key[1][0]},{key[1][1]})-({key[2][0]},{key[2][1]})")
    return out


def unlock_items(text, nets):
    """Temporarily remove lock markers only from the named nets' copper -> (text, item count)."""
    from . import boardtext as BT
    want = set(nets)
    edits = []
    for i, j, _key, blk in BT.scan(text):
        if BT.net_name(blk) in want and BT.is_locked(blk):
            edits.append((i, j, re.sub(r"\s*\(locked(?: yes)?\)", "", blk)))
    for i, j, blk in reversed(edits):
        text = text[:i] + blk + text[j + 1:]
    return text, len(edits)


def shove_nets(blockers, pair, cls, protect=()):
    """-> (shovable nets, [(net, why not)]) from blocker labels ("track NET LAYER", "via NET", "pad REF.N (NET)").
    Only classes in SHOVABLE; never a pair net, an RF net, a power/ground net or anything in `protect`."""
    shov, refused = [], []
    for lab in blockers or ():
        m = re.match(r"(?:track|via) (\S+)", lab)
        if not m:
            continue
        net = m.group(1)
        if net in pair or net in protect or net in shov or net in [r[0] for r in refused]:
            continue
        c = (cls.get(net) or {}).get("class", "signal")
        if c in SHOVABLE:
            shov.append(net)
        else:
            refused.append((net, f"class {c}"))
    return shov, refused


def _profile_row(B, pr):
    from . import pairaudit as PA
    pp, pn = PA.profile(B, pr[0]), PA.profile(B, pr[1])
    return {"p_mm": pp["length"], "n_mm": pn["length"], "skew": round(abs(pp["length"] - pn["length"]), 3),
            "vias": [pp["vias"], pn["vias"]], "layers": [pp["layers"], pn["layers"]]}


def _cleanup(work):
    """every file of this run: the working copy, its stripped (.pre) and candidate (.try) copies, their project sidecars, DRC
    reports and KiCad lock files (all named <out>.work...)."""
    import glob
    d, b = os.path.split(work)
    stem = b[:-len(".kicad_pcb")] if b.endswith(".kicad_pcb") else b
    for q in glob.glob(os.path.join(glob.escape(d), glob.escape(stem) + "*")) + glob.glob(os.path.join(glob.escape(d), "~" + glob.escape(stem) + "*")):
        try:
            os.unlink(q)
        except OSError:
            pass


def _sidecars(src, dst):
    for ext in (".kicad_pro", ".kicad_dru"):
        s = os.path.splitext(src)[0] + ext
        if os.path.exists(s):
            shutil.copy(s, os.path.splitext(dst)[0] + ext)


def reroute(board, out, names, cons=None, spec=None, kicad_cli="kicad-cli", budget=None, allow_shove=False,
            log=print, lay=None, drc=None, result=None, unlock=False):
    """-> report {out, stopped, elapsed_s, budget_s, unmatched, pairs: [row]}; row.status is rerouted | kept_original |
    refused_locked | already_ok | not_reached. `lay(board_path, cons, spec, only=set)` defaults to pairs.plan, `drc(path)` ->
    (keys, counts) to a kicad-cli DRC; both can be replaced (tests)."""
    from . import pairs as PR, pairaudit as PA, routediag as RD, routeconn as RC, netclass as NC, routerules as RR
    from . import drcguard as DG, qualaudit as QA
    cons = cons or {}
    lay = lay or (lambda b, c, s, only: PR.plan(b, c, s, only=only, log=lambda m: None))
    B0 = RD.load(board)
    cls = NC.classify(B0, cons, spec)
    sel, unmatched = resolve(names, PA.pair_list(cls))
    protect = set(QA.rf_names(B0, cons))
    bud = budget if isinstance(budget, TU.Budget) else TU.Budget(budget if budget is not None else TU.default_budget(len(sel), PER_PAIR_S), 0)
    work = out + ".work.kicad_pcb"
    pre = work + ".pre.kicad_pcb"
    try_ = work + ".try.kicad_pcb"
    rows = {}
    stopped = None
    for n in unmatched:
        log(f"  no pair matches {n!r}")

    def run_drc(p):
        if drc:
            return drc(p)
        return DG.drc_keys(p, kicad_cli, timeout=bud.drc_timeout())

    def put_pre(text):
        with open(pre, "w") as f:
            f.write(text)
        _sidecars(work, pre)

    try:
        with bud:
            shutil.copy(board, work)
            _sidecars(board, work)
            base = run_drc(work)
            open0 = RC.text_open(RD.load(work))
            aud0 = PA.audit(RD.load(work), sel, cons, {n for n, k in open0.items() if k})
            for pr in sel:
                bud.check()
                name = f"{pr[0]}/{pr[1]}"
                row = rows[name] = {"pair": name, "status": "kept_original", "reason": "", "before": {}, "after": {},
                                    "blockers": [], "shoved": [], "opened": []}
                text = open(work, errors="replace").read()
                Bw = RD.load(work)
                row["before"] = dict(_profile_row(Bw, pr), audit=aud0[pr]["status"], why=aud0[pr]["why"])
                lk = locked_items(text, pr)
                if lk and not unlock:
                    row.update(status="refused_locked", locked=lk[:20],
                               reason=f"{len(lk)} locked item(s): " + "; ".join(lk[:6]) + (" ..." if len(lk) > 6 else "") + " (use --unlock to re-route and re-lock this pair)")
                    log(f"  {name}: refused, {row['reason']}")
                    continue
                if aud0[pr]["status"] == "complete":
                    row.update(status="already_ok", reason="already connected, matched and in tolerance: not touched")
                    continue
                if lk:
                    text, row["unlocked_items"] = unlock_items(text, pr)
                    row["unlocked"] = lk[:20]
                stripped, nrem = PA.strip_legs(text, pr)
                row["removed_items"] = nrem
                put_pre(stripped)
                res = lay(pre, cons, spec, set(pr))
                e = next((x for x in res["pairs"] if tuple(x["pair"]) == pr), None) or {"status": "not-laid", "why": "pair not found by the pair layer"}
                bo_lim = FB.uncoupled_limit(cons, pr)
                row["breakout_limit"], bo_ring = bo_lim, None
                if e["status"] != "laid" and bo_lim > 0 and any(t in e.get("why", "") for t in BREAKOUT_WHY):
                    # b(iii) machinery (pairfb): the same pair lay with a gate ring for the break-outs, every leg bounded by the limit
                    bo_ring = breakout_ring(bo_lim)
                    res_b = lay(pre, FB.variant(cons, ring_mm=bo_ring, max_uncoupled_mm=bo_lim, skew_frac=1.0), spec, set(pr))
                    eb = next((x for x in res_b["pairs"] if tuple(x["pair"]) == pr), None) or {"status": "not-laid", "why": "pair not found"}
                    if eb["status"] == "laid":
                        res, e = res_b, eb
                    else:
                        bo_ring, row["breakout_why"] = None, eb.get("why", "")
                shoved = []
                if e["status"] != "laid":
                    row["blockers"] = list(e.get("blockers", []))
                    row["reason"] = f"blocked corridor: {e.get('why', '')}" + (f" (obstacles: {', '.join(row['blockers'])})" if row["blockers"] else "")
                    cand, refused = shove_nets(e.get("blockers", []) + [b for b in (e.get("blocked_by", "") or "").split(" | ") if b.startswith(("track ", "via "))],
                                               pr, cls, protect)
                    row["shove_candidates"] = cand
                    if refused:
                        row["shove_refused"] = [f"{n} ({w})" for n, w in refused]
                    if allow_shove and cand:
                        stripped2, n2 = PA.strip_legs(stripped, cand)
                        put_pre(stripped2)
                        res = lay(pre, cons, spec, set(pr))
                        e = next((x for x in res["pairs"] if tuple(x["pair"]) == pr), None) or {"status": "not-laid"}
                        if e["status"] == "laid":
                            shoved = cand
                            row["shoved"] = cand
                            row["removed_items"] += n2
                        else:
                            row["reason"] += f"; still blocked after removing {', '.join(cand)}"
                    if e["status"] != "laid":
                        log(f"  {name}: kept_original, {row['reason']}")
                        continue
                R = RR.rules(cons, pre, B=res["B"])
                Pm = PR.params(cons)
                proj = PR.project_classes(pre)
                PR.write(pre, try_, res, lambda net: PR.rules_for(pre, res["B"], cons, R, net, Pm, proj)["drill"])
                _sidecars(work, try_)
                B2 = RD.load(try_)
                opn = RC.text_open(B2)
                a = PA.audit(B2, [pr], cons, {n for n, k in opn.items() if k})[pr]
                prof = _profile_row(B2, pr)
                rr = PR.rules_for(pre, res["B"], cons, R, pr[0], Pm, proj)
                ends = PU.breakout_ends(B2, pr, PR.terminals(B2, pr[0], pr[1], Pm["pad_max"])[0], rr["w"], rr["gap"])
                unlocked_result = locked_items(open(try_, errors="replace").read(), pr, locked=False) if lk else []
                why = "pair result was not locked again" if unlocked_result else None
                if a["status"] == "unknown" and why is None:
                    pp, pn = PA.profile(B2, pr[0]), PA.profile(B2, pr[1])
                    if not (pp["path"] and pn["path"] and pp["layers"] == pn["layers"] and pp["vias"] == pn["vias"]):
                        why = "pair not connected on one layer sequence"
                elif a["status"] != "complete" and why is None:
                    why = f"pair audit: {a['status']}" + (f" ({a['why']})" if a["why"] else "")
                if why is None:
                    worse = sorted(n for n, k in opn.items() if n not in pr and n not in shoved and k > open0.get(n, 0))
                    if worse:
                        why = "opens other net(s): " + ", ".join(worse[:6])
                if why is None and bo_ring is not None:
                    over = [(k, leg, v[leg]) for k, v in ends.items() for leg in "PN" if v[leg] > bo_lim + 1e-9]
                    if over:
                        why = f"break-out {over[0][0]} {over[0][1]} leg {over[0][2]} mm over the {bo_lim:g} mm limit"
                if why is None:
                    keys, _cnt = run_drc(try_)
                    new = DG.new_keys(base[0], keys)
                    if shoved:                                   # the nets removed on purpose are open: that is the report, not a defect
                        new = type(new)({k: v for k, v in new.items()
                                         if not (k[0].startswith("U") and any(s in d for s in shoved for d in k[1]))})
                    if new:
                        why = f"new DRC violation: {sorted(new)[0][0]} {sorted(new)[0][1][:2]}"
                if why:
                    row.update(status="kept_original", reason=why, after=dict(prof, audit=a["status"], why=a["why"]))
                    log(f"  {name}: kept_original, {why}")
                    continue
                os.replace(try_, work)
                base, open0 = (keys, _cnt), opn
                aud0[pr] = a
                row.update(status="rerouted", reason="", after=dict(prof, audit=a["status"], why=a["why"]), opened=sorted(
                    n for n, k in opn.items() if n in shoved and k), relocked=bool(lk), breakout=ends,
                    breakout_retry=bo_ring is not None)
                log(f"  {name}: rerouted, P {prof['p_mm']} / N {prof['n_mm']} mm, skew {prof['skew']}, vias {prof['vias']}"
                    + (f"; opened {', '.join(row['opened'])}" if row["opened"] else "") + _bo_text(row))
    except TU.Stopped as s:
        stopped = s.reason
        for r in rows.values():                              # the pair in progress: its original copper is what the output holds
            if r["status"] == "kept_original" and not r["reason"]:
                r.update(status="not_reached", reason=f"stopped ({stopped}) while working on it; original copper kept")
        log(f"  stopped ({stopped}): writing the best accepted board")
    except BaseException:
        _cleanup(work)
        raise
    if not os.path.exists(work):
        shutil.copy(board, work)
    for q in (pre, try_):
        try:
            os.unlink(q)
        except OSError:
            pass
    TU._finalize(work, out)
    _cleanup(work)
    for pr in sel:
        rows.setdefault(f"{pr[0]}/{pr[1]}", {"pair": f"{pr[0]}/{pr[1]}", "status": "not_reached", "reason": stopped or ""})
    for ext in (".kicad_pro", ".kicad_prl"):
        src = os.path.splitext(board)[0] + ext
        dst = os.path.splitext(out)[0] + ext
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy(src, dst)
    rep = {"board": board, "out": out, "stopped": stopped, "elapsed_s": round(bud.elapsed(), 1), "budget_s": bud.seconds,
           "allow_shove": bool(allow_shove), "unlock": bool(unlock), "unmatched": unmatched, "pairs": [rows[f"{p[0]}/{p[1]}"] for p in sel]}
    if result is not None:
        result.update(rep)
    return rep


def groups(B, cls, pairs):
    """PHASE3C A: connector groups for the failed pairs -> {group: [(p, n), ...]}, groups by size descending.
    A pair's group is the footprint (ref) with the most pads of that pair, if that ref has >= 4 of the pair's pads or
    holds pads of >= 2 different pairs; tie -> the ref with more pairs overall, then the ref name. A pair with no such ref
    is its own group, named "P/N". `cls` is accepted for the call shape; the pads of `B` decide."""
    per, touch = {}, {}
    for pr in pairs:
        per[pr] = {}
        for q in B["pads"]:
            if not q.get("np") and q["net"] in pr:
                per[pr][q["ref"]] = per[pr].get(q["ref"], 0) + 1
                touch.setdefault(q["ref"], set()).add(pr)
    out = {}
    for pr in pairs:
        cands = [(-c, -len(touch[r]), r) for r, c in per[pr].items() if c >= 4 or len(touch[r]) >= 2]
        g = min(cands)[2] if cands else f"{pr[0]}/{pr[1]}"
        out.setdefault(g, []).append(pr)
    return dict(sorted(out.items(), key=lambda kv: (-len(kv[1]), kv[0])))


def _complete(board, members, cons):
    """how many of `members` pairaudit grades complete on `board` (the same open-net set reroute uses)."""
    from . import routediag as RD, routeconn as RC, pairaudit as PA
    B = RD.load(board)
    opn = RC.text_open(B)
    aud = PA.audit(B, list(members), cons, {n for n, k in opn.items() if k})
    return sum(1 for p in members if aud[p]["status"] == "complete")


def regroup(board, out, cons, spec, pairs, log=print, budget_s=None):
    """PHASE3C B: re-lay the failed pairs one connector group at a time (`reroute`, shove + unlock, the group's own copper
    only). A group's board is kept only when more of its pairs are complete than before the call. A group still failing
    whose family has no `gap_mm` is retried once with that family's gap_mm = the fab profile's min clearance (rounded up
    to 0.01 mm) and `gap_retry` is recorded. -> {board, groups: {ref: {pairs, laid, failed, gap_retry}}, failed: [(p, n)]}."""
    import copy
    import math
    import shutil
    import time
    from . import routediag as RD, netclass as NC, constraints as C, zgeom as ZG
    cons = cons or {}
    pairs = [tuple(p) for p in pairs]
    B0 = RD.load(board)
    grp = groups(B0, NC.classify(B0, cons, spec), pairs)
    total = float(budget_s) if budget_s else float(PER_PAIR_S * max(1, len(pairs)))
    t0 = time.monotonic()
    cur, res, final, tmps = board, {}, {}, []
    gap = max(0.01, math.ceil(float(C.fab_profile(cons)["min_clearance_mm"]) * 100 - 1e-9) / 100)
    names = lambda ps: [f"{p}/{n}" for p, n in ps]
    for i, (g, members) in enumerate(grp.items()):
        left = total - (time.monotonic() - t0)
        if left <= 0:
            log(f"  regroup: budget spent before group {g}")
            res[g] = {"pairs": names(members), "laid": [], "failed": names(members), "gap_retry": False}
            continue
        step = max(1.0, left / (len(grp) - i))
        before = _complete(cur, members, cons)
        tmp = f"{out}.g{i}.kicad_pcb"
        tmps.append(tmp)
        rep = reroute(cur, tmp, names(members), cons, spec, budget=step, allow_shove=True, unlock=True, log=log)
        st = {r["pair"]: r["status"] for r in rep["pairs"]}
        if sum(st.get(x) in ("rerouted", "already_ok") for x in names(members)) > before:
            cur = tmp
        gap_retry = False
        fails = [p for p in members if st.get(f"{p[0]}/{p[1]}") not in ("rerouted", "already_ok")]
        fams = {ZG.family_of(cons, p[0]) for p in fails} - {None}
        fams = {f for f in fams if "gap_mm" not in (cons.get("pairs", {}).get(f) or {})}
        if fails and fams and left - (time.monotonic() - t0) > 1:
            cons2 = copy.deepcopy(cons)
            for f in fams:
                cons2["pairs"][f]["gap_mm"] = gap
            tmp2 = f"{out}.g{i}r.kicad_pcb"
            tmps.append(tmp2)
            before2 = _complete(cur, members, cons)
            rep2 = reroute(cur, tmp2, names(fails), cons2, spec, budget=max(1.0, left - (time.monotonic() - t0)), allow_shove=True,
                           unlock=True, log=log)
            st2 = {r["pair"]: r["status"] for r in rep2["pairs"]}
            if _complete(tmp2, members, cons) > before2:
                cur, gap_retry = tmp2, True
                st.update(st2)
            log(f"  regroup {g}: gap_mm {gap:g} retry on {', '.join(sorted(fams))}: {'kept' if gap_retry else 'no gain'}")
        ok = [x for x in names(members) if st.get(x) in ("rerouted", "already_ok")]
        for x in names(members):
            final[x] = st.get(x, "not_reached")
        res[g] = {"pairs": names(members), "laid": [x for x in ok if st.get(x) == "rerouted"],
                  "failed": [x for x in names(members) if x not in ok], "gap_retry": gap_retry}
        log(f"  regroup {g}: {len(ok)}/{len(members)} complete")
    shutil.copy(cur, out)
    for ext in (".kicad_pro", ".kicad_dru"):
        if os.path.exists(os.path.splitext(board)[0] + ext):
            shutil.copy(os.path.splitext(board)[0] + ext, os.path.splitext(out)[0] + ext)
    for t in tmps:
        for ext in ("", ".kicad_pro", ".kicad_dru", ".kicad_prl"):
            q = os.path.splitext(t)[0] + ext if ext else t
            if os.path.exists(q) and q != out:
                os.unlink(q)
    failed = [p for g in res.values() for p in [tuple(x.split("/", 1)) for x in g["failed"]]]
    return {"board": out, "groups": res, "failed": failed}


def format_text(rep):
    c = {}
    for r in rep["pairs"]:
        c[r["status"]] = c.get(r["status"], 0) + 1
    head = f"pairs --only: {len(rep['pairs'])} pair(s): " + ", ".join(f"{n} {k}" for k, n in sorted(c.items())) \
        + (f"; stopped: {rep['stopped']}" if rep.get("stopped") else "") + (f"; no match for {', '.join(rep['unmatched'])}" if rep["unmatched"] else "")
    lines = [head]
    for r in rep["pairs"]:
        a = r.get("after") or {}
        if r["status"] == "rerouted":
            lines.append(f"  {r['pair']}: rerouted, P {a['p_mm']} / N {a['n_mm']} mm, skew {a['skew']}, vias {a['vias']}"
                         + (f"; OPENED {', '.join(r['opened'])} (not re-laid)" if r["opened"] else "") + _bo_text(r))
        else:
            lines.append(f"  {r['pair']}: {r['status']}" + (f", {r['reason']}" if r.get("reason") else "")
                         + (f"; neighbours that could be removed with --allow-shove: {', '.join(r['shove_candidates'])}" if r.get("shove_candidates") and not r.get("shoved") else ""))
    return "\n".join(lines)
