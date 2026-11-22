"""K3: board load and the items/info ops, on a pcbgen board (kernel/build.sh first)."""
import pytest

import pcbgen as G
from fluxplace import kernel as K

pytestmark = pytest.mark.skipif(not K.available(), reason="run kernel/build.sh")

BODY = ('(segment (start 10 10) (end 20 10) (width 0.25) (layer "F.Cu") (net "N") (uuid "11111111-1111-4111-8111-111111111111"))\n'
        '(via (at 15 15) (size 0.6) (drill 0.3) (net "N") (uuid "22222222-2222-4222-8222-222222222222"))\n')


def _nm(mm):
    return round(mm * 1e6)


def test_counts_coords_and_via_span(tmp_path):
    path = G.board(tmp_path, BODY)
    with K.Kernel(path) as k:
        info = k.call("info")
        assert info["layers"] == ["F.Cu", "In1.Cu", "B.Cu"]
        assert info["nets"] == 1
        assert info["unsupported"] == []
        items = k.call("items")["items"]
    kinds = [it["kind"] for it in items]
    assert kinds.count("track") == 1 and kinds.count("via") == 1 and kinds.count("hole") == 3
    assert kinds.count("edge") == 1
    track = next(it for it in items if it["kind"] == "track")
    seg = track["shapes"][0]["core"]
    assert [_nm(v) for v in seg["a"]] == [10_000_000, 10_000_000]
    assert [_nm(v) for v in seg["b"]] == [20_000_000, 10_000_000]
    assert _nm(track["shapes"][0]["r"]) == 125_000 and track["net_name"] == "N"
    assert track["layers"] == ["F.Cu"]
    via = next(it for it in items if it["kind"] == "via")
    assert via["layers"] == ["F.Cu", "In1.Cu", "B.Cu"]
    assert sorted(it["id"] for it in items) == list(range(len(items)))


def test_items_filters(tmp_path):
    path = G.board(tmp_path, BODY)
    with K.Kernel(path) as k:
        only_tracks = k.call("items", kinds=["track"])["items"]
        on_in1 = k.call("items", layer="In1.Cu")["items"]
        near = k.call("items", bbox=[9, 9, 11, 11])["items"]
        none = k.call("items", net="NOPE")["items"]
    assert [it["kind"] for it in only_tracks] == ["track"]
    assert {it["kind"] for it in on_in1} == {"via", "hole"}
    assert "track" in [it["kind"] for it in near] and "via" not in [it["kind"] for it in near]
    assert none == []
