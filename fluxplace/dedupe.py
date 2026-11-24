"""Exact duplicate copper: segment/arc/via blocks whose boardtext.key_of (geometry, width, layer, net) equals an earlier
block's. Keeps the first; a locked copy wins over an unlocked one. Removal is by span (not by key), so one copy survives."""
from . import boardtext as BT


def duplicates(text):
    """-> [(start, end) spans of the blocks to drop, in file order] (later copies; when the first copy is unlocked and a
    later one locked, the first goes)."""
    first, kill = {}, []
    for i, j, k, blk in BT.scan(text):
        if k not in first:
            first[k] = ((i, j), BT.is_locked(blk))
            continue
        fspan, flock = first[k]
        if BT.is_locked(blk) and not flock:
            kill.append(fspan)
            first[k] = ((i, j), True)
        else:
            kill.append((i, j))
    return sorted(kill)


def remove_spans(text, spans):
    """text without the forms at `spans` ((start, end) from BT.scan); the whole line is dropped, as BT.remove_blocks does."""
    out, last = [], 0
    for i, j in sorted(spans):
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


def dedupe_text(text):
    """-> (text without exact duplicate copper, removed count)."""
    kill = duplicates(text)
    return (remove_spans(text, kill), len(kill)) if kill else (text, 0)
