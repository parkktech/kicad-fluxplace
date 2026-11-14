"""Drafted block schematic: per-block layout, wiring and KiCad emission (`fluxplace schematic --layout blocks`).

Coordinates are 50-mil grid units (1.27 mm), y down. Every symbol is placed at rotation 0: passives come as vertical and
horizontal library variants, power symbols as up/down variants, so field text never has to be rotated.

One block = one hierarchical sheet. Inside a block every part that is not a 2-pad passive is a CELL: a box symbol whose
ground pins sit on the bottom (GND symbols), rail pins on top (rail symbols) and signal pins left/right in pad order.
Each signal pin grows a FAN TREE outward: the passives on its net hang off a trunk wire (to ground: down, to a rail: up,
to another signal: in series, recursing on the far net). Pin rows are spaced to fit their trees, so the trees need no
routing. Passives no tree takes (decoupling, rail-to-rail parts) sit in rows under their cell. Cells are packed in rows
with the anchor (most pins) in the middle of the first row. Nets inside a block that still have several pieces are
joined by a grid maze router (no overlap with another net's wire, no bend on or ending at another net's wire, no pass
through a body, a label or text; crossings are plain crossings); a piece the router cannot join gets a local label.
Ground/rail nets are power symbols; a net that leaves the block gets a global label; an internal net gets one local
label per piece (its name, so the netlist names match the spec); a declared-but-unused pad gets a no_connect flag.
Wires are split at every attachment point and junctions are written where three or more items meet.
"""
import heapq
import os
import re
from collections import defaultdict

from . import schinv as I

G = 1.27
CW = 0.92            # stroke-font advance per character at 1.27 mm, in grid units (measured: kicad-cli render, upper case + digits 0.84-0.92)
FONT = "(effects (font (size 1.27 1.27))%s)"


def tw(s):
    return len(str(s)) * CW


def slot_len(net):
    """Length of a wire that carries a local label: 1 grid clear of the pin number, the text, half a grid."""
    return ceil(tw(net) + 2.5)


def ceil(v):
    return int(-(-v // 1))


def _uuid(seed):
    import hashlib
    h = hashlib.sha1(seed.encode()).hexdigest()
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def mm(v):
    return "%.4f" % (v * G) if abs(v * G - round(v * G, 2)) > 1e-9 else "%.2f" % (v * G)


# ------------------------------------------------------------------ drawing container
class Draw:
    def __init__(self):
        self.syms = []      # {lib, ref, value, fp, x, y, fields:[(name,val,x,y,just,hide)], dnp, kind, pins:[(x,y,net)]}
        self.wires = []     # [x1, y1, x2, y2, net]
        self.labels = []    # {kind: local|global, net, x, y, dir: +1 right / -1 left}
        self.ncs = []       # (x, y)
        self.rects = []     # obstacles (x0, y0, x1, y1, kind)
        self.slots = []     # label slots {net, x, y, dir, kind, len}
        self.pts = []       # (x, y, net) connection points of pins / power symbols

    def add(self, o, dx=0, dy=0):
        for s in o.syms:
            t = dict(s)
            t["x"] += dx
            t["y"] += dy
            t["fields"] = [(n, v, x + dx, y + dy, j, h) for n, v, x, y, j, h in s["fields"]]
            t["pins"] = [(x + dx, y + dy, n) for x, y, n in s["pins"]]
            self.syms.append(t)
        self.wires += [[a + dx, b + dy, c + dx, d + dy, n] for a, b, c, d, n in o.wires]
        self.labels += [dict(l, x=l["x"] + dx, y=l["y"] + dy) for l in o.labels]
        self.ncs += [(x + dx, y + dy) for x, y in o.ncs]
        self.rects += [(a + dx, b + dy, c + dx, d + dy, k) for a, b, c, d, k in o.rects]
        self.slots += [dict(s, x=s["x"] + dx, y=s["y"] + dy) for s in o.slots]
        self.pts += [(x + dx, y + dy, n) for x, y, n in o.pts]
        self.lib = self.lib or o.lib

    lib = None

    def mirror(self):
        """x -> -x (a left-side fan tree is drawn as a right-side one, then mirrored)."""
        for s in self.syms:
            s["x"] = -s["x"]
            if s.get("orient") == "h":         # a horizontal passive swaps ends
                s["pa"], s["pb"] = s["pb"], s["pa"]
                s["lib"] = passive_lib(self.lib, s["pk"], "h", s["pa"], s["pb"])
            s["fields"] = [(n, v, -x, y, {"left": "right", "right": "left"}.get(j, j), h) for n, v, x, y, j, h in s["fields"]]
            s["pins"] = [(-x, y, n) for x, y, n in s["pins"]]
        self.wires = [[-a, b, -c, d, n] for a, b, c, d, n in self.wires]
        for l in self.labels:
            l["x"], l["dir"] = -l["x"], -l["dir"]
        self.ncs = [(-x, y) for x, y in self.ncs]
        self.rects = [(-c, b, -a, d, k) for a, b, c, d, k in self.rects]
        for s in self.slots:
            s["x"], s["dir"] = -s["x"], -s["dir"]
        self.pts = [(-x, y, n) for x, y, n in self.pts]

    def bbox(self):
        xs, ys = [], []
        for a, b, c, d, _k in self.rects:
            xs += [a, c]
            ys += [b, d]
        for a, b, c, d, _n in self.wires:
            xs += [a, c]
            ys += [b, d]
        for x, y, _n in self.pts:
            xs.append(x)
            ys.append(y)
        for x, y in self.ncs:
            xs += [x - 1, x + 1]
            ys += [y - 1, y + 1]
        if not xs:
            return (0, 0, 0, 0)
        return (min(xs), min(ys), max(xs), max(ys))


# ------------------------------------------------------------------ library symbols
class Lib:
    def __init__(self):
        self.syms = {}

    def text(self, used):
        return [self.syms[k] for k in sorted(used)]


def _prop(name, val, x, y, hide=False, just=""):
    return '      (property "%s" "%s" (at %s %s 0) %s)' % (
        name, _esc(val), mm(x), mm(y), FONT % ((" (justify %s)" % just if just else "") + (" hide" if hide else "")))


def _pin(num, name, x, y, ang, length, hide=False, kind="passive"):
    return ('        (pin %s line (at %s %s %d) (length %s)%s (name "%s" %s) (number "%s" %s))'
            % (kind, mm(x), mm(y), ang, mm(length), " hide" if hide else "", _esc(name), FONT % "", _esc(num), FONT % ""))


PKIND = [("R", r"^(R|RN)\d"), ("C", r"^C\d"), ("L", r"^(L|FB)\d"), ("D", r"^(D|LED)\d"), ("F", r"^F\d"), ("Y", r"^(Y|X|XO)\d")]


def pkind(ref):
    for k, rx in PKIND:
        if re.match(rx, ref):
            return k
    return "Z"


def passive_lib(lib, kind, orient, pa, pb):
    """2-pin symbol. orient 'v': pa top, pb bottom; 'h': pa left, pb right. Pins end 2 grid from the centre."""
    name = "P%s%s_%s_%s" % (kind, orient, re.sub(r"[^A-Za-z0-9]", "_", pa), re.sub(r"[^A-Za-z0-9]", "_", pb))
    lid = "fluxplace:" + name
    if lid in lib.syms:
        return lid
    g = []
    v = orient == "v"

    def P(x, y):         # body drawn for the vertical case in screen grid, mapped to lib mm (y up)
        if v:
            return "(xy %s %s)" % (mm(x), mm(-y))
        return "(xy %s %s)" % (mm(y), mm(-x))   # horizontal: rotate the vertical drawing
    poly = lambda pts, fill="none": "        (polyline (pts %s) (stroke (width 0.254) (type default)) (fill (type %s)))" % (
        " ".join(P(x, y) for x, y in pts), fill)
    if kind == "R":
        g.append(poly([(-0.4, -1), (0.4, -1), (0.4, 1), (-0.4, 1), (-0.4, -1)]))
    elif kind == "C":
        g.append(poly([(-0.8, -0.2), (0.8, -0.2)]))
        g.append(poly([(-0.8, 0.2), (0.8, 0.2)]))
        g.append(poly([(0, -1), (0, -0.2)]))
        g.append(poly([(0, 0.2), (0, 1)]))
    elif kind == "L":
        pts = []
        import math
        for i in range(4):
            for k in range(7):
                a = math.pi * k / 6.0
                pts.append((0.35 * math.sin(a), -1 + 0.5 * i + 0.25 - 0.25 * math.cos(a)))
        g.append(poly(pts))
    elif kind == "D":           # anode (pa) at top / left
        g.append(poly([(-0.6, -0.4), (0.6, -0.4), (0, 0.4), (-0.6, -0.4)], "outline"))
        g.append(poly([(-0.6, 0.4), (0.6, 0.4)]))
        g.append(poly([(0, -1), (0, -0.4)]))
        g.append(poly([(0, 0.4), (0, 1)]))
    elif kind == "F":
        g.append(poly([(-0.4, -1), (0.4, -1), (0.4, 1), (-0.4, 1), (-0.4, -1)]))
        g.append(poly([(0, -1), (0, 1)]))
    elif kind == "Y":
        g.append(poly([(-0.6, -0.3), (0.6, -0.3), (0.6, 0.3), (-0.6, 0.3), (-0.6, -0.3)]))
        g.append(poly([(-0.8, -0.55), (0.8, -0.55)]))
        g.append(poly([(-0.8, 0.55), (0.8, 0.55)]))
        g.append(poly([(0, -1), (0, -0.55)]))
        g.append(poly([(0, 0.55), (0, 1)]))
    else:
        g.append(poly([(-0.5, -1), (0.5, -1), (0.5, 1), (-0.5, 1), (-0.5, -1)]))
    if v:
        pins = [_pin(pa, "", 0, 2, 270, 1), _pin(pb, "", 0, -2, 90, 1)]
    else:
        pins = [_pin(pa, "", -2, 0, 0, 1), _pin(pb, "", 2, 0, 180, 1)]
    L = ['    (symbol "%s" (pin_numbers hide) (pin_names (offset 0) hide) (in_bom yes) (on_board yes)' % lid,
         _prop("Reference", kind, 0, 0, True), _prop("Value", "", 0, 0, True), _prop("Footprint", "", 0, 0, True),
         '      (symbol "%s_0_1"' % name] + g + ["      )", '      (symbol "%s_1_1"' % name] + pins + ["      )", "    )"]
    lib.syms[lid] = "\n".join(L)
    return lid


def power_lib(lib, net, kind, up):
    """Power symbol for `net`: kind 'gnd' (bar/triangle) or 'rail' (arrow); `up` = graphics above the pin."""
    tag = re.sub(r"[^A-Za-z0-9]", "_", net)
    import hashlib
    name = "%s%s_%s_%s" % ("GND" if kind == "gnd" else "PWR", "U" if up else "D", tag, hashlib.sha1(net.encode()).hexdigest()[:5])
    lid = "fluxplace:" + name
    if lid in lib.syms:
        return lid
    s = 1 if up else -1           # lib y is up
    if kind == "gnd":
        g = ["        (polyline (pts (xy 0 0) (xy 0 %s)) (stroke (width 0) (type default)) (fill (type none)))" % mm(s * 1),
             "        (polyline (pts (xy -1.27 %s) (xy 1.27 %s) (xy 0 %s) (xy -1.27 %s)) (stroke (width 0) (type default)) (fill (type none)))"
             % (mm(s * 1), mm(s * 1), mm(s * 2), mm(s * 1))]
    else:
        g = ["        (polyline (pts (xy 0 0) (xy 0 %s)) (stroke (width 0) (type default)) (fill (type none)))" % mm(s * 2),
             "        (polyline (pts (xy -0.762 %s) (xy 0 %s) (xy 0.762 %s)) (stroke (width 0) (type default)) (fill (type none)))"
             % (mm(s * 1), mm(s * 2), mm(s * 1))]
    L = ['    (symbol "%s" (power) (pin_numbers hide) (pin_names (offset 0) hide) (in_bom yes) (on_board yes)' % lid,
         _prop("Reference", "#PWR", 0, 0, True), _prop("Value", net, 0, s * 3.2), _prop("Footprint", "", 0, 0, True),
         '      (symbol "%s_0_1"' % name] + g + ["      )", '      (symbol "%s_1_1"' % name,
                                                    _pin("1", net, 0, 0, 90 if up else 270, 0, True, "power_in"), "      )", "    )"]
    lib.syms[lid] = "\n".join(L)
    return lid


def flag_lib(lib):
    lid = "fluxplace:PWR_FLAG"
    if lid not in lib.syms:
        lib.syms[lid] = "\n".join([
            '    (symbol "%s" (power) (pin_numbers hide) (pin_names (offset 0) hide) (in_bom yes) (on_board yes)' % lid,
            _prop("Reference", "#FLG", 0, 1.5, True), _prop("Value", "PWR_FLAG", 0, 3.2), _prop("Footprint", "", 0, 0, True),
            '      (symbol "PWR_FLAG_0_0"', _pin("1", "pwr", 0, 0, 90, 0, False, "power_out"), "      )",
            '      (symbol "PWR_FLAG_0_1"',
            "        (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27)) "
            "(stroke (width 0) (type default)) (fill (type none)))", "      )", "    )"])
    return lid


# ------------------------------------------------------------------ primitive placements (local grid coords)
def put_power(d, lib, net, kind, x, y, up):
    lid = power_lib(lib, net, kind, up)
    s = -1 if up else 1                       # screen direction of the graphics
    ty = y + s * 3.2
    d.syms.append({"lib": lid, "ref": "#PWR", "value": net, "fp": "", "x": x, "y": y, "kind": "pwr", "net": net,
                   "fields": [("Value", net, x, ty, "", False)], "pins": [(x, y, net)], "dnp": False})
    d.pts.append((x, y, net))
    w = max(2.2, tw(net)) / 2.0
    d.rects.append((x - w, min(y + s * 0.6, ty - 0.7 * s), x + w, max(y + s * 0.6, ty + 0.7 * s), "pwr"))
    d.rects.append((x - 1.1, min(y + s * 0.6, y + s * 2.2), x + 1.1, max(y + s * 0.6, y + s * 2.2), "pwr"))


def put_passive(d, lib, c, orient, x, y, pa, pb, M, text_side=1):
    """Passive centred at (x, y); pa is the pad at top/left. text_side: +1 text right of a vertical part, -1 left."""
    kind = pkind(c["ref"])
    lid = passive_lib(lib, kind, orient, pa, pb)
    na, nb = c["pins"][pa]["net"], c["pins"][pb]["net"]
    if orient == "v":
        pins = [(x, y - 2, na), (x, y + 2, nb)]
        just = "left" if text_side > 0 else "right"
        fx = x + 1.0 * text_side
        fields = [("Reference", c["ref"], fx, y - 0.6, just, False), ("Value", c["value"], fx, y + 0.6, just, False)]
        w = max(tw(c["ref"]), tw(c["value"]))
        d.rects.append((min(fx, fx + w * text_side), y - 1.3, max(fx, fx + w * text_side), y + 1.3, "text"))
        d.rects.append((x - 0.9, y - 1.4, x + 0.9, y + 1.4, "body"))
    else:
        pins = [(x - 2, y, na), (x + 2, y, nb)]
        fields = [("Reference", c["ref"], x, y - 1.5, "", False), ("Value", c["value"], x, y + 1.5, "", False)]
        w = max(tw(c["ref"]), tw(c["value"])) / 2.0
        d.rects.append((x - w, y - 2.1, x + w, y - 0.9, "text"))
        d.rects.append((x - w, y + 0.9, x + w, y + 2.1, "text"))
        d.rects.append((x - 1.4, y - 0.9, x + 1.4, y + 0.9, "body"))
    fields.append(("Footprint", c["fp"], x, y, "", True))
    d.syms.append({"lib": lid, "ref": c["ref"], "value": c["value"], "fp": c["fp"], "x": x, "y": y, "kind": "part",
                   "fields": fields, "pins": pins, "dnp": c["dnp"], "orient": orient, "pa": pa, "pb": pb, "pk": kind})
    d.lib = lib
    for px, py, n in pins:
        d.pts.append((px, py, n))
    return pins


# ------------------------------------------------------------------ fan trees
class Ctx:
    def __init__(self, M, kinds, block_refs, lib, external):
        self.M, self.kinds, self.lib = M, kinds, lib
        self.comps = M["comps"]
        self.pool = {r for r in block_refs if I.two_pin(M["comps"][r])}
        self.external = external          # nets that leave the block
        self.by_net = defaultdict(list)
        for r in sorted(block_refs, key=I.natkey):
            for p, i in M["comps"][r]["pins"].items():
                self.by_net[i["net"]].append(r)

    def take(self, net, limit=10):
        out = [r for r in self.by_net[net] if r in self.pool][:limit]
        for r in out:
            self.pool.discard(r)
        return out


def build_tree(ctx, net, seen, depth=0):
    """-> node {net, shunts: [(ref, pad_on_net, pad_other, kind)], series: [(ref, pad_near, pad_far, child)]}"""
    seen = seen | {net}
    node = {"net": net, "shunts": [], "series": []}
    for r in ctx.take(net):
        c = ctx.comps[r]
        pads = sorted(c["pins"], key=I.natkey)
        near = next(p for p in pads if c["pins"][p]["net"] == net)
        far = next((p for p in pads if p != near), None)
        fnet = c["pins"][far]["net"]
        k = ctx.kinds[fnet]
        if fnet == net:
            node["shunts"].append((r, near, far, "self"))
        elif k in ("gnd", "rail"):
            node["shunts"].append((r, near, far, k))
        elif depth < 4 and fnet not in seen:
            node["series"].append((r, near, far, build_tree(ctx, fnet, seen, depth + 1)))
        else:
            node["series"].append((r, near, far, {"net": fnet, "shunts": [], "series": [], "stub": True}))
    return node


def lay_tree(ctx, node):
    """Draw a node with its trunk starting at (0, 0) going +x. Returns (Draw, up, down, width)."""
    d = Draw()
    lib, net = ctx.lib, node["net"]
    ext = net in ctx.external
    up, down = 1.0, 0.0
    x = 0
    if not ext:                                   # local label slot on the trunk start
        L = slot_len(net)
        d.slots.append({"net": net, "x": 0, "y": 0, "dir": 1, "kind": "local", "len": L})
        d.rects.append((1.0, -1.3, L - 0.3, -0.3, "slot"))
        d.rects.append((L - 0.1, -0.1, L + 0.1, 0.1, "anchor"))          # no other net may touch the label point
        x = L
    else:
        x = 1
    prev_t = None
    last = x
    for r, near, far, k in node["shunts"]:
        c = ctx.comps[r]
        fnet = c["pins"][far]["net"]
        pn = fnet if k in ("gnd", "rail") else ""
        gap = 1 + tw(pn) / 2.0
        if prev_t is not None:
            gap = max(prev_t, (tw(pn) + prev_pn) / 2.0 + 1)
        sx = ceil(x + gap) if prev_t is None else ceil(last + gap)
        if k == "rail":
            put_passive(d, lib, c, "v", sx, -2, far, near, ctx.M)
            put_power(d, lib, fnet, "rail", sx, -4, True)
            up = max(up, 8.5)
        elif k == "gnd":
            put_passive(d, lib, c, "v", sx, 2, near, far, ctx.M)
            put_power(d, lib, fnet, "gnd", sx, 4, False)
            down = max(down, 8.5)
        else:                                     # both pads on this net: drawn down, far pad tied back by a wire
            put_passive(d, lib, c, "v", sx, 2, near, far, ctx.M)
            d.wires.append([sx, 4, sx + 1, 4, net])
            d.wires.append([sx + 1, 4, sx + 1, 0, net])
            down = max(down, 5)
            sx += 1
        prev_t = 2 + max(tw(c["ref"]), tw(c["value"]))
        prev_pn = tw(pn)
        last = sx
    x = max(x, last)
    rows = [("series", s) for s in node["series"]]
    if ext:
        rows.append(("global", None))
    if not rows:
        if x > 0:
            d.wires.append([0, 0, x, 0, net])
        return d, up, down, x
    bx = ceil(max(x + 1, last + prev_t)) if node["shunts"] else x
    d.wires.append([0, 0, bx, 0, net])
    y = 0
    width = bx
    prev_down = 0.0
    first = True
    for kind, item in rows:
        if kind == "global":
            sub, su, sd, sw = Draw(), 1.0, 1.0, 1 + tw(net) + 3
            sub.wires.append([0, 0, 1, 0, net])
            sub.labels.append({"kind": "global", "net": net, "x": 1, "y": 0, "dir": 1})
            sub.rects.append((1, -0.8, 1 + tw(net) + 2.5, 0.8, "label"))
        else:
            r, near, far, child = item
            c = ctx.comps[r]
            sw = max(4, 2 * ceil((max(tw(c["ref"]), tw(c["value"])) + 1) / 2.0))
            sub = Draw()
            px = 1 + (sw - 4) // 2
            sub.wires.append([0, 0, px, 0, net])
            put_passive(sub, lib, c, "h", px + 2, 0, near, far, ctx.M)
            if px + 4 < sw + 1:
                sub.wires.append([px + 4, 0, sw + 1, 0, c["pins"][far]["net"]])
            cd, cu, cdn, cw = lay_tree(ctx, child)
            sub.add(cd, sw + 1, 0)
            su, sd = max(2.2, cu), max(2.2, cdn)
            sw = sw + 1 + cw
        if not first:
            y = ceil(y + prev_down + su + 1)
            d.wires.append([bx, y - (y - prev_y), bx, y, net])
        else:
            up = max(up, su)
        prev_y = y
        d.add(sub, bx, y)
        width = max(width, bx + sw)
        prev_down = sd
        first = False
    down = max(down, y + prev_down)
    return d, up, down, width


# ------------------------------------------------------------------ cells
def build_cell(ctx, ref, NCm):
    """A box symbol for `ref` with fan trees on its side pins. Returns a Draw with the symbol's top-left at (0, 0)."""
    c = ctx.comps[ref]
    lib = ctx.lib
    groups = defaultdict(list)
    for p, i in c["pins"].items():
        groups[i["net"]].append(p)
    side, top, bot = [], [], []
    for net, pads in groups.items():
        pads = sorted(pads, key=I.natkey)
        if len(pads) == 1:
            name = c["pins"][pads[0]]["name"] or net
        else:
            name = "%s x%d" % (net, len(pads))
        item = {"net": net, "pads": pads, "name": name, "key": I.natkey(pads[0])}
        k = ctx.kinds[net]
        (bot if k == "gnd" else top if k == "rail" else side).append(item)
    for pad, name in c["nc"]:
        side.append({"net": None, "pads": [pad], "name": name or pad, "key": I.natkey(pad)})
    side.sort(key=lambda i: i["key"])
    top.sort(key=lambda i: i["key"])
    bot.sort(key=lambda i: i["key"])
    # trees
    for it in side:
        if it["net"] is None:
            it.update(tree=None, up=1.0, down=0.0, w=0)
            continue
        node = build_tree(ctx, it["net"], frozenset())
        td, u, dn, w = lay_tree(ctx, node)
        it.update(tree=td, up=u, down=dn, w=w)
    # split sides: contiguous, balanced by height
    hs = [max(2, i["up"] + i["down"] + 1) for i in side]
    tot = sum(hs)
    best, cut = None, 0
    for k in range(len(side) + 1):
        if len(side) >= 2 and k in (0, len(side)):
            continue
        v = abs(2 * sum(hs[:k]) - tot)
        if best is None or v < best:
            best, cut = v, k
    left, right = side[:cut], side[cut:]
    lw = max([tw(i["name"]) for i in left] + [0])
    rw = max([tw(i["name"]) for i in right] + [0])
    cw = max(tw(c["ref"]), tw(c["value"]))
    tsp = [max(2, ceil(tw(i["net"]) + 1)) for i in top]
    bsp = [max(2, ceil(tw(i["net"]) + 1)) for i in bot]
    span = max(sum(tsp), sum(bsp), ceil(cw) + 1)
    x0c = ceil(lw + 1.5) if left else 1
    W = x0c + span + (ceil(rw + 1.5) if right else 1)
    topzone = max([tw(i["name"]) for i in top] + [0]) + (1 if top else 0)
    botzone = max([tw(i["name"]) for i in bot] + [0]) + (1 if bot else 0)

    def rows(items, start):
        ys, y = [], start
        for k, it in enumerate(items):
            if k:
                y = ceil(y + max(2, items[k - 1]["down"] + it["up"] + 1))
            ys.append(y)
        return ys, (y if items else start)
    ly, lend = rows(left, 1)
    ry, rend = rows(right, 1)
    H = max(lend, rend) + 1
    H = max(H, ceil(topzone + botzone + 3.5), 2)
    # symbol
    lid = "fluxplace:B_" + re.sub(r"[^A-Za-z0-9_]", "_", ref)
    pins_l = []
    d = Draw()
    spins = []
    for it, y in zip(left, ly):
        it.update(px=-2, py=y, ang=0)
    for it, y in zip(right, ry):
        it.update(px=W + 2, py=y, ang=180)
    x = x0c
    for it, s in zip(top, tsp):
        it.update(px=x + s // 2, py=-2, ang=270)
        x += s
    x = x0c
    for it, s in zip(bot, bsp):
        it.update(px=x + s // 2, py=H + 2, ang=90)
        x += s
    for it in left + right + top + bot:
        for k, p in enumerate(it["pads"]):
            pins_l.append(_pin(p, it["name"], it["px"], -it["py"], it["ang"], 2, hide=k > 0))
            spins.append((it["px"], it["py"], it["net"]))
    ty = topzone + 1.0
    body = ['    (symbol "%s" (pin_names (offset 0.508)) (in_bom yes) (on_board yes)' % lid,
            _prop("Reference", "U", 0, 0), _prop("Value", "", 0, 0), _prop("Footprint", "", 0, 0, True),
            '      (symbol "%s_0_1"' % lid.split(":")[1],
            "        (rectangle (start 0 0) (end %s %s) (stroke (width 0.254) (type default)) (fill (type background)))"
            % (mm(W), mm(-H)), "      )", '      (symbol "%s_1_1"' % lid.split(":")[1]] + pins_l + ["      )", "    )"]
    lib.syms[lid] = "\n".join(body)
    cx = x0c + span / 2.0
    d.syms.append({"lib": lid, "ref": ref, "value": c["value"], "fp": c["fp"], "x": 0, "y": 0, "kind": "part",
                   "fields": [("Reference", ref, cx, ty, "", False), ("Value", c["value"], cx, ty + 1.3, "", False),
                              ("Footprint", c["fp"], cx, ty, "", True)],
                   "pins": [p for p in spins if p[2] is not None], "dnp": c["dnp"]})
    d.rects.append((0, 0, W, H, "body"))
    for px, py, net in spins:
        if net is not None:
            d.pts.append((px, py, net))
    for it in left + right:          # pin lines + number text are not routable
        x1, x2 = (-2, 0) if it["ang"] == 0 else (W, W + 2)
        d.rects.append((min(x1, x2) + 0.3, it["py"] - 0.9, max(x1, x2) - 0.3, it["py"] + 0.3, "pin"))
    for it in top + bot:
        y1, y2 = (-2, 0) if it["ang"] == 270 else (H, H + 2)
        d.rects.append((it["px"] - 0.3, min(y1, y2) + 0.3, it["px"] + 0.3, max(y1, y2) - 0.3, "pin"))
    # NC flags, power symbols, trees
    for it in left + right:
        if it["net"] is None:
            d.ncs.append((it["px"], it["py"]))
            continue
        t = it["tree"]
        if it["ang"] == 0:
            t.mirror()
        d.add(t, it["px"], it["py"])
    for it in top:
        put_power(d, lib, it["net"], "rail", it["px"], it["py"], True)
    for it in bot:
        put_power(d, lib, it["net"], "gnd", it["px"], it["py"], False)
    return d


def lay_loose(ctx, r):
    """A passive no tree took: decoupling / rail-to-rail drawn vertical, anything with a signal end horizontal."""
    c = ctx.comps[r]
    d = Draw()
    pads = sorted(c["pins"], key=I.natkey)
    if len(pads) != 2:
        return None
    a, b = pads
    ka, kb = ctx.kinds[c["pins"][a]["net"]], ctx.kinds[c["pins"][b]["net"]]
    na, nb = c["pins"][a]["net"], c["pins"][b]["net"]
    if ka != "sig" and kb != "sig":
        if ka == "gnd" and kb != "gnd":
            a, b, ka, kb, na, nb = b, a, kb, ka, nb, na
        put_passive(d, ctx.lib, c, "v", 0, 0, a, b, ctx.M)
        put_power(d, ctx.lib, na, ka, 0, -2, True)
        put_power(d, ctx.lib, nb, kb, 0, 2, False)
        return d
    if ka != "sig":
        a, b, ka, kb, na, nb = b, a, kb, ka, nb, na
    # a is a signal end, drawn left with its slot
    La = slot_len(na) if na not in ctx.external else 1
    e = max(0, ceil(max(tw(c["ref"]), tw(c["value"])) / 2.0 + 0.5) - 2)    # long text: stretch the leads
    if e:
        d.wires.append([La, 0, La + e, 0, na])
    put_passive(d, ctx.lib, c, "h", La + e + 2, 0, a, b, ctx.M)
    _slot(d, na, La, 0, -1, La, ctx)
    x2 = La + e + 4
    if kb == "sig":
        if e:
            d.wires.append([x2, 0, x2 + e, 0, nb])
        Lb = slot_len(nb) if nb not in ctx.external else 1
        _slot(d, nb, x2 + e, 0, 1, Lb, ctx)
    else:
        d.wires.append([x2, 0, x2 + e + 1, 0, nb])
        put_power(d, ctx.lib, nb, kb, x2 + e + 1, 0, kb == "rail")
    return d


def _slot(d, net, x, y, direction, L, ctx):
    """A stub from a pin end (x, y) outward with the net's label slot (local) or a global label at its end."""
    if net in ctx.external:
        d.wires.append([x, y, x + direction * L, y, net])
        d.labels.append({"kind": "global", "net": net, "x": x + direction * L, "y": y, "dir": direction})
        ex = x + direction * L
        w = tw(net) + 2.5
        d.rects.append((min(ex, ex + direction * w), y - 0.8, max(ex, ex + direction * w), y + 0.8, "label"))
    else:
        d.wires.append([x, y, x + direction * L, y, net])
        d.slots.append({"net": net, "x": x, "y": y, "dir": direction, "kind": "local", "len": L})
        d.rects.append((min(x + direction, x + direction * L) + 0.3, y - 1.3, max(x + direction, x + direction * L) - 0.3, y - 0.3, "slot"))
        ex = x + direction * L
        d.rects.append((ex - 0.1, y - 0.1, ex + 0.1, y + 0.1, "anchor"))


# ------------------------------------------------------------------ block sheet
def layout_block(M, kinds, block, lib, cons=None):
    refs = block["refs"]
    rset = set(refs)
    external = set()
    for r in refs:
        for p, i in M["comps"][r]["pins"].items():
            if any(q not in rset for q, _ in M["nets"][i["net"]]):
                external.add(i["net"])
    ctx = Ctx(M, kinds, refs, lib, external)
    comps = M["comps"]
    cells = [r for r in refs if not I.two_pin(comps[r])]
    # order: anchor first, then by signal nets shared with what is already placed
    sig_of = {r: {i["net"] for i in comps[r]["pins"].values() if kinds[i["net"]] == "sig"} for r in refs}
    order = [block["anchor"]] if block["anchor"] in cells else []
    rest = [r for r in sorted(cells, key=lambda r: (-len(comps[r]["pins"]), I.natkey(r))) if r not in order]
    while rest:
        have = set().union(*[sig_of[r] for r in order]) if order else set()
        nb = set()
        for r in order:
            for n in sig_of[r]:
                nb |= {q for q in ctx.by_net[n] if q in ctx.pool}
                for q in ctx.by_net[n]:
                    if q in ctx.pool:
                        nb |= {x for m in sig_of[q] for x in ctx.by_net[m]}
        r = max(rest, key=lambda r: (len(sig_of[r] & have) + (2 if r in nb else 0), len(comps[r]["pins"]), [-ord(ch) for ch in r]))
        order.append(r)
        rest.remove(r)
    built = {}
    for r in order:
        built[r] = build_cell(ctx, r, None)
    # loose passives join the cell they share most nets with (ownership by signal, then rail)
    loose = defaultdict(list)
    for r in sorted(ctx.pool, key=I.natkey):
        best, bs = None, -1
        nets = [i["net"] for i in comps[r]["pins"].values()]
        for q in order:
            qn = {i["net"] for i in comps[q]["pins"].values()}
            s = sum(3 if kinds[n] == "sig" else (1 if kinds[n] == "rail" else 0) for n in nets if n in qn)
            if s > bs:
                best, bs = q, s
        loose[best].append(r)
    units = []
    for r in order or [None]:
        d = built.get(r) or Draw()
        items = [lay_loose(ctx, q) for q in sorted(loose.get(r, []),
                                                   key=lambda q: (kinds[comps[q]["pins"][sorted(comps[q]["pins"], key=I.natkey)[0]]["net"]] == "sig",
                                                                  I.natkey(q)))]
        items = [i for i in items if i]
        if items:
            x0, y0, x1, y1 = d.bbox() if (d.syms or d.wires) else (0, 0, 0, 0)
            rowmax = max(x1 - x0, 40)
            cx, cy, rh = x0, y1 + 4, 0
            for it in items:
                bx0, by0, bx1, by1 = it.bbox()
                w = bx1 - bx0
                if cx > x0 and cx + w > x0 + rowmax:
                    cx, cy, rh = x0, cy + rh + 3, 0
                d.add(it, ceil(cx - bx0), ceil(cy - by0))
                cx = ceil(cx + w + 3)
                rh = max(rh, by1 - by0)
        units.append(d)
    # pack: first row holds the anchor in the middle
    boxes = [u.bbox() for u in units]
    area = sum((b[2] - b[0] + 10) * (b[3] - b[1] + 8) for b in boxes)
    maxw = max([b[2] - b[0] for b in boxes] + [0])
    rowW = max(maxw, (area ** 0.5) * 1.3)
    rows, cur, cw = [], [], 0
    for k, b in enumerate(boxes):
        w = b[2] - b[0] + 10
        if cur and cw + w > rowW:
            rows.append(cur)
            cur, cw = [], 0
        cur.append(k)
        cw += w
    if cur:
        rows.append(cur)
    if rows and len(rows[0]) > 2:              # anchor (index 0) to the middle of its row
        r0 = rows[0][1:]
        mid = len(r0) // 2
        rows[0] = r0[:mid] + [0] + r0[mid:]
    sheet = Draw()
    y = 0
    widths = [sum(boxes[k][2] - boxes[k][0] + 10 for k in row) - 10 for row in rows]
    W = max(widths + [0])
    for row, rw in zip(rows, widths):
        x = ceil((W - rw) / 2.0)
        h = 0
        for k in row:
            b = boxes[k]
            sheet.add(units[k], ceil(x - b[0]), ceil(y - b[1]))
            x = ceil(x + b[2] - b[0] + 10)
            h = max(h, b[3] - b[1])
        y = ceil(y + h + 8)
    route_block(sheet, kinds)
    finish_labels(sheet, kinds, external)
    split_and_junction(sheet)
    return sheet, external


# ------------------------------------------------------------------ connectivity helpers
class UF:
    def __init__(self):
        self.p = {}

    def f(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def u(self, a, b):
        self.p[self.f(a)] = self.f(b)


def _seg_cells(w):
    x1, y1, x2, y2 = w[:4]
    if x1 == x2:
        for y in range(min(y1, y2), max(y1, y2) + 1):
            yield (x1, y)
    else:
        for x in range(min(x1, x2), max(x1, x2) + 1):
            yield (x, y1)


def pieces(sheet, net):
    """Connected pieces of a net in the sheet (wires + pin points), as lists of cells."""
    uf = UF()
    cells = set()
    for w in sheet.wires:
        if w[4] != net:
            continue
        cs = list(_seg_cells(w))
        for c in cs:
            uf.u(cs[0], c)
            cells.add(c)
    for x, y, n in sheet.pts:
        if n == net:
            uf.f((x, y))
            cells.add((x, y))
    out = defaultdict(list)
    for c in cells:
        out[uf.f(c)].append(c)
    return list(out.values())


# ------------------------------------------------------------------ router
def route_block(sheet, kinds, max_len=70, budget=30000):
    """Join the pieces of every internal signal net with orthogonal wires (A* on the grid). A wire never runs along
    another net's wire, never bends or ends on one (a plain crossing is allowed and costs extra), never enters a body,
    label, text or another net's connection point; cells next to an obstacle cost extra so wires keep a grid of air.
    A join longer than min(max_len, 1.6 x distance + 12) is not drawn: that piece keeps its own label."""
    blocked = set()
    for a, b, c, d, k in sheet.rects:
        for x in range(int(-(-a // 1)), int(c // 1) + 1):
            for y in range(int(-(-b // 1)), int(d // 1) + 1):
                blocked.add((x, y))
    for x, y in sheet.ncs:
        blocked.add((x, y))
    near = set()
    for x, y in blocked:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                near.add((x + dx, y + dy))
    H, V = {}, {}            # cell -> net of a horizontal / vertical wire through it
    P = {}                   # connection points (wire ends, pins, label anchors) -> net
    for w in sheet.wires:
        x1, y1, x2, y2, n = w
        for c in _seg_cells(w):
            (V if x1 == x2 else H)[c] = n
        P[(x1, y1)] = n
        P[(x2, y2)] = n
    for x, y, n in sheet.pts:
        P[(x, y)] = n
    for l in sheet.labels:
        P[(l["x"], l["y"])] = l["net"]
    x0, y0, x1, y1 = sheet.bbox()
    X0, Y0, X1, Y1 = x0 - 6, y0 - 6, x1 + 6, y1 + 6
    nets = sorted({w[4] for w in sheet.wires} | {n for _x, _y, n in sheet.pts})
    jobs = []
    for n in nets:
        if kinds.get(n) != "sig":
            continue
        ps = pieces(sheet, n)
        if len(ps) > 1:
            jobs.append((n, ps))

    def bb(cells):
        xs = [c[0] for c in cells]
        ys = [c[1] for c in cells]
        return min(xs), min(ys), max(xs), max(ys)

    def bdist(c, b):
        return max(b[0] - c[0], 0, c[0] - b[2]) + max(b[1] - c[1], 0, c[1] - b[3])

    def spread(ps):
        bs = [bb(p) for p in ps]
        return max(bdist((b[0], b[1]), o) for b in bs for o in bs)
    jobs.sort(key=lambda j: (spread(j[1]), j[0]))
    DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    routed, failed = 0, 0

    def free_of_foreign(c, net):
        return H.get(c, net) == net and V.get(c, net) == net

    for net, ps in jobs:
        ps = sorted(ps, key=len, reverse=True)
        tree = set(ps[0])
        pending = list(ps[1:])
        while pending:
            tb = bb(tree)
            pending.sort(key=lambda p: min(bdist(c, tb) for c in p))
            piece = pending.pop(0)
            src = set(piece)
            dist = min(bdist(c, tb) for c in src)
            limit = min(max_len, 1.6 * dist + 12)
            heap = []
            for c in src:
                if (P.get(c) == net or c in H or c in V) and free_of_foreign(c, net):
                    for k in range(4):
                        heap.append((bdist(c, tb), 0, c, k, None))
            if not heap:
                failed += 1
                continue
            heapq.heapify(heap)
            best, prev, goal, pops = {}, {}, None, 0
            while heap and pops < budget:
                f, g, c, k, par = heapq.heappop(heap)
                key = (c, k)
                if key in best and best[key] <= g:
                    continue
                best[key] = g
                prev[key] = par
                pops += 1
                if c in tree and par is not None:
                    goal = key
                    break
                if g > limit:
                    continue
                for k2, (dx, dy) in enumerate(DIRS):
                    n2 = (c[0] + dx, c[1] + dy)
                    if not (X0 <= n2[0] <= X1 and Y0 <= n2[1] <= Y1) or n2 in src:
                        continue
                    if par is not None and (k2 ^ 1) == k:
                        continue                       # no U-turn
                    bend = par is not None and k2 != k
                    if bend and not free_of_foreign(c, net):
                        continue                       # a bend on another net's wire would join it
                    if n2 in blocked and n2 not in tree:
                        continue
                    pn = P.get(n2)
                    if pn is not None and pn != net:
                        continue
                    horiz = dy == 0
                    if (H if horiz else V).get(n2, net) != net:
                        continue                       # would run along another net's wire
                    if n2 in tree:
                        if (H if horiz else V).get(n2) == net and P.get(n2) != net:
                            continue                   # would run along an own wire
                        if not free_of_foreign(n2, net):
                            continue                   # ending on a crossing would join the crossing wire
                    cross = (V if horiz else H).get(n2) not in (None, net)
                    g2 = g + 1 + (3 if bend else 0) + (6 if cross else 0) + (1.5 if n2 in near and n2 not in tree else 0)
                    if (n2, k2) in best and best[(n2, k2)] <= g2:
                        continue
                    heapq.heappush(heap, (g2 + bdist(n2, tb), g2, n2, k2, key))
            if goal is None:
                failed += 1
                continue
            path = []
            key = goal
            while key is not None:
                path.append(key[0])
                key = prev[key]
            path.reverse()
            segs = []
            a = path[0]
            for i in range(1, len(path)):
                last = i == len(path) - 1
                if last or (path[i + 1][0] - path[i][0], path[i + 1][1] - path[i][1]) != \
                        (path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1]):
                    segs.append([a[0], a[1], path[i][0], path[i][1], net])
                    a = path[i]
            for s2 in segs:
                sheet.wires.append(s2)
                for c in _seg_cells(s2):
                    (V if s2[0] == s2[2] else H)[c] = net
                    tree.add(c)
                P[(s2[0], s2[1])] = net
                P[(s2[2], s2[3])] = net
            tree |= src
            routed += 1
    sheet.route_stats = {"joined": routed, "unjoined": failed}


def finish_labels(sheet, kinds, external):
    """One local label per piece of each internal signal net, on that piece's slot; a net that leaves the block keeps one
    global label per piece (the router may have joined pieces that each had one; the extra stubs are trimmed later)."""
    glob = defaultdict(list)
    for l in sheet.labels:
        if l["kind"] == "global":
            glob[l["net"]].append(l)
    drop = set()
    for net, ls in glob.items():
        if len(ls) < 2:
            continue
        for piece in pieces(sheet, net):
            cs = set(piece)
            mine = sorted((l for l in ls if (l["x"], l["y"]) in cs), key=lambda l: (l["y"], l["x"]))
            drop |= {id(l) for l in mine[1:]}
    sheet.labels = [l for l in sheet.labels if id(l) not in drop]
    by_net = defaultdict(list)
    for s in sheet.slots:
        by_net[s["net"]].append(s)
    for net, slots in sorted(by_net.items()):
        if net in external or kinds.get(net) != "sig":
            continue
        for piece in pieces(sheet, net):
            cs = set(piece)
            mine = [s for s in slots if (s["x"], s["y"]) in cs]
            if not mine:
                continue
            s = sorted(mine, key=lambda s: (s["y"], s["x"]))[0]
            # at the slot's outer end, reading back toward the pin, so the wire runs under the whole text
            sheet.labels.append({"kind": "local", "net": net, "x": s["x"] + s["dir"] * s["len"], "y": s["y"], "dir": -s["dir"]})


def split_and_junction(sheet):
    pts = defaultdict(set)
    for w in sheet.wires:
        pts[w[4]].add((w[0], w[1]))
        pts[w[4]].add((w[2], w[3]))
    for x, y, n in sheet.pts:
        pts[n].add((x, y))
    for l in sheet.labels:
        pts[l["net"]].add((l["x"], l["y"]))
    out = []
    for w in sheet.wires:
        x1, y1, x2, y2, n = w
        if (x1, y1) == (x2, y2):
            continue
        inner = sorted(p for p in pts[n] if p != (x1, y1) and p != (x2, y2) and
                       ((x1 == x2 == p[0] and min(y1, y2) < p[1] < max(y1, y2)) or
                        (y1 == y2 == p[1] and min(x1, x2) < p[0] < max(x1, x2))))
        seq = [(x1, y1)] + sorted(inner, key=lambda p: abs(p[0] - x1) + abs(p[1] - y1)) + [(x2, y2)]
        for a, b in zip(seq, seq[1:]):
            out.append([a[0], a[1], b[0], b[1], n])
    # dedupe overlapping identical segments
    seen, ded = set(), []
    for w in out:
        k = (min((w[0], w[1]), (w[2], w[3])), max((w[0], w[1]), (w[2], w[3])), w[4])
        if k not in seen:
            seen.add(k)
            ded.append(w)
    # trim dangling ends (no pin / label / power / other wire there)
    anchors = defaultdict(int)
    for x, y, n in sheet.pts:
        anchors[(x, y, n)] += 1
    for l in sheet.labels:
        anchors[(l["x"], l["y"], l["net"])] += 1
    changed = True
    while changed:
        changed = False
        deg = defaultdict(int)
        for w in ded:
            deg[(w[0], w[1], w[4])] += 1
            deg[(w[2], w[3], w[4])] += 1
        keep = []
        for w in ded:
            ea, eb = (w[0], w[1], w[4]), (w[2], w[3], w[4])
            if (deg[ea] == 1 and not anchors[ea]) or (deg[eb] == 1 and not anchors[eb]):
                changed = True
                continue
            keep.append(w)
        ded = keep
    sheet.wires = ded
    deg = defaultdict(int)
    for w in ded:
        deg[(w[0], w[1], w[4])] += 1
        deg[(w[2], w[3], w[4])] += 1
    for x, y, n in set(sheet.pts):             # stacked pins / a power symbol on a pin count once
        deg[(x, y, n)] += 1
    sheet.junctions = sorted({(x, y) for (x, y, n), k in deg.items() if k >= 3})


def conflicts(sheet):
    """Self-check: grid points where two nets touch (a wire end, pin, label or power anchor of one net on any item of
    another). A plain crossing of two wire interiors is not a conflict. Returns [(x, y, nets)]."""
    own = defaultdict(set)
    ends = set()
    for w in sheet.wires:
        ends.add((w[0], w[1], w[4]))
        ends.add((w[2], w[3], w[4]))
        for c in _seg_cells(w):
            own[c].add(w[4])
    anchors = defaultdict(set)
    for x, y, n in sheet.pts:
        anchors[(x, y)].add(n)
    for l in sheet.labels:
        anchors[(l["x"], l["y"])].add(l["net"])
    out = []
    for c in set(own) | set(anchors):
        nets = own.get(c, set()) | anchors.get(c, set())
        if len(nets) < 2:
            continue
        if not anchors.get(c) and not any((c[0], c[1], n) in ends for n in nets):
            continue
        out.append((c[0], c[1], sorted(nets)))
    return sorted(out)


# ------------------------------------------------------------------ emit
def emit_sheet(path, sheet, lib, title, project, inst_path, comment, paper_pad=(12, 12, 12, 40), offset=None):
    x0, y0, x1, y1 = sheet.bbox()
    for l in sheet.labels:
        w = tw(l["net"]) + 3
        x0, x1 = min(x0, l["x"] - w), max(x1, l["x"] + w)
    ml, mt, mr, mb = paper_pad
    dx, dy = offset if offset else (ceil(ml - x0), ceil(mt - y0))
    if offset:
        x0, y0 = min(x0, 0), min(y0, 0)
    Wg = x1 - x0 + ml + mr
    Hg = y1 - y0 + mt + mb
    Wmm, Hmm = max(297.0, Wg * G), max(210.0, Hg * G)
    used = {s["lib"] for s in sheet.syms}
    L = ['(kicad_sch (version 20231120) (generator "fluxplace")',
         '  (uuid "%s")' % _uuid("sheet:" + path + title),
         '  (paper "User" %.2f %.2f)' % (Wmm, Hmm),
         '  (title_block (title "%s") (comment 1 "%s"))' % (_esc(title), _esc(comment)),
         "  (lib_symbols"] + lib.text(used) + ["  )"]
    for j, (x, y) in enumerate(sheet.junctions):
        L.append('  (junction (at %s %s) (diameter 0) (color 0 0 0 0) (uuid "%s"))' % (mm(x + dx), mm(y + dy), _uuid("j%s%d%d" % (title, x, y))))
    for x, y in sheet.ncs:
        L.append('  (no_connect (at %s %s) (uuid "%s"))' % (mm(x + dx), mm(y + dy), _uuid("nc%s%d%d" % (title, x, y))))
    for k, w in enumerate(sheet.wires):
        L.append('  (wire (pts (xy %s %s) (xy %s %s)) (stroke (width 0) (type default)) (uuid "%s"))'
                 % (mm(w[0] + dx), mm(w[1] + dy), mm(w[2] + dx), mm(w[3] + dy), _uuid("w%s%d%s" % (title, k, w))))
    for k, l in enumerate(sheet.labels):
        u = _uuid("l%s%d%s" % (title, k, l["net"]))
        if l["kind"] == "local":
            ang = 0 if l["dir"] > 0 else 180
            L.append('  (label "%s" (at %s %s %d) %s (uuid "%s"))' % (_esc(l["net"]), mm(l["x"] + dx), mm(l["y"] + dy), ang,
                                                                    FONT % (" (justify left bottom)" if ang == 0 else " (justify right bottom)"), u))
        else:
            ang = 0 if l["dir"] > 0 else 180
            L.append('  (global_label "%s" (shape bidirectional) (at %s %s %d) %s (uuid "%s")'
                     % (_esc(l["net"]), mm(l["x"] + dx), mm(l["y"] + dy), ang,
                        FONT % (" (justify left)" if ang == 0 else " (justify right)"), u))
            L.append('    (property "Intersheetrefs" "${INTERSHEET_REFS}" (at %s %s 0) %s))'
                     % (mm(l["x"] + dx), mm(l["y"] + dy), FONT % " hide"))
    for s in sheet.syms:
        u = _uuid("s%s%s%s%s" % (title, s["ref"], s.get("net", ""), (s["x"], s["y"])))
        L.append('  (symbol (lib_id "%s") (at %s %s 0) (unit 1) (in_bom yes) (on_board yes)%s (uuid "%s")'
                 % (s["lib"], mm(s["x"] + dx), mm(s["y"] + dy), " (dnp yes)" if s.get("dnp") else "", u))
        fields = list(s["fields"])
        if s["kind"] == "pwr" or s["kind"] == "flag":
            fields = [("Reference", s["ref_i"], s["x"] + dx * 0, s["y"], "", True)] + fields
        for name, val, fx, fy, just, hide in fields:
            if s["kind"] in ("pwr", "flag") and name == "Reference":
                fx, fy = s["x"], s["y"]
            L.append('    (property "%s" "%s" (at %s %s 0) %s)'
                     % (name, _esc(val), mm(fx + dx), mm(fy + dy),
                        FONT % ((" (justify %s)" % just if just else "") + (" hide" if hide else ""))))
        ref = s["ref_i"] if s["kind"] in ("pwr", "flag") else s["ref"]
        L.append('    (instances (project "%s" (path "%s" (reference "%s") (unit 1))))' % (_esc(project), inst_path, _esc(ref)))
        L.append("  )")
    return L, (Wmm, Hmm), (dx, dy)
