"""Kickoff checks `ready` runs before any placement or route stage (docs/V2.0/w16/KICKOFF-2026-11-27.md K2, K7):
  placement_findings / placement_line   the placement classes of the review (preroute.run) + the collision gate, on the poses as they are
  stage_table / stage_lines             every flow stage on or off with the reason; a stage off only because a key is unset is FAIL
  budget_need / budget_lines            stage wall budgets against the work (pairs x per-pair search cap vs [pairlay] budget_min)
Board-agnostic: the board's intent is read from the constraints and the board only."""
PLACE_CODES = ("NET_OVER_BUDGET", "SUPPORT_PART_FAR", "PLACE_ESCAPE_UNRESERVED", "CONGESTION", "PLACEMENT_ILLEGAL", "KEEPOUT_TRAPPED")
COLLISION_CODE = "PLACEMENT_COLLISION"

# stage -> (constraints table, what turns it on). A stage here is optional: it runs only when its table says so.
SWITCHES = {"trace-finish": ("finish", "enabled", False), "placetour": ("placetour", "enabled", False),
            "lookahead": ("lookahead", "enabled", False), "silk": ("silk", "enabled", True)}


def placement_findings(B, cons, spec=None):
    """-> [{code, level, msg, refs}]: FAIL findings of PLACE_CODES (waivers and budget rows already applied by preroute.run) plus
    the collision-gate FAILs (placementcheck.gate_failures, [review] waive applied)."""
    from . import preroute as PR, placementcheck as PCK
    f, _card = PR.run(B, cons, spec, collide=False)
    out = [{"code": x["code"], "level": x["level"], "msg": x["msg"], "refs": list(x.get("refs") or [])}
           for x in f if x["level"] == "FAIL" and x["code"] in PLACE_CODES]
    out += [{"code": COLLISION_CODE, "level": "FAIL", "msg": x["msg"], "refs": list(x["refs"])} for x in PCK.gate_failures(B, cons)]
    return out


def summarise(findings):
    by = {}
    for x in findings:
        by.setdefault(x["code"], []).append(x)
    return by


def placement_line(B, cons, spec=None):
    """-> (ready line dict, findings). FAIL when any class has an unwaived FAIL; the blockers name the first of each class."""
    from .ready import _line, PASS, FAIL, UNCHECKED
    try:
        fs = placement_findings(B, cons, spec)
    except Exception as e:                                       # noqa: BLE001 - a check that could not run is never PASS
        return _line("placement classes", UNCHECKED, f"NOT CHECKED ({type(e).__name__}: {str(e)[:100]})"), []
    if not fs:
        return _line("placement classes", PASS, f"0 FAIL in {', '.join(PLACE_CODES[:4])} and the collision gate"), fs
    by = summarise(fs)
    txt = ", ".join(f"{c} {len(v)}" for c, v in sorted(by.items()))
    return _line("placement classes", FAIL, f"{len(fs)} FAIL: {txt}",
                 [f"{c}: {v[0]['msg'][:140]}" + (f" (+{len(v) - 1})" if len(v) > 1 else "") for c, v in sorted(by.items())]), fs


def gate_refusal(board, cons, spec=None):
    """'' when the placement classes are clear, else the refusal text (versions accept --allow-worse)."""
    from . import routediag as RD
    fs = placement_findings(RD.load(board), cons, spec)
    if not fs:
        return ""
    by = summarise(fs)
    return "placement classes FAIL (" + ", ".join(f"{c} {len(v)}" for c, v in sorted(by.items())) + \
        "): fix, or cover each with a [review] waive row or a budget row with a reason"


def load_current_line(board, cons):
    """-> ready line. WARN naming each rail and the parts whose draw is unknown (power.unknown_loads): the route stage sizes their
    branches by the rail total. PASS when every load has a figure; UNCHECKED when it could not run."""
    from .ready import _line, PASS, WARN, UNCHECKED
    from . import power as PW
    try:
        unk = PW.unknown_loads(board, cons)
    except Exception as e:                                       # noqa: BLE001 - a check that could not run is never PASS
        return _line("rail load currents", UNCHECKED, f"NOT CHECKED ({type(e).__name__}: {str(e)[:100]})")
    if not unk:
        return _line("rail load currents", PASS, "every [power] rail load has a known current ([[dcdrop]] row, part figure or 0 A DC)")
    return _line("rail load currents", WARN, f"{sum(len(v) for v in unk.values())} load(s) of {len(unk)} rail(s) with no known current: "
                 + "; ".join(f"{n}: {', '.join(v)}" for n, v in sorted(unk.items())),
                 [f"{n}: add a sourced [[dcdrop]] row (from the source part, to {u}) for each" for n, v in sorted(unk.items()) for u in v[:1]])


# ------------------------------------------------------------------ stage table (K2)
def stage_table(cons, flow=None, noted=None):
    """-> [{stage, on, why, unset}]. `noted`: {stage: reason} the constraints record for a deliberate off ([ready.stage_off])."""
    from . import pipeline as PL
    cons = cons or {}
    noted = noted if noted is not None else ((cons.get("ready") or {}).get("stage_off") or {})
    rows = []
    for st in PL.FLOWS.get(flow or "constructive", PL.CONSTRUCTIVE):
        sw = SWITCHES.get(st)
        if sw is None:
            rows.append({"stage": st, "on": True, "why": "always on", "unset": False})
            continue
        table, key, default = sw
        tab = cons.get(table) or {}
        if key in tab:
            on = tab[key] is True
            why = f"[{table}] {key} = {str(on).lower()}"
            unset = False
        else:
            on = default
            why = f"[{table}] {key} not set (built-in default {str(default).lower()})"
            unset = not default
        if not on and st in noted:
            why, unset = f"off: {noted[st]}", False
        rows.append({"stage": st, "on": on, "why": why, "unset": unset})
    return rows


def pair_budget(cons, npairs):
    """-> (need_min, budget_min): the worst case of the pair stage (every pair searches to the per-pair cap) vs [pairlay] budget_min."""
    from . import pairs as PA
    P = PA.params(cons or {})
    return npairs * PA.NODE_CAP_S / 60.0, P["budget_s"] / 60.0


def stage_lines(cons, npairs=0, flow=None):
    """-> (lines, detail): the K2 "stages" and "stage budgets" lines."""
    from .ready import _line, PASS, FAIL
    from . import pairs as PA
    rows = stage_table(cons, flow)
    unset = [r for r in rows if r["unset"]]
    on = ", ".join(r["stage"] for r in rows if r["on"])
    off = "; ".join(f"{r['stage']} ({r['why']})" for r in rows if not r["on"]) or "none"
    lines = []
    if unset:
        lines.append(_line("stages", FAIL, f"{len(unset)} stage(s) off only because a key is unset: {', '.join(r['stage'] for r in unset)}; off: {off}",
                           [f"{r['stage']}: {r['why']}: set it, or record the reason in [ready.stage_off]" for r in unset]))
    else:
        lines.append(_line("stages", PASS, f"on: {on}; off: {off}"))
    detail = {"stages": rows}
    if npairs:
        need, have = pair_budget(cons, npairs)
        detail["pair_budget"] = {"pairs": npairs, "need_min": need, "budget_min": have}
        if have > 0 and need > have:
            lines.append(_line("stage budgets", FAIL, f"pairs: {npairs} pairs x {PA.NODE_CAP_S} s cap = {need:.0f} min > [pairlay] budget_min {have:g}",
                               [f"raise [pairlay] budget_min to {need:.0f}, or lower the per-pair cap"]))
        else:
            lines.append(_line("stage budgets", PASS, f"pairs: {npairs} pairs x {PA.NODE_CAP_S} s = {need:.0f} min <= budget_min {have:g}"
                               if have > 0 else "pairs: no wall budget ([pairlay] budget_min = 0)"))
    return lines, detail
