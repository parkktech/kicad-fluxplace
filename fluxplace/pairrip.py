"""k15: HARD pair re-lay. A pair the pair layer cannot lay because LOCKED copper of other nets boxes its corridor (krt_locked
and pairgroup only rip FREE/SOFT or unlocked copper) gets its blockers ripped: both legs and every blocking net the pair layer
names (locked copper too) are stripped, the pair is laid, and the ripped nets are re-laid by the caller's router.

Never ripped: an RF net, a leg of another pair, a net with a zone (the pour carries it) and a [power] rail. The close guard
keeps the result only when the opens fall and no new DRC kind appears; the caller records the ripped nets."""
import os
import re

RIP_ROUNDS = 4            # lay -> blockers -> rip, at most this many times
RIP_NETS_MAX = 12         # nets one call may rip in all


def blocker_nets(labels):
    """Net names of the track / via labels the pair layer reports ("track NET (lab)", "via NET [via site]"); pads and edges excluded."""
    out = []
    for lab in labels or ():
        m = re.match(r"(?:track|via) (\S+)", str(lab))
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def rippable(nets, pair, never):
    """The nets of `nets` this rung may rip: not a leg of `pair`, not in `never` (rf, other pair legs, zone nets, rails)."""
    return [n for n in nets if n not in pair and n not in never]


def strip_all(text, nets):
    """Board text without ANY copper (locked too) of `nets` -> (text, removed count)."""
    from . import boardtext as BT
    S = set(nets)
    kill = [k for _i, _j, k, b in BT.scan(text) if BT.net_name(b) in S]
    return BT.remove_blocks(text, kill), len(kill)


def _row(res, pair):
    return next((x for x in res["pairs"] if tuple(x["pair"]) == tuple(pair)), None) or {"status": "not-laid", "why": "pair not found"}


def relay(board, out, pair, cons, never, lay=None, log=lambda m: None):
    """Strip `pair` (both legs) and lay it; while it is blocked, rip the blocking nets (locked too, minus `never`) and lay again.
    Writes `out` (pair laid and locked, ripped nets stripped) -> {status, ripped, rounds, why}. `lay(path, cons, only)` defaults
    to pairs.plan (tests replace it)."""
    from . import pairs as PR, pairsonly as PO, routerules as RR, pairbase as PB, pairfb as FB
    lay = lay or (lambda b, c, only: PR.plan(b, c, None, only=only, log=lambda m: None))
    pair = tuple(pair)
    cons, _w = PB.with_widths(cons, board, None, [pair])       # the pair's own width (as pairsonly.reroute), read before the strip
    bo_lim = FB.uncoupled_limit(cons, pair)
    text = open(board, errors="surrogateescape").read()
    text, _n = strip_all(text, pair)
    pre = out + ".pre.kicad_pcb"
    ripped, why = [], ""
    try:
        for rnd in range(RIP_ROUNDS + 1):
            with open(pre, "w", errors="surrogateescape") as fh:
                fh.write(text)
            PO._sidecars(board, pre)
            res = lay(pre, cons, set(pair))
            e = _row(res, pair)
            if e["status"] != "laid" and bo_lim > 0 and any(t in (e.get("why") or "") for t in PO.BREAKOUT_WHY):
                res_b = lay(pre, FB.variant(cons, ring_mm=PO.breakout_ring(bo_lim), max_uncoupled_mm=bo_lim, skew_frac=1.0), set(pair))
                eb = _row(res_b, pair)               # break-outs (uncoupled legs within the limit), as pairsonly.reroute retries
                if eb["status"] == "laid":
                    res, e = res_b, eb
                else:
                    e = dict(e, blockers=list(e.get("blockers") or []) + list(eb.get("blockers") or []))
            if e["status"] == "laid":
                R, Pm, proj = RR.rules(cons, pre, B=res["B"]), PR.params(cons), PR.project_classes(pre)
                PR.write(pre, out, res, lambda net: PR.rules_for(pre, res["B"], cons, R, net, Pm, proj)["drill"])
                PO._sidecars(board, out)
                return {"status": "laid", "ripped": ripped, "rounds": rnd}
            why = e.get("why") or e.get("blocked_by") or ""
            labels = list(e.get("blockers") or []) + [b for b in (e.get("blocked_by") or "").split(" | ") if b.startswith(("track ", "via "))]
            new = [n for n in rippable(blocker_nets(labels), pair, never) if n not in ripped][:RIP_NETS_MAX - len(ripped)]
            log(f"  pairrip {pair[0]}/{pair[1]} round {rnd}: {why[:120]}; rip {new}")
            if not new:
                return {"status": "blocked", "ripped": ripped, "rounds": rnd, "why": why[:300]}
            text, _n = strip_all(text, new)
            ripped += new
        return {"status": "blocked", "ripped": ripped, "rounds": RIP_ROUNDS, "why": why[:300]}
    finally:
        for f in [pre] + [os.path.splitext(pre)[0] + x for x in (".kicad_pro", ".kicad_dru")]:
            if os.path.exists(f):
                os.remove(f)
