"""fluxplace MCP server — the CLI, exposed as Model Context Protocol tools.

Speaks JSON-RPC 2.0 over stdio with NO third-party dependencies. That is a
deliberate constraint, not laziness: this server has to run on whichever
interpreter can import pcbnew, and on Linux that is a PEP 668 externally-managed
distro python where installing an SDK is exactly the friction `fluxplace doctor`
exists to remove. The protocol surface we need is small enough to implement
directly, so the server works anywhere the CLI works.

Tool schemas are DERIVED from cli.build_parser() rather than written out here.
There is one source of truth for what fluxplace can do: add a flag to the CLI
and the MCP tool gains it on the next start. A hand-maintained tool list would
drift from the CLI within a week.

Commands are classified by what they cost the caller:

  read   analysis only — reports, never writes a board. Always exposed.
  write  produces files (fab packages, documents). Always exposed.
  long   minutes-long pipelines that shell out to a router and rewrite boards.
         NOT exposed by default: a synchronous MCP call is the wrong shape for
         a job that can run for ten minutes. Pass --all or set
         FLUXPLACE_MCP_ALL=1 when you want them anyway.

Run it:
    python3 -m fluxplace.mcp_server
    python3 -m fluxplace.mcp_server --all
"""

import argparse
import contextlib
import io
import json
import os
import sys
import traceback

PROTOCOL_VERSION = "2025-06-18"
INSTRUCTIONS = ("fluxplace: KiCad placement, route gating, review, fab. Output is compact; the full report "
                "path is printed. Flags: fluxplace_help_brief. Others: fluxplace_run. Never poll "
                "logs: fluxplace_route_wait. Failures: fluxplace_doctor.")
SERVER_NAME = "fluxplace"

# --------------------------------------------------------------------------
# Output budget.
#
# Every byte a tool prints lands in the caller's context window and stays there
# for the rest of the conversation. `fluxplace netlist` on a 143-part board is
# 21 KB — roughly 5,400 tokens — for one call, and an agent debugging a board
# will call several tools several times. Unbounded output is not a formatting
# problem, it is a budget the caller never agreed to spend.
#
# So a tool result is capped. Past the cap the full text goes to a file and the
# caller gets the head, the tail, and the path — because the two ends of a
# report are almost always where the answer is (the summary line and the
# verdict), and the middle is the enumeration they can grep if they need it.
#
# Truncation is always ANNOUNCED. Silently returning a prefix would be the same
# failure as a DRC report that does not say what it skipped.
# --------------------------------------------------------------------------

MAX_RESULT_CHARS = int(os.environ.get("FLUXPLACE_MCP_MAX_CHARS", "6000"))
HEAD_LINES = 40
TAIL_LINES = 25


def budget(text, tool="result", max_chars=None):
    """Cap a tool result, spilling the remainder to a file. -> (text, spilled)"""
    cap = MAX_RESULT_CHARS if max_chars is None else max_chars
    if cap <= 0 or len(text) <= cap:
        return text, None
    import tempfile
    fd, path = tempfile.mkstemp(prefix="fluxplace-%s-" % tool.replace("/", "_"),
                                suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    lines = text.splitlines()
    if len(lines) <= HEAD_LINES + TAIL_LINES:
        head, tail, hidden = lines, [], 0
    else:
        head = lines[:HEAD_LINES]
        tail = lines[-TAIL_LINES:]
        hidden = len(lines) - HEAD_LINES - TAIL_LINES
    out = list(head)
    out.append("")
    out.append("... %d of %d lines omitted (%d chars total). Full output:"
               % (hidden, len(lines), len(text)))
    out.append("    %s" % path)
    out.append("Read or grep that file for the omitted section rather than "
               "re-running with a wider net.")
    out.append("")
    out.extend(tail)
    return "\n".join(out), path

# --------------------------------------------------------------------------
# Which commands become tools, and what they cost.
# Anything in the CLI but absent here is not exposed at all — a new subcommand
# is opt-in, so an experimental command cannot silently become a tool.
# --------------------------------------------------------------------------

POLICY = {
    # cmd: (kind, one-sentence description <= 160 chars). Long guidance: docs/commands/mcp.md
    # and `fluxplace help-brief CMD`.
    # ---- read ---------------------------------------------------------------
    "doctor":            ("read",  "Preflight KiCad, python deps, router and API keys; run first when anything fails. "
                                   "context=true measures the agent-context footprint."),
    "help-brief":        ("read",  "At most 15 lines on one command (flags, output); no command: one line per command."),
    "analyze":           ("read",  "Board communication graph: parts, nets, hub and fork topology."),
    "gather":            ("read",  "Dump structured board facts as JSON."),
    "plan":              ("read",  "Write a markdown placement plan from board facts."),
    "lint":              ("read",  "Design-completeness lint: unwired power/IO, dead-end nets, connector advice."),
    "comprehend":        ("read",  "Detect physics constraints (power nets, pairs, bypass, crystals); grade placement."),
    "comprehend-intent": ("read",  "Infer electrical intent as TOML."),
    "eval":              ("read",  "Score the placement (wirelength, overlaps, pin density); prc adds rule checks."),
    "route":             ("read",  "Global-route the placement and report congestion; board unchanged."),
    "sourcing":          ("read",  "Grade every MPN against live DigiKey/Mouser stock (availability gate)."),
    "preflight":         ("read",  "Check a board is ready for the next pipeline stage."),
    "drc-scope":         ("read",  "What a DRC run actually examined: ignored checks; full=true re-runs with all enabled."),
    "netlist":           ("read",  "Connection list read back from the board (every net with its pads)."),
    "stackup":           ("read",  "Layer stack, plane layers, netclass geometry, impedance verifiability."),
    "verify-models":     ("read",  "Check 3D models sit on their footprint pins."),
    "spec-check":        ("read",  "Documentation gate on a spec: MPN, datasheet on disk, pinmap on cited page."),
    "review":            ("read",  "Design-review gate: net rules, pair skew, RF impedance, part data, pinmap, sync. "
                                   "Compact: verdict, counts, worst N, report path."),
    "bom-opt":           ("read",  "Part-count report: array, value-merge, package and parallel candidates."),
    "route-ready":       ("read",  "Pre-route gate scorecard (net budgets, support parts, anchors, congestion); exit 1 on FAIL."),
    "route-diagnose":    ("read",  "Why nets stayed unrouted and which parts to move; read-only."),
    "clusters":          ("write", "IC support clusters; apply=true re-seats far support parts to out (input kept)."),
    "anchor-check":      ("write", "Per-anchor rotation table; apply=true rotates misoriented ones to out."),
    "route-status":      ("read",  "One line from a router run's status file. Never tail router logs."),
    "route-wait":        ("read",  "Block until the router run is done/stalled/checkpointed; one line plus result and "
                                   "diagnosis path. Keep timeout <= 300 here."),
    "pipeline":          ("write", "Staged pipeline with W/state.json handoff; run one stage with from/to; status=true "
                                   "prints the table. Stages may take minutes."),
    # ---- write --------------------------------------------------------------
    "stackup-define":    ("write", "Define the stackup from a fab profile, solve 50/100 ohm geometry; apply=true writes it."),
    "schematic":         ("write", "Generate a .kicad_sch from a spec or board and verify it against the netlist."),
    "datasheets":        ("write", "Fetch every MPN's datasheet via DigiKey/Mouser into the project."),
    "fab":               ("write", "Manufacturing package: gerbers, drill, centroid, DRC report (runs the review gate)."),
    "deliver":           ("write", "Package a fab directory for upload: CAM zip plus loose documents."),
    "pcbway":            ("write", "PCBWay order worksheet and assembly instructions from the board."),
    "repair":            ("write", "Copper repairs the review gate asks for: pair loops, RF widths, pin remap, text."),
    "finish":            ("long",  "Route named nets with freerouting; kept only if DRC does not worsen."),
    "drc-fix":           ("write", "Fix DRC noise a repair leaves behind; loops until the count stops falling."),
    "tune":              ("long",  "DRC-guarded differential-pair length tuning to the [pairs.*] skew limits."),
    "models":            ("write", "Fetch real vendor STEP models via distributor APIs and wire them to footprints."),
    "intake":            ("write", "Design interview: capture design intent to JSON."),
    # ---- long: minutes, board-mutating ---------------------------------------
    "auto":              ("long",  "Full pipeline: place, route, DFM, fab package. Minutes."),
    "compact":           ("long",  "Shrink a known-good placement and re-run downstream stages. Minutes."),
    "tournament":        ("long",  "Run competing placements and rank them by a real router. Many minutes."),
    "place":             ("long",  "Re-place the board (strategy routable: connection-driven). Rewrites positions."),
    "patch":             ("long",  "Regional rip-up and reroute for walled copper islands."),
    "launder":           ("long",  "DRC-driven copper launder pass."),
    "calibrate":         ("long",  "Calibrate placement scoring against router ground truth."),
    "sync-nets":         ("long",  "Synchronise nets onto the board."),
    "replace-footprint": ("long",  "Swap a footprint across the board."),
}

# Own tool (schema) in tools/list: cmd -> the parameters shown. Everything else in POLICY is reached
# through fluxplace_run (listed in its description). Unlisted flags still work when passed.
LISTED = {
    "doctor": ["problems_only", "context"],
    "help-brief": ["command"],
    "review": ["board", "spec", "constraints", "no_api", "waive", "fail_on", "top", "full_output"],
    "route-ready": ["board", "constraints", "spec", "waive", "all", "top", "full_output"],
    "route-diagnose": ["board", "nets", "log", "constraints", "spec", "top"],
    "clusters": ["board", "constraints", "spec", "apply", "out"],
    "anchor-check": ["board", "constraints", "spec", "apply", "out"],
    "route-status": ["target"],
    "route-wait": ["target", "timeout", "until"],
    "pipeline": ["board", "workdir", "constraints", "spec", "from_stage", "to_stage", "status", "route_budget"],
    "lint": ["board", "waive", "fail_on"],
    "eval": ["board", "prc"],
    "sourcing": ["board", "show_ok"],
    "spec-check": ["spec", "datasheets", "mpn_map"],
    "bom-opt": ["spec", "constraints"],
    "drc-scope": ["board", "report", "full"],
    "fab": ["board", "out", "spec", "constraints", "no_api"],
    "deliver": ["fab_dir", "out", "name"],
}
RUN_TOOL = "fluxplace_run"

ALWAYS = ("read", "write")


# --------------------------------------------------------------------------
# argparse -> JSON Schema
# --------------------------------------------------------------------------

def _json_type(action):
    """Map an argparse action to a JSON Schema type fragment."""
    import argparse as _ap
    if isinstance(action, (_ap._StoreTrueAction, _ap._StoreFalseAction)):
        return {"type": "boolean"}
    t = action.type
    if t is int:
        return {"type": "integer"}
    if t is float:
        return {"type": "number"}
    if action.nargs in ("*", "+") or isinstance(action, _ap._AppendAction):
        return {"type": "array", "items": {"type": "string"}}
    if action.choices:
        return {"type": "string", "enum": [str(c) for c in action.choices]}
    return {"type": "string"}


PARAM_DESC_MAX = 80


def _short(text, n=PARAM_DESC_MAX):
    text = " ".join((text or "").split())
    for stop in ("; ", ". ", " (", " - "):
        if stop in text:
            text = text.split(stop)[0]
    return text if len(text) <= n else text[: n - 1].rstrip() + "~"


def schema_for(parser, keep=None):
    """JSON Schema for one subparser's arguments (descriptions <= 80 chars).

    ``keep`` limits the listed parameters (flags outside it still work when a
    caller passes them: _argv_for reads the real parser, not this schema)."""
    import argparse as _ap
    props, required = {}, []
    for act in parser._actions:
        if isinstance(act, _ap._HelpAction) or act.dest in ("help", "fn", "cmd"):
            continue
        if act.dest == argparse.SUPPRESS:
            continue
        if keep is not None and act.dest not in keep:
            continue
        frag = _json_type(act)
        d = _short(act.help)
        if d:
            frag["description"] = d
        if isinstance(act.default, (int, float)) and not isinstance(act.default, bool) \
           and act.default is not None:
            frag["default"] = act.default
        props[act.dest] = frag
        if act.required or (not act.option_strings and act.nargs not in ("?", "*")):
            required.append(act.dest)
    if keep is not None:
        props = {k: props[k] for k in keep if k in props}
    out = {"type": "object", "properties": props}
    if required:
        out["required"] = [r for r in required if r in props]
    return out


def _subparsers(ap):
    import argparse as _ap
    for a in ap._actions:
        if isinstance(a, _ap._SubParsersAction):
            return a.choices
    return {}


def tool_name(cmd):
    return "fluxplace_" + cmd.replace("-", "_")


def cmd_from_tool(name):
    if not name.startswith("fluxplace_"):
        return None
    return name[len("fluxplace_"):].replace("_", "-")


def build_tools(expose_long=False):
    """The MCP tool list, derived from the live CLI parser: curated commands get their own
    (short) schema, everything else is reachable through fluxplace_run."""
    import cli
    ap = cli.build_parser()
    subs = _subparsers(ap)
    tools, via_run = [], []
    for cmd, parser in subs.items():
        pol = POLICY.get(cmd)
        if not pol:
            continue
        kind, desc = pol
        if kind == "long" and not expose_long:
            continue
        keep = LISTED.get(cmd)
        if keep is None and kind != "long":
            via_run.append(cmd)
            continue
        note = " LONG-RUNNING: minutes, rewrites the board." if kind == "long" else ""
        tools.append({"name": tool_name(cmd), "description": desc + note,
                      "inputSchema": schema_for(parser, keep), "_kind": kind, "_cmd": cmd})
    order = list(LISTED)
    tools.sort(key=lambda t: (order.index(t["_cmd"]) if t["_cmd"] in LISTED else 99, t["name"]))
    if via_run:
        tools.append({
            "name": RUN_TOOL,
            "description": "Run another fluxplace command by name: " + ", ".join(sorted(via_run))
                           + ". Flags: help_brief.",
            "inputSchema": {"type": "object", "required": ["command"], "properties": {
                "command": {"type": "string"},
                "args": {"type": "object", "description": "flag name (underscores) -> value; board/spec are flags too"}}},
            "_kind": "write", "_cmd": "run"})
    return tools


# --------------------------------------------------------------------------
# invocation
# --------------------------------------------------------------------------

def _argv_for(cmd, args, parser):
    """Turn a JSON arg object back into the argv the CLI expects.

    Going through the real parser (rather than calling the command function
    directly) means MCP callers get identical validation, defaults and dispatch
    to CLI users. One code path, one behaviour.
    """
    import argparse as _ap
    by_dest, positionals = {}, []
    for act in parser._actions:
        if act.option_strings:
            by_dest[act.dest] = act
        elif not isinstance(act, _ap._HelpAction) and act.dest not in ("fn", "cmd", "help"):
            positionals.append(act)

    argv = [cmd]
    for act in positionals:                    # positional parameters (route-ready BOARD ...)
        val = (args or {}).get(act.dest)
        if val is None:
            continue
        argv += [str(v) for v in val] if isinstance(val, list) else [str(val)]
    for key, val in (args or {}).items():
        act = by_dest.get(key)
        if act is None or val is None:
            continue
        flag = act.option_strings[-1]
        if isinstance(act, (_ap._StoreTrueAction, _ap._StoreFalseAction)):
            if val:
                argv.append(flag)
        elif isinstance(val, list):
            for v in val:
                argv += [flag, str(v)]
        else:
            argv += [flag, str(val)]
    return argv


def call_tool(name, args, expose_long=False):
    """Run one tool. Returns (text, is_error)."""
    if name == RUN_TOOL:
        cmd = (args or {}).get("command", "")
        args = (args or {}).get("args") or {}
        if not isinstance(args, dict):
            return ("args must be an object of flag -> value", True)
    else:
        cmd = cmd_from_tool(name)
    if not cmd:
        return ("unknown tool: %s" % name, True)
    pol = POLICY.get(cmd)
    if not pol:
        return ("command %r is not exposed over MCP" % cmd, True)
    if pol[0] == "long" and not expose_long:
        return ("%r is a long-running board-mutating pipeline and is not exposed "
                "by default. Start the server with --all (or FLUXPLACE_MCP_ALL=1) "
                "to enable it, or run it from the CLI." % cmd, True)

    import cli
    ap = cli.build_parser()
    parser = _subparsers(ap).get(cmd)
    if parser is None:
        return ("command %r not found in the CLI" % cmd, True)

    argv = _argv_for(cmd, args, parser)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            cli.main(argv)
    except SystemExit as e:
        # argparse errors and deliberate exits both land here
        code = e.code if isinstance(e.code, int) else 0
        text = buf.getvalue()
        if code not in (0, None):
            return (text + "\n(exit status %s)" % code, True)
        return (text, False)
    except Exception:
        return (buf.getvalue() + "\n" + traceback.format_exc(), True)
    return (buf.getvalue() or "(no output)", False)


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------

class Server:
    def __init__(self, expose_long=False, out=None, err=None):
        self.expose_long = expose_long
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self._tools = None

    def tools(self):
        if self._tools is None:
            self._tools = build_tools(self.expose_long)
        return self._tools

    # ---- protocol methods ------------------------------------------------

    def on_initialize(self, params):
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": self._version()},
            "instructions": INSTRUCTIONS,
        }

    def _version(self):
        try:
            import fluxplace
            return getattr(fluxplace, "__version__", "0")
        except Exception:
            return "0"

    def on_tools_list(self, params):
        return {"tools": [{k: v for k, v in t.items() if not k.startswith("_")}
                          for t in self.tools()]}

    def on_tools_call(self, params):
        name = (params or {}).get("name", "")
        args = (params or {}).get("arguments", {})
        text, is_err = call_tool(name, args, self.expose_long)
        text, spilled = budget(text, tool=name)
        res = {"content": [{"type": "text", "text": text}],
               "isError": bool(is_err)}
        if spilled:
            res["_meta"] = {"fullOutput": spilled}
        return res

    HANDLERS = {
        "initialize": on_initialize,
        "tools/list": on_tools_list,
        "tools/call": on_tools_call,
        "ping": lambda self, p: {},
    }

    # ---- loop ------------------------------------------------------------

    def handle(self, msg):
        """Handle one decoded JSON-RPC message. Returns a response dict or None
        (for notifications, which must not be answered)."""
        mid = msg.get("id")
        method = msg.get("method", "")
        if mid is None:
            return None                       # notification
        fn = self.HANDLERS.get(method)
        if fn is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": "method not found: %s" % method}}
        try:
            return {"jsonrpc": "2.0", "id": mid, "result": fn(self, msg.get("params"))}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": str(e),
                              "data": traceback.format_exc()}}

    def serve(self, stdin=None):
        stdin = stdin or sys.stdin
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._write({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "parse error"}})
                continue
            resp = self.handle(msg)
            if resp is not None:
                self._write(resp)

    def _write(self, obj):
        self.out.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self.out.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="fluxplace-mcp",
        description="fluxplace MCP server (JSON-RPC 2.0 over stdio)")
    ap.add_argument("--all", action="store_true",
                    help="also expose the long-running board-mutating pipelines")
    ap.add_argument("--list", action="store_true",
                    help="print the tool list and exit (does not start the server)")
    a = ap.parse_args(argv)

    expose_long = a.all or os.environ.get("FLUXPLACE_MCP_ALL") == "1"

    if a.list:
        for t in build_tools(expose_long):
            print("%-34s %-6s %s" % (t["name"], t["_kind"],
                                     t["description"].split("\n")[0]))
        return

    # A dependency problem must not look like a broken server.
    try:
        from fluxplace import deps
        if deps.blocking():
            sys.stderr.write(
                "fluxplace MCP: cannot start — core requirements missing.\n\n"
                + deps.report(show_ok=False) + "\n")
            sys.exit(1)
    except ImportError:
        pass

    Server(expose_long=expose_long).serve()


if __name__ == "__main__":
    main()
