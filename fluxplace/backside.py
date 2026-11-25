"""The back side as a placement move: part heights, who may stand on the back, who stands there illegally.

Policy, from `[placement]` (nothing else is per board):
  sides = "front"           no free part is moved to the back (a graded support with no front seat still may, as before);
  sides = "both"            any free SMD part may stand on the back, outside the back-closed areas;
  sides = "both-light-back" the same, but only parts at most `back_max_height_mm` tall and not of a heavy class;
  key absent                the legacy behaviour (unguarded fallback flip of a support with no seat); `active()` is False.
Back-closed areas = keep-outs with side "B" or "both" (`legalise.keepout_rects`).  A part that must stay on the front in
every policy: a connector / switch / battery / relay, an antenna part, a part on an RF or differential-pair net (the flip
reverses its pin order and moves its controlled line's layer), a regulator-template unit (the datasheet loop is same-side),
a through-hole part, and the refs in `[placement] front_only = [globs]`.

Height of a part, first source that answers wins (`height(...) -> (mm, source)`):
  1 `[placement.heights]` (owner)   2 footprint property `Height`   3 the 3D model's top (STEP / WRL bbox of the model named
  by the footprint, rotate / scale / offset applied; `${KIPRJMOD}` = the project folder, `${KICAD*_3DMODEL_DIR}` = the KiCad
  library, else the same relative path under the nearest `lib/3dmodels` above the board or the working directory)
  4 chip size table (`intent._CHIP_H`)   5 package-class default (`PKG_H`)   6 unknown -> not eligible while a limit is set.
Models are parsed once per file per process (`_PTS`), heights once per footprint id and model (`Ctx.cache`).
"""
import fnmatch
import math
import os
import re

from . import geom as GM

SIDES_OK = ("front", "both", "both-light-back")
HEAVY_PREFIX = re.compile(r"^(L|T|TR|K|RLY|XFMR|TX)\d")           # inductor, transformer, relay: tall or heavy, stay on the front in both-light-back
ALWAYS_FRONT = re.compile(r"^(J|P|CN|SW|BT|BAT|AE|ANT|MH|MK|H|FID|TP|F)\d")   # connectors, switches, batteries, antennas, hardware, test points, fuses
HEAVY_AREA_MM2 = 100.0           # a courtyard this large is a module or a power part: front in both-light-back
SMALL_MM2 = 12.0                 # a free "small part": the ones that fill the gaps around an owner's pins
# package-class default heights (mm, nominal max from the JEDEC / vendor outline), the last source before "unknown"
PKG_H = [(re.compile(p, re.I), h) for p, h in (
    (r"SOT-?23|SOT-?323|SC-?70|SOT-?363|SOT-?353|SOT-?563|SOD-?(123|323|523|923)|SOT-?143", 1.2),
    (r"SOT-?89|SOT-?223|SMA\b|DO-?214AC", 2.3), (r"SMB\b|SMC\b", 2.6),
    (r"SOIC|SO-?8|SOP|SSOP|TSOP", 1.8), (r"TSSOP|MSOP|VSSOP|TSOT|SOT-?23-?[5-8]", 1.2),
    (r"VQFN|WQFN|UQFN|QFN|DFN|WSON|SON|LGA|MLP", 1.0), (r"BGA|CSP|WLCSP", 1.4), (r"LQFP|TQFP|PQFP|QFP", 1.6),
    (r"LED", 0.8), (r"Crystal|XTAL", 1.0), (r"Fuse", 1.0), (r"Diode_SMD|D_", 1.2),
    (r"Inductor|L_", 2.0), (r"Buzzer", 3.0))]

_PTS = {}                        # resolved model file -> numpy (n, 3) points, or None
_SCAN = {}                       # board file -> (mtime, text, {ref: (start, end)})
_STEP_PT = re.compile(r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\)\s*\)")
_WRL_PTS = re.compile(r"point\s*\[([^\]]*)\]", re.S)


# ------------------------------------------------------------------------------------------------ policy
def _pl(cons):
    return (cons or {}).get("placement") or {}


def policy(cons):
    """(sides or None, back_max_height_mm or None)"""
    pl = _pl(cons)
    s = pl.get("sides")
    mh = pl.get("back_max_height_mm")
    return (s if s in SIDES_OK else None), (float(mh) if mh is not None else None)


def active(cons):
    """True when free parts may be moved to the back by policy (`both` / `both-light-back`)."""
    return policy(cons)[0] in ("both", "both-light-back")


# ------------------------------------------------------------------------------------------------ heights
def _scan(B):
    from . import boardwrite as BW
    f = B.get("file")
    if not f or not os.path.exists(f):
        return "", {}
    mt = os.path.getmtime(f)
    c = _SCAN.get(f)
    if c and c[0] == mt:
        return c[1], c[2]
    text = open(f, errors="surrogateescape").read()
    root_end = BW._end(text, text.index("("))
    idx = {}
    for fs, fe in BW._children(text, 0, root_end, "footprint"):
        m = BW._REF.search(text, fs, fe)
        if m:
            idx[m.group(1)] = (fs, fe)
    _SCAN[f] = (mt, text, idx)
    return text, idx


def _roots(B):
    """folders a model path may resolve against: the project folder, the library folders found above the board, the working directory"""
    f = B.get("file") or ""
    d = os.path.dirname(os.path.realpath(f)) if f else os.getcwd()
    roots = [d]
    up = d
    for _ in range(6):
        for sub in ("lib/3dmodels", "hardware/lib/3dmodels", "3dmodels"):
            roots.append(os.path.join(up, sub))
        up = os.path.dirname(up)
    roots += [os.path.join(os.getcwd(), s) for s in ("hardware/lib/3dmodels", "lib/3dmodels")]
    roots += [os.environ.get(k, "") for k in sorted(os.environ) if re.fullmatch(r"KICAD\d*_3DMODEL_DIR", k)]
    roots += ["/usr/share/kicad/3dmodels"]
    return [r for r in roots if r and os.path.isdir(r)]


def resolve_model(B, name):
    """the file a footprint's `(model "...")` names, or None"""
    f = B.get("file") or ""
    proj = os.path.dirname(os.path.realpath(f)) if f else os.getcwd()
    p = re.sub(r"\$\{KIPRJMOD\}", proj, name)
    for ext in ("", ".step", ".stp", ".wrl"):
        if "${" not in p and os.path.isfile(p + ext) and ext == "":
            return os.path.realpath(p)
    base = re.sub(r"^\$\{[^}]*\}/?", "", name)
    tails = [base]
    if "3dmodels/" in name:
        tails.append(name.split("3dmodels/", 1)[1])
    for r in _roots(B):
        for t in tails:
            q = os.path.normpath(os.path.join(r, t))
            for cand in (q, os.path.splitext(q)[0] + ".step", os.path.splitext(q)[0] + ".stp"):
                if os.path.isfile(cand):
                    return os.path.realpath(cand)
    return None


def _points(path):
    import numpy as np
    if path in _PTS:
        return _PTS[path]
    pts = None
    try:
        txt = open(path, errors="ignore").read()
        if path.lower().endswith((".step", ".stp")):
            v = _STEP_PT.findall(txt)
            if v:
                pts = np.array([[float(a), float(b), float(c)] for a, b, c in v])
        else:
            vals = []
            for blk in _WRL_PTS.findall(txt):
                nums = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", blk)]
                vals += [nums[i:i + 3] for i in range(0, len(nums) - 2, 3)]
            if vals:
                pts = np.array(vals) * 2.54               # a WRL unit is 0.1 inch in KiCad
    except OSError:
        pts = None
    _PTS[path] = pts
    return pts


def _xyz(blk, key):
    m = re.search(r"\(" + key + r"\s*\(xyz\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\)", blk)
    return [float(v) for v in m.groups()] if m else None


def model_top(B, ref):
    """(mm, model path) of the 3D model's top above the board surface, or None"""
    import numpy as np
    text, idx = _scan(B)
    if ref not in idx:
        return None
    node = text[idx[ref][0]:idx[ref][1]]
    best = None
    for m in re.finditer(r'\(model\s+"([^"]+)"', node):
        blk = node[m.end():m.end() + 400]
        path = resolve_model(B, m.group(1))
        if not path:
            continue
        pts = _points(path)
        if pts is None or not len(pts):
            continue
        off, sc, rot = _xyz(blk, "offset") or [0, 0, 0], _xyz(blk, "scale") or [1, 1, 1], _xyz(blk, "rotate") or [0, 0, 0]
        q = pts * np.array(sc)
        rx, ry, rz = (math.radians(a) for a in rot)
        Rx = np.array([[1, 0, 0], [0, math.cos(rx), -math.sin(rx)], [0, math.sin(rx), math.cos(rx)]])
        Ry = np.array([[math.cos(ry), 0, math.sin(ry)], [0, 1, 0], [-math.sin(ry), 0, math.cos(ry)]])
        Rz = np.array([[math.cos(rz), -math.sin(rz), 0], [math.sin(rz), math.cos(rz), 0], [0, 0, 1]])
        q = q @ (Rz @ Ry @ Rx).T
        top = float(q[:, 2].max() + off[2])
        if top > 0.15 and (best is None or top > best[0]):
            best = (top, path)
    return best


def height(B, cons, ref, cache=None):
    """(mm | None, source) of part `ref`; see the module docstring for the order"""
    from . import intent as IT
    key = (B["parts"][ref].get("fp", ""), ref if "${" in str(ref) else None)
    fp = str(B["parts"][ref].get("fp", "")).split(":")[-1]
    for g in (_pl(cons).get("heights") or {}):
        if fnmatch.fnmatch(ref, g) or fnmatch.fnmatch(fp, g):
            return float(_pl(cons)["heights"][g]), "[placement.heights] " + g
    if cache is not None and key in cache and key[0]:
        return cache[key]
    out = _height_uncached(B, cons, ref, fp, IT)
    if cache is not None and key[0]:
        cache[key] = out
    return out


def _height_uncached(B, cons, ref, fp, IT):
    text, idx = _scan(B)
    if ref in idx:
        node = text[idx[ref][0]:idx[ref][1]]
        m = re.search(r'\(property\s+"(?:Height|height)"\s+"\s*([0-9.]+)\s*(?:mm)?\s*"', node)
        if m and float(m.group(1)) > 0:
            return float(m.group(1)), "footprint Height property"
    mt = model_top(B, ref)
    if mt:
        return round(mt[0], 3), "3D model " + os.path.basename(mt[1])
    h = IT.height(B, {}, ref)
    if h is not None:
        return h, "chip size table"
    for rx, v in PKG_H:
        if rx.search(fp):
            return v, "package-class default"
    return None, "unknown"


# ------------------------------------------------------------------------------------------------ eligibility
class Ctx:
    """per-board facts the eligibility test needs once: held parts, net classes, back-closed rectangles, the height cache"""

    def __init__(self, B, cons, spec=None, template=()):
        from . import modules as MD, netclass as NC, legalise as LG
        self.B, self.cons, self.spec = B, cons or {}, spec
        self.sides, self.max_h = policy(cons)
        self.fixed = MD.fixed_set(B, cons)
        cls = NC.classify(B, cons, spec)
        self.hot = {n for n, c in cls.items() if c.get("class") in ("rf", "pair")}
        self.closed = [(r, nm, set(al)) for r, sd, al, _mh, nm in LG.keepout_rects(B, cons) if "B" in sd]
        self.front_only = list(_pl(cons).get("front_only") or [])
        self.allow = list(_pl(cons).get("back_allow") or [])      # heavy-class parts the owner lets go to the back (answer to `crowding`)
        self.template = set(template)
        self.cache = {}

    def height(self, ref):
        return height(self.B, self.cons, ref, self.cache)


def in_closed(ctx, ref, court=None):
    c = court or ctx.B["parts"][ref]["court"]
    for r, nm, allow in ctx.closed:
        if ref not in allow and c[0] < r[2] and c[2] > r[0] and c[1] < r[3] and c[3] > r[1]:
            return nm
    return None


def stays_front(ctx, ref):
    """why `ref` may never stand on the back (any policy), or None"""
    p = ctx.B["parts"][ref]
    if p["tht"]:
        return "through-hole part"
    if ALWAYS_FRONT.match(ref):
        return "connector / switch / battery / antenna / hardware class"
    if any(fnmatch.fnmatch(ref, g) for g in ctx.front_only):
        return "[placement] front_only"
    if ref in ctx.template:
        return "regulator template unit (loop rows are same-side)"
    if any(q.get("net") in ctx.hot for q in p["pads"]):
        return "RF / differential-pair endpoint (the flip reverses pin order)"
    return None


def back_ok(B, cons, ref, spec=None, ctx=None, at_pose=True):
    """(True | False, why): may free part `ref` stand on the back under the policy of `cons`"""
    ctx = ctx or Ctx(B, cons, spec)
    if ctx.sides not in ("both", "both-light-back"):
        return False, "sides policy is " + (ctx.sides or "not set")
    if ref in ctx.fixed or B["parts"][ref].get("locked"):
        return False, "fixed / locked"
    w = stays_front(ctx, ref)
    if w:
        return False, w
    p = B["parts"][ref]
    if ctx.sides == "both-light-back":
        c = p["court"]
        if (HEAVY_PREFIX.match(ref) or (c[2] - c[0]) * (c[3] - c[1]) > HEAVY_AREA_MM2) and not any(fnmatch.fnmatch(ref, g) for g in ctx.allow):
            return False, "heavy class (inductor / transformer / relay / module)"
        if ctx.max_h is not None:
            h, src = ctx.height(ref)
            if h is None:
                return False, "height unknown"
            if h > ctx.max_h + 1e-9:
                return False, f"{h:g} mm > back_max_height_mm {ctx.max_h:g} ({src})"
    if at_pose:
        nm = in_closed(ctx, ref)
        if nm:
            return False, "in back-closed area " + nm
    return True, "ok"


def guard(B, cons, spec=None, template=()):
    """callable(ref) -> bool for `clusters.reseat(back=...)`, or None when the policy does not move parts (legacy behaviour kept)"""
    if not active(cons):
        return None
    ctx = Ctx(B, cons, spec, template)
    return lambda ref: back_ok(B, cons, ref, ctx=ctx, at_pose=False)[0]


def side_illegal(B, cons, spec=None, ctx=None):
    """[{ref, why}] free parts standing on the back that the policy does not allow: not eligible, taller than the limit, or in a back-closed area"""
    ctx = ctx or Ctx(B, cons, spec)
    out = []
    for ref in sorted(B["parts"]):
        p = B["parts"][ref]
        if p["side"] != "B.Cu" or ref in ctx.fixed or p.get("locked") or p["tht"]:
            continue
        nm = in_closed(ctx, ref)
        if nm:
            out.append({"ref": ref, "why": "in back-closed area " + nm})
            continue
        if ctx.max_h is not None:
            h, src = ctx.height(ref)
            if h is not None and h > ctx.max_h + 1e-9:
                out.append({"ref": ref, "why": f"{h:g} mm > back_max_height_mm {ctx.max_h:g} ({src})"})
                continue
        if ctx.sides in ("both", "both-light-back"):
            ok, why = back_ok(B, cons, ref, ctx=ctx, at_pose=False)
            if not ok:
                out.append({"ref": ref, "why": why})
    return out


def gaps(B, cons, spec=None):
    """`side-illegal` gaps in the placegaps shape {id, kind, class, mag, refs, nets, at, row, detail}"""
    out = []
    for g in side_illegal(B, cons, spec):
        p = B["parts"][g["ref"]]
        out.append({"id": "side-illegal:" + g["ref"], "kind": "side-illegal", "class": "placement", "mag": 1.0, "refs": [g["ref"]],
                    "nets": sorted({q["net"] for q in p["pads"] if q.get("net")}), "at": [round(p["pos"][0], 3), round(p["pos"][1], 3)],
                    "row": None, "detail": g["why"]})
    return out


def assembly_note(B, cons, spec=None):
    """one line: the back parts and what a second side costs the assembler; heights sourced / unknown over the back parts"""
    ctx = Ctx(B, cons, spec)
    back = [r for r, p in sorted(B["parts"].items()) if p["side"] == "B.Cu" and not p["tht"]]
    if not back:
        return "assembly: all SMD parts on the front: single-sided reflow"
    hs = {r: ctx.height(r) for r in back}
    tall = max(hs, key=lambda r: (hs[r][0] or 0, r))
    unk = [r for r in back if hs[r][0] is None]
    mpns = {str(B["parts"][r].get("value", "")) for r in back}
    return (f"assembly: {len(back)} SMD part(s) on the back ({len(mpns)} distinct values), tallest {tall} "
            f"{(hs[tall][0] or 0):g} mm ({hs[tall][1]}), height unknown {len(unk)}; double-sided reflow = a second stencil and reflow pass, "
            f"back parts must survive the second pass or be glued")


def stats(B, cons, spec=None, refs=None):
    """{source: count} of the heights of `refs` (default: every part on the back)"""
    ctx = Ctx(B, cons, spec)
    refs = refs if refs is not None else [r for r, p in B["parts"].items() if p["side"] == "B.Cu"]
    out = {}
    for r in sorted(refs):
        src = ctx.height(r)[1]
        k = src.split(" ")[0] if src.startswith(("3D", "footprint", "chip", "package", "unknown")) else src
        out[k] = out.get(k, 0) + 1
    return out
