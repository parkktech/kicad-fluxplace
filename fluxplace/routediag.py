"""route-diagnose: explain WHY an autorouter left nets unrouted, per net, and
say what to change in placement. Read-only; pure text (boardfile.parse), no
pcbnew. Causes: SEALED_PAD, FAR_SUPPORT_PART, LONG_HAUL, KEEPOUT_TRAPPED,
SUB_RULE_PAD, CONGESTION (fallback hint)."""
import json
import math
import os
import re
from collections import defaultdict

from . import boardfile as BF

FINE_GAP = 0.3          # mm, neighbour-pad gap below which a part is fine-pitch
FAR_MM, FAR_TIGHT_MM = 5.0, 2.5
HAUL_MM, HAUL_FRAC = 30.0, 0.35
MIN_NECK = 0.127
TIGHT_NAMES = ("DECAP", "VREF", "BYP")
PASSIVE = re.compile(r"^(FB|C|R|L)\d")
NOT_IC = re.compile(r"^(FB|C|R|L|TP|MH|H|FID|MK|D|F)\d|^(TP|MH|FID)")
GROUND = re.compile(r"^(GND|AGND|DGND|PGND|GND_\w+|VSS)$", re.I)
CAUSES = ["SEALED_PAD", "FAR_SUPPORT_PART", "LONG_HAUL", "KEEPOUT_TRAPPED",
          "SUB_RULE_PAD", "CONGESTION"]


# ------------------------------------------------------------------ log
_START = re.compile(r"\]\s+(?:Routing|Re-routing ripped net)\s+(\S+?)(?:\s+\(id=\d+\))?\s*$")
_END = re.compile(r"^\s+(?:REROUTE\s+)?(?:RETRY\s+)?(SUCCESS|FAILED)\b")


def parse_logs(paths):
    """{net: 'SUCCESS'|'FAILED'}; a net's status is its LAST attempt."""
    st = {}
    for p in paths:
        cur = None
        for line in open(p, errors="replace"):
            m = _START.search(line)
            if m and "Routing MST" not in line:
                cur = m.group(1)
                continue
            m = _END.match(line)
            if m and cur:
                st[cur] = m.group(1)
    return st


# ------------------------------------------------------------------ board
def _poly_has(pts, x, y):
    ins = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            ins = not ins
        j = i
    return ins


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _f(node, tag, n=2):
    k = BF._first(node, tag)
    v = (BF._nums(k) if k else []) + [0.0] * n
    return v[:n]


def load(path, track=None, clearance=None):
    root = BF.parse(open(path, errors="replace").read())
    copper = [a[1] for a in (BF._first(root, "layers") or [])[1:]
              if isinstance(a, list) and len(a) > 1 and a[1].endswith(".Cu")]
    netname = {}
    for k in BF._kids(root, "net"):
        a = BF._atoms(k)
        if len(a) >= 2:
            netname[a[0]] = a[1]
    nm = lambda n: netname.get(n, n)

    pads, parts, zones = [], {}, []

    def zone_of(z, fx=0.0, fy=0.0, fang=0.0):
        keep = BF._first(z, "keepout")
        ls = BF._atoms(BF._first(z, "layers")) + BF._atoms(BF._first(z, "layer"))
        lay = set()
        for l in ls:
            if l == "*.Cu":
                lay |= set(copper)
            elif l == "F&B.Cu":
                lay |= {copper[0], copper[-1]} if copper else set()
            else:
                lay.add(l)
        poly = BF._first(BF._first(z, "polygon") or [], "pts") or []
        pts = []
        for xy in BF._kids(poly, "xy"):
            x, y = BF._nums(xy)[:2]
            rx, ry = BF._rot(x, y, fang)
            pts.append((fx + rx, fy + ry))
        return {"net": nm(BF._net(z)), "layers": lay, "pts": pts,
                "keepout": bool(keep),
                "no_tracks": bool(keep) and any(
                    k[:2] == ["tracks", "not_allowed"] for k in keep if isinstance(k, list)),
                "name": (BF._atoms(BF._first(z, "name")) or [""])[0]}

    for fp in BF._kids(root, "footprint"):
        props = {}
        for pr in BF._kids(fp, "property"):
            a = BF._atoms(pr)
            if len(a) >= 2:
                props.setdefault(a[0], a[1])
        ref = props.get("Reference", "")
        fx, fy, fang = (_f(fp, "at", 3))
        side = "B.Cu" if (BF._atoms(BF._first(fp, "layer")) or ["F.Cu"])[0] == "B.Cu" else "F.Cu"
        mine = []
        for pad in BF._kids(fp, "pad"):
            a = BF._atoms(pad)
            if not a or not a[0]:
                continue
            px, py, pa = _f(pad, "at", 3)
            dx, dy = BF._rot(px, py, fang)
            w, h = _f(pad, "size")
            rad = math.radians(pa)
            ex = abs(w * math.cos(rad)) / 2 + abs(h * math.sin(rad)) / 2
            ey = abs(w * math.sin(rad)) / 2 + abs(h * math.cos(rad)) / 2
            ls = BF._atoms(BF._first(pad, "layers"))
            lay = set()
            for l in ls:
                if l == "*.Cu":
                    lay |= set(copper)
                elif l.endswith(".Cu"):
                    lay.add(l)
            ptype = a[1] if len(a) > 1 else ""
            p = {"ref": ref, "num": a[0], "x": fx + dx, "y": fy + dy,
                 "w": w, "h": h, "ex": ex, "ey": ey, "min": min(w, h),
                 "layers": lay or {side}, "net": nm(BF._net(pad)), "angle": pa,
                 "shape": a[2] if len(a) > 2 else "",
                 "tht": ptype in ("thru_hole", "np_thru_hole")}
            pads.append(p)
            mine.append(p)
        for z in BF._kids(fp, "zone"):
            zones.append(zone_of(z, fx, fy, fang))
        cy_pts = []
        for g in fp:
            if (isinstance(g, list) and g and g[0].startswith("fp_")
                    and (BF._atoms(BF._first(g, "layer")) or [""])[0].endswith("CrtYd")):
                pts, _hw = BF._shape_pts(g)
                for px, py in pts:
                    rx, ry = BF._rot(px, py, fang)
                    cy_pts.append((fx + rx, fy + ry))
        if cy_pts:
            court = (min(q[0] for q in cy_pts), min(q[1] for q in cy_pts),
                     max(q[0] for q in cy_pts), max(q[1] for q in cy_pts))
        elif mine:
            court = (min(q["x"] - q["ex"] for q in mine) - 0.25, min(q["y"] - q["ey"] for q in mine) - 0.25,
                     max(q["x"] + q["ex"] for q in mine) + 0.25, max(q["y"] + q["ey"] for q in mine) + 0.25)
        else:
            court = (fx, fy, fx, fy)
        parts[ref] = {"pos": (fx, fy), "pads": mine, "side": side,
                      "value": props.get("Value", ""), "angle": fang,
                      "tht": any(q["tht"] for q in mine),
                      "fp": (BF._atoms(fp) or [""])[0], "court": court,
                      "locked": any(k == "locked" or (isinstance(k, list) and k[:2] == ["locked", "yes"])
                                    for k in fp if isinstance(k, (str, list)) and (
                                        k == "locked" or (isinstance(k, list) and k and k[0] == "locked"))),
                      "n_pads": len({p["num"] for p in mine})}
    for z in BF._kids(root, "zone"):
        zones.append(zone_of(z))

    segs, vias = [], []
    for n in root:
        if not isinstance(n, list) or not n or n[0] not in ("segment", "arc", "via"):
            continue
        net = nm(BF._net(n))
        if n[0] == "via":
            x, y = _f(n, "at")
            vias.append({"net": net, "x": x, "y": y, "d": _f(n, "size", 1)[0],
                         "layers": set(BF._atoms(BF._first(n, "layers")))})
        else:
            sx, sy = _f(n, "start")
            ex, ey = _f(n, "end")
            segs.append({"net": net, "a": (sx, sy), "b": (ex, ey),
                         "w": _f(n, "width", 1)[0],
                         "layer": (BF._atoms(BF._first(n, "layer")) or [""])[0]})

    xs, ys = [], []
    for g in root:
        if isinstance(g, list) and g and g[0].startswith("gr_") and (
                BF._atoms(BF._first(g, "layer")) or [""])[0] == "Edge.Cuts":
            for k in g:
                if isinstance(k, list) and k and k[0] in ("start", "end", "mid", "center"):
                    v = BF._nums(k)
                    xs.append(v[0]); ys.append(v[1])
                if isinstance(k, list) and k and k[0] == "pts":
                    for xy in BF._kids(k, "xy"):
                        v = BF._nums(xy); xs.append(v[0]); ys.append(v[1])
    if not xs:
        xs = [p["x"] for p in pads]; ys = [p["y"] for p in pads]
    bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 1, 1)

    dtrack, dclear = 0.15, 0.14
    pro = os.path.splitext(path)[0] + ".kicad_pro"
    if os.path.exists(pro):
        try:
            for c in json.load(open(pro))["net_settings"]["classes"]:
                if c.get("name") == "Default":
                    dtrack = float(c.get("track_width", dtrack))
                    dclear = float(c.get("clearance", dclear))
        except (KeyError, ValueError, TypeError):
            pass
    return {"pads": pads, "parts": parts, "zones": zones, "segs": segs,
            "vias": vias, "copper": copper, "bbox": bbox,
            "diag": math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]),
            "track": track if track is not None else dtrack,
            "clearance": clearance if clearance is not None else dclear}


def unrouted_nets(B):
    """Nets with 2+ pads, no track/via copper and no zone of their own."""
    pads = defaultdict(list)
    for p in B["pads"]:
        if p["net"]:
            pads[p["net"]].append(p)
    cu = {s["net"] for s in B["segs"]} | {v["net"] for v in B["vias"]} | \
         {z["net"] for z in B["zones"] if z["net"] and not z["keepout"]}
    return sorted(n for n, ps in pads.items() if len(ps) >= 2 and n not in cu)


# ------------------------------------------------------------------ causes
def _pad_name(p):
    return f"{p['ref']}.{p['num']}"


def _gap(p, q):
    return max(0.0, abs(p["x"] - q["x"]) - p["ex"] - q["ex"]) if False else math.hypot(
        max(0.0, abs(p["x"] - q["x"]) - p["ex"] - q["ex"]),
        max(0.0, abs(p["y"] - q["y"]) - p["ey"] - q["ey"]))


def _neigh_gap(p, part):
    g = [_gap(p, q) for q in part["pads"] if q is not p and q["num"] != p["num"]]
    return min(g) if g else 99.0


def _sealed(B, net, ps):
    """A fine-pitch pad whose outward exit side(s) are closed: walled by a
    neighbour pad or another net's track/via within (track + clearance) of the
    side edge, with at least one copper blocker (routing closed the escape,
    not just the footprint)."""
    out = []
    lim = B["track"] + B["clearance"]
    for p in ps:
        part = B["parts"].get(p["ref"])
        if not part or part["n_pads"] <= 2 or _neigh_gap(p, part) >= FINE_GAP:
            continue
        copper, closed = {}, 0
        cx = sum(q["x"] for q in part["pads"]) / len(part["pads"])
        cy = sum(q["y"] for q in part["pads"]) / len(part["pads"])
        dx, dy = p["x"] - cx, p["y"] - cy
        # outward side: the pad's long axis pointing away from the part centre
        # (square pads: the dominant offset axis); the edge is sampled at both
        # ends and the middle
        horiz = (abs(dx) >= abs(dy)) if abs(p["ex"] - p["ey"]) < 0.25 * max(p["ex"], p["ey"]) \
            else (p["ex"] > p["ey"])
        if horiz:
            sg = 1 if dx >= 0 else -1
            ex_ = p["x"] + sg * p["ex"]
            sides = [(ex_, p["y"] + k * p["ey"]) for k in (-1, 0, 1)]
        else:
            sg = 1 if dy >= 0 else -1
            ey_ = p["y"] + sg * p["ey"]
            sides = [(p["x"] + k * p["ex"], ey_) for k in (-1, 0, 1)]
        hit = False
        for P in sides:
            for s in B["segs"]:
                if s["net"] == net or s["layer"] not in p["layers"]:
                    continue
                d = _seg_dist(P[0], P[1], *s["a"], *s["b"]) - s["w"] / 2
                if d < lim:
                    hit = True
                    if d < copper.get(s["net"], (9, ""))[0]:
                        copper[s["net"]] = d, "track"
            for v in B["vias"]:
                if v["net"] == net or not (v["layers"] & p["layers"] or not v["layers"]):
                    continue
                d = math.hypot(P[0] - v["x"], P[1] - v["y"]) - v["d"] / 2
                if d < lim:
                    hit = True
                    if d < copper.get(v["net"], (9, ""))[0]:
                        copper[v["net"]] = d, "via"
        closed, sides = int(hit), [0]
        also = set()                    # copper hugging the other three sides
        if hit:
            for P in ((p["x"] + p["ex"], p["y"]), (p["x"] - p["ex"], p["y"]),
                      (p["x"], p["y"] + p["ey"]), (p["x"], p["y"] - p["ey"])):
                for s in B["segs"]:
                    if (s["net"] != net and s["net"] not in copper and s["layer"] in p["layers"]
                            and _seg_dist(P[0], P[1], *s["a"], *s["b"]) - s["w"] / 2 < lim):
                        also.add(s["net"])
        if closed == len(sides) and copper:
            bl = sorted(copper.items(), key=lambda kv: kv[1][0])[:3]
            names = ", ".join(f"{n} {k[1]} {k[0]:.2f} mm" for n, k in bl)
            if also:
                names += f"; also on the other sides: {', '.join(sorted(also))}"
            out.append({"cause": "SEALED_PAD", "part": p["ref"], "pad": p["num"],
                        "blockers": [n for n, _ in bl] + sorted(also),
                        "evidence": f"{_pad_name(p)} (neighbour gap {_neigh_gap(p, part):.2f} mm) outward "
                                    f"side closed; blocking copper: {names} (limit {lim:.2f} mm)",
                        "suggestion": f"fan out every pin of {p['ref']} before routing power, or rip "
                                      f"{' / '.join([n for n, _ in bl] + sorted(also))} near {p['ref']}"})
    return out


def _owners(B):
    """{support ref: ownership} from the cluster model (cached on the board)."""
    if "_own" not in B:
        from . import clusters as CL
        B["_own"] = CL.ownership(B, cons=B.get("_cons"), spec=B.get("_spec"), min_pads=3)[0]
    return B["_own"]


def _far(B, net, ps):
    """A support part (passive/crystal) far from the IC that OWNS it: ownership is
    the cluster model's (unique-IC net, else reference-designator block); a rail
    cap whose owner is only a guess is not reported, and a cap is never measured
    against an arbitrary nearest IC."""
    own, out = _owners(B), []
    for p in ps:
        part = B["parts"].get(p["ref"])
        o = own.get(p["ref"])
        if not part or not o or o["net"] != net or o["how"] == "spread" or not PASSIVE.match(p["ref"]):
            continue
        other = [q for q in part["pads"] if q["num"] != p["num"]]
        gnd = p["ref"].startswith("C") and other and GROUND.match(other[0]["net"] or "")
        tight = gnd or any(k in net.upper() for k in TIGHT_NAMES)
        lim = FAR_TIGHT_MM if tight else FAR_MM
        ic = next(q for q in B["parts"][o["ic"]]["pads"] if q["num"] == o["pad"])
        d = math.hypot(ic["x"] - p["x"], ic["y"] - p["y"])
        if d > lim:
            out.append({"cause": "FAR_SUPPORT_PART", "part": p["ref"], "toward": _pad_name(ic),
                        "dist": round(d, 1), "limit": lim,
                        "evidence": f"{p['ref']} is {d:.1f} mm from {_pad_name(ic)} (limit {lim:g} mm)",
                        "suggestion": f"move {p['ref']} to within {lim:g} mm of {_pad_name(ic)} "
                                      f"(now {d:.1f} mm)"})
    return out


def _haul(B, net, ps):
    best = None
    for i in range(len(ps)):
        for j in range(i + 1, len(ps)):
            a, b = ps[i], ps[j]
            if a["ref"] == b["ref"]:
                continue
            d = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
            if best is None or d > best[0]:
                best = (d, a, b)
    if not best:
        return []
    d, a, b = best
    lim = min(HAUL_MM, HAUL_FRAC * B["diag"])
    if d <= lim:
        return []
    na, nb = B["parts"][a["ref"]]["n_pads"], B["parts"][b["ref"]]["n_pads"]
    mv, tg = (a, b) if na <= nb else (b, a)
    return [{"cause": "LONG_HAUL", "part": mv["ref"], "toward": _pad_name(tg), "dist": round(d, 1),
             "evidence": f"{_pad_name(a)} to {_pad_name(b)} = {d:.1f} mm (limit {lim:.1f} mm: "
                         f"30 mm or 35% of {B['diag']:.0f} mm diagonal)",
             "suggestion": f"co-locate: move {mv['ref']} ({B['parts'][mv['ref']]['n_pads']} pads) "
                           f"toward {tg['ref']}"}]


def _keepout(B, net, ps):
    out = []
    for p in ps:
        for z in B["zones"]:
            if (z["keepout"] and z["no_tracks"] and z["pts"] and p["layers"] <= z["layers"]
                    and _poly_has(z["pts"], p["x"], p["y"])):
                nm = f" '{z['name']}'" if z["name"] else ""
                out.append({"cause": "KEEPOUT_TRAPPED", "part": p["ref"], "pad": p["num"],
                            "evidence": f"{_pad_name(p)} lies inside track-forbidding rule area{nm} "
                                        f"on {'/'.join(sorted(p['layers']))}",
                            "suggestion": f"reshape the rule area{nm} off {_pad_name(p)} or move {p['ref']}"})
                break
    return out


def _subrule(B, net, ps):
    out = []
    for p in ps:
        part = B["parts"].get(p["ref"])
        narrow = p["min"] < B["track"] - 1e-9
        gap = _neigh_gap(p, part) if part else 99.0
        tight = False
        if part:
            tight = any(q is not p and q["net"] != p["net"] and _gap(p, q) < B["clearance"] - 1e-9
                        for q in part["pads"])
        if not (narrow or tight):
            continue
        w = max(MIN_NECK, math.floor(min(p["min"], B["track"]) * 200) / 200)
        why = (f"pad {p['min']:.2f} mm < track {B['track']:.2f}" if narrow
               else f"neighbour gap {gap:.2f} mm < clearance {B['clearance']:.2f}")
        out.append({"cause": "SUB_RULE_PAD", "part": p["ref"], "pad": p["num"],
                    "evidence": f"{_pad_name(p)}: {why}",
                    "suggestion": f"add a local neck-down rule for {_pad_name(p)}: track {w:.3f} mm "
                                  f"(floor {MIN_NECK} mm)"})
    return out


def _congestion(B, net, ps):
    x0, x1 = min(p["x"] for p in ps), max(p["x"] for p in ps)
    y0, y1 = min(p["y"] for p in ps), max(p["y"] for p in ps)
    x0 -= 1; y0 -= 1; x1 += 1; y1 += 1
    area = max((x1 - x0) * (y1 - y0), 1e-6)
    cu = 0.0
    for s in B["segs"]:
        mx, my = (s["a"][0] + s["b"][0]) / 2, (s["a"][1] + s["b"][1]) / 2
        if x0 <= mx <= x1 and y0 <= my <= y1:
            cu += math.hypot(s["b"][0] - s["a"][0], s["b"][1] - s["a"][1]) * s["w"]
    for v in B["vias"]:
        if x0 <= v["x"] <= x1 and y0 <= v["y"] <= y1:
            cu += math.pi * (v["d"] / 2) ** 2
    for p in B["pads"]:
        if x0 <= p["x"] <= x1 and y0 <= p["y"] <= y1:
            cu += p["w"] * p["h"] * (1 if p["tht"] is False else 1)
    frac = cu / (area * max(1, len(B["copper"])))
    return [{"cause": "CONGESTION", "fill": round(frac, 3),
             "evidence": f"copper fill {frac:.0%} of the net's bbox ({x1 - x0:.0f} x {y1 - y0:.0f} mm, "
                         f"{len(B['copper'])} layers)",
             "suggestion": "no single geometric cause found: free channel space around the net's "
                           "pads or give it a layer/priority"}]


def diagnose(B, failed, cons=None, spec=None):
    B["_cons"], B["_spec"] = cons, spec
    B.pop("_own", None)
    pads = defaultdict(list)
    for p in B["pads"]:
        if p["net"]:
            pads[p["net"]].append(p)
    nets, missing = [], []
    for n in failed:
        ps = pads.get(n)
        if not ps:
            missing.append(n)
            continue
        c = []
        for fn in (_sealed, _far, _haul, _keepout, _subrule):
            c += fn(B, n, ps)
        if not c:
            c = _congestion(B, n, ps)
        nets.append({"net": n, "pads": [_pad_name(p) for p in ps], "causes": c})
    return {"nets": nets, "missing": missing, "summary": summarize(nets, B)}


def summarize(nets, B):
    counts = {k: sum(1 for n in nets if any(c["cause"] == k for c in n["causes"])) for k in CAUSES}
    mv = {}
    for n in nets:
        for c in n["causes"]:
            if c["cause"] not in ("FAR_SUPPORT_PART", "LONG_HAUL"):
                continue
            m = mv.setdefault(c["part"], {"part": c["part"], "toward": [], "nets": [], "dist": 0.0})
            if c["toward"] not in m["toward"]:
                m["toward"].append(c["toward"])
            if n["net"] not in m["nets"]:
                m["nets"].append(n["net"])
            m["dist"] = max(m["dist"], c["dist"])
    for m in mv.values():
        pos = B["parts"][m["part"]]["pos"]
        m["from"] = [round(pos[0], 2), round(pos[1], 2)]
    moves = sorted(mv.values(), key=lambda m: (-len(m["nets"]), -m["dist"], m["part"]))
    return {"failed": len(nets), "counts": counts, "moves": moves}


def format_text(res):
    L = []
    for n in res["nets"]:
        L.append(f"{n['net']}  ({len(n['pads'])} pads: {', '.join(n['pads'][:6])}"
                 f"{', ...' if len(n['pads']) > 6 else ''})")
        for c in n["causes"]:
            L.append(f"  {c['cause']}: {c['evidence']}")
            L.append(f"      -> {c['suggestion']}")
    for m in res["missing"]:
        L.append(f"{m}: not on the board (or no pads) - skipped")
    s = res["summary"]
    L.append("")
    L.append(f"SUMMARY: {s['failed']} failed net(s) diagnosed")
    for k in CAUSES:
        L.append(f"  {k:18s} {s['counts'][k]}")
    L.append("MOVE LIST (ranked by nets fixed):")
    if not s["moves"]:
        L.append("  (no placement moves suggested)")
    for i, m in enumerate(s["moves"], 1):
        L.append(f"  {i}. {m['part']} from ({m['from'][0]}, {m['from'][1]}) toward "
                 f"{', '.join(m['toward'])}  fixes {len(m['nets'])}: {', '.join(m['nets'])}")
    return "\n".join(L)
