"""fix-c #16: the auto/compact fab paths pass the loaded constraints to fab.emit."""
import ast
import pathlib


def test_fab_emit_calls_pass_cons():
    src = pathlib.Path(__file__).resolve().parents[1].joinpath("cli.py").read_text()
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "emit"
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "fab"]
    assert calls
    for c in calls:
        assert any(k.arg == "cons" for k in c.keywords), f"fab.emit at line {c.lineno} gets no cons"
