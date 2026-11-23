"""KiCad is the authority for routed net length (the number its own skew DRC reports: all copper of the net, vias by stackup height,
in-pad copper included).  `pair_lengths` writes one `(constraint skew)` rule per pair into a temp .kicad_dru next to a temp copy of the
board, runs `kicad-cli pcb drc` and parses `target net length X (from NET); actual Y`.  Nothing here knows a board."""
import os
import re
import subprocess
import sys

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
    try:
        rules = "(version 1)\n" + "".join(
            f"(rule \"sk{i}\" (condition \"A.NetName == '{_q(p)}' || A.NetName == '{_q(n)}'\") (constraint skew (max 0.0001mm)))\n"
            for i, (p, n) in enumerate(pairs))
        from . import drcguard as DG
        try:
            rep = DG.drc_json(board, kicad_cli, timeout, rules=rules, only_rules=["skew_out_of_range"], complete=False)
        except RuntimeError as e:
            print(f"netlen: kicad-cli drc failed: {e}", file=sys.stderr)
            return None
        res = {pr: {"p": None, "n": None, "skew": 0.0} for pr in pairs}
        for v in rep.get("violations", []):
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

