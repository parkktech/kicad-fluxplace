"""Route-stage ownership: a net excluded from Freerouting that is still open must be claimed by a later step, and every
open net at the end of the route stage has an owner (routeowners.py)."""
import os, sys, types
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
from fluxplace import batchroute as BR, pipeline as PL, routeowners as RO, routediag as RD


def two_pair_board(tmp_path, complete_dp):
    body = G.two("RA", 10, 10, "DP", "DP2") + G.two("RB", 30, 10, "DP", "DP2")
    body += G.two("RC", 10, 30, "DN", "DN2") + G.two("RD", 30, 30, "DN", "DN2")
    return G.board(tmp_path, body, outline=(0, 0, 60, 60))


def test_unclaimed_skipped_net_is_reported_only_when_open():
    open_conn = {"DP": 1, "DN": 0, "CLK": 2}
    assert RO.unclaimed_skips(["DP", "DN"], open_conn, claimer_runs=False) == ["DP"]
    assert RO.unclaimed_skips(["DP", "DN"], open_conn, claimer_runs=True) == []


def test_skipped_open_pair_without_a_later_claimer_fails_before_the_router(tmp_path):
    """Freerouting excludes the pair nets; with no per-net router to claim them the stage must fail up front
    (before: _freerouting ran Freerouting and the open pair was routed by nobody)."""
    board = two_pair_board(tmp_path, False)
    B = RD.load(board)
    cls = {"DP": {"class": "pair"}, "DN": {"class": "pair"}}
    S = BR.settings({})
    conn = BR.default_conn(S)
    c = types.SimpleNamespace(a=types.SimpleNamespace(freerouting_budget=None, freerouting_threads=None, fr_loc=None, layers=["F.Cu", "B.Cu"]), cons={})
    rec = PL._freerouting(c, str(tmp_path), board, B, cls, ["DN", "DP"], {"min_track": 0.127, "min_clearance": 0.127}, conn,
                          lambda p: None, {}, str(tmp_path / "s.json"), lambda: 100.0, 100.0, claimer_runs=False)
    assert rec["ok"] is False and rec["unowned"] == ["DN", "DP"]
    assert rec["error"].startswith("ROUTE_NET_UNOWNED")


def test_table_names_owner_and_reason_and_flags_unowned(tmp_path):
    cls = {"DP": {"class": "pair"}, "GND": {"class": "plane"}, "VX": {"class": "low-speed"}}
    diag = {"nets": [{"net": "VX", "pads": [], "causes": [{"cause": "CONGESTION"}]}]}
    rows, unowned = RO.table({"VX": 3, "DP": 1, "GND": 2}, cls, {"VX": "router", "DP": "router"}, diag)
    assert unowned == ["GND"]
    by = {r["net"]: r for r in rows}
    assert by["VX"]["owner"] == "router" and by["VX"]["reason"] == "CONGESTION" and by["GND"]["owner"] == "NONE"
    line = RO.write(str(tmp_path), rows, unowned)
    assert "unowned 1" in line and (tmp_path / "route_owners.json").exists() and (tmp_path / "route_owners.txt").exists()
