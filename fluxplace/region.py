"""Region-scoped finishing: route only the connections a scope names, and PROVE the rest of the copper unchanged.

Scope = intersection of: nets (--nets NAME|REGEX), parts (--refs GLOB, nets touching those parts),
area (--area X1,Y1,X2,Y2 or --area-of REF with --margin, mm board coordinates) and a selection file
({"nets": [...], "refs": [...], "area": [x1,y1,x2,y2]}).

Routing: Freerouting gets the whole board as wiring (existing copper is an obstacle). With an area, the
pins of in-scope nets outside the area are removed from the DSN so only in-area connections are asked
for; a net whose new copper leaves the area is dropped (reported). Only the NEW copper of in-scope nets
is copied back; the rest of the board object is untouched.

Protection: every track / via / zone outline of the input board is fingerprinted (net, layer, geometry;
coordinates rounded to 0.1 um). After the run, anything that disappeared must have been ripped on
purpose (--rip, inside the area, in-scope nets), and anything that appeared must be new copper of an
in-scope net inside the area. Any other difference is a FAIL: the output goes to
<out>.rejected.kicad_pcb and the diff is printed. The check reads both files; it does not trust the router.
"""
import fnmatch
import hashlib
import json
import os
import re
import subprocess
from collections import Counter, defaultdict

__all__ = ["parse_area", "parse_selection", "scope_from_args", "fingerprint", "check", "run"]

_NR = 4  # rounding for canonical keys (0.1 um)


def parse_area(text):
    """'X1,Y1,X2,Y2' (mm) -> (x0, y0, x1, y1) normalised."""
    try:
        v = [float(x) for x in re.split(r"[,\s]+", str(text).strip()) if x]
    except ValueError:
        raise SystemExit(f"--area: not four numbers: {text!r}")
    if len(v) != 4:
        raise SystemExit(f"--area: need X1,Y1,X2,Y2 (mm), got {text!r}")
    return (min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3]))


def parse_selection(path):
    """Selection file -> {"nets": [..], "refs": [..], "area": (x0,y0,x1,y1) or None}."""
    with open(path) as fh:
        d = json.load(fh)
    unknown = set(d) - {"nets", "refs", "area"}
    if unknown:
        raise SystemExit(f"--selection {path}: unknown keys {sorted(unknown)}")
    area = d.get("area")
    if area is not None:
        area = parse_area(",".join(str(x) for x in area))
    return {"nets": d.get("nets") or None, "refs": d.get("refs") or None, "area": area}


def _inside(area, x, y):
    return area is None or (area[0] - 1e-9 <= x <= area[2] + 1e-9 and area[1] - 1e-9 <= y <= area[3] + 1e-9)


def _pad_xy(p):
    return p["x"], p["y"]


def _pin_id(s):
    """DSN pin 'REF-NUM' or 'REF-NUM@k' -> (REF, NUM)."""
    s = s.split("@", 1)[0]
    ref, _, num = s.rpartition("-")
    return ref, num


def area_of(B, refs, margin):
    """Bounding box (courtyards, else pads) of the named parts, grown by margin mm."""
    pats = refs
    xs0, ys0, xs1, ys1 = [], [], [], []
    for ref, p in B["parts"].items():
        if not any(fnmatch.fnmatchcase(ref, pt) for pt in pats):
            continue
        c = p.get("court")
        if c:
            x0, y0, x1, y1 = c
        else:
            ps = p.get("pads") or []
            if not ps:
                continue
            x0 = min(q["x"] - q["w"] / 2 for q in ps)
            x1 = max(q["x"] + q["w"] / 2 for q in ps)
            y0 = min(q["y"] - q["h"] / 2 for q in ps)
            y1 = max(q["y"] + q["h"] / 2 for q in ps)
        xs0.append(x0), ys0.append(y0), xs1.append(x1), ys1.append(y1)
    if not xs0:
        raise SystemExit(f"--area-of: no part matches {','.join(refs)}")
    m = float(margin)
    return (min(xs0) - m, min(ys0) - m, max(xs1) + m, max(ys1) + m)


def scope_from_args(B, nets_args=None, refs=None, area=None, area_of=None, margin=2.0, selection=None):
    """-> {"nets": set|None (None = every net), "refs": [..], "area": tuple|None, "label": str}.
    Every constraint given narrows the scope (intersection); none at all is an error."""
    all_nets = {p["net"] for p in B["pads"] if p["net"]} | {s["net"] for s in B["segs"]} | \
               {v["net"] for v in B["vias"]}
    sel = parse_selection(selection) if selection else {"nets": None, "refs": None, "area": None}
    nets_args = list(nets_args or []) + list(sel["nets"] or [])
    refs = list(refs or []) + list(sel["refs"] or [])
    areas = []
    if area:
        areas.append(parse_area(area) if isinstance(area, str) else tuple(area))
    if area_of:
        areas.append(area_of_from(B, area_of, margin))
    if sel["area"]:
        areas.append(sel["area"])
    box = None
    for a in areas:
        box = a if box is None else (max(box[0], a[0]), max(box[1], a[1]), min(box[2], a[2]), min(box[3], a[3]))
        if box[0] > box[2] or box[1] > box[3]:
            raise SystemExit("finish: the areas given do not overlap")
    scope_nets = None
    if nets_args:
        pick = set()
        for tok in nets_args:
            if tok in all_nets:
                pick.add(tok)
                continue
            try:
                rx = re.compile(tok)
            except re.error as e:
                raise SystemExit(f"--nets {tok!r}: not a net name or regex ({e})")
            pick |= {n for n in all_nets if rx.fullmatch(n)}
        scope_nets = pick
    if refs:
        touch = {p["net"] for p in B["pads"]
                 if p["net"] and any(fnmatch.fnmatchcase(p["ref"], g) for g in refs)}
        scope_nets = touch if scope_nets is None else scope_nets & touch
    if scope_nets is None and box is None:
        raise SystemExit("finish: give --nets, --refs, --area / --area-of or --selection")
    names = sorted(scope_nets) if scope_nets is not None else []
    label = "; ".join(x for x in (
        (f"nets {names[:6]}{' ...' if len(names) > 6 else ''} ({len(names)})" if scope_nets is not None else ""),
        (f"refs {refs}" if refs else ""),
        (f"area {tuple(round(v, 3) for v in box)}" if box else "")) if x)
    return {"nets": scope_nets, "refs": refs, "area": box, "label": label}


def area_of_from(B, refs, margin):
    return area_of(B, refs.split(",") if isinstance(refs, str) else list(refs), margin)


# ---------------------------------------------------------------- fingerprints
def _r(v):
    return round(float(v), _NR) + 0.0


def fingerprint(path):
    """-> list of items {kind, key, net, layer, pts, w, d, drill}. key = canonical tuple for multiset compare."""
    from . import routediag as RD
    B = RD.load(path)
    items = []
    for s in B["segs"]:
        a = (_r(s["a"][0]), _r(s["a"][1]))
        b = (_r(s["b"][0]), _r(s["b"][1]))
        ends = tuple(sorted([a, b]))
        key = ("S", s["net"] or "", s["layer"], ends, _r(s["w"]))
        items.append({"kind": "seg", "key": key, "net": s["net"] or "", "layer": s["layer"],
                      "pts": [a, b], "w": s["w"]})
    for v in B["vias"]:
        key = ("V", v["net"] or "", (_r(v["x"]), _r(v["y"])), _r(v["d"]), _r(v["drill"]))
        items.append({"kind": "via", "key": key, "net": v["net"] or "", "layer": "",
                      "pts": [(v["x"], v["y"])], "d": v["d"], "drill": v["drill"]})
    for z in B["zones"]:
        if z["keepout"] or len(z["pts"]) < 3:
            continue
        pts = tuple(sorted((_r(x), _r(y)) for x, y in z["pts"]))
        key = ("Z", z["net"] or "", tuple(sorted(z["layers"])), pts)
        items.append({"kind": "zone", "key": key, "net": z["net"] or "", "layer": ",".join(sorted(z["layers"])),
                      "pts": [(x, y) for x, y in z["pts"]]})
    return items, B


def _digest(keys):
    h = hashlib.sha256()
    for k in sorted(repr(k) for k in keys):
        h.update(k.encode())
        h.update(b"\n")
    return h.hexdigest()[:16]


def check(before_path, after_path, scope, ripped=()):
    """-> {ok, hash_before, hash_after, removed, added, violations}.
    ripped: Counter of item keys deliberately removed (--rip). Added items must be copper of an in-scope
    net inside the area. Everything else of the input must still be there, unchanged."""
    bi, _ = fingerprint(before_path)
    ai, _ = fingerprint(after_path)
    cb = Counter(i["key"] for i in bi)
    ca = Counter(i["key"] for i in ai)
    rip = Counter(ripped)
    removed = cb - ca
    added = ca - cb
    viol = []
    for k, n in (removed - rip).items():
        viol.append(f"protected copper changed/removed x{n}: {_show(k)}")
    for k, n in added.items():
        item = next(i for i in ai if i["key"] == k)
        if item["kind"] == "zone":
            viol.append(f"zone outline added x{n}: {_show(k)}")
            continue
        if scope["nets"] is not None and item["net"] not in scope["nets"]:
            viol.append(f"copper added to an out-of-scope net {item['net']!r} x{n}")
        elif not all(_inside(scope["area"], x, y) for x, y in item["pts"]):
            viol.append(f"copper added outside the area on {item['net']!r} x{n}: {_show(k)}")
    hb = _digest((cb - rip).elements())
    ha = _digest((ca - added).elements())
    ok = not viol and hb == ha
    if hb != ha and not viol:
        viol.append("protected copper hash differs")
    return {"ok": ok, "hash_before": hb, "hash_after": ha, "removed": removed, "added": added,
            "violations": viol}


def _show(k):
    return " ".join(str(x) for x in k)


# ----------------------------------------------------------- DSN pin scope
_NET_RX = re.compile(r'\(net\s+("[^"]*"|\S+)\s*\(pins([^)]*)\)', re.S)


def filter_dsn(dsn_path, out_path, scope, pad_pos):
    """Keep only the pins of in-scope nets that lie in the area (all pins when no area).
    pad_pos: {(ref, num): [(x, y), ...]}. Out-of-scope nets are left as they are: their copper is
    discarded after routing, so their pins are never copied back."""
    text = open(dsn_path, errors="replace").read()
    area = scope["area"]
    nets = scope["nets"]
    dropped = [0]

    def sub(m):
        name = m.group(1).strip('"')
        if area is None or (nets is not None and name not in nets):
            return m.group(0)
        keep = []
        for pin in m.group(2).split():
            ps = pad_pos.get(_pin_id(pin))
            if ps is None or any(_inside(area, x, y) for x, y in ps):
                keep.append(pin)
            else:
                dropped[0] += 1
        head = m.group(0)[: m.start(2) - m.start(0)]
        return head + " " + " ".join(keep) + ")"

    new = _NET_RX.sub(sub, text)
    with open(out_path, "w") as fh:
        fh.write(new)
    return dropped[0]


def _pad_positions(B):
    out = defaultdict(list)
    for p in B["pads"]:
        out[(p["ref"], p["num"])].append((p["x"], p["y"]))
    return out


# ------------------------------------------------------------- routing run
def run(board_path, out_path, scope, *, planes=("In1.Cu", "In4.Cu"), jar=None, passes=3,
        timeout=900, rip=False, log=print, tamper=None):
    """Route the scope with Freerouting, copy back the new in-scope copper only, prove protection.
    -> the proof fields of check() (ok, violations, hash_before, hash_after, removed, added) plus
       added_tracks, added_vias, added_mm, nets_kept, nets_dropped, in_scope_before/after (open clusters per
       net), open_before/after, diagnose (routediag.diagnose of what is still open in scope), out.
    The output board is written only when the proof passes."""
    import pcbnew
    from . import kicad_io as IO, ses as SES, finish as FN, routeconn as RC
    from . import repair as RP, routestatus as RS
    jar = FN._find_jar(jar)
    if not jar:
        raise SystemExit("finish: no freerouting jar")
    work = out_path + ".region"
    base_file, dsn, dsn_f, ses = work + ".base.kicad_pcb", work + ".dsn", work + ".f.dsn", work + ".ses"
    scratch_file = work + ".scratch.kicad_pcb"

    # 1. baseline: the input, optionally with the in-scope copper inside the area ripped out
    board = pcbnew.LoadBoard(board_path)
    before_items, B0 = fingerprint(board_path)
    if rip:
        area = scope["area"]
        for t in list(board.GetTracks()):
            n = t.GetNetname()
            if scope["nets"] is not None and n not in scope["nets"]:
                continue
            if t.GetClass() == "PCB_VIA":
                pts = [(t.GetPosition().x / 1e6, t.GetPosition().y / 1e6)]
            else:
                pts = [(t.GetStart().x / 1e6, t.GetStart().y / 1e6), (t.GetEnd().x / 1e6, t.GetEnd().y / 1e6)]
            if all(_inside(area, x, y) for x, y in pts):
                RP._remove(board, t)
    pcbnew.SaveBoard(base_file, board)
    base_items, Bb = fingerprint(base_file)
    ripped = Counter(i["key"] for i in before_items) - Counter(i["key"] for i in base_items)
    base_keys = Counter(i["key"] for i in base_items)

    groups = {}
    open_before = RC.text_open(Bb, groups=groups)
    in_before = _in_scope_open(groups, scope)

    # 2. DSN: whole board as wiring, in-scope pins only (inside the area when given)
    if not IO.export_dsn(board, dsn):
        raise SystemExit("finish: DSN export failed")
    SES.declare_power_planes(dsn, list(planes))
    dropped = filter_dsn(dsn, dsn_f, scope, _pad_positions(Bb))
    log(f"    region: {dropped} out-of-area pins removed from the DSN")
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    try:
        r = RS.run_logged(FN.freerouting_cmd(jar, dsn_f, ses, passes), timeout=timeout, env=env,
                          out=ses, stage="finish")
        if r.returncode != 0:
            log(f"    finish: freerouting exited {r.returncode}")
    except subprocess.TimeoutExpired:
        log(f"    finish: freerouting hit the {timeout}s cap")
    if not os.path.exists(ses):
        raise SystemExit("finish: no session produced")

    # 3. import into a scratch board; new copper = scratch items on in-scope nets that the base lacks
    scratch = pcbnew.LoadBoard(base_file)
    # pcbnew's importer, as finish.route_nets uses it: a session is the board's complete routing
    if not pcbnew.ImportSpecctraSES(scratch, ses):
        raise SystemExit("finish: SES import rejected")
    pcbnew.SaveBoard(scratch_file, scratch)
    sc_items, _ = fingerprint(scratch_file)
    new_items = []
    cnt = Counter(base_keys)
    for it in sc_items:
        if cnt[it["key"]] > 0:
            cnt[it["key"]] -= 1
            continue
        new_items.append(it)
    kept, dropped_nets = [], defaultdict(int)
    by_net = defaultdict(list)
    for it in new_items:
        by_net[it["net"]].append(it)
    for n, its in by_net.items():
        if scope["nets"] is not None and n not in scope["nets"]:
            continue
        if all(all(_inside(scope["area"], x, y) for x, y in it["pts"]) for it in its):
            kept += its
        else:
            dropped_nets[n] += len(its)
    # 4. build the output: base + kept copper
    out = pcbnew.LoadBoard(base_file)
    nt = nv = 0
    mm = 0.0
    for it in kept:
        ni = out.FindNet(it["net"])
        if it["kind"] == "via":
            v = pcbnew.PCB_VIA(out)
            v.SetPosition(pcbnew.VECTOR2I(int(round(it["pts"][0][0] * 1e6)), int(round(it["pts"][0][1] * 1e6))))
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetWidth(int(round(it["d"] * 1e6)))
            v.SetDrill(int(round(it["drill"] * 1e6)))
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            v.SetNet(ni)
            out.Add(v)
            nv += 1
        else:
            s = pcbnew.PCB_TRACK(out)
            (ax, ay), (bx, by) = it["pts"]
            s.SetStart(pcbnew.VECTOR2I(int(round(ax * 1e6)), int(round(ay * 1e6))))
            s.SetEnd(pcbnew.VECTOR2I(int(round(bx * 1e6)), int(round(by * 1e6))))
            s.SetLayer(out.GetLayerID(it["layer"]))
            s.SetWidth(int(round(it["w"] * 1e6)))
            s.SetNet(ni)
            out.Add(s)
            nt += 1
            mm += ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
    pcbnew.SaveBoard(out_path, out)
    if tamper:                       # test seam: corrupt the output before the proof runs
        tamper(out_path)

    # 5. the proof, on the files
    res = check(board_path, out_path, scope, ripped)
    _, Bo = fingerprint(out_path)
    groups_a = {}
    open_after = RC.text_open(Bo, groups=groups_a)
    in_after = _in_scope_open(groups_a, scope)
    res.update({"added_tracks": nt, "added_vias": nv, "added_mm": round(mm, 3),
                "nets_kept": sorted({i["net"] for i in kept}), "nets_dropped": dict(dropped_nets),
                "in_scope_before": in_before, "in_scope_after": in_after, "open_before": sum(open_before.values()),
                "open_after": sum(open_after.values()), "out": out_path, "dsn_pins_dropped": dropped,
                "diagnose": None})
    left = sorted({n for n in in_after})
    if left:
        from . import routediag as _RD
        res["diagnose"] = _RD.diagnose(Bo, left)
    for f in (base_file, dsn, dsn_f, ses, scratch_file):
        try:
            os.unlink(f)
        except OSError:
            pass
    return res


def _in_scope_open(groups, scope):
    """{net: count} of open clusters that the scope asks for: the net is in scope and, with an area,
    every pad of the cluster lies inside it."""
    out = {}
    for net, clusters in groups.items():
        if scope["nets"] is not None and net not in scope["nets"]:
            continue
        k = sum(1 for c in clusters if all(_inside(scope["area"], p["x"], p["y"]) for p in c))
        if k:
            out[net] = k
    return out
