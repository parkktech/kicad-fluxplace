"""DRC endpoint to geometric-island resolution with colliding display UUIDs."""
import types

from fluxplace import handroute as H
from fluxplace.hrgeom import Ob


RULE = {"class": "Default", "width": 0.2, "clearance": 0.2, "via_dia": 0.6, "via_drill": 0.3}


def _ob(kind, uid, x):
    if kind == "pad":
        g = ("poly", [(x - 0.2, -0.2), (x + 0.2, -0.2), (x + 0.2, 0.2), (x - 0.2, 0.2)])
    elif kind == "track":
        g = ("seg", (x - 0.5, 0.0), (x + 0.5, 0.0), 0.1)
    else:
        g = ("seg", (x, 0.0), (x, 0.0), 0.3)
    return Ob(kind, "N", ("F.Cu", "B.Cu") if kind == "via" else ("F.Cu",), g, id=uid)


def _item(kind, uid, x, zone=False):
    if zone:
        desc, pos = "Zone [N] on F.Cu", {"x": x, "y": 1.0}
    elif kind == "pad":
        desc, pos = "Pad 1 [N] of P1 on F.Cu", {"x": x, "y": 0.0}
    elif kind == "track":
        desc, pos = "Track [N] on F.Cu, length 1 mm", {"x": x, "y": 0.0}
    else:
        desc, pos = "Via [N] on F.Cu - B.Cu", {"x": x, "y": 0.0}
    return {"uuid": uid, "description": desc, "pos": pos}


def _resolve(kind_a, kind_b, zone_a=False):
    uid_a = "12345678-0000-4000-8000-000000000001"
    uid_b = "12345678-0000-4000-8000-000000000002"
    decoy = _ob("track", "12345678-0000-4000-8000-000000000003", 20)
    decoy.net = "OTHER"
    obs = [decoy]
    groups = []
    if not zone_a:
        obs.append(_ob(kind_a, uid_a, 0))
        groups.append({"id": 0, "items": [obs[-1]], "zones": []})
    a = _item(kind_a, uid_a, 0, zone_a)
    b = _item(kind_b, uid_b, 5)
    obs.append(_ob(kind_b, uid_b, 5))
    groups.append({"id": len(groups), "items": [obs[-1]], "zones": []})
    zones = []
    if zone_a:
        fill = {"outline": [(-1, 0), (1, 0), (1, 2), (-1, 2)], "holes": [], "bb": (-1, 0, 1, 2)}
        zones.append({"net": "N", "fills": {"F.Cu": [fill]}})
        groups.insert(0, {"id": 0, "items": [], "zones": [(0, "F.Cu", 0)]})
        groups[1]["id"] = 1
    omap = {o.id: g["id"] for g in groups for o in g["items"]}
    model = types.SimpleNamespace(obs=obs, zones=zones, net_rule=lambda net: RULE,
                                  islands=lambda net: (groups, omap))
    raw = {"unconnected_items": [{"items": [a, b]}], "violations": []}
    return H.open_nets("synthetic.kicad_pcb", raw=raw, M=model)["connections"][0]


def test_open_nets_resolves_pad_pad_with_colliding_uuid_prefixes():
    c = _resolve("pad", "pad")
    assert (c["a"]["island"], c["b"]["island"]) == (0, 1)
    assert c["a"]["uuid"].startswith(c["a"]["id"]) and len(c["a"]["uuid"]) > len(c["a"]["id"])


def test_open_nets_resolves_track_pad_endpoint():
    c = _resolve("track", "pad")
    assert (c["a"]["island"], c["b"]["island"]) == (0, 1)


def test_open_nets_resolves_track_track_endpoint():
    c = _resolve("track", "track")
    assert (c["a"]["island"], c["b"]["island"]) == (0, 1)


def test_open_nets_resolves_via_endpoint():
    c = _resolve("via", "pad")
    assert (c["a"]["island"], c["b"]["island"]) == (0, 1)


def test_open_nets_resolves_zone_piece_endpoint():
    c = _resolve("pad", "pad", zone_a=True)
    assert (c["a"]["island"], c["b"]["island"]) == (0, 1)
