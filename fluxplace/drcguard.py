"""Shared DRC acceptance guard for every fixer.

Rule: a candidate board is accepted iff no violation key occurs MORE often in the
candidate than in the base (a multiset test). Removed keys are fine. Raw counts are
never compared as a whole: the kicad-cli total moves run to run with zone refill
(observed 872-875 on one board), so a "total must not rise" guard flips on noise.

Identity of a violation: (type, sorted item descriptions with decimal numbers replaced).
Decimals are lengths and coordinates, which every geometry fix moves (a chamfer shortens
a dangling stub: "length 10.0000 mm" -> "length 9.7000 mm"). Integers stay (pad and ref
numbers, which name the violation). Position is NOT in the key: a fix that moves an
existing violation must not read as a new one. Unconnected items get the prefix 'U'.
A multiset, not a set, so a second instance of an existing key still counts as new.

Noise rule (the only filter): violations with an item whose description contains
"Zone" are left out of the key set. That is the class patch.py _vkeys recorded as
flickering with zone fill. Nothing else is filtered; whole types are never ignored.
Measured 2026-11-14: 5/5 identical key multisets on v15 and v16 copies, with and without a
pcbnew round trip (no flicker reproduced here, so the exclusion is inherited).
"""
import json
import os
import re
import shutil
import subprocess
from collections import Counter

ZONE_MARK = "Zone"
_DECIMAL = re.compile(r"-?\d+\.\d+")


def key_of(v, prefix=""):
    items = v.get("items") or []
    descs = tuple(sorted(_DECIMAL.sub("#", str(i.get("description", ""))) for i in items))
    return (prefix + str(v.get("type", "")), descs)


def _zone_item(v):
    return any(ZONE_MARK in str(i.get("description", "")) for i in v.get("items") or [])


def keys_from_report(d, keep=None, unconnected=True):
    """(Counter of keys, (violations, unconnected)) from a kicad-cli JSON dict.
    keep(v) -> bool restricts violations (fanout counts errors only)."""
    vio = [v for v in d.get("violations", []) if keep is None or keep(v)]
    unc = d.get("unconnected_items", [])
    keys = [key_of(v) for v in vio if not _zone_item(v)] + ([key_of(u, "U") for u in unc] if unconnected else [])
    return Counter(keys), (len(vio), len(unc))


def drc_json(board_path, kicad_cli="kicad-cli", timeout=600):
    """The raw kicad-cli DRC dict (all severities, zones refilled)."""
    out = board_path + ".drc.json"
    subprocess.run([kicad_cli, "pcb", "drc", "--format", "json",
                    "--severity-all", "--all-track-errors", "--refill-zones", "-o", out,
                    board_path], capture_output=True, text=True, timeout=timeout)
    if not os.path.exists(out):
        raise RuntimeError("DRC produced no report")
    from . import drcfull as DF
    d = DF.complete(board_path, json.load(open(out)), fill=True)
    os.unlink(out)
    return d


def drc_keys(board_path, kicad_cli="kicad-cli", timeout=600, keep=None, unconnected=True):
    """Run kicad-cli DRC (all severities, zones refilled) -> (keys, (violations, unconnected))."""
    return keys_from_report(drc_json(board_path, kicad_cli, timeout), keep, unconnected)


_ZONE_DESC = re.compile(r"^Zone \[([^\]]*)\] on ([^,\s]+)")


def open_split(d):
    """Unconnected items of a kicad-cli DRC dict, split: `islands` {(net, layer): n} for a zone (pour) island that
    touches no same-net pad, via or track, and `track_opens` for every other open (track, via, pad)."""
    islands, opens = Counter(), 0
    for u in d.get("unconnected_items", []):
        m = next((_ZONE_DESC.match(str(i.get("description", ""))) for i in u.get("items") or []
                  if _ZONE_DESC.match(str(i.get("description", "")))), None)
        if m:
            islands[(m.group(1), m.group(2))] += 1
        else:
            opens += 1
    return {"islands": dict(islands), "track_opens": opens}


def island_lines(split):
    """The final-summary lines for an open_split: track opens, then one ZONE_ISLAND row per island and the fix."""
    out = [f"open connections: {split['track_opens']} track/via open(s), {sum(split['islands'].values())} zone island(s)"]
    out += [f"ZONE_ISLAND {net} {layer} x{n}" for (net, layer), n in sorted(split["islands"].items())]
    if split["islands"]:
        out.append("  fix: fluxplace pour-island BOARD --net NET --out OUT (one guarded stitching via per island); "
                   "a zone island is not a track open")
    return out


def new_keys(base_keys, keys):
    """Keys the candidate has more of than the base (the only rejection cause)."""
    return Counter(keys) - Counter(base_keys)


def check(candidate, base, kicad_cli="kicad-cli", log=print, stats=None, report=None, keep=None):
    """DRC the saved candidate file. Returns (ok, (keys, counts), new_keys)."""
    run = report or (lambda p, c: drc_keys(p, c, keep=keep))
    keys, counts = run(candidate, kicad_cli)
    if stats is not None:
        stats["drc_runs"] = stats.get("drc_runs", 0) + 1
    new = new_keys(base[0], keys)
    if log and new:
        log(f"      rejected by DRC: {len(new)} new violation(s), e.g. {sorted(new)[0][0]}")
    return not new, (keys, counts), new


def guard_board(board_path, work, mutate, base, kicad_cli="kicad-cli", log=print,
                stats=None, report=None, keep=None):
    """pcbnew-load `work`, mutate(board) -> bool, save to a tmp file, DRC-guard it.
    Accepted into `work` only when no new key appears. base = (keys, counts).
    Returns (accepted, (keys, counts))."""
    import pcbnew
    board = pcbnew.LoadBoard(work)
    if not mutate(board):
        return False, base
    tmp = work + ".try.kicad_pcb"
    pcbnew.SaveBoard(tmp, board)
    for ext in (".kicad_pro", ".kicad_dru"):
        side = os.path.splitext(board_path)[0] + ext
        if os.path.exists(side):
            shutil.copy(side, os.path.splitext(tmp)[0] + ext)
    ok, cur, _ = check(tmp, base, kicad_cli, log, stats, report, keep)
    if ok:
        shutil.move(tmp, work)
        return True, cur
    os.unlink(tmp)
    return False, base
