"""`fluxplace help-brief [CMD]` - at most 15 lines per command, from the live parser.

Same source as the CLI and the MCP schemas (cli.build_parser), so it cannot drift.
The long per-command prose lives in docs/commands/<cmd>.md; agents should not need it.
"""
import argparse
import os

MAX_LINES = 15

# command -> doc that holds the long form (moved from the README, nothing deleted)
CMD_DOC = {
    "pipeline": "../AGENT-PIPELINE",
    "review": "review", "route-ready": "route-ready", "route-diagnose": "route-diagnose",
    "clusters": "clusters", "floorplan-check": "floorplan-check", "templates": "templates", "anchor-check": "anchor-check", "place": "place",
    "bom-opt": "bom-opt", "magnetics": "magnetics", "fit-sheet": "fit-sheet", "auto": "auto", "compact": "auto",
    "route-status": "route-wait", "route-wait": "route-wait", "drc-scope": "audit",
    "stackup": "audit", "stackup-define": "audit", "netlist": "audit",
    "mcp": "mcp", "doctor": "mcp", "analyze": "cli", "plan": "cli", "gather": "cli", "route": "route", "tournament": "cli",
    "comprehend": "cli", "lint": "cli", "eval": "cli", "fab": "cli",
    "parts": "parts", "ready": "ready", "pin-groups": "pin-groups", "part-diff": "part-diff",
    "map": "map", "plan-route": "plan-route", "plan-compare": "plan-compare", "plan-options": "plan-options",
}


# parsers created without help= (cli.py is not edited here): one-liners + common flags
FALLBACK_HELP = {
    "analyze": "print the communication map: hub, branches, forks, lint",
    "place": "re-place components (--strategy radial|flux|pack|quad|build|routable) and save the board",
    "eval": "placement quality: weighted wirelength, overlaps, board size, pin density",
}
FLAG_HELP = {"board": "board .kicad_pcb", "constraints": "constraints TOML", "spec": "spec JSON (pin names, parts)",
             "out": "output file or folder", "track": "track width mm", "clearance": "clearance mm"}


def _subs(ap):
    for a in ap._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a
    return None


def _first_sentence(s, n=100):
    if not s:
        return ""
    s = " ".join((s or "").split())
    for stop in ("; ", ". "):
        if stop in s:
            s = s.split(stop)[0]
    return s if len(s) <= n else s[: n - 1] + "~"


def index(ap=None):
    """[(cmd, one-line help)] for every command, in parser order."""
    if ap is None:
        import cli
        ap = cli.build_parser()
    sp = _subs(ap)
    helps = {ca.dest: ca.help for ca in sp._choices_actions}
    return [(c, _first_sentence(helps.get(c) or p.description or FALLBACK_HELP.get(c) or (
        (p.get_default("fn").__doc__ or "").strip().splitlines() or [""])[0], 84)) for c, p in sp.choices.items()]


def index_lines(ap=None):
    return [f"{c:18s} {h}" for c, h in index(ap)]


def readme_index(ap=None, width=44):
    """The README command index (kept in sync by a test)."""
    rows = []
    for c, h in index(ap):
        h = h if len(h) <= width else h[: width - 1].rsplit(" ", 1)[0].rstrip(",:;-") + "..."
        rows.append(f"{c:18s}{h}")
    return "\n".join(rows)


def brief(cmd, ap=None):
    if ap is None:
        import cli
        ap = cli.build_parser()
    sp = _subs(ap)
    p = sp.choices.get(cmd)
    if p is None:
        near = [c for c in sp.choices if cmd in c][:5]
        return f"help-brief: unknown command {cmd!r}" + (f"; did you mean {', '.join(near)}?" if near else "")
    helps = {ca.dest: ca.help for ca in sp._choices_actions}
    one = dict(index(ap)).get(cmd) or ""
    out = [f"fluxplace {cmd} - {_first_sentence(helps.get(cmd) or p.description or FALLBACK_HELP.get(cmd) or one, 140)}"]
    pos, req, opt = [], [], []
    for a in p._actions:
        if isinstance(a, argparse._HelpAction) or a.dest in ("fn", "cmd", "full_output", "raw", "quiet", "top"):
            continue
        if not a.option_strings:
            pos.append(a)
        elif a.required:
            req.append(a)
        else:
            opt.append(a)
    use = " ".join([cmd] + [f"<{a.dest}>" for a in pos] + [f"{a.option_strings[-1]} {a.dest.upper()}" for a in req])
    out.append("usage: fluxplace " + use)
    room = MAX_LINES - len(out) - 3          # reserve: flags overflow line, output line, doc line
    shown = 0
    for a in pos + req + opt:
        if shown >= room - 1 and (len(pos) + len(req) + len(opt)) > room:
            break
        name = a.option_strings[-1] if a.option_strings else a.dest
        h = _first_sentence(a.help or FLAG_HELP.get(a.dest, ""), 70)
        if a.choices and not h:
            h = "|".join(map(str, a.choices))
        d = f" [{a.default}]" if a.option_strings and a.default not in (None, False, [], argparse.SUPPRESS) and not a.required else ""
        out.append(f"  {name:20s} {h}{d}"[:110])
        shown += 1
    rest = [(a.option_strings[-1] if a.option_strings else a.dest) for a in pos + req + opt][shown:]
    if rest:
        out.append("  more: " + " ".join(rest)[:100] + " (fluxplace " + cmd + " -h)")
    from fluxplace import outputs as OUT
    if cmd in OUT.COMPACT_CMDS:
        out.append("output: compact (verdict, counts, worst N, report path); --full all, -q verdict, --top N")
    doc = CMD_DOC.get(cmd)
    if doc:
        out.append(f"long form: docs/commands/{doc}.md (only if this is not enough)")
    return "\n".join(l[:150] for l in out[:MAX_LINES])


def main_text(cmd=None):
    if cmd:
        return brief(cmd)
    return "\n".join(index_lines())
