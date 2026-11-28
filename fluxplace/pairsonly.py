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
__all__ = ["resolve", "locked_items", "shove_nets", "breakout_ring", "reroute", "groups", "regroup", "format_text", "PER_PAIR_S", "lay_centreline"]


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
    a = PA.audit(B, [tuple(pr)], {}, set()).get(tuple(pr), {})          # KiCad's lengths when the board is a file (netlen)
    sk = round(abs(pp["length"] - pn["length"]), 3)
    if a.get("skew") is not None and "p" in a:
        pp, pn, sk = a["p"], a["n"], a["skew"]
    return {"p_mm": pp["length"], "n_mm": pn["length"], "skew": sk,
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


def _cleanup_one(path):
    for x in (path, os.path.splitext(path)[0] + ".kicad_pro", os.path.splitext(path)[0] + ".kicad_dru"):
        if os.path.exists(x):
            os.remove(x)


def _sidecars(src, dst):
    for ext in (".kicad_pro", ".kicad_dru"):
        s = os.path.splitext(src)[0] + ext
        if os.path.exists(s):
            shutil.copy(s, os.path.splitext(dst)[0] + ext)


def _blocker_pairs(labels, pairs):
    """PHASE3I B5b: the pairs (of `pairs`) one of whose legs the blocker labels name (whole-token match)."""
    toks = set(re.split(r"[\s,;()|]+", " ".join(labels)))
    return [p for p in pairs if any(leg in toks for leg in p)]


def _commit_guard(board, out, sel, rows, log):
    """A3: the one commit point. `out` may differ from `board` only in the nets of the pairs being re-routed (`sel`); any other protected
    copper changing (boardwrite manifest of `board`, re-seeded loudly when stale), duplicate copper or an id collision puts the input back
    as the output.  A board with no manifest is seeded from its locked items and complete pairs first (a hand-made `--only` run)."""
    from . import boardwrite as BW
    if not os.path.exists(out):
        return
    try:
        man = BW.fresh_manifest(board, log)
    except BW.ManifestStale as e:                       # B1: an unexplained edit is not legitimised: the input board is the output
        log(f"  commit refused: {e}")
        shutil.copy(board, out)
        return
    if not man["nets"]:
        man = BW.seed_from_board(board, None, log=log)
    if not man["nets"]:
        return
    rel = {x for pr in sel for x in pr} | {x for k, r in rows.items() if r.get("status") == "rerouted" for x in k.split("/")}   # + the grouped blockers
    v = BW.commit(board, out, out, release=rel, manifest=man, reason="pair re-route")
    if not v["written"]:
        log(f"  commit refused: {v['why']}; the input board is the output")
        shutil.copy(board, out)
        for r in rows.values():
            if r.get("status") == "rerouted":
                r.update(status="kept_original", reason=v["why"][:160])


def reroute(board, out, names, cons=None, spec=None, kicad_cli="kicad-cli", budget=None, allow_shove=False,
            log=print, lay=None, drc=None, result=None, unlock=False, force=(), group=True, baseline=None, tune_skew=True, accept_skew_x=1.0):
    """-> report {out, stopped, elapsed_s, budget_s, unmatched, pairs: [row]}; row.status is rerouted | kept_original |
    refused_locked | already_ok | not_reached. `lay(board_path, cons, spec, only=set)` defaults to pairs.plan, `drc(path)` ->
    (keys, counts) to a kicad-cli DRC; both can be replaced (tests)."""
    from . import pairs as PR, pairaudit as PA, routediag as RD, routeconn as RC, netclass as NC, routerules as RR
    from . import drcguard as DG, qualaudit as QA, gaps as GP
    cons = cons or {}
    lay = lay or (lambda b, c, s, only: PR.plan(b, c, s, only=only, log=lambda m: None))
    B0 = RD.load(board)
    cls = NC.classify(B0, cons, spec)
    sel, unmatched = resolve(names, PA.pair_list(cls))
    from . import pairbase as PB
    cons, geom_from = PB.with_widths(cons, board, baseline, sel)     # the pair's own width (baseline / input board), never the DRC minimum
    for pr_, (w_, src_) in sorted(geom_from.items()):
        log(f"  {pr_[0]}/{pr_[1]}: laid at {w_:g} mm ({src_})")
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
        d = DG.drc_json(p, kicad_cli, timeout=bud.drc_timeout())      # w82: opens are keyed per NET (keys_per_net): an open's item text changes whenever nearby copper does
        return DG.keys_per_net(d), (len(d.get("violations", [])), len(d.get("unconnected_items", [])))

    def put_pre(text):
        with open(pre, "w") as f:
            f.write(text)
        _sidecars(work, pre)

    try:
        with bud:
            shutil.copy(board, work)
            _sidecars(board, work)
            zoff = GP.impedance_off_nets(work, cons)
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
                if zoff is None:                         # A6: impedance UNKNOWN is never "in tolerance": the pair is re-laid
                    row["impedance_unknown"] = True
                elif set(pr) & zoff:
                    row["impedance_off"] = sorted(set(pr) & zoff)
                if aud0[pr]["status"] == "complete" and zoff is not None and not (set(pr) & zoff) and name not in force:
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
                if a["status"] == "mismatch" and (a.get("why") or "").startswith("skew") and tune_skew:
                    # laid coupled on one layer sequence but over the skew limit: a cheap guarded tune of THIS pair (meander on its shorter leg)
                    tt = try_ + ".tune.kicad_pcb"
                    try:
                        _sidecars(try_, tt)
                        TU.tune_pairs(try_, tt, {pr[1]: pr[0]}, lambda m: PR.guard_limit(cons, m), budget=90.0, cons=cons, log=lambda m: None)
                        if os.path.isfile(tt):
                            os.replace(tt, try_)
                            B2 = RD.load(try_)
                            opn = RC.text_open(B2)
                            a = PA.audit(B2, [pr], cons, {n for n, k in opn.items() if k})[pr]
                            row["tuned"] = True
                    except Exception as e:                       # noqa: BLE001 - the untuned candidate is judged as it is
                        row["tune_error"] = f"{type(e).__name__}: {str(e)[:80]}"
                prof = _profile_row(B2, pr)
                rr = PR.rules_for(pre, res["B"], cons, R, pr[0], Pm, proj)
                ends = PU.breakout_ends(B2, pr, PR.terminals(B2, pr[0], pr[1], Pm["pad_max"])[0], rr["w"], rr["gap"])
                unlocked_result = locked_items(open(try_, errors="replace").read(), pr, locked=False) if lk else []
                why = "pair result was not locked again" if unlocked_result else None
                if a["status"] == "unknown" and why is None:
                    pp, pn = PA.profile(B2, pr[0]), PA.profile(B2, pr[1])
                    if not (pp["path"] and pn["path"] and pp["layers"] == pn["layers"] and pp["vias"] == pn["vias"]):
                        why = "pair not connected on one layer sequence"
                elif (a["status"] == "mismatch" and (a.get("why") or "").startswith("skew") and why is None and accept_skew_x > 1.0
                      and abs(prof["skew"] or 0.0) <= accept_skew_x * PR.guard_limit(cons, pr[0])):
                    row["skew_accepted"] = f"skew {prof['skew']} mm within {accept_skew_x:g} x the limit (reported by review, not tuned further)"
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
                                         if not (k[0] == "Unet" and k[1] in shoved)})
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
            # PHASE3I B5b: a pair blocked by one in-tolerance (complete, not named) pair is re-laid as a group: the blocker is
            # re-laid whole, then the blocked pair; both are kept only when both end complete, else the board is restored.
            for row in [r for r in rows.values() if group and r["status"] == "kept_original" and r.get("blockers")]:
                cands = [p for p in _blocker_pairs(row["blockers"], PA.pair_list(cls)) if p not in sel]
                if len(cands) != 1:
                    continue
                bname = f"{cands[0][0]}/{cands[0][1]}"
                opn_now = RC.text_open(RD.load(work))
                if PA.audit(RD.load(work), [cands[0]], cons, {n for n, k in opn_now.items() if k})[cands[0]]["status"] != "complete":
                    continue
                left = bud.remaining()                       # PHASE3I F7 (R14): the sub-call gets the seconds left, no fresh budget
                if left is not None and left <= 0:
                    row["group_refused"] = "budget spent"
                    continue
                from . import lockpolicy as LP
                lk_b = locked_items(open(work, errors="replace").read(), list(cands[0]))
                if lk_b and not unlock and any(LP.level(n, cons, list(cands[0]) + list(row["pair"].split("/"))) == LP.HARD for n in cands[0]):
                    row["group_refused"] = f"{bname}: blocker hard-locked"  # a hard-locked blocker is re-laid only with unlock
                    log(f"  {row['pair']}: kept_original, group with {bname} refused (blocker hard-locked)")
                    continue
                snap, out2 = work + ".grp.kicad_pcb", work + ".grp2.kicad_pcb"
                shutil.copy(work, snap)
                _sidecars(work, snap)
                sub = reroute(work, out2, [bname, row["pair"]], cons, spec, kicad_cli, left, allow_shove, log, lay, drc,
                              unlock=unlock, force=(bname,), group=False)   # blocker first (order unchanged: see report)
                got = {r["pair"]: r["status"] for r in sub["pairs"]}
                sub_rows = {r["pair"]: r for r in sub["pairs"]}
                bb, bs = sub_rows.get(bname, {}).get("before", {}), sub_rows.get(bname, {}).get("after", {})
                blocker_ok = not lk_b or (bs.get("skew", 0) <= bb.get("skew", 0) and sum(bs.get("vias") or [0]) <= sum(bb.get("vias") or [0]))
                if got.get(bname) == "rerouted" and got.get(row["pair"]) == "rerouted" and blocker_ok:
                    os.replace(out2, work)
                    _sidecars(out2, work)
                    row.update(status="rerouted", reason="", grouped_with=bname)
                    rows[bname] = dict(sub_rows[bname], grouped_with=row["pair"])      # the blocker's full row, not a stub
                    log(f"  {row['pair']}: rerouted as a group with {bname} (blocker re-laid whole)")
                else:
                    shutil.copy(snap, work)
                    row["group_refused"] = f"{bname}: {got.get(bname, 'not reached')}, {row['pair']}: {got.get(row['pair'], 'not reached')}" \
                        + ("" if blocker_ok else ", blocker skew/vias worse")
                    log(f"  {row['pair']}: kept_original, group with {bname} refused ({row['group_refused']})")
                for ext in ("", ".pre"):
                    _cleanup_one(out2 + ext)
                _cleanup_one(snap)
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
    _commit_guard(board, out, sel, rows, log)
    for pr in sel:
        rows.setdefault(f"{pr[0]}/{pr[1]}", {"pair": f"{pr[0]}/{pr[1]}", "status": "not_reached", "reason": stopped or ""})
    for ext in (".kicad_pro", ".kicad_prl"):
        src = os.path.splitext(board)[0] + ext
        dst = os.path.splitext(out)[0] + ext
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy(src, dst)
    rep = {"board": board, "out": out, "stopped": stopped, "elapsed_s": round(bud.elapsed(), 1), "budget_s": bud.seconds,
           "allow_shove": bool(allow_shove), "unlock": bool(unlock), "unmatched": unmatched, "pairs": [rows[f"{p[0]}/{p[1]}"] for p in sel] + [r for k, r in rows.items() if k not in {f"{p[0]}/{p[1]}" for p in sel}]}
    if result is not None:
        result.update(rep)
    return rep


def _dir_index(u):
    """index of the pairlay direction equal to the unit vector u (45/90 only), else None."""
    from . import pairlay as PL
    for i, v in enumerate(PL.DVEC):
        if abs(v[0] - u[0]) < 1e-6 and abs(v[1] - u[1]) < 1e-6:
            return i
    return None


def lay_centreline(board, out, pair, layer, waypoints, cons=None):
    """PHASE3G G1 (agent pair_path): one coupled centre line through `waypoints` (45/90 segments only), laid as the pair's two
    legs at the family geometry of each layer (zgeom.family_table, as gaps._pair_block reads it), each leg joined to its pad by
    a break-out (pairlay.breakout, the pairs stage's own), skew compensated (pairlay.bump) against the pair's limit.
    PHASE3G G1b a: waypoints are [x, y] or [x, y, "Layer"]; a waypoint naming another layer than the current one is a LAYER
    CHANGE at that point (a via pair, pairlay.legs fans the two legs out to it and back in; at most 4, never on the last point).
    The start break-out must find the start pad on the first run's layer (else refused, naming the pad). Every change where the
    reference plane changes gets ONE return via of the reference net at a free site within `[review] return_via_mm` of the via
    pair (returnpath's own test); none fits -> laid anyway, listed in `return_via_missing`.
    The sign of each leg (which leg is on the left of travel) is the one with the shorter total break-out.
    Old copper: every copper of the pair's two nets goes (the locked pair copper is replaced too, as reroute --unlock does);
    the new copper is locked. `out` None edits `board` in place (the session work file of gaps._apply_ai); nothing is written
    unless laid. -> {"laid", "why", "p_mm", "n_mm", "layer", "layers", "vias", "sign", "stripped": {"items", "locked"},
    "return_via_missing": [[x, y]]} on success; on refusal {"laid": False, "why", "p_mm", "n_mm", "at": [x, y] | None,
    "blocker": {"label", "net", "layer"} | None, "refusals": [{"sign", "why", "at", "blocker"}]}."""
    import math
    from . import routediag as RD, routerules as RR, pairs as PR, pairlay as PL, pairgeo as G, zgeom as ZG, returnpath as RP

    def no(why, at=None, blocker=None):
        return {"laid": False, "why": why[:300], "p_mm": None, "n_mm": None, "at": at, "blocker": blocker}

    def blk(it, pieces, at=None):
        """{"at", "blocker"} of an item that refuses `pieces` (its copper point nearest to them, or `at`)."""
        if it is None:
            return {"at": at, "blocker": None}
        here = at or G.blocker_at(it, pieces)
        lays = set(it.get("layers") or ())
        on = [p[1] for p in pieces if p[0] == "seg" and p[1] in lays]
        lay = on[0] if on else (sorted(lays)[0] if lays else None)
        return {"at": [round(here[0], 3), round(here[1], 3)],
                "blocker": {"label": it["label"], "net": it.get("net") or None, "layer": lay}}

    cons = cons or {}
    if len(waypoints) < 2:
        return no("need at least 2 waypoints")
    pts, lays, cur = [], [], layer
    for q in waypoints:
        xy = (float(q[0]), float(q[1]))
        if len(q) > 2 and q[2]:
            cur = q[2]
        if pts and abs(xy[0] - pts[-1][0]) < 1e-6 and abs(xy[1] - pts[-1][1]) < 1e-6:
            lays[-1] = cur                                  # a repeated point (corridor joins) is one point; a layer change on it is kept
            continue
        pts.append(xy)
        lays.append(cur)
    for k, (a, b) in enumerate(zip(pts, pts[1:])):
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        if dx < 1e-6 and dy < 1e-6:
            return no(f"segment {k} has zero length", at=list(a))
        if min(dx, dy) > 1e-6 and abs(dx - dy) > 1e-6:
            return no(f"segment {k} is not 45/90", at=list(a))
    chg = [i for i in range(1, len(pts)) if lays[i] != lays[i - 1]]
    if chg and chg[-1] == len(pts) - 1:
        return no("the last waypoint cannot change layer: the end pad is on the last run's layer", at=list(pts[-1]))
    if len(chg) > 4:
        return no("more than 4 layer changes", at=list(pts[chg[4]]))
    runs = []
    for s, e in zip([0] + chg, chg + [len(pts) - 1]):
        rp = pts[s:e + 1]
        t0, t1 = G.unit(rp[0], rp[1]), G.unit(rp[-2], rp[-1])
        runs.append({"layer": lays[s], "pts": rp, "t0": t0, "t1": t1, "d": _dir_index(t1)})
    vias = [{"at": pts[c], "t": runs[j]["t1"], "d": runs[j]["d"]} for j, c in enumerate(chg)]
    P_net, N_net = pair
    nets = [P_net, N_net]
    try:                                                # the family table the pairs stage lays (gaps._pair_block reads the same)
        fam = ZG.family_of(cons, P_net)
        rows, planes, _src = ZG.resolve_stack(cons, board)
        cu = [r["name"] for r in rows if r.get("type") == "copper" and r.get("name")]
        sig = [l for l in cu if l not in set(planes)]
        tab = ZG.family_table(cons, rows, planes, sig, fam, board=board) if fam and rows else {}
    except Exception as e:                              # noqa: BLE001 - a stackup the family table cannot read: refuse, write nothing
        return no(f"pair geometry unavailable: {e}")
    dist, wmap = {}, {}
    for r in runs:
        g = tab.get(r["layer"]) or {}
        if not g.get("w") or not g.get("gap"):
            return no(f"no pair geometry for {r['layer']}")
        dist[r["layer"]] = (float(g["w"]) + float(g["gap"])) / 2
        wmap[r["layer"]] = float(g["w"])
    g0 = tab[runs[0]["layer"]]
    w0, gap0 = wmap[runs[0]["layer"]], float(g0["gap"])
    dist_max = max([(float(g["w"]) + float(g["gap"])) / 2 for g in tab.values() if g.get("w") and g.get("gap")] or [max(dist.values())])
    B = RD.load(board)
    try:
        B = dict(B, pads=B["pads"] + G.holes(board, B["copper"]))
    except (OSError, ValueError, IndexError):
        pass
    R = RR.rules(cons, board, B=B)
    Pm = PR.params(cons)
    rule = PR.rules_for(board, B, cons, R, P_net, Pm)
    clr, via_d = rule["clr"], rule["via"]
    hv = PR.via_half_pitch(via_d, rule["via_gap"], dist_max, Pm["step"])[2] if vias else []
    terms, _lone = PR.terminals(B, P_net, N_net, Pm["pad_max"])
    if len(terms) < 1:
        return no("no P and N pad pair to join")
    ta = min(terms, key=lambda t: math.hypot(t["c"][0] - pts[0][0], t["c"][1] - pts[0][1]))
    tb = min(terms, key=lambda t: math.hypot(t["c"][0] - pts[-1][0], t["c"][1] - pts[-1][1]))
    if ta is tb:
        return no("start and end waypoints reach the same pad pair", at=list(pts[0]))
    for t, r, verb in ((ta, runs[0], "start"), (tb, runs[-1], "end")):
        for pad in (t["p"], t["n"]):
            if r["layer"] not in pad["layers"]:
                return no(f"pad {pad['ref']}.{pad['num']} is not on {r['layer']}: {verb} the centre line on the pad's layer",
                          at=list(t["c"]))
    nets_set = set(nets)
    stripped = {"items": sum(1 for s in B["segs"] if s["net"] in nets_set) + sum(1 for v in B["vias"] if v["net"] in nets_set),
                "locked": sum(1 for s in B["segs"] if s["net"] in nets_set and s["locked"])
                + sum(1 for v in B["vias"] if v["net"] in nets_set and v["locked"])}
    t0, t1 = runs[0]["t0"], runs[-1]["t1"]
    slack = max(0.0, clr - gap0)                        # the pair's own gap may sit below the routing clearance (mate, as pairs.py)
    limit = PR.guard_limit(cons, P_net)
    items = G.Items.of_board(B, old_nets=nets_set)
    skip, base_n = nets_set, items.mark()
    via_lim = RP.return_via_limit(cons)
    Pl = RP.Planes(board, B["copper"]) if vias else None
    fb = lambda pad, e, lay, net: PL.stub(B, items, base_n, set(), pad, e, lay, wmap[lay], net, clr, skip)

    def attempt(sp):
        cp, p0, p1 = PL.legs(runs, vias, dist, hv, wmap, via_d, sp)
        cn, n0, n1 = PL.legs(runs, vias, dist, hv, wmap, via_d, -sp)
        mark = items.mark()
        try:
            g1, it1 = PL.check(items, cp, P_net, clr, skip)
            if g1 < clr - 1e-6:
                return None, f"{P_net} centre line {g1:.3f} mm from {it1['label']} (exact check)", blk(it1, cp)
            PR._claim(items, cp, P_net)
            g2, it2 = PL.check(items, cn, N_net, clr, skip, (P_net, slack))
            if g2 < clr - 1e-6:
                return None, f"{N_net} centre line {g2:.3f} mm from {it2['label']} (exact check)", blk(it2, cn)
            PR._claim(items, cn, N_net)
            skew0 = PL.piece_len(cp) - PL.piece_len(cn) + ta.get("bias", 0.0) + tb.get("bias", 0.0)   # terminal bias, as pairs.py
            ba = PL.breakout(items, (ta["p"], ta["n"]), (p0, n0), t0, runs[0]["layer"], w0, nets, clr, skip, bias=skew0,
                             fallback=fb, gap=gap0)
            if ba[0] is None:
                return None, ba[1], blk(ba[2], [("seg", runs[0]["layer"], ta["c"], ta["c"], w0)], ta["c"])
            PR._claim(items, ba[0], P_net)
            PR._claim(items, ba[1], N_net)
            wl = wmap[runs[-1]["layer"]]
            bb = PL.breakout(items, (tb["p"], tb["n"]), (p1, n1), (-t1[0], -t1[1]), runs[-1]["layer"], wl, nets, clr, skip,
                             bias=skew0 + ba[2], fallback=fb, gap=float(tab[runs[-1]["layer"]]["gap"]))
            if bb[0] is None:
                return None, bb[1], blk(bb[2], [("seg", runs[-1]["layer"], tb["c"], tb["c"], wl)], tb["c"])
            PR._claim(items, bb[0], P_net)
            PR._claim(items, bb[1], N_net)
            skew = skew0 + ba[2] + bb[2]
            if abs(skew) > limit:                       # the short leg gets the bumps, on the side away from its partner
                short, net_s, side = (cn, N_net, -sp) if skew > 0 else (cp, P_net, sp)
                bumped, left = PL.bump(items, short, net_s, abs(skew), side, clr, skip, 0, len(short),
                                       hmax=max(0.6, min(2.0, abs(skew) / 6)), mate=P_net if skew > 0 else N_net,
                                       gap={l: float(g["gap"]) for l, g in tab.items() if g.get("gap")})
                if skew > 0:
                    cn = bumped
                else:
                    cp = bumped
                if left > Pm["skew_frac"] * limit:
                    return None, f"skew {left:.2f} mm over the {limit:g} mm limit after compensation", {"at": list(pts[0]), "blocker": None}
            # PHASE3G G1b a: one return via per layer change where the reference plane changes (returnpath's test, one site)
            rv, rv_missing, placed = [], [], []
            for j, v in enumerate(vias):
                need = RP.ref_need(Pl, {runs[j]["layer"], runs[j + 1]["layer"]})
                if not need:
                    continue
                missing, _n = RP.return_missing(Pl, B["vias"] + [{"net": n, "x": x, "y": y} for n, x, y in placed],
                                                v["at"][0], v["at"][1], P_net, need, via_lim)
                if not missing:
                    continue
                rnets = missing[0][1]
                ref_net = sorted(rnets)[0] if rnets else None
                site = None
                ax, ay = v["at"]
                # the 8 neighbours at via pitch + clearance, nearest ring first; rings step 0.1 mm out to the limit (the first
                # ring sits inside the pair's own via pieces for a straight run, so one ring alone rarely fits)
                pitch = via_d + clr
                rings = [pitch + 0.1 * k for k in range(int(max(0.0, via_lim - pitch) / 0.1) + 1)] if ref_net is not None else []
                for rad in rings:
                    for ux, uy in PL.DVEC:
                        c = (ax + ux * rad, ay + uy * rad)
                        g, _it = PL.check(items, [("via", c[0], c[1], via_d)], ref_net, clr, skip)
                        if g >= clr - 1e-6:
                            site = c
                            break
                    if site is not None:
                        break
                if site is None:
                    rv_missing.append([round(ax, 3), round(ay, 3)])
                    continue
                piece = ("via", site[0], site[1], via_d)
                PR._claim(items, [piece], ref_net)
                rv.append((ref_net, piece))
                placed.append((ref_net, site[0], site[1]))
            Pp, Np = ba[0] + cp + bb[0], ba[1] + cn + bb[1]
            bo = sum(PL.piece_len(x) for x in (ba[0], ba[1], bb[0], bb[1]))
            return {"P": Pp, "N": Np, "bo": bo, "p_mm": round(PL.piece_len(Pp), 3), "n_mm": round(PL.piece_len(Np), 3), "sign": sp,
                    "rv": rv, "rv_missing": rv_missing}, None, None
        finally:
            items.rollback(mark)

    got, refusals = [], []
    for sp in (1, -1):
        r, msg, info = attempt(sp)
        if r is not None:
            got.append(r)
        else:
            refusals.append({"sign": sp, "why": msg, "at": (info or {}).get("at"), "blocker": (info or {}).get("blocker")})
    if not got:
        first = refusals[0]
        return {"laid": False, "why": "; ".join(f"{x['why']}" for x in refusals)[:300], "p_mm": None, "n_mm": None,
                "at": first["at"], "blocker": first["blocker"], "refusals": refusals}
    best = min(got, key=lambda r: r["bo"])
    laid = {P_net: best["P"], N_net: best["N"]}
    for net, piece in best["rv"]:
        laid.setdefault(net, []).append(piece)
    res = {"strip": nets, "kill": [], "keep": [], "laid": laid, "B": B}
    if out is None:
        tmp = board + ".centre.tmp.kicad_pcb"
        PR.write(board, tmp, res, lambda net: rule["drill"])
        os.replace(tmp, board)
    else:
        PR.write(board, out, res, lambda net: rule["drill"])
    return {"laid": True, "why": "", "p_mm": best["p_mm"], "n_mm": best["n_mm"], "layer": runs[0]["layer"],
            "layers": [r["layer"] for r in runs], "vias": len(vias), "sign": best["sign"], "stripped": stripped,
            "return_via_missing": best["rv_missing"]}


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
        live = {x for x in names(members) if st.get(x) == "rerouted"}   # "rerouted" lives only on a kept group board
        if sum(st.get(x) in ("rerouted", "already_ok") for x in names(members)) > before:
            cur = tmp
        else:
            live = set()
        gap_retry = False
        fails = [p for p in members if st.get(f"{p[0]}/{p[1]}") not in ("rerouted", "already_ok")]
        fams = {ZG.family_of(cons, p[0]) for p in fails} - {None}
        fams = {f for f in fams if "gap_mm" not in (cons.get("pairs", {}).get(f) or {})}
        remaining = total - (time.monotonic() - t0)              # once per check: elapsed is not subtracted twice
        if fails and fams and remaining > 1:
            cons2 = copy.deepcopy(cons)
            for f in fams:
                cons2["pairs"][f]["gap_mm"] = gap
            tmp2 = f"{out}.g{i}r.kicad_pcb"
            tmps.append(tmp2)
            before2 = _complete(cur, members, cons)
            rep2 = reroute(cur, tmp2, names(fails), cons2, spec, budget=max(1.0, remaining), allow_shove=True,
                           unlock=True, log=log)
            st2 = {r["pair"]: r["status"] for r in rep2["pairs"]}
            if _complete(tmp2, members, cons) > before2:
                cur, gap_retry = tmp2, True
                live |= {x for x in names(fails) if st2.get(x) == "rerouted"}
                st.update(st2)
            log(f"  regroup {g}: gap_mm {gap:g} retry on {', '.join(sorted(fams))}: {'kept' if gap_retry else 'no gain'}")
        for x in names(members):                             # a "rerouted" pair whose group board was discarded is failed
            if st.get(x) == "rerouted" and x not in live:
                st[x] = "board_discarded"
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
