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
import hashlib
import json
import os
import re
import shutil
import subprocess
from fluxplace import procs as _procs
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


_NET = re.compile(r"\[([^\]]+)\]")


def unc_nets(u):
    """Net names of an unconnected item (from its item descriptions, e.g. 'Track [NET] on B.Cu, ...')."""
    return sorted({m.group(1) for it in u.get("items") or [] for m in [_NET.search(str(it.get("description", "")))] if m})


def keys_per_net(d, keep=None):
    """Counter of keys for a kicad-cli JSON dict with OPENS counted per NET (key ("Unet", net)), not by item
    description: moving a track end changes which items an existing open names, and that read as a new open
    every run (W9/W13). Clearance items on a zone are counted (keys_from_report leaves zone items out).
    keep(v) -> bool restricts the violations; unconnected items are always counted per net. (PHASE3G G2a: the
    body of snapiter._keys minus its CENTRED filter.)"""
    keys = keys_from_report(d, keep=keep, unconnected=False)[0]
    for v in d.get("violations", []):
        if v.get("type") == "clearance" and _zone_item(v):
            keys[key_of(v)] += 1
    for u in d.get("unconnected_items", []):
        for n in unc_nets(u) or ["?"]:
            keys[("Unet", n)] += 1
    return keys


def report_slot(board_path):
    """A unique kicad-cli DRC report path beside the board (mkstemp): two runs on one board never unlink each
    other's report (the pour-island missing-file crash)."""
    import tempfile
    fd, out = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(board_path)),
                               prefix=os.path.basename(board_path) + ".", suffix=".drc.json")
    os.close(fd)
    return out


CANARY_RULE = ("(rule \"fxcanary\" (condition \"A.Type == 'Footprint' || A.Type == 'Track'\") "
               "(constraint assertion \"A.Type == 'Nothing'\"))")


def drc_json(board_path, kicad_cli="kicad-cli", timeout=600, rules=None, only_rules=None, complete=True, pro_edit=None, canary=None):
    """THE kicad-cli DRC runner (all severities, zones always refilled). Runs on a temp COPY of the board (never refills or
    rewrites the original) with its .kicad_pro/.kicad_dru beside it, and a unique report path (concurrent calls never collide).
    rules: .kicad_dru text that replaces the board's rule file. only_rules: list of rule-severity keys left on, every other
    check set to ignore in the temp project (a 2 s run for one rule). complete: complete the capped unconnected list.
    pro_edit(dict): edits the temp project (e.g. a tuning profile). canary: names of rules in `rules` at least one of which must fire:
    a syntax error anywhere in a .kicad_dru silently disables EVERY rule (kicad-cli still exits 0), so a report in which none of
    the canary rules appears raises RuntimeError("rule file ignored").
    Raises RuntimeError when kicad-cli writes no report.  Never saves a board (BOARD.Save() would rewrite a neighbouring .kicad_pro)."""
    import shutil
    import tempfile
    stem = os.path.splitext(os.path.abspath(board_path))[0]
    with tempfile.TemporaryDirectory(prefix="fxdrc-") as td:
        b = os.path.join(td, "b.kicad_pcb")
        shutil.copy(board_path, b)
        dru = stem + ".kicad_dru"
        dtext = rules if rules is not None else (open(dru, errors="replace").read() if os.path.isfile(dru) else None)
        if dtext is not None:
            # a syntax error anywhere in a rule file silently disables EVERY rule (exit 0, fewer violations that read as "better"):
            # a canary rule that always fires goes into every custom-rule run and must be in the report
            dtext = dtext.rstrip() + "\n" + CANARY_RULE + "\n"
            open(os.path.join(td, "b.kicad_dru"), "w").write(dtext)
        pro = stem + ".kicad_pro"
        pd = {}
        if os.path.isfile(pro):
            try:
                pd = json.load(open(pro))
            except (OSError, ValueError):
                pd = {}
        if only_rules is not None:
            rs = pd.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rule_severities", {})
            for k in list(rs):
                rs[k] = "ignore"
            for k in only_rules:
                rs[k] = "error"
        if pro_edit:
            pro_edit(pd)
        if pd:
            json.dump(pd, open(os.path.join(td, "b.kicad_pro"), "w"))
        out = os.path.join(td, "drc.json")
        for attempt in (1, 2):                         # kicad-cli sometimes exits without a report under load (critroute: 10 of 13 tries); once more
            try:
                r = _procs.run([kicad_cli, "pcb", "drc", "--format", "json", "--severity-all", "--all-track-errors",
                                    "--refill-zones", "--output", out, b], capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired as e:
                raise RuntimeError(f"DRC timed out after {timeout} s") from e
            rc = getattr(r, "returncode", 0)
            if os.path.isfile(out) and os.path.getsize(out):
                break
        else:
            raise RuntimeError(f"DRC produced no report: kicad-cli exited {rc} with no report (2 tries): {(getattr(r, 'stderr', '') or getattr(r, 'stdout', '') or '')[-200:]}")
        if rc not in (0, None):
            raise RuntimeError(f"kicad-cli exited {rc}: {(getattr(r, 'stderr', '') or getattr(r, 'stdout', '') or '')[-200:]}")
        d = json.load(open(out))
        if dtext is not None and not any("rule 'fxcanary'" in str(v.get("description", "")) for v in d.get("violations", [])):
            raise RuntimeError("rule file ignored: the canary rule did not fire (a syntax error in the .kicad_dru disables every rule, "
                               "or the board has no footprints or tracks)")
        if dtext is not None:                                                  # our canary is a probe, never a finding
            d["violations"] = [v for v in d.get("violations", []) if "rule 'fxcanary'" not in str(v.get("description", ""))]
        if canary and not any(f"rule '{c}'" in str(v.get("description", "")) for v in d.get("violations", []) for c in canary):
            raise RuntimeError("rule file ignored: no canary rule fired (" + ", ".join(canary[:3]) + "); a syntax error in the .kicad_dru "
                               "disables every rule, or the nets have no copper")
        if complete:
            from . import drcfull as DF
            d = DF.complete(b, d, fill=True)
        return d


def drc_keys(board_path, kicad_cli="kicad-cli", timeout=600, keep=None, unconnected=True):
    """Run kicad-cli DRC (all severities, zones refilled) -> (keys, (violations, unconnected))."""
    return keys_from_report(drc_json(board_path, kicad_cli, timeout), keep, unconnected)


def measure_from_report(d, severity="error"):
    """Measure dict from a raw kicad-cli DRC dict (no DRC run). `keys` and `err_keys` are the same Counter.
    severity "error": keys of every error-severity violation (no zone filter; the old gaps err_keys).
    severity "all": keys of every violation except zone violations (today's session_commit key set).
    `errors` counts non-zone violations of the chosen severity (every severity for "all")."""
    from . import drcfull
    vs = d.get("violations") or []
    unc = d.get("unconnected_items") or []
    err = [v for v in vs if v.get("severity") == "error"]
    if severity == "all":
        keys = keys_from_report(d, unconnected=False)[0]
    else:
        keys = Counter(key_of(v) for v in err)
    chosen = [v for v in vs if severity == "all" or v.get("severity") == "error"]
    # B3a: zone-item violations measured, not enforced (`keys`/`errors` keep the zone exemption until B3b)
    zk = Counter(key_of(v) for v in chosen)
    return {"keys_zone": zk, "errors_zone": len(chosen),"opens": len(unc), "opens_by_net": Counter(drcfull.net_of(u) for u in unc),
            "shorts": sum(1 for v in vs if v.get("type") == "shorting_items"),
            "errors": sum(1 for v in chosen if not _zone_item(v)), "keys": keys, "err_keys": keys}


_CACHE = {}


def content_sha(path):
    """sha1 of a file's bytes ("" when absent): the one cache identity (a path, mtime and size repeat across different content)."""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def rules_sha(board):
    """sha1 over the board's rule sources: sibling .kicad_pro and .kicad_dru content (an absent file hashes as empty)."""
    stem = os.path.splitext(os.path.abspath(board))[0]
    h = hashlib.sha1()
    for ext in (".kicad_pro", ".kicad_dru"):
        h.update(ext.encode() + b"\0" + content_sha(stem + ext).encode() + b"\0")
    return h.hexdigest()


def measure(board, kicad_cli="kicad-cli"):
    """One DRC per board (cached on board sha + rules sha + kicad-cli; last 4 kept). `errors` excludes unconnected items.
    The record carries board_sha, rules_sha and status "exact" (a DRC that did not run raises; see measure_record)."""
    p = os.path.abspath(board)
    bsha, rsha = content_sha(p), rules_sha(p)
    key = (bsha, rsha, kicad_cli)
    if key not in _CACHE:
        d = drc_json(p, kicad_cli)
        _CACHE[key] = dict(measure_from_report(d), drc=d, board_sha=bsha, rules_sha=rsha, status="exact")
        while len(_CACHE) > 4:
            _CACHE.pop(next(iter(_CACHE)))
    return _CACHE[key]


def measure_record(board, kicad_cli="kicad-cli"):
    """measure() that never raises: a DRC that cannot run (kicad-cli absent, no report, timeout) -> status "failed" with the
    error and the shas, no counts. Gated stages read `exact(m)`."""
    try:
        return measure(board, kicad_cli)
    except (RuntimeError, OSError, ValueError) as e:
        p = os.path.abspath(board)
        return {"status": "failed", "error": str(e)[:200], "board_sha": content_sha(p), "rules_sha": rules_sha(p)}


def repeatability(board, runs=5, kicad_cli="kicad-cli", timeout=600):
    """B3a: DRC `runs` times, each on a fresh copy of the board with its .kicad_pro/.kicad_dru (drc_json copies again and refills
    zones). -> {"identical": zone-inclusive key multisets all equal, "runs", "errors_zone": [...], "errors": [...], "keys_zone": [Counter]}.
    The evidence B3b needs before zone errors are enforced."""
    import tempfile
    stem = os.path.splitext(os.path.abspath(board))[0]
    recs = []
    for _ in range(runs):
        with tempfile.TemporaryDirectory(prefix="fxrep-") as td:
            b = os.path.join(td, "r.kicad_pcb")
            shutil.copy(stem + ".kicad_pcb", b)
            for ext in (".kicad_pro", ".kicad_dru"):
                if os.path.isfile(stem + ext):
                    shutil.copy(stem + ext, os.path.join(td, "r" + ext))
            recs.append(measure_from_report(drc_json(b, kicad_cli, timeout)))
    kz = [r["keys_zone"] for r in recs]
    return {"identical": all(k == kz[0] for k in kz), "runs": runs, "errors_zone": [r["errors_zone"] for r in recs],
            "errors": [r["errors"] for r in recs], "keys_zone": kz}


def exact(m):
    """True only for a measure that ran kicad-cli on the whole board (an approx or failed measure may not give a pass)."""
    return (m or {}).get("status") == "exact"


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


def protected_nets(cons, names):
    """Nets no trade may open (R5) and whose copper stays locked: the [rf] nets, nets named as RF (review.is_rf),
    every pair leg from the constraints (krtchain.pair_nets) and every leg found by naming (graph.diff_pairs)."""
    from . import krtchain, graph, review, constraints as C
    names = sorted(names)
    nopair = C.not_pairs(cons)
    rf = set(((cons or {}).get("rf") or {}).get("nets") or []) | {n for n in names if review.is_rf(n)}
    try:
        pairs = set(krtchain.pair_nets(cons, names, None))
    except Exception:
        pairs = set()
    try:
        dp = graph.diff_pairs(names, exclude=nopair)
        pairs |= set(dp) | set(dp.values())
    except Exception:
        pass
    return rf | (pairs - nopair)


def snap_nets(snap):
    """Net names of an audit.snapshot: every pad net and every copper net, empty name dropped."""
    names = {n for ns in (snap.get("pads") or {}).values() for n in ns} | set(snap.get("copper") or {})
    return sorted(n for n in names if n)


MODES = ("repair", "build", "edit")


def accept(base, cand, mode="repair", target=(), kind=None, base_board=None, cand_board=None, score=None,
           locked_ok=None, snap=None, protect=()):
    """Guard a candidate measure against its base; first failing rule gives `why`. Rules and modes:
    docs/recipes/PHASE2-guard.md. mode: repair (R0-R5, score, target), build (R0, R1, R3, R5, opens), edit (R0, R1,
    R3, R5). R5: no protected net gains opens. Other trades are allowed: no rule compares opens per plain net.
    Unknown mode raises ValueError. -> (ok, why)."""
    from . import audit
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    if base_board and cand_board:
        snap = snap or (lambda p: audit.snapshot(p, checks=False))
        diff = audit.connectivity_diff(snap(base_board), snap(cand_board))
        if not diff["ok"]:
            return False, ("R0 " + (diff["violations"][0] if diff.get("violations") else "connectivity"))[:100]
    if cand["shorts"] > base["shorts"]:
        return False, "R1 shorts up"
    if protect:
        bo, co = base.get("opens_by_net") or {}, cand.get("opens_by_net") or {}
        for n in sorted(protect):
            if co.get(n, 0) > bo.get(n, 0):
                return False, f"R5 protected net opened {n} {bo.get(n, 0)}->{co.get(n, 0)}"[:120]
    if mode == "repair":
        new = new_keys(base.get("keys", base.get("err_keys")), cand.get("keys", cand.get("err_keys")))
        if kind is not None:
            from .gaps import KIND_OF_TYPE
            new = Counter({k: v for k, v in new.items() if KIND_OF_TYPE.get(k[0]) != kind})
        if new:
            return False, f"R2 new DRC key {next(iter(new))[0]}"[:120]
    if locked_ok is not None and not locked_ok():
        return False, "R3 locked copper changed"
    if mode == "repair":
        sc = score or (lambda m: [m["shorts"], m["opens"], m["errors"]])
        if not sc(cand) < sc(base):
            return False, "R4 score not lower"
        if target and "gap_ids" in cand and not any(t not in cand["gap_ids"] for t in target):
            return False, "target gap still there"
    elif mode == "build" and not cand["opens"] < base["opens"]:
        return False, "R4 opens not lower"
    return True, ""


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
