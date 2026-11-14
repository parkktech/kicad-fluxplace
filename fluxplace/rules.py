"""Selector rules: ONE cascade for per-object settings (net width, clearance, straight copper, skew ...).

Two sources feed the same list of rules:

  * `[[rule]]` entries (the selector form):

        [[rule]]
        name = "usb pairs"                 # optional, shown in reports
        select = { net = "^USB_.*" }       # keys: ref, net, netclass, type, layer, prop, pair (AND)
        set = { clearance_mm = 0.2, width_mm = 0.15 }
        important = false

  * the existing per-object blocks, translated at load time so there is one resolution path:
    `[power.X]`, `[nets.X]`, `[rf] nets = [...]`, `[pairs.FAMILY]`, and the `[route]` floors.

Specificity, high to low:  exact ref / exact net  >  ref glob / net regex  >  prop  >  pair
>  netclass  >  type / layer  >  global default (empty select).  Several select keys are AND
and rank by their most specific key.  Among equal specificity the longer pair family (or longer
glob/regex pattern) wins, then the rule with more select keys, then the later rule (legacy blocks load
first, `[[rule]]` entries in file order after them).
`important = true` beats any non-important rule regardless of specificity.

`resolve(obj, key)` is per key: the winner is the best-ranked rule that SETS that key and matches
the object, so a `[[rule]]` that sets only `width_mm` does not hide a `clearance_mm` from elsewhere.

An object is a dict: {"ref", "net", "netclass", "type", "layer", "props": {field: text}}; missing
attributes simply never match a selector that needs them.
"""
import fnmatch
import re
from dataclasses import dataclass, field

SELECT_KEYS = ("ref", "net", "netclass", "type", "layer", "prop", "pair")
OBJECT_TYPES = ("track", "via", "pad", "footprint", "zone")

# set key -> where the legacy block states it. Names are the SAME names the blocks use.
SET_KEYS = {
    "width_mm": "[power.X] width_mm | max_current_ma, [nets.X] width_mm, [rf] nets width_mm, [pairs.F] width_mm",
    "gap_mm": "[pairs.F] gap_mm",
    "clearance_mm": "[pairs.F] clearance_mm",
    "skew_mm": "[pairs.F] skew_mm",
    "skew_ps": "[pairs.F] skew_ps (converted to mm with the layer delay of the pair)",
    "impedance_diff": "[pairs.F] impedance_diff",
    "straight_copper": "[nets.X] straight_copper",
    "max_vias": "[nets.X] max_vias, [rf] nets max_vias",
    "pour": "[power.X] pour",
    "min_track_mm": "[route] min_track_mm (a floor; routerules takes the max with [fab])",
    "min_clearance_mm": "[route] min_clearance_mm (a floor; routerules takes the max with [fab])",
}
_NUMERIC = {"width_mm", "gap_mm", "clearance_mm", "skew_mm", "skew_ps", "impedance_diff", "max_vias",
            "min_track_mm", "min_clearance_mm"}

LEVEL = {"exact": 100, "pattern": 90, "prop": 70, "pair": 60, "netclass": 50, "type_layer": 40, "default": 0}
_GLOB = set("*?[")
_EXACT_RX = re.compile(r"\^((?:\\.|[^\\.^$*+?()\[\]{}|])*)\$")


class RuleError(ValueError):
    """A rule the engineer wrote is wrong. `code` is the review code (RULE_UNKNOWN_KEY, ...)."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


@dataclass
class Rule:
    origin: str                      # "[[rule]] #2" or "[nets.PTT_THRU]" ...
    name: str | None
    select: dict
    set: dict
    important: bool = False
    order: int = 0
    legacy: bool = False
    _rx: object = field(default=None, repr=False)
    _level: int = 0
    _detail: int = 0

    @property
    def label(self):
        return f'"{self.name}"' if self.name else self.origin

    def rank(self):
        """Sort key, higher wins: (important, specificity level, detail, number of select keys, file order).
        detail = pair family length, else the net/ref pattern length (longer = narrower)."""
        return (self.important, self._level, self._detail, len(self.select), self.order)

    def describe_select(self):
        if not self.select:
            return "*"
        return " & ".join(f"{k}={v}" for k, v in self.select.items())


def _ref_level(pat):
    return LEVEL["exact"] if not (set(pat) & _GLOB) else LEVEL["pattern"]


def _net_level(rx):
    return LEVEL["exact"] if _EXACT_RX.fullmatch(rx) else LEVEL["pattern"]


def _compile_select(origin, select):
    """Validate one selector; return (rule level, detail, compiled net regex)."""
    if not isinstance(select, dict):
        raise RuleError("RULE_BAD_VALUE", f"{origin}: select must be a table")
    for k in select:
        if k not in SELECT_KEYS:
            raise RuleError("RULE_UNKNOWN_KEY", f"{origin}: unknown select key '{k}' (known: {', '.join(SELECT_KEYS)})")
    levels, detail, rx = [], 0, None
    if "ref" in select:
        if not isinstance(select["ref"], str):
            raise RuleError("RULE_BAD_VALUE", f"{origin}: select.ref must be a string")
        levels.append(_ref_level(select["ref"]))
        detail = max(detail, len(select["ref"]) if levels[-1] == LEVEL["pattern"] else 0)
    if "net" in select:
        if not isinstance(select["net"], str):
            raise RuleError("RULE_BAD_VALUE", f"{origin}: select.net must be a regex string")
        try:
            rx = re.compile(select["net"])
        except re.error as e:
            raise RuleError("RULE_BAD_REGEX", f"{origin}: select.net '{select['net']}' is not a regex: {e}")
        levels.append(_net_level(select["net"]))
        detail = max(detail, len(select["net"]) if levels[-1] == LEVEL["pattern"] else 0)
    if "prop" in select:
        p = select["prop"]
        if not isinstance(p, dict) or not all(isinstance(v, str) for v in p.values()):
            raise RuleError("RULE_BAD_VALUE", f"{origin}: select.prop must be {{Field = \"substring\"}}")
        levels.append(LEVEL["prop"])
    if "pair" in select:
        if not isinstance(select["pair"], str) or not select["pair"]:
            raise RuleError("RULE_BAD_VALUE", f"{origin}: select.pair must be a family prefix")
        levels.append(LEVEL["pair"])
        detail = max(detail, len(select["pair"]))
    if "netclass" in select:
        levels.append(LEVEL["netclass"])
    if "type" in select:
        if select["type"] not in OBJECT_TYPES:
            raise RuleError("RULE_BAD_VALUE", f"{origin}: select.type must be one of {', '.join(OBJECT_TYPES)}")
        levels.append(LEVEL["type_layer"])
    if "layer" in select:
        levels.append(LEVEL["type_layer"])
    return (max(levels) if levels else LEVEL["default"]), detail, rx


def _check_set(origin, values):
    if not isinstance(values, dict):
        raise RuleError("RULE_BAD_VALUE", f"{origin}: set must be a table")
    for k, v in values.items():
        if k not in SET_KEYS:
            raise RuleError("RULE_UNKNOWN_KEY", f"{origin}: unknown set key '{k}' (known: {', '.join(SET_KEYS)})")
        if k in _NUMERIC and (isinstance(v, bool) or not isinstance(v, (int, float))):
            raise RuleError("RULE_BAD_VALUE", f"{origin}: set.{k} must be a number")
        if k == "straight_copper" and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
            raise RuleError("RULE_BAD_VALUE", f"{origin}: set.straight_copper must be a list of \"REF:PIN\" strings")
        if k == "pour" and not isinstance(v, bool):
            raise RuleError("RULE_BAD_VALUE", f"{origin}: set.pour must be true or false")
    return dict(values)


def _make(origin, name, select, values, important, order, legacy=False):
    level, detail, rx = _compile_select(origin, select)
    r = Rule(origin=origin, name=name, select=dict(select), set=values, important=bool(important),
             order=order, legacy=legacy, _rx=rx, _level=level, _detail=detail)
    return r


def _legacy(cons, out):
    """Translate the per-object blocks into rules (exact/pair level). Only keys in SET_KEYS move;
    other keys stay with their own readers (fab floors, placement weights, si defaults ...)."""
    def add(origin, select, values):
        if values:
            out.append(_make(origin, None, select, values, False, len(out), legacy=True))

    route = dict((cons.get("route") or {}))
    add("[route]", {}, {k: float(route[k]) for k in ("min_track_mm", "min_clearance_mm")
                        if isinstance(route.get(k), (int, float)) and not isinstance(route.get(k), bool)})

    for net, p in (cons.get("power") or {}).items():
        if not isinstance(p, dict):
            continue
        vals = {}
        if "width_mm" in p:
            vals["width_mm"] = float(p["width_mm"])
        elif "max_current_ma" in p:
            vals["width_mm"] = max(0.3, 0.5 * float(p["max_current_ma"]) / 1000.0)
        if p.get("pour") is True:
            vals["pour"] = True
        add(f'[power."{net}"]', {"net": "^" + re.escape(net) + "$"}, vals)

    rf = cons.get("rf") or {}
    for net in rf.get("nets", []) or []:
        vals = {}
        if isinstance(rf.get("width_mm"), (int, float)) and not isinstance(rf.get("width_mm"), bool):
            vals["width_mm"] = float(rf["width_mm"])
        if isinstance(rf.get("max_vias"), int) and not isinstance(rf.get("max_vias"), bool):
            vals["max_vias"] = rf["max_vias"]
        add("[rf] nets", {"net": "^" + re.escape(net) + "$"}, vals)

    for net, n in (cons.get("nets") or {}).items():
        if not isinstance(n, dict):
            continue
        vals = {}
        if "straight_copper" in n:
            vals["straight_copper"] = list(n["straight_copper"])
        if "max_vias" in n:
            vals["max_vias"] = n["max_vias"]
        if "width_mm" in n:
            vals["width_mm"] = float(n["width_mm"])
        add(f"[nets.{net}]", {"net": "^" + re.escape(net) + "$"}, vals)

    for fam, p in (cons.get("pairs") or {}).items():
        if not isinstance(p, dict):
            continue
        vals = {k: p[k] for k in ("width_mm", "gap_mm", "clearance_mm", "skew_mm", "skew_ps", "impedance_diff")
                if isinstance(p.get(k), (int, float)) and not isinstance(p.get(k), bool)}
        add(f"[pairs.{fam}]", {"pair": fam}, {k: float(v) if k != "impedance_diff" else v for k, v in vals.items()})


class RuleSet(list):
    """The ordered rule list from `load`, with the cascade methods."""

    def cascade(self, obj, key):
        """Every rule that sets `key` and matches `obj`, best first: [(rule, value)]. The first is the winner."""
        hits = [r for r in self if key in r.set and matches(r, obj)]
        hits.sort(key=lambda r: r.rank(), reverse=True)
        return [(r, r.set[key]) for r in hits]

    def resolve(self, obj, key):
        """-> (value, provenance) for one key; (None, None) when no rule sets it for this object."""
        hits = self.cascade(obj, key)
        if not hits:
            return None, None
        r, v = hits[0]
        return v, r.label

    def resolve_all(self, obj):
        """-> {key: (value, provenance)} for every key some matching rule sets."""
        keys = []
        for r in self:
            if matches(r, obj):
                keys += [k for k in r.set if k not in keys]
        return {k: self.resolve(obj, k) for k in keys}


def load(cons):
    """Ordered RuleSet: legacy blocks first (translated), then `[[rule]]` entries in file order.
    Raises RuleError for a bad `[[rule]]`; the legacy blocks never raise."""
    cons = cons or {}
    rs = RuleSet(_legacy_rules(cons))
    for i, raw in enumerate(cons.get("rule") or []):
        origin = f"[[rule]] #{i + 1}"
        if not isinstance(raw, dict):
            raise RuleError("RULE_BAD_VALUE", f"{origin}: must be a table")
        for k in raw:
            if k not in ("name", "select", "set", "important"):
                raise RuleError("RULE_UNKNOWN_KEY", f"{origin}: unknown key '{k}' (known: name, select, set, important)")
        name = raw.get("name")
        if name is not None and not isinstance(name, str):
            raise RuleError("RULE_BAD_VALUE", f"{origin}: name must be a string")
        imp = raw.get("important", False)
        if not isinstance(imp, bool):
            raise RuleError("RULE_BAD_VALUE", f"{origin}: important must be true or false")
        vals = _check_set(origin, raw.get("set", {}))
        rs.append(_make(origin, name, raw.get("select", {}), vals, imp, len(rs)))
    return rs


def _legacy_rules(cons):
    tmp = []
    _legacy(cons, tmp)
    return tmp


def matches(rule, obj):
    """True when every select key of `rule` holds for `obj` (AND)."""
    s = rule.select
    if not s:
        return True
    if "ref" in s:
        r = obj.get("ref")
        if not r or not (r == s["ref"] or (set(s["ref"]) & _GLOB and fnmatch.fnmatchcase(r, s["ref"]))):
            return False
    if "net" in s:
        n = obj.get("net")
        if not n or not rule._rx.search(n):
            return False
    if "prop" in s:
        props = obj.get("props") or {}
        if not all(k in props and v in str(props[k]) for k, v in s["prop"].items()):
            return False
    if "pair" in s:
        n = obj.get("net")
        if not n or not n.startswith(s["pair"]):
            return False
    if "netclass" in s and obj.get("netclass") != s["netclass"]:
        return False
    if "type" in s and obj.get("type") != s["type"]:
        return False
    if "layer" in s and obj.get("layer") != s["layer"]:
        return False
    return True


def resolve(rules, obj, key):
    """Module-level form of RuleSet.resolve."""
    return rules.resolve(obj, key)


def resolve_all(rules, obj):
    """Module-level form of RuleSet.resolve_all."""
    return rules.resolve_all(obj)


def check_matches(rules, nets=(), refs=(), objects=None, notes=None):
    """RULE_MATCHES_NOTHING warnings: [(rule, text)] for rules whose selectors match no object.

    With `objects` (board elements from `board_element_objects`: tracks, vias, zones, pads and
    footprints, each with net/netclass/layer/type) every select key is evaluated. Without them only
    net/ref/pair can be checked from names; a rule that also has netclass/type/layer is checked on
    its name keys alone and a note is appended to `notes` (when given) saying what was skipped."""
    import dataclasses
    out = []
    name_objs = [{"net": n} for n in nets] + [{"ref": f} for f in refs]
    for r in rules:
        if not r.set or r.legacy:
            continue
        s = r.select
        if objects is not None:
            pool, rr = objects, r
        else:
            if not ({"net", "ref", "pair"} & set(s)):
                continue
            skipped = sorted(set(s) - {"net", "ref", "pair"})
            pool = name_objs
            rr = r
            if skipped:
                rr = dataclasses.replace(r, select={k: v for k, v in s.items() if k in ("net", "ref", "pair")})
                if notes is not None:
                    notes.append(f"{r.label}: {', '.join(skipped)} not checked (no board objects)")
        if not any(matches(rr, o) for o in pool):
            out.append((r, f"{r.label} matches no net or footprint on this board ({r.describe_select()})"
                        if objects is None else
                        f"{r.label} matches no board object ({r.describe_select()})"))
    return out


def matched_nets(rule, nets):
    """Net names a rule's select hits (for the rules table: makes regex scope surprises visible)."""
    return sorted({n["net"] for n in nets if matches(rule, n)})


def board_objects(board_path):
    """-> (nets, footprints) as objects, read with pcbnew. Nets carry their netclass; footprints their fields."""
    import pcbnew
    b = pcbnew.LoadBoard(board_path)
    nets = []
    for name, item in b.GetNetInfo().NetsByName().items():
        name = str(name)
        if name:
            nets.append({"net": name, "netclass": str(item.GetNetClassName())})
    fps = []
    for fp in b.GetFootprints():
        props = {str(f.GetName()): str(f.GetText()) for f in fp.GetFields()}
        fps.append({"ref": str(fp.GetReference()), "type": "footprint",
                    "layer": str(fp.GetLayerName()), "props": props})
    return nets, fps


def board_element_objects(board_path):
    """Every board element a selector can match: tracks, vias, zones, pads (with ref/net/netclass/layer).
    One object per layer an element sits on. Needs pcbnew."""
    import pcbnew
    b = pcbnew.LoadBoard(board_path)
    net_class = {}
    for name, item in b.GetNetInfo().NetsByName().items():
        net_class[str(name)] = str(item.GetNetClassName())

    def _net(item):
        n = item.GetNet() if hasattr(item, "GetNet") else None
        name = str(n.GetNetname()) if n is not None else ""
        return name, net_class.get(name, "")

    objs = []
    for t in b.GetTracks():
        net, nc = _net(t)
        if t.Type() == pcbnew.PCB_VIA_T:
            layers = [str(b.GetLayerName(i)) for i in t.GetLayerSet().Seq()]
            objs += [{"net": net, "netclass": nc, "type": "via", "layer": ly} for ly in layers]
        else:
            objs.append({"net": net, "netclass": nc, "type": "track", "layer": str(b.GetLayerName(t.GetLayer()))})
    for z in b.Zones():
        net, nc = _net(z)
        for ly in z.GetLayerSet().Seq():
            objs.append({"net": net, "netclass": nc, "type": "zone", "layer": str(b.GetLayerName(ly))})
    for fp in b.GetFootprints():
        ref = str(fp.GetReference())
        props = {str(f.GetName()): str(f.GetText()) for f in fp.GetFields()}
        objs.append({"ref": ref, "type": "footprint", "layer": str(fp.GetLayerName()), "props": props})
        for pad in fp.Pads():
            net, nc = _net(pad)
            objs.append({"ref": ref, "net": net, "netclass": nc, "type": "pad",
                         "layer": str(fp.GetLayerName()), "props": props})
    return objs


__all__ = ["load", "RuleSet", "RuleError", "resolve", "resolve_all", "matches", "check_matches",
           "matched_nets", "board_element_objects", "board_objects", "SET_KEYS", "SELECT_KEYS", "OBJECT_TYPES"]
