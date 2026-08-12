"""Real 3D MODELS: find footprints whose 3D model is missing or broken and
fetch the real manufacturer STEP via distributor APIs.

Sources, in order:
  digikey  Product Information API v4 /media endpoint ("CAD Models" links,
           direct STEP or a zip containing one). Needs DIGIKEY_CLIENT_ID /
           DIGIKEY_CLIENT_SECRET.
  mouser   Search API. Mouser's API does not serve CAD binaries; it is used
           to normalize MPNs and confirm availability when DigiKey misses.
           Needs MOUSER_API_KEY.

Credentials come from the environment or (fallback) any `env` block inside
~/.claude.json containing those key names (the razor-board convention).

The policy is REAL VENDOR STEP ONLY — no hand-authored stand-ins. When no
CAD link exists the part is reported, never faked.

Pure-python helpers are separated from pcbnew so the resolution logic is
testable: `audit(models_iter, resolver)` decides missing/broken; fetching
and board wiring live in `fetch_step` / `attach`.
"""
import io
import json
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request
import zipfile

__all__ = ["credentials", "audit_board", "fetch_step", "attach", "sync"]

UA = {"User-Agent": "Mozilla/5.0"}


# ---------------------------------------------------------------- credentials
def credentials():
    """-> dict with any of DIGIKEY_CLIENT_ID/SECRET, MOUSER_API_KEY found."""
    keys = ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET", "MOUSER_API_KEY")
    out = {k: os.getenv(k) for k in keys if os.getenv(k)}
    if len(out) == len(keys):
        return out
    try:
        cfg = json.load(open(os.path.expanduser("~/.claude.json")))
    except Exception:
        return out

    def walk(o):
        if isinstance(o, dict):
            if any(k in o for k in keys):
                for k in keys:
                    if o.get(k):
                        out.setdefault(k, o[k])
            for v in o.values():
                walk(v)

    walk(cfg)
    return out


# ---------------------------------------------------------------- digikey api
def _dk_token(creds):
    d = urllib.parse.urlencode({
        "client_id": creds["DIGIKEY_CLIENT_ID"],
        "client_secret": creds["DIGIKEY_CLIENT_SECRET"],
        "grant_type": "client_credentials"}).encode()
    r = urllib.request.Request("https://api.digikey.com/v1/oauth2/token",
                               data=d, headers={"Content-Type":
                                                "application/x-www-form-urlencoded"})
    return json.load(urllib.request.urlopen(r, timeout=25))["access_token"]


def _dk_headers(creds, tok):
    return {"Authorization": "Bearer " + tok,
            "X-DIGIKEY-Client-Id": creds["DIGIKEY_CLIENT_ID"],
            "Content-Type": "application/json",
            "X-DIGIKEY-Locale-Site": "US", "X-DIGIKEY-Locale-Currency": "USD"}


def dk_media(mpn, creds, tok):
    """-> (normalized_mpn, media_links[]) via keyword search + /media."""
    h = _dk_headers(creds, tok)
    body = json.dumps({"Keywords": mpn, "Limit": 1}).encode()
    r = urllib.request.Request(
        "https://api.digikey.com/products/v4/search/keyword", data=body, headers=h)
    prods = json.load(urllib.request.urlopen(r, timeout=25)).get("Products", [])
    real = prods[0]["ManufacturerProductNumber"] if prods else mpn
    pn = urllib.parse.quote(real, safe="")
    r = urllib.request.Request(
        "https://api.digikey.com/products/v4/search/%s/media" % pn, headers=h)
    return real, (json.load(urllib.request.urlopen(r, timeout=25))
                  .get("MediaLinks") or [])


def mouser_lookup(mpn, creds):
    """Normalize an MPN via Mouser search (no CAD binaries in their API)."""
    if "MOUSER_API_KEY" not in creds:
        return None
    body = json.dumps({"SearchByPartRequest": {"MouserPartNumber": mpn}}).encode()
    r = urllib.request.Request(
        "https://api.mouser.com/api/v1/search/partnumber?apiKey=" +
        creds["MOUSER_API_KEY"], data=body,
        headers={"Content-Type": "application/json"})
    try:
        parts = (json.load(urllib.request.urlopen(r, timeout=25))
                 .get("SearchResults") or {}).get("Parts") or []
    except Exception:
        return None
    return parts[0].get("ManufacturerPartNumber") if parts else None


# ---------------------------------------------------------------- step fetch
# hosts whose "CAD Models" links always need a browser login — attempting a
# scripted download just burns a timeout every run (DigiKey-first policy:
# DigiKey/Mouser APIs are the sources; anything else is best-effort only)
LOGIN_WALLED = ("snapeda.com", "snapmagic.com", "componentsearchengine.com",
                "samacsys.com", "ultralibrarian.com")


def _fail_cache(dest_dir):
    p = pathlib.Path(dest_dir) / ".fetch_failures.json"
    try:
        return p, json.load(open(p))
    except Exception:
        return p, {}


def fetch_step(mpn, dest_dir, creds, tok, log=print, force=False):
    """Fetch the real STEP for `mpn` into dest_dir. Returns (path, status).

    Failures are remembered in dest_dir/.fetch_failures.json so a blocked
    CDN or an MPN DigiKey doesn't carry is not re-attempted (with its
    timeout) on every subsequent run — pass force=True to retry them."""
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.+-]", "_", mpn)
    dest = dest_dir / (safe + ".step")
    if dest.exists() and dest.stat().st_size > 1000:
        return dest, "cached"
    cache_path, fails = _fail_cache(dest_dir)

    def _fail(status):
        fails[mpn] = status
        try:
            json.dump(fails, open(cache_path, "w"), indent=1)
        except Exception:
            pass
        return None, status

    if not force and mpn in fails:
        return None, "known-fail (use --force to retry): %s" % fails[mpn]
    try:
        real, links = dk_media(mpn, creds, tok)
    except Exception as e:
        # one Mouser-normalization retry for MPNs DigiKey's search rejects
        alt = mouser_lookup(mpn, creds)
        if alt and alt != mpn:
            try:
                real, links = dk_media(alt, creds, tok)
            except Exception as e2:
                return _fail("api-error: %s" % e2)
        else:
            return _fail("api-error: %s" % e)
    cad = [m for m in links if m.get("MediaType") == "CAD Models" and
           "STP" in (m.get("Title", "") + m.get("Url", "")).upper()]
    cad = cad or [m for m in links if m.get("MediaType") == "CAD Models"]
    if not cad:
        kinds = sorted({m.get("MediaType") for m in links})
        return _fail("no-CAD-link (media: %s)" % ", ".join(k or "?" for k in kinds))
    url = cad[0]["Url"]
    if any(h in url.lower() for h in LOGIN_WALLED):
        return _fail("login-walled: %s" % url[:90])
    try:
        req = urllib.request.Request(url, headers=UA)
        data = urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        return _fail("download-blocked: %s -> %s" % (type(e).__name__, url[:90]))
    if data[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(data))
        member = sorted((n for n in z.namelist()
                         if n.lower().endswith((".step", ".stp"))),
                        key=lambda n: -z.getinfo(n).file_size)
        member = member or [n for n in z.namelist()
                            if n.lower().endswith(".wrl")]
        if not member:
            return _fail("zip-without-step")
        data = z.read(member[0])
        if member[0].lower().endswith(".wrl"):
            dest = dest.with_suffix(".wrl")
            dest.write_bytes(data)
            return dest, "fetched"
    if not data.lstrip()[:40].startswith(b"ISO-10303"):
        return _fail("not-a-step-file (%d bytes)" % len(data))
    dest.write_bytes(data)
    return dest, "fetched"


KICAD3D_RAW = ("https://gitlab.com/kicad/libraries/kicad-packages3D/-/raw/"
               "master/%s")


def fetch_kicad_official(broken_path, dest_dir, log=print):
    """A broken ${KICAD*_3DMODEL_DIR}/Lib.3dshapes/Name.step reference means
    the OFFICIAL KiCad model exists as a name but not in the local install —
    fetch it from the kicad-packages3D repo (the footprint's own intended
    model). Returns (path, status)."""
    m = re.match(r"\$\{KICAD\d*_3DMODEL_DIR\}/(.+)$", broken_path)
    if not m:
        return None, "not-a-stdlib-path"
    rel = m.group(1)
    if not rel.lower().endswith((".step", ".stp", ".wrl")):
        rel += ".step"
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / pathlib.Path(rel).name
    if dest.exists() and dest.stat().st_size > 1000:
        return dest, "cached"
    url = KICAD3D_RAW % urllib.parse.quote(rel)
    try:
        data = urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=60).read()
    except Exception as e:
        return None, "kicad-official-miss: %s" % type(e).__name__
    if not data.lstrip()[:40].startswith(b"ISO-10303"):
        return None, "kicad-official-not-step"
    dest.write_bytes(data)
    return dest, "fetched"


# ---------------------------------------------------------------- alignment
_PT_RE = re.compile(
    rb"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*"
    rb"(-?[\d.Ee+-]+)\s*,\s*(-?[\d.Ee+-]+)\s*,\s*(-?[\d.Ee+-]+)\s*\)")


def step_bbox(path):
    """Approximate model bbox in mm from the STEP point cloud. Control
    points can slightly overshoot true surfaces — good enough to center a
    body and choose a 90-degree orientation. Returns (min, max) 3-tuples."""
    data = pathlib.Path(path).read_bytes()
    scale = 1.0
    if re.search(rb"SI_UNIT\s*\(\s*\.MILLI\.", data):
        scale = 1.0
    elif re.search(rb"SI_UNIT\s*\([^)]*\.METRE\.", data):
        scale = 1000.0
    pts = _PT_RE.findall(data)
    if not pts:
        return None
    xs = [float(p[0]) * scale for p in pts]
    ys = [float(p[1]) * scale for p in pts]
    zs = [float(p[2]) * scale for p in pts]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


PROBE_MM = 40.0     # probe board edge length (calibration reference)


def measure_model(model_path, rotation=(0.0, 0.0, 0.0), kicad_cli="kicad-cli",
                  offset=None):
    """TRUE rendered geometry of a model under a KiCad rotation/offset (see
    _measure_inproc). ALWAYS runs in a fresh interpreter: building probe
    BOARDs in a process that already holds another board corrupts SWIG
    state and segfaults (the one-board-per-process gotcha). Returns
    dict(w, h, z, zmin, cx, cy) mm or None."""
    import json as _json
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    args = _json.dumps(dict(path=str(model_path), rotation=list(rotation),
                            offset=list(offset) if offset else None,
                            kicad_cli=kicad_cli))
    code = ("import sys, json, os; sys.path.insert(0, %r); "
            "from fluxplace.models import _measure_inproc; "
            "a = json.loads(%r); "
            "r = _measure_inproc(a['path'], tuple(a['rotation']), "
            "a['kicad_cli'], tuple(a['offset']) if a['offset'] else None); "
            "print('RESULT ' + json.dumps(r)); sys.stdout.flush(); "
            "os._exit(0)" % (repo, args))
    try:
        rc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, timeout=600, env=dict(os.environ))
        for line in rc.stdout.splitlines():
            if line.startswith("RESULT "):
                return _json.loads(line[7:])
    except Exception:
        pass
    return None


def _measure_inproc(model_path, rotation=(0.0, 0.0, 0.0),
                    kicad_cli="kicad-cli", offset=None):
    """TRUE rendered geometry of a model under a KiCad rotation/offset — by
    RENDERING it. A throwaway board (known 40 mm outline = pixel scale)
    carries one footprint with the model; kicad-cli renders orthographic top
    and front views and the model's pixel bbox is measured. This is the
    same pipeline as the deliverable renders, so whatever it measures is
    what the user sees. (VRML export ignores FP_3DMODEL transforms; raw
    STEP clouds lie via control points — rendering is the only honest
    ruler.) Returns dict(w, h, z, zmin, cx, cy) mm or None."""
    import pcbnew
    import subprocess
    import tempfile
    from PIL import Image

    def _px_bbox(png, bg_tol=18):
        im = Image.open(png).convert("RGB")
        w, h = im.size
        corner = im.getpixel((2, 2))
        xs, ys = [], []
        px = im.load()
        for y in range(0, h, 2):
            for x in range(0, w, 2):
                r, g, b2 = px[x, y]
                if (abs(r - corner[0]) + abs(g - corner[1])
                        + abs(b2 - corner[2])) > bg_tol:
                    xs.append(x)
                    ys.append(y)
        if not xs:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    def _diff_bbox(bare_png, full_png, tol=26):
        """Pixel bbox of what the MODEL added: full render minus bare."""
        a = Image.open(bare_png).convert("RGB")
        b = Image.open(full_png).convert("RGB")
        if a.size != b.size:
            return None
        pa, pb = a.load(), b.load()
        w, h = a.size
        xs, ys = [], []
        for y in range(0, h, 2):
            for x in range(0, w, 2):
                ra, ga, ba = pa[x, y]
                rb, gb, bb = pb[x, y]
                if abs(ra - rb) + abs(ga - gb) + abs(ba - bb) > tol:
                    xs.append(x)
                    ys.append(y)
        if len(xs) < 4:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    with tempfile.TemporaryDirectory() as td:
        def render(bp, side, out):
            subprocess.run([kicad_cli, "pcb", "render", "--side", side,
                            "--width", "600", "--height", "600",
                            "--background", "opaque", "--quality", "basic",
                            "-o", out, bp], capture_output=True, timeout=180)
            return os.path.exists(out)

        def probe_board(with_model):
            board = pcbnew.BOARD()
            half = pcbnew.FromMM(PROBE_MM / 2)
            pts = [(-half, -half), (half, -half), (half, half), (-half, half)]
            for i in range(4):
                seg = pcbnew.PCB_SHAPE(board)
                seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
                seg.SetStart(pcbnew.VECTOR2I(*pts[i]))
                seg.SetEnd(pcbnew.VECTOR2I(*pts[(i + 1) % 4]))
                seg.SetLayer(pcbnew.Edge_Cuts)
                seg.SetWidth(pcbnew.FromMM(0.1))
                board.Add(seg)
            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference("X1")
            fp.Reference().SetVisible(False)
            fp.Value().SetVisible(False)
            if with_model:
                m = pcbnew.FP_3DMODEL()
                m.m_Filename = str(model_path)
                m.m_Rotation = pcbnew.VECTOR3D(*rotation)
                if offset is not None:
                    m.m_Offset = pcbnew.VECTOR3D(*offset)
                fp.Models().append(m)
            board.Add(fp)
            fp.SetPosition(pcbnew.VECTOR2I(0, 0))
            bp = os.path.join(td, f"probe{int(with_model)}.kicad_pcb")
            pcbnew.SaveBoard(bp, board)
            return bp

        # calibration: bare board pixel width == PROBE_MM
        bare = probe_board(False)
        full = probe_board(True)
        top_bare = os.path.join(td, "bare_top.png")
        top_full = os.path.join(td, "full_top.png")
        front_full = os.path.join(td, "full_front.png")
        front_bare = os.path.join(td, "bare_front.png")
        if not (render(bare, "top", top_bare) and render(full, "top", top_full)
                and render(full, "front", front_full)
                and render(bare, "front", front_bare)):
            return None
        cal = _px_bbox(top_bare)
        if not cal:
            return None
        mm_per_px = PROBE_MM / max(1, cal[2] - cal[0])
        bx = (cal[0] + cal[2]) / 2.0
        by = (cal[1] + cal[3]) / 2.0
        mb = _diff_bbox(top_bare, top_full)      # the model = what changed
        if not mb:
            return None
        # front view: X horizontal, Z vertical (image y down). Its zoom
        # auto-fits per render, so calibrate from the bare board SLAB whose
        # thickness is known (1.6 mm) and whose top row is z=0.
        fb = _diff_bbox(front_bare, front_full)
        fbare = _px_bbox(front_bare)
        z = zmin = None
        foot = 1.0
        if fb and fbare:
            slab_px = max(1, fbare[3] - fbare[1])
            mmz = 1.6 / slab_px
            z = (fb[3] - fb[1]) * mmz
            zmin = (fbare[1] - fb[3]) * mmz
            # footedness: SMD bodies flare at the BOTTOM (gull-wing leads at
            # board level). Width of the model's bottom quarter vs top
            # quarter in the front view — >1 means feet-down, <1 means the
            # leads point up (inverted). THE ±90-tip tie-breaker: plain
            # bboxes measure identical either way.
            a = Image.open(front_bare).convert("RGB")
            bimg = Image.open(front_full).convert("RGB")
            pa, pb2 = a.load(), bimg.load()
            span = max(1, fb[3] - fb[1])
            q = max(2, span // 4)

            def _row_width(y0, y1):
                wmax = 0
                for y in range(max(0, y0), min(a.size[1], y1), 1):
                    xs = [x for x in range(fb[0], fb[2] + 1, 1)
                          if x < a.size[0] and (
                              abs(pa[x, y][0] - pb2[x, y][0]) +
                              abs(pa[x, y][1] - pb2[x, y][1]) +
                              abs(pa[x, y][2] - pb2[x, y][2])) > 26]
                    if xs:
                        wmax = max(wmax, max(xs) - min(xs))
                return wmax
            wtop = _row_width(fb[1], fb[1] + q)
            wbot = _row_width(fb[3] - q, fb[3] + 1)
            foot = (wbot + 1.0) / (wtop + 1.0)
        return dict(
            w=(mb[2] - mb[0]) * mm_per_px,
            h=(mb[3] - mb[1]) * mm_per_px,
            z=z, zmin=zmin, foot=foot,
            cx=((mb[0] + mb[2]) / 2.0 - bx) * mm_per_px,
            cy=((mb[1] + mb[3]) / 2.0 - by) * mm_per_px)


# candidate base orientations for a misaligned vendor STEP: identity, the
# four axis tips, upside down — each with an optional in-plane quarter turn
ORIENT_CANDIDATES = [(0, 0, 0), (90, 0, 0), (-90, 0, 0),
                     (0, 90, 0), (0, -90, 0), (180, 0, 0)]


def orient_plan(model_path, fp_w, fp_h, kicad_cli="kicad-cli", log=print,
                incumbent=None):
    """Measurement-driven orientation solver: try every base orientation
    (plus a Z-quarter-turn where the aspect asks for it), MEASURE each by
    rendering, and keep the one whose board-plane footprint best matches
    the part. Feet-down beats leads-up via the front-view footedness ratio
    (plain bboxes cannot tell a part from its upside-down twin). The offset
    is then solved by probing: guess from the measured center/floor,
    re-render, keep whichever sign convention actually centers — no
    Euler/axis-convention assumptions anywhere.

    `incumbent`: (rotation, offset) currently on the footprint. A working
    orientation is never replaced unless a candidate beats it by a clear
    margin — the solver must not 'fix' the DF40 that was already right.
    Returns dict(offset, rotation, bbox) or None."""

    def _score(meas, w, h):
        s = abs(w - fp_w) + abs(h - fp_h)
        if meas.get("z") is not None:
            s += max(0.0, meas["z"] - max(fp_w, fp_h)) * 2.0
        if meas.get("foot", 1.0) < 0.95:
            s += 4.0          # leads-up: heavily penalized
        return s

    best = None
    for base in ORIENT_CANDIDATES:
        meas = measure_model(model_path, base, kicad_cli)
        if not meas:
            continue
        for zrot in (0.0, 90.0):
            if zrot:
                w, h = meas["h"], meas["w"]
                rot = (base[0], base[1], base[2] + 90.0)
            else:
                w, h = meas["w"], meas["h"]
                rot = base
            score = _score(meas, w, h)
            if best is None or score < best[0]:
                best = (score, rot)
    if best is None:
        return None
    if incumbent is not None:
        irot, ioff = incumbent
        imeas = measure_model(model_path, irot, kicad_cli, offset=ioff)
        if imeas:
            iscore = _score(imeas, imeas["w"], imeas["h"])
            if iscore <= best[0] + 1.5:
                return None       # incumbent stands — do not touch it
    rot = best[1]
    final = measure_model(model_path, rot, kicad_cli)
    if not final:
        return None
    zoff = -(final["zmin"] or 0.0)
    # probe both XY sign conventions; keep whichever truly centers/floors
    chosen = None
    for cand in ((-final["cx"], final["cy"], zoff),
                 (-final["cx"], -final["cy"], zoff)):
        chk = measure_model(model_path, rot, kicad_cli, offset=cand)
        if not chk:
            continue
        resid = abs(chk["cx"]) + abs(chk["cy"]) + abs(chk.get("zmin") or 0.0)
        if chosen is None or resid < chosen[0]:
            chosen = (resid, cand, chk)
    if chosen is None:
        return dict(offset=(-final["cx"], final["cy"], zoff), rotation=rot,
                    bbox=(final["w"], final["h"], final["z"]))
    resid, offset, chk = chosen
    if resid > 1.5:
        log(f"    orient: residual {resid:.1f}mm after centering "
            f"{os.path.basename(str(model_path))} — inspect the render")
    return dict(offset=offset, rotation=rot,
                bbox=(chk["w"], chk["h"], chk["z"]))


def plan_alignment(step_path, fp_w, fp_h, log=print):
    """Offset+rotation that centers the model on the footprint and floors
    it to the board. If the model's XY aspect clearly disagrees with the
    footprint's (both non-square, axes swapped), add a 90-degree z-rotation.
    Returns dict(offset=(x,y,z), rotation=(0,0,zdeg)) in FOOTPRINT frame."""
    bb = step_bbox(step_path)
    if not bb:
        return None
    (x0, y0, z0), (x1, y1, z1) = bb
    mw, mh = x1 - x0, y1 - y0
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    rot = 0.0
    if fp_w and fp_h:
        fp_land = fp_w >= fp_h * 1.15
        m_land = mw >= mh * 1.15
        fp_port = fp_h >= fp_w * 1.15
        m_port = mh >= mw * 1.15
        if (fp_land and m_port) or (fp_port and m_land):
            rot = 90.0
            cx, cy = cy, -cx  # centre after rotating the model +90 about z
    return dict(offset=(-cx, cy, -z0), rotation=(0.0, 0.0, rot),
                bbox=(mw, mh, z1 - z0))


# ---------------------------------------------------------------- board side
def resolve_vars(path, prj_dir):
    env = dict(os.environ)
    for var in ("KICAD10_3DMODEL_DIR", "KICAD9_3DMODEL_DIR"):
        if var not in env and os.path.isdir("/usr/share/kicad/3dmodels"):
            env[var] = "/usr/share/kicad/3dmodels"
    env.setdefault("KIPRJMOD", prj_dir)
    return re.sub(r"\$\{(\w+)\}",
                  lambda g: env.get(g.group(1), "\0"), path)


def audit_board(board):
    """-> [(footprint, reason)] needing a model: none attached, or every
    attached path fails to resolve to an existing file."""
    prj = os.path.dirname(board.GetFileName() or ".")
    todo = []
    for f in board.GetFootprints():
        models = list(f.Models())
        if not models:
            todo.append((f, "no-model"))
            continue
        if not any(os.path.exists(resolve_vars(m.m_Filename, prj))
                   for m in models):
            todo.append((f, "broken-path"))
    return todo


def attach(fp, path, plan=None):
    """Attach a STEP to a footprint (replacing broken entries). `plan` from
    plan_alignment() sets offset/rotation so arbitrary-origin vendor models
    land centered, floored, and axis-matched."""
    import pcbnew
    fp.Models().clear()
    m = pcbnew.FP_3DMODEL()
    m.m_Filename = str(path)
    if plan:
        m.m_Offset = pcbnew.VECTOR3D(*plan["offset"])
        m.m_Rotation = pcbnew.VECTOR3D(*plan["rotation"])
    fp.Models().append(m)


def sync(board, mpn_map, dest_dir, path_prefix=None, align=True, log=print,
         force=False):
    """Fetch authoritative models and attach. Default: only footprints whose
    models are missing/broken. `force=True`: every ref in `mpn_map` gets an
    authoritative fetch attempt and the REAL model replaces whatever is
    attached (visual stand-ins included) — the accurate-models policy:
    DigiKey CAD media first, Mouser MPN-normalization retry, KiCad official
    for stdlib refs; a fetch failure keeps the current model and is
    reported, never silently guessed around. Returns report dict; caller
    saves the board."""
    creds = credentials()
    if "DIGIKEY_CLIENT_ID" not in creds:
        raise SystemExit("no DigiKey credentials (env or ~/.claude.json)")
    tok = _dk_token(creds)
    report = {"fetched": [], "cached": [], "skipped": [], "failed": []}
    todo = list(audit_board(board))
    if force:
        seen = {fp.GetReference() for fp, _ in todo}
        for fp in board.GetFootprints():
            if fp.GetReference() in mpn_map and fp.GetReference() not in seen:
                todo.append((fp, "force-refresh"))
    for fp, why in todo:
        ref = fp.GetReference()
        path = status = None
        if why == "broken-path":
            # a stdlib footprint's own official model beats any distributor CAD
            for m in fp.Models():
                path, status = fetch_kicad_official(m.m_Filename, dest_dir,
                                                    log=log)
                if path:
                    break
        mpn = mpn_map.get(ref)
        if not path and not mpn:
            report["skipped"].append((ref, why, "no MPN mapping"))
            continue
        if not path:
            path, status = fetch_step(mpn, dest_dir, creds, tok, log=log,
                                      force=force)
        if not path:
            report["failed"].append((ref, mpn or why, status))
            log(f"  ! {ref} {mpn}: {status}")
            continue
        wired = str(path)
        if path_prefix:
            wired = path_prefix.rstrip("/") + "/" + path.name
        plan = None
        keep_incumbent = False
        if align and str(path).lower().endswith((".step", ".stp")):
            try:
                import pcbnew
                # footprint dims in the footprint's LOCAL frame — the model
                # rides the footprint's rotation, so a world-frame bbox on a
                # 90-degree part would solve the orientation backwards (the
                # bug that laid two same-model transformers the same way when
                # their footprints were 90 degrees apart)
                bbf = fp.GetBoundingBox(False, False)
                fw = pcbnew.ToMM(bbf.GetWidth())
                fh = pcbnew.ToMM(bbf.GetHeight())
                if round(abs(fp.GetOrientationDegrees()) % 180) == 90:
                    fw, fh = fh, fw
                incumbent = None
                for m in fp.Models():
                    if os.path.basename(m.m_Filename) == os.path.basename(
                            str(path)):
                        incumbent = (
                            (m.m_Rotation.x, m.m_Rotation.y, m.m_Rotation.z),
                            (m.m_Offset.x, m.m_Offset.y, m.m_Offset.z))
                        break
                plan = orient_plan(path, fw, fh, log=log,
                                   incumbent=incumbent)
                if plan is None and incumbent is not None:
                    keep_incumbent = True
            except Exception as e:
                log(f"    align skipped for {ref}: {e}")
        if keep_incumbent:
            report[status].append((ref, mpn, "incumbent-kept"))
            log(f"  {ref} {mpn}: {status} (orientation already correct)")
            continue
        attach(fp, wired, plan)
        report[status].append((ref, mpn, wired))
        log(f"  {ref} {mpn}: {status} -> {wired}")
    return report
