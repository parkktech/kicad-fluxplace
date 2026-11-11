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
           "rewrite_constraints", "off_sheet", "BORDER_MM", "CLEAR_MM",
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


def fit_file(path, out=None, paper="auto", log=print, dry_run=False, refill=False):
    """Load, fit, save (to `out` or in place), patch the paper. Returns the
    report. Used by the CLI and by the build commands."""
    import pcbnew
    board = pcbnew.LoadBoard(path)
    rep = fit(board, paper)
    if dry_run:
        if rep["status"] == "fitted":
            rep["status"] = "would fit"
        return rep
    if rep["moved"]:
        if refill:
            pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        pcbnew.SaveBoard(out or path, board)
    elif out and out != path:
        import shutil
        shutil.copyfile(path, out)
    if rep["paper_changed"]:
        set_paper_text(out or path, rep["paper"])
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
_NUM = r"[-+]?\d+(?:\.\d+)?"
_AT_RE = re.compile(rf"^(\s*[\w.\"-]+\s*=\s*\[\s*)({_NUM})(\s*,\s*)({_NUM})(.*)$")
_RECT_RE = re.compile(rf"^(\s*rect\s*=\s*\[\s*)({_NUM})(\s*,\s*)({_NUM})(\s*,\s*)({_NUM})(\s*,\s*)({_NUM})(.*)$")


def _shift(tok, d):
    dec = len(tok.split(".")[1]) if "." in tok else 0
    v = round(float(tok) + d, 6)
    s = f"{v:.{dec}f}"
    return s.lstrip("-") if float(s) == 0 else s


def rewrite_constraints(text, dx, dy, date=None):
    """(new_text, n_changed). Shifts `[lock.at]` REF = [x, y, ...] rows and
    `rect = [x0, y0, x1, y1]` rows inside `[[keepout]]` tables, in place; all
    other bytes (comments, spacing, ordering) are untouched. A closing comment
    line records the shift."""
    import datetime
    date = date or datetime.date.today().isoformat()
    lines, table, n = text.split("\n"), "", 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("["):
            table = s.split("#")[0].strip()
            continue
        if table == "[lock.at]":
            m = _AT_RE.match(ln)
            if m:
                lines[i] = (m.group(1) + _shift(m.group(2), dx) + m.group(3) +
                            _shift(m.group(4), dy) + m.group(5))
                n += 1
        elif table == "[[keepout]]" and re.match(r"\s*rect\s*=", ln):
            m = _RECT_RE.match(ln)
            if not m:
                raise ValueError(f"line {i + 1}: multi-line or unparsable keepout rect; "
                                 f"shift it by hand")
            g = m.groups()
            lines[i] = (g[0] + _shift(g[1], dx) + g[2] + _shift(g[3], dy) + g[4] +
                        _shift(g[5], dx) + g[6] + _shift(g[7], dy) + g[8])
            n += 1
    out = "\n".join(lines)
    if not out.endswith("\n"):
        out += "\n"
    out += (f"# fit-sheet {date}: all absolute coordinates shifted by "
            f"({dx:+g}, {dy:+g}) mm\n")
    return out, n
