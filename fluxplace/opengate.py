"""Open connections are a FAIL, not advice.

`measure(board)` counts the open connections of a SAVED board two ways and compares them:
  fresh   pcbnew after a fresh zone fill (conn_helper --fill), with the pad pair of every open connection
  saved   pcbnew on the file as written (stored fills), and `kicad-cli pcb drc` on the file as written
  cli     `kicad-cli pcb drc --refill-zones` (nothing saved)
The fill is STALE when any of these disagree, or when the DRC clearance errors against zones differ before/after
the refill (zones that hold copper where the saved file says they do not, or the reverse).

Findings (review.check_open): UNROUTED (FAIL, one per open connection: net, both pads, distance) and
STALE_FILL (FAIL). Waive per net: `[review] waive = ["UNROUTED:^\\+3V3$"]` (matches the net, listed in refs).
`refuse(board, cons, waivers)` is the hard stop for fab / deliver / pcbway (--no-review does not skip it).
`refill(board)` refills the zones and saves, run by every command that writes a routed board."""
import json
import math
import os
import re
import subprocess
import tempfile

from . import routeconn as RC

PY = RC.PY
FILL_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fill_helper.py")


def has_zones(board):
    with open(board, errors="surrogateescape") as fh:
        return re.search(r"^\s*\(zone\b", fh.read(), re.M) is not None


def refill(board, out=None):
    """Refill the zones of `board` and save (to `out`, default in place). -> zone count (0 = untouched, nothing to fill)."""
    if not has_zones(board):
        return 0
    r = subprocess.run([PY, FILL_HELPER, board] + ([out] if out else []), capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError("zone refill failed: " + (r.stderr or r.stdout)[-200:])
    return json.loads([l for l in r.stdout.splitlines() if l.startswith("{")][-1])["zones"]


def _helper(board, fill):
    r = subprocess.run([PY, RC.HELPER, board] + (["--fill"] if fill else []), capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-300:])
    return json.loads([l for l in r.stdout.splitlines() if l.startswith("{")][-1])


def _drc(board, refill_zones, cli="kicad-cli"):
    """-> {unconnected, zone_clearance} from kicad-cli DRC (the board file is not written)."""
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "drc.json")
        cmd = [cli, "pcb", "drc", "--format", "json", "--severity-all", "--all-track-errors"] + \
              (["--refill-zones"] if refill_zones else []) + ["-o", out, board]
        subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if not os.path.exists(out):
            raise RuntimeError("kicad-cli drc produced no report")
        d = json.load(open(out))
    zc = 0
    for v in d.get("violations", []):
        if v.get("type") in ("clearance", "shorting_items", "starved_thermal", "isolated_copper") and \
                any("zone" in (i.get("description") or "").lower() for i in v.get("items", [])):
            zc += 1
    return {"unconnected": len(d.get("unconnected_items", [])), "zone_clearance": zc}


def connections(clusters):
    """{net: [[pad dict]...]} -> [{net, a, b, mm}]: per net the minimum spanning tree over its clusters (closest pad pair)."""
    out = []
    for net, cl in sorted(clusters.items()):
        k = len(cl)
        best = {}
        for i in range(k):
            for j in range(i + 1, k):
                best[(i, j)] = min(((math.hypot(p["x"] - q["x"], p["y"] - q["y"]), p, q) for p in cl[i] for q in cl[j]),
                                   key=lambda t: t[0])
        seen, edges = {0}, []
        while len(seen) < k:
            d, i, j = min((best[(min(a, b), max(a, b))][0], a, b) for a in seen for b in range(k) if b not in seen)
            _d, p, q = best[(min(i, j), max(i, j))]
            edges.append((p, q))
            seen.add(j)
        for p, q in edges:
            out.append({"net": net, "a": p, "b": q, "mm": round(math.hypot(p["x"] - q["x"], p["y"] - q["y"]), 2)})
    return out


def measure(board, cli="kicad-cli", cross=True):
    """-> {fresh, saved, cli_saved, cli_fresh, stale: [why], connections, nets}."""
    zones = has_zones(board)
    fresh = _helper(board, True)
    saved = _helper(board, False) if zones else fresh
    n_fresh = sum(fresh["nets"].values())
    res = {"fresh": n_fresh, "saved": sum(saved["nets"].values()), "nets": fresh["nets"],
           "connections": connections(fresh.get("clusters", {})), "stale": [], "cli_saved": None, "cli_fresh": None}
    if cross:
        try:
            ds, df = _drc(board, False, cli), (_drc(board, True, cli) if zones else None)
            res["cli_saved"] = ds["unconnected"]
            res["cli_fresh"] = (df or ds)["unconnected"]
            res["zone_clr"] = (ds["zone_clearance"], (df or ds)["zone_clearance"])
        except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as e:
            res["cross_error"] = str(e)[:100]
    if zones:
        if res["saved"] != n_fresh:
            res["stale"].append(f"pcbnew open count {res['saved']} on the saved fills, {n_fresh} after a refill")
        if res["cli_saved"] is not None and res["cli_saved"] != res["cli_fresh"]:
            res["stale"].append(f"kicad-cli unconnected {res['cli_saved']} on the saved fills, {res['cli_fresh']} after a refill")
        if res.get("zone_clr") and res["zone_clr"][0] != res["zone_clr"][1]:
            res["stale"].append(f"DRC clearance errors against zones {res['zone_clr'][0]} on the saved fills, "
                                f"{res['zone_clr'][1]} after a refill")
    return res


def _pad(p):
    return f"{p['ref']}.{p['num']}"


def findings(m, level="FAIL"):
    """review findings from `measure` -> [{level, code, msg, refs}]."""
    out = []
    for c in m["connections"]:
        out.append({"level": level, "code": "UNROUTED", "refs": sorted({c["net"], c["a"]["ref"], c["b"]["ref"]}),
                    "msg": f"{c['net']}: {_pad(c['a'])} - {_pad(c['b'])} not connected ({c['mm']} mm apart)"})
    x = m.get("cli_fresh")
    if x is not None and x != m["fresh"] and not m["stale"]:
        out.append({"level": level, "code": "UNROUTED", "refs": [],
                    "msg": f"kicad-cli counts {x} unconnected after a refill, pcbnew {m['fresh']}: use the larger"})
    if m["stale"]:
        out.append({"level": level, "code": "STALE_FILL", "refs": [],
                    "msg": "saved zone fills are stale (" + "; ".join(m["stale"]) + "): refill zones in KiCad (B) and save, "
                           "or run a fluxplace command that writes the board"})
    return out


def waived(f, waivers):
    for w in waivers or ():
        code, _, rx = w.partition(":")
        if f["code"] == code and (not rx or re.search(rx, f["msg"]) or any(re.search(rx, r) for r in f.get("refs", []))):
            return True
    return False


def refuse(board, cons=None, waivers=(), log=print):
    """Hard stop for fab/deliver/pcbway: SystemExit when any unwaived UNROUTED / STALE_FILL remains."""
    waivers = list(waivers or ()) + list(((cons or {}).get("review", {}) or {}).get("waive", []))
    bad = [f for f in findings(measure(board)) if not waived(f, waivers)]
    if bad:
        for f in bad[:10]:
            log(f"  {f['level']} {f['code']} {f['msg']}")
        raise SystemExit(f"REFUSED: {len(bad)} unrouted/stale-fill finding(s) in {board}. Route them, or waive per net with "
                         f"[review] waive = [\"UNROUTED:^NET$\"] and a reason.")
    return []
