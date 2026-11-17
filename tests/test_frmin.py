"""Freerouting older than 2.5.0 is refused where the 2.5.0 flags would be used, and reported by deps."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import deps, routefr as F  # noqa: E402

S = {"threads": 2}


def test_command_refuses_old_jar_naming_version():
    with pytest.raises(RuntimeError, match=r"Freerouting 2\.4\.9 at /x/freerouting-2\.4\.9\.jar is too old: 2\.5\.0 or newer"):
        F.command({"jar": "/x/freerouting-2.4.9.jar", "java": "java"}, "a.dsn", "a.ses", dict(F.settings({}), threads=2), 60, "/tmp", False)


def test_deps_reports_old_jar_not_ok(tmp_path, monkeypatch):
    jar = tmp_path / "freerouting-2.3.0.jar"
    jar.write_text("x")
    monkeypatch.setenv("FLUXPLACE_FREEROUTING_JAR", str(jar))
    r = deps.check_one(next(q for q in deps.REQUIREMENTS if q["key"] == "freerouting"))
    assert r["ok"] is False and "2.3.0" in r["detail"] and "2.5.0" in r["detail"]
    new = tmp_path / "freerouting-2.5.0.jar"
    new.write_text("x")
    monkeypatch.setenv("FLUXPLACE_FREEROUTING_JAR", str(new))
    assert deps.check_one(next(q for q in deps.REQUIREMENTS if q["key"] == "freerouting"))["ok"] is True
