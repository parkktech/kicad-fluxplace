"""fit-sheet — put the board inside the KiCad drawing sheet.

A board whose coordinates start near (0, 0) or go negative opens half outside
the page frame. This module computes the Edge.Cuts bounding box, picks a paper
size and translates EVERYTHING by a whole-millimetre offset so the outline is
centred in the usable area above the title block.

Numbers (measured 2026-11-11 by exporting a board with kicad-cli 10.0.5 and
reading the frame geometry from the SVG; the default drawing sheet is compiled
into KiCad, there is no .kicad_wks for it under /usr/share/kicad):
  * outer frame 10 mm from every page edge, inner frame 12 mm (2 mm index band)
  * title block 108 x 32 mm, flush with the inner frame's bottom-right corner
    (A4: x 177..285, y 166..198)
Usable area = 10 mm border + 5 mm clearance = 15 mm from every page edge.
The title block keep-out is the block grown by the same 5 mm. A project that
carries a custom drawing sheet is not read; the default one is assumed.

pcbnew 10.0.5 does not wrap PAGE_INFO in SWIG, so the paper cannot be read or
set through the board object: it is read from / patched into the file text
(`read_paper`, `set_paper_text`), after the board has been saved.

`plan()` is pure; `fit()` works on a loaded pcbnew board and does not save.
"""
import math
import re

__all__ = ["PAPERS", "plan", "overlaps_title_block", "usable", "fit", "fit_file",
           "outline_bbox_swig", "read_paper", "page_from_tree", "set_paper_text", "page_dims",
           "rewrite_constraints", "shift_text", "verify_shift", "ConstraintsShiftError", "off_sheet", "BORDER_MM", "CLEAR_MM",
           "TB_W_MM", "TB_H_MM", "FRAME_MM"]

# landscape (w, h) mm, KiCad PAGE_INFO sizes (US sizes: 11x8.5, 17x11, 22x17,
# 34x22 in — confirmed against pcbnew's own page-size table in KiCad 10)
PAPERS = {
    "A5": (210.0, 148.0), "A4": (297.0, 210.0), "A3": (420.0, 297.0),
    "A2": (594.0, 420.0), "A1": (841.0, 594.0), "A0": (1189.0, 841.0),
    "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8),
    "D": (863.6, 558.8),
}
_SERIES = {"iso": ["A5", "A4", "A3", "A2", "A1", "A0"], "us": ["A", "B", "C", "D"]}
BORDER_MM = 10.0      # drawing-sheet outer frame
FRAME_MM = 12.0       # inner frame (title block anchor)
CLEAR_MM = 5.0
TB_W_MM, TB_H_MM = 108.0, 32.0
EPS = 1e-6


def page_dims(paper, portrait=False, user=None):
    """(w, h) mm of a paper name; `user` = (w, h) for the User size."""
    w, h = user if paper == "User" and user else PAPERS[paper]
    return (h, w) if portrait else (w, h)


def usable(w, h):
    """(x0, y0, x1, y1) usable area and the title-block keep-out rect."""
    m = BORDER_MM + CLEAR_MM
    area = (m, m, w - m, h - m)
    tb = (w - FRAME_MM - TB_W_MM - CLEAR_MM, h - FRAME_MM - TB_H_MM - CLEAR_MM,
          w - FRAME_MM + CLEAR_MM, h - FRAME_MM + CLEAR_MM)
    return area, tb


def overlaps_title_block(bbox, w, h):
    _, tb = usable(w, h)
    return not (bbox[2] <= tb[0] + EPS or bbox[0] >= tb[2] - EPS or
                bbox[3] <= tb[1] + EPS or bbox[1] >= tb[3] - EPS)


def off_sheet(bbox, w, h):
    """True when the bbox leaves the usable area or touches the title block."""
    a, _ = usable(w, h)
    outside = (bbox[0] < a[0] - EPS or bbox[1] < a[1] - EPS or
               bbox[2] > a[2] + EPS or bbox[3] > a[3] + EPS)
    return outside or overlaps_title_block(bbox, w, h)


def _targets(bw, bh, w, h):
    """Candidate centres, in preference order, for a bw x bh board."""
    (ux0, uy0, ux1, uy1), tb = usable(w, h)
    out = []
    if bw <= ux1 - ux0 + EPS and bh <= tb[1] - uy0 + EPS:       # above the block
        out.append(((ux0 + ux1) / 2, (uy0 + tb[1]) / 2))
    if bw <= tb[0] - ux0 + EPS and bh <= uy1 - uy0 + EPS:       # left of the block
        out.append(((ux0 + tb[0]) / 2, (uy0 + uy1) / 2))
    return out


def _snap(bbox, tx, ty, w, h):
    """Whole-mm offset toward the target centre that keeps the board clear."""
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    base = (round(tx - cx), round(ty - cy))
    best = None
    for ddx in (0, -1, 1):
        for ddy in (0, -1, 1):
            dx, dy = base[0] + ddx, base[1] + ddy
            nb = (bbox[0] + dx, bbox[1] + dy, bbox[2] + dx, bbox[3] + dy)
            if not off_sheet(nb, w, h):
                d = math.hypot(tx - (nb[0] + nb[2]) / 2, ty - (nb[1] + nb[3]) / 2)
                if best is None or d < best[0] - 1e-9:
                    best = (d, (dx, dy), nb)
    return best


def plan(bbox, paper="auto", current=None, portrait=False, user=None):
    """(paper, (dx, dy), target_bbox) — PURE, no pcbnew.

    bbox: (x0, y0, x1, y1) mm. paper: "auto" or a PAPERS key. `current` is
    the board file's paper: "auto" keeps it when the board fits its usable
    area, else takes the smallest size of the same series (ISO unless the
    current paper is a US size) that fits. An explicit paper that cannot hold
    the board goes up one size at a time within its series. The offset is
    whole millimetres; (0, 0) means already fitted."""
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if paper == "auto":
        series = _SERIES["us"] if current in _SERIES["us"] else _SERIES["iso"]
        known = current in PAPERS or current == "User"
        later = series[series.index(current) + 1:] if current in series else series
        cands = ([current] if known else []) + later
    else:
        if paper not in PAPERS:
            raise ValueError(f"unknown paper {paper!r}; choose auto or one of "
                             f"{', '.join(PAPERS)}")
        series = _SERIES["us"] if paper in _SERIES["us"] else _SERIES["iso"]
        cands = series[series.index(paper):]
    for name in cands:
        w, h = page_dims(name, portrait, user)
        for tx, ty in _targets(bw, bh, w, h):
            got = _snap(bbox, tx, ty, w, h)
            if got:
                _, (dx, dy), nb = got
                return name, (dx, dy), nb
    raise ValueError(f"board {bw:.1f} x {bh:.1f} mm does not fit the largest "
                     f"{'US' if cands[-1] in _SERIES['us'] else 'ISO'} paper")


# --------------------------------------------------------------------------
# paper in the file text
# --------------------------------------------------------------------------
_PAPER_RE = re.compile(r'\(paper\s+"([^"]+)"(?:\s+([0-9.]+)\s+([0-9.]+))?(\s+portrait)?\s*\)')


def read_paper(path):
    """{"paper", "w_mm", "h_mm", "portrait"} from the file text (landscape
    dims unless portrait; `w_mm/h_mm` are the page as drawn)."""
    with open(path, errors="replace") as fh:
        head = fh.read(4000)
    return parse_paper(head)


def parse_paper(text):
    m = _PAPER_RE.search(text)
    if not m:
        return {"paper": "A4", "w_mm": 297.0, "h_mm": 210.0, "portrait": False}
    name, w, h, por = m.group(1), m.group(2), m.group(3), bool(m.group(4))
    if name == "User" and w and h:
        dims = (float(w), float(h))
    else:
        dims = PAPERS.get(name, PAPERS["A4"])
    if por:
        dims = (dims[1], dims[0])
    return {"paper": name, "w_mm": dims[0], "h_mm": dims[1], "portrait": por}


def page_from_tree(root):
    """The same dict from a parsed S-expression tree (boardfile.parse)."""
    for k in root:
        if isinstance(k, list) and k and k[0] == "paper":
            a = [x for x in k[1:] if isinstance(x, str)]
            if not a:
                break
            name, por = a[0], "portrait" in a[1:]
            nums = []
            for x in a[1:]:
                try:
                    nums.append(float(x))
                except ValueError:
                    pass
            dims = tuple(nums[:2]) if name == "User" and len(nums) >= 2 else PAPERS.get(name, PAPERS["A4"])
            if por:
                dims = (dims[1], dims[0])
            return {"paper": name, "w_mm": dims[0], "h_mm": dims[1], "portrait": por}
    return {"paper": "A4", "w_mm": 297.0, "h_mm": 210.0, "portrait": False}


def set_paper_text(path, paper):
    """Rewrite the `(paper "X")` token in place; keeps a `portrait` flag.
    Returns True if the file changed."""
    with open(path) as fh:
        txt = fh.read()
    new, n = re.subn(r'\(paper\s+"[^"]+"(?:\s+[0-9.]+\s+[0-9.]+)?(\s+portrait)?\s*\)',
                     lambda m: f'(paper "{paper}"{m.group(1) or ""})', txt, count=1)
    if not n:                                   # no token: KiCad order is general, paper, ...
        new, n = re.subn(r"\n(\s*)\(layers", lambda m: f'\n{m.group(1)}(paper "{paper}")'
                         f'\n{m.group(1)}(layers', txt, count=1)
    if n and new != txt:
        with open(path, "w") as fh:
            fh.write(new)
        return True
    return False


# --------------------------------------------------------------------------
# pcbnew side
# --------------------------------------------------------------------------
def outline_bbox_swig(board, fallback=True):
    """(bbox_mm, source). Edge.Cuts graphics (stroke included), else (with
    `fallback`) the union of footprint bounding boxes."""
    import pcbnew
    boxes = [d.GetBoundingBox() for d in board.GetDrawings()
             if d.GetLayer() == pcbnew.Edge_Cuts]
    src = "Edge.Cuts"
    if not boxes and fallback:
        boxes = [fp.GetBoundingBox(False) for fp in board.GetFootprints()]
        src = "footprints (no Edge.Cuts outline)"
    if not boxes:
        return None, "empty board"
    x0 = min(b.GetLeft() for b in boxes); y0 = min(b.GetTop() for b in boxes)
    x1 = max(b.GetRight() for b in boxes); y1 = max(b.GetBottom() for b in boxes)
    mm = pcbnew.ToMM
    return (mm(x0), mm(y0), mm(x1), mm(y1)), src


def _items(board):
    """Every top-level item, by kind."""
    kinds = {"footprints": list(board.GetFootprints()),
             "tracks": [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"],
             "arcs": [t for t in board.GetTracks() if t.GetClass() == "PCB_ARC"],
             "vias": [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"],
             "zones": list(board.Zones()),
             "drawings": list(board.GetDrawings()),
             "groups": list(board.Groups()),
             "markers": list(board.Markers()),
             "generators": list(board.Generators())}
    return kinds


def _pos(item):
    p = item.GetPosition()
    return (p.x, p.y)


def snapshot(board):
    """[(kind, uuid-or-index, (x, y), extra)] positions of every item, plus
    first filled-polygon vertex and zone outline vertex."""
    out = []
    for kind, items in _items(board).items():
        for i, it in enumerate(items):
            key = (kind, i)
            if kind in ("tracks", "arcs"):
                s, e = it.GetStart(), it.GetEnd()
                out.append((key, (s.x, s.y)))
                out.append((key + ("end",), (e.x, e.y)))
            elif kind == "zones":
                o = it.Outline()
                if o.OutlineCount():
                    v = o.CVertex(0)
                    out.append((key + ("outline",), (v.x, v.y)))
                try:
                    for layer in it.GetLayerSet().Seq():
                        fp = it.GetFilledPolysList(layer)
                        if fp.OutlineCount():
                            v = fp.CVertex(0)
                            out.append((key + ("fill", int(layer)), (v.x, v.y)))
                except Exception:
                    pass
            else:
                out.append((key, _pos(it)))
    ds = board.GetDesignSettings()
    for nm in ("GetAuxOrigin", "GetGridOrigin"):
        v = getattr(ds, nm)()
        out.append(((nm,), (v.x, v.y)))
    return out


def fit(board, paper="auto", current=None, snap_check=True):
    """Translate the whole board (not saved) so the outline sits inside the
    sheet. Returns a report dict; `report["paper"]` must be written into the
    file after saving (`set_paper_text`) — pcbnew 10 cannot set it."""
    import pcbnew
    cur = current
    if cur is None:
        fn = board.GetFileName()
        cur = read_paper(fn) if fn else {"paper": "A4", "portrait": False,
                                         "w_mm": 297.0, "h_mm": 210.0}
    bbox, src = outline_bbox_swig(board)
    if bbox is None:
        raise ValueError("board has no outline and no footprints")
    user = (cur["w_mm"], cur["h_mm"]) if cur["paper"] == "User" else None
    if user and cur["portrait"]:
        user = (cur["h_mm"], cur["w_mm"])
    name, (dx, dy), target = plan(bbox, paper, current=cur["paper"],
                                  portrait=cur["portrait"], user=user)
    w, h = page_dims(name, cur["portrait"], user)
    rep = {"paper": name, "paper_before": cur["paper"], "paper_changed": name != cur["paper"],
           "portrait": cur["portrait"], "w_mm": w, "h_mm": h, "source": src,
           "usable": usable(w, h)[0], "title_block": usable(w, h)[1],
           "bbox_before": bbox, "bbox_after": target, "offset": (dx, dy),
           "moved": False, "counts": {k: len(v) for k, v in _items(board).items()}}
    if (dx, dy) == (0, 0):
        rep["status"] = "already fitted" if not rep["paper_changed"] else "paper change only"
        return rep
    before = snapshot(board) if snap_check else None
    vec = pcbnew.VECTOR2I(pcbnew.FromMM(dx), pcbnew.FromMM(dy))
    board.Move(vec)
    ds = board.GetDesignSettings()
    for get, setter in (("GetAuxOrigin", "SetAuxOrigin"), ("GetGridOrigin", "SetGridOrigin")):
        o = getattr(ds, get)()
        if o.x or o.y:
            getattr(ds, setter)(pcbnew.VECTOR2I(o.x + vec.x, o.y + vec.y))
    if snap_check:
        after = snapshot(board)
        bad = [k for (k, a), (_, b) in zip(before, after)
               if (b[0] - a[0], b[1] - a[1]) != (vec.x, vec.y)
               and not (k[0] in ("GetAuxOrigin", "GetGridOrigin") and a == (0, 0))]
        if len(before) != len(after) or bad:
            raise RuntimeError(f"fit-sheet: {len(bad)} item(s) did not move with the "
                               f"board, e.g. {bad[:3]} — nothing saved")
        rep["verified_points"] = len(after)
    rep["moved"] = True
    rep["status"] = "fitted"
    nb, _ = outline_bbox_swig(board)
    rep["bbox_after"] = nb
    return rep


def fit_file(path, out=None, paper="auto", log=print, dry_run=False, refill=False,
             constraints=None, date=None):
    """Load, fit, save (to `out` or in place), patch the paper. Returns the
    report. Used by the CLI and by the build commands. With `constraints` (a
    path) the same offset is applied to every registered coordinate key; the
    shift is computed and self-checked BEFORE anything is written, so a failure
    (ConstraintsShiftError) leaves board and constraints untouched. The new
    text is in rep["constraints_text"] / rep["constraints_rows"]; it is written
    to `constraints` after the board is saved (not on dry_run)."""
    import pcbnew
    board = pcbnew.LoadBoard(path)
    rep = fit(board, paper)
    if constraints and rep["offset"] != (0, 0):
        ob, oa = rep["bbox_before"], rep["bbox_after"]
        rep["constraints_text"], rep["constraints_rows"] = rewrite_constraints(
            open(constraints).read(), *rep["offset"], date=date, origin=((ob[0], ob[1]), (oa[0], oa[1])))
    if dry_run:
        if rep["status"] == "fitted":
            rep["status"] = "would fit"
        return rep
    if rep["moved"]:
        if refill:
            pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        pcbnew.SaveBoard(out or path, board)
        from . import boardwrite as _BW
        _BW.run_post_write(out or path, [f.GetReference() for f in board.GetFootprints()])
    elif out and out != path:
        import shutil
        shutil.copyfile(path, out)
    if rep["paper_changed"]:
        set_paper_text(out or path, rep["paper"])
    if rep.get("constraints_text") is not None:
        with open(constraints, "w") as fh:
            fh.write(rep["constraints_text"])
    return rep


def off_sheet_file(path):
    """True/False: is the saved board off its page? None if it cannot tell."""
    import pcbnew
    board = pcbnew.LoadBoard(path)
    bbox, _ = outline_bbox_swig(board)
    if bbox is None:
        return None
    pg = read_paper(path)
    return off_sheet(bbox, pg["w_mm"], pg["h_mm"])


def _bb(b):
    return f"x {b[0]:.1f}..{b[2]:.1f}, y {b[1]:.1f}..{b[3]:.1f}"


def summary_line(rep, label="fit-sheet"):
    if not rep["moved"] and rep["status"] == "already fitted":
        return f"{label}: already fitted on {rep['paper']}"
    return (f"{label}: {rep['paper']}, offset ({rep['offset'][0]:+g}, {rep['offset'][1]:+g}) mm, "
            f"outline {_bb(rep['bbox_after'])}")


def summary(rep, dry_run=False):
    a, tb = rep["usable"], rep["title_block"]
    n = sum(rep["counts"].values())
    rows = [f"fit-sheet{' (dry run)' if dry_run else ''}: {rep['status']}",
            f"  paper        {rep['paper_before']} -> {rep['paper']}"
            + (" (portrait)" if rep["portrait"] else "") + f"  {rep['w_mm']:g} x {rep['h_mm']:g} mm",
            f"  usable area  {_bb(a)}",
            f"  title block  {_bb(tb)} (keep-out incl. clearance)",
            f"  outline src  {rep['source']}",
            f"  bbox before  {_bb(rep['bbox_before'])}",
            f"  bbox target  {_bb(rep['bbox_after'])}",
            f"  offset       ({rep['offset'][0]:+g}, {rep['offset'][1]:+g}) mm"]
    if not dry_run:
        rows.append("  moved        " + (
            ", ".join(f"{v} {k}" for k, v in rep["counts"].items() if v) + f"  ({n} items)"
            if rep["moved"] else "nothing"))
    return rows


# --------------------------------------------------------------------------
# constraints file: shift absolute coordinates textually
# --------------------------------------------------------------------------
# The coordinate-bearing keys come from ONE registry (constraints.COORD_KEYS); this is
# a comment-preserving TOML scanner that finds the number tokens of those keys and
# rewrites only them. Multi-line arrays, inline tables, dotted keys and quoted names
# are followed. Nothing is written unless the before/after self-check passes.
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


class ConstraintsShiftError(ValueError):
    """The shift cannot be proven complete; nothing was written."""


def _shift(tok, d):
    dec = len(tok.split(".")[1].split("e")[0]) if "." in tok else 0
    dec = max(dec, len(repr(float(d)).split(".")[1].rstrip("0")) if "." in repr(float(d)) else 0)
    v = round(float(tok) + d, 6)
    s = f"{v:.{dec}f}"
    return s.lstrip("-") if float(s) == 0 else s


class _Scan:
    """Walks TOML text once; collects (key path, [(start, end, token)]) for every value."""

    def __init__(self, text):
        self.t, self.i, self.n = text, 0, len(text)
        self.vals = []

    def ws(self, nl=True):
        t = self.t
        while self.i < self.n:
            c = t[self.i]
            if c in " \t\r" or (nl and c == "\n"):
                self.i += 1
            elif c == "#":
                while self.i < self.n and t[self.i] != "\n":
                    self.i += 1
            else:
                break

    def string(self):
        t, q = self.t, self.t[self.i]
        if t.startswith(q * 3, self.i):
            j = t.find(q * 3, self.i + 3)
            if j < 0:
                raise ConstraintsShiftError("unterminated multi-line string")
            self.i = j + 3
            return
        j = self.i + 1
        while j < self.n and t[j] != q and t[j] != "\n":
            j += 2 if (q == '"' and t[j] == "\\") else 1
        self.i = j + 1

    def key(self):
        """dotted key up to '=' -> [segments]"""
        segs, t = [], self.t
        while True:
            self.ws(False)
            if t[self.i] in "\"'":
                a = self.i
                self.string()
                segs.append(t[a + 1:self.i - 1])
            else:
                a = self.i
                while self.i < self.n and (t[self.i].isalnum() or t[self.i] in "_-"):
                    self.i += 1
                if a == self.i:
                    raise ConstraintsShiftError(f"cannot parse a key at offset {a}")
                segs.append(t[a:self.i])
            self.ws(False)
            if self.i < self.n and t[self.i] == ".":
                self.i += 1
                continue
            return segs

    def value(self, path, toks):
        """parse one value; numbers under it append to `toks`; inline-table members register themselves."""
        self.ws()
        t, c = self.t, self.t[self.i]
        if c in "\"'":
            self.string()
        elif c == "[":
            self.i += 1
            while True:
                self.ws()
                if t[self.i] == "]":
                    self.i += 1
                    return
                self.value(path, toks)
                self.ws()
                if t[self.i] == ",":
                    self.i += 1
        elif c == "{":
            self.i += 1
            while True:
                self.ws()
                if t[self.i] == "}":
                    self.i += 1
                    return
                seg = self.key()
                self.ws(False)
                self.i += 1                                  # '='
                sub = []
                self.value(tuple(path) + tuple(seg), sub)
                self.vals.append((tuple(path) + tuple(seg), sub))
                self.ws()
                if t[self.i] == ",":
                    self.i += 1
        else:
            a = self.i
            while self.i < self.n and t[self.i] not in ",]}#\n \t\r":
                self.i += 1
            tok = t[a:self.i]
            if _NUM_RE.fullmatch(tok):
                toks.append((a, self.i, tok))

    def run(self):
        t, table = self.t, []
        while True:
            self.ws()
            if self.i >= self.n:
                return
            if t[self.i] == "[":
                dbl = t.startswith("[[", self.i)
                self.i += 2 if dbl else 1
                table = self.key()
                self.ws(False)
                self.i += 2 if dbl else 1
                continue
            seg = self.key()
            self.ws(False)
            if self.i >= self.n or t[self.i] != "=":
                raise ConstraintsShiftError(f"expected '=' after key {'.'.join(seg)!r}")
            self.i += 1
            toks = []
            self.value(tuple(table) + tuple(seg), toks)
            self.vals.append((tuple(table) + tuple(seg), toks))


def shift_text(text, dx, dy):
    """(new_text, n_keys): every number of every registered coordinate key shifted; all other bytes kept."""
    from . import constraints as C
    sc = _Scan(text)
    sc.run()
    edits, nkeys = [], 0
    for path, toks in sc.vals:
        kind = C.coord_kind(path)
        if not kind or not toks:
            continue
        if kind == "xy2":
            toks = toks[:2]
        nkeys += 1
        for k, (a, b, tok) in enumerate(toks):
            edits.append((a, b, _shift(tok, dx if k % 2 == 0 else dy)))
    out = text
    for a, b, new in sorted(edits, reverse=True):
        out = out[:a] + new + out[b:]
    return out, nkeys


def verify_shift(before, after, dx, dy, origin_before=None, origin_after=None, tol=1e-6):
    """Self-check on two PARSED constraints dicts: every coordinate key keeps its offset from the
    board-outline origin (origin_after - origin_before = (dx, dy) is itself asserted). Raises
    ConstraintsShiftError on the first mismatch."""
    from . import constraints as C
    ob = origin_before or (0.0, 0.0)
    oa = origin_after or (ob[0] + dx, ob[1] + dy)
    if abs(oa[0] - ob[0] - dx) > 1e-3 or abs(oa[1] - ob[1] - dy) > 1e-3:
        raise ConstraintsShiftError(
            f"board outline origin moved by ({oa[0] - ob[0]:+g}, {oa[1] - ob[1]:+g}) mm, not the "
            f"({dx:+g}, {dy:+g}) mm the constraints were shifted by")
    a, b = C.coord_items(before), C.coord_items(after)
    if [k for k, _ in a] != [k for k, _ in b]:
        raise ConstraintsShiftError(f"coordinate keys differ after the shift ({len(a)} before, {len(b)} after)")
    for (k, (x0, y0)), (_, (x1, y1)) in zip(a, b):
        if abs((x1 - oa[0]) - (x0 - ob[0])) > tol or abs((y1 - oa[1]) - (y0 - ob[1])) > tol:
            raise ConstraintsShiftError(
                f"{k}: ({x0:g}, {y0:g}) -> ({x1:g}, {y1:g}) does not keep its offset from the board origin")
    return len(a)


def rewrite_constraints(text, dx, dy, date=None, origin=None):
    """(new_text, n_changed). Shifts every coordinate key of constraints.COORD_KEYS by (dx, dy) in
    place (comments, spacing, ordering untouched) and appends a closing comment line recording the
    shift. `origin` = (board outline origin before, after) for the offset check. A zero shift returns
    the text unchanged. Raises ConstraintsShiftError — writing nothing — when a coordinate-looking key is
    not in the registry or the before/after self-check fails."""
    import datetime
    import tomllib
    from . import constraints as C
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConstraintsShiftError(f"constraints do not parse: {e}")
    bad = C.unregistered_coords(before)
    if bad:
        raise ConstraintsShiftError(
            "coordinate-looking key(s) not in constraints.COORD_KEYS, so a shift cannot move them: "
            + ", ".join(bad) + " — register them (or rename), nothing written")
    new, n = shift_text(text, dx, dy)
    if (dx, dy) == (0, 0):
        return text, n
    try:
        after = tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise ConstraintsShiftError(f"shifted constraints do not parse: {e}")
    ob, oa = origin if origin else (None, None)
    verify_shift(before, after, dx, dy, ob, oa)
    date = date or datetime.date.today().isoformat()
    if not new.endswith("\n"):
        new += "\n"
    new += (f"# fit-sheet {date}: all absolute coordinates shifted by "
            f"({dx:+g}, {dy:+g}) mm\n")
    return new, n
