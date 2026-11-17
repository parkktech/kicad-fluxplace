import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import deps as D  # noqa: E402


def test_doctor_selects_newest_freerouting_and_honors_explicit_jar(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    for version in ("2.3.0", "2.5.0", "2.4.1"):
        (tools / f"freerouting-{version}.jar").write_text("jar")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(D, "FREEROUTING_PATHS", (str(tools / "freerouting-*.jar"),))
    monkeypatch.delenv("FREEROUTING_JAR", raising=False)
    monkeypatch.delenv("FLUXPLACE_FREEROUTING_JAR", raising=False)
    assert D.find_freerouting().endswith("freerouting-2.5.0.jar")
    explicit = tools / "freerouting-2.4.1.jar"
    monkeypatch.setenv("FLUXPLACE_FREEROUTING_JAR", str(explicit))
    assert D.find_freerouting() == str(explicit)
