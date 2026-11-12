"""Docs that cost little: size budgets, command index in sync, links, help-brief limits."""
import os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cli
from fluxplace import helpbrief as HB

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*p):
    return open(os.path.join(ROOT, *p)).read()


def test_size_budgets():
    assert len(_read("README.md").encode()) <= 6144
    assert len(_read("AGENTS.md").encode()) <= 3072


def test_readme_index_in_sync_and_complete():
    r = _read("README.md")
    block = r.split("<!-- index:start -->\n")[1].split("\n<!-- index:end -->")[0]
    assert block == HB.readme_index(), "regenerate: README index block != helpbrief.readme_index()"
    cmds = [l.split()[0] for l in block.splitlines()]
    sp = HB._subs(cli.build_parser()).choices
    assert sorted(cmds) == sorted(sp)


def test_relative_links_resolve():
    files = ["README.md", "AGENTS.md"]
    for d in ("docs", "docs/commands"):
        files += [os.path.join(d, f) for f in os.listdir(os.path.join(ROOT, d)) if f.endswith(".md")]
    bad = []
    for f in files:
        base = os.path.dirname(os.path.join(ROOT, f))
        for m in re.finditer(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", _read(f)):
            t = m.group(1)
            if t.startswith(("http", "mailto:")):
                continue
            if not os.path.exists(os.path.normpath(os.path.join(base, t))):
                bad.append((f, t))
    assert not bad, bad


def test_help_brief_at_most_15_lines_every_command():
    ap = cli.build_parser()
    for c in HB._subs(ap).choices:
        b = HB.brief(c, ap)
        assert 2 <= len(b.splitlines()) <= 15, c
        assert all(len(l) <= 160 for l in b.splitlines()), c


def test_help_brief_cli(capsys):
    cli.main(["help-brief", "route-ready"])
    out = capsys.readouterr().out
    assert out.startswith("fluxplace route-ready") and "--waive" in out
    cli.main(["help-brief", "nope"])
    assert "unknown command" in capsys.readouterr().out


def test_agents_md_names_every_agent_command():
    a = _read("AGENTS.md")
    for c in ("route-ready", "anchor-check", "clusters", "route-diagnose", "route-wait", "route-status",
              "pipeline", "help-brief", "doctor --context", "docs/AGENT-PIPELINE.md"):
        assert c in a, c
