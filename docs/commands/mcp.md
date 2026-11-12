# MCP server

## MCP server

fluxplace exposes its commands as Model Context Protocol tools, so an agent can
analyse boards, audit DRC scope, check part availability and build fab packages
directly.

```
python3 -m fluxplace.mcp_server            # 20 tools (read + write)
python3 -m fluxplace.mcp_server --all      # + the long-running pipelines
python3 -m fluxplace.mcp_server --list     # show the tools and exit
```

Register it alongside your other KiCad servers:

```json
{
  "mcpServers": {
    "fluxplace": {
      "command": "/home/you/.fluxplace-venv/bin/python",
      "args": ["-m", "fluxplace.mcp_server"],
      "env": { "PYTHONPATH": "/path/to/kicad-fluxplace" }
    }
  }
}
```

Use the interpreter that can import `pcbnew` — the venv from the section above,
or your system python. The server has **no third-party dependencies**: it speaks
JSON-RPC 2.0 over stdio directly, so it runs anywhere the CLI runs, including on
a PEP 668 distro python where installing an SDK is the exact friction `doctor`
exists to remove.

**Tool schemas are derived from the CLI parser**, not hand-written. Add a flag to
a subcommand and the MCP tool gains it on the next start; there is no second
description of the commands to drift.

Commands are classified by what they cost you. `read` (analysis, never touches
the board) and `write` (produces files) are exposed by default. `long` — `auto`,
`tournament`, `compact`, `place`, `patch`, `launder` — are minute-scale pipelines
that rewrite the board, which is the wrong shape for a synchronous tool call, so
they are behind `--all`.

### Token budget

Tool output lands in the caller's context and stays there for the rest of the
conversation, so results are capped (default 6000 chars, `FLUXPLACE_MCP_MAX_CHARS`
to change). Past the cap the full text is written to a temp file and the reply
carries the head, the tail and the path — the two ends of a report are where the
summary and the verdict live, and the middle is the enumeration you can grep.
Truncation is always stated; a silently-shortened result would be the same
failure as a DRC report that does not say what it skipped.

`fluxplace_netlist` on a 143-part board went from ~5,400 tokens to ~925 capped,
or ~236 with `summary=true`, which returns counts, the largest nets and any
single-pad nets — the shape plus the outliers, which is what the full dump was
usually being read for.
