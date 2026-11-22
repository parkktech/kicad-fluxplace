"""PHASE3I F9 (R19): item ids are unique. Real boards (kicad-cli DRC): three items share an 8-hex id head; a prefix that is
not unique is refused and lists its matches; breakout uuids carry the marker at the tail; reuuid_collisions renews the
heads on a work copy and keeps the items and the DRC keys."""
import hashlib
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
pytest.importorskip("pcbnew")
from fluxplace import handroute as H, boardtext as BT, breakout as BO  # noqa: E402
from test_handroute import Brd  # noqa: E402

HEAD = "deadbeef"


def _three_track_board(d):
    """net A, three segments on F.Cu, uuids rewritten so that all three share the 8-hex head HEAD."""
    os.makedirs(d, exist_ok=True)
    B = Brd()
    B.edge(-2, -8, 22, 8)
    B.pad("A1", "A", 2, 0)
    B.pad("A2", "A", 18, 0)
    B.track("A", [(2, 0), (6, 0), (10, 0), (14, 0), (18, 0)])
    p = B.save(d)
    text = open(p).read()
    seg_ids = re.findall(r'\(segment\b[^()]*(?:\([^()]*\)[^()]*)*?\(uuid "([0-9a-fA-F-]{36})"\)', text)[:3]
    assert len(seg_ids) == 3, seg_ids
    for u in seg_ids:
        text = text.replace(u, HEAD + u[8:])
    open(p, "w").write(text)
    return p


def _items(p, net="A"):
    return sorted(o.id for o in H._model(p, refill=False).obs if o.kind in ("track", "via") and o.net == net)


def test_shared_head_ids_print_longer_and_a_prefix_is_refused_with_its_matches(tmp_path):
    p = _three_track_board(str(tmp_path / "b"))
    M = H._model(p, refill=False)
    ids = [o.id for o in M.obs if o.id.startswith(HEAD)]
    assert len(ids) == 3
    assert H._id_len(M) > 8 and len(H._sid(ids[0], M)) == H._id_len(M)
    with pytest.raises(ValueError, match="is not unique"):
        H._resolve(M, HEAD)
    r = H.shove(p, "A", ids=[HEAD], out=str(tmp_path / "o.kicad_pcb"))
    assert not r["accepted"] and "is not unique" in r["reason"]
    assert sum(1 for i in ids if i in r["reason"]) == 3                       # the three matches are listed
    assert H._resolve(M, ids[0]) == [next(o for o in M.obs if o.id == ids[0])]   # a full id wins


def test_breakout_uuid_marker_is_at_the_tail_and_the_fanout_check_finds_it():
    import uuid as UU
    u = str(UU.uuid5(UU.NAMESPACE_URL, "breakout/x/1"))[:-8] + BO.MARK
    assert len(u) == 36 and u.endswith(BO.MARK) and not u.startswith(BO.MARK)
    from fluxplace import fanout_helper as FH
    assert FH.BREAKOUT_UUID == BO.MARK


def test_reuuid_collisions_renews_heads_keeps_items_and_drc_keys(tmp_path):
    p = _three_track_board(str(tmp_path / "b"))
    before_items = _items(p)
    keys_before = H.summarize_drc(H.drc_raw(p))["keys"]
    r = BT.reuuid_collisions(p)
    assert r["collisions"] == 1 and r["changed"] == 2
    heads = [u[:8] for u in before_items]
    after = _items(p)
    assert len(after) == len(before_items)                                    # the same number of items on the net
    assert len({u[:8] for u in after}) == len(after)                          # every head is unique now
    assert BT.reuuid_collisions(p) == {"collisions": 0, "changed": 0}
    assert H.summarize_drc(H.drc_raw(p))["keys"] == keys_before
    assert heads.count(HEAD) == 3
