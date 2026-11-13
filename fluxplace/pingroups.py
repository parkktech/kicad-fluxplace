"""Pin-function groups for ANY multi-channel part (generalises the magnetics `channels`).

A spec part declares its functional pin groups from the datasheet, citing the page:

    "pin_groups": [{"name": "CH1",
                    "roles": {"in_p": "1", "in_n": "2", "out_p": "6", "out_n": "5", "ct": "3"},
                    "pairs": [["in_p", "in_n"], ["out_p", "out_n"]],      # the legs of one differential pair
                    "links": [["in_p", "out_p"], ["in_n", "out_n"]],       # the signal continues from one role to the other
                    "tied":  [],                                           # roles the part connects internally (same net required)
                    "kinds": {"ct": "ct"}}],                               # else inferred from the role name
    "pinmap_source": "X.pdf#p3"

What the roles are is the datasheet's business: a transformer's windings, an ESD array's in/out through-pins, a mux's A/B/select,
a dual op-amp's unit pins, a connector's pair assignment.  Kinds: signal (default), ct (centre tap), nc, supply, control.

Generic checks on the board's pad nets (codes; FAIL unless noted):
  PINGRP_PAIR_SPLIT      the two legs of a declared pair are not one pair (polarity-named nets with different bases, only one leg a
                         pair net, or both legs on one net)
  PINGRP_ROLE_MISUSE     a signal net on a ct / nc / supply pin (a net that carries a pair leg of the part, a polarity-named net, or,
                         for ct/nc, a net reaching a connector); or, in a group that has a ct, a signal pin on a bias/termination net
  PINGRP_TIED_SHORT      roles the part ties internally sit on different nets (the part would short them)
  PINGRP_POLARITY        WARN (FAIL with `[pin_groups] polarity = "strict"`): a linked P/N pair arrives swapped
  PINGRP_UNSOURCED       WARN: groups without pinmap_source
  PINGRP_PIN_UNRESOLVED  WARN: a role names a pad the board part does not have
  PINGRP_NONE_DECLARED   WARN: the pinmap names look multi-channel (same stem repeated) and no groups/channels are declared

`channels` (magnetics) keep their own profile: `check` runs magnetics.check for them with the MAG_* codes unchanged, and
`from_channels` converts them to generic groups (the generic engine finds the same pads).  Nothing here is board- or part-specific."""
import re

from . import magnetics as MG

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"
CODES = ("PINGRP_PAIR_SPLIT", "PINGRP_ROLE_MISUSE", "PINGRP_TIED_SHORT", "PINGRP_POLARITY", "PINGRP_UNSOURCED",
         "PINGRP_PIN_UNRESOLVED", "PINGRP_NONE_DECLARED")
_KIND_RX = (("ct", re.compile(r"(^|_)(ct|tap|center|centre)(_|\d|$)|ct\d*$", re.I)),
            ("nc", re.compile(r"(^|_)(nc|dnc|n_c)(_|\d|$)", re.I)),
            ("supply", re.compile(r"(^|_)(vcc|vdd|vss|vee|gnd|agnd|dgnd|vbus|vin|pwr|supply|vcca|vccd)(_|\d|$)", re.I)),
            ("control", re.compile(r"(^|_)(sel|select|en|enable|ctrl|control|oe|cs)(_|\d|$)", re.I)))


def _f(level, code, msg, refs=()):
    return {"level": level, "code": code, "msg": msg, "refs": sorted(set(refs))}


def kind_of(role, group):
    k = (group.get("kinds") or {}).get(role)
    if k:
        return k
    for name, rx in _KIND_RX:
        if rx.search(role):
            return name
    return "signal"


def from_channels(comp):
    """magnetics `channels` -> generic groups (the transformer profile)."""
    out = []
    for i, ch in enumerate(comp.get("channels") or [], 1):
        roles = {k: str(v) for k, v in ch.items() if v not in (None, "")}
        out.append({"name": f"CH{i}", "roles": roles, "pairs": [["chip_p", "chip_n"], ["line_p", "line_n"]],
                    "links": [["chip_p", "line_p"], ["chip_n", "line_n"]], "kinds": {"chip_ct": "ct", "line_ct": "ct"}})
    return out


def groups_of(comp, channels=False):
    g = list(comp.get("pin_groups") or [])
    if isinstance(comp.get("pin_groups"), dict):
        g = list(comp["pin_groups"].get("groups") or [])
    return g + (from_channels(comp) if channels else [])


def _stem_counts(pinmap):
    stems = {}
    for name in (pinmap or {}):
        s = re.sub(r"\d+", "#", str(name))
        stems.setdefault(s, set()).add(re.sub(r"\D+", "", str(name)))
    skip = re.compile(r"RSVD|CONFIG|GND|VCC|VDD|VSS|NC|DNC|PAD|EP|N/C", re.I)
    return {s: n for s, n in stems.items() if len(n) >= 3 and re.search(r"[A-Za-z]", s) and not skip.search(s)}


def check(parts, net_pads, spec, cons=None, generic_channels=False):
    """parts {ref: {"pads": {num: net}, ...}}, net_pads {net: [(ref, pad)]}, spec -> findings.  `generic_channels`: run the generic
    engine on magnetics channels too (instead of the magnetics profile)."""
    cfg = (cons or {}).get("pin_groups", {}) or {}
    strict = str(cfg.get("polarity", "warn")).lower() == "strict"
    out = []
    comps = [c for c in (spec or {}).get("components", []) if isinstance(c, dict)]
    if not generic_channels:
        out += MG.check(parts, net_pads, spec, cons)
    for comp in comps:
        ref = comp.get("ref")
        groups = groups_of(comp, channels=generic_channels)
        part = parts.get(ref)
        if part is None:
            continue
        pads = part.get("pads") if isinstance(part.get("pads"), dict) else {}
        if not groups:
            if not comp.get("channels") and len(comp.get("pinmap") or {}) >= 6 and not MG._CONN_REF.match(ref or "") \
                    and _stem_counts(comp.get("pinmap")):
                st = _stem_counts(comp["pinmap"])
                out.append(_f(WARN, "PINGRP_NONE_DECLARED",
                              f"{ref} ({comp.get('value')}): pin names repeat per channel ({', '.join(sorted(st)[:4])}) and no pin_groups "
                              f"are declared: read the datasheet and declare the channels/pairs", refs=[ref]))
            continue
        pg = comp.get("pin_groups")
        if not (comp.get("pinmap_source") or (pg.get("source") if isinstance(pg, dict) else None)):
            out.append(_f(WARN, "PINGRP_UNSOURCED", f"{ref}: pin_groups declared without pinmap_source (datasheet file#page)", refs=[ref]))

        def net(pad):
            return pads.get(str(pad)) or ""

        sig_nets = set()
        for g in groups:
            for role, pad in (g.get("roles") or {}).items():
                if kind_of(role, g) == "signal" and net(pad):
                    sig_nets.add(net(pad))

        def conn(n):
            return [(r, p) for r, p in {tuple(x) for x in net_pads.get(n, [])} if r != ref and MG._is_conn(r, parts.get(r))]

        def only_res(n):
            oth = [(r, p) for r, p in {tuple(x) for x in net_pads.get(n, [])} if r != ref]
            return bool(oth) and not conn(n) and all(MG._RES_REF.match(r) for r, _ in oth)

        for g in groups:
            gn = f"{ref} {g.get('name', '?')}"
            roles = {k: str(v) for k, v in (g.get("roles") or {}).items()}
            bad = [k for k, v in roles.items() if v not in pads and v not in (part.get("pad_geom") and [x[0] for x in part["pad_geom"]] or [])]
            for k in bad:
                out.append(_f(WARN, "PINGRP_PIN_UNRESOLVED", f"{gn} {k}={roles[k]}: no such pad on the board part", refs=[ref]))
            roles = {k: v for k, v in roles.items() if k not in bad}
            has_ct = any(kind_of(k, g) == "ct" for k in roles)
            for a, b in g.get("pairs") or []:
                if a not in roles or b not in roles:
                    continue
                na, nb = net(roles[a]), net(roles[b])
                if not na or not nb:
                    continue
                if na == nb:
                    out.append(_f(FAIL, "PINGRP_PAIR_SPLIT", f"{gn}: both legs of the pair {a}/{b} (pads {roles[a]}/{roles[b]}) are on {na}", refs=[ref]))
                    continue
                ba, bb = MG._base(na), MG._base(nb)
                if (ba is not None or bb is not None) and ba != bb:
                    out.append(_f(FAIL, "PINGRP_PAIR_SPLIT",
                                  f"{gn}: pair {a}/{b} on pads {roles[a]}/{roles[b]} carries {na}/{nb}, which are not one pair "
                                  f"(both legs of a pair stay in one group)", refs=[ref]))
            for role, pad in sorted(roles.items()):
                kind, n = kind_of(role, g), net(pad)
                if not n:
                    continue
                if kind in ("ct", "nc", "supply"):
                    why = None
                    if n in sig_nets:
                        why = "a net that carries a signal pin of this part"
                    elif MG._pol(n) and kind != "supply":
                        why = "a polarity-named signal net"
                    elif kind in ("ct", "nc") and conn(n):
                        why = f"a net that reaches {', '.join(f'{r}.{p}' for r, p in sorted(conn(n))[:3])}"
                    if why:
                        out.append(_f(FAIL, "PINGRP_ROLE_MISUSE", f"{gn}: pad {pad} ({role}, {kind}) is on {n}, {why}: the datasheet has a "
                                      f"{kind} here, not a signal", refs=[ref]))
                elif kind == "signal" and has_ct and only_res(n):
                    out.append(_f(FAIL, "PINGRP_ROLE_MISUSE", f"{gn}: pad {pad} ({role}, signal) is on {n}, a bias/termination net "
                                  f"(resistors only, no connector): the datasheet has the signal here", refs=[ref]))
            for a, b in g.get("tied") or []:
                if a in roles and b in roles and net(roles[a]) and net(roles[b]) and net(roles[a]) != net(roles[b]):
                    out.append(_f(FAIL, "PINGRP_TIED_SHORT", f"{gn}: pads {roles[a]} ({a}) and {roles[b]} ({b}) are tied inside the part but "
                                  f"on {net(roles[a])} and {net(roles[b])}", refs=[ref]))
            links = [l for l in g.get("links") or [] if l[0] in roles and l[1] in roles]
            pol = [(l, MG._pol(net(roles[l[0]])), MG._pol(net(roles[l[1]]))) for l in links]
            known = [x for x in pol if x[1] and x[2]]
            if len(known) >= 2 and all(p1 != p2 for _, p1, p2 in known):
                out.append(_f(FAIL if strict else WARN, "PINGRP_POLARITY",
                              f"{gn}: linked pairs arrive with P and N swapped ("
                              + ", ".join(f"{roles[l[0]]}={net(roles[l[0]])} -> {roles[l[1]]}={net(roles[l[1]])}" for l, _, _ in known)
                              + ")", refs=[ref]))
    return out


def summary(findings):
    n = {FAIL: 0, WARN: 0, INFO: 0}
    for f in findings:
        n[f["level"]] += 1
    return n


# ------------------------------------------------------------------ command
def cmd_pin_groups(a):
    import json
    from . import constraints as C, review as R
    spec = json.load(open(a.spec)) if a.spec else None
    if not spec:
        raise SystemExit("pin-groups: --spec is required (parts declare their groups there)")
    cons = C.load(a.constraints)
    facts = R.facts_from_board(a.board, backend="file")
    f = check(facts["parts"], facts.get("net_pads") or {}, spec, cons, generic_channels=a.generic)
    order = {"FAIL": 0, "WARN": 1, "INFO": 2}
    f.sort(key=lambda x: (order[x["level"]], x["code"], x["msg"]))
    n = summary(f)
    ngroups = sum(len(groups_of(c, channels=True)) for c in spec.get("components", []) if isinstance(c, dict))
    from . import parts as P
    top = getattr(a, "top", 10) or 10
    head = f"pin-groups: {'FAIL' if n['FAIL'] else 'ok'} - {n['FAIL']} FAIL, {n['WARN']} WARN ({ngroups} declared group(s)/channel(s))"
    if a.json:
        print(json.dumps({"board": a.board, "groups": ngroups, "counts": n, "findings": f}, indent=1))
    elif getattr(a, "quiet", False):
        print(head)
    else:
        print(head)
        for x in (f if getattr(a, "full_output", False) else P._spread(f, top)):
            print(f"  {x['level']:4s} {x['code']} {x['msg']}"[:230])
        if len(f) > top and not getattr(a, "full_output", False):
            print(f"  ... {len(f) - top} more (--full)")
    if n["FAIL"]:
        raise SystemExit(1)


def add_parser(sub):
    from . import partscli as PC
    p = sub.add_parser("pin-groups", help="functional pin groups of multi-channel parts vs the board nets (pairs stay pairs, no signal on ct/nc)")
    p.add_argument("board")
    p.add_argument("--spec", default=None, help="spec JSON with pin_groups / channels")
    p.add_argument("--constraints", default=None, help="[pin_groups] polarity = strict, [magnetics]")
    p.add_argument("--generic", action="store_true", help="run magnetics channels through the generic engine (PINGRP_* codes)")
    p.add_argument("--json", action="store_true")
    PC._flags(p)
    p.set_defaults(fn=cmd_pin_groups)
