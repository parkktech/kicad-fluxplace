"""Escalation ladder of the route stage: a flat round is not the end.

When the rounds make no more progress and nets are still open, climb until the ladder is exhausted or the time budget is gone:

  ripup   rip-up-and-retry: the nets that block a stuck net (route-diagnose `blockers`) lose their UNLOCKED copper, the stuck net
          is routed first, the ripped nets after it.  Honours `[route] allow_rip` (never: skipped; low-speed: controlled nets,
          fan-out copper and plain copper are never ripped; any: every net).
  order   net order: every open net routed one after the other on the growing result, shortest first, then most-constrained first
          (sealed pads, many pads); a net that does not lower the open count is discarded.
  layer   another layer: a stackup signal layer the router was not given (never a plane layer) for the stuck net class.

Each rung records what it closed (`open_before` -> `open_after`, nets).  Nothing here touches the router itself: it needs
`run(src, dst, nets, timeout, limited)` (batchroute.make_runner), `conn(path)`, and `keep(path)` (keep-best, returns the best open
count)."""
import os
import shutil
import time

from . import boardtext as BT
from . import netclass as NC
from . import ripolicy as RIP
from . import routediag as RD

RUNGS = ("ripup", "order", "layer")


def _strip_unlocked(src, dst, nets):
    """copy src -> dst without the UNLOCKED copper of `nets`; -> items removed."""
    text = open(src, errors="surrogateescape").read()
    nets = set(nets)
    keys = [k for _i, _j, k, blk in BT.scan(text) if BT.net_name(blk) in nets and not BT.is_locked(blk)]
    text = BT.remove_blocks(text, set(keys))
    with open(dst, "w", errors="surrogateescape") as fh:
        fh.write(text)
    side = os.path.splitext(src)[0] + ".kicad_pro"
    if os.path.exists(side):
        shutil.copy(side, os.path.splitext(dst)[0] + ".kicad_pro")
    return len(keys)


def blockers_of(B, net, cons=None, spec=None):
    """nets the router's diagnosis names as blocking `net` (sealed pads), nearest first."""
    out = []
    for r in RD.diagnose(B, [net], cons, spec)["nets"]:
        for c in r["causes"]:
            for b in c.get("blockers", []):
                if b != net and b not in out:
                    out.append(b)
    return out


def signal_layers_unused(B, cons, used):
    """stackup signal layers (never plane layers) the router was not given."""
    from . import preroute as PR
    try:
        sig, planes = PR.layer_split(B, cons)
    except Exception:                                   # noqa: BLE001
        return []
    return [l for l in B["copper"] if l in sig and l not in planes and l not in used]


def clock(budget, t0, min_slice=0.0, now=time.time):
    """-> left(): seconds the ladder may still use = what is left of `budget` (started at `t0`), but never less than
    `min_slice` counted from this call.  The rounds, their audits and post-steps can overrun their share; the ladder's
    slice is not theirs to spend."""
    t1 = now()

    def left():
        t = now()
        return max(budget - (t - t0), min_slice - (t - t1), 0.0)
    return left


class Ladder:
    def __init__(self, best, workdir, run, run_for, conn, keep, left, B, cons, spec, cls, used_layers, trial_s=120,
                 post=None, log=lambda m: None):
        self.best, self.dir, self.run, self.run_for, self.conn, self.keep, self.left = best, workdir, run, run_for, conn, keep, left
        self.B, self.cons, self.spec, self.cls, self.used = B, cons, spec, cls, list(used_layers)
        self.trial_s, self.post, self.log = trial_s, post, log
        self.rec, self.k, self.tried = [], 0, set()
        os.makedirs(workdir, exist_ok=True)

    def path(self, tag):
        self.k += 1
        return os.path.join(self.dir, f"ladder-{self.k:03d}-{tag}.kicad_pcb")

    def tmo(self):
        return max(5.0, min(self.trial_s, self.left()))

    def open_now(self):
        return sum(self.conn(self.best).values())

    def note(self, rung, what, before, after, **kw):
        r = dict(rung=rung, what=what, open_before=before, open_after=after, closed=max(0, before - after), **kw)
        self.rec.append(r)
        self.log(f"    ladder {rung}: {what}: open {before} -> {after}")
        return r

    def adopt(self, path):
        """keep-best + post-step on the new best; -> open count."""
        n = self.keep(path)
        if self.post and os.path.exists(self.best):
            self.post(self.best)
            n = self.open_now()
        return n

    # ---- rungs
    def ripup(self, nets):
        pol = RIP.policy(self.cons)
        if pol == "never":
            return [self.note("ripup", "skipped: [route] allow_rip = never", self.open_now(), self.open_now())]
        ctl = RIP.controlled_nets(self.cls, self.cons, names=[p["net"] for p in self.B["pads"] if p["net"]]) if pol != "any" else {}
        plain = RIP.plain_nets(self.cons)
        out = []
        for n in nets[:6]:
            if self.left() < 10:
                break
            B = RD.load(self.best)
            bl = [b for b in blockers_of(B, n, self.cons, self.spec) if b not in ctl and b not in plain]
            key = ("ripup", n, tuple(bl))
            if not bl or key in self.tried:
                if not bl:
                    out.append(self.note("ripup", f"{n}: no ripable blocker reported", self.open_now(), self.open_now(), nets=[n]))
                continue
            self.tried.add(key)
            before = self.open_now()
            src, mid, dst = self.path("rip-in"), self.path("rip-mid"), self.path("rip-out")
            n_cut = _strip_unlocked(self.best, src, bl)
            self.run(src, mid, [n], self.tmo(), False)
            cur = mid if os.path.exists(mid) else src
            again = [b for b in bl if b in self.conn(cur)]
            if again and self.left() > 5:
                self.run(cur, dst, again, self.tmo(), False)
                cur = dst if os.path.exists(dst) else cur
            after = self.adopt(cur)
            out.append(self.note("ripup", f"{n}: ripped {len(bl)} net(s) ({n_cut} items): {', '.join(bl[:4])}", before, min(after, before),
                                 nets=[n]))
        return out

    def _sequential(self, nets, run, tag):
        """route `nets` one after the other on a growing copy of the best board; a net that does not lower the open count is dropped."""
        cur = self.best
        cur_open = self.open_now()
        for n in nets:
            if self.left() < 10:
                break
            dst = self.path(tag)
            shutil.copy(cur, dst + ".in")
            side = os.path.splitext(cur)[0] + ".kicad_pro"
            if os.path.exists(side):
                shutil.copy(side, os.path.splitext(dst + ".in")[0] + ".kicad_pro")
            run(dst + ".in", dst, [n], self.tmo(), False)
            if os.path.exists(dst):
                k = sum(self.conn(dst).values())
                if k < cur_open:
                    cur, cur_open = dst, k
            if os.path.exists(dst + ".in"):
                os.remove(dst + ".in")
        return cur

    def order(self, nets):
        pads = NC.net_pads(self.B)
        ln = lambda n: NC.net_length(self.B, n, pads.get(n))
        diag = {r["net"]: sum(1 for c in r["causes"] if c["cause"] == "SEALED_PAD") for r in RD.diagnose(RD.load(self.best), nets[:40], self.cons, self.spec)["nets"]}
        orders = {"shortest-first": sorted(nets, key=lambda n: (ln(n), n)),
                  "most-constrained-first": sorted(nets, key=lambda n: (-diag.get(n, 0), -len(pads.get(n, ())), ln(n), n))}
        out = []
        for name, od in orders.items():
            sig = ("order", name, tuple(od))
            if sig in self.tried or self.left() < 20:
                continue
            self.tried.add(sig)
            before = self.open_now()
            cur = self._sequential(od[:12], self.run, "ord")
            after = self.adopt(cur) if cur != self.best else before
            out.append(self.note("order", name, before, min(after, before), nets=od[:12]))
        return out

    def layer(self, nets):
        extra = signal_layers_unused(self.B, self.cons, self.used)
        if not extra:
            return [self.note("layer", "no further stackup signal layer (planes are never used); router has " + ",".join(self.used),
                              self.open_now(), self.open_now())]
        by_cls = {}
        for n in nets:
            by_cls.setdefault((self.cls.get(n) or {}).get("class", "other"), []).append(n)
        out = []
        for c, ns in sorted(by_cls.items(), key=lambda kv: -len(kv[1])):
            sig = ("layer", c, tuple(extra), tuple(ns))
            if sig in self.tried or self.left() < 20:
                continue
            self.tried.add(sig)
            before = self.open_now()
            run = self.run_for(self.used + extra)
            cur = self._sequential(ns[:8], run, "lay")
            after = self.adopt(cur) if cur != self.best else before
            out.append(self.note("layer", f"class {c} with +{','.join(extra)}", before, min(after, before), nets=ns[:8]))
        return out

    def climb(self):
        """-> {rungs: [records], exhausted, timed_out}."""
        timed_out = False
        while True:
            nets = sorted(self.conn(self.best), key=lambda n: (self.conn(self.best)[n], n))
            if not nets:
                break
            if self.left() < 15:
                timed_out = True
                break
            gain = 0
            for rung in (self.ripup, self.order, self.layer):
                if self.left() < 15:
                    timed_out = True
                    break
                nets = sorted(self.conn(self.best))
                if not nets:
                    break
                gain += sum(r["closed"] for r in rung(nets))
            if timed_out or not gain:
                break
        return {"rungs": self.rec, "exhausted": not timed_out and bool(self.conn(self.best)), "timed_out": timed_out}


def summary(res):
    closed = {}
    for r in res["rungs"]:
        closed[r["rung"]] = closed.get(r["rung"], 0) + r["closed"]
    return ("ladder " + ", ".join(f"{k} closed {v}" for k, v in closed.items() if k in RUNGS) +
            ("; time budget reached" if res["timed_out"] else "; ladder exhausted" if res["exhausted"] else ""))
