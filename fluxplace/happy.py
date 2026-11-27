"""B15: the kicad-happy plugin's analyzers, called as subprocesses (never copied) from the existing stages.

  review stage  EMC pre-compliance   analyze_schematic + analyze_pcb --full -> analyze_emc      -> EMC_<RULE> findings
  ready stage   SPICE subcircuits    analyze_schematic -> simulate_subcircuits (needs a simulator) -> SPICE_<TYPE> findings

Declared per board in the constraints (a board with no `[happy]` table is not analysed):
  [happy]
  schematic = "../hardware/x.kicad_sch"   # relative to the constraints file; else the .kicad_sch next to the board
  fail = ["EMC_DC_001", "SPICE_"]         # codes (or prefixes ending in "_") promoted from WARN to FAIL; default none: warn-only
  timeout_s = 300                         # per analyzer process
  plugin = "/path/to/kicad-happy/skills"  # else $KICAD_HAPPY_SKILLS, else the newest ~/.claude/plugins/cache/kicad-happy install

A result is {"status": "OK" | "UNKNOWN", "reason": str, "findings": [{code, severity, msg, refs, rule}]}. UNKNOWN (plugin, schematic, simulator
missing, or an analyzer failed) is never a pass: callers report it as NOT CHECKED. Codes map to gap kinds / owner stages in gapmap.py.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

OK, UNKNOWN = "OK", "UNKNOWN"
WARN, FAIL = "WARN", "FAIL"

# every EMC rule id the plugin documents (emc/SKILL.md, emc_rules.py): each becomes the code EMC_<ID with _> and has a row in gapmap
EMC_RULES = ("GP-001 GP-002 GP-003 GP-004 GP-005 DC-001 DC-002 DC-003 IO-001 IO-002 SW-001 SW-002 SW-003 CK-001 CK-002 CK-003 CK-004 "
             "VS-001 SU-001 SU-002 SU-003 DP-001 DP-002 DP-003 DP-004 BE-001 BE-002 BE-003 PD-001 PD-002 PD-003 PD-004 RP-001 XT-001 "
             "EF-001 EF-002 ES-001 ES-002 TH-001 TH-002 SH-001 EE-001 EE-002 ML-001").split()
# simulate_subcircuits result types (simulation_results[].subcircuit_type): each becomes SPICE_<TYPE>
SPICE_TYPES = ("RC_FILTER LC_FILTER VOLTAGE_DIVIDER OPAMP_CIRCUIT CRYSTAL_CIRCUIT FEEDBACK_NETWORK TRANSISTOR_CIRCUIT CURRENT_SENSE "
               "PROTECTION_DEVICE DECOUPLING POWER_REGULATOR RF_MATCHING BRIDGE_CIRCUIT BMS_SYSTEM RF_CHAIN SNUBBER_CIRCUIT INRUSH").split()
UNKNOWN_CODES = ("EMC_UNKNOWN", "SPICE_UNKNOWN")


def codes():
    """Every code this adapter can emit as a finding (promotable to FAIL): gapmap asserts each has an owner stage."""
    return tuple(["EMC_" + r.replace("-", "_") for r in EMC_RULES] + ["SPICE_" + t for t in SPICE_TYPES] + list(UNKNOWN_CODES))


def settings(cons):
    """The `[happy]` table of the constraints, or None when the board did not opt in."""
    h = (cons or {}).get("happy")
    return dict(h) if isinstance(h, dict) and h.get("enabled", True) else None


def skills_dir(cfg=None):
    """The kicad-happy skills directory (holds kicad/, emc/, spice/), or None."""
    cands = [(cfg or {}).get("plugin"), os.environ.get("KICAD_HAPPY_SKILLS")]
    cands += sorted(glob.glob(os.path.expanduser("~/.claude/plugins/cache/kicad-happy/kicad-happy/*/skills")),
                    key=lambda p: [int(x) if x.isdigit() else 0 for x in re.split(r"\W+", p.split("kicad-happy/")[-1])], reverse=True)
    return next((c for c in cands if c and os.path.isfile(os.path.join(c, "emc", "scripts", "analyze_emc.py"))), None)


def simulator():
    """Path of an installed SPICE simulator (the plugin's own detection order: NGSPICE_PATH, ngspice, ltspice, xyce), or None."""
    env = os.environ.get("NGSPICE_PATH")
    if env and os.path.exists(env):
        return env
    return next((p for p in (shutil.which(n) for n in ("ngspice", "ltspice", "LTspice", "xyce", "Xyce")) if p), None)


def find_schematic(board, cons):
    """The schematic the analyzers read: `[happy] schematic`, else <board stem>.kicad_sch, else the only .kicad_sch beside the board."""
    s = (settings(cons) or {}).get("schematic")
    if s:
        return s if os.path.isfile(s) else None
    if not board:
        return None
    for d in dict.fromkeys((os.path.dirname(os.path.abspath(board)), os.path.dirname(os.path.realpath(board)))):
        own = os.path.join(d, os.path.splitext(os.path.basename(board))[0] + ".kicad_sch")
        if os.path.isfile(own):
            return own
        one = glob.glob(os.path.join(d, "*.kicad_sch"))
        if len(one) == 1:
            return one[0]
    return None


def _unknown(reason):
    return {"status": UNKNOWN, "reason": reason, "findings": []}


def _run(args, timeout):
    """Run one analyzer; (ok, error text)."""
    try:
        p = subprocess.run([sys.executable] + args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{type(e).__name__}: {e}"
    tail = (p.stderr or p.stdout).strip().splitlines()
    return p.returncode == 0, tail[-1] if tail else ""


_memo = {}


def _sig(*paths):
    return tuple((p, os.stat(p).st_mtime_ns, os.stat(p).st_size) for p in paths if p)


def _analyze(sk, sch, board, work, timeout):
    """Run the kicad skill's schematic (and, with a board, PCB) analyzer into `work`; (sch.json, pcb.json | None, error)."""
    sj = os.path.join(work, "sch.json")
    ok, err = _run([os.path.join(sk, "kicad", "scripts", "analyze_schematic.py"), sch, "--output", sj], timeout)
    if not ok:
        return None, None, f"analyze_schematic failed: {err}"
    if not board:
        return sj, None, ""
    pj = os.path.join(work, "pcb.json")
    ok, err = _run([os.path.join(sk, "kicad", "scripts", "analyze_pcb.py"), board, "--full", "--output", pj], timeout)
    return (sj, pj, "") if ok else (None, None, f"analyze_pcb failed: {err}")


def _load(path):
    with open(path) as fh:
        return json.load(fh)


def parse_emc(doc):
    """analyze_emc.py JSON -> [{code, severity, msg, refs, rule}]; INFO rows are kept (severity 'info') for the report, not dropped."""
    out = []
    for f in (doc or {}).get("findings") or []:
        if f.get("suppressed"):
            continue
        rule = str(f.get("rule_id") or "")
        if not rule:
            continue
        out.append({"code": "EMC_" + rule.replace("-", "_"), "rule": rule, "severity": str(f.get("severity") or "info").lower(),
                    "msg": f"{rule} {f.get('title') or f.get('summary') or ''}: {f.get('description') or ''}".strip()[:300],
                    "refs": list(f.get("components") or []), "nets": [n for n in (f.get("nets") or []) if n and n != "?"],
                    "confidence": f.get("confidence", "")})
    return out


def parse_spice(doc):
    """simulate_subcircuits.py JSON -> [{code, severity, msg, refs, rule}] for every result whose status is not pass/skip."""
    out = []
    for r in (doc or {}).get("simulation_results") or []:
        st = str(r.get("status") or "").lower()
        if st in ("pass", "skip", "skipped", ""):
            continue
        typ = str(r.get("subcircuit_type") or "unknown").upper()
        why = "; ".join(r.get("warnings") or []) or r.get("note") or r.get("error") or ""
        out.append({"code": "SPICE_" + typ, "rule": typ.lower(), "severity": "error" if st in ("fail", "error") else "warning",
                    "msg": f"{r.get('reference') or '/'.join(r.get('components') or [])} {st}: {why}".strip()[:300],
                    "refs": list(r.get("components") or []), "nets": [], "confidence": ""})
    return out


def level_of(code, cons):
    """FAIL when the constraints promote the code (exact, or a prefix ending in "_"), else WARN."""
    for p in (settings(cons) or {}).get("fail") or ():
        if code == p or (p.endswith("_") and code.startswith(p)):
            return FAIL
    return WARN


def as_findings(raw, cons):
    """Review-style findings {level, code, msg, refs} (extra keys: severity, nets, tool) from parsed rows. INFO rows are not findings."""
    return [{"level": level_of(r["code"], cons), "code": r["code"], "msg": f"[{r['severity']}] {r['msg']}", "refs": sorted(set(r["refs"])),
             "nets": r["nets"], "severity": r["severity"], "tool": "kicad-happy"} for r in raw if r["severity"] != "info"]


def _guard(cons, board, need_board, need_sim):
    """(settings, skills dir, schematic, None) or (None, None, None, UNKNOWN result): the preconditions every run shares."""
    cfg = settings(cons)
    if cfg is None:
        return None, None, None, _unknown("no [happy] table in the constraints")
    sk = skills_dir(cfg)
    if not sk:
        return None, None, None, _unknown("kicad-happy plugin not found (set [happy] plugin or $KICAD_HAPPY_SKILLS)")
    if need_board and not (board and os.path.isfile(board)):
        return None, None, None, _unknown("no board file")
    sch = find_schematic(board, cons)
    if not sch:
        return None, None, None, _unknown("schematic not found (set [happy] schematic)")
    if need_sim and not simulator():
        return None, None, None, _unknown("no SPICE simulator (ngspice, LTspice or Xyce) installed")
    return cfg, sk, sch, None


def _go(kind, board, cons, need_board, need_sim, script, extra, parse):
    cfg, sk, sch, bad = _guard(cons, board, need_board, need_sim)
    if bad:
        return bad
    key = (kind,) + _sig(sch, board if need_board else None)
    if key in _memo:
        return _memo[key]
    timeout = float(cfg.get("timeout_s", 300))
    work = tempfile.mkdtemp(prefix="fluxplace-happy-")
    try:
        sj, pj, err = _analyze(sk, sch, board if need_board else None, work, timeout)
        if err:
            return _unknown(err)
        out = os.path.join(work, kind + ".json")
        args = [os.path.join(sk, script[0], "scripts", script[1])] + extra(sj, pj, out)
        ok, err = _run(args, timeout)
        if not ok or not os.path.isfile(out):
            return _unknown(f"{script[1]} failed: {err}")
        res = {"status": OK, "reason": "", "findings": parse(_load(out)), "schematic": sch}
    except (OSError, ValueError) as e:
        return _unknown(f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    _memo[key] = res
    return res


def run_emc(board, cons):
    """EMC pre-compliance of the board + its schematic."""
    return _go("emc", board, cons, True, False, ("emc", "analyze_emc.py"),
               lambda sj, pj, out: ["-s", sj, "-p", pj, "-o", out], parse_emc)


def run_spice(board, cons):
    """SPICE subcircuit simulation of the schematic (the board only locates the schematic)."""
    return _go("spice", board, cons, False, True, ("spice", "simulate_subcircuits.py"),
               lambda sj, pj, out: [sj, "-o", out, "--compact"], parse_spice)


def check_emc(facts, cons):
    """Review findings of the EMC run: warn-only unless promoted; an UNKNOWN run is one EMC_UNKNOWN warning (never silence)."""
    if settings(cons) is None:
        return []
    bp = facts.get("board_path") or facts.get("board")
    r = run_emc(bp if isinstance(bp, str) else None, cons)
    if r["status"] != OK:
        return [{"level": WARN, "code": "EMC_UNKNOWN", "msg": f"EMC pre-compliance NOT CHECKED: {r['reason']}", "refs": []}]
    return as_findings(r["findings"], cons)
