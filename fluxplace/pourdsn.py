"""Pour-aware routing job (router copy only): connections that pour-tie VERIFIED as satisfied through filled copper are not
Freerouting's job.

  (a) a pour net whose every pad is satisfied: its `(net NAME (pins ...))` loses its pins; its pads and the copper of their
      ties stay as obstacles, and the pour (restored after the import) connects them.
  (b) a pour net with untied pads: the real net loses its pins, and a derived net `NAME__PT` holds ONLY the untied pins
      plus one satisfied anchor pin (the nearest satisfied pad to the untied ones). Freerouting connects each untied pad to
      the anchor with a short track; `unmap_ses` renames the derived net back to NAME in the session file.

Only the DSN text (network section) and the SES text (net names) are changed; the board is not. Called from routefr.route_board."""
import json
import re

SUFFIX = "__PT"


def _net_blocks(text):
    """{net name: (start, end, pins_text)} of the `(net NAME (pins ...))` entries of the network section."""
    out = {}
    for m in re.finditer(r'\(net\s+"?([^\s"()]+)"?\s*\(pins([^)]*)\)\s*\)', text):
        out[m.group(1)] = (m.start(), m.end(), m.group(2).split())
    return out


def _token(ref, num):
    return f"{ref}-{num}"


def plan(pour_rec):
    """pour_rec (pourtie.json) -> {net: {"satisfied": [tokens], "untied": [tokens], "anchor": token|None}} for pour nets."""
    plan_ = {}
    sat = {}
    for s in pour_rec.get("satisfied", []):
        sat.setdefault(s["net"], []).append(s)
    for net, info in pour_rec.get("nets", {}).items():
        s_list = sat.get(net, [])
        un = [u for u in pour_rec.get("untied", []) if u["net"] == net]
        anchor = None
        if s_list and un:
            def d2(s):
                return min((s["at"][0] - u["at"][0]) ** 2 + (s["at"][1] - u["at"][1]) ** 2 for u in un)
            anchor = min(s_list, key=d2)
        plan_[net] = {"satisfied": [_token(s["ref"], s["pad"]) for s in s_list],
                      "untied": [_token(u["ref"], u["pad"]) for u in un if u.get("ref")],
                      "anchor": _token(anchor["ref"], anchor["pad"]) if anchor else None}
    return plan_


def apply(dsn_text, pour_rec):
    """-> (dsn_text, report). Report: counts per net and the pins each derived net takes."""
    rep = {"satisfied_nets": [], "partial_nets": [], "pins_removed": 0, "derived": {}, "unmatched": []}
    if not pour_rec:
        return dsn_text, rep
    blocks = _net_blocks(dsn_text)
    for net, p in sorted(plan(pour_rec).items()):
        if net not in blocks:
            continue
        start, end, pins = blocks[net]
        tokens = set(pins)
        # tokens the pour records name must exist in the DSN network entry (a DSN pin name that differs is reported)
        missing = [t for t in p["satisfied"] + p["untied"] if t not in tokens]
        rep["unmatched"] += [f"{net} {t}" for t in missing]
        if not p["untied"]:
            new_entry = f'(net {net})'
            rep["satisfied_nets"].append(net)
        else:
            anchor = p["anchor"]
            keep = [t for t in p["untied"] if t in tokens] + ([anchor] if anchor and anchor in tokens else [])
            new_entry = f'(net {net})'
            if len(keep) >= 2:
                new_entry += f'\n    (net {net}{SUFFIX} (pins {" ".join(keep)}))'
                rep["derived"][net] = keep
            rep["partial_nets"].append(net)
        rep["pins_removed"] += len(pins)
        dsn_text = dsn_text[:start] + new_entry + dsn_text[end:]
        blocks = _net_blocks(dsn_text)
    return dsn_text, rep


def unmap_ses(ses_path):
    """the SES net names of derived nets back to the real net (the wires are the real net's copper)."""
    txt = open(ses_path, errors="surrogateescape").read()
    n = len(re.findall(re.escape(SUFFIX) + r'\b', txt))
    if n:
        txt = re.sub(r'([A-Za-z0-9_+.\-]+)' + re.escape(SUFFIX) + r'\b', r'\1', txt)
        open(ses_path, "w", errors="surrogateescape").write(txt)
    return n
