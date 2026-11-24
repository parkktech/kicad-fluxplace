"""Fixed-part reasons (placer step 2, F1): every part the placer may not move carries a reason class, and no reason exempts a
support part from its distance budget.

Constraint form (board-agnostic, under [placement]):

    [placement.fixed_reason."J12"]
    reason = "enclosure"       # mechanical | enclosure | module-mount | owner
    decision = "D71dh"         # the decision row id; required for reason "owner"
    note = "RJ45 on the edge"  # free text

Classes: mechanical (holes, standoffs, fiducials: parts without an electrical role), enclosure (edge or face connector,
panel-visible), module-mount (positioned by a fixed mounting pattern), owner (an owner decision, cited by id).

Findings, all OPEN (ready "fixed reasons" line):
    FIXED_NO_REASON          a fixed part with no reason class, or one outside the four
    FIXED_OWNER_NO_DECISION  reason "owner" without a decision id
    FIXED_UNDECLARED         a part held only because it was detected as an anchor (preroute.fixed_parts): relocate, closure and
                             the refiners hold it, the floorplan (modules.fixed_set) does not. Declare it fixed with a reason, or
                             free in `[floorplan] movable`
    FIXED_REASON_MISMATCH    reason "mechanical" on a part that has a support row (a capacitor is never mechanical)
    FIXED_OVER_BUDGET        a fixed support part over its max distance (powerbudget.supports), whatever its reason
    FIXED_OWNER_GUESSED      a fixed support part whose served pin is a guess: its distance cannot be graded until the pin is named
"""

REASONS = ("mechanical", "enclosure", "module-mount", "owner")
ORDER = ("FIXED_OVER_BUDGET", "FIXED_REASON_MISMATCH", "FIXED_OWNER_GUESSED", "FIXED_OWNER_NO_DECISION", "FIXED_UNDECLARED",
         "FIXED_NO_REASON")


def declared(cons):
    """{ref: {reason, decision, note}} from [placement.fixed_reason]."""
    rows = ((cons or {}).get("placement") or {}).get("fixed_reason") or {}
    return {str(r): v for r, v in rows.items() if isinstance(v, dict)}


def _f(code, ref, msg):
    return {"code": code, "level": "OPEN", "ref": ref, "msg": msg}


def findings(fixed, cons, sup_rows, detected=(), unconnected=()):
    """fixed: refs the placer will not move. detected: refs held only by anchor detection. unconnected: fixed refs with no pad on
    a signal or power net (holes, standoffs, fiducials): mechanical by construction, no declaration needed. sup_rows: {ref:
    [support rows]} (powerbudget.supports; each row has ref, role, serves, dist_mm, sure, limit {max}).
    -> [{code, level, ref, msg}], all OPEN, faults first (ORDER), then by ref."""
    dec = {**{r: {"reason": "mechanical"} for r in unconnected}, **declared(cons)}
    free = set(((cons or {}).get("floorplan") or {}).get("movable", []) or [])
    out = []
    for ref in sorted(set(fixed) | (set(detected) - free)):
        d = dec.get(ref) or {}
        reason = d.get("reason")
        rows = sup_rows.get(ref, [])
        if reason not in REASONS:
            if ref not in fixed:
                out.append(_f("FIXED_UNDECLARED", ref, f"{ref} is held as a detected anchor only: declare it in [placement] fixed "
                                                       f"with a reason, or free in [floorplan] movable"))
            else:
                got = f"reason = {reason!r}" if reason else "no reason"
                out.append(_f("FIXED_NO_REASON", ref, f"{ref} is fixed with {got}; one of {', '.join(REASONS)} is needed"))
        elif reason == "owner" and not str(d.get("decision") or "").strip():
            out.append(_f("FIXED_OWNER_NO_DECISION", ref, f"{ref} is fixed for reason owner without a decision id"))
        elif reason == "mechanical" and rows:
            out.append(_f("FIXED_REASON_MISMATCH", ref, f"{ref} is fixed as mechanical but is a support part ({rows[0]['role']} "
                                                        f"serves {rows[0]['serves']}): mechanical is for parts without an electrical role"))
        why = reason if reason in REASONS else "no reason"
        over = [s for s in rows if s.get("sure") and s["dist_mm"] > s["limit"]["max"]]
        if over:                           # no class is exempt: a support part is graded whatever the reason says
            s = max(over, key=lambda r: r["dist_mm"] - r["limit"]["max"])
            mx = s["limit"]["max"]
            out.append(_f("FIXED_OVER_BUDGET", ref, f"{ref} ({why}) {s['role']} serves {s['serves']}: {s['dist_mm']:g} mm from its "
                                                    f"pin, max {mx:g} mm (over by {round(s['dist_mm'] - mx, 2):g} mm)"
                                                    + (" (derived limit)" if s["limit"].get("derived") else "")))
        elif rows and not any(s.get("sure") for s in rows):
            s = rows[0]
            out.append(_f("FIXED_OWNER_GUESSED", ref, f"{ref} ({why}) {s['role']}: its pin is a guess ({s['serves']}, "
                                                      f"{s['dist_mm']:g} mm), not graded until [power.NET] support names the pin"))
    return sorted(out, key=lambda f: (ORDER.index(f["code"]), f["ref"]))


def blockers(fs):
    """Checklist rows: one row per fault with numbers, one row per code for the reason-only findings (refs listed)."""
    solo = ("FIXED_OVER_BUDGET", "FIXED_REASON_MISMATCH", "FIXED_OWNER_GUESSED")
    out = [f"{f['code']} {f['msg']}" for f in fs if f["code"] in solo]
    for code in ORDER:
        refs = [f["ref"] for f in fs if f["code"] == code and code not in solo]
        if refs:
            out.append(f"{code} {len(refs)} part(s): {', '.join(refs)}")
    return out


def unclassified_pairs(cls, cons):
    """Name pairs with no class: both legs are class `pair` (netclass.classify) and neither is in a `[pairs.*]` or `[channel.*]`
    family nor carries a per-net budget. -> ["A_N/A_P", ...]. Settled by a family row, or top-level `not_pairs` (plain signals)."""
    fams = list((cons or {}).get("pairs") or {}) + list((cons or {}).get("channel") or {})
    seen, out = set(), []
    for n in sorted(cls):
        i = cls[n]
        if i["class"] != "pair" or n in seen:
            continue
        legs = [n] + ([i["partner"]] if i.get("partner") in cls else [])
        seen.update(legs)
        if not any(x.startswith(f) for x in legs for f in fams) and not any(cls[x].get("override") for x in legs):
            out.append("/".join(legs))
    return out
