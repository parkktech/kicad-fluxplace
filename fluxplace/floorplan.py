"""Area budget BEFORE placing: does every block fit the space it is meant to live in?
Pure text (routediag model). `fluxplace floorplan-check BOARD --constraints C [--spec S]`.

For each block and each board side: demand = sum of the parts' courtyard areas on that side plus the
escape allowance the legaliser keeps clear around fine-pitch ICs and big connectors (a through-hole part
counts on both sides). Supply = the free area of the region the block is assigned to, or of the whole
side when it has none, after removing the outline's corners and cut-outs, `[placement.keepout.*]` /
`[[keepout]]` rectangles of that side, height-limited regions the block's tall parts cannot use, and the
courtyards of fixed parts that belong to no block assigned there.

    [placement.blocks]                       # or [placement] blocks_dir = "docs/v16-blocks", or spec "block"
    bluetooth = ["U30*", "R30*", "C31*"]     # refs or globs
    [placement.region.bt]                    # a region assigned to blocks
    rect = [0, 40, 40, 70]
    side = "B"
    blocks = ["bluetooth"]
    [placement.floorplan]
    max_utilisation = 0.7                    # REGION_OVERFULL above this (default 0.7)
    cell_mm = 0.5

REGION_OVERFULL (FAIL): a region/side above the utilisation limit, or a part (>= 25 mm2) that has no
free rectangle its size (either orientation) anywhere in its region's free area (the "no slot" case).
Blocks sharing a region/side are judged together.
"""
import fnmatch
import math
from collections import defaultdict

from . import geom as GM
from . import legalise as LG
from . import placechecks as PCK

BIG_MM2 = 25.0
_MECH = ("MH", "MK", "FID", "TP")


class Grid:
    def __init__(self, bbox, cell):
        self.x0, self.y0, self.x1, self.y1, self.c = bbox[0], bbox[1], bbox[2], bbox[3], cell
        self.nx = max(1, int(math.ceil((self.x1 - self.x0) / cell)))
        self.ny = max(1, int(math.ceil((self.y1 - self.y0) / cell)))

    def zeros(self):
        return bytearray(self.nx * self.ny)

    def rect(self, r, val=1, into=None):
        m = into if into is not None else self.zeros()
        i0 = max(0, int(math.floor((r[0] - self.x0) / self.c + 0.5)))
        i1 = min(self.nx, int(math.floor((r[2] - self.x0) / self.c + 0.5)))
        for j in range(max(0, int(math.floor((r[1] - self.y0) / self.c + 0.5))),
                       min(self.ny, int(math.floor((r[3] - self.y0) / self.c + 0.5)))):
            if i1 > i0:
                m[j * self.nx + i0:j * self.nx + i1] = bytes([val]) * (i1 - i0)
        return m

    def outline(self, B):
        m = self.zeros()
        edge = B.get("edge")
        if not edge:
            return self.rect(B["bbox"], 1, m)
        for j in range(self.ny):
            y = self.y0 + (j + 0.5) * self.c
            xs = sorted(a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]) for a, b in edge
                        if (a[1] > y) != (b[1] > y))
            for k in range(0, len(xs) - 1, 2):
                i0 = int(math.ceil((xs[k] - self.x0) / self.c - 0.5))
                i1 = int(math.floor((xs[k + 1] - self.x0) / self.c - 0.5)) + 1
                i0, i1 = max(0, i0), min(self.nx, i1)
                if i1 > i0:
                    m[j * self.nx + i0:j * self.nx + i1] = b"\x01" * (i1 - i0)
        return m

    def area(self, m):
        return sum(m) * self.c * self.c


def _and(a, b):
    return bytearray(x & y for x, y in zip(a, b))


def _sub(a, b):
    return bytearray(x & (1 - y) for x, y in zip(a, b))


def _prefix(G, m):
    """2D summed-area table of a mask."""
    nx = G.nx
    S = [[0] * (nx + 1) for _ in range(G.ny + 1)]
    for j in range(G.ny):
        row, acc = S[j + 1], 0
        up = S[j]
        for i in range(nx):
            acc += m[j * nx + i]
            row[i + 1] = up[i + 1] + acc
    return S


def _fits(G, S, w, h):
    """Is there a w x h mm axis-aligned rectangle entirely inside the mask?"""
    cw, ch = max(1, int(math.ceil(w / G.c))), max(1, int(math.ceil(h / G.c)))
    if cw > G.nx or ch > G.ny:
        return False
    need = cw * ch
    for j in range(G.ny - ch + 1):
        a, b = S[j], S[j + ch]
        for i in range(G.nx - cw + 1):
            if b[i + cw] - b[i] - a[i + cw] + a[i] == need:
                return True
    return False


def part_demand(M, B, ref):
    """(courtyard mm2, escape allowance mm2) of a part."""
    p = B["parts"][ref]
    c = p["court"]
    bare = max(0.0, (c[2] - c[0]) * (c[3] - c[1]))
    rel = M.rel(ref, p["angle"])
    infl = rel[0]
    esc = max(0.0, (infl[2] - infl[0]) * (infl[3] - infl[1]) - (rel[1][2] - rel[1][0]) * (rel[1][3] - rel[1][1]))
    return bare, esc


def regions(B, cons):
    """[(name, rect|None, side, [blocks])]: the named regions."""
    out = []
    for name, r in sorted((((cons or {}).get("placement") or {}).get("region") or {}).items()):
        if isinstance(r, dict) and r.get("rect"):
            side = str(r.get("side", "both")).upper()
            out.append((name, tuple(float(v) for v in r["rect"]), {"F", "B"} if side in ("BOTH", "") else {side[0]},
                        list(r.get("blocks", []))))
    return out


def check(B, cons=None, spec=None, fixed=None):
    """-> (findings, rows). rows = one dict per (region|pool, side): name, side, blocks, demand, free,
    util, parts, escape."""
    from .preroute import _f
    cons = cons or {}
    pl = cons.get("placement") or {}
    fp = pl.get("floorplan") or {}
    limit = float(fp.get("max_utilisation", 0.7))
    G = Grid(B["bbox"], float(fp.get("cell_mm", 0.5)))
    fixed = set(fixed) if fixed is not None else PCK_fixed(B, cons)
    M = LG._Model(B, cons, fixed, B["bbox"])
    blocks = PCK.blocks_of(B, spec, cons)
    block_of = {r: b for b, rs in blocks.items() for r in rs}
    parts = {r: p for r, p in B["parts"].items() if not r.startswith(_MECH)}
    inside = G.outline(B)
    heights = pl.get("heights") or {}
    hr = [(tuple(k["rect"]), str(k.get("side", "B")).upper()[0], float(k["max_height_mm"]))
          for k in (pl.get("height_region") or [])]
    R = regions(B, cons)
    assigned = {b: name for name, _r, _s, bs in R for b in bs}
    findings, rows = [], []
    base = {}
    for side in ("F", "B"):
        m = inside
        for rect, ks, allow, mh, _n in M.ko:
            if side in ks and mh is None:
                m = _sub(m, G.rect(rect))
        base[side] = m
    # who is assigned where
    pools = []          # (name, rect|None, side, blocks or None for "unassigned")
    for name, rect, sides, bs in R:
        for s in sorted(sides):
            pools.append((name, rect, s, set(bs)))
    pool_blocks = set(blocks) - set(assigned)
    for s in ("F", "B"):
        pools.append(("(side " + s + ")", None, s, pool_blocks | {None}))
    expl = {s: [rect for _n, rect, ss, _b in R if s in ss] for s in ("F", "B")}
    for name, rect, side, bs in pools:
        free = base[side]
        if rect is not None:
            free = _and(free, G.rect(rect))
        else:
            for r in expl[side]:
                free = _sub(free, G.rect(r))       # explicit regions are reserved for their own blocks
        mine = [r for r, p in parts.items()
                if block_of.get(r) in bs and (p["side"][0] == side or p["tht"])]
        # height-limited regions: unusable by a block with a part known to be taller than the limit
        hgt = lambda r: next((float(v) for g, v in heights.items() if fnmatch.fnmatch(r, g)), 0.0)
        for rr, hs, mx in hr:
            if hs == side and any(hgt(r) > mx for r in mine):
                free = _sub(free, G.rect(rr))
        # fixed parts that belong to no block here occupy the region
        for r, p in parts.items():
            if r in fixed and r not in mine and (p["side"][0] == side or p["tht"]) and block_of.get(r) not in bs:
                free = _sub(free, G.rect(p["court"]))
        dem = esc = 0.0
        for r in mine:
            a, e = part_demand(M, B, r)
            dem += a
            esc += e
        fa = G.area(free)
        util = (dem + esc) / fa if fa > 0 else (math.inf if dem else 0.0)
        row = {"name": name, "side": side, "blocks": sorted(b for b in bs if b), "demand": round(dem, 1),
               "escape": round(esc, 1), "free": round(fa, 1), "util": round(util, 3), "parts": len(mine)}
        rows.append(row)
        if mine and util > limit:
            bl = ", ".join(row["blocks"]) or "unassigned parts"
            findings.append(_f("FAIL", "REGION_OVERFULL",
                               f"{name} side {side} [{bl}]: {dem + esc:.0f} mm2 needed ({dem:.0f} courtyard + {esc:.0f} escape) "
                               f"of {fa:.0f} mm2 free = {100 * util:.0f} % > {100 * limit:.0f} %",
                               refs=mine[:12], value=round(100 * util, 1), limit=round(100 * limit, 1)))
        # no-slot: a big part with no free rectangle of its size in the region's free area
        if mine:
            S = None
            for r in sorted(mine, key=lambda r: -part_demand(M, B, r)[0]):
                a, _e = part_demand(M, B, r)
                if a < BIG_MM2:
                    break
                if r in fixed:
                    continue
                if S is None:
                    S = _prefix(G, free)
                c = parts[r]["court"]
                w, h = c[2] - c[0], c[3] - c[1]
                if not (_fits(G, S, w, h) or _fits(G, S, h, w)):
                    findings.append(_f("FAIL", "REGION_OVERFULL",
                                       f"{name} side {side}: no free slot for {r} ({w:.1f} x {h:.1f} mm) in the "
                                       f"{fa:.0f} mm2 free area", refs=[r], value=round(a, 1), limit=0))
    return findings, rows


def PCK_fixed(B, cons):
    from .preroute import fixed_parts
    return fixed_parts(B, cons)


def format_text(findings, rows, title=""):
    L = [f"FLOORPLAN {title}".rstrip(),
         f"  {'region / side':22s} {'side':4s} {'parts':>5s} {'need mm2':>9s} {'free mm2':>9s} {'util':>6s}  blocks"]
    for r in rows:
        if r["parts"] or r["name"].startswith("(side") is False:
            L.append(f"  {r['name']:22s} {r['side']:4s} {r['parts']:5d} {r['demand'] + r['escape']:9.0f} "
                     f"{r['free']:9.0f} {100 * r['util']:5.0f}%  {', '.join(r['blocks'])}")
    for f in findings:
        L.append(f"  {f['level']:4s} {f['code']}: {f['msg']}")
    n = sum(1 for f in findings if f["level"] == "FAIL")
    L.append(f"  RESULT: {'REGION_OVERFULL (%d)' % n if n else 'fits'}")
    return "\n".join(L)
