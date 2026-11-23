"""KiCad is the authority for routed net length (the number its own skew DRC reports: all copper of the net, vias by stackup height,
in-pad copper included).  `pair_lengths` writes one `(constraint skew)` rule per pair into a temp .kicad_dru next to a temp copy of the
board, runs `kicad-cli pcb drc` and parses `target net length X (from NET); actual Y`.  Nothing here knows a board."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_RE = re.compile(r"rule '(sk\d+)'.*?actual (-?[\d.]+) mm; target net length ([\d.]+) mm \(from ([^)]+)\); actual ([\d.]+) mm")
CACHE = {}


def _q(net):
    return net.replace("\\", "\\\\").replace("'", "\\'")


def pair_lengths(board, pairs, kicad_cli="kicad-cli", timeout=300):
    """{(p, n): {"p": mm | None, "n": mm | None, "skew": P - N signed mm}} for the pairs of `board` (a path), from KiCad's DRC.
    A pair KiCad reports no skew for (equal within 0.0001 mm) has skew 0.0 and lengths None.  None when the CLI fails (the caller
    falls back to its own measure and warns)."""
    pairs = [tuple(p) for p in pairs]
    if not pairs or not board or not os.path.isfile(board):
        return {} if not pairs else None
    st = os.stat(board)
    key = (os.path.abspath(board), st.st_mtime_ns, st.st_size, tuple(pairs))
    if key in CACHE:
        return CACHE[key]
    tmp = tempfile.mkdtemp(prefix="fxlen-")
    try:
        b = os.path.join(tmp, "b.kicad_pcb")
        shutil.copy(board, b)
        pro = os.path.splitext(board)[0] + ".kicad_pro"
        d = {}
        if os.path.isfile(pro):
            try:
                d = json.load(open(pro))
            except (OSError, ValueError):
                d = {}
        rs = d.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rule_severities", {})
        for k in list(rs):                                   # only the skew rule: the run takes ~2 s
            rs[k] = "ignore"
        rs["skew_out_of_range"] = "error"
        json.dump(d, open(os.path.join(tmp, "b.kicad_pro"), "w"))
        open(os.path.join(tmp, "b.kicad_dru"), "w").write("(version 1)\n" + "".join(
            f"(rule \"sk{i}\" (condition \"A.NetName == '{_q(p)}' || A.NetName == '{_q(n)}'\") (constraint skew (max 0.0001mm)))\n"
            for i, (p, n) in enumerate(pairs)))
        out = os.path.join(tmp, "d.json")
        r = subprocess.run([kicad_cli, "pcb", "drc", "--format", "json", "--severity-error", "--output", out, b],
                           capture_output=True, text=True, timeout=timeout)
        if not os.path.isfile(out):
            print(f"netlen: kicad-cli drc failed ({r.returncode}): {(r.stderr or r.stdout)[-200:]}", file=sys.stderr)
            return None
        res = {pr: {"p": None, "n": None, "skew": 0.0} for pr in pairs}
        for v in json.load(open(out)).get("violations", []):
            m = _RE.search(v.get("description", ""))
            if not m:
                continue
            p, n = pairs[int(m.group(1)[2:])]
            tgt, frm, oth = float(m.group(3)), m.group(4), float(m.group(5))
            lp, ln = (tgt, oth) if frm == p else (oth, tgt)
            res[(p, n)] = {"p": lp, "n": ln, "skew": round(lp - ln, 4)}
        CACHE[key] = res
        return res
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        print(f"netlen: kicad-cli drc unavailable: {e}", file=sys.stderr)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
