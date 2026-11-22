"""Board snapshots for the dashboard (contract: docs/EVENTS-CONTRACT.md, section Snapshot / Delta).

Full snapshots are written whole; deltas carry only tracks/vias added or removed by id and the changed parts.
Item ids are the first 12 hex chars of sha1 over geometry + net, so they are stable across snapshots.
Snapper never raises: a snapshot failure is swallowed so a stage is never failed or slowed by the dashboard.
"""
import hashlib
import json
import os
import time

from . import events
from . import routediag as RD

FULL_EVERY = 20
SNAP_S = 3.0
EDGE_MAX = 200  # zone outline points kept (contract)


def _id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _r(v, n=3):
    return round(float(v), n)


def _pt(p):
    return [_r(p[0]), _r(p[1])]


def _simplify(pts, cap=EDGE_MAX):
    if len(pts) <= cap:
        return pts
    step = len(pts) / float(cap)
    out = [pts[int(i * step)] for i in range(cap)]
    return out


def _build(B, seq, board_path=""):
    """Full-snapshot dict from a routediag.load() result. Pure; no I/O."""
    net_names = sorted({s["net"] for s in B["segs"] if s["net"]} |
                       {v["net"] for v in B["vias"] if v["net"]} |
                       {p["net"] for p in B["pads"] if p["net"]} |
                       {z["net"] for z in B["zones"] if z["net"]})
    nid = {nm: str(i) for i, nm in enumerate(net_names)}
    nets = {v: k for k, v in nid.items()}

    tracks = []
    for s in B["segs"]:
        a, b, m = _pt(s["a"]), _pt(s["b"]), (_pt(s["mid"]) if s.get("mid") else None)
        n = nid.get(s["net"], "")
        l = s["layer"]
        tracks.append({"id": _id(l, n, a, b, _r(s["w"]), m), "l": l, "n": n, "w": _r(s["w"]),
                       "a": a, "b": b, "m": m, "lk": bool(s.get("locked"))})
    vias = []
    for v in B["vias"]:
        n = nid.get(v["net"], "")
        x, y = _r(v["x"]), _r(v["y"])
        vias.append({"id": _id("via", x, y, _r(v["d"]), n), "x": x, "y": y, "d": _r(v["d"]), "n": n,
                     "lk": bool(v.get("locked"))})
    parts, pads = [], []
    for ref, pr in B["parts"].items():
        cx0, cy0, cx1, cy1 = pr["court"]
        parts.append({"ref": ref, "x": _r(pr["pos"][0]), "y": _r(pr["pos"][1]), "rot": _r(pr["angle"], 2),
                      "side": pr["side"], "crt": [_r(cx0), _r(cy0), _r(cx1), _r(cy1)],
                      "value": pr.get("value", "")})
    for p in B["pads"]:
        pads.append({"ref": p["ref"], "num": p["num"], "x": _r(p["x"]), "y": _r(p["y"]),
                     "w": _r(p["w"]), "h": _r(p["h"]), "rot": _r(p["angle"], 2), "shape": p["shape"],
                     "layers": sorted(p["layers"]), "net": nid.get(p["net"], "")})
    zones = []
    for z in B["zones"]:
        if z.get("keepout"):
            continue
        lay = sorted(z["layers"])
        zones.append({"l": lay[0] if lay else "", "n": nid.get(z["net"], ""),
                      "pts": [_pt(q) for q in _simplify(z["pts"])]})
    bx0, by0, bx1, by1 = B["bbox"]
    return {"v": 1, "seq": seq, "ts": round(time.time(), 3), "board": board_path,
            "bbox": [_r(bx0), _r(by0), _r(bx1), _r(by1)], "layers": list(B["copper"]),
            "edge": [_pt(e[0]) for e in B["edge"]], "nets": nets, "parts": parts, "pads": pads,
            "tracks": tracks, "vias": vias, "zones": zones, "opens": None}


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)


def full(board_path, out_path, seq, B=None):
    """Write a full snapshot of board_path to out_path. Returns the dict written."""
    B = B if B is not None else RD.load(board_path)
    obj = _build(B, seq, os.path.basename(board_path))
    _write(out_path, obj)
    return obj


def _delta_of(prev, cur):
    """Delta dict from full dict `prev` to full dict `cur`."""
    pt = {t["id"]: t for t in prev["tracks"]}
    ct = {t["id"]: t for t in cur["tracks"]}
    pv = {v["id"]: v for v in prev["vias"]}
    cv = {v["id"]: v for v in cur["vias"]}
    pp = {p["ref"]: p for p in prev["parts"]}
    changed = [p for p in cur["parts"] if pp.get(p["ref"]) != p]
    return {"v": 1, "seq": cur["seq"], "base": prev["seq"],
            "add": {"tracks": [ct[i] for i in ct if i not in pt], "vias": [cv[i] for i in cv if i not in pv]},
            "del": {"tracks": [i for i in pt if i not in ct], "vias": [i for i in pv if i not in cv]},
            "parts": changed, "opens": cur.get("opens")}


def delta(prev_full, board_path, out_path, seq, cur=None):
    """Delta of board_path against prev_full (a full dict). `cur` is the already-built full dict if the
    caller has one, so the board is loaded once."""
    if cur is None:
        cur = _build(RD.load(board_path), seq, os.path.basename(board_path))
    cur = dict(cur, seq=seq)
    obj = _delta_of(prev_full, cur)
    _write(out_path, obj)
    return obj


class Snapper:
    """Keeps the last full snapshot in memory; writes deltas, periodic and stage-end fulls under work/snap."""

    def __init__(self, work, snap_s=SNAP_S, full_every=FULL_EVERY):
        self.work = os.fspath(work)
        self.dir = os.path.join(self.work, "snap")
        self.snap_s = snap_s
        self.full_every = full_every
        self.seq = 0
        self.prev = None          # last full dict (in memory)
        self.since_full = 0       # deltas since last full
        self.last_t = 0.0

    def take(self, board_path, reason, force=False, opens=None):
        """Write a snapshot of board_path. Returns the relative path written, or None (throttled / failed)."""
        try:
            now = time.monotonic()
            if not force and now - self.last_t < self.snap_s:
                return None
            self.last_t = now
            os.makedirs(self.dir, exist_ok=True)
            self.seq += 1
            seq = self.seq
            B = RD.load(board_path)
            cur = _build(B, seq, os.path.basename(board_path))
            cur["opens"] = opens
            want_full = (force or self.prev is None or reason == "stage_end"
                         or self.since_full >= self.full_every)
            if want_full:
                rel = os.path.join("snap", "%06d.json" % seq)
                _write(os.path.join(self.work, rel), cur)
                _write(os.path.join(self.dir, "latest.json"), cur)
                self.prev, self.since_full = cur, 0
                is_full = True
            else:
                rel = os.path.join("snap", "%06d.delta.json" % seq)
                _write(os.path.join(self.work, rel), _delta_of(self.prev, cur))
                self.since_full += 1
                is_full = False
            events.emit("snapshot", path=rel.replace(os.sep, "/"), full=is_full, reason=reason, opens=opens)
            return rel.replace(os.sep, "/")
        except Exception:
            return None
