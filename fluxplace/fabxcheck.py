"""FAB_BOM_POS_MISMATCH: does the placement file and the BOM agree with the board?

A footprint on the board is expected in:
  * the pick-and-place (.pos) file, unless it is DNP or exclude_from_pos_files;
  * the BOM, unless it is exclude_from_bom.
(DNP parts are still in the BOM, as the BOM tells the buyer what to leave off.)
Each expected ref must appear EXACTLY once. Also reported: refs in pos/BOM that
are not on the board, refs in pos that the board excludes, side and rotation
disagreements between the .pos and the board.

Pure text parsing of the .kicad_pcb (no pcbnew import), like fab.py.
"""
import csv
import re

CODE = "FAB_BOM_POS_MISMATCH"
_TOK = re.compile(r'\(|\)|"((?:[^"\\]|\\.)*)"|([^\s()"]+)')
_LIST_CAP = 20
_ROT_TOL = 0.01


def _parse(text):
    """KiCad s-expression -> nested lists of str (quotes stripped)."""
    stack = [[]]
    for m in _TOK.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append([])
        elif t == ")":
            node = stack.pop()
            stack[-1].append(node)
        else:
            stack[-1].append(m.group(1) if m.group(1) is not None else m.group(2))
    return stack[0][0] if stack[0] else []


def board_footprints(board_path):
    """{ref: {layer, rot, dnp, excl_pos, excl_bom}} for every footprint on the board,
    plus the list of refs that occur twice on the board."""
    root = _parse(open(board_path, encoding="utf-8", errors="replace").read())
    fps, dupes = {}, []
    for node in root[1:]:
        if not (isinstance(node, list) and node and node[0] == "footprint"):
            continue
        kids = [c for c in node[1:] if isinstance(c, list) and c]
        attr = set()
        layer, rot, ref = "", 0.0, None
        for c in kids:
            if c[0] == "attr":
                attr.update(x for x in c[1:] if isinstance(x, str))
            elif c[0] == "layer" and len(c) > 1:
                layer = c[1]
            elif c[0] == "at" and len(c) > 3:
                rot = float(c[3])
            elif c[0] == "property" and len(c) > 2 and c[1] == "Reference":
                ref = c[2]
            elif c[0] == "fp_text" and len(c) > 2 and c[1] == "reference" and ref is None:
                ref = c[2]        # KiCad 5-7 footprint text, when no property form exists
        if not ref or ref.startswith("#"):
            continue
        if ref in fps:
            dupes.append(ref)
        fps[ref] = {"layer": layer, "rot": rot,
                    "dnp": "dnp" in attr,
                    "excl_pos": "exclude_from_pos_files" in attr,
                    "excl_bom": "exclude_from_bom" in attr}
    return fps, dupes


def pos_rows(pos_path):
    """[(ref, side, rot)] from a KiCad CSV position file (Ref,Val,Package,PosX,PosY,Rot,Side)."""
    with open(pos_path, newline="", encoding="utf-8", errors="replace") as f:
        rd = csv.DictReader(f)
        cols = {c.strip().lower(): c for c in (rd.fieldnames or [])}
        if "ref" not in cols:
            raise ValueError("position file has no Ref column")
        rows = []
        for r in rd:
            ref = (r.get(cols["ref"]) or "").strip()
            side = (r.get(cols.get("side", ""), "") or "").strip().lower()
            try:
                rot = float(r.get(cols.get("rot", ""), "") or 0)
            except ValueError:
                rot = 0.0
            if ref:
                rows.append((ref, side, rot))
    return rows


_BOM_REF_COLS = ("references", "reference(s)", "designator", "designators",
                 "refs", "ref", "refdes", "reference")


_RANGE_RX = re.compile(r"^([A-Za-z]+)(\d+)-([A-Za-z]+)?(\d+)$")


def _expand_ref(tok):
    """`R1-R4` / `R1-4` -> R1..R4 (same prefix, ascending). Anything else is returned as written."""
    m = _RANGE_RX.match(tok)
    if not m:
        return [tok]
    pre, a, pre2, b = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
    if pre2 and pre2 != pre:
        return [tok]
    if b < a or b - a > 500:
        return [tok]
    return [f"{pre}{i}" for i in range(a, b + 1)]


def bom_refs(bom_path):
    """All reference designators listed in a BOM CSV (KiCad/JLCPCB/generic). The header row is searched in the
    first 10 rows; `R1-R4` ranges expand to R1..R4 (same prefix only)."""
    with open(bom_path, newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    hdr = None
    for i, r in enumerate(rows[:10]):
        head = [h.strip().lower() for h in r]
        if any(c in head for c in _BOM_REF_COLS) or any("ref" in h or "designator" in h for h in head):
            hdr = i
            break
    if hdr is None:
        raise ValueError("BOM has no reference-designator column")
    head = [h.strip().lower() for h in rows[hdr]]
    col = next((head.index(c) for c in _BOM_REF_COLS if c in head), None)
    if col is None:
        col = next((i for i, h in enumerate(head) if "ref" in h or "designator" in h), None)
    refs = []
    for r in rows[hdr + 1:]:
        if len(r) > col:
            for x in re.split(r"[,\s;]+", r[col]):
                if x:
                    refs += _expand_ref(x)
    return refs


def _fmt(refs):
    refs = sorted(refs)
    s = ", ".join(refs[:_LIST_CAP])
    return s + (f" (+{len(refs) - _LIST_CAP} more)" if len(refs) > _LIST_CAP else "")


def _dupes(refs):
    seen, out = set(), set()
    for r in refs:
        (out if r in seen else seen).add(r)
    return out


def _angle_diff(a, b):
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def check(board, pos=None, bom=None):
    """List of FAIL messages (each prefixed with the code). Empty list = pass.
    board: .kicad_pcb. pos: position CSV or None (skipped). bom: BOM CSV or None (skipped)."""
    fps, board_dupes = board_footprints(board)
    msgs = []
    for r in sorted(set(board_dupes)):
        msgs.append(f"duplicate reference on the board: {r}")

    if pos:
        try:
            rows = pos_rows(pos)
        except (OSError, ValueError) as e:
            return msgs + [f"cannot read position file {pos}: {e}"]
        refs = [r for r, _, _ in rows]
        expected = {r for r, f in fps.items() if not f["dnp"] and not f["excl_pos"]}
        _cross(msgs, "pos", refs, expected, fps, excluded={
            r for r, f in fps.items() if f["dnp"] or f["excl_pos"]})
        for r in sorted(set(refs) & set(fps)):
            side_b = {"F.Cu": "top", "B.Cu": "bottom"}.get(fps[r]["layer"], fps[r]["layer"])
            for pr, side, rot in rows:
                if pr != r:
                    continue
                if side and side != side_b:
                    msgs.append(f"side mismatch {r}: pos={side} board={side_b}")
                if _angle_diff(rot, fps[r]["rot"]) > _ROT_TOL:
                    msgs.append(f"rotation mismatch {r}: pos={rot:g} board={fps[r]['rot']:g}")
                break

    if bom:
        try:
            refs = bom_refs(bom)
        except (OSError, ValueError) as e:
            return msgs + [f"cannot read BOM {bom}: {e}"]
        expected = {r for r, f in fps.items() if not f["dnp"] and not f["excl_bom"]}
        _cross(msgs, "BOM", refs, expected, fps, excluded={
            r for r, f in fps.items() if f["excl_bom"]})
    return [f"{CODE}: {m}" for m in msgs]


def _cross(msgs, label, refs, expected, fps, excluded):
    present = set(refs)
    missing = expected - present
    if missing:
        msgs.append(f"missing from {label}: {_fmt(missing)}")
    extra = present - set(fps)
    if extra:
        msgs.append(f"in {label} but not on the board: {_fmt(extra)}")
    bad = present & excluded
    if bad:
        msgs.append(f"in {label} but excluded on the board (DNP / exclude flag): {_fmt(bad)}")
    dup = _dupes(refs)
    if dup:
        msgs.append(f"duplicate in {label}: {_fmt(dup)}")
