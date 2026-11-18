"""Progress tracking for long AI-driven repair sessions: `fluxplace progress checkpoint|block|show|brief`.

State: <workdir>/.fluxplace/progress.json = {accepted: {board, rev, ts, note}, history: [{ts, board, rev, metrics, note}],
blocked: [{item, reason, ts}]}. The verdict (progressing | stalled | looping) comes from the history and the call log
(<workdir>/.fluxplace/calls.jsonl, see calllog.py). Exit code of `show`: 0 progressing, 1 stalled, 2 looping.
Board-agnostic: metrics are caller-supplied numbers (plus `--measure`: unconnected / errors from a DRC of a temp copy).
"""
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import tempfile

N_LOOP, K_STALL, M_CALLS, OSC_WINDOW = 3, 5, 60, 6
EXIT = {"progressing": 0, "stalled": 1, "looping": 2}


def rev(path):
    """first 12 hex of sha256 of the file bytes (same definition as boardrev.rev on main)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()[:12]


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _dt(s):
    try:
        return datetime.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def path_of(workdir):
    return os.path.join(workdir, ".fluxplace", "progress.json")


def load(workdir):
    try:
        with open(path_of(workdir)) as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            return {"accepted": d.get("accepted"), "history": list(d.get("history") or []), "blocked": list(d.get("blocked") or [])}
    except (OSError, ValueError):
        pass
    return {"accepted": None, "history": [], "blocked": []}


def save(workdir, st):
    p = path_of(workdir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, p)


def parse_metrics(items):
    out = {}
    for it in items or ():
        k, sep, v = it.partition("=")
        if not sep or not k.strip():
            raise ValueError(f"bad --metric {it!r} (want key=value)")
        try:
            f = float(v)
        except ValueError:
            raise ValueError(f"bad --metric {it!r}: value is not a number")
        out[k.strip()] = int(f) if f == int(f) else f
    return out


def measure(board, timeout=900, cli=None):
    """{unconnected, errors} from a DRC of a temp COPY of the board (the original is never refilled)."""
    cli = cli or os.environ.get("FLUXPLACE_KICAD_CLI") or shutil.which("kicad-cli") or "kicad-cli"
    with tempfile.TemporaryDirectory(prefix="fxprog-") as d:
        base = os.path.splitext(os.path.basename(board))[0]
        tb = os.path.join(d, base + ".kicad_pcb")
        shutil.copy2(board, tb)
        for ext in (".kicad_pro", ".kicad_dru"):
            side = os.path.splitext(board)[0] + ext
            if os.path.exists(side):
                shutil.copy2(side, os.path.join(d, base + ext))
        rep = os.path.join(d, "drc.json")
        subprocess.run([cli, "pcb", "drc", "--severity-error", "--format", "json", "--refill-zones", "--output", rep, tb],
                       capture_output=True, timeout=timeout)
        with open(rep) as fh:
            j = json.load(fh)
        if not isinstance(j.get("violations"), list):
            raise RuntimeError("DRC report has no violations list")
        shorts = sum(1 for v in j["violations"] if v.get("type") == "shorting_items")
        from . import drcguard as DG
        split = DG.open_split(j)
        return {"unconnected": len(j.get("unconnected_items") or []), "errors": len(j["violations"]), "shorts": shorts,
                "track_opens": split["track_opens"], "zone_islands": sum(split["islands"].values()),
                "islands": [[net, layer, n] for (net, layer), n in sorted(split["islands"].items())]}


def checkpoint(board, workdir=None, note="", metrics=None, do_measure=False, measure_fn=None, allow_new_shorts=False):
    """Append a checkpoint, set `accepted`. -> (state, checkpoint).
    Shorts gate: when the previous accepted checkpoint recorded `shorts` (DRC shorting_items), the board is measured too,
    and it is refused (ValueError, nothing written) when shorts went up, unless allow_new_shorts."""
    board = os.path.abspath(board)
    if not os.path.isfile(board):
        raise FileNotFoundError(board)
    workdir = workdir or os.path.dirname(board)
    m = dict(metrics or {})
    st = load(workdir)
    prev = st["history"][-1]["metrics"] if st["history"] else {}
    islands = None
    if do_measure or "shorts" in prev:
        got = dict((measure_fn or measure)(board))
        islands = got.pop("islands", None)                  # a list, kept on the checkpoint, not in the metrics
        m.update(got)
    if "shorts" in prev and "shorts" in m and m["shorts"] > prev["shorts"] and not allow_new_shorts:
        raise ValueError(f"refused: {m['shorts']} shorting_items vs {prev['shorts']} at the last accepted checkpoint "
                         f"(new shorts; --allow-new-shorts to accept anyway)")
    cp = {"ts": now(), "board": board, "rev": rev(board), "metrics": m, "note": note or ""}
    if islands is not None:
        cp["islands"] = islands
    st["history"].append(cp)
    st["accepted"] = {"board": board, "rev": cp["rev"], "ts": cp["ts"], "note": cp["note"]}
    save(workdir, st)
    return st, cp


def block(item, reason, workdir):
    st = load(workdir)
    st["blocked"].append({"item": item, "reason": reason or "", "ts": now()})
    save(workdir, st)
    return st


# ------------------------------------------------------------------ call log
def calls_since(workdir, since):
    """[call records] after `since` (ISO ts or None = all), excluding progress/calls commands. Missing log -> []."""
    out = []
    t0 = _dt(since) if since else None
    try:
        with open(os.path.join(workdir, ".fluxplace", "calls.jsonl")) as fh:
            for ln in fh:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(r, dict) or r.get("cmd") in ("progress", "calls"):
                    continue
                t = _dt(r.get("ts"))
                if t0 and t and t < t0:
                    continue
                out.append(r)
    except OSError:
        pass
    return out


def _improved(prev, cur):
    return any(k in prev and v < prev[k] for k, v in cur.items() if isinstance(v, (int, float)) and isinstance(prev.get(k), (int, float)))


def _oscillates(hist, window):
    """metric names that went up >= 2 times and down >= 2 times across the last `window` checkpoints, net no better."""
    h = hist[-window:]
    out = []
    keys = {k for c in h for k in (c.get("metrics") or {})}
    for k in sorted(keys):
        vs = [c["metrics"][k] for c in h if isinstance((c.get("metrics") or {}).get(k), (int, float))]
        if len(vs) < 3:
            continue
        d = [b - a for a, b in zip(vs, vs[1:])]
        if sum(x > 0 for x in d) >= 2 and sum(x < 0 for x in d) >= 2 and vs[-1] >= vs[0]:
            out.append(f"{k} {'>'.join(str(v) for v in vs)}")
    return out


def verdict(st, calls, n=N_LOOP, k=K_STALL, m=M_CALLS, window=OSC_WINDOW):
    """-> (verdict, [evidence lines]). `calls` = call-log records since the last checkpoint."""
    hist = st.get("history") or []
    ev = []
    groups = {}
    for c in calls:
        groups.setdefault(tuple(c.get("argv") or ()), []).append(c.get("rc"))
    for argv, rcs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(rcs) >= n and all(rc for rc in rcs):
            ev.append(f"same command failed {len(rcs)}x since the last checkpoint: {' '.join(argv)[:100]}")
    for o in _oscillates(hist, window):
        ev.append(f"metric oscillating with no net gain over the last {window} checkpoints: {o}")
    for b in st.get("blocked") or ():
        item = str(b.get("item") or "")
        hits = sum(1 for c in calls if item and item in " ".join(c.get("argv") or ()))
        if item and hits > n:
            ev.append(f"blocked item {item!r} named in {hits} calls since the last checkpoint")
    if ev:
        return "looping", ev
    if len(hist) >= k + 1 and not any(_improved(a.get("metrics") or {}, b.get("metrics") or {}) for a, b in zip(hist[-k - 1:], hist[-k:])):
        ev.append(f"no improved metric in the last {k} checkpoints")
    if len(calls) > m:
        ev.append(f"{len(calls)} calls since the last checkpoint (limit {m})")
    if ev:
        return "stalled", ev
    if not hist and not calls:
        return "progressing", ["no data"]
    return "progressing", [f"{len(hist)} checkpoint(s), {len(calls)} call(s) since the last"]


def assess(workdir, **th):
    st = load(workdir)
    last = st["history"][-1]["ts"] if st["history"] else None
    calls = calls_since(workdir, last)
    v, ev = verdict(st, calls, **th)
    return st, calls, v, ev


# ------------------------------------------------------------------ text
def _fmt(v):
    return f"{v:g}" if isinstance(v, float) else str(v)


def _mline(m):
    return " ".join(f"{k}={_fmt(v)}" for k, v in sorted((m or {}).items())) or "-"


def _change(hist):
    if not hist:
        return "no checkpoints"
    a, b = hist[0].get("metrics") or {}, hist[-1].get("metrics") or {}
    ks = [k for k in sorted(set(a) | set(b)) if k in a and k in b]
    if not ks:
        return "current " + _mline(b)
    return ", ".join(f"{k} {_fmt(a[k])} -> {_fmt(b[k])}" for k in ks)


def failed_commands(calls, cap=10):
    g = {}
    for c in calls:
        if c.get("rc"):
            key = " ".join(c.get("argv") or ())[:100]
            g[key] = g.get(key, 0) + 1
    return sorted(g.items(), key=lambda kv: (-kv[1], kv[0]))[:cap]


def table(hist, last=None):
    rows = hist[-last:] if last else hist
    return [f"  {c['ts'][5:19]}  {c.get('rev', '?')}  {_mline(c.get('metrics'))}" + (f"  | {c['note'][:50]}" if c.get("note") else "") for c in rows]


def show_text(workdir, **th):
    st, calls, v, ev = assess(workdir, **th)
    acc = st["accepted"]
    out = [f"accepted: {acc['board'] if acc else '(none)'}"]
    out.append("history:" if st["history"] else "history: none")
    out += table(st["history"])
    if st["blocked"]:
        out.append("blocked:")
        out += [f"  {b['item']}: {b['reason']}" for b in st["blocked"]]
    out.append(f"verdict: {v} - " + "; ".join(ev))
    return "\n".join(out), v, st, ev


def ledger_tail(board, n=5):
    p = (board or "") + ".session/ledger.txt"
    try:
        with open(p, errors="replace") as fh:
            return p, [l.rstrip() for l in fh.read().splitlines() if l.strip()][-n:]
    except OSError:
        return None, []


def brief(workdir, max_chars=2500, **th):
    """-> (text, data). Sections 1-3 are never truncated; the oldest history goes first."""
    st, calls, v, ev = assess(workdir, **th)
    hist, acc = st["history"], st["accepted"]
    head = [f"1. accepted: {acc['board']} rev {acc['rev']}; last note: {acc['note'] or '-'}" if acc else "1. accepted: none (no checkpoint yet)",
            f"2. metrics: {_mline(hist[-1].get('metrics')) if hist else '-'}; change since first: {_change(hist)}",
            "3. blockers: " + ("; ".join(f"{b['item']} ({b['reason']})" for b in st["blocked"]) if st["blocked"] else "none")]
    fails = failed_commands(calls)
    ledger_path, ledger = ledger_tail(acc["board"] if acc else None)
    hl = table(hist, 5)
    parts = {"verdict": [f"4. verdict: {v} - " + "; ".join(ev)],
             "retry": ["5. do not retry (failed since the last checkpoint):"] + ([f"  {c}x {k}" for k, c in fails] or ["  none"]),
             "hist": ["6. last checkpoints:"] + (hl or ["  none"]),
             "ledger": ["7. session ledger: " + (ledger_path + "\n" + "\n".join("  " + l for l in ledger) if ledger_path else "none")]}

    def render():
        return "\n".join(head + parts["verdict"] + parts["retry"] + parts["hist"] + parts["ledger"])
    while len(render()) > max_chars and len(parts["hist"]) > 2:
        del parts["hist"][1]                                   # oldest checkpoint line first
    while len(render()) > max_chars and len(parts["ledger"][0].split("\n")) > 2:
        lines = parts["ledger"][0].split("\n")
        parts["ledger"] = ["\n".join(lines[:1] + lines[2:])]   # oldest ledger line
    while len(render()) > max_chars and len(parts["retry"]) > 2:
        parts["retry"].pop()
    text = render()
    if len(text) > max_chars:
        keep = len("\n".join(head)) + 1
        text = text[:max(max_chars, keep)]
    data = {"accepted": acc, "metrics": hist[-1].get("metrics") if hist else None, "change": _change(hist), "blocked": st["blocked"],
            "verdict": v, "evidence": ev, "do_not_retry": [{"command": k, "count": c} for k, c in fails],
            "checkpoints": hist[-5:], "ledger": {"path": ledger_path, "tail": ledger}}
    return text, data
