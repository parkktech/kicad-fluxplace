"""[lock.at] REF = [x, y, "F"|"B", angle?]: make the lock authoritative before any placement stage.

Every locked footprint is moved (origin) to its row; the pipeline then holds it fixed. Fails loudly (LockError)
when a ref is missing, two locks coincide, or a lock lies outside the board outline.
"""
import os


class LockError(RuntimeError):
    pass


def rows(cons):
    lock = (cons or {}).get("lock") or {}
    return {r: list(v) for r, v in (lock.get("at") or {}).items()}


def apply(board_in, board_out, cons):
    """-> list of (ref, (x0, y0), (x1, y1)) moved. Writes board_out only when something moved."""
    import pcbnew
    want = rows(cons)
    if not want:
        return []
    tol = float((cons.get("lock") or {}).get("tol_mm", 0.01))
    b = pcbnew.LoadBoard(board_in)
    fps = {f.GetReference(): f for f in b.GetFootprints()}
    missing = sorted(r for r in want if r not in fps)
    if missing:
        raise LockError("[lock.at] refs not on the board: " + ", ".join(missing))
    seen = {}
    for r, v in sorted(want.items()):
        k = (round(float(v[0]), 2), round(float(v[1]), 2))
        if k in seen:
            raise LockError(f"[lock.at] {seen[k]} and {r} share the position {k}")
        seen[k] = r
    bb = b.GetBoardEdgesBoundingBox()
    if bb.GetWidth() > 0:
        x0, y0, x1, y1 = (pcbnew.ToMM(v) for v in (bb.GetLeft(), bb.GetTop(), bb.GetRight(), bb.GetBottom()))
        bad = [r for r, v in sorted(want.items()) if not (x0 - tol <= float(v[0]) <= x1 + tol and y0 - tol <= float(v[1]) <= y1 + tol)]
        if bad:
            raise LockError("[lock.at] outside the board outline (%.2f..%.2f, %.2f..%.2f): %s" % (x0, x1, y0, y1, ", ".join(bad)))
    moved = []
    for r, v in sorted(want.items()):
        fp = fps[r]
        p = fp.GetPosition()
        cur = (pcbnew.ToMM(p.x), pcbnew.ToMM(p.y))
        side = next((str(s).upper()[:1] for s in v[2:] if isinstance(s, str)), None)
        ang = next((float(a) for a in v[2:] if not isinstance(a, str)), None)
        dirty = abs(cur[0] - float(v[0])) > tol or abs(cur[1] - float(v[1])) > tol
        if side and ("B" if fp.IsFlipped() else "F") != side:
            fp.Flip(p, False)
            dirty = True
        if ang is not None and abs(fp.GetOrientationDegrees() - ang) > 1e-6:
            fp.SetOrientationDegrees(ang)
            dirty = True
        if dirty:
            fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(float(v[0])), pcbnew.FromMM(float(v[1]))))
            moved.append((r, cur, (float(v[0]), float(v[1]))))
    if moved:
        os.makedirs(os.path.dirname(os.path.abspath(board_out)), exist_ok=True)
        b.Save(board_out)
    return moved
