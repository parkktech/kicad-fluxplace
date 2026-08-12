"""Design-completeness LINT: catch missing wiring and connector-choice smells
before placement/routing effort is spent on a board that isn't finished.

Checks (v1):
  no-power-entry        no connector carries a power-family net
  no-io-connector       board has no connectors at all
  unwired-connector     a connector whose pads carry no nets
  dead-end-net          net with exactly one pad (a wire to nowhere)
  no-gnd-on-part        part (>=4 pads) powered but with no ground pin
  no-net-pads           parts with unconnected pads (info: may be NC by design)
  barrel-jack           barrel/DC jack found — prefer a LATCHING connector
                        (JST VH/SM, Molex Micro-Fit/Mini-Fit, screw terminal):
                        barrel plugs walk out under vibration
  power-on-friction-header
                        power on a bare 0.1" pin header — same latching advice

Pure python: operates on a flat pad list so it is testable without pcbnew.
`pads_from_board(board)` adapts a pcbnew BOARD. Every finding is a dict
{severity, code, msg, refs}; severities: error > warning > info.
"""
import re

__all__ = ["run", "pads_from_board", "POWER_RE", "GND_RE"]

POWER_RE = re.compile(
    r"^(\+?\d+(\.\d+)?V\d*(_\w+)?|VCC\w*|VDD\w*|VIN\w*|VBUS\w*|VBAT\w*|"
    r"PWR\w*|V_?SYS\w*|P\d+V\d*(_\w+)?|\w*_\d+V\d*)$", re.I)
GND_RE = re.compile(r"^(GND\w*|AGND|DGND|PGND|VSS\w*|EARTH|0V)$", re.I)


def _basename(net):
    """KiCad hierarchical nets ('/POWER_ENTRY/FAN_12V') -> leaf name."""
    return net.rsplit("/", 1)[-1]


def _is_power(net):
    return bool(POWER_RE.match(_basename(net)))

CONN_REF_RE = re.compile(r"^(J|CN|X|P)\d+", re.I)
CONN_FP_RE = re.compile(
    r"conn|header|jack|socket|terminal|usb|xlr|sma|u\.fl|jst|molex|phoenix|"
    r"df\d+|receptacle|plug", re.I)
BARREL_RE = re.compile(r"barrel|dc.?jack|jack.?dc|\bpj-?\d|5\.5x2\.[15]", re.I)
FRICTION_HDR_RE = re.compile(r"pinheader|pin_header|pinsocket|pin_socket", re.I)
RF_FRICTION_RE = re.compile(r"u\.?fl|umcc|\bmhf\b|ipex|i-pex", re.I)
MECH_RE = re.compile(r"mount|fiducial|logo|hole|testpoint|tp_|solderjumper", re.I)


def pads_from_board(board):
    """pcbnew BOARD -> flat pad list for run()."""
    out = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        fpname = str(fp.GetFPID().GetLibItemName())
        val = fp.GetValue()
        for pad in fp.Pads():
            out.append(dict(
                ref=ref, footprint=fpname, value=val,
                pad=pad.GetNumber(), net=pad.GetNetname() or "",
                drill=pad.GetDrillSize().x > 0))
    return out


def _is_connector(ref, fpname, value):
    if MECH_RE.search(fpname):
        return False
    return bool(CONN_REF_RE.match(ref)) and bool(
        CONN_FP_RE.search(fpname + " " + value))


def run(pads, waivers=None):
    """pads: [{ref, footprint, value, pad, net, drill}] -> findings list.

    waivers: ["code:regex", ...] — drop findings whose code matches and whose
    msg or any ref matches the regex (for de-scoped features, spare footprints,
    split-board interconnect stubs)."""
    findings = []
    by_part = {}
    net_pads = {}
    for p in pads:
        by_part.setdefault(p["ref"], []).append(p)
        if p["net"]:
            net_pads.setdefault(p["net"], []).append(p)

    connectors = {r for r, ps in by_part.items()
                  if _is_connector(r, ps[0]["footprint"], ps[0]["value"])}

    def add(sev, code, msg, refs):
        findings.append(dict(severity=sev, code=code, msg=msg,
                             refs=sorted(refs)))

    # --- power entry / io presence ---------------------------------------
    if not connectors:
        add("warning", "no-io-connector",
            "no connectors found — how do power and signals get on/off "
            "this board?", [])
    else:
        entry = {r for r in connectors
                 if any(_is_power(p["net"]) for p in by_part[r])}
        if not entry:
            add("warning", "no-power-entry",
                "no connector carries a power-family net (VCC/VIN/+NV...) — "
                "missing power input wiring?", sorted(connectors))

    # --- unwired connectors ----------------------------------------------
    for r in sorted(connectors):
        ps = [p for p in by_part[r] if p["pad"]]
        if len(ps) >= 2 and not any(p["net"] for p in ps):
            add("error", "unwired-connector",
                f"{r} ({by_part[r][0]['footprint']}) has no nets on any pad",
                [r])

    # --- dead-end nets ----------------------------------------------------
    dead = [n for n, ps in net_pads.items()
            if len(ps) == 1 and not n.lower().startswith("unconnected")]
    for n in sorted(dead):
        p = net_pads[n][0]
        add("warning", "dead-end-net",
            f"net '{n}' reaches only {p['ref']} pad {p['pad']} — "
            "a wire to nowhere", [p["ref"]])

    # --- powered part without ground -------------------------------------
    for r, ps in sorted(by_part.items()):
        numbered = [p for p in ps if p["pad"]]
        if len(numbered) < 4 or r in connectors:
            continue
        if MECH_RE.search(ps[0]["footprint"]):
            continue
        has_pwr = any(_is_power(p["net"]) for p in numbered)
        has_gnd = any(GND_RE.match(_basename(p["net"])) for p in numbered)
        if has_pwr and not has_gnd:
            add("warning", "no-gnd-on-part",
                f"{r} ({ps[0]['value']}) has power but no ground pin — "
                "missing return path?", [r])

    # --- unconnected pads (informational: NC pins are legitimate) --------
    loose = []
    for r, ps in sorted(by_part.items()):
        numbered = [p for p in ps if p["pad"]]
        if len(numbered) < 2 or MECH_RE.search(ps[0]["footprint"]):
            continue
        n_open = sum(1 for p in numbered if not p["net"])
        if n_open:
            loose.append((r, n_open, len(numbered)))
    if loose:
        add("info", "no-net-pads",
            "unconnected pads (verify each is NC by design): " +
            ", ".join(f"{r} {k}/{n}" for r, k, n in loose),
            [r for r, _, _ in loose])

    # --- connector style: latch beats friction ---------------------------
    for r, ps in sorted(by_part.items()):
        blob = ps[0]["footprint"] + " " + ps[0]["value"]
        if BARREL_RE.search(blob):
            add("warning", "barrel-jack",
                f"{r} is a barrel/DC jack — prefer a LATCHING connector "
                "(JST VH/SM, Molex Micro-Fit/Mini-Fit, screw terminal); "
                "barrel plugs walk out under vibration", [r])
    for r in sorted(connectors):
        ps = by_part[r]
        blob = ps[0]["footprint"]
        if FRICTION_HDR_RE.search(blob) and any(
                _is_power(p["net"]) for p in ps):
            add("warning", "power-on-friction-header",
                f"{r} feeds power through a friction-fit pin header — "
                "use a latching connector for anything that must survive "
                "vibration", [r])

    # --- RF friction coax: U.FL/MHF snap-fits walk off under vibration ---
    for r, ps in sorted(by_part.items()):
        blob = (ps[0]["footprint"] + " " + ps[0]["value"])
        if RF_FRICTION_RE.search(blob) and not re.search(r"lk|lock", blob, re.I):
            add("warning", "rf-friction-coax",
                f"{r} is a snap-fit micro-coax (U.FL/MHF class, ~30 cycles, "
                "a few N retention) — for vibration environments use a "
                "locking variant (I-PEX MHF LK), a threaded connector "
                "(SMA/MMCX), or adhesive-stake the mated pair with strain "
                "relief", [r])

    if waivers:
        rules = []
        for w in waivers:
            code, _, pat = w.partition(":")
            rules.append((code, re.compile(pat or ".")))
        findings = [f for f in findings if not any(
            f["code"] == c and (rx.search(f["msg"]) or
                                any(rx.search(r) for r in f["refs"]))
            for c, rx in rules)]
    return findings


def summarize(findings, log=print):
    order = {"error": 0, "warning": 1, "info": 2}
    n = {"error": 0, "warning": 0, "info": 0}
    for f in sorted(findings, key=lambda f: (order[f["severity"]], f["code"])):
        n[f["severity"]] += 1
        log(f"  {f['severity'].upper():7s} {f['code']}: {f['msg']}")
    log(f"lint: {n['error']} errors, {n['warning']} warnings, "
        f"{n['info']} info")
    return n
