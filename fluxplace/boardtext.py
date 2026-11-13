"""Text-level edits of a .kicad_pcb that need no pcbnew: scan top-level copper forms, remove or add them, take a net off
pads.  Used by the merge (routepar), the pair audit, the via-centre snap and the rail jobs."""
import re

_START = re.compile(r"^[ \t]?\((segment|arc|via)[ \t\n]", re.M)
_NUM = r"(-?\d+(?:\.\d+)?)"


def block_end(text, i):
    """index of the ')' closing the form that opens at text[i] == '('."""
    depth, j, instr = 0, i, False
    n = len(text)
    while j < n:
        c = text[j]
        if instr:
            if c == "\\":
                j += 1
            elif c == '"':
                instr = False
        elif c == '"':
            instr = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n - 1


def _num2(blk, tag):
    m = re.search(r"\(%s %s %s\)" % (tag, _NUM, _NUM), blk)
    return (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)


def key_of(kind, blk):
    net = re.search(r'\(net ("[^"]*"|\d+)\)', blk)
    net = net.group(1) if net else ""
    if kind == "via":
        x, y = _num2(blk, "at")
        sz = re.search(r"\(size %s\)" % _NUM, blk)
        return ("v", round(x, 4), round(y, 4), sz.group(1) if sz else "", net)
    a, b = sorted((tuple(round(v, 4) for v in _num2(blk, "start")), tuple(round(v, 4) for v in _num2(blk, "end"))))
    w = re.search(r"\(width %s\)" % _NUM, blk)
    ly = re.search(r'\(layer "([^"]*)"\)', blk)
    return ("s", a, b, w.group(1) if w else "", ly.group(1) if ly else "", net)


def scan(text):
    """[(start, end_inclusive, key, block)] of the top-level (segment|arc|via ...) forms, in file order."""
    out = []
    for m in _START.finditer(text):
        i = m.start() + m.group(0).index("(")
        j = block_end(text, i)
        blk = text[i:j + 1]
        out.append((i, j, key_of(m.group(1), blk), blk))
    return out


def blocks(text):
    """{key: block text}."""
    return {k: b for _i, _j, k, b in scan(text)}


def net_name(blk):
    m = re.search(r'\(net "([^"]*)"\)', blk)
    return m.group(1) if m else None


def is_locked(blk):
    return bool(re.search(r"\(locked(?: yes)?\)", blk))


def remove_blocks(text, keys):
    """text without the forms whose key is in `keys` (the whole line is dropped)."""
    keys = set(keys)
    out, last = [], 0
    for i, j, k, _b in scan(text):
        if k in keys:
            s = text.rfind("\n", 0, i) + 1
            if text[s:i].strip():
                s = i
            e = j + 1
            if text[e:e + 1] == "\n":
                e += 1
            out.append(text[last:s])
            last = e
    out.append(text[last:])
    return "".join(out)


def append_blocks(text, blks):
    """add copper forms before the final closing paren of the board."""
    blks = list(blks)
    if not blks:
        return text
    return text.rstrip()[:-1].rstrip("\n") + "\n" + "\n".join(blks) + "\n)\n"


_FP = re.compile(r"^[ \t]*\(footprint ", re.M)


def strip_pad_nets(text, net, keep):
    """Take `net` off every pad whose (ref, number) is not in `keep`: the pads stay as obstacles with no net.
    A router job for ONE connection of a rail sees only that connection's pads on the net."""
    tag = '(net "%s")' % net
    out, last = [], 0
    for m in _FP.finditer(text):
        if m.start() < last:
            continue
        i = text.index("(", m.start())
        j = block_end(text, i)
        fp = text[i:j + 1]
        if tag not in fp:
            continue
        rm = re.search(r'\(property "Reference" "([^"]*)"', fp)
        ref = rm.group(1) if rm else ""
        new, pos = [], 0
        for pm in re.finditer(r'\(pad "([^"]*)"', fp):
            if pm.start() < pos:
                continue
            pe = block_end(fp, pm.start())
            pad = fp[pm.start():pe + 1]
            if tag in pad and (ref, pm.group(1)) not in keep:
                pad = re.sub(r"[ \t]*" + re.escape(tag) + r"[ \t]*\n?", "", pad, count=1)
            new.append(fp[pos:pm.start()])
            new.append(pad)
            pos = pe + 1
        new.append(fp[pos:])
        out.append(text[last:i])
        out.append("".join(new))
        last = j + 1
    out.append(text[last:])
    return "".join(out)
