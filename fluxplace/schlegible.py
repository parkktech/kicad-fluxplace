"""Legibility check of a .kicad_sch tree, read back from the files (independent of how they were drawn).

Per sheet: WIRE_OVERLAP (two wire segments collinear and overlapping), WIRE_THROUGH_BODY (a wire crossing the interior of
a symbol body), LABEL_OVERLAP (two label boxes intersect), LABEL_SYMBOL_OVERLAP (a label box intersects a symbol body or a
visible field's text), TEXT_OVERLAP (a reference/value text over another one or over another symbol's body). Text extents are estimated from the stroke font (0.92 x height per character, measured), so label checks are
conservative to about half a character. Symbols at rotation 0/90/180/270 without mirroring are supported.
"""
import os
import re
from collections import defaultdict

CHW = 0.92          # advance per character, in units of the font height (measured on a kicad-cli render)
EPS = 0.02


def parse(text):
    tok = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')
    stack, cur = [], []
    for m in tok.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append(cur)
            cur = []
        elif t == ")":
            done = cur
            cur = stack.pop()
            cur.append(done)
        else:
            cur.append(t[1:-1].replace('\\"', '"').replace("\\\\", "\\") if t.startswith('"') else t)
    return cur[0] if cur else []


def kids(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def kid(node, name):
    k = kids(node, name)
    return k[0] if k else None


def _f(v):
    return float(v)


def _font_h(node):
    e = kid(node, "effects")
    f = kid(e, "font") if e else None
    s = kid(f, "size") if f else None
    return _f(s[1]) if s else 1.27


def _hidden(node):
    e = kid(node, "effects")
    return bool(e and ("hide" in e or any(isinstance(c, list) and c[0] == "hide" and "yes" in c for c in e))) or "hide" in node


def _just(node):
    e = kid(node, "effects")
    j = kid(e, "justify") if e else None
    return set(j[1:]) if j else set()


def text_box(x, y, s, h, just, angle=0):
    w = len(s) * CHW * h
    x0 = x - w / 2 if not ({"left", "right"} & just) else (x if "left" in just else x - w)
    y0 = y - h / 2 if not ({"top", "bottom"} & just) else (y if "top" in just else y - h)
    if int(round(angle)) % 180 == 90:
        return (x - h / 2, y - w, x + h / 2, y) if "left" in just else (x - h / 2, y - w / 2, x + h / 2, y + w / 2)
    return (x0, y0, x0 + w, y0 + h)


def _xf(px, py, at):
    """lib point (y up) -> sheet point for a symbol placed at (x, y, rot)."""
    x, y, r = at
    sx, sy = px, -py
    for _ in range(int(round(r / 90.0)) % 4):
        sx, sy = sy, -sx
    return x + sx, y + sy


def lib_geometry(sym):
    """{lib_id: {"body": [rects in lib coords], "power": bool}}"""
    out = {}
    for s in sym:
        if not (isinstance(s, list) and s and s[0] == "symbol"):
            continue
        name = s[1]
        rects = []
        for u in kids(s, "symbol"):
            for r in kids(u, "rectangle"):
                a, b = kid(r, "start"), kid(r, "end")
                rects.append((_f(a[1]), _f(a[2]), _f(b[1]), _f(b[2])))
            pls = kids(u, "polyline")
            if pls:
                xs, ys = [], []
                for pl in pls:
                    for xy in kids(kid(pl, "pts"), "xy"):
                        xs.append(_f(xy[1]))
                        ys.append(_f(xy[2]))
                rects.append((min(xs), min(ys), max(xs), max(ys)))
        out[name] = {"body": rects, "power": any(c == "power" or (isinstance(c, list) and c and c[0] == "power") for c in s)}
    return out


def sheet_items(path):
    root = parse(open(path, errors="ignore").read())
    libs = lib_geometry(kid(root, "lib_symbols") or [])
    wires = []
    for w in kids(root, "wire"):
        p = kids(kid(w, "pts"), "xy")
        wires.append((_f(p[0][1]), _f(p[0][2]), _f(p[1][1]), _f(p[1][2])))
    labels = []
    for kind in ("label", "global_label", "hierarchical_label"):
        for l in kids(root, kind):
            at = kid(l, "at")
            x, y, a = _f(at[1]), _f(at[2]), _f(at[3]) if len(at) > 3 else 0.0
            h = _font_h(l)
            w = len(l[1]) * CHW * h
            if kind == "label":
                box = text_box(x, y - 0.3, l[1], h, {"left", "bottom"}) if int(a) == 0 else \
                    text_box(x, y - 0.3, l[1], h, {"right", "bottom"})
            else:
                ext = w + 1.6 * h
                box = (x, y - 0.6 * h, x + ext, y + 0.6 * h) if int(a) == 0 else (x - ext, y - 0.6 * h, x, y + 0.6 * h)
            labels.append({"net": l[1], "box": box, "kind": kind})
    bodies, ftexts = [], []
    for s in kids(root, "symbol"):
        lid = kid(s, "lib_id")[1]
        at = kid(s, "at")
        pos = (_f(at[1]), _f(at[2]), _f(at[3]) if len(at) > 3 else 0.0)
        g = libs.get(lid, {"body": [], "power": False})
        ref = ""
        for p in kids(s, "property"):
            if p[1] == "Reference":
                ref = p[2]
        for r in g["body"]:
            a = _xf(r[0], r[1], pos)
            b = _xf(r[2], r[3], pos)
            bodies.append({"ref": ref, "box": (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])),
                           "power": g["power"]})
        for p in kids(s, "property"):
            if _hidden(p) or p[1] not in ("Reference", "Value") or not p[2]:
                continue
            pa = kid(p, "at")
            ftexts.append({"ref": ref, "box": text_box(_f(pa[1]), _f(pa[2]), p[2], _font_h(p), _just(p), _f(pa[3]) if len(pa) > 3 else 0)})
    sheets = []
    for sh in kids(root, "sheet"):
        for p in kids(sh, "property"):
            if p[1] == "Sheetfile":
                sheets.append(p[2])
    return {"wires": wires, "labels": labels, "bodies": bodies, "texts": ftexts, "sheets": sheets}


def _inter(a, b, eps=EPS):
    return a[0] < b[2] - eps and b[0] < a[2] - eps and a[1] < b[3] - eps and b[1] < a[3] - eps


def _seg_through(w, r, eps=0.05):
    x1, y1, x2, y2 = w
    a0, b0, a1, b1 = r[0] + eps, r[1] + eps, r[2] - eps, r[3] - eps
    if a0 >= a1 or b0 >= b1:
        return False
    if abs(y1 - y2) < 1e-6:
        return b0 < y1 < b1 and min(x1, x2) < a1 and max(x1, x2) > a0
    if abs(x1 - x2) < 1e-6:
        return a0 < x1 < a1 and min(y1, y2) < b1 and max(y1, y2) > b0
    return False


def check_sheet(path):
    it = sheet_items(path)
    out = defaultdict(list)
    ws = it["wires"]
    hz = defaultdict(list)
    vt = defaultdict(list)
    for w in ws:
        if abs(w[1] - w[3]) < 1e-6:
            hz[round(w[1], 3)].append((min(w[0], w[2]), max(w[0], w[2])))
        elif abs(w[0] - w[2]) < 1e-6:
            vt[round(w[0], 3)].append((min(w[1], w[3]), max(w[1], w[3])))
        else:
            out["WIRE_DIAGONAL"].append(w)
    for key, groups in (("h", hz), ("v", vt)):
        for c, segs in groups.items():
            segs.sort()
            for i in range(len(segs)):
                for j in range(i + 1, len(segs)):
                    if segs[j][0] >= segs[i][1] - 1e-6:
                        break
                    out["WIRE_OVERLAP"].append((key, c, segs[i], segs[j]))
    for w in ws:
        for b in it["bodies"]:
            if not b["power"] and _seg_through(w, b["box"]):
                out["WIRE_THROUGH_BODY"].append((b["ref"], w))
    L = it["labels"]
    for i in range(len(L)):
        for j in range(i + 1, len(L)):
            if _inter(L[i]["box"], L[j]["box"]):
                out["LABEL_OVERLAP"].append((L[i]["net"], L[j]["net"]))
        for b in it["bodies"]:
            if _inter(L[i]["box"], b["box"]):
                out["LABEL_SYMBOL_OVERLAP"].append((L[i]["net"], b["ref"]))
        for t in it["texts"]:
            if _inter(L[i]["box"], t["box"]):
                out["LABEL_SYMBOL_OVERLAP"].append((L[i]["net"], t["ref"] + " text"))
    T = it["texts"]
    for i in range(len(T)):
        for j in range(i + 1, len(T)):
            if _inter(T[i]["box"], T[j]["box"]):
                out["TEXT_OVERLAP"].append((T[i]["ref"], T[j]["ref"]))
        for b in it["bodies"]:
            if b["ref"] != T[i]["ref"] and _inter(T[i]["box"], b["box"]):
                out["TEXT_OVERLAP"].append((T[i]["ref"], b["ref"]))
    return out, it


def check_tree(root):
    d = os.path.dirname(os.path.abspath(root))
    todo, seen, per = [os.path.abspath(root)], set(), {}
    tot = defaultdict(int)
    examples = defaultdict(list)
    while todo:
        p = todo.pop(0)
        if p in seen or not os.path.exists(p):
            continue
        seen.add(p)
        res, it = check_sheet(p)
        per[os.path.basename(p)] = {k: len(v) for k, v in res.items()}
        per[os.path.basename(p)]["wires"] = len(it["wires"])
        per[os.path.basename(p)]["labels"] = len(it["labels"])
        for k, v in res.items():
            tot[k] += len(v)
            examples[k] += [(os.path.basename(p),) + tuple(map(str, e)) for e in v[:3]]
        todo += [os.path.join(d, s) for s in it["sheets"]]
    codes = ("WIRE_OVERLAP", "WIRE_THROUGH_BODY", "LABEL_OVERLAP", "LABEL_SYMBOL_OVERLAP", "TEXT_OVERLAP", "WIRE_DIAGONAL")
    counts = {k: tot.get(k, 0) for k in codes}
    return {"counts": counts, "total": sum(counts.values()), "per_sheet": per,
            "examples": {k: v[:6] for k, v in examples.items()}}
