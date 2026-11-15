"""Route-stage accounting: every net still not fully connected at the end of the route stage has a named owner (the step
that was responsible for closing it) and the measured reason (routediag causes).  A net with no owner is a stage FAIL
(ROUTE_NET_UNOWNED); a skipped net the router never claims is refused at the start of the stage."""
import json
import os

OWNERS = ("pairs", "router", "plane-tie", "rail-pour", "finish")


def unclaimed_skips(skip, open_conn, claimer_runs):
    """skip: nets the Freerouting job excludes; a skipped net that is open is only safe if a later step claims it.
    -> the open skipped nets nobody claims (non-empty means the stage must fail before any router hour)."""
    if claimer_runs:
        return []
    return sorted(n for n in skip if open_conn.get(n))


def table(open_conn, cls, claims, diag):
    """open_conn {net: open connections}, cls netclass classification, claims {net: owner} for every net a step is
    responsible for, diag routediag.diagnose result (or None) -> (rows, unowned).  rows sorted by open connections."""
    causes = {}
    for rec in (diag or {}).get("nets", []):
        causes[rec["net"]] = [c["cause"] for c in rec.get("causes", [])]
    rows, unowned = [], []
    for n in sorted(open_conn, key=lambda x: (-open_conn[x], x)):
        owner = claims.get(n)
        if owner is None:
            unowned.append(n)
        rows.append({"net": n, "class": (cls or {}).get(n, {}).get("class", "?"), "open": open_conn[n], "owner": owner or "NONE",
                     "reason": ",".join(causes.get(n, [])) or ("not diagnosed" if owner else "-")})
    return rows, unowned


def write(d, rows, unowned):
    """route_owners.json + route_owners.txt in the stage dir -> one line for the stage result."""
    with open(os.path.join(d, "route_owners.json"), "w") as fh:
        json.dump({"rows": rows, "unowned": unowned}, fh, indent=1)
    lines = [f"{'net':<24}{'class':<12}{'open':>5}  {'owner':<10}reason"]
    lines += [f"{r['net']:<24}{r['class']:<12}{r['open']:>5}  {r['owner']:<10}{r['reason']}" for r in rows]
    lines.append(f"total: {len(rows)} net(s), {sum(r['open'] for r in rows)} open connection(s), unowned {len(unowned)}")
    with open(os.path.join(d, "route_owners.txt"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return lines[-1]
