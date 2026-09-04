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
    # ---- read: analysis, no board mutation --------------------------------
    "doctor":            ("read",  "Preflight the whole toolchain: KiCad, python packages, "
                                   "router, distributor credentials and the wider suite. "
                                   "Run this first when anything fails unexpectedly."),
    "analyze":           ("read",  "Analyze a board's communication graph: parts, nets, "
                                   "hub and fork topology."),
    "gather":            ("read",  "Dump structured board facts as JSON — parts, nets, "
                                   "pads, geometry."),
    "plan":              ("read",  "Gather board facts and write a detailed placement plan "
                                   "in markdown."),
    "lint":              ("read",  "Design-completeness rules: unwired power/IO, dead-end "
                                   "nets, connector retention advice for high-vibration use."),
    "comprehend":        ("read",  "Auto-detect physics constraints from the netlist — power "
                                   "nets, differential pairs, bypass caps, crystals, "
                                   "converters — and optionally grade the placement against "
                                   "them."),
    "comprehend-intent": ("read",  "Infer electrical intent as TOML: pairs, bypass, crystals, "
                                   "power classes."),
    "eval":              ("read",  "Score the current placement, optionally against the "
                                   "physics rule checks."),
    "route":             ("read",  "Global-route the current placement and report congestion "
                                   "hotspots without modifying the board."),
    "sourcing":          ("read",  "Grade every MPN against live DigiKey and Mouser stock. "
                                   "This is the availability gate: a part nobody stocks is a "
                                   "design bug, not a purchasing problem."),
    "preflight":         ("read",  "Check a board is ready for the next pipeline stage."),
    "drc-scope":         ("read",  "Report what a DRC result actually examined: which "
                                   "checks are switched off in the project, which of those "
                                   "matter for fabrication, and (with full=true) what a "
                                   "re-run with every check enabled actually finds. A clean "
                                   "DRC report is a claim about scope — this is how you "
                                   "learn the scope."),
    "netlist":           ("read",  "Read the connection list back out of the routed board: "
                                   "every net with its pads, every component, pin by pin. "
                                   "For a board generated from a netlist spec there is no "
                                   ".kicad_sch at all, and this is the only connectivity "
                                   "document that exists."),
    "stackup":           ("read",  "Report the layer stack, which layers carry plane pours, "
                                   "the netclass track/diff-pair geometry, and whether "
                                   "controlled impedance can be verified from these files "
                                   "at all — it cannot if no dielectric is defined."),
    "stackup-define":    ("write", "Define the board stackup from a named fab profile "
                                   "(JLCPCB/PCBWay 4-layer 1.6mm and others), solve the "
                                   "trace geometry it implies for 50 ohm single-ended and "
                                   "100 ohm differential, and grade both the netclasses AND "
                                   "the actual routed copper against those targets. With "
                                   "apply=true it writes the stackup into the .kicad_pcb "
                                   "(backed up first, refuses to overwrite an existing one). "
                                   "Without a stackup, no impedance claim on a board can be "
                                   "checked at all."),
    "schematic":         ("write", "Generate a real .kicad_sch from the netlist spec "
                                   "(or from the routed copper) and VERIFY it by exporting "
                                   "its netlist and diffing against the source. For a board "
                                   "built from a spec rather than drawn, this is the "
                                   "reviewable electrical document that otherwise does not "
                                   "exist. Net-label style, not hand-drawn signal flow."),
    "verify-models":     ("read",  "Verify 3D models sit on their footprint pins — displaced "
                                   "models look like mechanical truth and are a review hazard."),
    "datasheets":        ("write", "Fetch every MPN's datasheet into the project through the "
                                   "DigiKey/Mouser APIs, hash it into datasheets.json, and "
                                   "list the ones that need a browser. Documentation is a "
                                   "build requirement: review fails a part with no datasheet "
                                   "on disk."),
    "spec-check":        ("read",  "Documentation gate on a netlist spec: every part has a "
                                   "manufacturer part number, its datasheet on disk, and a "
                                   "pinmap whose names appear on the cited datasheet page. "
                                   "schematic refuses an undocumented spec."),
    "review":            ("read",  "The design-review gate — what an outside reviewer checks "
                                   "that DRC, ERC and lint do not: spec net rules (straight-"
                                   "copper nets), diff-pair skew and layer use, RF impedance "
                                   "graded on the layer the copper is actually on, distributor "
                                   "package / pin count / operating temperature vs the "
                                   "footprint and the [env] profile, spec pinmap vs the KiCad "
                                   "official symbol, spec/board size and layer sync, hold-up "
                                   "and TVS margin. fab and deliver run it and abort on FAIL."),

    # ---- write: produce files, fast ---------------------------------------
    "fab":               ("write", "Emit the manufacturing package: gerbers, drill, centroid, "
                                   "DRC report and manifest."),
    "deliver":           ("write", "Package a fab directory for upload: CAM-only zip plus the "
                                   "loose documents whoever places the order actually reads."),
    "pcbway":            ("write", "Generate the PCBWay order worksheet and assembly "
                                   "instructions from the board, so the order form is not "
                                   "answered from memory."),
    "repair":            ("write", "Copper repairs the review gate asks for: remove router "
                                   "loops/stubs on differential pairs, re-width RF segments to "
                                   "what their layer needs, remap pads to corrected nets (rips "
                                   "old stubs, drops GND vias), net twin pads, add silkscreen "
                                   "text. Optionally runs the patcher for unrouted pads."),
    "finish":            ("long",  "Route named nets with freerouting and take back only "
                                   "their copper (planes declared as power in the DSN); kept "
                                   "only if DRC does not worsen and the unconnected count "
                                   "falls. For the connection the grid patcher cannot close."),
    "drc-fix":           ("write", "Fix the DRC noise a repair leaves behind: rip tracks "
                                   "that short a swapped footprint's pads, neck widened RF "
                                   "segments at clearance pinches, snap or delete stray track "
                                   "ends, move colliding reference text; loops DRC until the "
                                   "count stops falling."),
    "tune":              ("long",  "DRC-guarded differential-pair length tuning: bridge "
                                   "hairpins on the long side, add serpentine meanders on "
                                   "the short side, accept each step only when kicad-cli "
                                   "DRC does not get worse, until every pair is inside its "
                                   "[pairs.*] skew_mm limit."),
    "models":            ("write", "Fetch real manufacturer STEP models via distributor APIs "
                                   "and wire them to footprints."),
    "intake":            ("write", "Design interview: capture design intent to JSON."),

    # ---- long: minute-scale pipelines, board-mutating ----------------------
    "auto":              ("long",  "Full pipeline: place, outline, planes, route, DFM, fab "
                                   "package. Minutes."),
    "compact":           ("long",  "Shrink a known-good placement and re-run the downstream "
                                   "stages. Minutes."),
    "tournament":        ("long",  "Run competing placement candidates and rank them by a "
                                   "real router. Many minutes."),
    "place":             ("long",  "Re-place the board by signal flow. Rewrites positions."),
    "patch":             ("long",  "Regional rip-up and reroute for walled copper islands."),
    "launder":           ("long",  "DRC-driven copper launder pass."),
    "calibrate":         ("long",  "Calibrate placement scoring against router ground truth."),
    "sync-nets":         ("long",  "Synchronise nets onto the board."),
    "replace-footprint": ("long",  "Swap a footprint across the board."),
}

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


def schema_for(parser):
    """JSON Schema for one subparser's arguments."""
    import argparse as _ap
    props, required = {}, []
    for act in parser._actions:
        if isinstance(act, _ap._HelpAction) or act.dest in ("help", "fn", "cmd"):
            continue
        if act.dest == argparse.SUPPRESS:
            continue
        frag = _json_type(act)
        if act.help:
            frag["description"] = act.help
        if act.default is not None and not isinstance(act.default, bool) \
           and act.default != argparse.SUPPRESS:
            frag["default"] = act.default
        props[act.dest] = frag
        if act.required:
            required.append(act.dest)
    out = {"type": "object", "properties": props}
    if required:
        out["required"] = required
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
    """The MCP tool list, derived from the live CLI parser."""
    import cli
    ap = cli.build_parser()
    subs = _subparsers(ap)
    tools = []
    for cmd, parser in sorted(subs.items()):
        pol = POLICY.get(cmd)
        if not pol:
            continue
        kind, desc = pol
        if kind == "long" and not expose_long:
            continue
        note = ""
        if kind == "write":
            note = "\n\nWrites files to disk."
        elif kind == "long":
            note = ("\n\nLONG-RUNNING and board-mutating: this can take many minutes "
                    "and rewrites the .kicad_pcb. Prefer the CLI for these.")
        tools.append({
            "name": tool_name(cmd),
            "description": desc + note,
            "inputSchema": schema_for(parser),
            "_kind": kind,
            "_cmd": cmd,
        })
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
    by_dest = {}
    for act in parser._actions:
        if act.option_strings:
            by_dest[act.dest] = act

    argv = [cmd]
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
            "instructions": (
                "fluxplace drives KiCad 10 for PCB placement, routing analysis, "
                "design linting, live part sourcing and manufacturer packaging. "
                "Call fluxplace_doctor first if anything behaves unexpectedly — "
                "most failures are a missing dependency, and it names them."),
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
        self.out.write(json.dumps(obj) + "\n")
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
