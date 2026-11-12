"""Silkscreen check and fit (`fluxplace silk`).

check(scene, cfg) -> findings; fit(scene, cfg) -> plan. Both are pure Python on a *scene* (polygons in mm) that
`silk_helper.py` extracts from the board under KiCad's python; `apply_plan` writes the plan back through the same
helper. Only footprint REFERENCE texts are ever changed (moved, resized, or, last resort, hidden from the
silkscreen and kept on the Fab layer). Footprints, tracks, vias, zones, board texts and footprint graphics are
never modified. Deterministic: no randomness, ordered containers, explicit tie-breaks.

Finding codes (level): SILK_OFF_BOARD, SILK_OVER_PAD, SILK_OVER_HOLE = FAIL; SILK_OVERLAP, SILK_UNDER_PART,
SILK_TOO_SMALL, SILK_FAR_FROM_PART = WARN; SILK_REF_HIDDEN = INFO (WARN for connector / test point / switch /
LED / polarised parts); SILK_MISSING_MARK = INFO.
"""
import json
import math
import os
import re
import subprocess

PY = os.environ.get("FLUXPLACE_KICAD_PYTHON", "/usr/bin/python3")
HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "silk_helper.py")
MARK = "@@SILK@@"

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
LEVEL = {"SILK_VERSION_STALE": FAIL, "SILK_TITLE_MISSING": WARN, "SILK_TITLE_NO_ROOM": FAIL, "SILK_OFF_BOARD": FAIL, "SILK_OVER_PAD": FAIL, "SILK_OVER_HOLE": FAIL, "SILK_OVERLAP": WARN,
         "SILK_UNDER_PART": WARN, "SILK_TOO_SMALL": WARN, "SILK_FAR_FROM_PART": WARN,
         "SILK_REF_HIDDEN": INFO, "SILK_MISSING_MARK": INFO}
CODES = tuple(LEVEL)
HARD = ("SILK_OFF_BOARD", "SILK_OVER_PAD", "SILK_OVER_HOLE", "SILK_OVERLAP", "SILK_UNDER_PART")
FIT_CODES = HARD + ("SILK_TOO_SMALL", "SILK_FAR_FROM_PART")

DEFAULTS = {
    "enabled": True,              # false: review and the write hooks skip silk entirely
    "edge_clearance_mm": 0.25,    # text/graphics closer than this to Edge.Cuts = SILK_OFF_BOARD
    "pad_clearance_mm": 0.15,     # clearance around a pad's solder-mask opening
    "hole_clearance_mm": 0.15,    # clearance around NPTH / mounting holes and untented via holes
    "min_height_mm": 0.8,         # SILK_TOO_SMALL below this text height
    "max_ref_distance_mm": 4.0,   # SILK_FAR_FROM_PART beyond this; also the fit's search radius
    "under_part_overlap": 0.5,    # share of a reference box inside another part's courtyard that counts as covered
    "text_clearance_mm": 0.0,     # extra gap required between two texts / a text and other silk graphics
    "ref_gap_mm": 0.2,            # fit: gap between a courtyard edge and the re-placed text
    "size_step_mm": 0.1,          # fit: text height step when shrinking
    "auto_fit": True,             # write hooks run the fit on moved parts' references
    "check_graphics": True,       # also check footprint silkscreen graphics (off board / over pads / over holes)
}
FIT_MARGIN = 0.05                 # fit keeps this much more than the check demands (model error, rounding)
_CONN = re.compile(r"^(J|P|CN|X|JP)\d", re.I)
_TP = re.compile(r"^TP\d", re.I)
_SW = re.compile(r"^(SW|S|SB)\d", re.I)
_LED = re.compile(r"^(LED|D)\d", re.I)
_POLAR = re.compile(r"^(D|LED|CP|BT|BAT|Q)\d", re.I)
_POLAR_FP = re.compile(r"polari[sz]ed|(^|_)CP_|tantalum|elec|_pol", re.I)
_IC = re.compile(r"^(U|IC)\d", re.I)


def settings(cons=None):
    cfg = dict(DEFAULTS)
    for k, v in ((cons or {}).get("silk") or {}).items():
        if k in DEFAULTS:
            cfg[k] = type(DEFAULTS[k])(v) if not isinstance(DEFAULTS[k], bool) else bool(v)
    from . import constraints as C
    cfg["min_stroke_mm"] = C.fab_profile(cons)["min_silk_width_mm"]
    return cfg


# ------------------------------------------------------------------ geometry
def _aabb(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _inside_poly(pt, poly):
    x, y = pt
    c = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            c = not c
        j = i
    return c


def _seg_x(a, b, c, d):
    def o(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    o1, o2, o3, o4 = o(a, b, c), o(a, b, d), o(c, d, a), o(c, d, b)
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def rect(cx, cy, hw, hh, ang=0.0):
    """Corners (p0..p3, in order) of a box of half extents (hw, hh) in its own frame, rotated by the KiCad
    angle `ang` (counter-clockwise on screen, y down) about (cx, cy)."""
    a = math.radians(ang)
    c, s = math.cos(a), math.sin(a)
    return [[cx + x * c + y * s, cy - x * s + y * c] for x, y in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]


def inflate(q, d):
    """A rectangle (corner order as from rect()) grown by d on every side."""
    if d == 0:
        return q
    cx = sum(p[0] for p in q) / 4.0
    cy = sum(p[1] for p in q) / 4.0
    ux, uy = q[1][0] - q[0][0], q[1][1] - q[0][1]
    vx, vy = q[3][0] - q[0][0], q[3][1] - q[0][1]
    lu, lv = math.hypot(ux, uy) or 1e-9, math.hypot(vx, vy) or 1e-9
    ux, uy, vx, vy = ux / lu, uy / lu, vx / lv, vy / lv
    hu, hv = lu / 2 + d, lv / 2 + d
    return [[cx + sx * hu * ux + sy * hv * vx, cy + sx * hu * uy + sy * hv * vy]
            for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def _quads_overlap(a, b):
    for q in (a, b):
        for i in range(4):
            ex, ey = q[(i + 1) % 4][0] - q[i][0], q[(i + 1) % 4][1] - q[i][1]
            nx, ny = -ey, ex
            pa = [p[0] * nx + p[1] * ny for p in a]
            pb = [p[0] * nx + p[1] * ny for p in b]
            if max(pa) <= min(pb) or max(pb) <= min(pa):
                return False
    return True


def _quad_poly(q, poly, qb=None):
    """Does the convex quad overlap the (possibly concave) polygon?"""
    qb = qb or _aabb(q)
    pb = _aabb(poly)
    if qb[2] < pb[0] or pb[2] < qb[0] or qb[3] < pb[1] or pb[3] < qb[1]:
        return False
    if any(_inside_poly(p, poly) for p in q) or any(_inside_quad(p, q) for p in poly):
        return True
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        for k in range(4):
            if _seg_x(a, b, q[k], q[(k + 1) % 4]):
                return True
    return False


def _inside_quad(p, q):
    s = 0
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        cr = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        if cr == 0:
            continue
        if s == 0:
            s = 1 if cr > 0 else -1
        elif (cr > 0) != (s > 0):
            return False
    return True


def _area(poly):
    return sum(poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1]
               for i in range(len(poly))) / 2.0


def _clip_area(poly, q):
    """Area of `poly` inside the convex quad `q` (Sutherland-Hodgman)."""
    out = [tuple(p) for p in poly]
    sgn = 1 if _area(q) > 0 else -1
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        inp, out = out, []
        if not inp:
            break

        def side(p):
            return sgn * ((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]))
        for j in range(len(inp)):
            p, r = inp[j], inp[(j + 1) % len(inp)]
            sp, sr = side(p), side(r)
            if sp >= 0:
                out.append(p)
            if (sp >= 0) != (sr >= 0):
                t = sp / (sp - sr)
                out.append((p[0] + t * (r[0] - p[0]), p[1] + t * (r[1] - p[1])))
    return abs(_area(out)) if len(out) >= 3 else 0.0


class Grid:
    """Uniform-grid spatial index: insert (aabb, payload), query by aabb. Remove by id."""

    def __init__(self, cell=3.0):
        self.cell, self.cells, self.items = cell, {}, {}

    def _keys(self, b):
        c = self.cell
        return [(i, j) for i in range(int(math.floor(b[0] / c)), int(math.floor(b[2] / c)) + 1)
                for j in range(int(math.floor(b[1] / c)), int(math.floor(b[3] / c)) + 1)]

    def add(self, key, bbox, payload):
        self.items[key] = (bbox, payload)
        for k in self._keys(bbox):
            self.cells.setdefault(k, []).append(key)

    def remove(self, key):
        it = self.items.pop(key, None)
        if it:
            for k in self._keys(it[0]):
                self.cells[k].remove(key)

    def query(self, b):
        seen, out = set(), []
        for k in self._keys(b):
            for key in self.cells.get(k, ()):
                if key not in seen:
                    seen.add(key)
                    bb, pl = self.items[key]
                    if not (bb[2] < b[0] or b[2] < bb[0] or bb[3] < b[1] or b[3] < bb[1]):
                        out.append((key, pl))
        out.sort(key=lambda kv: str(kv[0]))
        return out


# ------------------------------------------------------------------ scene
def load_scene(board, py=None):
    """Run the pcbnew helper; -> scene dict. Raises RuntimeError when pcbnew/the board is unusable."""
    r = subprocess.run([py or PY, HELPER, "scene", board], capture_output=True, text=True, timeout=600)
    if MARK not in r.stdout:
        raise RuntimeError("silk helper failed: " + (r.stderr.strip().splitlines() or ["no output"])[-1][:200])
    return _prep(json.loads(r.stdout.split(MARK, 1)[1]))


MIN_VISIBLE_H = 0.05      # a text this small is deliberately invisible (logo footprints use 0.001 mm refs)


def _prep(sc):
    sc["texts"] = [t for t in sc["texts"] if t["h"] >= MIN_VISIBLE_H]
    for i, t in enumerate(sc["texts"]):
        t["id"] = i
        t["aabb"] = _aabb(t["quad"])
    return sc


def apply_plan(board, out, plan, py=None):
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(plan, fh)
        pf = fh.name
    try:
        r = subprocess.run([py or PY, HELPER, "apply", board, out, pf], capture_output=True, text=True, timeout=600)
    finally:
        os.unlink(pf)
    if MARK not in r.stdout:
        raise RuntimeError("silk apply failed: " + (r.stderr.strip().splitlines() or ["no output"])[-1][:200])
    if os.path.abspath(board) != os.path.abspath(out):       # SaveBoard writes a DEFAULT .kicad_pro: keep the project's
        import shutil
        sp = os.path.splitext(board)[0] + ".kicad_pro"
        if os.path.exists(sp):
            shutil.copy(sp, os.path.splitext(out)[0] + ".kicad_pro")
    return json.loads(r.stdout.split(MARK, 1)[1])


def part_class(ref, part):
    """-> set of {'connector','testpoint','switch','led','polar','ic'} for a footprint."""
    c = set()
    nm = (part.get("name") or "") + " " + (part.get("value") or "")
    if _CONN.match(ref):
        c.add("connector")
    if _TP.match(ref):
        c.add("testpoint")
    if _SW.match(ref):
        c.add("switch")
    if _LED.match(ref) and (ref.upper().startswith("LED") or "led" in nm.lower()):
        c.add("led")
    if _POLAR.match(ref) or _POLAR_FP.search(nm):
        c.add("polar")
    if _IC.match(ref):
        c.add("ic")
    return c


def court_rect(part):
    """(x0, y0, x1, y1) of the footprint courtyard, else of its pads grown by 0.25, else None."""
    if part.get("court"):
        b = _aabb([p for poly in part["court"] for p in poly])
        return b
    pb = part.get("pad_bbox")
    if pb:
        return (pb[0] - 0.25, pb[1] - 0.25, pb[2] + 0.25, pb[3] + 0.25)
    return None


def _dist_rect(x, y, r):
    dx = max(r[0] - x, 0.0, x - r[2])
    dy = max(r[1] - y, 0.0, y - r[3])
    return math.hypot(dx, dy)


class Layout:
    """Everything a silkscreen text can collide with, indexed per side; texts can be added/removed."""

    def __init__(self, sc, cfg):
        self.sc, self.cfg = sc, cfg
        rings = sc.get("outline") or []
        self.rings = [r["pts"] for r in rings if len(r["pts"]) >= 3]
        self.edges = [(a[i], a[(i + 1) % len(a)]) for a in self.rings for i in range(len(a))]
        self.pads = {"F": Grid(), "B": Grid()}
        self.holes = {"F": Grid(), "B": Grid()}
        self.courts = {"F": Grid(), "B": Grid()}
        self.gfx = {"F": Grid(), "B": Grid()}
        self.texts = {"F": Grid(), "B": Grid()}
        for i, p in enumerate(sc["pads"]):
            self.pads[p["side"]].add(i, _aabb(p["poly"]), p)
        for i, h in enumerate(sc["holes"]):
            for s in h["sides"]:
                self.holes[s].add(i, _aabb(h["poly"]), h)
        k = 0
        for c in sc["courts"]:
            for poly in c["polys"]:
                self.courts[c["side"]].add(k, _aabb(poly), (c["owner"], poly))
                k += 1
        k = 0
        for g in sc["gfx"]:
            for poly in g["polys"]:
                self.gfx[g["side"]].add(k, _aabb(poly), (g["owner"], poly))
                k += 1

    def add_text(self, t, quad=None):
        q = quad or t["quad"]
        self.texts[t["side"]].add(t["id"], _aabb(q), (t, q))

    def remove_text(self, t):
        self.texts[t["side"]].remove(t["id"])

    # -- outline ----------------------------------------------------
    def in_region(self, p):
        n = 0
        for r in self.rings:
            if _inside_poly(p, r):
                n += 1
        return n % 2 == 1

    def off_board(self, q, d):
        """True when the rectangle `q` grown by d is not wholly inside the outline."""
        if not self.rings:
            return False
        g = inflate(q, d)
        if not all(self.in_region(p) for p in g):
            return True
        gb = _aabb(g)
        for a, b in self.edges:
            if max(a[0], b[0]) < gb[0] or min(a[0], b[0]) > gb[2] or max(a[1], b[1]) < gb[1] or min(a[1], b[1]) > gb[3]:
                continue
            if _inside_quad(a, g) or _inside_quad(b, g) or any(_seg_x(a, b, g[k], g[(k + 1) % 4]) for k in range(4)):
                return True
        return False

    # -- one probe --------------------------------------------------
    def probe(self, q, side, owner, kind, self_id=None, margin=0.0):
        """Collisions of the text box `q`: {code: [partner,...]} plus soft = {'under': frac, 'own_gfx': n}."""
        cfg = self.cfg
        res, soft = {}, {"under": 0.0, "own_gfx": 0}
        if self.off_board(q, cfg["edge_clearance_mm"] + margin):
            res["SILK_OFF_BOARD"] = ["edge"]
        qb = _aabb(q)
        d = cfg["pad_clearance_mm"] + margin
        g = inflate(q, d)
        gb = _aabb(g)
        for _, p in self.pads[side].query(gb):
            if _quad_poly(g, p["poly"], gb):
                res.setdefault("SILK_OVER_PAD", []).append(f"{p['owner']}.{p['num']}")
        d = cfg["hole_clearance_mm"] + margin
        g = inflate(q, d)
        gb = _aabb(g)
        for _, h in self.holes[side].query(gb):
            if _quad_poly(g, h["poly"], gb):
                res.setdefault("SILK_OVER_HOLE", []).append(h["owner"] or h["kind"])
        d = cfg["text_clearance_mm"] + margin
        g = inflate(q, d)
        gb = _aabb(g)
        for key, (t, tq) in self.texts[side].query(gb):
            if key != self_id and _quads_overlap(g, tq):
                res.setdefault("SILK_OVERLAP", []).append(("text", t["id"]))
        for _, (go, poly) in self.gfx[side].query(gb):
            if owner and go == owner:
                if _quad_poly(g, poly, gb):
                    soft["own_gfx"] += 1
            elif _quad_poly(g, poly, gb):
                res.setdefault("SILK_OVERLAP", []).append(("gfx", go))
        if kind == "ref":
            area = (math.hypot(q[1][0] - q[0][0], q[1][1] - q[0][1]) * math.hypot(q[3][0] - q[0][0], q[3][1] - q[0][1])) or 1e-9
            tot = {}
            for _, (co, poly) in self.courts[side].query(qb):
                if co != owner:
                    tot[co] = tot.get(co, 0.0) + _clip_area(poly, q) / area
            if tot:
                co = max(sorted(tot), key=lambda k: tot[k])
                soft["under"] = tot[co]
                if tot[co] > cfg["under_part_overlap"]:
                    res["SILK_UNDER_PART"] = [co]
        return res, soft


# ------------------------------------------------------------------ title / version text
# A version is v/V/rev/Rev/REV + digits(.digits)* as its own token (whitespace / start / end around it).
# "3V3" "1V8" "5V" "12V" "+3.3V" "24V" "V+" "VBAT" "VIN" never match.
_VER_RE = re.compile(r"(?<![^\s(\[,;:])(?:[vV]|[rR][eE][vV]\.?\s?)(\d+(?:\.\d+)*)(?![^\s)\],;:])")
_NETLIKE_RE = re.compile(r"\s*[vV]\d+\s*")      # a lone "V1" / "V12": a test-point or rail label, ambiguous


def version_tokens(text, company=None, is_title=False):
    """Version numbers (strings) a board text really states; [] for voltage / net-style labels. A lone integer
    'V<n>' is ambiguous (net label) and only counts when the text is the title or carries the company string."""
    if _NETLIKE_RE.fullmatch(text) and not is_title and not (company and company.lower() in text.lower()):
        return []
    return [m.group(1) for m in _VER_RE.finditer(text)]


_VER_ANY = r"(?:[vV]|[rR]ev\.?\s?)\s?\d+(?:\.\d+)*"
TITLE_DEFAULTS = {"text": "{company}  V{version}", "company": "", "version": "", "side": "F", "height_mm": 1.5,
                  "required": True}
TITLE_MIN_H = 1.0


def norm_version(tok):
    """'1.6' | '16' | 'V1.60' -> (1, 6): a bare two-digit number reads as major.minor (the file-name convention
    board-v16 = 1.6); trailing zero components are dropped so 1.0 == 1."""
    s = str(tok).strip().lstrip("vV").strip()
    if re.fullmatch(r"\d\d", s):
        s = s[0] + "." + s[1]
    try:
        v = [int(x) for x in s.split(".")]
    except ValueError:
        return None
    while len(v) > 1 and v[-1] == 0:
        v.pop()
    return tuple(v)


def _vshow(v):
    return ".".join(str(x) for x in v) if len(v) > 1 else f"{v[0]}.0"


def title_settings(cons, title_text=None):
    """cfg['title'] from `[silk.title]` (or the CLI --title literal), else None."""
    tb = ((cons or {}).get("silk") or {}).get("title")
    if tb is None and title_text is None:
        return None
    t = dict(TITLE_DEFAULTS)
    t.update({k: v for k, v in (tb or {}).items() if k in TITLE_DEFAULTS})
    if title_text is not None:
        t["text"] = title_text
        t["cli"] = True
    t["side"] = str(t["side"]).upper()[:1] if str(t["side"]).lower() != "both" else "both"
    if t["side"] not in ("F", "B", "both"):
        t["side"] = "F"
    t["height_mm"] = max(TITLE_MIN_H, float(t["height_mm"]))
    t["required"] = bool(t["required"])
    t["sides"] = ["F", "B"] if t["side"] == "both" else [t["side"]]
    return t


def resolve_version(cfg, cons, spec, sc, board):
    """-> (version tuple, source text) or None. Order: [silk.title] version, [project] version (constraints),
    the spec JSON's version/revision (needs --spec), the board's title-block revision, a -vNN / _vN.N suffix
    in the board file name."""
    t = cfg.get("title") or {}
    if t.get("version"):
        v = norm_version(t["version"])
        if v:
            return v, "[silk.title] version"
    pv = ((cons or {}).get("project") or {}).get("version")
    if pv and norm_version(pv):
        return norm_version(pv), "[project] version in the constraints"
    if spec:
        for d, nm in ((spec, "spec"), (spec.get("board") if isinstance(spec.get("board"), dict) else {}, "spec board")):
            for k in ("version", "revision"):
                if d.get(k) and norm_version(re.sub(r"(?i)^rev\s*", "", str(d[k]))):
                    return norm_version(re.sub(r"(?i)^rev\s*", "", str(d[k]))), f"{nm} {k}"
    rev = (sc or {}).get("title_rev") or ""
    m = re.fullmatch(r"\s*(?:[vV]|[rR]ev\.?\s?)?(\d+(?:\.\d+)*)\s*", rev)
    if m and norm_version(m.group(1)):
        return norm_version(m.group(1)), "board title-block revision"
    stem = os.path.basename(board or "")
    stem = stem[:-len(".kicad_pcb")] if stem.endswith(".kicad_pcb") else stem
    ms = list(re.finditer(r"(?:^|[-_])[vV](\d+(?:\.\d+)?)(?=$|[-_.])", stem))
    if ms and norm_version(ms[-1].group(1)):
        return norm_version(ms[-1].group(1)), "board file name"
    return None


def title_regex(t):
    """Regex matching any board text that is this title with ANY version."""
    tpl = t["text"]
    parts = re.split(r"(\{company\}|\{version\})", tpl)
    rx = ""
    for p in parts:
        if p == "{company}":
            rx += re.escape(t["company"]).replace(r"\ ", r"\s+") if t["company"] else r".*?"
        elif p == "{version}":
            rx += r"\d+(?:\.\d+)*"
        else:
            segs = _VER_RE.split(p) if False else re.split(r"((?:[vV]|[rR]ev\.?\s?)\d+(?:\.\d+)*)", p)
            for i, sg in enumerate(segs):
                rx += _VER_ANY if re.fullmatch(r"(?:[vV]|[rR]ev\.?\s?)\d+(?:\.\d+)*", sg or "!") and i % 2 else \
                    re.sub(r"(\\ )+", r"\\s+", re.escape(sg))
    return re.compile(r"^\s*" + rx + r"\s*$", re.I | re.S)


def render_title(t, ver):
    """The title text for version `ver` (None when the template needs a version that cannot be resolved)."""
    if "{version}" in t["text"] and ver is None:
        return None
    return t["text"].format(company=t["company"], version=_vshow(ver) if ver else "")


def mark_title(sc, cfg):
    """Flag the board texts that are the title (t['title'] = True) for this cfg."""
    t = cfg.get("title")
    rx = title_regex(t) if t else None
    for x in sc["texts"]:
        x["title"] = bool(rx and x["kind"] == "board" and rx.match(x["text"]))
    return sc


def prepare(board, cons=None, spec=None, title=None, py=None, sc=None):
    """(scene, cfg) with the title settings, resolved version and title texts marked."""
    cfg = settings(cons)
    cfg["title"] = title_settings(cons, title)
    sc = sc or load_scene(board, py)
    cfg["_version"] = resolve_version(cfg, cons, spec, sc, board)
    mark_title(sc, cfg)
    return sc, cfg


def _check_title(sc, cfg, side, out):
    ver = cfg.get("_version")
    t = cfg.get("title")
    bt = [x for x in sc["texts"] if x["kind"] == "board" and (side == "both" or x["side"] == side[0].upper())]
    seen = {}
    for x in bt:
        for tok in version_tokens(x["text"], (t or {}).get("company"), x.get("title")):
            v = norm_version(tok)
            if v:
                seen.setdefault(v, []).append(x)
    for v, xs in sorted(seen.items()):
        if ver and v != ver[0]:
            for x in xs:
                out.append(_fnd("SILK_VERSION_STALE", f"{_txt_label(x)} {x['side']} ({x['pos'][0]:.1f}, {x['pos'][1]:.1f}) "
                                f"says V{_vshow(v)} but this board is V{_vshow(ver[0])} (from {ver[1]})", ["TITLE"] if x.get("title") else [],
                                x["id"], x["pos"][0], x["pos"][1]))
    if len(seen) > 1:
        lst = "; ".join(f"V{_vshow(v)} in " + ", ".join(f"'{x['text'][:20]}'" for x in xs[:2]) for v, xs in sorted(seen.items()))
        out.append(_fnd("SILK_VERSION_STALE", f"board texts disagree with each other: {lst}", ["TITLE"]))
    if t:
        for s in t["sides"]:
            if side != "both" and s != side[0].upper():
                continue
            if not any(x["title"] and x["side"] == s for x in bt):
                why = "" if render_title(t, ver[0] if ver else None) else " (and no version could be resolved for the template)"
                f = _fnd("SILK_TITLE_MISSING", f"no title text on {s}.SilkS matching '{t['text']}'{why}", ["TITLE"])
                f["level"] = FAIL if t["required"] else WARN
                out.append(f)


# ------------------------------------------------------------------ check
def _txt_label(t):
    if t["kind"] == "ref":
        return f"{t['owner']} ref text"
    if t["kind"] == "value":
        return f"{t['owner']} value text"
    if t["kind"] == "fptext":
        return f"{t['owner']} text '{t['text'][:16]}'"
    if t.get("title"):
        return f"title text '{t['text'][:28]}'"
    return f"board text '{t['text'][:16]}'"


def _fnd(code, msg, refs, tid=None, x=None, y=None):
    return {"level": LEVEL[code], "code": code, "msg": msg, "refs": sorted(set(r for r in refs if r)),
            "tid": tid, "x": x, "y": y}


def check(sc, cfg, side="both"):
    """All silkscreen findings for the scene, worst first."""
    lay = Layout(sc, cfg)
    mark_title(sc, cfg)
    for t in sc["texts"]:
        lay.add_text(t)
    out = []
    parts = sc["parts"]
    for t in sc["texts"]:
        if side != "both" and t["side"] != side[0].upper():
            continue
        x, y = t["pos"]
        where = f"{t['side']} ({x:.1f}, {y:.1f})"
        cx, cy = sum(p[0] for p in t["quad"]) / 4.0, sum(p[1] for p in t["quad"]) / 4.0
        own, lab = t["owner"], _txt_label(t)
        res, soft = lay.probe(t["quad"], t["side"], own, t["kind"], self_id=t["id"])
        for code, who in res.items():
            if code == "SILK_OVERLAP":
                if any(w[0] == "text" and w[1] < t["id"] for w in who) and not any(
                        w[0] == "gfx" for w in who):
                    continue                              # pair reported from the lower id
                names = []
                for k, v in who:
                    names.append(_txt_label(sc["texts"][v]) if k == "text" else f"{v or 'board'} silk graphics")
                refs = [own] + [sc["texts"][v]["owner"] if k == "text" else v for k, v in who]
                out.append(_fnd(code, f"{lab} {where} overlaps " + ", ".join(sorted(set(names))[:3]), refs, t["id"], x, y))
            elif code == "SILK_OFF_BOARD":
                out.append(_fnd(code, f"{lab} {where} outside the board or < {cfg['edge_clearance_mm']:g} mm from Edge.Cuts",
                                [own], t["id"], x, y))
            elif code == "SILK_OVER_PAD":
                out.append(_fnd(code, f"{lab} {where} over pad " + ", ".join(sorted(set(who))[:3]),
                                [own] + [w.split(".")[0] for w in who], t["id"], x, y))
            elif code == "SILK_OVER_HOLE":
                out.append(_fnd(code, f"{lab} {where} over hole " + ", ".join(sorted(set(who))[:3]),
                                [own] + [w for w in who if w not in ("via", "npth")], t["id"], x, y))
            elif code == "SILK_UNDER_PART":
                out.append(_fnd(code, f"{lab} {where} {soft['under'] * 100:.0f}% inside the courtyard of {who[0]}",
                                [own, who[0]], t["id"], x, y))
        if t["h"] < cfg["min_height_mm"] - 1e-6 or t["t"] < cfg["min_stroke_mm"] - 1e-6:
            out.append(_fnd("SILK_TOO_SMALL", f"{lab} {where} height {t['h']:.2f} mm / stroke {t['t']:.2f} mm "
                            f"(min {cfg['min_height_mm']:g} / {cfg['min_stroke_mm']:g})", [own], t["id"], x, y))
        if t["kind"] == "ref" and own in parts:
            cr = court_rect(parts[own])
            if cr:
                dd = _dist_rect(cx, cy, cr)
                if dd > cfg["max_ref_distance_mm"]:
                    out.append(_fnd("SILK_FAR_FROM_PART", f"{lab} {where} is {dd:.1f} mm from its part "
                                    f"(max {cfg['max_ref_distance_mm']:g})", [own], t["id"], x, y))
    # footprint graphics
    if cfg.get("check_graphics", True):
        seen = {}
        for gi, gobj in enumerate(sc["gfx"]):
            own = gobj["owner"]
            if side != "both" and gobj["side"] != side[0].upper():
                continue
            for poly in gobj["polys"]:
                bb = _aabb(poly)
                q = [[bb[0], bb[1]], [bb[2], bb[1]], [bb[2], bb[3]], [bb[0], bb[3]]]
                # off board: the polygon's own vertices (an AABB would flag every diagonal inside a corner cut)
                if lay.rings and any(lay.off_board(rect(p[0], p[1], 0.001, 0.001), cfg["edge_clearance_mm"]) for p in poly[::max(1, len(poly) // 8)]):
                    seen.setdefault((own, "SILK_OFF_BOARD"), set()).add(own)
                for _, p in lay.pads[gobj["side"]].query(bb):
                    if p["owner"] != own and _poly_poly(poly, p["poly"]):
                        seen.setdefault((own, "SILK_OVER_PAD"), set()).add(f"{p['owner']}.{p['num']}")
                for _, h in lay.holes[gobj["side"]].query(bb):
                    if h["owner"] != own and _poly_poly(poly, h["poly"]):
                        seen.setdefault((own, "SILK_OVER_HOLE"), set()).add(h["owner"] or h["kind"])
        for (own, code), who in sorted(seen.items()):
            what = {"SILK_OFF_BOARD": "outside the board or too close to Edge.Cuts",
                    "SILK_OVER_PAD": "over pad " + ", ".join(sorted(who)[:3]),
                    "SILK_OVER_HOLE": "over hole " + ", ".join(sorted(who)[:3])}[code]
            out.append(_fnd(code, f"{own or 'board'} silk graphics {what}", [own] + [w.split(".")[0] for w in who]
                            if code != "SILK_OFF_BOARD" else [own]))
    # reference visibility / marks
    ref_on_silk = {t["owner"] for t in sc["texts"] if t["kind"] == "ref"}
    for ref in sorted(parts):
        part = parts[ref]
        if side != "both" and part["side"] != side[0].upper():
            continue
        cls = part_class(ref, part)
        if ref not in ref_on_silk:
            lvl = WARN if cls & {"connector", "testpoint", "switch", "led", "polar"} else INFO
            f = _fnd("SILK_REF_HIDDEN", f"{ref} reference is not on the silkscreen"
                     + (f" ({'/'.join(sorted(cls & {'connector', 'testpoint', 'switch', 'led', 'polar'}))} part)" if lvl == WARN else ""), [ref])
            f["level"] = lvl
            out.append(f)
        if cls & {"polar", "ic"} and part["silk_gfx"] == 0 and part["n_pads"] >= 2 and not cls & {"testpoint"}:
            out.append(_fnd("SILK_MISSING_MARK", f"{ref} ({'/'.join(sorted(cls & {'polar', 'ic'}))}) has no silkscreen "
                            f"graphics, so no pin-1/polarity mark", [ref]))
    _check_title(sc, cfg, side, out)
    for f in out:
        if f.get("tid") is not None and sc["texts"][f["tid"]].get("title") and "TITLE" not in f["refs"]:
            f["refs"] = sorted(set(f["refs"]) | {"TITLE"})
    order = {FAIL: 0, WARN: 1, INFO: 2}
    out.sort(key=lambda f: (order[f["level"]], f["code"], f["msg"]))
    return out


def _poly_poly(a, b):
    ab, bb = _aabb(a), _aabb(b)
    if ab[2] < bb[0] or bb[2] < ab[0] or ab[3] < bb[1] or bb[3] < ab[1]:
        return False
    if any(_inside_poly(p, b) for p in a[::2]) or any(_inside_poly(p, a) for p in b[::2]):
        return True
    for i in range(len(a)):
        for j in range(len(b)):
            if _seg_x(a[i], a[(i + 1) % len(a)], b[j], b[(j + 1) % len(b)]):
                return True
    return False


def counts(findings):
    c = {}
    for f in findings:
        c[f["code"]] = c.get(f["code"], 0) + 1
    return c


def fails(findings):
    return sum(1 for f in findings if f["level"] == FAIL)


# ------------------------------------------------------------------ title placement
def measure_text(text, sizes, mirror, cfg, py=None):
    """pcbnew ink boxes of `text` for each (h, stroke): [{h, t, w, hh, cx, cy}] (centre-justified, angle 0)."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"text": text, "mirror": mirror, "sizes": sizes}, fh)
        pf = fh.name
    try:
        r = subprocess.run([py or PY, HELPER, "measure", pf], capture_output=True, text=True, timeout=120)
    finally:
        os.unlink(pf)
    if MARK not in r.stdout:
        raise RuntimeError("silk measure failed: " + (r.stderr.strip().splitlines() or ["no output"])[-1][:200])
    return json.loads(r.stdout.split(MARK, 1)[1])


def _wmin(rows, k):
    """Sliding minimum of width k along each row (doubling); -> rows of len(row) - k + 1."""
    out = []
    for row in rows:
        cur, span = row, 1
        while span * 2 <= k:
            cur = [a if a < b else b for a, b in zip(cur, cur[span:])]
            span *= 2
        if k > span:
            cur = [a if a < b else b for a, b in zip(cur, cur[k - span:])]
        out.append(cur)
    return out


def _free_map(lay, side, c):
    """Occupancy of the side at c mm resolution: -> (ox, oy, nx, ny, dist rows [mm to nearest obstacle cell])."""
    xs = [p[0] for r in lay.rings for p in r]
    ys = [p[1] for r in lay.rings for p in r]
    ox, oy = math.floor(min(xs) / 0.5) * 0.5, math.floor(min(ys) / 0.5) * 0.5
    nx, ny = int(math.ceil((max(xs) - ox) / c)) + 1, int(math.ceil((max(ys) - oy) / c)) + 1
    occ = [bytearray(nx) for _ in range(ny)]
    for j in range(ny):                                   # outside the outline (even-odd scanline)
        y = oy + (j + 0.5) * c
        xc = []
        for a, b in lay.edges:
            if (a[1] > y) != (b[1] > y):
                xc.append(a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]))
        xc.sort()
        inside = bytearray(nx)
        for k in range(0, len(xc) - 1, 2):
            i0 = max(0, int(math.ceil((xc[k] - ox) / c - 0.5)))
            i1 = min(nx - 1, int(math.floor((xc[k + 1] - ox) / c - 0.5)))
            for i in range(i0, i1 + 1):
                inside[i] = 1
        row = occ[j]
        for i in range(nx):
            if not inside[i]:
                row[i] = 1

    def mark(bb):
        i0, i1 = max(0, int(math.floor((bb[0] - ox) / c))), min(nx - 1, int(math.floor((bb[2] - ox) / c)))
        j0, j1 = max(0, int(math.floor((bb[1] - oy) / c))), min(ny - 1, int(math.floor((bb[3] - oy) / c)))
        for j in range(j0, j1 + 1):
            row = occ[j]
            for i in range(i0, i1 + 1):
                row[i] = 1
    for grid in (lay.pads[side], lay.holes[side], lay.courts[side], lay.gfx[side], lay.texts[side]):
        for bb, _ in grid.items.values():
            mark(bb)
    big = 1e9
    d = [[0.0 if row[i] else big for i in range(nx)] for row in occ]
    dg = 1.4142 * c
    for j in range(ny):                                   # two-pass chamfer distance (mm)
        r0, r1 = d[j], d[j - 1] if j else None
        for i in range(nx):
            v = r0[i]
            if v == 0.0:
                continue
            if i and r0[i - 1] + c < v:
                v = r0[i - 1] + c
            if r1:
                if r1[i] + c < v:
                    v = r1[i] + c
                if i and r1[i - 1] + dg < v:
                    v = r1[i - 1] + dg
                if i + 1 < nx and r1[i + 1] + dg < v:
                    v = r1[i + 1] + dg
            r0[i] = v
    for j in range(ny - 1, -1, -1):
        r0, r1 = d[j], d[j + 1] if j + 1 < ny else None
        for i in range(nx - 1, -1, -1):
            v = r0[i]
            if v == 0.0:
                continue
            if i + 1 < nx and r0[i + 1] + c < v:
                v = r0[i + 1] + c
            if r1:
                if r1[i] + c < v:
                    v = r1[i] + c
                if i and r1[i - 1] + dg < v:
                    v = r1[i - 1] + dg
                if i + 1 < nx and r1[i + 1] + dg < v:
                    v = r1[i + 1] + dg
            r0[i] = v
    return ox, oy, nx, ny, d


def _largest_free_rect(d, c, need, ox, oy):
    """Largest axis-aligned rectangle of cells with distance >= need: (w_mm, h_mm, x_mm, y_mm)."""
    ny, nx = len(d), len(d[0])
    h = [0] * nx
    best = (0, 0, 0, 0, 0)
    for j in range(ny):
        row = d[j]
        for i in range(nx):
            h[i] = h[i] + 1 if row[i] >= need else 0
        st = []
        for i in range(nx + 1):
            cur = h[i] if i < nx else 0
            start = i
            while st and st[-1][1] >= cur:
                s0, hh = st.pop()
                area = hh * (i - s0)
                if area > best[0]:
                    best = (area, i - s0, hh, s0, j - hh + 1)
                start = s0
            st.append((start, cur))
    _, w, hh, i0, j0 = best
    return w * c, hh * c, ox + i0 * c, oy + j0 * c


def _fit_title(sc, cfg, lay, plan, side, py=None):
    """Place the title text on `side` in the clearest legal spot. -> (info dict | None, finding | None)."""
    t = cfg["title"]
    ver = cfg["_version"][0] if cfg.get("_version") else None
    text = render_title(t, ver)
    if text is None:
        return None, _fnd("SILK_TITLE_NO_ROOM", "title text not written: the template needs a version and none could "
                          "be resolved (set [silk.title] version or [project] version)", ["TITLE"])
    mine = [x for x in sc["texts"] if x.get("title") and x["side"] == side]
    mine.sort(key=lambda x: x["id"])
    if not lay.rings:
        return None, None
    hs, h = [], t["height_mm"]
    while h >= TITLE_MIN_H - 1e-9:
        hs.append(round(h, 3))
        h -= cfg["size_step_mm"]
    if hs[-1] > TITLE_MIN_H + 1e-9:
        hs.append(TITLE_MIN_H)
    sizes = [[s, max(cfg["min_stroke_mm"], round(0.15 * s, 3))] for s in hs]
    dims = measure_text(text, sizes, side == "B", cfg, py)
    c = 0.25
    ox, oy, nx, ny, dist = _free_map(lay, side, c)
    clear = max(cfg["edge_clearance_mm"], cfg["pad_clearance_mm"], cfg["hole_clearance_mm"]) + FIT_MARGIN
    best = None
    for s, (h_, tk), m in zip(hs, sizes, dims):
        if m is None:
            continue
        cands = []
        for va in (0, 90):
            bw, bh = (m["w"], m["hh"]) if va == 0 else (m["hh"], m["w"])
            kw, kh = int(math.ceil(bw / c)), int(math.ceil(bh / c))
            if kw > nx or kh > ny:
                continue
            rows = _wmin(dist, kw)                                   # along x
            cols = _wmin([list(r) for r in zip(*rows)], kh)          # along y: cols[i][j]
            for i in range(0, len(cols), 2):
                col = cols[i]
                for j in range(0, len(col), 2):
                    mg = col[j]
                    if mg >= clear + c:
                        cands.append((-mg, va != 0, oy + j * c, ox + i * c, va, i, j, kw, kh, bw, bh))
        cands.sort()
        for cand in cands[:400]:
            _, _, _, _, va, i, j, kw, kh, bw, bh = cand
            cx, cy = ox + (i + kw / 2.0) * c, oy + (j + kh / 2.0) * c
            q = rect(cx, cy, bw / 2.0, bh / 2.0, 0.0)
            res, _soft = lay.probe(q, side, "", "title", margin=FIT_MARGIN)
            if res:
                continue
            qb = _aabb(q)
            if any(_quad_poly(q, poly, qb) for _, (co, poly) in lay.courts[side].query(qb)):
                continue                                              # never under a part body
            mg = 0.0
            while mg < 6.0:                                           # exact clearance margin by probing
                r2, _ = lay.probe(q, side, "", "title", margin=mg + 0.1)
                if r2 or any(_quad_poly(inflate(q, mg + 0.1), poly) for _, (co, poly) in
                             lay.courts[side].query(_aabb(inflate(q, mg + 0.1)))):
                    break
                mg += 0.1
            best = (s, tk, va, cx, cy, q, m, round(mg, 2))
            break
        if best:
            break
    if not best:
        need = clear + c
        w, hh, fx, fy = _largest_free_rect(dist, c, need, ox, oy)
        m0 = dims[0]
        return None, _fnd("SILK_TITLE_NO_ROOM", f"no room for the title '{text}' on {side}.SilkS at {TITLE_MIN_H:g} mm "
                          f"(needs {m0['w']:.1f} x {m0['hh']:.1f} mm clear of pads, holes, texts, courtyards and the edge); "
                          f"largest free rectangle {w:.1f} x {hh:.1f} mm at ({fx:.1f}, {fy:.1f})", ["TITLE"], None, fx, fy)
    s, tk, va, cx, cy, q, m, mg = best
    ang = _stored_angle(va, side)
    a = math.radians(ang)
    ox_, oy_ = m["cx"], m["cy"]
    px, py_ = cx - (ox_ * math.cos(a) + oy_ * math.sin(a)), cy - (-ox_ * math.sin(a) + oy_ * math.cos(a))
    plan["@title:" + side] = {"text": text, "side": side, "x": round(px, 4), "y": round(py_, 4), "angle": ang,
                              "h": s, "w": s, "t": tk, "bidx": mine[0]["bidx"] if mine else None,
                              "drop": [x["bidx"] for x in mine[1:]]}
    nt = {"id": 10 ** 6 + len(plan), "side": side, "quad": q, "title": True, "kind": "board", "owner": ""}
    lay.add_text(nt, q)
    return {"side": side, "text": text, "x": round(cx, 2), "y": round(cy, 2), "h": s, "angle": va,
            "margin_mm": mg, "size": [round(m["w"], 2), round(m["hh"], 2)]}, None


# ------------------------------------------------------------------ fit
def _view_angle(angle):
    return 0 if (abs(angle) % 180.0) < 45.0 or (abs(angle) % 180.0) > 135.0 else 90


def _stored_angle(va, side):
    return float(va) if side == "F" else float((360 - va) % 360)


def _sizes(h0, cfg):
    mn, st = cfg["min_height_mm"], cfg["size_step_mm"]
    top = max(h0, mn)
    s, out = top, []
    while s >= mn - 1e-9:
        out.append(round(s, 3))
        s -= st
    if out[-1] > mn + 1e-9:
        out.append(round(mn, 3))
    if h0 < mn - 1e-9:
        out.append(round(h0, 3))                      # last resort: keep the (too small) original size
    return out


def _thick(t0, h0, s, cfg):
    t = max(cfg["min_stroke_mm"], t0 * s / h0 if h0 else t0)
    return round(min(t, max(0.25 * s, cfg["min_stroke_mm"])), 3)


class _Spec:
    """Ink-box model of one reference text: dimensions at any size, and pos <-> ink centre."""

    def __init__(self, t):
        m = t["model"] or {"x0": -t["h"] * 1.5, "y0": -t["h"] / 2, "x1": t["h"] * 1.5, "y1": t["h"] / 2}
        self.h0, self.w0, self.t0 = t["h"], t["w"], t["t"]
        self.sx = max(1e-6, (m["x1"] - m["x0"]) - self.t0)
        self.sy = max(1e-6, (m["y1"] - m["y0"]) - self.t0)
        self.cx, self.cy = (m["x0"] + m["x1"]) / 2.0, (m["y0"] + m["y1"]) / 2.0

    def dims(self, s, t):
        k = s / self.h0
        return self.sx * k + t, self.sy * k + t

    def pos_for(self, centre, s, angle):
        k = s / self.h0
        a = math.radians(angle)
        c, sn = math.cos(a), math.sin(a)
        ox, oy = self.cx * k, self.cy * k
        return centre[0] - (ox * c + oy * sn), centre[1] - (-ox * sn + oy * c)


def _candidates(t, spec, part, s, tk, va, cfg, stage, pref):
    """Candidate (cost, cx, cy, va) ink-box centres around the part, cheapest first."""
    cr = court_rect(part) if part else None
    if cr is None:
        x, y = t["pos"]
        cr = (x - 0.5, y - 0.5, x + 0.5, y + 0.5)
    wl, hl = spec.dims(s, tk)
    hw, hh = (wl / 2, hl / 2) if va == 0 else (hl / 2, wl / 2)
    x0, y0, x1, y1 = cr
    if stage == "near":
        gaps = [cfg["ref_gap_mm"], cfg["ref_gap_mm"] + 0.3]
        step = 0.4
    else:
        g, gaps = cfg["ref_gap_mm"] + 0.8, []
        while g <= cfg["max_ref_distance_mm"]:
            gaps.append(round(g, 3))
            g += 0.6
        step = 0.8
    mx = (x0 + x1) / 2.0
    my = (y0 + y1) / 2.0
    out = {}

    def put(cx, cy):
        dd = _dist_rect(cx, cy, cr)
        if dd > cfg["max_ref_distance_mm"]:
            return
        key = (round(cx, 2), round(cy, 2))
        cost = dd + (0.6 if va != pref else 0.0)
        if key not in out or out[key][0] > cost:
            out[key] = (cost, cx, cy, va)
    kx = int(math.ceil(((x1 - x0) / 2 + hw) / step))
    ky = int(math.ceil(((y1 - y0) / 2 + hh) / step))
    for d in gaps:
        for i in range(-kx, kx + 1):
            put(mx + i * step, y0 - d - hh)
            put(mx + i * step, y1 + d + hh)
        for j in range(-ky, ky + 1):
            put(x0 - d - hw, my + j * step)
            put(x1 + d + hw, my + j * step)
        for cx, cy in ((x0 - d - hw, y0 - d - hh), (x1 + d + hw, y0 - d - hh),
                       (x0 - d - hw, y1 + d + hh), (x1 + d + hw, y1 + d + hh)):
            put(cx, cy)
    if stage == "near":
        put(t["pos"][0] + 0.0, t["pos"][1] + 0.0)           # the current spot, resized
    return sorted(out.values(), key=lambda c: (c[0], c[2], c[1], c[3]))


def fit(sc, cfg, findings=None, only=None, side="both", log=lambda m: None, title=True, py=None):
    """Re-place reference texts so the check passes. -> {"plan", "moved", "shrunk", "hidden", "failed", "kept"}.
    `only`: set of footprint refs; just offenders involving one of them are touched (write hooks)."""
    findings = findings if findings is not None else check(sc, cfg)
    texts = sc["texts"]
    bad = {}
    for f in findings:
        if f["code"] in FIT_CODES and f.get("tid") is not None:
            ids = [f["tid"]]
            if f["code"] == "SILK_OVERLAP":                               # the partner text too, when it is a reference
                ids += [t["id"] for t in texts if t["kind"] == "ref" and t["owner"] in f["refs"][:] and
                        t["owner"] != texts[f["tid"]]["owner"] and _touches(t, texts[f["tid"]])]
            for i in ids:
                t = texts[i]
                if t["kind"] != "ref" or t["owner"] not in sc["parts"] or (
                        side != "both" and t["side"] != side[0].upper()):
                    continue
                if only is not None and not ({t["owner"]} | set(f["refs"])) & set(only):
                    continue
                bad.setdefault(i, set()).add(f["code"])
    lay = Layout(sc, cfg)
    own_title = bool(title and cfg.get("title"))
    for t in texts:
        if t["id"] not in bad and not (own_title and t.get("title")):
            lay.add_text(t)
    refs_va = {t["owner"]: _view_angle(t["angle"]) for t in texts if t["kind"] == "ref" and t["id"] not in bad}

    def density(t):
        x, y = t["pos"]
        return len(lay.texts[t["side"]].query((x - 5, y - 5, x + 5, y + 5))) + len(lay.pads[t["side"]].query((x - 3, y - 3, x + 3, y + 3)))
    order = sorted(bad, key=lambda i: (0 if bad[i] & {"SILK_OFF_BOARD", "SILK_OVER_PAD", "SILK_OVER_HOLE"} else 1,
                                       -density(texts[i]), texts[i]["owner"]))
    plan, shrunk, pending = {}, set(), []

    def pref_va(t):
        x, y = t["pos"]
        tally = {0: 0, 90: 0}
        for _, (o, _q) in lay.texts[t["side"]].query((x - 10, y - 10, x + 10, y + 10)):
            if o["kind"] == "ref" and o["owner"] in refs_va:
                tally[refs_va[o["owner"]]] += 1
        return 90 if tally[90] > tally[0] else 0

    def place(t):
        spec = _Spec(t)
        part = sc["parts"][t["owner"]]
        pv = pref_va(t)
        for stage in ("near", "ring"):
            for s in _sizes(t["h"], cfg):
                tk = _thick(t["t"], t["h"], s, cfg)
                best = None
                cands = []
                for va in (0, 90):
                    cands += _candidates(t, spec, part, s, tk, va, cfg, stage, pv)
                cands.sort(key=lambda c: (c[0], c[2], c[1], c[3]))
                wl, hl = spec.dims(s, tk)
                for cost, cx, cy, va in cands:
                    if best is not None and cost >= best[0]:
                        break
                    hw, hh = (wl / 2, hl / 2) if va == 0 else (hl / 2, wl / 2)
                    q = rect(cx, cy, hw, hh, 0.0)
                    res, soft = lay.probe(q, t["side"], t["owner"], "ref", margin=FIT_MARGIN)
                    if res:
                        continue
                    total = cost + 3.0 * soft["under"] + 2.0 * soft["own_gfx"]
                    if best is None or total < best[0]:
                        best = (total, cx, cy, va, q, s, tk, spec)
                if best:
                    return best
        return None

    def commit(t, best):
        _, cx, cy, va, q, s, tk, spec = best
        ang = _stored_angle(va, t["side"])
        px, py = spec.pos_for((cx, cy), s, ang)
        k = s / t["h"]
        plan[t["owner"]] = {"x": round(px, 4), "y": round(py, 4), "angle": ang, "h": round(s, 4),
                            "w": round(t["w"] * k, 4), "t": tk}
        nt = dict(t)
        nt["quad"] = q
        lay.add_text(nt, q)
        refs_va[t["owner"]] = va
        if s < t["h"] - 1e-6:
            shrunk.add(t["owner"])

    for pas in (1, 2):
        todo, pending = (order if pas == 1 else pending), []
        for i in todo:
            t = texts[i]
            best = place(t)
            if best:
                commit(t, best)
            else:
                pending.append(i)
    hidden, kept = [], []
    for i in pending:
        t = texts[i]
        res = {}
        if not bad[i] & set(HARD):                        # left where it was: unless texts placed meanwhile now cover it
            res, _ = lay.probe(t["quad"], t["side"], t["owner"], "ref")
        if bad[i] & set(HARD) or set(res) & set(HARD):
            plan[t["owner"]] = {"hide": True}
            hidden.append(t["owner"])
        else:
            kept.append(t["owner"])
            lay.add_text(t)
    tinfo, tfind = {}, []
    if own_title:                                        # AFTER the references: it sees their final places
        for sd in cfg["title"]["sides"]:
            if side != "both" and sd != side[0].upper():
                continue
            info, fnd = _fit_title(sc, cfg, lay, plan, sd, py)
            if info:
                tinfo[sd] = info
            if fnd:
                tfind.append(fnd)
    return {"plan": plan, "moved": sorted(r for r in plan if not r.startswith("@") and "hide" not in plan[r]),
            "shrunk": sorted(shrunk), "hidden": sorted(hidden), "kept": sorted(kept), "offenders": len(bad),
            "title": tinfo, "findings": tfind}


def _touches(a, b):
    return _quads_overlap(inflate(a["quad"], 0.5), inflate(b["quad"], 0.5))


# ------------------------------------------------------------------ driver
def run_check(board, cons=None, side="both", py=None, sc=None, spec=None, title=None):
    sc, cfg = prepare(board, cons, spec, title, py, sc)
    return sc, cfg, check(sc, cfg, side)


def summary_line(findings, prefix="silk"):
    c = counts(findings)
    return (f"{prefix}: {c.get('SILK_OFF_BOARD', 0)} off-board, {c.get('SILK_OVER_PAD', 0) + c.get('SILK_OVER_HOLE', 0)} over pad/hole, "
            f"{c.get('SILK_OVERLAP', 0) + c.get('SILK_UNDER_PART', 0)} overlap/covered")


def fix_board(board, out, cons=None, side="both", only=None, py=None, log=print, spec=None, title=None):
    """check -> fit -> write `out` -> re-check. -> (before, after, result); the NO_ROOM findings are in after."""
    sc, cfg, before = run_check(board, cons, side, py, spec=spec, title=title)
    res = fit(sc, cfg, before, only=only, side=side, log=log)
    if res["plan"]:
        apply_plan(board, out, res["plan"], py)
    elif os.path.abspath(board) != os.path.abspath(out):
        import shutil
        shutil.copy(board, out)
    after = (check(load_scene(out, py), cfg, side) if res["plan"] else before) + res.get("findings", [])
    return before, after, res


def after_write(board, moved, cons=None, log=print, py=None):
    """Write hook (boardwrite.run_post_write): one silk summary line for the footprints just moved and, with
    `[silk] auto_fit` (default true), re-fit their reference texts and any text they now collide with, in place.
    Never raises: a missing pcbnew or a failed fit is one line, the write itself already succeeded."""
    cfg = settings(cons)
    if not cfg["enabled"]:
        return None
    moved = sorted(set(moved or ()))
    try:
        sc, cfg = prepare(board, cons, None, None, py)
        before = check(sc, cfg)
        for f in before:                                  # a revision copy still carrying the old version
            if f["code"] == "SILK_VERSION_STALE":
                log(f"silk: {f['level']} {f['code']} {f['msg']}")
        if not cfg["auto_fit"]:
            log(summary_line(before) + " (auto_fit off)")
            return before
        near = set(moved)
        for f in before:                                  # texts of other parts now colliding with a moved one
            if set(f["refs"]) & set(moved):
                near |= set(f["refs"])
        res = fit(sc, cfg, before, only=near, title=False)
        if not res["plan"]:
            log(summary_line(before))
            return before
        apply_plan(board, board, res["plan"], py)
        after = check(prepare(board, cons, None, None, py)[0], cfg)
        log(summary_line(before) + " -> " + summary_line(after, "").strip(": ").strip()
            + f" ({len(res['moved'])} ref(s) re-placed, {len(res['hidden'])} hidden"
            + (": " + ", ".join(res["hidden"][:6]) if res["hidden"] else "") + ")")
        return after
    except Exception as e:                                # noqa: BLE001 - advisory hook
        log(f"silk: not run ({str(e)[:120]})")
        return None
