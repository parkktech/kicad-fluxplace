"""Pairs-stage completion ladder: every differential pair leaves the stage in exactly one state, reported per pair.

  a       complete, coupled, laid by the main pass (also: a pair that was already connected)
  b(i)    completed by another via / layer pattern (cheaper vias, inner layers at face cost, a bigger search)
  b(ii)   completed after ripping what blocked it: single-ended fan-out stubs near its terminals (their pins are escaped
          again) or the copper of an earlier LOWER-priority pair (both pairs are laid again; the trial is kept only
          when every ripped pair is complete again)
  b(iii)  completed with a bounded uncoupled break-out at the terminals (`[pairs.FAMILY] max_uncoupled_mm`, else
          `[pairlay] max_uncoupled_mm`, default DEFAULT_UNCOUPLED_MM) and the skew allowed up to its own limit
  c       none of the above: the pair's own partial copper is not written (reconcile(): a pair that has copper on the board is
          never c), the two nets are handed to the general
          router as ordinary nets of the pair's class (`handoff`), and `pairuncoupled` reports what it made of them
  a-mismatch  connected, but the legs differ in layer sequence / via count: the copper is kept (connected beats open)

Every trial runs on a copy of the stage's board through `pairs.run` (which checks each piece against foreign copper and
audits the written board); a trial is kept only when it completes a pair that was not complete and every pair it ripped
is complete again, so a step never leaves the board worse than it found it."""
import copy
import math
import os
import re
import shutil

DEFAULT_UNCOUPLED_MM = 5.0        # 10 pitches of a 0.5 mm part: about the length one fan-out escape needs to leave a pad row
STATES = ("a", "b(i)", "b(ii)", "b(iii)", "c")
_BLOCK = re.compile(r"^(track|via) (\S*)(?: .*)?$")


def assign_states(rep):
    """first state of every row of a pairs report (the main pass): a / a-mismatch / None (still to complete)."""
    for r in rep["pairs"]:
        # `unknown` (ungraded: unmeasured / MEASURE_DISAGREE) with both legs connected is copper, never an open pair (W16 ETH_P0/P3)
        r["state"] = "a" if r["status"] == "complete" else "a-mismatch" if r["status"] == "mismatch" or (
            r["status"] == "unknown" and r.get("open") == [0, 0]) else None
        r.setdefault("tried", [])


def pending(rep):
    return [r for r in rep["pairs"] if r.get("state") is None]


def blocker_nets(row):
    """names of the nets the failure lines point at: {'track MIC_N F.Cu', 'via GND', 'pad U1.1 (X)'} -> {'MIC_N', 'GND'}
    (copper only: a pad blocker is a part, not something to rip)."""
    out = set()
    for b in row.get("blockers") or []:
        m = _BLOCK.match(b.strip())
        if m and m.group(2) and not m.group(2).startswith("("):
            out.add(m.group(2))
    return out


def variant(cons, **kv):
    c = copy.deepcopy(cons or {})
    c.setdefault("pairlay", {}).update(kv)
    return c


def uncoupled_limit(cons, pair, default=None):
    """[pairs.FAMILY] max_uncoupled_mm (longest matching family) > [pairlay] max_uncoupled_mm > DEFAULT_UNCOUPLED_MM."""
    fams = (cons or {}).get("pairs") or {}
    fam = max((f for f in fams if isinstance(fams[f], dict) and pair[0].startswith(f)), key=len, default=None)
    if fam and "max_uncoupled_mm" in fams[fam]:
        return float(fams[fam]["max_uncoupled_mm"])
    pl = (cons or {}).get("pairlay") or {}
    return float(pl.get("max_uncoupled_mm", default if default is not None else DEFAULT_UNCOUPLED_MM))


def stub_candidates(B, terminals, pair_nets, plane_nets, nets, reach):
    """copper of single-ended nets in `nets` (the nets the failure line names, so already local to the failure) that sits within
    `reach` mm of a terminal (None = anywhere) and is only a fan-out stub:
    a connected piece of at most 4 segments + vias, at most 6 mm, touching exactly one pad.  -> [("seg"|"via", item)]."""
    out = []
    pads = [p for p in B["pads"] if not p.get("np")]
    for net in sorted(nets):
        if net in pair_nets or net in plane_nets:
            continue
        items = [("seg", s) for s in B["segs"] if s["net"] == net] + [("via", v) for v in B["vias"] if v["net"] == net]
        if not items:
            continue
        npads = [p for p in pads if p["net"] == net]
        # connected pieces by shared end points / via centres (0.02 mm)
        pts = lambda k, o: ([o["a"], o["b"]] if k == "seg" else [(o["x"], o["y"])])
        comp = list(range(len(items)))

        def find(i):
            while comp[i] != i:
                comp[i] = comp[comp[i]]
                i = comp[i]
            return i
        for i, (ki, oi) in enumerate(items):
            for j in range(i):
                kj, oj = items[j]
                if any(math.hypot(a[0] - b[0], a[1] - b[1]) <= 0.02 + (oi["d"] if ki == "via" else 0) / 2 + (oj["d"] if kj == "via" else 0) / 2
                       for a in pts(ki, oi) for b in pts(kj, oj)):
                    comp[find(i)] = find(j)
        groups = {}
        for i in range(len(items)):
            groups.setdefault(find(i), []).append(items[i])
        for g in groups.values():
            if len(g) > 4:
                continue
            ln = sum(math.hypot(o["a"][0] - o["b"][0], o["a"][1] - o["b"][1]) for k, o in g if k == "seg")
            if ln > 6.0:
                continue
            touch = {id(p) for p in npads for k, o in g for q in pts(k, o) if math.hypot(q[0] - p["x"], q[1] - p["y"]) <= max(p.get("w", 0.3), p.get("h", 0.3)) / 2 + 0.05}
            if len(touch) != 1:
                continue
            near = reach is None or any(math.hypot(q[0] - t[0], q[1] - t[1]) <= reach for k, o in g for q in pts(k, o) for t in terminals)
            if near:
                out += g
    return out


def _remove(src, dst, items):
    """copy of board `src` at `dst` without `items` ([(kind, item)]); the project file travels along."""
    from . import boardtext as BT
    text = open(src, errors="replace").read()
    ks, kv = set(), set()
    r4 = lambda v: round(v, 4)
    for k, o in items:
        if k == "seg":
            ks.add((tuple(sorted(((r4(o["a"][0]), r4(o["a"][1])), (r4(o["b"][0]), r4(o["b"][1]))))), o["layer"], o["net"]))
        else:
            kv.add((r4(o["x"]), r4(o["y"]), o["net"]))
    keys = []
    for _i, _j, key, blk in BT.scan(text):
        net = BT.net_name(blk)
        if not net:
            continue
        if key[0] == "s" and (tuple(sorted((key[1], key[2]))), key[4], net) in ks:
            keys.append(key)
        elif key[0] == "v" and (key[1], key[2], net) in kv:
            keys.append(key)
    if keys:
        text = BT.remove_blocks(text, keys)
    open(dst, "w").write(text)
    _project(src, dst)
    return len(keys)


def _project(src, dst):
    for ext in (".kicad_pro", ".kicad_prl"):
        s = os.path.splitext(src)[0] + ext
        if os.path.exists(s):
            shutil.copy(s, os.path.splitext(dst)[0] + ext)


def _drop(*paths):
    """delete working boards and their sidecars."""
    for p in paths:
        if not p:
            continue
        stem = os.path.splitext(p)[0]
        for f in (p, stem + ".kicad_pro", stem + ".kicad_prl", stem + ".fluxmap.json", stem + ".pairs.json"):
            try:
                os.remove(f)
            except OSError:
                pass


def _take(rep, sub, state, only_rows=None):
    """rows of `sub` that are complete replace the pending rows of `rep` (state `state`); ripped pairs that are complete again
    keep their state.  -> number of pending pairs completed."""
    by = {tuple(r["pair"]): r for r in sub["pairs"]}
    n = 0
    for i, r in enumerate(rep["pairs"]):
        s = by.get(tuple(r["pair"]))
        if not s:
            continue
        if r.get("state") is None and s["status"] in ("mismatch", "unknown") and s.get("open") == [0, 0]:
            # the kept trial board carries this pair's copper (connected, graded mismatch / not graded): the report says so
            rep["pairs"][i] = dict(s, state="a-mismatch", tried=r.get("tried", []))
            continue
        if s["status"] != "complete":
            continue
        if r.get("state") is None:
            rep["pairs"][i] = dict(s, state=state, tried=r.get("tried", []))
            n += 1
        elif only_rows and tuple(r["pair"]) in only_rows:      # a ripped pair laid again
            rep["pairs"][i] = dict(s, state=r["state"], tried=r.get("tried", []), relaid=True)
    return n


def _trial(rep, out, cons_v, spec, run_fn, tag, state, log, src=None, victims=(), cs_args=None, escape=None, ripped_nets=()):
    """one trial on a copy of `out`; kept (moved onto `out`) when it completes a pending pair and every victim is complete again."""
    pend = pending(rep)
    if not pend:
        return 0
    d = os.path.dirname(out)
    srcb = src or out
    trial = os.path.join(d, f"fb-{tag}.kicad_pcb")
    only = {n for r in pend for n in r["pair"]} | {n for v in victims for n in v}
    sub = run_fn(srcb, cons_v, spec, trial, only=only, fallback=False)
    got = {tuple(r["pair"]) for r in sub["pairs"] if r["status"] == "complete"}
    new = [r for r in pend if tuple(r["pair"]) in got]
    ok = bool(new) and all(tuple(v) in got for v in victims)
    log(f"pairs fallback {tag}: {len(new)} of {len(pend)} pending pair(s) completed" + (f", {len(victims)} ripped pair(s) laid again" if victims else "")
        + ("" if ok else " (trial discarded)"))
    for r in pend:                                              # what was tried, per pair
        r.setdefault("tried", []).append(f"{state}:{'ok' if ok and tuple(r['pair']) in got else 'no'}")
    if ok:
        shutil.move(trial, out)
        _project(trial, out)
        _take(rep, sub, state, only_rows={tuple(v) for v in victims})
        if ripped_nets and escape:
            rep.setdefault("fallback_escape", {})[tag] = escape(out, sorted(ripped_nets))
    _drop(trial, src)
    return len(new) if ok else 0


def reconcile(rep, aud, opn):
    """report = board: every row, state c included, takes its status / opens / lengths from the audit of the written board.  A
    pair the ladder left in state c whose legs are both connected on that board HAS copper (a trial lays more pairs than it is
    credited with): it is `a` / `a-mismatch`, never handed to the router.  Only pairs without copper stay c / handoff."""
    for r in rep["pairs"]:
        a = aud[tuple(r["pair"])]
        r["open"] = [opn.get(r["pair"][0], 0), opn.get(r["pair"][1], 0)]
        r["status"] = a["status"]
        if a["status"] in ("complete", "mismatch"):
            r.update(length=[a["p"]["length"], a["n"]["length"]], skew=a["skew"], vias=a["p"]["vias"], layers=a["p"]["layers"])
            if a["status"] == "mismatch":
                r["audit"] = a["why"]
        if r.get("state") == "c" and r["open"] == [0, 0]:
            r["state"], r["handoff"] = ("a" if a["status"] == "complete" else "a-mismatch"), False
            for k in ("handoff_reason", "handoff_reasons", "handoff_detail"):
                r.pop(k, None)
            r.setdefault("tried", []).append("reconciled: copper on the board")
    return rep


def _effort_reasons(row):
    why = row.get("why") or ""
    reasons = []
    if "search budget" in why:
        reasons.append("budget_reached")
    if "no path for the coupled centre line" in why or "no corridor" in why:
        reasons.append("no_corridor")
    if any(text in why for text in ("no clear break-out", "no two terminals", "single terminal", "unpaired pad")):
        reasons.append("escape_blocked")
    if "taken back: pair audit" in why:
        reasons.append("audit_rejected")
    return reasons or ["unsolved"]


def handoff_effort(rep, P):
    """Annotate handed-off pairs with measured causes and configuration-skipped effort; never changes routing decisions."""
    counts = {}
    for r in rep["pairs"]:
        if not r.get("handoff"):
            continue
        reasons = _effort_reasons(r)
        skipped = not P.get("fallback", True) or P.get("fb_trials", 12) <= 0
        if skipped:
            reasons.append("fallback_trials_skipped_by_configuration")
        r["handoff_reason"] = reasons[0]
        r["handoff_reasons"] = reasons
        r["handoff_detail"] = {"why": r.get("why") or "", "measured_blockers": list(r.get("blockers") or []),
                               "fallback_trial_records": len(r.get("tried") or []),
                               "fallback_trials_skipped_by_configuration": skipped}
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
    ordered = ["budget_reached", "no_corridor", "escape_blocked", "audit_rejected", "fallback_trials_skipped_by_configuration"]
    ordered += sorted(k for k in counts if k not in ordered)
    labels = {"budget_reached": "budget reached", "no_corridor": "no corridor", "escape_blocked": "escape blocked",
              "audit_rejected": "audit rejected", "fallback_trials_skipped_by_configuration": "fallback trials skipped by configuration",
              "unsolved": "unsolved"}
    summary = "pairs handoff effort: " + "; ".join(f"{labels.get(k, k.replace('_', ' '))} {counts.get(k, 0)}" for k in ordered)
    rep["handoff_effort"] = {"counts": {k: counts.get(k, 0) for k in ordered}, "summary": summary}
    return rep["handoff_effort"]


def ladder(rep, out, cons, spec, P, run_fn, cs_args=None, escape=None, log=lambda m: None):
    """run the fallback ladder on the written board `out` for the pending pairs of `rep` (edited in place), then set every
    remaining pending pair to state c with handoff = True.  -> rep."""
    from . import routediag as RD, netclass as NC, pairaudit as PA
    t0 = __import__("time").time()
    steps = []
    if pending(rep) and P.get("fallback", True):
        # b(i): another via / layer pattern
        k = 0
        for kv in ({"via_cost_mm": P["via_cost"] * 0.4, "inner_cost": 1.0, "max_nodes": P["nodes"] * 3, "negotiate": False, "passes": 2},
                   {"via_cost_mm": min(3.0, P["via_cost"] * 0.15), "turn_cost_mm": P["turn_cost"] * 0.5, "inner_cost": 1.0,
                    "max_nodes": P["nodes"] * 5, "negotiate": False, "passes": 2, "via_exit": False}):
            k += 1
            if pending(rep):
                steps.append(("b(i)", _trial(rep, out, variant(cons, **kv), spec, run_fn, f"i{k}", "b(i)", log)))
        # b(i)-tight: the fab-limit geometry, only for families that allow it ([pairs] / [pairs.F] allow_fab_limit = true)
        if pending(rep) and (((cons or {}).get("pairs") or {}).get("allow_fab_limit") is True or any(
                isinstance(v, dict) and v.get("allow_fab_limit") is True for v in ((cons or {}).get("pairs") or {}).values())):
            steps.append(("b(i)", _trial(rep, out, variant(cons, tight_geometry=True, max_nodes=P["nodes"] * 3, negotiate=False, passes=2),
                                         spec, run_fn, "i-tight", "b(i)", log)))
        # b(ii): rip blockers (stubs of single-ended nets near the terminals, lower-priority pair copper)
        if pending(rep):
            steps.append(("b(ii)", _rip(rep, out, cons, spec, P, run_fn, escape, cs_args, log)))
        # b(iii): bounded uncoupled break-outs, skew up to the limit
        if pending(rep):
            groups = {}
            for r in pending(rep):
                groups.setdefault(uncoupled_limit(cons, r["pair"], P.get("max_uncoupled") or None), []).append(r["pair"])
            for g, (lim, prs) in enumerate(sorted(groups.items())):
                if not pending(rep):
                    break
                cv = variant(cons, max_uncoupled_mm=lim, skew_frac=1.0, max_nodes=P["nodes"] * 3, negotiate=False, passes=2,
                             via_cost_mm=P["via_cost"] * 0.4)
                steps.append(("b(iii)", _hold_trial(rep, [r for r in pending(rep) if r["pair"] in prs], out, cv, spec, run_fn, f"iii{g}", "b(iii)", log)))
    for r in pending(rep):
        r["state"], r["handoff"] = "c", True
    handoff_effort(rep, P)
    rep["fallback"] = {"steps": steps, "secs": round(__import__("time").time() - t0, 1)}
    return rep


def _hold_trial(rep, rows, out, cons_v, spec, run_fn, tag, state, log, **kw):
    """a trial for `rows` only: the other pending pairs are held out of it."""
    hold = [r for r in pending(rep) if r not in rows]
    for r in hold:
        r["state"] = "_hold"
    try:
        return _trial(rep, out, cons_v, spec, run_fn, tag, state, log, **kw)
    finally:
        for r in hold:
            if r.get("state") == "_hold":
                r["state"] = None


def _rip(rep, out, cons, spec, P, run_fn, escape, cs_args, log):
    """b(ii): one trial with everything the pending pairs' failure lines blame and that may be ripped; then, when nothing
    was gained, one trial per pending pair."""
    from . import routediag as RD, netclass as NC, pairaudit as PA, pairs as PR
    B = RD.load(out)
    cls = NC.classify(B, cons, spec)
    pairs = [tuple(p) for p in PA.pair_list(cls)]
    pair_of = {n: p for p in pairs for n in p}
    plane_nets = {n for n, i in cls.items() if i.get("class") in ("rail", "rf", "clock", "switch_node")}
    state = {r["pair"][0]: r for r in rep["pairs"]}
    done = [0]
    budget = [P.get("fb_trials", 12)]

    def plan_for(rows):
        stubs, victims = [], []
        for r in rows:
            names = blocker_nets(r)
            terms = []
            try:
                Pm = PR.params(cons)
                for pp in [tuple(r["pair"])]:
                    ts, _lone = PR.terminals(B, pp[0], pp[1], Pm["pad_max"])
                    terms += [t["c"] for t in ts]
            except Exception:                                   # noqa: BLE001
                terms = []
            if P.get("rip_stubs", True):
                stubs += stub_candidates(B, terms, set(pair_of), plane_nets, names - set(pair_of), None)
            for n in names & set(pair_of):
                v = pair_of[n]
                vr = state.get(v[0])
                if vr and vr.get("state") in ("a", "b(i)", "b(ii)", "b(iii)") and v != tuple(r["pair"]) and (vr.get("key") or [0, 0, 0]) > (r.get("key") or [0, 0, 0]):
                    victims.append(v)
        return stubs, sorted(set(victims))

    def attempt(rows, tag):
        if budget[0] <= 0:
            return 0
        stubs, victims = plan_for(rows)
        if not stubs and not victims:
            return 0
        budget[0] -= 1
        items = list(stubs)
        for v in victims:
            items += [("seg", s) for s in B["segs"] if s["net"] in v] + [("via", x) for x in B["vias"] if x["net"] in v]
        src = os.path.join(os.path.dirname(out), f"fb-{tag}-in.kicad_pcb")
        _remove(out, src, items)
        return _hold_trial(rep, rows, out, cons, spec, run_fn, tag, "b(ii)", log, src=src, victims=victims, cs_args=cs_args,
                           escape=escape, ripped_nets={o["net"] for k, o in stubs})

    n = attempt(pending(rep), "ii-all")
    if not n:
        for r in list(pending(rep)):
            if r.get("state") is None:
                B = RD.load(out)
                n += attempt([r], "ii-" + r["pair"][0])
    return n


def handoff_nets(state, work):
    """nets of the pairs the pairs stage handed to the general router (state c): they are ordinary nets of their class
    for the router, never on its skip list.  `state` = pipeline state.json dict, `work` = the workdir."""
    import json
    try:
        d = ((state or {}).get("stages") or {}).get("pairs", {}).get("dir")
        if not d:
            return set()
        rep = json.load(open(os.path.join(work, d, "board.pairs.json")))
        return set(rep.get("handoff") or [])
    except (OSError, ValueError, TypeError, AttributeError):
        return set()


def gated_open_legs(cons, pair_legs, open_names, extra=()):
    """PHASE3I F1: both legs of every gated pair (review.pair_gated) that has a leg open (or in `extra`, the handed-over
    nets). Pairs come from naming over `pair_legs`; `not_pairs` nets are never a pair."""
    from . import graph, review, constraints as C
    pairs = graph.diff_pairs(pair_legs, exclude=C.not_pairs(cons))
    hit = set(open_names) | set(extra)
    out = set()
    for slave, master in pairs.items():
        if review.pair_gated(cons, master) and (slave in hit or master in hit):
            out.update((slave, master))
    return out


def handoff_hs_nets(state, work, cons=None):
    """PHASE3I B1: the legs of handed-over pairs that are gated (high-speed). They never reach the plain router."""
    from . import review as RV
    import json
    try:
        d = ((state or {}).get("stages") or {}).get("pairs", {}).get("dir")
        if not d:
            return set()
        rep = json.load(open(os.path.join(work, d, "board.pairs.json")))
        return {n for r in rep.get("pairs", []) if r.get("handoff") and RV.pair_gated(cons, r["pair"][0]) for n in r["pair"]}
    except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError):
        return set()


def router_skip(todo, controlled, handoff, mode):
    """nets the whole-board router is told to leave alone: the controlled nets (pairs, RF, clocks). PHASE3C D71cj (c): a pair
    leg is never routed as a plain net, handed-over pairs (`handoff`, state c) included; the pairgroups step re-lays them
    per connector group first. `handoff` stays in the signature for the call sites."""
    return [n for n in todo if n in controlled] if mode == "exclude" else []
