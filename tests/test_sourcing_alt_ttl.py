"""fix-c #9: alternates() never uses a cache row older than CACHE_TTL."""
import json
import time
from fluxplace import sourcing as S

DATA = {"package": "0603", "raw_params": {"Resistance": "10k"}}


def _setup(tmp_path, alt_t):
    pdata = {"R_A": {"data": DATA}, "R_B": {"data": DATA}}
    (tmp_path / S.PARTDATA_CACHE).write_text(json.dumps(pdata))
    report = {"R_A": {"verdict": "NONE", "required": 10}}
    cache = {"R_B": {"_t": alt_t, "dk": [1000, [[1, 0.5]]], "mo": None}}
    return report, cache


def test_fresh_alternate_is_used(tmp_path):
    report, cache = _setup(tmp_path, time.time())
    S.alternates(report, cache, str(tmp_path))
    assert [a["mpn"] for a in report["R_A"]["alternates"]] == ["R_B"]


def test_stale_alternate_not_used_and_counted(tmp_path):
    report, cache = _setup(tmp_path, time.time() - S.CACHE_TTL - 60)
    S.alternates(report, cache, str(tmp_path))
    assert report["R_A"]["alternates"] == []
    assert report["R_A"]["alternates_stale"] == 1
