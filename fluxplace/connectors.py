"""Connector knowledge: entry direction and mating axis of every connector, read from the footprint.

  entry    "card"  a socket for a card that lies ON the board (M.2, SIM): no edge rule; the card area is a rigid group's
                   swept area (`[[intent.group]] swept`), proposed by `intent-draft`;
           "side"  the plug enters in the board plane (horizontal / right-angle / edge-mount): the part belongs on an
                   outline edge with its opening pointing out (intent.py turns this into an implicit `edge` item);
           "top"   the plug enters along z (vertical / straight / board-to-board): it needs open space above, so it may
                   not sit under a height-limited region or the swept area of a rigid group;
           "unknown"  neither the name nor the geometry decides: ASKED (`connector-entry:<footprint>`), never assumed.
  opening  world unit vector of the mating side for a side-entry part (None when the geometry does not show it).
  how      declared (`[intent.connectors] REF = {entry = "side", opening = "+y", edge = false}`, or the older
           `[placement.connectors] REF = {opening = ...}`), name (footprint name pattern), geometry (courtyard reaches
           well beyond the pads on exactly one side), answer (a recorded answer), none.

Table: `fluxplace intent BOARD` prints it (section `connectors`) and writes it to the JSON report.
"""
import re

from . import geom as GM

CONN_REF = re.compile(r"^(J|P|CN|CON|X|USB)\d")
CONN_LIB = re.compile(r"^(Connector|Conn)[_:]", re.I)
SIDE = re.compile(r"horizontal|right[-_ .]?angle|angled|edge[-_ ]?(mount|launch)|(^|_)RA(_|$)|side[-_ ]?entry|90deg", re.I)
TOP = re.compile(r"vertical|straight|top[-_ ]?entry|(^|_)ST(_|$)|upright|mezzanine|board[-_ ]?to[-_ ]?board|b2b"
                 r"|U\.?FL|MHF\d|IPEX|W\.FL|MMCX", re.I)
CARD = re.compile(r"M\.?2(_|$)|NGFF|mPCIe|mini[-_ ]?PCI|card[-_ ]?edge|SIM|SD[-_ ]?card|microSD", re.I)
EDGE_LAUNCH = re.compile(r"edge[-_ ]?(mount|launch)", re.I)
NOT_CONN = re.compile(r"crystal|oscillator|resonator|xtal|testpoint|test[-_ ]?point|mounting|fiducial", re.I)
DIRS = {"+x": (1.0, 0.0), "-x": (-1.0, 0.0), "+y": (0.0, 1.0), "-y": (0.0, -1.0)}
ASYM_MM, ASYM_RATIO = 2.0, 0.5


def is_connector(ref, part):
    """a part people plug something into: connector reference or a connector footprint library, two pads or more."""
    if part["n_pads"] < 2 or not any(not q.get("np") for q in part["pads"]) or NOT_CONN.search(str(part.get("fp", ""))):
        return False
    return bool(CONN_REF.match(ref) or CONN_LIB.search(str(part.get("fp", ""))))


def declared(cons, ref):
    c = dict((((cons or {}).get("placement") or {}).get("connectors") or {}).get(ref) or {})
    c.update((((cons or {}).get("intent") or {}).get("connectors") or {}).get(ref) or {}
             if isinstance((((cons or {}).get("intent") or {}).get("connectors") or {}).get(ref), dict) else {})
    return c


def local_slack(part):
    """{dir: mm the courtyard reaches beyond the pad box}, footprint frame."""
    px, py = part["pos"]
    loc = [GM.rot(p["x"] - px, p["y"] - py, -part["angle"]) for p in part["pads"]]
    c = part["court"]
    cs = [GM.rot(x - px, y - py, -part["angle"]) for x, y in ((c[0], c[1]), (c[2], c[1]), (c[2], c[3]), (c[0], c[3]))]
    cb = (min(a for a, _ in cs), min(b for _, b in cs), max(a for a, _ in cs), max(b for _, b in cs))
    pb = (min(a for a, _ in loc), min(b for _, b in loc), max(a for a, _ in loc), max(b for _, b in loc))
    return {"-x": pb[0] - cb[0], "+x": cb[2] - pb[2], "-y": pb[1] - cb[1], "+y": cb[3] - pb[3]}


def world(part, key):
    dx, dy = GM.rot(*DIRS[key], part["angle"])
    return (round(dx, 6), round(dy, 6))


def local_key(part, vec):
    """footprint-frame direction name of a world vector."""
    lx, ly = GM.rot(vec[0], vec[1], -part["angle"])
    return max(DIRS, key=lambda k: DIRS[k][0] * lx + DIRS[k][1] * ly)


def classify(B, ref, cons=None, answers=None):
    """-> {ref, fp, entry, opening, key, how}; `key` is the opening in the footprint frame (+x, -x, +y, -y) or None."""
    part = B["parts"][ref]
    name = str(part.get("fp", ""))
    short = name.split(":")[-1]
    d = declared(cons, ref)
    out = {"ref": ref, "fp": short, "entry": "unknown", "opening": None, "key": None, "how": "none"}
    sl = local_slack(part)
    k, v = max(sorted(sl.items()), key=lambda kv: kv[1])
    second = sorted(sl.values())[-2]
    geo = k if v >= 1.0 and second <= 0.6 * v else None
    strong = k if v >= ASYM_MM and second <= ASYM_RATIO * v else None
    op = d.get("opening")
    ent = str(d.get("entry", "")).lower()
    if op == "z" or ent == "top":
        out.update(entry="top", how="declared")
    elif op in DIRS or ent == "side":
        key = op if op in DIRS else geo
        out.update(entry="side", key=key, how="declared")
    elif (answers or {}).get(short) in ("side", "top"):
        out.update(entry=answers[short], key=geo if answers[short] == "side" else None, how="answer")
    else:
        side, top = bool(SIDE.search(short)), bool(TOP.search(short))
        rows = _rows(part)
        if side and not top:
            out.update(entry="side", key=geo, how="name")
        elif top and not side:
            out.update(entry="top", how="name")
        elif CARD.search(short):
            out.update(entry="card", how="name")
        elif strong:
            out.update(entry="side", key=strong, how="geometry")
        elif rows >= 2 and not part["tht"] and v - second < 0.3:
            out.update(entry="top", how="geometry")         # an SMD pad grid in a symmetric courtyard: mates along z
    if out["key"]:
        out["opening"] = world(part, out["key"])
    return out


def _rows(part):
    """number of distinct pad rows along the shorter pad-box axis (a grid of pins is a vertical header or a socket)."""
    px, py = part["pos"]
    loc = [GM.rot(p["x"] - px, p["y"] - py, -part["angle"]) for p in part["pads"] if not p.get("np")]
    xs, ys = sorted({round(a, 1) for a, _ in loc}), sorted({round(b, 1) for _, b in loc})
    return min(len(xs), len(ys))


def table(B, cons=None, answers=None):
    """[classify(...)] for every connector on the board, by reference."""
    return [classify(B, r, cons, answers) for r in sorted(B["parts"]) if is_connector(r, B["parts"][r])]


def answers_of(cons):
    """{footprint: side|top} from recorded answers (`[answers] "connector-entry:<footprint>" = "side"`)."""
    out = {}
    for k, v in ((cons or {}).get("answers") or {}).items():
        if str(k).startswith("connector-entry:"):
            out[str(k).split(":", 1)[1]] = str(v)
    return out


def ask(S, B, cons, **_):
    """question topic `connector-entry`: one question per footprint whose direction cannot be derived."""
    ans = dict(answers_of(cons))
    groups = {}
    for row in table(B, cons, ans):
        if row["entry"] == "unknown":
            groups.setdefault(row["fp"], []).append(row["ref"])
    for fp in sorted(groups):
        refs = groups[fp]

        def apply(box, ch, refs=tuple(refs)):
            con = box["cons"].setdefault("intent", {}).setdefault("connectors", {})
            for r in refs:
                con.setdefault(r, {})["entry"] = ch
        S.ask(f"connector-entry:{fp}", "connector-entry",
              f"{', '.join(refs[:6])}{'...' if len(refs) > 6 else ''} (footprint {fp}): the name and the courtyard do not "
              f"say whether the plug enters from the side or from above",
              {"side": "plug enters in the board plane: the part must sit on an outline edge, opening outward",
               "top": "plug enters from above: it may sit anywhere with open space above it"},
              "top", "[intent.connectors] REF = {entry = \"side\"|\"top\", opening = \"+x\"|\"-x\"|\"+y\"|\"-y\"}", apply=apply)


def overhang(part, key, edge_clearance=0.3):
    """mm an edge-launch footprint is drawn to pass the outline: the courtyard beyond the pads on the opening side, less the
    edge clearance the pads keep. 0 for anything whose name does not say edge mount / edge launch."""
    if not key or not EDGE_LAUNCH.search(str(part.get("fp", ""))):
        return 0.0
    return max(0.0, local_slack(part)[key] - edge_clearance)


def overhang_max(part, key, edge_clearance=0.3):
    """the most a side-entry footprint can pass the outline with its pads still on the board: the courtyard beyond the
    pads on the opening side, less the edge clearance."""
    return max(0.0, local_slack(part)[key] - edge_clearance) if key else 0.0


def format_table(rows):
    out = [f"{'ref':8s}{'entry':9s}{'opening':10s}{'how':10s}footprint"]
    for r in rows:
        op = "-" if not r["opening"] else f"({r['opening'][0]:+.0f},{r['opening'][1]:+.0f})"
        out.append(f"{r['ref']:8s}{r['entry']:9s}{op:10s}{r['how']:10s}{r['fp'][:60]}")
    return "\n".join(out)
