"""`fluxplace doctor --context`: the measured agent-context footprint, with budgets.

Everything an agent pays for before and while using the tool: the MCP tools/list,
the server instructions, the two docs agents read, help-brief per command, and the
default output of every analysis command on a small bundled sample board
(fluxplace/sample/). A test fails when any budget is exceeded, so the tool cannot
silently grow back.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

BUDGET = {
    "mcp tools/list": 12000,
    "mcp instructions": 300,
    "AGENTS.md": 3072,
    "README.md": 6144,
    "command output": 3000,      # bytes, per analysis command, default output
    "command lines": 40,
    "help-brief lines": 15,
}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample")


def _capture(argv):
    import cli
    buf = io.StringIO()
    rc = 0
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        try:
            cli.main(argv)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return rc, buf.getvalue()


def _synthetic(cmd):
    """Network-bound commands: push a worst-case 400-line report through the same reporter."""
    from . import outputs as O
    if cmd == "sourcing":
        raw = "sourcing: 400 MPNs\n" + "\n".join(f"    NONE  PART{i:04d}  0 in stock   [U{i}]" for i in range(400))
    else:
        raw = "\n".join(f"  FAIL DATASHEET_MISSING   U{i}: no datasheet for PART{i}" for i in range(400))
        raw += "\ndatasheets: 0/400 on disk in ds"
    return O.compact_text(cmd, raw, 1, paths=["/p/b.%s.txt" % cmd, "/p/b.%s.json" % cmd])[0]


def measure(include_commands=True):
    """-> list of (name, value, budget, unit)."""
    rows = []
    from fluxplace import mcp_server as M
    srv = M.Server()
    tools = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    rows.append(("mcp tools/list (%d tools)" % len(tools),
                 len(json.dumps(tools, separators=(",", ":"))), BUDGET["mcp tools/list"], "B"))
    rows.append(("mcp instructions", len(M.INSTRUCTIONS), BUDGET["mcp instructions"], "B"))
    for name in ("AGENTS.md", "README.md"):
        p = os.path.join(ROOT, name)
        if os.path.exists(p):
            rows.append((name, os.path.getsize(p), BUDGET[name], "B"))
    from fluxplace import helpbrief as HB
    import cli
    ap = cli.build_parser()
    worst = max(((len(HB.brief(c, ap).splitlines()), c) for c in HB._subs(ap).choices))
    rows.append((f"help-brief max lines ({worst[1]})", worst[0], BUDGET["help-brief lines"], "lines"))
    if not include_commands:
        return rows
    tmp = tempfile.mkdtemp(prefix="fluxplace-ctx-")
    try:
        for f in os.listdir(SAMPLE):
            shutil.copy(os.path.join(SAMPLE, f), tmp)
        b = os.path.join(tmp, "sample.kicad_pcb")
        sp = os.path.join(tmp, "sample.spec.json")
        ct = os.path.join(tmp, "sample.constraints.toml")
        ds = os.path.join(tmp, "ds")
        os.makedirs(ds, exist_ok=True)
        runs = {
            "review": ["review", "--board", b, "--spec", sp, "--constraints", ct, "--no-api"],
            "route-ready": ["route-ready", b, "--constraints", ct, "--spec", sp],
            "route-diagnose": ["route-diagnose", b, "--constraints", ct, "--spec", sp],
            "clusters": ["clusters", b, "--constraints", ct, "--spec", sp],
            "anchor-check": ["anchor-check", b, "--constraints", ct, "--spec", sp],
            "bom-opt": ["bom-opt", "--spec", sp],
            "lint": ["lint", "--board", b],
            "verify-models": ["verify-models", "--board", b],
            "spec-check": ["spec-check", "--spec", sp, "--datasheets", ds],
            "drc-scope": ["drc-scope", "--board", b],
            "eval": ["eval", "--board", b],
        }
        from . import outputs as O
        for cmd in O.COMPACT_CMDS:
            if cmd in runs:
                rc, out = _capture(runs[cmd])
                tag = ""
            elif cmd in ("sourcing", "datasheets"):
                out, tag = _synthetic(cmd), " (synthetic 400-line report)"
            else:
                continue                                    # floorplan-check: not in this build
            rows.append((f"out {cmd}{tag}", len(out.encode()), BUDGET["command output"], "B"))
            rows.append((f"out {cmd} lines", out.count("\n") + 1, BUDGET["command lines"], "lines"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rows


def report(rows=None):
    """-> (text, ok)"""
    rows = rows if rows is not None else measure()
    out = ["fluxplace context footprint (budget in agent context; PASS = within budget)"]
    bad = 0
    for name, val, bud, unit in rows:
        ok = val <= bud
        bad += not ok
        out.append(f"  {name:46s} {val:>7d} / {bud:<6d} {unit:5s} {'PASS' if ok else 'FAIL'}")
    out.append(f"context: {'PASS' if not bad else 'FAIL'} ({len(rows) - bad}/{len(rows)} within budget)")
    return "\n".join(out), not bad
