"""Slow tier: every test here is marked `replay` and is SKIPPED unless GATE_REPLAY=1 (tools/merge-gate.sh runs it then, once per batch)."""
import os
import pytest


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="slow-tier replay: set GATE_REPLAY=1 (docs/REPLAY.md)")
    for item in items:
        if "/replay/" in item.nodeid.replace("\\", "/") or item.nodeid.startswith("tests/replay"):
            item.add_marker(pytest.mark.replay)
            item.add_marker(pytest.mark.xdist_group("serial"))
            if os.environ.get("GATE_REPLAY") != "1":
                item.add_marker(skip)
