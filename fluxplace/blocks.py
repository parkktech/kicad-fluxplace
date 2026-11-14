"""Functional blocks: one inventory shared by the schematic (sheets + `Functional Group` symbol field), the board
(`Functional Group` footprint field), placement (`[placement] use_blocks`, modules.build) and review (BLOCK_SCATTERED).

inventory(M, cons): M is a netlist model (schinv.from_spec / schinv.from_board / model_from_board).
  1. `[blocks] NAME = ["U5", "C5*", ...]` claims parts first (`[schematic.blocks]` is accepted as an alias; both merge).
     A ref claimed by two blocks is a FAIL (BlockError). A pattern that matches nothing is a WARN.
  2. every other part gets the automatic grouping of schinv.inventory (modules, merged by shared signal nets).
Returns {"blocks": [{name, refs, how, anchor}], "of": {ref: block}, "warnings": [str]}.

spread(B, inv): per block, the placed footprint spread: bounding box, area, number of spatial clusters (parts closer than
`link_mm` chain together), parts outside the main cluster, and BLOCK_SCATTERED when another block's part lies between two
clusters of the same block (within `sep_mm` of the straight line joining them).

write_board(board_path, out, inv): sets the hidden footprint field `Functional Group` on every footprint (pcbnew).
"""
import fnmatch
import json
import math
from collections import defaultdict

from . import schinv as I

FIELD = "Functional Group"


class BlockError(ValueError):
    pass


def model_from_board(B, name="board"):
    """A schinv-style netlist model from a routediag board model (B): pads with a net, nets as (ref, pad) lists."""
    comps = {r: {"ref": r, "value": str(p.get("value", "")), "fp": str(p.get("fp", "")), "dnp": False,
                 "pins": {}, "nc": [], "pinmap": {}, "errors": []} for r, p in B["parts"].items()}
    nets = defaultdict(list)
    for q in B["pads"]:
        n = q.get("net")
        if not n or q["ref"] not in comps:
            continue
        comps[q["ref"]]["pins"][str(q["num"])] = {"name": "", "net": n}
        nets[n].append((q["ref"], str(q["num"])))
    return {"name": name, "comps": comps, "nets": {n: sorted(v) for n, v in nets.items()}, "source": name}


def _claims(M, cons):
    """{block: [refs]} from `[blocks]` plus the `[schematic.blocks]` alias, plus warnings. Raises BlockError on a clash."""
    table = defaultdict(list)
    for src in (((cons or {}).get("schematic") or {}).get("blocks") or {}, (cons or {}).get("blocks") or {}):
        for name, pats in src.items():
            table[str(name)].extend([pats] if isinstance(pats, str) else list(pats))
    refs = sorted(M["comps"], key=I.natkey)
    owner, claims, warns, clash = {}, defaultdict(list), [], []
    for name, pats in table.items():
        for p in pats:
            hits = [r for r in refs if fnmatch.fnmatchcase(r, p)]
            if not hits:
                warns.append(f"[blocks] {name!r}: '{p}' matches no part")
            for r in hits:
                if owner.get(r, name) != name:
                    clash.append(f"{r} is in two blocks: {owner[r]!r} and {name!r}")
                    continue
                if r not in owner:
                    owner[r] = name
                    claims[name].append(r)
    if clash:
        raise BlockError("; ".join(clash[:10]))
    return dict(claims), warns


def inventory(M, cons=None):
    cons = cons or {}
    claims, warns = _claims(M, cons)
    sc = dict(cons.get("schematic") or {})
    sc["blocks"] = {k: sorted(v, key=I.natkey) for k, v in claims.items()}
    inv = I.inventory(M, dict(cons, schematic=sc))
    return {"blocks": inv["blocks"], "of": inv["of"], "warnings": warns, "modules": inv.get("modules", 0)}


def explicit_claims(B, cons=None, spec=None):
    """{block: [refs]} from `[blocks]` / `[schematic.blocks]` ONLY: no automatic groups. Raises BlockError on a clash."""
    M = I.from_spec(spec) if isinstance(spec, str) else model_from_board(B)
    claims, _warns = _claims(M, cons or {})
    return {k: sorted(v, key=I.natkey) for k, v in claims.items()}


def of_board(B, cons=None, spec=None):
    """inventory for placement: from the spec file when given (the netlist of record), else from the board's pads."""
    return inventory(I.from_spec(spec) if isinstance(spec, str) else model_from_board(B), cons)


def block_of(inv, ref):
    return inv["of"].get(ref)


def _sep(a, b, p):
    """distance from point p to segment ab"""
    (ax, ay), (bx, by), (px, py) = a, b, p
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


def _clusters(pts, link):
    """single-linkage clusters of {ref: (x, y)}: lists of refs, largest first"""
    par = {r: r for r in pts}

    def find(r):
        while par[r] != r:
            par[r] = par[par[r]]
            r = par[r]
        return r
    rs = sorted(pts, key=I.natkey)
    for i, a in enumerate(rs):
        for b in rs[i + 1:]:
            if math.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1]) <= link:
                par[find(a)] = find(b)
    groups = defaultdict(list)
    for r in rs:
        groups[find(r)].append(r)
    return sorted(groups.values(), key=lambda g: (-len(g), I.natkey(g[0])))


def spread(B, inv, link_mm=6.0, sep_mm=3.0):
    """per-block placed spread -> [{block, n, bbox, area_mm2, clusters, outside_main, scattered, between}]"""
    pos = {r: p["pos"] for r, p in B["parts"].items()}
    rows = []
    for b in inv["blocks"]:
        name = b["name"]
        pts = {r: pos[r] for r in b["refs"] if r in pos}
        if not pts or b.get("how") == "mechanical":        # mounting hardware is spread by design
            continue
        xs = [p[0] for p in pts.values()]
        ys = [p[1] for p in pts.values()]
        cl = _clusters(pts, link_mm)
        main = set(cl[0])
        between = []
        others = [(r, pos[r]) for r in B["parts"] if inv["of"].get(r) not in (None, name) and r in pos]
        # a region counts when it is not a stray: >= 3 parts or >= 15 % of the block (single parts are `outside_main`)
        big = [c for c in cl[1:] if len(c) >= 3 or len(c) >= 0.15 * len(pts)]
        for c in big:
            # the closest pair of parts between the main cluster and this one
            a, c_ = min(((x, y) for x in main for y in c), key=lambda ab: math.hypot(pts[ab[0]][0] - pts[ab[1]][0],
                                                                                 pts[ab[0]][1] - pts[ab[1]][1]))
            for r, p in others:
                if _sep(pts[a], pts[c_], p) <= sep_mm and r not in between:
                    between.append(r)
        rows.append({"block": name, "n": len(pts), "bbox": [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2),
                                                             round(max(ys), 2)],
                     "area_mm2": round((max(xs) - min(xs)) * (max(ys) - min(ys)), 1), "clusters": len(cl),
                     "outside_main": sum(len(c) for c in cl[1:]), "scattered": bool(between), "regions": 1 + len(big),
                     "between": sorted(between, key=I.natkey)[:12]})
    return rows


def check(B, inv, level="INFO", link_mm=6.0, sep_mm=3.0):
    """-> review findings (preroute._f shape): BLOCK_SCATTERED for each block split with another block between."""
    from .preroute import _f
    out = []
    for row in spread(B, inv, link_mm, sep_mm):
        if row["scattered"]:
            out.append(_f(level, "BLOCK_SCATTERED",
                          f"block '{row['block']}' ({row['n']} parts) is split into {row['clusters']} regions with another "
                          f"block's parts between them: {', '.join(row['between'][:8])}",
                          refs=row["between"], value=row["outside_main"]))
    return out


def write_board(board_path, out_path, inv):
    """Set the hidden `Functional Group` field on every footprint (pcbnew, the same path sync-nets uses)."""
    from . import kicad_io as IO
    board = IO.load(board_path)
    n = 0
    for fp in board.GetFootprints():
        name = inv["of"].get(fp.GetReference())
        if not name:
            continue
        if fp.HasField(FIELD):
            f = fp.GetField(FIELD)
            f.SetText(name)
        else:
            fp.SetField(FIELD, name)
            f = fp.GetField(FIELD)
        f.SetVisible(False)
        n += 1
    IO.save(board, out_path)
    return n


def _text(inv, rows=None):
    lines = []
    for b in inv["blocks"]:
        lines.append(f"  {b['name']} [{b['how']}] {len(b['refs'])}: {', '.join(b['refs'][:12])}"
                     + (" ..." if len(b["refs"]) > 12 else ""))
    for w in inv["warnings"]:
        lines.append(f"  WARN {w}")
    if rows is not None:
        lines.append(f"{'block':28} {'n':>4} {'clusters':>8} {'outside':>7} {'area mm2':>9}  bbox  scattered")
        for r in rows:
            lines.append(f"{r['block'][:28]:28} {r['n']:>4} {r['clusters']:>8} {r['outside_main']:>7} {r['area_mm2']:>9}  "
                         f"{r['bbox']}  {'YES ' + ','.join(r['between'][:4]) if r['scattered'] else 'no'}")
    return lines


def cmd_blocks(a):
    import os
    from . import constraints as C
    from . import routediag as RD
    cons = C.load(a.constraints)
    B = RD.load(a.board) if a.board else None
    if not (a.spec or B):
        raise SystemExit("blocks: a board or --spec is required")
    if a.write and not a.board:
        raise SystemExit("blocks: --write needs a board (--board); a spec alone has no board to write")
    if a.write and not a.out:
        raise SystemExit("blocks: --write needs --out (the input board is never written in place by default)")
    try:
        inv = of_board(B, cons, a.spec)
    except BlockError as e:
        raise SystemExit(f"blocks: {e}")
    rows = spread(B, inv, a.link, a.sep) if (a.check and B) else None
    level = "WARN" if (cons.get("placement") or {}).get("use_blocks") else "INFO"
    findings = check(B, inv, level, a.link, a.sep) if rows is not None else []
    if a.write:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
        n = write_board(a.board, a.out, inv)
        print(f"blocks: wrote {FIELD!r} on {n} footprint(s) -> {a.out}")
    if a.json:
        print(json.dumps({"blocks": inv["blocks"], "warnings": inv["warnings"], "spread": rows,
                          "findings": findings}, indent=1, default=str))
    else:
        print("\n".join(_text(inv, rows)))
        for f in findings:
            print(f"  {f['level']} {f['code']}: {f['msg']}")
        if rows is not None:
            print(f"blocks --check: {sum(1 for r in rows if r['scattered'])} scattered of {len(rows)}")


def add_parser(sub):
    p = sub.add_parser("blocks", help="functional-block inventory: --check placed spread, --write Functional Group field")
    p.add_argument("board", nargs="?", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--constraints", default=None)
    p.add_argument("--check", action="store_true", help="per-block spread on the placed board (BLOCK_SCATTERED)")
    p.add_argument("--write", action="store_true", help="write the Functional Group field to --out")
    p.add_argument("--out", default=None)
    p.add_argument("--link", type=float, default=6.0, help="mm: parts closer than this are one region")
    p.add_argument("--sep", type=float, default=3.0, help="mm: a foreign part this close to the line between regions splits them")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_blocks)
