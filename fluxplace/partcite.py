"""Pin-table citation for datasheets whose pages are drawings (no extractable text).

The AGENT reads the page; the tool only gives it the picture and checks and records what it read:
  render_page(pdf, page, png)   pdftoppm -> PNG the agent can look at (`parts-page MPN --page N --png out.png`)
  compare(...)                  the pin table the agent read vs what the spec uses and the footprint has: findings, never a silent fix
  record(...)                   the citation (file, page, who/when, the table read, the findings) in <datasheets>/pin-citations.json;
                                the spec's `pinmap_source` is set (with --write-source) only when nothing mismatched
"""
import json
import os
import re
import subprocess
import time

from . import partdocs as PD

CITATIONS = "pin-citations.json"
_SHELL = re.compile(r"^(MP\d*|SH\d*|S\d+|SHIELD|SHLD|MH\d*|TAB\d*|GND_?TAB\d*|EP|PAD)$", re.I)


def page_count(pdf):
    try:
        out = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True, timeout=60).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"^Pages:\s+(\d+)", out, re.M)
    return int(m.group(1)) if m else None


def render_page(pdf, page, png, dpi=110):
    """-> (ok, detail).  Writes `png` (a .png suffix is added by pdftoppm and removed again)."""
    n = page_count(pdf)
    if n is None:
        return False, "pdfinfo unavailable or the PDF is unreadable"
    if not 1 <= page <= n:
        return False, f"page {page} out of range (the document has {n})"
    stem = png[:-4] if png.lower().endswith(".png") else png
    r = subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi), "-png", "-singlefile", pdf, stem],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or not os.path.exists(stem + ".png"):
        return False, (r.stderr or "pdftoppm failed")[:160]
    return True, f"{stem}.png (page {page} of {n}, {dpi} dpi)"


def used_pins(spec, ref):
    out = set()
    for pins in (spec.get("nets") or {}).values():
        for r, p in pins:
            if r == ref:
                out.add(str(p))
    return out


def compare(spec, ref, read, pad_count=None):
    """read: {"pins": {"1": "VBUS", ...}, "count": N?}.  -> {"ok": bool, "findings": [str]}; ok means no mismatch and nothing unread."""
    comp = next((c for c in spec.get("components", []) if c.get("ref") == ref), None)
    if comp is None:
        return {"ok": False, "findings": [f"{ref}: not in the spec"]}
    table = {str(k): str(v) for k, v in (read.get("pins") or {}).items()}
    f = []
    sig = {k for k in table if not _SHELL.match(k)}
    count = read.get("count") or len(sig)
    if len(sig) != count:
        f.append(f"{ref}: the table read lists {len(sig)} signal pins but states a count of {count}")
    lp = comp.get("landpattern") or {}
    if pad_count is not None and count != pad_count and not (lp.get("pins") == count):
        f.append(f"{ref}: the datasheet table has {count} signal pins, the board footprint has {pad_count} signal pads")
    if lp.get("pins") and lp["pins"] != count:
        f.append(f"{ref}: the datasheet table has {count} signal pins, the spec land pattern says {lp['pins']}")
    for p in sorted(used_pins(spec, ref), key=lambda x: (len(x), x)):
        if p not in table and not (_SHELL.match(p) and any(_SHELL.match(k) for k in table) or (_SHELL.match(p) and read.get("mounting"))):
            f.append(f"{ref}: the spec uses pin {p}, which is not in the datasheet table")
    for name, pin in (comp.get("pinmap") or {}).items():
        ds = table.get(str(pin))
        if ds is None:
            f.append(f"{ref}: pinmap {name}->{pin}: pin {pin} not in the table")
        elif not (PD._aliases(name) & PD._aliases(ds) or PD._norm(name) in PD._norm(ds) or PD._norm(ds) in PD._norm(name)):
            f.append(f"{ref}: pinmap {name}->{pin} but the datasheet names pin {pin} '{ds}'")
    return {"ok": not f, "findings": f}


def record(ds_dir, ref, mpn, file, page, read, result, by="agent"):
    p = os.path.join(ds_dir, CITATIONS)
    try:
        cur = json.load(open(p))
    except (OSError, ValueError):
        cur = {}
    cur[ref] = {"mpn": mpn, "file": file, "page": page, "read_by": by, "date": time.strftime("%Y-%m-%d"), "table": read.get("pins"),
                "count": read.get("count"), "ok": result["ok"], "findings": result["findings"]}
    json.dump(cur, open(p, "w"), indent=1, sort_keys=True)
    return p


def set_source(spec, ref, file, page):
    """Set pinmap_source (file#page of the pin table the agent read).  A pinmap is never invented: a positional connector has none.  -> bool."""
    for c in spec.get("components", []):
        if c.get("ref") == ref:
            c["pinmap_source"] = f"{file}#p{page}"
            return True
    return False
