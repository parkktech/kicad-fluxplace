"""Final-routing tournament (D71ct): KRT variants (ordering, bus, via/turn cost, MPS options) each judged by a KRT
--quick 2 lean copy (lookahead.lean_krt) + route_review blame (placetour.judge_key); the top N run the full krtchain;
the best full board (gaps.measure: shorts, opens, errors; then judge vias) is the route stage output."""
import json, os, time
from concurrent.futures import ThreadPoolExecutor

DEFAULT_VARIANTS = [
    {"name": "mps", "flags": []},
    {"name": "inside_out", "flags": ["--ordering", "inside_out"]},
    {"name": "bus", "flags": ["--ordering", "bus", "--bus"]},
    {"name": "mps_rev", "flags": ["--mps-reverse-rounds"]},
    {"name": "mps_swap", "flags": ["--mps-layer-swap"]},
    {"name": "via_cheap", "flags": ["--via-cost", "40"]},
    {"name": "via_dear", "flags": ["--via-cost", "150"]},
    {"name": "straight", "flags": ["--turn-cost", "2000", "--mps-layer-swap"]},
]


def params(cons):
    t = dict(((cons or {}).get("route") or {}).get("tournament") or {})
    return {"enabled": t.get("enabled") is True, "judge_min": float(t.get("judge_min", 5)), "top": int(t.get("top", 2)),
            "jobs": int(t.get("jobs", 4)), "full_jobs": int(t.get("full_jobs", 2)),
            "variants": [dict(v) for v in (t.get("variants") or DEFAULT_VARIANTS)]}


def full_key(board):
    """(shorts, opens, errors) of a full-route output, smaller is better; no board: worst."""
    from . import gaps as GP
    if not board or not os.path.exists(board):
        return (10**9, 10**9, 10**9)
    m = GP.measure(board)
    return (int(m.get("shorts", 0)), int(m.get("opens", 0)), int(m.get("errors", 0)))


class _Sub:
    """the stage context with its own dir root and krt_extra; every other attribute is the parent's."""
    def __init__(self, c, root, extra):
        self._c, self._root, self.krt_extra = c, root, list(extra)

    def dir(self, n, name):
        p = os.path.join(self._root, f"{n}-{name}")
        os.makedirs(p, exist_ok=True)
        return p

    def __getattr__(self, k):
        return getattr(self._c, k)


def _progress(path, state, msg, **kw):
    json.dump({"stage": "routetour", "state": state, "pid": os.getpid(), "current_step": msg, "updated": time.time(), **kw},
              open(path, "w"), indent=1)


def _table(path, rows):
    with open(path, "w") as fh:
        fh.write("| phase | variant | key | secs | board |\n|---|---|---|---|---|\n")
        for r in rows:
            fh.write(f"| {r['phase']} | {r['name']} | {r['key']} | {r['secs']} | {r.get('board') or ''} |\n")


def run(c, n, board):
    from . import lookahead as LA, placetour as PT, krtchain as KC
    P = params(c.cons)
    d = c.dir(n, "route")
    spath, tpath = os.path.join(d, "progress.json"), os.path.join(d, "TOURNAMENT.md")
    rows = []

    def judge(v):
        t0 = time.time()
        vd = os.path.join(d, "judge", v["name"])
        os.makedirs(vd, exist_ok=True)
        routed = LA.lean_krt(c.a, board, vd, P["judge_min"], extra=v["flags"])
        blame = PT._review(c.a, routed, os.path.join(vd, "review.json")) if routed else None
        return {"phase": "judge", "name": v["name"], "flags": v["flags"], "key": PT.judge_key(routed, blame, c.cons),
                "secs": round(time.time() - t0, 1), "board": routed}
    _progress(spath, "running", f"judge {len(P['variants'])} variants")
    with ThreadPoolExecutor(max_workers=max(1, P["jobs"])) as ex:
        judged = list(ex.map(judge, P["variants"]))
    judged.sort(key=lambda r: r["key"])
    rows += judged
    _table(tpath, rows)

    def full(r):
        t0 = time.time()
        sub = _Sub(c, os.path.join(d, "full", r["name"]), r["flags"])
        res = KC.run(sub, n, board)
        out = res.get("board")
        return {"phase": "full", "name": r["name"], "flags": r["flags"], "key": full_key(out) + (r["key"][3],),
                "secs": round(time.time() - t0, 1), "board": out, "res": res}
    top = judged[:max(1, P["top"])]
    _progress(spath, "running", f"full route of {[r['name'] for r in top]}")
    with ThreadPoolExecutor(max_workers=max(1, P["full_jobs"])) as ex:
        fulls = list(ex.map(full, top))
    fulls.sort(key=lambda r: r["key"])
    rows += [{k: v for k, v in r.items() if k != "res"} for r in fulls]
    _table(tpath, rows)
    win = fulls[0]
    with open(tpath, "a") as fh:
        fh.write(f"\nwinner: {win['name']} {win['flags']} key {win['key']} board {win['board']}\n")
    _progress(spath, "done", f"winner {win['name']} key {win['key']}", board=win["board"])
    res = dict(win["res"])
    res["line"] = f"routetour winner {win['name']} (shorts, opens, errors, vias) {win['key']}; " + str(res.get("line", ""))
    return res
