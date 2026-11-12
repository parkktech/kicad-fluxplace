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


def move_file(src, dst, moves):
    text = open(src, errors="surrogateescape").read()
    out, done = apply_moves(text, moves)
    with open(dst, "w", errors="surrogateescape") as fh:
        fh.write(out)
    return done
