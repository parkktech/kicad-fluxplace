import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import argparse
from fluxplace import partscli, dsfetch


def test_parts_fetch_splits_comma_and_space_lists(tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(dsfetch, "fetch_datasheet", lambda m, ds, refresh=False: (
        seen.append(m) or {"mpn": m, "ok": True, "level": "exact", "source": "s", "file": "f", "cached": False, "tried": []}))
    a = argparse.Namespace(mpns=["AAA1,BBB2", " CCC3 ,", ",DDD4", "AAA1"], spec=None, constraints=None, mpn_map=None,
                           datasheets=str(tmp_path), refresh=False, file=None, verbose=False, json=False)
    partscli.cmd_parts_fetch(a)
    assert seen == ["AAA1", "BBB2", "CCC3", "DDD4"]
