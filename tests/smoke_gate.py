"""PHASE3I F10: router-free end-to-end smoke of the gate loop. Run by hand, never collected by pytest:

    /usr/bin/python3 tests/smoke_gate.py BOARD CONSTRAINTS

BOARD is copied (with .kicad_pro / .kicad_dru) into a temp dir; the input is never written. [ai] mode is "session" in
memory. Every rung except `ai` is replaced by a recorder that returns {} (no KRT, no Freerouting, no router). The pairs
gate runs once (gaps.close scoped to gate_kinds("pairs")). Prints a block for the commit message and asserts the gate
contracts. Exit 0 when every assertion holds."""
import collections
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from fluxplace import boardtext as BT, constraints as C, gaps as G, graph as GR, handroute as H  # noqa: E402
from fluxplace import krtchain as K, lockpolicy as LP, pairfb as PFB, review as RV, routeconn as RC  # noqa: E402


def sha1(path):
    return hashlib.sha1(open(path, "rb").read()).hexdigest()


def uuid_head_collisions(path):
    """items whose 8-hex uuid head repeats an earlier item's (the reuuid_collisions condition)."""
    text = open(path, errors="surrogateescape").read()
    found = list(dict.fromkeys(re.findall(r'"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"', text)))
    heads = collections.Counter(u[:8].lower() for u in found)
    return sum(n - 1 for n in heads.values() if n > 1)


def main(board, constraints):
    if not os.path.isfile(board) or not os.path.isfile(constraints):
        raise SystemExit("smoke_gate: BOARD and CONSTRAINTS must be files")
    sha0 = sha1(board)
    tmp = tempfile.mkdtemp(prefix="smoke-gate-")
    ok = True
    try:
        copy = os.path.join(tmp, "in.kicad_pcb")
        shutil.copy(board, copy)
        for ext in (".kicad_pro", ".kicad_dru"):
            if os.path.exists(os.path.splitext(board)[0] + ext):
                shutil.copy(os.path.splitext(board)[0] + ext, os.path.splitext(copy)[0] + ext)
        cons = C.load(constraints)
        cons["ai"] = {"mode": "session"}
        calls, real = [], G._call_rung

        def recorder(name, base, out, gaps, ctx):
            calls.append(name)
            return real(name, base, out, gaps, ctx) if name == "ai" else {}
        G._call_rung = recorder
        kinds = G.gate_kinds(cons, "pairs")
        work = os.path.join(tmp, "W")
        r = G.close(copy, work, constraints, cons, kinds=kinds, budget_min=5, log=lambda m: print("  " + m),
                    runs=os.path.join(tmp, "runs.jsonl"), attempts=os.path.join(tmp, "attempts.jsonl"))
        G._call_rung = real
        gaps = json.load(open(os.path.join(work, "gaps.json")))["gaps"]
        by_kind = collections.Counter(g["kind"] for g in gaps)
        scoped = [g for g in gaps if G.in_scope(g, kinds)]
        briefs = sorted(glob.glob(os.path.join(work, "ai", "*.brief.json")))
        regions = json.load(open(os.path.join(work, "ai", "sessions.json"))) if os.path.exists(os.path.join(work, "ai", "sessions.json")) else []
        print("SMOKE gate pairs kinds=" + ",".join(kinds))
        print("  gaps all: " + ", ".join(f"{k} x{n}" for k, n in sorted(by_kind.items())) + f" (total {len(gaps)})")
        print(f"  gaps in scope: {len(scoped)}")
        print(f"  stop={r['stop']} exit={r['exit']} attempts={r['attempts']} level={r['level']}")
        print(f"  rungs called: {dict(collections.Counter(calls))}")
        print(f"  session regions: {len(regions)}; brief files: {len(briefs)}")
        model = H._model(copy, refill=False)
        print(f"  _id_len={H._id_len(model)}")
        pre = uuid_head_collisions(copy)
        fixed = os.path.join(tmp, "fixed.kicad_pcb")
        shutil.copy(copy, fixed)
        for ext in (".kicad_pro", ".kicad_dru"):
            if os.path.exists(os.path.splitext(copy)[0] + ext):
                shutil.copy(os.path.splitext(copy)[0] + ext, os.path.splitext(fixed)[0] + ext)
        BT.reuuid_collisions(fixed)
        print(f"  prefix collisions before={pre} after reuuid_collisions={uuid_head_collisions(fixed)}")
        nets = sorted({o.net for o in model.obs if o.net})
        hard = [n for n in nets if LP.level(n, cons, nets) == LP.HARD]
        print(f"  HARD nets: {len(hard)} of {len(nets)}")
        pn = K.pair_nets(cons, nets, None)
        open_now = set(RC.open_by_net(copy))
        legs = PFB.gated_open_legs(cons, pn, open_now, ())
        gated = GR.diff_pairs(nets, exclude=C.not_pairs(cons))
        gated_pairs = [(s, m) for s, m in gated.items() if RV.pair_gated(cons, m)]
        print(f"  gated pairs: {len(gated_pairs)}; gated open legs: {len(legs)}")
        # assertions
        if not scoped:
            ok &= check(r["stop"] == "clean" and r["exit"] == 0, "no scoped gap: stop clean, exit 0")
        else:
            ok &= check(r["stop"] in ("packets", "flat", "stalled", "budget"), f"scoped gaps: stop {r['stop']} (not clean)")
            if r["stop"] == "packets":
                ok &= check(r["exit"] == 5 and len(briefs) >= 1, "packets: exit 5 and at least one brief")
        allowed = {x for rungs in G.DISPATCH.values() for x in rungs} | {"ai"}   # DISPATCH is keyed by gap kind
        ok &= check(set(calls) <= allowed, "every recorded rung is a dispatch rung (stubbed except ai)")
        ok &= check(all(LP.level(x, cons, nets) == LP.HARD for s, m in gated_pairs for x in (s, m)),
                    "every leg of every gated pair is HARD")
        excluded = K.route_net_set(nets, set(), legs, open_now & set(nets))
        if isinstance(excluded, list):
            ok &= check(not (set(excluded) & (legs & open_now)), "route_net_set holds no open gated leg")
        ok &= check(sha1(board) == sha0, "BOARD sha1 unchanged")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok &= check(not os.path.exists(tmp), "temp dir removed")
    print("SMOKE RESULT " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    return bool(cond)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: smoke_gate.py BOARD CONSTRAINTS")
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
