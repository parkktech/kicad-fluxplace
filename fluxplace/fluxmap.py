"""The meta map: one compact, deterministic JSON that stands in for the board file.

`fluxplace map BOARD --constraints C` writes `<board>.fluxmap.json` (schema: docs/fluxmap.md).  Agents read
slices of it (`map --show SECTION --ref R`) instead of the board; the planner (plan-route) fills the
`escapes`, `grid`, `route`, `channels` and `verdict` sections.  Coordinates are mm rounded to 0.01, key order
is fixed, lists are sorted: the same board and constraints give a byte-identical file.
"""
import json
import math
import os
import re
from collections import Counter, defaultdict

from . import anchors as AN
from . import clusters as CL
from . import escapeplan as EP
from . import geom as GM
from . import netclass as NC
from . import placechecks as PCK
from . import preroute as PR

VERSION = 1
SECTIONS = ["board", "parts", "relations", "groups", "nets", "escapes", "grid", "route", "channels", "verdict"]
_KIND = [("Y", "crystal"), ("X", "crystal"), ("L", "inductor"), ("FB", "ferrite"), ("D", "esd/diode"),
         ("C", "cap"), ("R", "res")]


def r2(v):
    return round(float(v) + 0.0, 2)


def _outline(B):
    """Edge.Cuts segments chained into one polygon (bbox rectangle when there are none)."""
    edge = B.get("edge") or []
    x0, y0, x1, y1 = B["bbox"]
    if not edge:
        return [[r2(x0), r2(y0)], [r2(x1), r2(y0)], [r2(x1), r2(y1)], [r2(x0), r2(y1)]]
    segs = [(tuple(a), tuple(b)) for a, b in edge]
    tol = 0.02
    used = [False] * len(segs)
    poly = [segs[0][0], segs[0][1]]
    used[0] = True
    for _ in range(len(segs)):
        end = poly[-1]
        nxt = None
        for i, (a, b) in enumerate(segs):
            if used[i]:
                continue
            if math.hypot(a[0] - end[0], a[1] - end[1]) < tol:
                nxt = (i, b)
                break
            if math.hypot(b[0] - end[0], b[1] - end[1]) < tol:
                nxt = (i, a)
                break
        if not nxt:
            break
        used[nxt[0]] = True
        poly.append(nxt[1])
    pts = []
    for p in poly:
        q = [r2(p[0]), r2(p[1])]
        if not pts or q != pts[-1]:
            pts.append(q)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    # drop collinear arc chords' noise: keep points that bend by more than 1 degree
    out = []
    for i, p in enumerate(pts):
        a, b = pts[i - 1], pts[(i + 1) % len(pts)]
        v1, v2 = (p[0] - a[0], p[1] - a[1]), (b[0] - p[0], b[1] - p[1])
        cr = v1[0] * v2[1] - v1[1] * v2[0]
        n = math.hypot(*v1) * math.hypot(*v2)
        if n == 0 or abs(cr) / n > 0.017:
            out.append(p)
    return out or pts


def layer_roles(B, cons):
    sig, planes = PR.layer_split(B, cons)
    out = []
    for l in B["copper"]:
        pour = sorted({z["net"] for z in B["zones"] if z["net"] and not z["keepout"] and l in z["layers"]})
        if l in planes:
            role, net = "plane", (pour[0] if pour else "")
        elif pour and l in sig:
            role, net = "mixed", pour[0] if len(pour) == 1 else "/".join(pour)
        else:
            role, net = "signal", ""
        out.append({"name": l, "role": role, "net": net})
    return out


def _kind(ref, cls):
    for pre, k in _KIND:
        if re.match(rf"^{pre}\d", ref):
            if k == "cap" and cls in ("bypass", "rail"):
                return "decap"
            return k
    return "support"


def build(B, cons=None, spec=None, board_path=None, cls=None, state=None):
    """The map dict without the planner's sections."""
    cons = cons or {}
    cls = cls if cls is not None else NC.classify(B, cons, spec)
    pr = EP.plan_params(cons)
    pl = cons.get("placement") or {}
    parts = B["parts"]
    fixed = PR.fixed_parts(B, cons)
    anchors = {r for a in AN.detect(B, cons) for r in a["refs"]}
    expl = PR.explicit_fixed(B, cons)
    heights = pl.get("heights") or {}
    import fnmatch
    # ---- groups
    groups, src_of = {}, {}
    for name, g in sorted((pl.get("group") or {}).items()):
        refs = sorted(r for r in g.get("refs", []) if r in parts)
        if refs:
            groups[name] = {"refs": refs, "src": "placement.group"}
    for name, refs in sorted(PCK.blocks_of(B, spec, cons).items()):
        groups.setdefault(name, {"refs": sorted(refs), "src": "blocks"})
    if not any(g["src"] == "blocks" for g in groups.values()):
        sheets = defaultdict(list)
        for r, p in parts.items():
            if p.get("sheet") and not EP.is_mech(r):
                sheets[p["sheet"].strip("/") or "root"].append(r)
        for name, refs in sorted(sheets.items()):
            if len(refs) >= 2:
                groups.setdefault("sheet:" + name, {"refs": sorted(refs), "src": "sheet"})
    cl, _links = CL.build(B, cons, spec)
    in_group = {}
    for name, g in groups.items():
        for r in g["refs"]:
            in_group.setdefault(r, name)
    # connectivity: an IC with its owned support parts joins the group of the IC, else forms "cl:IC"
    for c in cl:
        gname = in_group.get(c["ic"]) or ("cl:" + c["ic"])
        if gname.startswith("cl:"):
            groups.setdefault(gname, {"refs": [c["ic"]], "src": "cluster"})
            in_group.setdefault(c["ic"], gname)
        for s in c["supports"]:
            if s["ref"] not in in_group:
                in_group[s["ref"]] = gname
                if gname in groups and s["ref"] not in groups[gname]["refs"]:
                    groups[gname]["refs"].append(s["ref"])
    # ---- parts
    plist = []
    for ref in sorted(parts):
        p = parts[ref]
        mode = "anchor" if ref in anchors else ("fixed" if ref in fixed or p.get("locked") else "movable")
        h = next((float(v) for g, v in heights.items() if fnmatch.fnmatch(ref, g)), None)
        c = p["court"]
        plist.append({"ref": ref, "v": p["value"], "fp": p["fp"], "x": r2(p["pos"][0]), "y": r2(p["pos"][1]),
                      "rot": r2(p["angle"]), "side": p["side"][0], "court": [r2(c[0]), r2(c[1]), r2(c[2]), r2(c[3])],
                      "h": h, "pads": p["n_pads"], "mode": mode, "group": in_group.get(ref, ""),
                      "pitch": r2(EP.part_pitch(p)) if EP.part_pitch(p) < 99 else None})
    # ---- relations
    rel = []
    for c in cl:
        for s in c["supports"]:
            rel.append({"part": s["ref"], "kind": _kind(s["ref"], s["cls"]), "serves": f"{c['ic']}.{s['pad']}",
                        "net": s["net"], "max": r2(s["limit"]), "now": r2(s["dist"]), "sure": bool(s["sure"])})
    rel.sort(key=lambda r: (r["serves"], r["part"]))
    # ---- groups (final)
    gl = []
    pads = NC.net_pads(B)
    for name in sorted(groups):
        refs = sorted(set(groups[name]["refs"]))
        cs = [parts[r]["court"] for r in refs]
        box = [min(c[0] for c in cs), min(c[1] for c in cs), max(c[2] for c in cs), max(c[3] for c in cs)]
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        ext = Counter()
        mem = set(refs)
        for n, ps in pads.items():
            if NC.is_ground(n):
                continue
            inside = [p for p in ps if p["ref"] in mem]
            outside = [p for p in ps if p["ref"] not in mem]
            if inside and outside:
                ox = sum(p["x"] for p in outside) / len(outside) - cx
                oy = sum(p["y"] for p in outside) / len(outside) - cy
                ext[("E" if ox > 0 else "W") if abs(ox) >= abs(oy) else ("S" if oy > 0 else "N")] += 1
        anc = [r for r in refs if r in anchors]
        gl.append({"name": name, "src": groups[name]["src"], "members": refs, "anchor": anc[0] if anc else None,
                   "box": [r2(v) for v in box], "area": r2((box[2] - box[0]) * (box[3] - box[1])),
                   "ext_nets": {k: ext[k] for k in "NESW" if ext[k]}, "ext_total": sum(ext.values())})
    # ---- nets
    state = state or {"open": {}, "routed": set()}
    nl = []
    for n in sorted(cls):
        info = cls[n]
        ps = sorted(pads[n], key=lambda p: (p["ref"], p["num"]))
        lim = NC.effective_budget(info)
        if n in state["open"]:
            st = f"open:{state['open'][n]}"
        elif n in state["routed"]:
            st = "routed"
        else:
            st = "unrouted"
        nl.append({"n": n, "c": info["class"], "pads": [[f"{p['ref']}.{p['num']}", r2(p["x"]), r2(p["y"]),
                                                        parts[p["ref"]]["side"][0]] for p in ps],
                   "budget": None if lim is None else r2(lim), "mst": r2(NC.net_length(B, n, ps)),
                   "pair": info.get("partner"), "state": st})
    # ---- board
    ko = []
    for z in B["zones"]:
        if z["keepout"] and z["pts"]:
            xs, ys = [p[0] for p in z["pts"]], [p[1] for p in z["pts"]]
            lays = sorted(z["layers"] & set(B["copper"]))
            side = "all" if len(lays) >= len(B["copper"]) else (
                "F" if lays == [B["copper"][0]] else ("B" if lays == [B["copper"][-1]] else "+".join(l[:-3] for l in lays)))
            ko.append({"name": z.get("name") or z.get("owner") or "", "side": side,
                       "no": [k for k, f in (("tracks", z["no_tracks"]), ("vias", z.get("no_vias"))) if f],
                       "box": [r2(min(xs)), r2(min(ys)), r2(max(xs)), r2(max(ys))]})
    ko.sort(key=lambda k: (k["box"], k["name"]))
    hr = [{"name": k.get("name", ""), "rect": k["rect"], "side": k.get("side", "B"), "max_h": k["max_height_mm"]}
          for k in pl.get("height_region", []) or []]
    board = {"file": os.path.basename(board_path or ""), "outline": _outline(B),
             "bbox": [r2(v) for v in B["bbox"]], "layers": layer_roles(B, cons),
             "floors": {"track": pr["track_mm"], "clearance": pr["clearance_mm"], "via": pr["via_mm"],
                        "drill": pr["drill_mm"], "fab_min_track": ((cons.get("fab") or {}).get("min_track_mm")),
                        "fab_min_clearance": ((cons.get("fab") or {}).get("min_clearance_mm"))},
             "keepouts": ko, "height_regions": hr}
    return {"fluxmap": VERSION, "board": board, "parts": plist, "relations": rel, "groups": gl, "nets": nl,
            "escapes": {}, "grid": {}, "route": {}, "channels": [], "verdict": {}}


def dumps(m):
    return json.dumps(m, separators=(",", ":"), ensure_ascii=True)


def save(m, path):
    with open(path, "w") as fh:
        fh.write(dumps(m))
    return os.path.getsize(path)


def load(path):
    with open(path) as fh:
        return json.load(fh)


def default_path(board):
    return os.path.splitext(board)[0] + ".fluxmap.json"


# ------------------------------------------------------------------ slices
def show(m, section, ref=None, net=None, group=None, limit=6000):
    """A small slice of the map as compact JSON text."""
    if section not in SECTIONS:
        return f"unknown section {section!r}; sections: {', '.join(SECTIONS)}"
    out = m.get(section)
    if section == "parts" and ref:
        out = [p for p in m["parts"] if p["ref"] == ref]
        if out:
            out = {"part": out[0], "relations": [r for r in m["relations"] if r["part"] == ref or r["serves"].startswith(ref + ".")],
                   "escapes": (m.get("escapes") or {}).get("pins", {}).get(ref),
                   "headroom": (m.get("escapes") or {}).get("headroom", {}).get(ref),
                   "nets": sorted({n["n"] for n in m["nets"] if any(p[0].startswith(ref + ".") for p in n["pads"])})}
    elif section == "parts" and group:
        mem = next((g["members"] for g in m["groups"] if g["name"] == group), [])
        out = [p for p in m["parts"] if p["ref"] in mem]
    elif section == "parts":
        out = [{k: p[k] for k in ("ref", "x", "y", "rot", "side", "mode", "group", "pads")} for p in m["parts"]]
    elif section == "relations" and ref:
        out = [r for r in m["relations"] if r["part"] == ref or r["serves"].startswith(ref + ".")]
    elif section == "nets" and net:
        out = [n for n in m["nets"] if n["n"] == net]
        out = {"net": out[0] if out else None, "route": (m.get("route") or {}).get(net),
               "findings": [f for f in (m.get("verdict") or {}).get("findings", [])
                            if net in (f.get("net"), ) or net in f.get("nets", [])]}
    elif section == "nets" and ref:
        out = [n for n in m["nets"] if any(p[0].startswith(ref + ".") for p in n["pads"])]
    elif section == "nets":
        out = [{k: n[k] for k in ("n", "c", "state", "budget", "mst")} for n in m["nets"]]
    elif section == "groups" and group:
        out = [g for g in m["groups"] if g["name"] == group]
    elif section == "groups":
        out = [{k: g[k] for k in ("name", "src", "anchor", "area", "ext_nets")} | {"n": len(g["members"])} for g in m["groups"]]
    elif section == "escapes" and ref:
        e = m.get("escapes") or {}
        out = {"pins": e.get("pins", {}).get(ref), "headroom": e.get("headroom", {}).get(ref)}
    elif section == "escapes":
        e = m.get("escapes") or {}
        out = {"headroom": e.get("headroom"), "legend": e.get("legend"),
               "pins_per_part": {r: len(v) for r, v in (e.get("pins") or {}).items()}}
    elif section == "route" and net:
        out = (m.get("route") or {}).get(net)
    elif section == "route":
        out = {n: {k: v for k, v in r.items() if k != "path"} for n, r in (m.get("route") or {}).items()}
    elif section == "grid":
        g = m.get("grid") or {}
        out = {k: v for k, v in g.items() if not isinstance(v, (str, dict))} | {"arrays": sorted(k for k, v in g.items() if isinstance(v, dict))}
    elif section == "board":
        out = {k: v for k, v in m["board"].items() if k != "outline"} | {"outline_points": len(m["board"]["outline"])}
    elif section == "verdict":
        out = m.get("verdict")
    s = json.dumps(out, separators=(",", ":"))
    if len(s) > limit:
        s = s[:limit] + f"... [{len(s) - limit} more bytes; narrow with --ref/--net/--group]"
    return s


# ------------------------------------------------------------------ rendering
class _PNG:
    def __init__(self, w, h, bg=(255, 255, 255)):
        from PIL import Image, ImageDraw
        self.im = Image.new("RGBA", (w, h), bg + (255,))
        self.d = ImageDraw.Draw(self.im, "RGBA")

    def rect(self, b, fill=None, outline=None):
        self.d.rectangle(b, fill=fill, outline=outline)

    def line(self, a, b, col, w=1):
        self.d.line([a, b], fill=col, width=w)

    def poly(self, pts, outline, w=1):
        self.d.line(list(pts) + [pts[0]], fill=outline, width=w)

    def text(self, xy, s, col=(0, 0, 0)):
        self.d.text(xy, s, fill=col)

    def circle(self, c, r, outline=None, fill=None):
        self.d.ellipse((c[0] - r, c[1] - r, c[0] + r, c[1] + r), outline=outline, fill=fill)

    def save(self, path):
        self.im.convert("RGB").save(path)


class _SVG:
    def __init__(self, w, h, bg=(255, 255, 255)):
        self.w, self.h, self.o = w, h, [f'<rect width="{w}" height="{h}" fill="{self._c(bg)}"/>']

    @staticmethod
    def _c(c):
        if c is None:
            return "none"
        return f"rgb({c[0]},{c[1]},{c[2]})" + ("" if len(c) < 4 else "")

    def _op(self, c):
        return "" if c is None or len(c) < 4 else f' opacity="{c[3] / 255:.2f}"'

    def rect(self, b, fill=None, outline=None):
        self.o.append(f'<rect x="{b[0]}" y="{b[1]}" width="{b[2] - b[0]}" height="{b[3] - b[1]}" fill="{self._c(fill)}"'
                      f' stroke="{self._c(outline)}"{self._op(fill)}/>')

    def line(self, a, b, col, w=1):
        self.o.append(f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" stroke="{self._c(col)}" stroke-width="{w}"{self._op(col)}/>')

    def poly(self, pts, outline, w=1):
        self.o.append('<polygon points="' + " ".join(f"{x},{y}" for x, y in pts) + f'" fill="none" stroke="{self._c(outline)}" stroke-width="{w}"/>')

    def text(self, xy, s, col=(0, 0, 0)):
        self.o.append(f'<text x="{xy[0]}" y="{xy[1] + 9}" font-size="9" font-family="monospace" fill="{self._c(col)}">{s}</text>')

    def circle(self, c, r, outline=None, fill=None):
        self.o.append(f'<circle cx="{c[0]}" cy="{c[1]}" r="{r}" fill="{self._c(fill)}" stroke="{self._c(outline)}"/>')

    def save(self, path):
        with open(path, "w") as fh:
            fh.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}">' + "".join(self.o) + "</svg>")


PALETTE = [(120, 170, 235), (240, 170, 90), (130, 200, 130), (200, 130, 200), (230, 210, 100), (110, 200, 210),
           (230, 130, 130), (170, 170, 170)]


def render(m, out, scale=9.0):
    """One picture per board side (parts, groups, relation lines, overflow heat, sealed pins) and one of the
    inner-layer demand.  `out` is OUT.png or OUT.svg; files OUT_F, OUT_B, OUT_inner are written."""
    base, ext = os.path.splitext(out)
    ext = ext.lower() or ".png"
    Canvas = _SVG if ext == ".svg" else _PNG
    x0, y0, x1, y1 = m["board"]["bbox"]
    pad = 20
    W, H = int((x1 - x0) * scale) + 2 * pad, int((y1 - y0) * scale) + 2 * pad + 30
    X = lambda x: int((x - x0) * scale) + pad
    Y = lambda y: int((y - y0) * scale) + pad + 30
    grid = m.get("grid") or {}
    gl = {g["name"]: i for i, g in enumerate(m["groups"])}
    P = {p["ref"]: p for p in m["parts"]}
    esc = (m.get("escapes") or {}).get("pins", {})
    ov = [f for f in (m.get("verdict") or {}).get("findings", []) if f["code"] == "PLAN_OVERFLOW"]
    files = []

    def heat(c, side_layers, inner=False):
        if not grid:
            return
        nx, ny, cell, (gx, gy) = grid["nx"], grid["ny"], grid["cell"], grid["origin"]
        for j in range(ny):
            for i in range(nx):
                k = j * nx + i
                tot = capsum = over = 0
                for lay in side_layers:
                    for d in ("H", "V"):
                        a = grid["cap" + d][lay][k]
                        b = grid["use" + d][lay][k]
                        a = 10 if a == "x" else int(a)
                        b = 10 if b == "x" else int(b)
                        capsum += a
                        tot += b
                        over += max(0, b - a)
                if capsum == 0 and tot == 0:
                    continue
                box = (X(gx + i * cell), Y(gy + j * cell), X(gx + (i + 1) * cell), Y(gy + (j + 1) * cell))
                if inner:
                    if tot:
                        c.rect(box, fill=(40, 90, 220, int(30 + 150 * min(1.0, tot / max(capsum, 1)))))
                elif over:
                    c.rect(box, fill=(230, 30, 30, min(230, 90 + 40 * over)))
                elif capsum == 0:
                    c.rect(box, fill=(90, 90, 90, 60))

    for tag, side in (("F", "F"), ("B", "B"), ("inner", None)):
        c = Canvas(W, H)
        c.text((pad, 6), f"{os.path.basename(m['board'].get('file', ''))}  side {tag}   fluxmap v{m['fluxmap']}   "
                         f"PLAN_SCORE {(m.get('verdict') or {}).get('score', '-')}", (0, 0, 0))
        poly = [(X(a), Y(b)) for a, b in m["board"]["outline"]]
        if len(poly) > 2:
            c.poly(poly, (0, 0, 0), 2)
        if side is None:
            heat(c, [grid["layers"][k] for k in range(len(grid.get("layers", []))) if 0 < k < len(grid["layers"]) - 1]
                 if grid else [], inner=True)
        else:
            sl = [l for l in grid.get("layers", []) if l.startswith(side)] if grid else []
            heat(c, sl)
        for p in m["parts"]:
            cb = p["court"]
            col = PALETTE[gl[p["group"]] % len(PALETTE)] if p["group"] in gl else (200, 200, 200)
            on = side is None or p["side"] == side
            if on:
                c.rect((X(cb[0]), Y(cb[1]), X(cb[2]), Y(cb[3])), fill=col + (110,),
                       outline=(0, 0, 0) if p["mode"] != "movable" else (80, 80, 80))
                if (cb[2] - cb[0]) * (cb[3] - cb[1]) > 12:
                    c.text((X(cb[0]) + 2, Y(cb[1]) + 2), p["ref"], (0, 0, 0))
            else:
                c.rect((X(cb[0]), Y(cb[1]), X(cb[2]), Y(cb[3])), outline=(190, 190, 190))
        for r in m["relations"]:
            sp, ic = P.get(r["part"]), P.get(r["serves"].split(".")[0])
            if not sp or not ic or not r.get("sure") or (side and sp["side"] != side and ic["side"] != side):
                continue
            bad = r["now"] > r["max"]
            c.line((X(sp["x"]), Y(sp["y"])), (X(ic["x"]), Y(ic["y"])), (220, 0, 0) if bad else (0, 140, 0), 2 if bad else 1)
        for ref, pins in esc.items():
            part = P.get(ref)
            if not part or (side and part["side"] != side):
                continue
            for pnum, rec in pins.items():
                if rec[0] in ("s", "n"):
                    pos = next((q for n in m["nets"] for q in n["pads"] if q[0] == f"{ref}.{pnum}"), None)
                    if pos:
                        col = (255, 0, 0) if rec[0] == "s" else (200, 0, 200)
                        c.circle((X(pos[1]), Y(pos[2])), 5, outline=col)
                        c.line((X(pos[1]) - 5, Y(pos[2]) - 5), (X(pos[1]) + 5, Y(pos[2]) + 5), col, 2)
        for f in ov:
            b = f.get("box")
            if b and side is not None:
                c.rect((X(b[0]), Y(b[1]), X(b[2]), Y(b[3])), outline=(230, 0, 0))
        path = f"{base}_{tag}{ext}"
        c.save(path)
        files.append(path)
    return files
