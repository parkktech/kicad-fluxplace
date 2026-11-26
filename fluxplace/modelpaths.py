"""Are the board's 3D model paths resolvable? A copy of a board in a work folder breaks every `${KIPRJMOD}`-relative
path (no lib/ beside the folder), and the parts then show no body. `${KICAD*_3DMODEL_DIR}` paths are KiCad library
paths and are skipped; every other path resolves against `${KIPRJMOD}` (the board's own folder) or is taken as is."""
import os
import re

_MODEL = re.compile(r'\(model\s+"([^"]+)"')
FIX = ("keep the board beside its lib/ folder (write the work copy inside the project), or make the model paths "
       "absolute; then rerun")


def _resolve(path, board_dir):
    if "${KIPRJMOD}" in path:
        return os.path.normpath(path.replace("${KIPRJMOD}", board_dir))
    if "${" in path:
        return None                                  # a KiCad library variable: not a file this board owns
    return os.path.normpath(os.path.join(board_dir, path))


ANCESTOR_LEVELS = 4


def ancestor_hit(path, board_dir, resolve=None, levels=ANCESTOR_LEVELS):
    """A `${KIPRJMOD}` path that misses the board's own folder, retried with each ancestor folder of the board
    (1..levels up, nearest first) as `${KIPRJMOD}`. -> (file, folder) of the first hit, or None. The run folder
    of a board is one level deeper than the folder its lib/ paths were written for. `resolve(path, folder)`
    defaults to `_resolve`."""
    if "${KIPRJMOD}" not in path:
        return None
    resolve = resolve or _resolve
    d = os.path.abspath(board_dir)
    for _ in range(levels):
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
        f = resolve(path, d)
        if f and os.path.isfile(f):
            return f, d
    return None


LIB_DIRS = (("lib", "3dmodels"), ("hardware", "lib", "3dmodels"))


def project_lib(board_dir, levels=6):
    """The project's 3D model library: the nearest `lib/3dmodels` (or `hardware/lib/3dmodels`) folder in the board's folder
    or an ancestor (up to `levels`), real path. None when the project has none."""
    d = os.path.abspath(board_dir)
    for _ in range(levels + 1):
        for parts in LIB_DIRS:
            c = os.path.join(d, *parts)
            if os.path.isdir(c):
                return os.path.realpath(c)
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    return None


def outside_lib(path_as_written, full, board_dir):
    """True when the project has a model library and this model file lies outside it. A `${KICAD*_3DMODEL_DIR}` path
    (the installed KiCad stock library) is not a project path and is judged by whether it resolves, not by where."""
    if "${KICAD" in path_as_written:
        return False
    lib = project_lib(board_dir)
    if not lib:
        return False
    real = os.path.realpath(full)
    return not (real == lib or real.startswith(lib + os.sep))


def scan(board):
    """-> {"total": n model paths, "unresolved": [path as written]} for a .kicad_pcb file. A path that resolves
    through an ancestor folder (ancestor_hit) is resolved."""
    with open(board, encoding="utf-8", errors="replace") as fh:
        paths = _MODEL.findall(fh.read())
    d = os.path.dirname(os.path.abspath(board))
    bad, total = [], 0
    for p in paths:
        r = _resolve(p, d)
        if r is None:
            continue
        total += 1
        if not os.path.isfile(r) and ancestor_hit(p, d) is None:
            bad.append(p)
    return {"total": total, "unresolved": sorted(set(bad))}


def warn_lines(res):
    """WARN lines for a scan result ([] when every path resolves)."""
    if not res["unresolved"]:
        return []
    return [f"WARN MODEL_PATH_UNRESOLVED: {len(res['unresolved'])} of {res['total']} 3D model path(s) do not resolve "
            f"(e.g. {res['unresolved'][0]}); fix: {FIX}"]
