"""Artefact first: which source decides each fact.

When the owner's records exist (constraints TOML, spec JSON, `<board>.fluxmap.json`) the facts come from them; the bare board is
only inferred from when they are silent.  Order per fact (first present wins; docs/FLOW.md):

  groups        constraints [placement.group] > constraints/spec blocks > map (recorded, not sheet/cluster) > schematic sheets / clusters
  net classes   constraints [pairs]/[rf]/[power]/[placement.net] > .kicad_pro net classes > map nets > name patterns
  fixed parts   constraints [placement] fixed / [lock.at] > locked in the board > map > mounting hardware / edge connectors by shape
  pours         constraints [[plan.pour]] / [power.X] pour > map `pours` > zones on the board
  pin roles     spec pinmaps > none (pin names are not guessed)

`resolve` reports the winner per fact; `apply_map` lets recorded pours/fixed parts of the map stand in for missing constraints;
`record` writes the facts a command derived (planned pours, fan-out groups, sources) back to the map."""
import json
import os
import re

from . import fluxmap as FM


def load_map(board):
    try:
        return FM.load(FM.default_path(board))
    except (OSError, ValueError):
        return {}


def _pro_classes(board):
    try:
        d = json.load(open(os.path.splitext(board)[0] + ".kicad_pro"))["net_settings"]
        return bool(d.get("netclass_patterns")) or bool(d.get("netclass_assignments"))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def resolve(board, B, cons, spec, m=None):
    """-> {fact: {source, note}} for groups, net_classes, fixed_parts, pours, pin_roles."""
    from . import placechecks as PCK
    cons = cons or {}
    m = m if m is not None else load_map(board)
    pl = cons.get("placement") or {}
    out = {}
    if pl.get("group"):
        out["groups"] = ("constraints", f"{len(pl['group'])} [placement.group]")
    elif PCK.blocks_of(B, spec, cons):
        out["groups"] = ("spec" if spec else "constraints", "blocks")
    elif any((g or {}).get("src") not in (None, "sheet", "cluster") for g in (m.get("groups") or {}).values()
             if isinstance(g, dict)):
        out["groups"] = ("map", "recorded groups")
    else:
        out["groups"] = ("board-inferred", "schematic sheets / support clusters")
    if any(cons.get(k) for k in ("pairs", "rf", "power")) or pl.get("net"):
        out["net_classes"] = ("constraints", ", ".join(k for k in ("pairs", "rf", "power") if cons.get(k)))
    elif _pro_classes(board):
        out["net_classes"] = ("board (.kicad_pro)", "net class patterns")
    elif m.get("nets"):
        out["net_classes"] = ("map", "recorded nets")
    else:
        out["net_classes"] = ("board-inferred", "name patterns")
    if pl.get("fixed") or cons.get("lock"):
        out["fixed_parts"] = ("constraints", "[placement] fixed / [lock.at]")
    elif any(p.get("locked") for p in B["parts"].values()):
        out["fixed_parts"] = ("board", "locked footprints")
    elif any(p.get("mode") == "fixed" for p in (m.get("parts") or {}).values() if isinstance(p, dict)):
        out["fixed_parts"] = ("map", "recorded fixed parts")
    else:
        out["fixed_parts"] = ("board-inferred", "mounting hardware / connectors by shape")
    if (cons.get("plan") or {}).get("pour") or any(isinstance(v, dict) and v.get("pour") for v in (cons.get("power") or {}).values()):
        out["pours"] = ("constraints", "[[plan.pour]] / [power.X] pour")
    elif m.get("pours"):
        out["pours"] = ("map", f"{len(m['pours'])} recorded")
    elif any(z["net"] and not z["keepout"] for z in B["zones"]):
        out["pours"] = ("board", "zones")
    else:
        out["pours"] = ("none", "")
    out["pin_roles"] = ("spec", "pinmaps") if any(c.get("pinmap") for c in (spec or {}).get("components", [])) else ("none", "")
    return {k: {"source": s, "note": n} for k, (s, n) in out.items()}


def apply_map(cons, m):
    """constraints with the map's recorded pours / fixed parts added where the constraints say nothing. -> (cons, [what])"""
    used = []
    cons = dict(cons or {})
    if m.get("pours") and not (cons.get("plan") or {}).get("pour"):
        plan = dict(cons.get("plan") or {})
        plan["pour"] = [dict(z) for z in m["pours"]]
        cons["plan"] = plan
        used.append(f"{len(m['pours'])} pour(s)")
    return cons, used


def record(board, **sections):
    """merge `sections` into <board>.fluxmap.json (created when missing)."""
    path = FM.default_path(board)
    try:
        m = FM.load(path)
    except (OSError, ValueError):
        m = {"fluxmap": 1}
    m.update(sections)
    FM.save(m, path)
    return path


def fanout_groups(board_path):
    try:
        txt = open(board_path, errors="replace").read()
    except OSError:
        return []
    return sorted(set(re.findall(r'\(group "(fanout:[^"]*)"', txt)))
