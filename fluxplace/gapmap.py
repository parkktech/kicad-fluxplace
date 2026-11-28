"""GAPMAP (B10): the total map review FAIL code -> gap kind -> owner stage.

Every FAIL a review (or a stage gate that feeds it) can raise has a row here: the gap kind a dispatcher repairs it under (placegaps /
gaps kinds; None while no kind exists yet) and the stage that owns the repair. A FAIL nobody can repair by a move (a datasheet, a
spec, a constraint, a part choice) is tagged OWNER_DATA: it is the owner's to answer, never silently nobody's.
`fail_codes()` finds every code the source can emit as a FAIL (AST scan, level FAIL or not a constant); the test
`test_gapmap` asserts `missing()` is empty, so a new FAIL without a row breaks the build. Rows: exact code first, then the longest
prefix (a family such as SILK_). Board-agnostic: codes and stages only.
"""
import ast
import glob
import os
import re

OWNER_DATA = "data/owner"
# stages that can own a repair: pipeline stages (flow + constructive) plus the output stages
STAGES = frozenset({"ready", "modules", "floorplan", "fanout", "placetour", "lookahead", "pairs", "tune", "critroute", "route", "close",
                    "trace-finish", "silk", "review", "fab", "deliver"})

# (code or prefix ending in "_" -> (gap kind | None, owner stage | OWNER_DATA)
_X = {
    # placement (floorplan / placetour dispatcher: placegaps kinds)
    "SUPPORT_PART_FAR": ("support-far", "floorplan"),
    "NET_OVER_BUDGET": ("net-budget", "floorplan"),
    "ANCHOR_MISORIENTED": ("row-template", "floorplan"),
    "CONNECTOR_BLOCKED": ("row-edge", "floorplan"),
    "HEIGHT_REGION": ("side-illegal", "floorplan"),
    "KEEPOUT_TRAPPED": ("row-other", "floorplan"),
    "KEEPOUT_COMPONENT": ("row-other", "floorplan"),
    "REGION_OVERFULL": ("row-other", "floorplan"),
    "PLACEMENT_ILLEGAL": ("clearance", "floorplan"),
    "COLLISION": ("clearance", "floorplan"),
    "PIN_ESCAPE_BLOCKED": ("pin-escape", "floorplan"),
    "PAIR_ESCAPE_BLOCKED": ("pin-escape", "floorplan"),
    "PLACE_ESCAPE_UNRESERVED": ("pin-escape", "floorplan"),
    "FANOUT_PIN_UNROUTED": ("pin-escape", "floorplan"),
    "CONGESTION": ("ghost-hot", "placetour"),
    "SWLOOP_LARGE": ("row-pin-bypass", "floorplan"),
    "MAG_": ("row-other", "floorplan"),
    "BLOCK_SCATTERED": ("row-other", "floorplan"),
    "HOLE_TO_HOLE": ("clearance", "floorplan"),
    "REF_PLANE_GAP": (None, "route"),
    "STALE_FILL": (None, "close"),
    "SOURCE_OVERLOAD": (None, OWNER_DATA),
    "FUSE_SIZING": (None, OWNER_DATA),
    "CHANNEL_MAX": ("row-away", "floorplan"),
    # routing
    "UNROUTED": ("open-free", "route"),
    "UNROUTED_SUMMARY": ("open-free", "route"),    # B3a: the count row of the UNROUTED detail rows (same owner)
    "POWER_WIDTH": ("power-width", "route"),
    "POWER_VIAS": ("power-vias", "route"),
    "POWER_COPPER_IMPOSSIBLE": (None, OWNER_DATA),
    "POWER_PLAN_NO_BUDGET": (None, OWNER_DATA),
    "PAIR_UNMEASURED": ("pair-unmeasured", "tune"),
    "PAIR_SKEW": ("pair-skew", "tune"),
    "PAIR_STUB": ("pair-stub", "tune"),
    "PAIR_IMPEDANCE_OFF": ("pair-impedance", "pairs"),
    "PAIR_NARROWED": ("pair-impedance", "pairs"),
    "PAIR_IMPEDANCE_UNREACHABLE": (None, OWNER_DATA),
    "MATCH_GROUP_SKEW": ("pair-skew", "tune"),
    "MATCH_CONFIG": (None, OWNER_DATA),
    "TIMING_SKEW": ("pair-skew", "tune"),
    "TIMING_FLYBY_ORDER": (None, "route"),
    "TIMING_CONFIG": (None, OWNER_DATA),
    "MEASURE_DISAGREE": ("pair-unmeasured", "tune"),
    "RF_IMPEDANCE_OFF": (None, "route"),
    "RF_WIDTH_OFF": (None, "route"),
    "RF_VIA_COUNT": (None, "route"),
    "NET_STRAIGHT_COPPER": (None, "route"),
    "PINGRP_": (None, OWNER_DATA),
    "SILK_": ("silk-fit", "silk"),                 # B8: off board / over pad / over hole / overlap: the silk fit moves text, artwork goes to Fab
    "SILK_VERSION_STALE": ("silk-title", "silk"),
    "SILK_TITLE_": ("silk-title", "silk"),
    "SILK_TITLEBLOCK_": ("silk-title", "silk"),
    "VIA_TYPE": (None, OWNER_DATA),
    "FAB_": (None, OWNER_DATA),
    "TRACK_BELOW_FAB": (None, OWNER_DATA),
    "VIA_BELOW_FAB": (None, OWNER_DATA),
    "DRILL_BELOW_FAB": (None, OWNER_DATA),
    "GERBER_": (None, "fab"),
    "FP_": (None, "ready"),
    "OUTLINE_MISSING": (None, OWNER_DATA),
    "PAD_OFF_BOARD": ("row-edge", "floorplan"),
    # parts, data, spec: the ready gate asks, the owner answers
    "DATASHEET_MISSING": ("part-datasheet", "ready"),
    "PART_NO_STOCK": ("part-stock", "ready"),
    "PART_": (None, "ready"),
    "MPN_MISSING": (None, "ready"),
    "PINMAP_": (None, "ready"),
    "PIN_": (None, "ready"),
    "LANDPATTERN_": (None, "ready"),
    "FOOTPRINT_PACKAGE_MISMATCH": (None, "ready"),
    "MODEL_": (None, "ready"),
    "BOM_": (None, OWNER_DATA),
    "SPEC_": (None, OWNER_DATA),
    "NET_SINGLE_PAD": (None, OWNER_DATA),
    "LOCK_": (None, OWNER_DATA),
    "LIFECYCLE": (None, OWNER_DATA),
    "TEMP_RATING": (None, OWNER_DATA),
    "POLICY_": (None, OWNER_DATA),
    "CN_": (None, OWNER_DATA),
    "HOLDUP_SHORT": (None, OWNER_DATA),
    "TVS_MARGIN": (None, OWNER_DATA),
    "CAP_VOLTAGE_MARGIN": (None, OWNER_DATA),
    "CONTACT_CURRENT": (None, OWNER_DATA),
    "SUPPLY_RANGE": (None, OWNER_DATA),
    "SHEET_OFF_PAGE": (None, OWNER_DATA),
    "TITLEBLOCK_": (None, OWNER_DATA),
    "PART_SUBSTITUTION_": (None, OWNER_DATA),
    # B15: kicad-happy EMC (review) and SPICE (ready) findings, warn-only until a constraint promotes a code to FAIL
    "EMC_DC_": ("support-far", "floorplan"),            # decoupling cap far from its IC
    "EMC_ES_001": ("support-far", "floorplan"),         # ESD device far from its connector
    "EMC_ES_": (None, "route"),
    "EMC_SW_001": (None, OWNER_DATA),
    "EMC_SW_": ("row-pin-bypass", "floorplan"),         # switch-node copper / input-cap loop
    "EMC_DP_001": ("pair-skew", "tune"),
    "EMC_DP_": (None, "route"),
    "EMC_BE_001": (None, "route"),
    "EMC_BE_": (None, "close"),
    "EMC_VS_": (None, "close"),
    "EMC_GP_003": (None, OWNER_DATA),
    "EMC_GP_005": (None, OWNER_DATA),
    "EMC_GP_": (None, "route"),
    "EMC_CK_": (None, "route"),
    "EMC_RP_": (None, "route"),
    "EMC_XT_": (None, "route"),
    "EMC_": (None, OWNER_DATA),                         # stackup, I/O filter, PDN, EMI filter, thermal, shielding, emission estimates, UNKNOWN
    "SPICE_": (None, "ready"),
}
# codes a review can raise that the source scan cannot see (the level or the code is built at run time)
EXTRA_FAIL = ("MEASURE_DISAGREE", "UNROUTED", "CONGESTION", "SILK_OFF_BOARD", "SILK_OVER_PAD", "SILK_OVER_HOLE", "SILK_VERSION_STALE",
              "SILK_TITLE_NO_ROOM", "COLLISION", "CHANNEL_MAX")

# modules whose code-like strings are not review findings: the planner's PLAN_* findings, router drop reasons, a KiCad enum
_NOT_REVIEW = ("gapmap.py", "planroute.py", "globalroute.py", "batchroute.py", "routepar.py", "thermal.py")
_CODE = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")


def lookup(code):
    """(kind | None, owner) of a code: the exact row, else the longest family prefix; None when unmapped."""
    if code in _X:
        return _X[code]
    best = max((k for k in _X if k.endswith("_") and code.startswith(k)), key=len, default=None)
    return _X[best] if best else None


def owner(code):
    r = lookup(code)
    return r[1] if r else None


def kind(code):
    r = lookup(code)
    return r[0] if r else None


def _is_fail(n):
    if isinstance(n, ast.Name):
        return n.id == "FAIL"
    if isinstance(n, ast.Constant):
        return n.value == "FAIL"
    return isinstance(n, ast.IfExp) and (_is_fail(n.body) or _is_fail(n.orelse))


def _is_level(n):
    """A level expression that may be FAIL: the FAIL constant, a conditional with it, or anything not a constant (a variable)."""
    if _is_fail(n):
        return True
    return not isinstance(n, ast.Constant) and not (isinstance(n, ast.Name) and n.id in ("WARN", "INFO"))


def _codes(n):
    if isinstance(n, ast.Constant) and isinstance(n.value, str) and _CODE.match(n.value):
        return [n.value]
    if isinstance(n, ast.IfExp):
        return _codes(n.body) + _codes(n.orelse)
    return []


def fail_codes(pkg_dir=None):
    """{code: [module, ...]} of every code the package emits at level FAIL, or at a level the scan cannot read: `_f(LEVEL, "CODE", ...)`,
    `(LEVEL, "CODE", ...)` tuples, `{"level": LEVEL, "code": "CODE"}` dicts, plus EXTRA_FAIL. A non-constant level counts (it may be FAIL)."""
    pkg_dir = pkg_dir or os.path.dirname(os.path.abspath(__file__))
    res = {}
    for path in sorted(glob.glob(os.path.join(pkg_dir, "*.py"))):
        mod = os.path.basename(path)
        if mod in _NOT_REVIEW:
            continue
        for n in ast.walk(ast.parse(open(path, errors="surrogateescape").read())):
            if isinstance(n, (ast.Call, ast.Tuple)):
                el = n.args if isinstance(n, ast.Call) else n.elts
                for i, a in enumerate(el[:-1]):
                    if _is_level(a):
                        for c in _codes(el[i + 1]):
                            res.setdefault(c, set()).add(mod)
            elif isinstance(n, ast.Dict):
                d = {k.value: v for k, v in zip(n.keys, n.values) if isinstance(k, ast.Constant)}
                lv = d.get("level", d.get("severity"))
                if lv is not None and "code" in d and _is_level(lv):
                    for c in _codes(d["code"]):
                        res.setdefault(c, set()).add(mod)
            elif isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict) and any(isinstance(t, ast.Name) and t.id == "LEVEL" for t in n.targets):
                for k, v in zip(n.value.keys, n.value.values):                  # silk.LEVEL = {"CODE": FAIL, ...}
                    if isinstance(k, ast.Constant) and _is_fail(v) and _CODE.match(str(k.value)):
                        res.setdefault(k.value, set()).add(mod)
    from . import happy
    for c in EXTRA_FAIL + happy.codes():            # B15: the adapter's codes are built at run time and promotable to FAIL
        res.setdefault(c, set()).add("(dynamic)")
    return {c: sorted(m) for c, m in sorted(res.items())}


def missing(codes=None):
    """The FAIL codes with no row (neither an owner stage nor the data/owner tag)."""
    codes = fail_codes() if codes is None else codes
    return sorted(c for c in codes if owner(c) is None)


def kinds_known():
    """Every gap kind a dispatcher knows (placegaps + gaps)."""
    from . import gaps, placegaps
    return set(placegaps.ORDER) | set(gaps.ORDER)


def uncovered(findings, gaps, level="FAIL"):
    """The review findings (dicts: level, code, refs) of `level` whose mapped placement gap kind has no gap on the same parts: what a
    placement gate must list but did not. Findings whose code maps to another kind (or none) are not the placement gate's."""
    from . import placegaps as PG
    mine = [f for f in findings if f.get("level") == level and kind(f["code"]) in set(PG.ORDER)]
    out = []
    for f in mine:
        refs = set(f.get("refs") or ())
        if not any(g["kind"] == kind(f["code"]) and refs <= set(g.get("refs") or ()) for g in gaps):
            out.append(f)
    return out
