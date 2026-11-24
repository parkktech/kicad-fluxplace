"""Fixed-part reasons (placer step 2, F1): every part the placer may not move carries a reason class, and a
non-mechanical fixed support part over its max distance is OPEN.

Constraint form (board-agnostic, under [placement]):

    [placement.fixed_reason."J12"]
    reason = "enclosure"       # mechanical | enclosure | module-mount | owner
    decision = "D71dh"         # the decision row id; required for reason "owner"
    note = "RJ45 on the edge"  # free text

Classes: mechanical (outline, holes, standoffs: no distance check), enclosure (edge or face connector, panel-visible),
module-mount (positioned by a fixed mounting pattern), owner (an owner decision, cited by id).
"""

REASONS = ("mechanical", "enclosure", "module-mount", "owner")
NO_DISTANCE = ("mechanical",)


def declared(cons):
    """{ref: {reason, decision, note}} from [placement.fixed_reason]."""
    rows = ((cons or {}).get("placement") or {}).get("fixed_reason") or {}
    return {str(r): v for r, v in rows.items() if isinstance(v, dict)}


def findings(fixed, cons, sup_rows):
    """fixed: refs the placer will not move. sup_rows: {ref: [support rows]} (powerbudget.supports; each row has
    ref, role, serves, dist_mm, sure, limit {max}). -> [{code, level, ref, msg}], all OPEN."""
    dec = declared(cons)
    out = []
    for ref in sorted(fixed):
        d = dec.get(ref) or {}
        reason = d.get("reason")
        if reason not in REASONS:
            got = f"reason = {reason!r}" if reason else "no reason"
            out.append({"code": "FIXED_NO_REASON", "level": "OPEN", "ref": ref,
                        "msg": f"{ref} is fixed with {got}; one of {', '.join(REASONS)} is needed"})
        elif reason == "owner" and not str(d.get("decision") or "").strip():
            out.append({"code": "FIXED_OWNER_NO_DECISION", "level": "OPEN", "ref": ref,
                        "msg": f"{ref} is fixed for reason owner without a decision id"})
        if reason in NO_DISTANCE:
            continue                    # an unclassified part is still graded on distance: the report shows what is over
        for s in sup_rows.get(ref, []):
            mx = s["limit"]["max"]
            if s.get("sure") and s["dist_mm"] > mx:
                out.append({"code": "FIXED_OVER_BUDGET", "level": "OPEN", "ref": ref,
                            "msg": f"{ref} ({reason}) {s['role']} serves {s['serves']}: {s['dist_mm']:g} mm from its pin, "
                                   f"max {mx:g} mm (over by {s['dist_mm'] - mx:g} mm)"})
                break
    return out
