"""Phase 1c: AI pre-route of critical nets (D9). The stage writes one packet per open critical net; an AI agent answers
with `ai/actions.json` (order, layers, avoid circles); the stage then lays each suggested path and keeps it only when it
lowers opens and does not raise shorts. Design: docs/recipes/PHASE1C-preroute.md.
"""
import json
import math
import os
import re
import sys
import time

from . import calllog

CLASSES = ("rf", "clock")            # netclass classes pre-routed by default; then the [critroute] nets list
PACKET_MAX = 4096                    # bytes per packet file
MAX_CONNS, MAX_HOT, MAX_AVOID = 6, 4, 6
HOT_MM = 15.0                        # a hot region counts when its centre is within this of a connection end
RF_VIA_COST = 20.0                   # suggest via cost (mm of track) for an RF net: every layer transition is an impedance discontinuity
# k11: search escalation for one connection, (pitch mm, window margin mm, node cap). A search that ran out of window (few nodes,
# no path) gets a wider window; one that hit the node cap (a long connection) gets a coarser grid with a larger cap.
SEARCH = ((0.05, 2.0, 2500000), (0.05, 6.0, 2500000), (0.1, 3.0, 6000000), (0.1, 6.0, 8000000))
GRID_MM = 10.0                       # density.compute default grid: region x/y is the cell corner
ASK = "order, layers, avoid circles [x, y, r] for this net; no coordinates of copper"


class _Refused(Exception):
    """The guarded lay declined the path (its reason is the message): a measured outcome, not an error."""


def _log(msg):
    print("critroute: " + msg, file=sys.stderr)


def _safe(net):
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", net)


def _listed(cons):
    return [str(n) for n in ((cons or {}).get("critroute") or {}).get("nets") or []]


def critical(board, cons, spec, cons_path=None):
    """Open nets to pre-route: netclass classes in CLASSES plus the `[critroute] nets` list, each net once, rf first, then
    clock, then listed nets; nets without an open connection are skipped. -> [{net, class, reason}]."""
    from . import handroute as HR, netclass as NC, routediag as RD
    open_names = {r["net"] for r in HR.open_nets(board, constraints=cons_path)["connections"]}
    cls = NC.classify(RD.load(board), cons, spec)
    rank = {"rf": 0, "clock": 1}
    rows = {}
    for net, info in cls.items():
        if info["class"] in CLASSES:
            rows[net] = {"net": net, "class": info["class"], "reason": info.get("reason") or ""}
    for net in _listed(cons):
        if net not in rows:
            info = cls.get(net, {})
            rows[net] = {"net": net, "class": info.get("class", "listed"), "reason": info.get("reason") or "[critroute] nets"}
    out = [r for r in rows.values() if r["net"] in open_names]
    out.sort(key=lambda r: (rank.get(r["class"], 2), r["net"]))
    return out


def _layers(side):
    return [side["layer"]] if side.get("layer") else []


def _fit(p):
    """Clip to PACKET_MAX: drop `hot` first, then conns beyond 3."""
    while len(json.dumps(p).encode()) > PACKET_MAX:
        if p["hot"]:
            p["hot"] = []
        elif len(p["conns"]) > 3:
            p["conns"] = p["conns"][:3]
        else:
            break
    return p


def packet(board, net, cons, spec, cons_path=None, cls=None, R=None, B=None):
    """Packet for one critical net (keys as the recipe's schema). `cls`, `R`, `B` may be passed in to avoid recomputing."""
    from . import density as DS, gaps, handroute as HR, netclass as NC, routediag as RD
    from . import forbid as FB
    B = FB.augment(B if B is not None else RD.load(board), FB.windows(cons))      # B7
    cls = cls if cls is not None else NC.classify(B, cons, spec)
    R = R if R is not None else HR.open_nets(board, constraints=cons_path)
    idx = [i for i, r in enumerate(R["connections"]) if r["net"] == net][:MAX_CONNS]
    recs = [R["connections"][i] for i in idx]
    conns = [{"i": i, "a": [r["a"]["x"], r["a"]["y"]], "b": [r["b"]["x"], r["b"]["y"]],
              "layers_a": _layers(r["a"]), "layers_b": _layers(r["b"])} for i, r in zip(idx, recs)]
    rule = recs[0]["rules"] if recs else {}
    rules = {"width": rule.get("width"), "clearance": rule.get("clearance"),
             "via": [rule.get("via_dia"), rule.get("via_drill")]}
    ends = [(c["a"], c["b"]) for c in conns]
    hot = []
    for r in DS.compute(B, cons, spec)["regions"]:
        cx, cy = r["x"] + GRID_MM / 2, r["y"] + GRID_MM / 2
        if any(min(math.hypot(cx - p[0], cy - p[1]), math.hypot(cx - q[0], cy - q[1])) <= HOT_MM for p, q in ends):
            u = r["util"]
            hot.append({"side": r["side"], "x": round(r["x"], 3), "y": round(r["y"], 3),
                        "util": round(u, 3) if math.isfinite(u) else None})
        if len(hot) >= MAX_HOT:
            break
    if idx:
        t = HR.suggest(board, net, conn=idx[0], constraints=cons_path)
        try_ = {"found": bool(t.get("found")), "reason": str(t.get("reason") or "")[:200],
                "len_mm": t.get("length_mm") if t.get("found") else None}
    else:
        try_ = {"found": False, "reason": "no open connection", "len_mm": None}
    info = cls.get(net, {}) if isinstance(cls, dict) else {}
    p = {"v": 1, "net": net, "class": info.get("class", "listed"), "reason": str(info.get("reason") or ""),
         "conns": conns, "rules": rules, "layers": gaps.signal_layers(board), "hot": hot, "try": try_, "ask": ASK}
    return _fit(p)


def _circle(c):
    return (isinstance(c, list) and len(c) == 3 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in c)
            and all(math.isfinite(v) for v in c) and c[2] > 0)


def load_actions(path, nets=None, layers=None):
    """Read the AI action file. Invalid entries (unknown net, non-copper layer, malformed or non-positive circle, more than
    MAX_AVOID circles) are dropped with a log line; never raises. -> {order, nets, tokens}, or None when absent or unreadable."""
    if not os.path.exists(path):
        return None
    try:
        raw = json.load(open(path))
    except (OSError, ValueError) as e:
        _log(f"actions file unreadable ({type(e).__name__}); defaults apply")
        return None
    if not isinstance(raw, dict):
        _log("actions file is not a JSON object; defaults apply")
        return None
    known = set(nets) if nets is not None else None
    ok_layers = set(layers) if layers is not None else None
    order = []
    for n in raw.get("order") or []:
        if not isinstance(n, str) or (known is not None and n not in known):
            _log(f"order: unknown net {n!r} dropped")
        elif n not in order:
            order.append(n)
    out = {}
    for n, a in (raw.get("nets") or {}).items():
        if known is not None and n not in known:
            _log(f"nets: unknown net {n!r} dropped")
            continue
        if not isinstance(a, dict):
            _log(f"nets: {n!r} is not an object; dropped")
            continue
        lay = []
        for l in a.get("layers") or []:
            if ok_layers is not None and l not in ok_layers:
                _log(f"{n}: layer {l!r} is not a copper layer of the board; dropped")
            else:
                lay.append(l)
        av = []
        for c in a.get("avoid") or []:
            if not _circle(c):
                _log(f"{n}: avoid entry {c!r} is not [x, y, r] with r > 0; dropped")
            elif len(av) >= MAX_AVOID:
                _log(f"{n}: more than {MAX_AVOID} avoid circles; extra dropped")
            else:
                av.append([float(c[0]), float(c[1]), float(c[2])])
        out[n] = {"layers": lay, "avoid": av, "why": str(a.get("why") or "")[:200]}
    tok = raw.get("tokens", 0)
    tok = int(tok) if isinstance(tok, (int, float)) and not isinstance(tok, bool) else 0
    return {"order": order, "nets": out, "tokens": tok}


def _avoid(circles):
    return [(float(x), float(y), float(r)) for x, y, r in (circles or [])]


def _key(r):
    return (r["a"]["x"], r["a"]["y"], r["b"]["x"], r["b"]["y"])


def _drop(p):
    """Remove a scratch board made by this stage (name carries the tag) with its project sidecars."""
    from . import gaps
    if "critroute" not in os.path.basename(p):
        return
    gaps._rm(p)
    stem = os.path.splitext(p)[0]
    for ext in (".kicad_pro", ".kicad_dru"):
        gaps._rm(stem + ext)


def search(HR, board, net, i, cons_path, layers, avoid, width, rec, M, via_cost):
    """k11: HR.suggest over SEARCH, one result per step (found or not); a path that the lay refuses gets the next step's path.
    Same path twice is skipped; endpoints that are not two islands stop the search."""
    seen = set()
    for pitch, margin, cap in SEARCH:
        r = HR.suggest(board, net, conn=i, constraints=cons_path, layers=layers or None, avoid=avoid or None, width=width, rec=rec,
                       M=M, via_cost=via_cost, pitch=pitch, margin=margin, max_nodes=cap)
        if r.get("found"):
            if r.get("path") in seen:
                continue
            seen.add(r.get("path"))
        else:
            r = dict(r, reason=f"p{pitch}/m{margin}: {r.get('reason') or ''}")
        yield r
        if "not two separate islands" in str(r.get("reason") or ""):
            return


def apply(board, out, actions, cons_path, cons, attempts, log=None, crit=None):
    """Lay the suggested path of every open connection of each critical net, in action order (default: `crit` order).
    A path is kept (locked) only when the measured opens fall and the shorts do not rise. The last kept board is copied to
    `out`. One attempt-log record per try. -> {board, kept, tried, open_nets}."""
    from . import gaps, handroute as HR
    crit = crit if crit is not None else critical(board, cons, None, cons_path)
    names = [c["net"] for c in crit]
    acts = actions or {"order": [], "nets": {}}
    order = [n for n in acts.get("order", []) if n in names]
    order += [n for n in names if n not in order]
    default_layers = gaps.signal_layers(board)
    from . import rfroute as RFR
    rf_outer = RFR.outer_layers(default_layers)
    rf_w = RFR.widths(board, cons, [c["net"] for c in crit if c["class"] == "rf"], rf_outer) if rf_outer else {}
    cur, base = board, gaps.measure(board)
    tried, kept, lines, n_try, open_now = set(), [], [], 0, None
    for net in order:
        a = acts.get("nets", {}).get(net, {})
        layers = a.get("layers") or default_layers
        width = None
        if net in rf_w:                                 # RF: outer layers at the impedance width, never the default width on an inner layer
            layers = [x for x in layers if x in rf_outer] or rf_outer
            width = rf_w[net]["w"]
        avoid = _avoid(a.get("avoid"))
        while True:
            M = HR._model(cur, cons_path)
            R = HR.open_nets(cur, constraints=cons_path, M=M)
            open_now = {r["net"] for r in R["connections"]}
            # canonical try order (ledger 2026-11-25): by endpoint coordinates, never by the order kicad-cli lists the connections in
            todo = sorted(((i, r) for i, r in enumerate(R["connections"]) if r["net"] == net and _key(r) not in tried),
                          key=lambda t: _key(t[1]))
            if not todo:
                break
            i, rec = todo[0]
            tried.add(_key(rec))
            n_try += 1
            t0 = time.time()
            result, why, whys = "no-path", "", []
            for r in search(HR, cur, net, i, cons_path, layers, avoid, width, rec, M, RF_VIA_COST if net in rf_w else 2.0):
                if not r.get("found"):
                    whys.append(str(r.get("reason") or "")[:120])
                    continue
                tmp = HR._tmp_beside(board, f"critroute{n_try}")   # named from the stage input: a name built on `cur` grew by one tag per kept path (ENAMETOOLONG at ~14)
                try:
                    res = HR.lay(cur, net, r["path"], out=tmp, constraints=cons_path, lock=True, width=width)
                    if not res.get("accepted"):             # a refused lay never writes `tmp`: it is still the empty mkstemp slot (W30: "Unexpected end of input")
                        raise _Refused(str(res.get("reason") or "lay refused")[:200])
                    m = gaps.measure(tmp)
                    ok = m["opens"] < base["opens"] and m["shorts"] <= base["shorts"]
                    result = "kept" if ok else "dropped"
                    why = f"opens {base['opens']}->{m['opens']}, shorts {base['shorts']}->{m['shorts']}"
                    if ok:
                        if cur != board:
                            _drop(cur)
                        kept.append({"net": net, "conn": i, "len_mm": r.get("length_mm")})
                        cur, base = tmp, m
                        tmp = None
                except _Refused as e:
                    result, why = "refused", str(e)
                except Exception as e:                      # noqa: BLE001 - a failed lay is one failed try, not a stage error
                    result, why = "error", f"{type(e).__name__}: {str(e)[:160]}"
                if tmp:
                    _drop(tmp)
                if result == "kept":
                    break
                whys.append(f"{result}: {why}")             # k11: a refused lay (e.g. an escape lock) tries the next search step's path
            if result != "kept":
                why = " | ".join(whys)[:400]
            rec_log = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "stage": "critroute", "net": net, "conn": i,
                       "class": next((c["class"] for c in crit if c["net"] == net), None), "method": "suggest+lay",
                       "layers": layers, "width": width, "avoid": len(avoid), "result": result, "why": why,
                       "s": round(time.time() - t0, 1), "ai_tokens": acts.get("tokens", 0)}
            if attempts:
                calllog.append(rec_log, attempts)
            lines.append(f"{net} conn {i}: {result} ({why})")
    if open_now is None:
        open_now = {r["net"] for r in HR.open_nets(cur, constraints=cons_path)["connections"]}
    if cur != board:
        gaps._copy(cur, out)
        gaps._sidecars(cur, out)
        _drop(cur)
        cur = out
    return {"board": cur, "kept": kept, "tried": n_try, "open_nets": sorted(open_now), "lines": lines}


def stage(c, n, board):
    """Pipeline stage `critroute`. No actions file and `[ai] mode` = "packet" (default): writes the packets and returns
    verdict "packets" (the pipeline stops with exit 5) only when `[ai] answer` = "required". Otherwise (default "optional") an
    unanswered packet set does not stop the flow: each net gets its own try with the default order and layers, and the
    result carries `ai_unanswered` (count, packet files); the packets stay on disk for an optional agent pass."""
    from . import gaps
    d = c.dir(n, "critroute")
    ai = os.path.join(d, "ai")
    os.makedirs(ai, exist_ok=True)
    crit = critical(board, c.cons, c.spec, c.cons_path)
    if not crit:
        return {"verdict": "pass", "line": "critroute: no critical net open", "board": board}
    from . import netclass as NC, routediag as RD, handroute as HR
    from . import forbid as FB
    B = FB.augment(RD.load(board), FB.windows(c.cons))      # B7
    cls = NC.classify(B, c.cons, c.spec)
    R = HR.open_nets(board, constraints=c.cons_path)
    names = [r["net"] for r in crit]
    for r in crit:
        p = packet(board, r["net"], c.cons, c.spec, c.cons_path, cls=cls, R=R, B=B)
        with open(os.path.join(ai, _safe(r["net"]) + ".packet.json"), "w") as fh:
            json.dump(p, fh, indent=1)
    apath = os.path.join(ai, "actions.json")
    mode = str(((c.cons or {}).get("ai") or {}).get("mode", "packet"))
    acts = load_actions(apath, names, gaps.copper_layers(board)) if os.path.exists(apath) else None
    unanswered = None
    if acts is None and mode == "packet":
        if gaps.ai_answer_required(c.cons):
            return {"verdict": "packets", "board": board, "next": [f"write {apath} (order, nets: layers, avoid circles) and re-run"],
                    "line": f"critroute: {len(names)} packet(s) in {ai}; write {apath} and re-run"}
        unanswered = True
    res = apply(board, os.path.join(d, "board.kicad_pcb"), acts, c.cons_path, c.cons, calllog.attempts_path(board), crit=crit)
    still = [x for x in names if x in res["open_nets"]]
    with open(os.path.join(d, "stage.log"), "a") as fh:
        fh.write("\n".join(res["lines"]) + "\n")
    line = (f"critroute: {len(res['kept'])} path(s) kept, {len(names) - len(still)}/{len(names)} critical net(s) closed"
            + (f"; open: {', '.join(still)}" if still else ""))
    out = {"verdict": "warn" if still else "pass", "line": line, "board": res["board"],
           "next": [f"critroute open: {x}" for x in still]}
    if unanswered:
        from . import packets as PK, questions as QS
        ans = {**{str(k): str(v) for k, v in ((c.cons or {}).get("answers") or {}).items()}, **QS.parse_answers(getattr(getattr(c, "a", None), "answer", None))}
        ua = PK.resolve([(os.path.join(ai, _safe(x) + ".packet.json"), x) for x in names], set(still), ans, ai_dir=ai)
        out["ai_unanswered"] = ua
        if ua["questions"]:
            out.update(verdict="questions", next=[f"owner decision {q['id']}: {q['found']}; options: " + "/".join(o["choice"] for o in q["options"]) for q in ua["questions"]])
    return out
