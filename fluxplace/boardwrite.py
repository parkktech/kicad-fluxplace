"""Pure-text footprint moves: set a footprint's origin and rotation in a
.kicad_pcb without pcbnew. The footprint, its pads and its texts rotate as one
rigid body (pad and text angles in the file are absolute, so each gains the
rotation delta; relative offsets and 3D model transforms are untouched). Nothing
else in the file is read or rewritten, and no absolute path is ever written."""
import re

_TOK = re.compile(r'[()"]')
_REF = re.compile(r'\(property\s+"Reference"\s+"([^"]*)"')


def _end(text, i):
    """Index just past the node that opens at text[i] == '('."""
    depth, instr = 0, False
    for m in _TOK.finditer(text, i):
        c = m.group()
        k = m.start()
        if instr:
            if c == '"' and text[k - 1] != "\\":
                instr = False
            continue
        if c == '"':
            instr = True
        elif c == "(":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return k + 1
    raise ValueError("unbalanced parentheses")


def _children(text, start, end, tag=None):
    """Spans of the direct child nodes of the node text[start:end]."""
    out, i = [], text.index("(", start) + 1
    while True:
        j = _TOK.search(text, i)
        while j and j.group() == '"':       # skip a leading atom string, e.g. (footprint "X"
            k = j.end()
            while text[k] != '"' or text[k - 1] == "\\":
                k += 1
            j = _TOK.search(text, k + 1)
        if not j or j.start() >= end - 1 or j.group() == ")":
            return out
        s = j.start()
        e = _end(text, s)
        if tag is None or text.startswith("(" + tag, s) and not text[s + 1 + len(tag)].isalnum():
            out.append((s, e))
        i = e


def _num(v):
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _norm(a):
    a = (a + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


_AT = re.compile(r"\(at\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)(?:\s+(-?[\d.eE+-]+))?\s*\)")


def _rot_at(text, s, e, delta):
    """Add `delta` to the angle of the (at ...) child of the node text[s:e]."""
    for cs, ce in _children(text, s, e, "at"):
        m = _AT.match(text, cs)
        if m:
            ang = _norm(float(m.group(3) or 0.0) + delta)
            return cs, ce, f"(at {m.group(1)} {m.group(2)} {_num(ang)})"
    return None


def apply_moves(text, moves):
    """`moves` = {ref: (x, y, angle_deg)} -> new board text. Unknown refs are
    ignored; returns (text, [refs moved])."""
    root_end = _end(text, text.index("("))
    edits, done = [], []
    for fs, fe in _children(text, 0, root_end, "footprint"):
        m = _REF.search(text, fs, fe)
        if not m or m.group(1) not in moves:
            continue
        ref = m.group(1)
        x, y, ang = moves[ref]
        kids = _children(text, fs, fe)
        own = next(((cs, ce) for cs, ce in kids if text.startswith("(at", cs)
                    and text[cs + 3].isspace()), None)
        old = _AT.match(text, own[0]) if own else None
        if not old:
            continue
        delta = _norm(ang - float(old.group(3) or 0.0))
        edits.append((own[0], own[1], f"(at {_num(x)} {_num(y)} {_num(_norm(ang))})"))
        if abs(delta) > 1e-9:
            for cs, ce in kids:
                tag = text[cs + 1:cs + 12].split()[0].rstrip(")")
                if tag in ("pad", "property", "fp_text"):
                    r = _rot_at(text, cs, ce, delta)
                    if r:
                        edits.append(r)
        done.append(ref)
    for s, e, new in sorted(edits, reverse=True):
        text = text[:s] + new + text[e:]
    return text, done


_POST_WRITE = []          # callbacks (board_path, moved_refs); the CLI registers the silkscreen hook here


def set_post_write(fn):
    """Register (or clear with None) the callback run after every board that moved footprints is written."""
    _POST_WRITE[:] = [fn] if fn else []


def run_post_write(path, refs):
    for fn in _POST_WRITE:
        fn(path, sorted(refs))


def move_file(src, dst, moves, post=True):
    text = open(src, errors="surrogateescape").read()
    out, done = apply_moves(text, moves)
    with open(dst, "w", errors="surrogateescape") as fh:
        fh.write(out)
    if post and done:
        run_post_write(dst, done)
    return done


_NETNAME = re.compile(r'\(net\s+"([^"]*)"\)')


def strip_nets(text, nets):
    """Remove the track/arc/via nodes of the named nets (KiCad 10 net-name form).
    Zones, pads and every other net's copper are untouched. -> (text, count)."""
    nets = set(nets)
    root_end = _end(text, text.index("("))
    cut = []
    for cs, ce in _children(text, 0, root_end):
        if text.startswith(("(segment", "(arc", "(via"), cs):
            m = _NETNAME.search(text, cs, ce)
            if m and m.group(1) in nets:
                cut.append((cs, ce))
    for s, e in reversed(cut):
        # swallow the line break/indent before the node so the file stays tidy
        k = s
        while k > 0 and text[k - 1] in " \t":
            k -= 1
        text = text[:k] + text[e:]
    return text, len(cut)


def strip_indexed(text, seg_idx, via_idx):
    """Remove track/arc nodes (`seg_idx`) and vias (`via_idx`) by their position in file
    order, the order routediag.load lists them in."""
    root_end = _end(text, text.index("("))
    cut, si, vi = [], 0, 0
    for cs, ce in _children(text, 0, root_end):
        if text.startswith(("(segment", "(arc"), cs):
            if si in seg_idx:
                cut.append((cs, ce))
            si += 1
        elif text.startswith("(via", cs):
            if vi in via_idx:
                cut.append((cs, ce))
            vi += 1
    for s, e in reversed(cut):
        k = s
        while k > 0 and text[k - 1] in " \t":
            k -= 1
        text = text[:k] + text[e:]
    return text


_PT_NODE = re.compile(r"\((start|end|mid|at)(\s+)(-?[\d.eE+-]+)(\s+)(-?[\d.eE+-]+)")


def carry_copper(text, moves_by_ref, pieces):
    """Carry stub copper rigidly with its part. `moves_by_ref` = {ref: (old_pose, new_pose)}, poses (x, y, angle_deg) as
    routediag reads them; `pieces` = [{"ref", "segs": [track index], "vias": [via index]}] in routediag order. Each listed
    track/arc/via moves by the part's translation and turns about the footprint origin by the angle change (KiCad
    convention, same as apply_moves). Keys and locks are untouched. -> text (same node count and order)."""
    from .boardfile import _rot
    owner_s, owner_v = {}, {}
    for pc in pieces:
        if pc["ref"] not in moves_by_ref:
            continue
        for i in pc["segs"]:
            owner_s[i] = pc["ref"]
        for j in pc["vias"]:
            owner_v[j] = pc["ref"]
    if not owner_s and not owner_v:
        return text

    def xf(x, y, ref):
        (ox, oy, oa), (nx, ny, na) = moves_by_ref[ref]
        dx, dy = _rot(x - ox, y - oy, na - oa)
        return nx + dx, ny + dy

    root_end = _end(text, text.index("("))
    edits, si, vi = [], 0, 0
    for cs, ce in _children(text, 0, root_end):
        ref = None
        if text.startswith(("(segment", "(arc"), cs):
            ref = owner_s.get(si)
            si += 1
        elif text.startswith("(via", cs):
            ref = owner_v.get(vi)
            vi += 1
        if ref is None:
            continue
        for m in _PT_NODE.finditer(text, cs, ce):
            x, y = xf(float(m.group(3)), float(m.group(5)), ref)
            new = f"({m.group(1)}{m.group(2)}{_num(x)}{m.group(4)}{_num(y)}"
            edits.append((m.start(), m.end(), new))
    for s, e, new in sorted(edits, reverse=True):
        text = text[:s] + new + text[e:]
    return text


# ---------------------------------------------------------------------------------------------- A3: the one commit point
# A board that left a stage carries a manifest `<board>.done.json`: {board_sha, nets: {net: {why, items: [item hashes], fp}}}.  An item hash
# covers one piece of copper (class, layer, ends rounded to 1 um, width, locked flag; vias: position, size, drill, layer pair, type;
# no UUIDs).  `why` = "locked" (the LOCKED items of the net are protected: they may not move or vanish, copper may be ADDED to the net) or
# "pair P/N" (both legs of a pair the audit calls complete: the whole copper of the net is protected, nothing added or removed).
# commit()/verify() refuse a candidate that changes protected copper (PROTECTED_CHANGED), adds duplicate copper or colliding item ids; a
# legitimate re-lay names the nets in `release` (logged to attempts.jsonl).  The manifest stores the sha of the board it describes: a
# board edited outside the flow is MANIFEST_STALE (re-seeded loudly at a pipeline start, refused in the middle of a flow).
import hashlib
import json
import os
import shutil

DONE_EXT = ".done.json"


def done_path(board):
    return board + DONE_EXT


def file_sha(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def net_items(path, nets=None, locked_only=False):
    """{net: sorted list of item hashes} of that net's copper (all items, or only the locked ones); pcbnew, 1 um rounding, no UUIDs."""
    import pcbnew
    b = pcbnew.LoadBoard(path)
    mm = lambda v: round(v / 1e6, 3)                                            # noqa: E731
    xy = lambda p: (mm(p.x), mm(p.y))                                           # noqa: E731
    rows = {}
    for t in b.GetTracks():
        net = t.GetNetname()
        if not net or (nets is not None and net not in nets) or (locked_only and not t.IsLocked()):
            continue
        cls = t.GetClass()
        if cls == "PCB_VIA":
            row = ("via", xy(t.GetPosition()), mm(t.GetWidth(pcbnew.F_Cu)), mm(t.GetDrillValue()), b.GetLayerName(t.TopLayer()),
                   b.GetLayerName(t.BottomLayer()), int(t.GetViaType()), bool(t.IsLocked()))
        else:
            ends = sorted([xy(t.GetStart()), xy(t.GetEnd())])
            row = (cls, b.GetLayerName(t.GetLayer()), tuple(ends), mm(t.GetWidth()), xy(t.GetMid()) if cls == "PCB_ARC" else None, bool(t.IsLocked()))
        rows.setdefault(net, []).append(hashlib.sha1(repr(row).encode()).hexdigest()[:16])
    return {n: sorted(r) for n, r in rows.items()}


def net_fingerprints(path, nets=None):
    """{net: sha1 of all of that net's copper}."""
    return {n: hashlib.sha1("".join(r).encode()).hexdigest() for n, r in net_items(path, nets).items()}


def locked_nets(path):
    """Nets that have at least one locked item."""
    return set(net_items(path, locked_only=True))


def load_manifest(board):
    try:
        with open(done_path(board)) as fh:
            d = json.load(fh)
        return d if isinstance(d.get("nets"), dict) else {"nets": {}}
    except (OSError, ValueError):
        return {"nets": {}}


def _save_manifest(board, man):
    man["board_sha"] = file_sha(board)
    tmp = done_path(board) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(man, fh, sort_keys=True)
    os.replace(tmp, done_path(board))


def is_stale(board, man=None):
    """True when the manifest of `board` describes another file (the board was edited after it was sealed)."""
    man = man if man is not None else load_manifest(board)
    return bool(man["nets"]) and man.get("board_sha") != file_sha(board)


def seed(board, why_by_net=None, keep=None, locked=True):
    """Write `<board>.done.json` from the board's copper.  `keep` = a manifest whose entries stay (re-read from this board: a "locked"
    entry keeps its locked items, a "pair" entry all items), `why_by_net` = {net: why} protected whole (a completed pair), and with
    `locked` every net's locked items are protected too.  -> the manifest."""
    why = {n: e.get("why", "kept") for n, e in ((keep or {}).get("nets") or {}).items()}
    why.update(why_by_net or {})
    whole = {n for n, w in why.items() if w != "locked"}
    items = net_items(board)                                                                      # one read of the board
    lk = net_items(board, locked_only=True) if (locked or any(w == "locked" for w in why.values())) else {}
    nets = {}
    for n, w in why.items():
        src = items.get(n) if n in whole else lk.get(n)
        if src:
            nets[n] = {"why": w, "items": src}
    if locked:
        for n, src in lk.items():
            nets.setdefault(n, {"why": "locked", "items": src})
    for e in nets.values():
        e["fp"] = hashlib.sha1("".join(e["items"]).encode()).hexdigest()
    man = {"nets": nets}
    _save_manifest(board, man)
    return man


def fresh_manifest(board, log=None):
    """The manifest of `board`, re-seeded loudly when it is stale (the board was edited outside the flow)."""
    man = load_manifest(board)
    if is_stale(board, man):
        if log:
            log(f"manifest of {os.path.basename(board)} is stale (the board changed after it was sealed): re-seeded from the board")
        man = seed(board, keep=man)
    return man


def changed(board, manifest, release=()):
    """Protected nets whose protected copper is not intact in `board`: a "locked" net lost or moved one of its locked items (additions are
    fine); a "pair" net differs at all."""
    rel = set(release)
    names = [n for n in manifest["nets"] if n not in rel]
    if not names:
        return []
    now = net_items(board, set(names))
    bad = []
    for n in names:
        e = manifest["nets"][n]
        have = now.get(n, [])
        if e["why"] == "locked":
            hv = {}
            for x in have:
                hv[x] = hv.get(x, 0) + 1
            need = {}
            for x in e["items"]:
                need[x] = need.get(x, 0) + 1
            if any(hv.get(x, 0) < c for x, c in need.items()):
                bad.append(n)
        elif have != e["items"]:
            bad.append(n)
    return sorted(bad)


def uuid_collisions(path):
    """Number of item-id groups that share a full id (KiCad keys items by the whole uuid, so only an identical id collides)."""
    import collections
    import re
    text = open(path, errors="surrogateescape").read()
    ids = re.findall(r'\(uuid "([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"\)', text)
    return sum(1 for v in collections.Counter(u.lower() for u in ids).values() if v > 1)


def _dups(path):
    from . import dedupe
    return len(dedupe.duplicates(open(path, errors="surrogateescape").read()))


def verify(base, cand, release=(), manifest=None):
    """Read-only check of `cand` against the manifest of `base` (or `manifest`).
    -> {"ok", "why", "changed": [nets], "duplicates": n, "uuid_collisions": n}.  A duplicate or id collision already in `base` does not
    block a candidate that does not add more (a repair that removes them must pass).  `cand` may be `base` itself (a stage that edited
    its input in place): the recorded manifest is then the baseline.  A stale manifest of `base` is refused (MANIFEST_STALE)."""
    man = manifest if manifest is not None else load_manifest(base)
    if manifest is None and is_stale(base, man):
        return {"ok": False, "why": f"MANIFEST_STALE {os.path.basename(base)} was edited after it was sealed", "changed": [],
                "duplicates": 0, "uuid_collisions": 0}
    bad = changed(cand, man, release)
    d1 = _dups(cand)
    u1 = uuid_collisions(cand)
    same = os.path.abspath(base) == os.path.abspath(cand)
    d0, u0 = (d1, u1) if same else ((_dups(base), uuid_collisions(base)) if os.path.isfile(base) else (0, 0))
    why = []
    if bad:
        why.append("PROTECTED_CHANGED " + ", ".join(bad[:6]) + (f" (+{len(bad) - 6})" if len(bad) > 6 else ""))
    if d1 > d0:
        why.append(f"DUPLICATE_COPPER {d1 - d0} new")
    if u1 > u0:
        why.append(f"UUID_COLLISION {u1 - u0} new")
    return {"ok": not why, "why": "; ".join(why), "changed": bad, "duplicates": d1, "uuid_collisions": u1}


def _log_release(board, nets, reason, attempts=None):
    try:
        from . import calllog
        if calllog.enabled():
            calllog.append({"ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"), "event": "release", "board": os.path.basename(board),
                            "nets": sorted(nets), "reason": reason}, attempts or calllog.attempts_path(board))
    except Exception:                                                       # noqa: BLE001 - the log is advice
        pass


def release(board, names, reason, attempts=None):
    """Drop `names` from the manifest of `board` (an explicit, logged act); they come back through commit(complete=...) or seed()."""
    man = load_manifest(board)
    gone = [n for n in names if man["nets"].pop(n, None) is not None]
    if gone:
        _save_manifest(board, man)
        _log_release(board, gone, reason, attempts)
    return gone


def commit(base, cand, dst, release=(), complete=None, reason="", log=None, locked=True, manifest=None):
    """THE write of a candidate over a stage board: verify(base, cand), then copy cand -> dst (and its project sidecars; no copy when
    cand is dst) and write dst's manifest = base's entries minus `release` (re-read from the new copper) + `complete` {net: why} + the
    locked items.  -> verify's dict plus "written".  Refused: nothing is written."""
    man = manifest if manifest is not None else load_manifest(base)
    r = verify(base, cand, release, manifest=man if manifest is not None else None)
    r["written"] = False
    if not r["ok"]:
        if log:
            log(f"commit refused ({reason or os.path.basename(cand)}): {r['why']}")
        return r
    if os.path.abspath(cand) != os.path.abspath(dst):
        shutil.copy2(cand, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
        for ext in (".kicad_pro", ".kicad_dru"):
            s = os.path.splitext(cand)[0] + ext
            if os.path.exists(s) and os.path.abspath(s) != os.path.abspath(os.path.splitext(dst)[0] + ext):
                shutil.copy2(s, os.path.splitext(dst)[0] + ext)
    rel = set(release)
    keep = {"nets": {n: e for n, e in man["nets"].items() if n not in rel}}
    seed(dst, complete, keep=keep, locked=locked)
    gone = sorted(n for n in rel if n in man["nets"])
    if gone:
        _log_release(dst, gone, reason or "commit")                         # an explicit, logged release (attempts.jsonl)
    r["written"] = True
    return r


def seed_from_board(board, cons=None, log=None):
    """Seed the manifest of a board that has none (a `--from` run, a hand-made board): its locked items plus the pairs the audit calls
    complete (whole copper).  The audit failing seeds the locked items only (said loudly)."""
    why = {}
    try:
        from . import routediag as RD, netclass as NC, qualaudit as QA, routeconn as RC
        B = RD.load(board)
        cls = NC.classify(B, cons or {})
        for r in QA.pair_rows(B, cls, cons or {}, set(RC.open_by_net(board))):
            if r["status"] == "complete":
                why[r["p"]] = why[r["n"]] = f"pair {r['p']}/{r['n']}"
    except Exception as e:                                                  # noqa: BLE001
        if log:
            log(f"manifest seed: pair audit failed ({type(e).__name__}: {str(e)[:80]}); locked items only")
    return seed(board, why)
