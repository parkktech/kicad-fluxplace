"""3D models at project start (the `ready` stage): every non-hole footprint has a model, bound, resolving, sourced.

Per footprint, in order:
  1. ok          a model is bound and resolves (project lib, or the installed KiCad stock library).
  2. bound now   not bound / unresolved, but the project already has one: a footprint-library entry of the same name that
                 resolves (its transform is the verified one), a `bindings` row in the sources file (model_sources.json),
                 or a model file in the project lib named for the part's MPN (transform 0: "orientation unverified").
  3. fetched now none in the project: the source chain (modelchain.resolve: project library, DigiKey media, EasyEDA/LCSC by
                 LCSC code, manufacturer) -> project lib, provenance row in the sources file, bound, "orientation unverified".
                 Offline (`--no-api`): "fetch pending (offline)".
  4. owner       still none (login-gated site, no source): the part, MPN and the page to download from. The model gate stays
                 FAIL until it is solved or waived with `[review] waive = ["MODEL_MISSING:^REF "]` and a reason.
Only the `(model ...)` entries of the board change. Board-agnostic: the project lib is found from the board's location."""
import datetime
import glob
import json
import os
import re

from . import modelpaths as MP

_MODEL = re.compile(r'\(model\s+"([^"]+)"')
WAIVE_CODES = ("MODEL_MISSING", "MODEL_FILE_MISSING", "MODEL_OUTSIDE_LIB", "MODEL_STANDIN")


def waived(ref, cons, footprint=""):
    """True when `[review] waive` names a model code for this part."""
    from . import review as R
    ws = list(((cons or {}).get("review") or {}).get("waive") or [])
    return any(R._waived({"code": c, "msg": f"{ref} footprint {footprint} 3D model", "refs": [ref]}, ws) for c in WAIVE_CODES)


# ------------------------------------------------------------------ project files
def find_sources(lib_dir):
    d = os.path.abspath(lib_dir)
    for _ in range(5):
        d = os.path.dirname(d)
        for rel in (os.path.join("tools", "model_sources.json"), os.path.join("hardware", "tools", "model_sources.json"),
                    "model_sources.json"):
            if os.path.isfile(os.path.join(d, rel)):
                return os.path.join(d, rel)
    return None


def _entries(txt):
    """-> [(path, offset, scale, rotate)] of a footprint file's / block's model entries."""
    out = []
    for m in re.finditer(r'\(model\s+"([^"]+)"', txt):
        body = txt[m.start():_block_end(txt, m.start())]

        def vec(k, d):
            mm = re.search(r"\(%s\s*\(xyz\s+([-\d.eE+ ]+)\)" % k, body)
            return tuple(float(x) for x in mm.group(1).split()) if mm else d
        out.append((m.group(1), vec("offset", (0.0, 0.0, 0.0)), vec("scale", (1.0, 1.0, 1.0)), vec("rotate", (0.0, 0.0, 0.0))))
    return out


def _lib_footprint_binding(fpname, lib_dir, board_dir):
    """A footprint-library entry of this name carrying a model that resolves -> (file, offset, scale, rotate)."""
    root = os.path.dirname(os.path.abspath(lib_dir))
    for pat in ("*.pretty", os.path.join("*", "*.pretty"), os.path.join("*", "*", "*.pretty")):
        for pretty in sorted(glob.glob(os.path.join(root, pat))):
            f = os.path.join(pretty, fpname + ".kicad_mod")
            if not os.path.isfile(f):
                continue
            for path, off, sc, rot in _entries(open(f, encoding="utf-8", errors="replace").read()):
                full = _resolve_in(path, board_dir)
                if full:
                    return full, off, sc, rot
    return None


def _resolve_in(path, board_dir):
    from . import review as R
    full = R.resolve_model(path, os.path.join(board_dir, "_"))
    if os.path.isfile(full):
        return full
    hit = MP.ancestor_hit(path, board_dir, resolve=lambda q, d: R.resolve_model(q, os.path.join(d, "_")))
    return hit[0] if hit else None


def kiprjmod_path(file, lib_dir, board_dir):
    """The path written into the board: `${KIPRJMOD}/../lib/3dmodels/<rel>` when the project lib is `lib/3dmodels` (the form
    stage copies resolve through the ancestor search), else relative to the board's folder. Never absolute."""
    lib = os.path.realpath(lib_dir)
    real = os.path.realpath(file)
    if real.startswith(lib + os.sep) and os.path.basename(os.path.dirname(lib)) == "lib" and os.path.basename(lib) == "3dmodels":
        cand = "${KIPRJMOD}/../lib/3dmodels/" + os.path.relpath(real, lib).replace(os.sep, "/")
        got = _resolve_in(cand, board_dir)
        if got and os.path.realpath(got) == real:
            return cand
    return "${KIPRJMOD}/" + os.path.relpath(real, os.path.realpath(board_dir)).replace(os.sep, "/")


# ------------------------------------------------------------------ board text edit (model entries only)
def _block_end(s, start):
    j = s.index("(", start) + 1
    d, q = 1, False
    while d:
        c = s[j]
        if c == "\\" and q:
            j += 2
            continue
        if c == '"':
            q = not q
        elif not q:
            d += (c == "(") - (c == ")")
        j += 1
    return j


def bind_board(board_path, bindings):
    """bindings: {footprint name: (model path string, offset, scale, rotate)}. Replaces the model entries of every footprint of
    that name; nothing else in the file changes. -> number of footprints edited."""
    s = open(board_path, encoding="utf-8").read()
    out, p, n = [], 0, 0
    for m in re.finditer(r'^[ \t]*\(footprint "([^"]+)"', s, re.M):
        name = m.group(1).split(":")[-1]
        if m.start() < p or name not in bindings:
            continue
        j = _block_end(s, m.start())
        blk = s[m.start():j]
        while True:                                    # strip the footprint's own model entries
            mm = re.search(r"\n[ \t]*\(model ", blk)
            if not mm:
                break
            a = mm.start() + 1
            e = _block_end(blk, a)
            blk = blk[:a] + blk[e:].lstrip(" \t").lstrip("\n")
        path, off, sc, rot = bindings[name]
        ind = re.match(r"[ \t]*", s[m.start():]).group(0) + "\t"
        ent = (f'{ind}(model "{path}"\n{ind}\t(offset\n{ind}\t\t(xyz {off[0]:g} {off[1]:g} {off[2]:g})\n{ind}\t)\n'
               f'{ind}\t(scale\n{ind}\t\t(xyz {sc[0]:g} {sc[1]:g} {sc[2]:g})\n{ind}\t)\n'
               f'{ind}\t(rotate\n{ind}\t\t(xyz {rot[0]:g} {rot[1]:g} {rot[2]:g})\n{ind}\t)\n{ind})\n')
        close = re.match(r"[ \t]*", s[m.start():]).group(0)
        nb = blk[:-1].rstrip("\n\t ") + "\n" + ent + close + ")"
        out += [s[p:m.start()], nb]
        p = j
        n += 1
    out.append(s[p:])
    if n:
        open(board_path, "w", encoding="utf-8").write("".join(out))
    return n


def rebind_swapped(board_path, fp_name, models, log=print):
    """After an in-place footprint swap (`replace-footprint`): the library footprint's (model ...) paths (`models` = [(path, offset, scale,
    rotate)]) that resolve on this machine stay; the first that does not is replaced by a project-library file that does: the sources
    file's binding for the footprint, the same file name, or the file carrying the part number in the footprint / model name
    (a copy of the vendor model, as the 3D chain keeps them), written as the project-relative path.  -> {"bound": file|None, "line": str}."""
    bd = os.path.dirname(os.path.abspath(board_path))
    bad = [m for m in models if not _resolve_in(m[0], bd)]
    if not models or not bad:
        return {"bound": None, "line": "3D model: resolves" if models else "3D model: the library footprint names none"}
    name = fp_name.split(":")[-1]
    hit, lib = None, None
    for lib in MP.project_libs(bd):                     # every library up the tree: the source project's, not only the nearest
        src = find_sources(lib)
        row = ((json.load(open(src)).get("bindings") or {}).get(name)) if src else None
        if row and os.path.isfile(os.path.join(lib, row["file"])):
            hit = os.path.join(lib, row["file"])
        files = [os.path.join(r, f) for r, _d, fs in os.walk(lib) for f in fs if f.lower().endswith((".step", ".stp", ".wrl"))]
        base = os.path.basename(bad[0][0]).lower()
        if not hit:
            hit = next((f for f in files if os.path.basename(f).lower() == base), None)
        if not hit:
            toks = set(re.findall(r"\d{3,}-\d{2,}", name + " " + base))
            hit = next((f for f in sorted(files) if any(t in os.path.basename(f) for t in toks)), None) if toks else None
        if hit:
            break
    if not hit:
        return {"bound": None, "line": f"WARNING 3D model {bad[0][0]} does not resolve and the project library has no match: run `fluxplace ready` model start"}
    _p, off, sc, rot = bad[0]
    bind_board(board_path, {name: (kiprjmod_path(hit, lib, bd), off, sc, rot)})
    return {"bound": hit, "line": f"3D model: {os.path.basename(bad[0][0])} unresolved -> project library {os.path.relpath(hit, lib)}"}


# ------------------------------------------------------------------ the owner-action page
def owner_page(mpn, registry):
    """A page to download the model from: a URL the project's sources file already names for the part, else the DigiKey
    search for the MPN (its product page carries the CAD/3D-model links; manufacturers' sign-in walls are reached from there)."""
    for sect, rows in (registry or {}).items():
        if isinstance(rows, dict):
            for note in rows.values():
                if isinstance(note, str) and mpn in note:
                    m = re.search(r"https?://[^\s,;)\"']+", note)
                    if m:
                        return m.group(0)
    return "https://www.digikey.com/en/products/result?keywords=" + re.sub(r"[^\w.+\-]", "", mpn or "")


def _safe(s):
    return re.sub(r"[^\w.+\-]+", "_", s or "").strip("_") or "part"


# ------------------------------------------------------------------ the pass
def run(board_path, facts, mpn_map, cons=None, offline=False, resolver=None, write=True, log=lambda m: None):
    """-> {"ok": [refs], "bound": [{ref, footprint, file, verified}], "fetched": [..], "owner": [{ref, mpn, footprint, page}],
    "pending": [{ref, mpn, footprint}], "waived": [refs], "lib": dir|None, "line": str, "flags": [str]}.
    `resolver(mpn, lib_dir, fp_size, registry)` -> modelchain.resolve's dict (tests pass a fake)."""
    from . import review as R
    from . import partsgate as PG
    bd = os.path.dirname(os.path.abspath(board_path))
    lib = MP.project_lib(bd)
    res = {"ok": [], "bound": [], "fetched": [], "owner": [], "pending": [], "waived": [], "lib": lib, "flags": []}
    todo = {}                                           # footprint name -> [refs] needing work
    for ref, p in sorted((facts or {}).get("parts", {}).items()):
        if p.get("mech") or ref.startswith("__"):
            continue
        ok, _ = PG.model_state(ref, facts)
        if ok:
            res["ok"].append(ref)
        elif waived(ref, cons, p.get("footprint", "")):
            res["waived"].append(ref)
        else:
            todo.setdefault((p.get("footprint") or "").split(":")[-1], []).append(ref)
    src_path = find_sources(lib) if lib else None
    registry = json.load(open(src_path)) if src_path else {}
    new = {}                                            # footprint -> (path string, off, scale, rot)
    changed_registry = False
    for fpname, refs in sorted(todo.items()):
        mpns = {mpn_map.get(r) for r in refs} - {None, ""}
        mpn = sorted(mpns)[0] if mpns else ""
        hit, verified, how = None, False, ""
        if lib:
            b = _lib_footprint_binding(fpname, lib, bd)
            if b:
                hit, verified, how = b, True, "footprint library"
            row = (registry.get("bindings") or {}).get(fpname)
            if not hit and row and os.path.isfile(os.path.join(lib, row["file"])):
                hit = (os.path.join(lib, row["file"]), tuple(row.get("offset", (0, 0, 0))), tuple(row.get("scale", (1, 1, 1))),
                       tuple(row.get("rotate", (0, 0, 0))))
                verified, how = bool(row.get("verified", True)), "sources file binding"
            if not hit and mpn:
                from . import modelchain as MC
                f, _why = MC.src_registry(mpn, {"lib_dir": lib, "registry": registry})
                if f:
                    hit, verified, how = (f, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)), False, "project library by MPN"
        if not hit and lib and mpn and not offline:
            try:
                from . import modelchain as MC
                size = None
                try:
                    size = MC.fp_dims(board_path, refs[0])
                except Exception:                                                     # noqa: BLE001
                    size = None
                r = (resolver or (lambda m, l, s, g: MC.resolve(m, l, fp_size=s, registry=g)))(mpn, lib, size, registry)
            except Exception as e:                                                    # noqa: BLE001
                r = {"ok": False, "tried": [{"source": "fetch", "outcome": f"{type(e).__name__}: {e}"[:140]}]}
            if r.get("ok"):
                dest = os.path.join(lib, _safe(mpn) + ".step")
                if os.path.abspath(r["path"]) != os.path.abspath(dest):
                    import shutil
                    shutil.copyfile(r["path"], dest)
                hit, verified, how = (dest, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)), False, "fetched: " + str(r.get("source"))
                registry.setdefault("real", {})[refs[0]] = (
                    f"{mpn} - {r.get('source')}, fetched {datetime.date.today().isoformat()} -> {os.path.relpath(dest, os.path.dirname(os.path.dirname(src_path or lib)))} "
                    f"(fluxplace ready model start; orientation unverified: render check)")
                registry.setdefault("bindings", {})[fpname] = {"file": os.path.basename(dest), "offset": [0, 0, 0], "rotate": [0, 0, 0],
                                                                "verified": False, "note": "bound at project start; render check"}
                changed_registry = True
                for ref in refs:
                    res["fetched"].append({"ref": ref, "footprint": fpname, "file": os.path.basename(dest), "source": r.get("source")})
                new[fpname] = (kiprjmod_path(dest, lib, bd),) + hit[1:]
                res["flags"].append(f"{', '.join(refs)} {mpn}: model fetched ({r.get('source')}), orientation unverified: render check")
                continue
        if hit:
            new[fpname] = (kiprjmod_path(hit[0], lib, bd),) + tuple(hit[1:])
            for ref in refs:
                res["bound"].append({"ref": ref, "footprint": fpname, "file": os.path.basename(hit[0]), "verified": verified, "how": how})
            if not verified:
                res["flags"].append(f"{', '.join(refs)} {mpn}: model bound from {how}, orientation unverified: render check")
            continue
        if not offline or not mpn or not lib:
            for ref in refs:
                res["owner"].append({"ref": ref, "mpn": mpn, "footprint": fpname,
                                     "page": owner_page(mpn, registry) if mpn else "no MPN: set the part's MPN first"})
        else:
            for ref in refs:
                res["pending"].append({"ref": ref, "mpn": mpn, "footprint": fpname})
    if new and write:
        bind_board(board_path, new)
    if changed_registry and write and src_path:
        with open(src_path, "w", encoding="utf-8") as fh:
            json.dump(registry, fh, indent=1, ensure_ascii=False)
    res["line"] = summary(res)
    return res


def summary(r):
    s = (f"3D models: {len(r['ok'])} ok, {len(r['bound'])} bound now, {len(r['fetched'])} fetched now, "
         f"{len(r['owner'])} need owner action")
    if r["pending"]:
        s += f", {len(r['pending'])} fetch pending (offline)"
    if r["waived"]:
        s += f", {len(r['waived'])} waived"
    return s


def detail_lines(r):
    out = [f"{b['ref']} bound from {b['how']}: {b['file']}" + ("" if b["verified"] else " (orientation unverified: render check)") for b in r["bound"]]
    out += [f"{b['ref']} fetched {b['file']} from {b['source']} (orientation unverified: render check)" for b in r["fetched"]]
    out += [f"OWNER ACTION {o['ref']} {o['mpn']} ({o['footprint']}): download the STEP from {o['page']} into the project lib/3dmodels, "
            f"or waive MODEL_MISSING:^{o['ref']} in [review] waive with a reason" for o in r["owner"]]
    out += [f"{o['ref']} {o['mpn']} ({o['footprint']}): fetch pending (offline)" for o in r["pending"]]
    return out
