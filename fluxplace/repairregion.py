"""repair-region: rip the open connections of a board and their UNLOCKED blockers in one area, re-route them
together with Freerouting (region.run, keep_locked=True), and keep the result only if the board got strictly
better: fewer open connections, no new shorts, no new DRC violation kind.

Targets: the nets of the unconnected items of a kicad-cli DRC (zone islands excluded). RF and pair nets are never
ripped (reported as skipped). Area: bounding box of the open items' positions, grown by margin mm; each failed try
grows the margin by 1.5x. Blockers: other nets with an unlocked track or via fully inside the area, minus RF, pair,
GND and power-plane nets. A failed run never writes out_path; its sealing list names what blocks the area.
"""
import os
import re
import shutil

from . import region as RG, routediag as RD, drcguard as DG
from .krtchain import pair_nets
from .review import is_rf

__all__ = ["repair"]

_NET = re.compile(r"\[([^\]]*)\]")
_SIDE = (".kicad_pro", ".kicad_dru")


def _island(u):
    return any(DG._ZONE_DESC.match(str(i.get("description", ""))) for i in u.get("items") or [])


def _open_items(d):
    return [u for u in d.get("unconnected_items", []) if not _island(u)]


def _shorts(d):
    return sum(1 for v in d.get("violations", []) if v.get("type") == "shorting_items")


def _targets(d, log):
    """{net: [(x, y) mm, ...]} of the open items (one entry per open item)."""
    out = {}
    for u in _open_items(d):
        items = u.get("items") or []
        net = next((m.group(1) for i in items for m in [_NET.search(str(i.get("description", "")))]
                    if m and m.group(1)), None)
        pos = [(float(i["pos"]["x"]), float(i["pos"]["y"])) for i in items if i.get("pos")]
        if not net:
            log(f"    repair-region: open item without a net skipped: {str(items[:1])[:120]}")
            continue
        out.setdefault(net, []).extend(pos)
    return out


def _box(points, margin):
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    m = float(margin)
    return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)


def _in(area, x, y):
    return RG._inside(area, x, y)


def _blockers(B, area, protected):
    """Nets with an UNLOCKED track or via fully inside the area, minus the protected ones."""
    out = set()
    for s in B["segs"]:
        if not s["locked"] and s["net"] and _in(area, *s["a"]) and _in(area, *s["b"]):
            out.add(s["net"])
    for v in B["vias"]:
        if not v["locked"] and v["net"] and _in(area, v["x"], v["y"]):
            out.add(v["net"])
    return {n for n in out if n not in protected}


def _sealing(B, area, targets, ripped, limit=20):
    """What blocks the area: locked tracks and vias of other nets first, then pads of other nets."""
    out = []
    for s in B["segs"]:
        if s["locked"] and s["net"] not in targets and _in(area, *s["a"]) and _in(area, *s["b"]):
            out.append(f"track {s['net'] or '-'} {s['layer']} locked")
    for v in B["vias"]:
        if v["locked"] and v["net"] not in targets and _in(area, v["x"], v["y"]):
            out.append(f"via {v['net'] or '-'} locked")
    for p in B["pads"]:
        if p["net"] and p["net"] not in targets and p["net"] not in ripped and _in(area, p["x"], p["y"]):
            out.append(f"{p['ref']}:{p['num']} {p['net']}")
    return out[:limit]


def _drc(path, kicad_cli):
    return DG.drc_json(path, kicad_cli)


def _sidecars(src, dst):
    for ext in _SIDE:
        side = os.path.splitext(src)[0] + ext
        if os.path.exists(side):
            shutil.copy(side, os.path.splitext(dst)[0] + ext)


def repair(board_path, out_path, *, nets=None, margin=2.0, tries=3, constraints=None,
           planes=("In1.Cu", "In4.Cu"), jar=None, passes=3, timeout=900, kicad_cli="kicad-cli", log=print):
    """-> {"status": "kept", opens_before, opens_after, area, ripped_nets, tries}
          {"status": "failed", opens_before, area, tries, sealing}  (out_path not written)
          {"status": "nothing", skipped}  (no open connection the tool may repair)."""
    from . import constraints as CF
    cons = CF.load(constraints) if (constraints is None or isinstance(constraints, str)) else (constraints or {})
    rf_names = set((cons.get("rf") or {}).get("nets") or [])
    B0 = RD.load(board_path)
    names = {p["net"] for p in B0["pads"] if p["net"]} | {s["net"] for s in B0["segs"] if s["net"]} | \
            {v["net"] for v in B0["vias"] if v["net"]}
    pairs = set(pair_nets(cons, sorted(names), None))
    planes_nets = {z["net"] for z in B0["zones"] if z["net"]}

    d0 = _drc(board_path, kicad_cli)
    keys0 = DG.keys_from_report(d0)[0]
    opens0 = len(_open_items(d0))
    shorts0 = _shorts(d0)
    targets = _targets(d0, log)
    if nets:                                  # a token that is a net name is literal; otherwise a regex
        keep = set()
        for tok in nets:
            if tok in targets:
                keep.add(tok)
            else:
                try:
                    rx = re.compile(tok)
                except re.error as e:
                    raise SystemExit(f"--nets {tok!r}: not a net name or regex ({e})")
                keep |= {n for n in targets if rx.fullmatch(n)}
        targets = {n: p for n, p in targets.items() if n in keep}
    if not targets:
        log("repair-region: no open connection to repair")
        return {"status": "nothing", "skipped": {}}
    skipped = {}
    for n in sorted(targets):
        if n in rf_names or is_rf(n):
            skipped[n] = "rf"
        elif n in pairs:
            skipped[n] = "pair"
    for n in skipped:
        del targets[n]
    if skipped:
        log(f"    skipped: {skipped} (rf/pair: use pairs --only)")
    if not targets:
        return {"status": "nothing", "skipped": skipped}
    protected = rf_names | pairs | planes_nets | {n for n in names if is_rf(n) or "GND" in n.upper()}
    pts = [p for ps in targets.values() for p in ps]
    log_path = os.path.splitext(out_path)[0] + ".repair-region.log"
    tmp = out_path + ".repair.kicad_pcb"
    m = float(margin)
    area, ripped, verdict = None, set(), ""
    for t in range(1, int(tries) + 1):
        area = _box(pts, m)
        ripped = set(targets) | _blockers(B0, area, protected)
        scope = {"nets": ripped, "refs": [], "area": area, "label": f"repair-region {sorted(targets)}"}
        res = RG.run(board_path, tmp, scope, planes=planes, jar=jar, passes=passes, timeout=timeout,
                     rip=True, keep_locked=True, log=log)
        opens1 = shorts1 = None
        if not res.get("ok"):
            verdict = "protection FAILED: " + "; ".join(res.get("violations", [])[:2])
        else:
            d1 = _drc(tmp, kicad_cli)
            keys1 = DG.keys_from_report(d1)[0]
            opens1, shorts1 = len(_open_items(d1)), _shorts(d1)
            new = [k for k in DG.new_keys(keys0, keys1) if not k[0].startswith("U")]
            if opens1 >= opens0:
                verdict = "rejected: opens not reduced"
            elif shorts1 > shorts0:
                verdict = "rejected: shorts rose"
            elif new:
                verdict = f"rejected: {len(new)} new violation(s)"
            else:
                verdict = "kept"
        ar = [round(v, 3) for v in area]
        with open(log_path, "a") as fh:
            fh.write(f"try {t} margin {m:g} area {ar} ripped {sorted(ripped)} opens {opens0}->"
                     f"{opens1 if opens1 is not None else '-'} : {verdict}\n")
        log(f"    repair-region try {t}: area {ar}, {len(ripped)} net(s) ripped, opens {opens0}->"
            f"{opens1 if opens1 is not None else '-'}: {verdict}")
        if verdict == "kept":
            shutil.move(tmp, out_path)
            _sidecars(board_path, out_path)
            return {"status": "kept", "opens_before": opens0, "opens_after": opens1, "area": ar,
                    "ripped_nets": sorted(ripped), "tries": t}
        if os.path.exists(tmp):
            os.unlink(tmp)
        m *= 1.5
    return {"status": "failed", "opens_before": opens0, "area": [round(v, 3) for v in area], "tries": int(tries),
            "ripped_nets": sorted(ripped), "sealing": _sealing(B0, area, targets, ripped)}
