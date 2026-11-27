"""hand-repair: deterministic driver for steps 1-2 of the repair ladder (docs/ROUTING-PLAYBOOK.md) on a hand-route session.

For each open connection, most constrained first: suggest -> lay; else suggest --explore, remove the cheapest unlocked blocker
set, lay the blocked net, re-lay each removed net (suggest, then copy-net from the session start board). An attempt that leaves
anything open that was not open before is undone whole. No part moves; pair nets are reported, never shoved or routed."""
import json
import os
import time

from . import handroute as H


def _key(c):
    return (c["net"], tuple(sorted((c["a"].get("id") or c["a"].get("pin") or "", c["b"].get("id") or c["b"].get("pin") or ""))))


def _pairs(cons_path, names):
    if not cons_path:
        return set()
    from . import constraints as C, krtchain as K
    return set(K.pair_nets(C.load(cons_path), names, None))


def _rank(c, r):
    """smaller = more constrained: fewest via sites, then smallest reached region; a found path is the least constrained."""
    sb = r.get("sealed_by") or {}
    if r.get("found") or not sb:
        return (1, 0, 0)
    return (0, sb.get("via_sites_in_window", 0), sum(v["cells"] for v in sb.get("reached_cells", {}).values()))


def _unc(work):
    return H.session_status(work)["now"]["by_net"]


def _undo_to(work, n0):
    while H.session_status(work)["edits_applied"] > n0:
        if not H.session_undo(work).get("undone"):
            break


def _same_conn(work, cons, key):
    for i, c in enumerate(H.open_nets(work, cons)["connections"]):
        if _key(c) == key:
            return i, c
    return None, None


def _relay(work, cons, net, start, kw):
    """close every open connection of `net`: suggest+lay, else copy-net from the session start board."""
    left = 0
    for _ in range(12):
        recs = [(i, c) for i, c in enumerate(H.open_nets(work, cons)["connections"]) if c["net"] == net]
        if not recs:
            return 0
        i, c = recs[0]
        r = H.suggest(work, net, conn=i, constraints=cons, **kw)
        if r.get("found") and H.lay(work, net, r["path"], None, cons)["accepted"]:
            continue
        cn = H.copy_net(start, work, net, None, cons)
        return 0 if cn["accepted"] else len(recs)
    return left or 1


def repair(work, out, constraints=None, max_attempts=2, budget=900.0, layers=None, report=None, pitch=0.05):
    t0 = time.time()
    rows = []
    res = {"connections": rows, "closed": 0, "blocked": 0, "pairs": 0, "elapsed_s": 0.0, "budget_s": budget}

    def save():
        res["elapsed_s"] = round(time.time() - t0, 1)
        res["closed"] = sum(1 for r in rows if r["status"] == "closed")
        res["blocked"] = sum(1 for r in rows if r["status"] == "blocked")
        res["pairs"] = sum(1 for r in rows if r["status"] == "pair")
        if report:
            json.dump(res, open(report, "w"), indent=1, default=str)

    try:
        S = H.Sess(work)
        if not S.exists:
            raise RuntimeError("repair needs a session on WORK (session begin SRC --work WORK)")
        start = os.path.join(S.dir, "start.kicad_pcb")
        kw = {"pitch": pitch, "layers": layers.split(",") if isinstance(layers, str) else layers}
        names = {c["net"] for c in H.open_nets(work, constraints)["connections"]}
        pairs = _pairs(constraints, names | set(_unc(work)))
        done, committed = set(), 0
        while time.time() - t0 < budget:
            conns = H.open_nets(work, constraints)["connections"]
            todo = [(i, c) for i, c in enumerate(conns) if _key(c) not in done]
            if not todo:
                break
            scored = []
            for i, c in todo:
                if time.time() - t0 >= budget:
                    break
                if c["net"] in pairs:
                    done.add(_key(c))
                    rows.append({"net": c["net"], "a": c["a"].get("pin"), "b": c["b"].get("pin"), "status": "pair", "note": "pair net: needs the pair re-route, not handled here"})
                    continue
                scored.append((_rank(c, H.suggest(work, c["net"], conn=i, constraints=constraints, **kw)), i, c))
            if not scored:
                continue
            scored.sort(key=lambda t: t[0])
            _, i, c = scored[0]
            key = _key(c)
            done.add(key)
            rows.append(_one(work, constraints, key, c, start, kw, max_attempts, pairs, t0, budget))
            if rows[-1]["status"] == "closed":           # w78: commit per closed connection, a kill at the deadline keeps what is closed
                res["commit"] = H.session_commit(work, out, constraints)
                committed = len(rows)
            save()
        res["budget_exhausted"] = time.time() - t0 >= budget
        if committed != len(rows) or "commit" not in res:
            res["commit"] = H.session_commit(work, out, constraints)
    except Exception as e:
        res["error"] = {"type": type(e).__name__, "message": str(e)}
        save()
        raise
    save()
    return res


def _one(work, cons, key, c, start, kw, max_attempts, pairs, t0, budget):
    net = c["net"]
    row = {"net": net, "a": c["a"].get("pin"), "b": c["b"].get("pin"), "status": "blocked", "attempts": 0}
    for att in range(max_attempts):
        if time.time() - t0 >= budget:
            row["note"] = "budget exhausted"
            break
        row["attempts"] = att + 1
        i, c = _same_conn(work, cons, key)
        if i is None:
            row["status"] = "closed"
            return row
        r = H.suggest(work, net, conn=i, constraints=cons, **kw)
        if r.get("found"):
            lr = H.lay(work, net, r["path"], None, cons)
            if lr["accepted"]:
                row["status"] = "closed"
                return row
            row["lay_rejected"] = lr.get("reason")
            continue
        row["seal"] = {"reason": r.get("reason"), "limiting": (r.get("sealed_by") or {}).get("limiting_obstacles", [])[:4],
                       "via_sites": (r.get("sealed_by") or {}).get("via_sites_in_window")}
        ex = H.suggest(work, net, conn=i, constraints=cons, explore=True, **kw)
        opts = [e for e in ex.get("explore") or [] if e.get("then_found") and not any(x["net"] in pairs for x in e["remove"])]
        if not opts:
            row["note"] = "no unlocked blocker set frees it (locked copper, pair net, or part move needed)"
            break
        pick = min(opts, key=lambda e: (len(e["remove"]), e.get("length_mm") or 1e9))
        n0 = H.session_status(work)["edits_applied"]
        before = dict(_unc(work))
        ok = True
        by_net = {}
        for x in pick["remove"]:
            by_net.setdefault(x["net"], []).append(x["id"])
        for bn, ids in by_net.items():
            if not H.shove(work, bn, None, ids, None, True, cons)["accepted"]:
                ok = False
                break
        if ok:
            i2, c2 = _same_conn(work, cons, key)
            r2 = H.suggest(work, net, conn=i2, constraints=cons, **kw) if i2 is not None else {"found": True}
            if i2 is not None:
                ok = bool(r2.get("found")) and H.lay(work, net, r2["path"], None, cons)["accepted"]
        left = 0
        if ok:
            left = sum(_relay(work, cons, bn, start, kw) for bn in by_net)
            after = _unc(work)
            worse = {n: (before.get(n, 0), v) for n, v in after.items() if v > before.get(n, 0) and n != net}
            ok = left == 0 and not worse and _same_conn(work, cons, key)[0] is None
        if ok:
            row["status"] = "closed"
            row["removed_and_relaid"] = sorted(by_net)
            return row
        _undo_to(work, n0)
        row["attempt_undone"] = {"removed": sorted(by_net), "left_open": left}
    return row
