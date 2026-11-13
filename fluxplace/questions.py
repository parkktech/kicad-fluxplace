"""Ask, don't assume.  A command that meets something it cannot decide from the owner's records (spec JSON, constraints TOML,
`.fluxmap.json`, net classes) raises a QUESTION instead of guessing:

    {id (stable), topic, found, options [{choice, consequence}], default (used only when forced), settle (what would settle it)}

A command with an open question that affects its result stops BEFORE writing anything: exit code 3, the questions printed
compactly (`--json` as JSON) and written to `<board>.questions.json`.  Answers: constraints `[answers] id = "choice"`, or
`--answer id=choice` (repeatable); both are recorded in `<board>.fluxmap.json` (key `answers`), so a question is never asked
twice.  `--assume-defaults` proceeds with each default, prints `ASSUMED: <id>` and writes `<output>.assumed.json`; fab and
deliver refuse a board that carries one.  Decisive inferences are not questions: they print `inferred: ... because ...`.

An agent that gets exit code 3 relays the questions to the user and waits; it never answers them itself."""
import copy
import json
import os
import re

from . import netclass as NC

EXIT = 3


def _stem(board):
    return os.path.splitext(board)[0]


def questions_path(board):
    return _stem(board) + ".questions.json"


def assumed_path(board):
    return board + ".assumed.json"


def assumed_ids(board):
    """ids of assumed answers a board (and the boards it was made from) carries."""
    try:
        return list(json.load(open(assumed_path(board))).get("assumed", []))
    except (OSError, ValueError):
        return []


def notready_path(board):
    return board + ".notready.json"


def notready_reasons(board):
    """reasons a board (and the boards it was made from) carries from `ready --allow-incomplete`."""
    try:
        return list(json.load(open(notready_path(board))).get("reasons", []))
    except (OSError, ValueError):
        return []


def refuse_assumed(board, what="fab/deliver"):
    nr = notready_reasons(board)
    if nr:
        raise SystemExit(f"{what} refused: NOT READY: {'; '.join(nr)[:300]} (made with `ready --allow-incomplete`; fix the readiness blockers and re-run the stages)")
    ids = assumed_ids(board)
    if ids:
        raise SystemExit(f"{what} refused: {os.path.basename(board)} was made with assumed answers (ASSUMED: {', '.join(ids)}). "
                         "Answer them (--answer id=choice or [answers] in the constraints) and re-run the stages.")


def add_args(p, json_flag=True):
    p.add_argument("--answer", action="append", default=[], metavar="ID=CHOICE",
                   help="answer an open question (repeatable); recorded in the map")
    p.add_argument("--assume-defaults", dest="assume_defaults", action="store_true",
                   help="proceed with every question's default; outputs are marked ASSUMED and cannot be fabricated")
    if json_flag:
        p.add_argument("--qjson", action="store_true", help="print open questions as JSON (exit code 3)")


def parse_answers(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--answer {it!r}: expected ID=CHOICE")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


class Session:
    def __init__(self, board, cons=None, answers=None, assume=False, cmd=""):
        self.board, self.cons, self.cmd, self.assume = board, cons or {}, cmd, assume
        self.cli = dict(answers or {})
        self.cons_ans = {str(k): str(v) for k, v in ((cons or {}).get("answers") or {}).items()}
        self.map_ans = self._map_answers()
        self.open, self.assumed, self.used, self.notes, self.appliers = [], [], {}, [], []

    # -- storage
    def map_path(self):
        return _stem(self.board) + ".fluxmap.json"

    def _map_answers(self):
        try:
            return {str(k): str(v) for k, v in (json.load(open(self.map_path())).get("answers") or {}).items()}
        except (OSError, ValueError):
            return {}

    def persist(self):
        """answers used (from the command line or the constraints) go into the map, so they are never asked again."""
        new = {k: v for k, v in self.used.items() if self.map_ans.get(k) != v}
        if not new or not self.board or not os.path.exists(self.board):
            return
        path = self.map_path()
        try:
            m = json.load(open(path))
        except (OSError, ValueError):
            m = {"fluxmap": 1}
        m.setdefault("answers", {}).update(new)
        with open(path, "w") as fh:
            fh.write(json.dumps(m, separators=(",", ":"), ensure_ascii=True))

    # -- asking
    def inferred(self, what, because):
        self.notes.append(f"INFO inferred: {what} because {because}")

    def ask(self, qid, topic, found, options, default, settle, apply=None):
        """-> the chosen option.  Unanswered: the default is returned provisionally and the question is listed as open
        (or, with --assume-defaults, as assumed)."""
        ans = self.cli.get(qid, self.cons_ans.get(qid, self.map_ans.get(qid)))
        if ans is not None:
            if ans not in options:
                raise SystemExit(f"answer {qid}={ans!r}: choose one of {', '.join(options)}")
            self.used[qid] = ans
            choice = ans
        elif self.assume:
            self.assumed.append(qid)
            choice = default
        else:
            self.open.append({"id": qid, "topic": topic, "found": found, "default": default, "settle": settle,
                              "options": [{"choice": k, "consequence": v} for k, v in options.items()]})
            choice = default
        if apply:
            self.appliers.append((apply, choice))
        return choice

    def apply(self, cons, spec):
        """(constraints, spec) copies with the answers applied."""
        cons2, spec2 = copy.deepcopy(cons or {}), copy.deepcopy(spec) if spec is not None else None
        box = {"cons": cons2, "spec": spec2}
        for fn, choice in self.appliers:
            fn(box, choice)
        return box["cons"], box["spec"]

    # -- output
    def gate(self, want_json=False, out=print):
        """Print notes; with open questions write <board>.questions.json, print them, -> EXIT, else 0."""
        for n in self.notes[:8]:
            out(n)
        if len(self.notes) > 8:
            out(f"INFO inferred: ... {len(self.notes) - 8} more")
        for qid in self.assumed:
            out(f"ASSUMED: {qid}")
        if not self.open:
            return 0
        doc = {"board": self.board, "command": self.cmd, "questions": self.open,
               "answer_with": "--answer ID=CHOICE  or  [answers] ID = \"CHOICE\" in the constraints"}
        try:
            with open(questions_path(self.board), "w") as fh:
                json.dump(doc, fh, indent=1)
        except OSError:
            pass
        if want_json:
            out(json.dumps(doc, indent=1))
            return EXIT
        out(f"{self.cmd or 'command'}: {len(self.open)} question(s) open, nothing written (exit {EXIT}); "
            f"answer with --answer ID=CHOICE or [answers]; file {questions_path(self.board)}")
        for q in self.open:
            out(f"  ? {q['id']} [{q['topic']}] {q['found']}")
            out("      " + " | ".join(f"{o['choice']}: {o['consequence']}" for o in q["options"]))
            out(f"      default if forced: {q['default']}; settled by: {q['settle']}")
        return EXIT

    def mark(self, out_board):
        """outputs of an assumed run carry the ids (sidecar `<out>.assumed.json`); inherited from the input board."""
        ids = sorted(set(self.assumed) | set(assumed_ids(self.board)))
        if not ids or not out_board:
            return
        try:
            with open(assumed_path(out_board), "w") as fh:
                json.dump({"assumed": ids, "command": self.cmd}, fh)
        except OSError:
            return


def inherit(src, dst):
    """a board made from `src` carries its assumed answers."""
    nr = notready_reasons(src)
    if nr and os.path.exists(dst):
        try:
            with open(notready_path(dst), "w") as fh:
                json.dump({"reasons": nr, "command": "inherited"}, fh)
        except OSError:
            pass
    ids = sorted(set(assumed_ids(src)) | set(assumed_ids(dst)))
    if ids and os.path.exists(dst):
        try:
            with open(assumed_path(dst), "w") as fh:
                json.dump({"assumed": ids}, fh)
        except OSError:
            pass


# ------------------------------------------------------------------ checks
def _has(cons, *keys):
    c = cons or {}
    return any(c.get(k) for k in keys)


def _pro_has_netclasses(board):
    try:
        d = json.load(open(_stem(board) + ".kicad_pro"))["net_settings"]
        return bool(d.get("netclass_patterns")) or bool(d.get("netclass_assignments"))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _chunk(names, n=6):
    names = sorted(names)
    return ", ".join(names[:n]) + (f" (+{len(names) - n})" if len(names) > n else "")


def check_layer_roles(S, B, cons, spec=None, **_):
    """plane vs signal for inner layers whose zones do not decide it."""
    from . import preroute as PR
    pc = (cons or {}).get("preroute") or {}
    inner = B["copper"][1:-1]
    if not inner or "signal_layers" in pc or "plane_layers" in pc:
        return
    x0, y0, x1, y1 = B["bbox"]
    area = max((x1 - x0) * (y1 - y0), 1e-6)
    base_sig, base_planes = PR.layer_split(B, cons)
    amb, decisive = [], []
    for l in inner:
        cov = 0.0
        for z in B["zones"]:
            if z["net"] and not z["keepout"] and l in z["layers"] and z["pts"]:
                xs, ys = [q[0] for q in z["pts"]], [q[1] for q in z["pts"]]
                cov = max(cov, (max(xs) - min(xs)) * (max(ys) - min(ys)) / area)
        nt = sum(1 for s in B["segs"] if s["layer"] == l)
        if cov >= 0.6 and nt == 0:
            decisive.append(l)
        elif cov >= 0.2 or (cov >= 0.6 and nt):
            amb.append((l, cov, nt))
        elif nt:
            decisive.append(l)
    bare = [l for l in inner if l not in decisive and l not in [a[0] for a in amb]]
    if decisive:
        S.inferred(f"layer roles of {_chunk(decisive)}", "their zones/tracks decide it")
    for l, cov, nt in amb:
        S.ask(f"layer-role:{l}", "layer-roles", f"{l}: a net zone covers {cov * 100:.0f} % of the outline and {nt} track(s) run on it",
              {"plane": "no signal routing on it; fan-out and routing count it as a plane",
               "signal": "routable; the zone is just a pour"}, "signal",
              "a [preroute] plane_layers / signal_layers entry in the constraints",
              apply=_layer_applier(l, base_planes))
    if bare and len(bare) == len(inner) and len(inner) >= 2 and not decisive and not amb:
        S.ask("layer-roles:inner", "layer-roles", f"{len(inner)} inner layers ({_chunk(inner)}) carry no zone and no track: roles cannot be read",
              {"signal": "all inner layers are routable (most capacity, no return-path planes assumed)",
               "planes": "all inner layers are planes (no inner routing)"}, "signal",
              "[preroute] plane_layers / signal_layers, or the stackup",
              apply=lambda box, ch: box["cons"].setdefault("preroute", {}).__setitem__(
                  "plane_layers" if ch == "planes" else "signal_layers", list(inner) if ch == "planes" else list(B["copper"])))


def _layer_applier(layer, base_planes):
    def f(box, choice):
        pc = box["cons"].setdefault("preroute", {})
        planes = set(pc.get("plane_layers", base_planes))
        (planes.add if choice == "plane" else planes.discard)(layer)
        pc["plane_layers"] = sorted(planes)
    return f


def check_controlled(S, B, cons, spec=None, cls=None, **_):
    """pairs / RF / clocks / power rails by name when no net classes or constraints say so."""
    if _has(cons, "pairs", "rf", "power") or ((cons or {}).get("placement") or {}).get("net") or _pro_has_netclasses(S.board):
        return
    cls = cls if cls is not None else NC.classify(B, cons, spec)
    groups = {"pair": [], "rf": [], "clock": [], "rail": []}
    clean_pairs = []
    for n, i in sorted(cls.items()):
        c, why = i["class"], i.get("reason", "")
        if c == "pair":
            if i.get("kind") and i["kind"] != "other" and i.get("partner") in cls and cls[i["partner"]]["pads"] == i["pads"]:
                clean_pairs.append(n)
            else:
                groups["pair"].append(n)
        elif c == "rf" and why == "RF-named":
            groups["rf"].append(n)
        elif c == "clock" and why == "clock-named":
            groups["clock"].append(n)
        elif c == "rail" and why == "power-named net":
            groups["rail"].append(n)
    if clean_pairs:
        S.inferred(f"{len(clean_pairs)} differential-pair nets ({_chunk(clean_pairs, 4)})",
                   "both legs exist with equal pad counts and a known interface name")
    txt = {"pair": ("a _P/_N-style name pair of unknown interface", "controlled pair: routed together, never ripped, skew checked",
                    "ordinary signals"),
           "rf": ("RF in the name only", "RF net: budget, no vias, never ripped", "ordinary signals"),
           "clock": ("clock in the name only, no crystal/oscillator", "clock: short, never ripped", "ordinary signals"),
           "rail": ("power in the name only", "power rail: wide, rail routing", "ordinary signals")}
    for k, nets in groups.items():
        if nets:
            what, yes, no = txt[k]
            S.ask(f"controlled:{k}", "controlled-nets", f"{len(nets)} net(s) with {what}: {_chunk(nets)}",
                  {"controlled": yes, "ordinary": no}, "controlled",
                  "[pairs.*] / [rf] nets / [power.NET] in the constraints, or net classes in the .kicad_pro",
                  apply=_class_applier(nets, k))


def _class_applier(nets, kind):
    def f(box, choice):
        if choice == "ordinary":
            ov = box["cons"].setdefault("placement", {}).setdefault("net", {})
            for n in nets:
                ov.setdefault(n, {})["class"] = "signal"
        elif kind == "rail":
            pw = box["cons"].setdefault("power", {})
            for n in nets:
                pw.setdefault(n, {})
    return f


def check_rip_existing(S, B, cons, board_text=None, **_):
    """existing routing the command may not own: unlocked tracks/vias with no allow_rip decision."""
    if "allow_rip" in ((cons or {}).get("route") or {}):
        return
    free = sum(1 for s in B["segs"] if not s.get("locked")) + sum(1 for v in B["vias"] if not v.get("locked"))
    if not free:
        return
    S.ask("rip-existing", "existing-routing", f"{free} track/via item(s) already on the board are not fluxplace fan-out (not locked)",
          {"never": "routing only adds copper; a result that removes existing copper is dropped",
           "low-speed": "a result may remove existing copper of ordinary (not pair/RF/clock/locked) nets if every one is routed again"},
          "never", "[route] allow_rip = \"never\" | \"low-speed\" in the constraints",
          apply=lambda box, ch: box["cons"].setdefault("route", {}).__setitem__("allow_rip", ch))


def check_fixed_parts(S, B, cons, **_):
    """parts that look fixed (mounting hardware, edge connectors) but are neither locked nor declared."""
    from . import preroute as PR
    c = cons or {}
    if (c.get("placement") or {}).get("fixed") or c.get("lock"):
        return
    explicit = PR.explicit_fixed(B, cons)
    cand = sorted(PR.fixed_parts(B, cons) - explicit)
    if not cand:
        return
    S.ask("fixed-parts", "fixed-parts", f"{len(cand)} part(s) look fixed but are not locked or declared: {_chunk(cand)}",
          {"lock": "they stay where they are (mounting holes, connectors are the board's interface)",
           "movable": "the placer / floorplan may move them"}, "lock",
          "lock them in the board, or [placement] fixed / [lock.at] in the constraints",
          apply=lambda box, ch: box["cons"].setdefault("placement", {}).__setitem__(
              "fixed", sorted(set(box["cons"].get("placement", {}).get("fixed", [])) | (set(cand) if ch == "lock" else set()))))


def check_rail_pour(S, B, cons, cls=None, **_):
    """a rail (many pads, no plane) with no pour decision: pour or tree."""
    from . import railroute as RL
    cls = cls if cls is not None else NC.classify(B, cons)
    rails, _poured = RL.rail_nets(B, cons, cls)
    pads = NC.net_pads(B)
    for n in rails:
        if ((cons or {}).get("power") or {}).get(n, {}).get("pour") is not None:
            continue
        k = len(pads[n])
        S.ask(f"rail-pour:{n}", "rail-pour", f"{n}: {k} pads, no plane and no declared pour",
              {"tree": f"routed as a tree of {k - 1} two-pad jobs (many vias, slow)",
               "pour": "a zone on the outer layer carrying most of its pads (bounding box + 3 mm); its pads are fanned out to it"},
              "tree", "[power.\"%s\"] pour = true|false, or a [[plan.pour]] entry" % n,
              apply=_pour_applier(n, pads[n], B))


def _pour_applier(net, pads, B):
    def f(box, choice):
        pw = box["cons"].setdefault("power", {}).setdefault(net, {})
        if choice == "tree":
            pw["pour"] = False
            return
        pw["pour"] = True
        cu = B["copper"]
        n_top = sum(1 for p in pads if cu[0] in p["layers"])
        lay = cu[0] if n_top >= len(pads) / 2 else cu[-1]
        xs, ys = [p["x"] for p in pads], [p["y"] for p in pads]
        box["cons"].setdefault("plan", {}).setdefault("pour", []).append(
            {"net": net, "layers": [lay], "region": [round(min(xs) - 3, 2), round(min(ys) - 3, 2), round(max(xs) + 3, 2), round(max(ys) + 3, 2)]})
    return f


def check_floors(S, B, cons, **_):
    """no fab/route floor and no design rules on the board."""
    from . import routerules as RR
    c = cons or {}
    r = c.get("route") or {}
    if c.get("fab") or any(k in r for k in ("min_track_mm", "min_clearance_mm", "min_via_mm", "min_drill_mm")):
        return
    pro = RR._pro_rules(S.board) if S.board else {}
    if any(pro.get(k) for k in ("min_track_width", "min_clearance", "min_via_diameter")):
        return
    prof = {"standard": (0.127, 0.127, 0.4, 0.2), "relaxed": (0.2, 0.2, 0.5, 0.3), "advanced": (0.1, 0.1, 0.3, 0.15)}

    def f(box, ch):
        t, cl, v, d = prof[ch]
        box["cons"].setdefault("fab", {}).update({"min_track_mm": t, "min_clearance_mm": cl, "min_via_dia_mm": v, "min_drill_mm": d})
    S.ask("rule-floors", "rule-floors", "no [fab] / [route] floors and the board carries no design-rule minimums",
          {k: f"track/clearance {a} mm, via {c_} / drill {d} mm" for k, (a, _b, c_, d) in prof.items()}, "relaxed",
          "[fab] min_track_mm / min_clearance_mm / min_via_dia_mm / min_drill_mm, or the fab's capability sheet", apply=f)


def check_spec_board(S, B, cons, spec=None, **_):
    """the spec names parts or nets the board does not have."""
    if not spec or not spec.get("components"):
        return
    from . import escapeplan as EP
    refs = {c["ref"] for c in spec["components"]}
    missing = sorted(refs - set(B["parts"]))
    onboard = {p["net"] for p in B["pads"] if p["net"]}
    nets_missing = sorted(n for n in (spec.get("nets") or {}) if n not in onboard)
    extra = sorted(r for r in B["parts"] if r not in refs and not EP.is_mech(r))
    if not (missing or nets_missing or (extra and len(extra) > max(2, len(refs) // 10))):
        return
    bits = []
    if missing:
        bits.append(f"{len(missing)} spec part(s) not on the board ({_chunk(missing, 4)})")
    if nets_missing:
        bits.append(f"{len(nets_missing)} spec net(s) not on the board ({_chunk(nets_missing, 4)})")
    if extra and len(extra) > max(2, len(refs) // 10):
        bits.append(f"{len(extra)} board part(s) not in the spec ({_chunk(extra, 4)})")

    def f(box, ch):
        if ch == "trust-board" and box["spec"] is not None:
            box["spec"]["components"] = [c for c in box["spec"]["components"] if c["ref"] in B["parts"]]
            box["spec"]["nets"] = {n: v for n, v in (box["spec"].get("nets") or {}).items() if n in onboard}
    S.ask("spec-board", "spec-vs-board", "; ".join(bits),
          {"trust-board": "spec entries the board lacks are ignored; the board's parts and nets are the truth",
           "trust-spec": "spec facts (groups, pin roles) are used as written even where the board disagrees"}, "trust-board",
          "update the spec or the board so they agree (spec-check)", apply=f)


CHECKS = {"layer-roles": check_layer_roles, "controlled": check_controlled, "rip-existing": check_rip_existing,
          "fixed-parts": check_fixed_parts, "rail-pour": check_rail_pour, "floors": check_floors, "spec-board": check_spec_board}
def check_connector_entry(S, B, cons, **_):
    """connectors whose entry direction cannot be derived (asked only when the constraints carry `[intent]`)."""
    from . import connectors as CN, intent as IT
    if IT.enabled(cons):
        CN.ask(S, B, cons)


CHECKS["connector-entry"] = check_connector_entry
TOPICS = {"fanout": ("spec-board", "layer-roles", "controlled", "floors", "rail-pour"),
          "modules": ("spec-board", "fixed-parts", "layer-roles", "controlled"),
          "floorplan": ("spec-board", "fixed-parts", "layer-roles", "controlled", "connector-entry"),
          "route": ("spec-board", "rip-existing", "layer-roles", "controlled", "floors", "rail-pour")}


def guard(a, cmd, board, cons, spec, topics=None, B=None):
    """Raise the command's questions before it writes anything.  -> (Session, constraints, spec) with the answers applied;
    SystemExit(3) when a question is open."""
    from . import routediag as RD
    from . import facts as FA
    m = FA.load_map(board)
    cons, used = FA.apply_map(cons, m)                         # records of the map stand in for missing constraints
    S = Session(board, cons, parse_answers(getattr(a, "answer", None)), bool(getattr(a, "assume_defaults", False)), cmd)
    if used:
        S.inferred(f"{', '.join(used)} taken from {os.path.basename(FA.FM.default_path(board))}", "the constraints declare none and the map recorded them")
    B = B or RD.load(board)
    cls = NC.classify(B, cons, spec)
    for t in topics or TOPICS.get(cmd, ()):
        CHECKS[t](S, B, cons, spec=spec, cls=cls)
    code = S.gate(bool(getattr(a, "qjson", False)))
    if code:
        raise SystemExit(code)
    S.persist()
    cons2, spec2 = S.apply(cons, spec)
    return S, cons2, spec2
