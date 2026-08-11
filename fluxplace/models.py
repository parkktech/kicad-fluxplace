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
def fetch_step(mpn, dest_dir, creds, tok, log=print):
    """Fetch the real STEP for `mpn` into dest_dir. Returns (path, status)."""
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.+-]", "_", mpn)
    dest = dest_dir / (safe + ".step")
    if dest.exists() and dest.stat().st_size > 1000:
        return dest, "cached"
    try:
        real, links = dk_media(mpn, creds, tok)
    except Exception as e:
        # one Mouser-normalization retry for MPNs DigiKey's search rejects
        alt = mouser_lookup(mpn, creds)
        if alt and alt != mpn:
            try:
                real, links = dk_media(alt, creds, tok)
            except Exception as e2:
                return None, "api-error: %s" % e2
        else:
            return None, "api-error: %s" % e
    cad = [m for m in links if m.get("MediaType") == "CAD Models" and
           "STP" in (m.get("Title", "") + m.get("Url", "")).upper()]
    cad = cad or [m for m in links if m.get("MediaType") == "CAD Models"]
    if not cad:
        kinds = sorted({m.get("MediaType") for m in links})
        return None, "no-CAD-link (media: %s)" % ", ".join(k or "?" for k in kinds)
    url = cad[0]["Url"]
    try:
        req = urllib.request.Request(url, headers=UA)
        data = urllib.request.urlopen(req, timeout=60).read()
    except Exception as e:
        return None, "download-blocked: %s -> %s" % (type(e).__name__, url[:90])
    if data[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(data))
        member = [n for n in z.namelist()
                  if n.lower().endswith((".step", ".stp"))]
        if not member:
            return None, "zip-without-step"
        data = z.read(member[0])
    if not data.lstrip()[:40].startswith(b"ISO-10303"):
        return None, "not-a-step-file (%d bytes)" % len(data)
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


def attach(fp, path):
    """Attach a STEP to a footprint (replacing broken entries)."""
    import pcbnew
    fp.Models().clear()
    m = pcbnew.FP_3DMODEL()
    m.m_Filename = str(path)
    fp.Models().append(m)


def sync(board, mpn_map, dest_dir, path_prefix=None, log=print):
    """Audit `board`, fetch what `mpn_map` (ref -> MPN) covers, attach.
    Returns report dict; caller saves the board."""
    creds = credentials()
    if "DIGIKEY_CLIENT_ID" not in creds:
        raise SystemExit("no DigiKey credentials (env or ~/.claude.json)")
    tok = _dk_token(creds)
    report = {"fetched": [], "cached": [], "skipped": [], "failed": []}
    for fp, why in audit_board(board):
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
            path, status = fetch_step(mpn, dest_dir, creds, tok, log=log)
        if not path:
            report["failed"].append((ref, mpn or why, status))
            log(f"  ! {ref} {mpn}: {status}")
            continue
        wired = str(path)
        if path_prefix:
            wired = path_prefix.rstrip("/") + "/" + path.name
        attach(fp, wired)
        report[status].append((ref, mpn, wired))
        log(f"  {ref} {mpn}: {status} -> {wired}")
    return report
