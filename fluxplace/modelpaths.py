"""Are the board's 3D model paths resolvable? A copy of a board in a work folder breaks every `${KIPRJMOD}`-relative
path (no lib/ beside the folder), and the parts then show no body. `${KICAD*_3DMODEL_DIR}` paths are KiCad library
paths and are skipped; every other path resolves against `${KIPRJMOD}` (the board's own folder) or is taken as is."""
import os
import re
import shutil

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


def project_libs(board_dir, levels=6):
    """Every model library on the way up from the board's folder (nearest first), real paths. A board inside a delivery folder
    sees the delivery's own (partial) lib first and the source project's after it."""
    out, d = [], os.path.abspath(board_dir)
    for _ in range(levels + 1):
        for parts in LIB_DIRS:
            c = os.path.join(d, *parts)
            if os.path.isdir(c) and os.path.realpath(c) not in out:
                out.append(os.path.realpath(c))
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    return out


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


# ------------------------------------------------------------------ self-contained delivery
_STOCK = re.compile(r"\$\{(KICAD\w*_3DMODEL_DIR|KISYS3DMOD)\}")
_STOCK_DEFAULT = "/usr/share/kicad/3dmodels"
_TAIL = "3dmodels/"


def lib_roots(board_dir, lib_dirs=()):
    """The source project's 3D model libraries, nearest first: `<d>/3dmodels` for each lib dir given, then the board's own `project_lib`."""
    out = []
    for d in list(lib_dirs) + [None]:
        c = os.path.join(d, "3dmodels") if d else project_lib(board_dir)
        if c and os.path.isdir(c) and os.path.realpath(c) not in out:
            out.append(os.path.realpath(c))
    return out


def _by_name(path, roots):
    """A library file for a model path that resolves nowhere: the same file name, else the one carrying the path's part number
    (`43650-0315` in `Molex_..._43650-0315_1x03...step` finds `MOLEX_43650-0315.step`), searched in every library root."""
    base = os.path.basename(path).lower()
    toks = set(re.findall(r"\d{3,}-\d{2,}", base))
    files = [os.path.join(r, f) for root in roots for r, _d, fs in os.walk(root) for f in sorted(fs)
             if f.lower().endswith((".step", ".stp", ".wrl"))]
    return next((f for f in files if os.path.basename(f).lower() == base), None) or \
        (next((f for f in files if any(t in os.path.basename(f) for t in toks)), None) if toks else None)


def source_model(path, board_dir, roots):
    """-> (file, kind) for a model path as written in the SOURCE board: the file it names, resolved against the source project first
    (`${KIPRJMOD}`, an ancestor folder, then the same `3dmodels/` tail under each library root); kind "lib" | "stock" | "ext".
    (None, None) when nothing resolves."""
    m = _STOCK.match(path)
    if m:
        base = os.environ.get(m.group(1)) or _STOCK_DEFAULT
        f = os.path.normpath(path.replace(m.group(0), base))
        if os.path.isfile(f):
            return f, "stock"
        f = _by_name(path, roots)                       # not in this machine's stock library: the project library's copy
        return (f, "lib") if f else (None, None)
    if "${" in path and "${KIPRJMOD}" not in path:
        return None, None
    f = _resolve(path, board_dir)
    if not os.path.isfile(f):
        hit = ancestor_hit(path, board_dir)
        f = hit[0] if hit else None
    if not f and _TAIL in path:
        tail = path.split(_TAIL, 1)[1]
        f = next((os.path.join(r, tail) for r in roots if os.path.isfile(os.path.join(r, tail))), None)
    if not f:
        return None, None
    return f, ("lib" if any(os.path.realpath(f).startswith(r + os.sep) for r in roots) else "ext")


def bundle_models(board_text, board_dir, out_dir, lib_dirs=(), log=print):
    """Make a delivered board self-contained: copy every model file the board names into `<out_dir>/lib/3dmodels/` (library files keep their
    path under the library, KiCad stock models go under `kicad/<name>`, anything else under `ext/`) and rewrite each path to
    `${KIPRJMOD}/lib/3dmodels/...`. -> (new text, {"copied": n, "unresolved": [paths as written]}). Paths that resolve nowhere stay as written."""
    roots = lib_roots(board_dir, lib_dirs)
    dst_root = os.path.join(out_dir, "lib", "3dmodels")
    done, bad, n = {}, set(), 0

    def one(m):
        nonlocal n
        p = m.group(1)
        if p in done:
            return m.group(0).replace(p, done[p])
        f, kind = source_model(p, board_dir, roots)
        if not f:
            bad.add(p)
            return m.group(0)
        real = os.path.realpath(f)
        if kind == "lib":
            rel = os.path.relpath(real, next(r for r in roots if real.startswith(r + os.sep)))
        elif kind == "stock":
            rel = "kicad/" + os.path.relpath(f, os.environ.get(_STOCK.match(p).group(1)) or _STOCK_DEFAULT)
        else:
            rel = "ext/" + os.path.basename(f)
        dst = os.path.join(dst_root, rel)
        if not (os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(real)):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(real, dst)
            n += 1
        done[p] = "${KIPRJMOD}/lib/3dmodels/" + rel.replace(os.sep, "/")
        return m.group(0).replace(p, done[p])

    text = _MODEL.sub(one, board_text)
    if bad:
        log("WARN MODEL_BUNDLE_UNRESOLVED: %d model path(s) resolve nowhere in the source project (e.g. %s)" % (len(bad), sorted(bad)[0]))
    return text, {"copied": n, "unresolved": sorted(bad)}
