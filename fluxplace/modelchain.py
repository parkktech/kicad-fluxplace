"""3D model source chain: an ordered list of places a REAL STEP can come from, every attempt recorded, every file validated.

Order: (1) the project's own library and registry (model_sources.json), (2) the DigiKey media API (manufacturer STEP links, zips
opened), (3) EasyEDA/LCSC by the part's LCSC code (community exporter), (4) manufacturer templates (MFR_MODEL_TEMPLATES, plain
downloads only).  Sign-in walls and bot challenges are recorded as the outcome; nothing is worked around.  A file is accepted only
when it is a real STEP (ISO-10303 header, points present) AND its bounding box is plausible for the footprint (`plausible`).
A part with no fetchable model gets {"ok": False, "tried": [...]}; a stand-in is never generated.
"""
import glob
import json
import os
import re

from . import models as M

# (mpn regex, [url templates with {mpn}]): plain-download manufacturer STEP links only.  Empty by default: add a host here only after
# measuring that it serves a file to an ordinary client.
MFR_MODEL_TEMPLATES = []


def _safe(mpn):
    return re.sub(r"[^\w.+-]", "_", mpn)


def validate_step(path):
    """-> (ok, detail, bbox (w, h, z) | None)."""
    try:
        head = open(path, "rb").read(4096)
    except OSError as e:
        return False, f"unreadable: {e.__class__.__name__}", None
    if not head.lstrip()[:40].startswith(b"ISO-10303"):
        return False, "not a STEP file (no ISO-10303 header)", None
    bb = M.step_bbox(path)
    if not bb:
        return False, "STEP has no geometry points", None
    (x0, y0, z0), (x1, y1, z1) = bb
    return True, "STEP ok", (x1 - x0, y1 - y0, z1 - z0)


def plausible(bbox, fp_dims, lo=0.4, hi=3.0):
    """Model footprint (w, h) vs the footprint's courtyard/pad extent (either orientation): -> (ok, detail)."""
    if not fp_dims or not bbox:
        return None, "no footprint size to compare"
    w, h = sorted((bbox[0], bbox[1]))
    fw, fh = sorted(fp_dims)
    rw, rh = w / max(fw, 1e-6), h / max(fh, 1e-6)
    ok = lo <= rw <= hi and lo <= rh <= hi
    return ok, f"model {w:.2f}x{h:.2f} mm vs footprint {fw:.2f}x{fh:.2f} mm (ratio {rw:.2f}, {rh:.2f}; allowed {lo}-{hi})"


def fp_dims(board_path, ref):
    """(w, h) mm of the footprint's courtyard, else of its pads, from the board file (pcbnew)."""
    from . import kicad_io as IO
    b = IO.load(board_path)
    fp = next((f for f in b.GetFootprints() if f.GetReference() == ref), None)
    if fp is None:
        return None
    import pcbnew
    for layer in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
        try:
            bb = fp.GetBoundingBox(False)
            ys = [s for s in fp.GraphicalItems() if s.GetLayer() == layer]
            if ys:
                box = ys[0].GetBoundingBox()
                for s in ys[1:]:
                    box.Merge(s.GetBoundingBox())
                return (pcbnew.ToMM(box.GetWidth()), pcbnew.ToMM(box.GetHeight()))
        except Exception:                                              # noqa: BLE001
            break
    pads = list(fp.Pads())
    if not pads:
        return None
    box = pads[0].GetBoundingBox()
    for p in pads[1:]:
        box.Merge(p.GetBoundingBox())
    return (pcbnew.ToMM(box.GetWidth()), pcbnew.ToMM(box.GetHeight()))


# ---------------------------------------------------------------- sources
def src_registry(mpn, ctx):
    hits = sorted(glob.glob(os.path.join(ctx["lib_dir"], "*" + _safe(mpn) + "*.step")) +
                  glob.glob(os.path.join(ctx["lib_dir"], "*" + _safe(mpn) + "*.stp")))
    if hits:
        return hits[0], "already in the project library"
    reg = ctx.get("registry") or {}
    for sect in ("real", "community", "kicad_library"):
        for ref, note in (reg.get(sect) or {}).items():
            if isinstance(note, str) and mpn in note:
                m = re.search(r"lib/3dmodels/([\w.+\-/]+\.(?:step|stp))", note)
                if m and os.path.exists(os.path.join(ctx["lib_dir"], os.path.basename(m.group(1)))):
                    return os.path.join(ctx["lib_dir"], os.path.basename(m.group(1))), f"registered ({sect} {ref})"
    return None, "not in the project library or registry"


def src_digikey(mpn, ctx):
    creds = ctx["creds"]
    if "DIGIKEY_CLIENT_ID" not in creds:
        return None, "skipped: no DigiKey credentials"
    tok = M._dk_token(creds)
    path, status = M.fetch_step(mpn, ctx["scratch"], creds, tok, force=ctx.get("force", False))
    return (str(path) if path else None), status


def src_easyeda(mpn, ctx):
    from . import dsfetch as D
    codes = D._lcsc_codes(mpn)
    if not codes:
        return None, "no LCSC listing found"
    notes = []
    for c in codes[:2]:
        got = M.easyeda_model(c, os.path.join(ctx["scratch"], ".easyeda_tmp", c), log=lambda m: notes.append(m.strip()[:110]))
        if got and str(got).lower().endswith((".step", ".stp")):
            return str(got), f"EasyEDA/LCSC {c}"
        notes.append(f"{c}: " + (notes.pop() if notes else "no STEP (wrl only)" if got else "no 3D model"))
    return None, "; ".join(notes)[:240]


def src_manufacturer(mpn, ctx):
    from . import dsfetch as D
    import zipfile
    import io
    tried = []
    for rx, tpls in MFR_MODEL_TEMPLATES:
        if re.match(rx, mpn.upper()):
            for t in tpls:
                u = t.format(mpn=mpn)
                st, _ct, data = D.get(u)
                if st != 200:
                    tried.append(f"HTTP {st}")
                    continue
                if data[:2] == b"PK":
                    z = zipfile.ZipFile(io.BytesIO(data))
                    n = next((n for n in z.namelist() if n.lower().endswith((".step", ".stp"))), None)
                    data = z.read(n) if n else b""
                p = os.path.join(ctx["scratch"], _safe(mpn) + ".step")
                open(p, "wb").write(data)
                return p, "manufacturer download"
    return None, "no manufacturer template" if not tried else "; ".join(tried)


SOURCES = [("project-library", src_registry), ("digikey-media", src_digikey), ("easyeda-lcsc", src_easyeda),
           ("manufacturer", src_manufacturer)]


def resolve(mpn, lib_dir, creds=None, fp_size=None, registry=None, scratch=None, sources=None, force=False):
    """-> {"ok", "path", "source", "bbox", "plausible", "tried": [{source, outcome}]}.  The first source whose file is a real STEP
    AND plausible for the footprint wins; a real STEP that does not fit the footprint is recorded and the chain goes on."""
    creds = creds if creds is not None else M.credentials()
    scratch = scratch or os.path.join(lib_dir, ".fetch")
    os.makedirs(scratch, exist_ok=True)
    ctx = {"lib_dir": lib_dir, "creds": creds, "registry": registry, "scratch": scratch, "force": force}
    tried = []
    for name, fn in (sources or SOURCES):
        try:
            path, why = fn(mpn, ctx)
        except Exception as e:                                         # noqa: BLE001 - a source failing is an outcome
            tried.append({"source": name, "outcome": M._redact(f"{type(e).__name__}: {e}")[:140]})
            continue
        if not path:
            tried.append({"source": name, "outcome": why})
            continue
        ok, detail, bb = validate_step(path)
        if not ok:
            tried.append({"source": name, "outcome": f"rejected: {detail} ({why})"})
            continue
        pl, pd = plausible(bb, fp_size)
        if pl is False:
            tried.append({"source": name, "outcome": f"rejected: {pd}"})
            continue
        tried.append({"source": name, "outcome": f"accepted: {why}; {pd}"})
        return {"ok": True, "path": path, "source": name, "bbox": bb, "plausible": pl, "tried": tried}
    return {"ok": False, "path": None, "source": None, "bbox": None, "plausible": None, "tried": tried}
