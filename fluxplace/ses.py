"""Specctra .ses import that actually works headless.

KiCad 10's pcbnew.ImportSpecctraSES() returns True headless but silently
mangles geometry (needs GUI frame state): imported fragments land with
sub-mm gaps and kicad-cli reports 10-15 'missing connection' items per
board on plane nets. Parsing the session ourselves and building
PCB_TRACK/PCB_VIA directly produces byte-exact copper (0 unconnected on the
same session — proven on tournament-a1/landscape-t1, whose importer this
module ports from utv-comms-bridge hardware/tools/import_specctra_ses.py).

Coords: ses units * 100 = nm, Y flipped. One board per process — callers
should run this in a throwaway interpreter (SWIG cleanup segfaults).
"""
import re

__all__ = ["import_into"]


def _tokenize(s):
    return re.findall(r'\(|\)|"[^"]*"|[^\s()]+', s)


def _parse(tokens):
    it = iter(tokens)

    def rd():
        node = []
        for tok in it:
            if tok == '(':
                node.append(rd())
            elif tok == ')':
                return node
            else:
                node.append(tok.strip('"'))
        return node

    for tok in it:
        if tok == '(':
            return rd()
    return []


def _find_all(node, key):
    if isinstance(node, list):
        if node and node[0] == key:
            yield node
        for c in node:
            yield from _find_all(c, key)


def import_into(board, ses_path, replace=True):
    """Build tracks/vias from `ses_path` onto `board` (a loaded pcbnew
    BOARD). Returns (tracks, vias, skipped_nets, unconnected_after).

    replace=True (the default, and almost always what you want) clears the
    board's existing tracks and vias first.

    A Specctra session is the COMPLETE routing for the board its DSN was
    exported from, not a patch. Importing one onto a board that still holds its
    original copper doubles every segment, and the duplicates land exactly on
    top of each other — which DRC reports as co-located holes rather than as
    anything resembling the real cause. Measured on utv-comms-v14: importing
    without clearing produced 199 holes_co_located and 334 violations total;
    clearing first, from the same session file, gave 131. Same routing, same
    board; the difference was entirely doubled copper.

    Pass replace=False only if you know the session covers a subset and you
    intend to merge.
    """
    import pcbnew
    if replace:
        for t in list(board.GetTracks()):
            board.Remove(t)
    layer_id = {pcbnew.LayerName(l): l
                for l in board.GetEnabledLayers().CuStack()}
    ni = board.GetNetInfo()
    net_by_name = {ni.GetNetItem(i).GetNetname(): ni.GetNetItem(i).GetNetCode()
                   for i in range(ni.GetNetCount())}
    tree = _parse(_tokenize(open(ses_path, errors="ignore").read()))
    SC = 100  # ses unit -> nm

    def X(v):
        return int(round(float(v) * SC))

    def Y(v):
        return int(round(-float(v) * SC))

    n_tracks = n_vias = n_skip = 0
    for net in _find_all(tree, "net"):
        if len(net) < 2 or not isinstance(net[1], str):
            continue
        nc = net_by_name.get(net[1])
        if nc is None:
            n_skip += 1
            continue
        for wire in _find_all(net, "wire"):
            for path in _find_all(wire, "path"):
                lid = layer_id.get(path[1])
                if lid is None:
                    continue
                width = int(round(float(path[2]) * SC))
                coords = path[3:]
                pts = [(X(coords[i]), Y(coords[i + 1]))
                       for i in range(0, len(coords) - 1, 2)]
                for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                    if (x1, y1) == (x2, y2):
                        continue
                    tr = pcbnew.PCB_TRACK(board)
                    tr.SetStart(pcbnew.VECTOR2I(x1, y1))
                    tr.SetEnd(pcbnew.VECTOR2I(x2, y2))
                    tr.SetWidth(width)
                    tr.SetLayer(lid)
                    tr.SetNetCode(nc)
                    board.Add(tr)
                    n_tracks += 1
        for via in _find_all(net, "via"):
            try:
                x, y = X(via[2]), Y(via[3])
            except (IndexError, ValueError):
                continue
            v = pcbnew.PCB_VIA(board)
            v.SetPosition(pcbnew.VECTOR2I(x, y))
            m = re.search(r"_(\d+):(\d+)_um", via[1])
            v.SetWidth(int(m.group(1)) * 1000 if m else 600000)
            v.SetDrill(int(m.group(2)) * 1000 if m else 300000)
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            v.SetNetCode(nc)
            board.Add(v)
            n_vias += 1
    board.BuildConnectivity()
    unrouted = board.GetConnectivity().GetUnconnectedCount(True)
    return n_tracks, n_vias, n_skip, unrouted
