# `density` - how full is each side

```
fluxplace density BOARD --constraints C [--spec S] [--target 0.65] [--grid 10] [--top N] [--full] [-q]
```
Per side (F, B) and total: courtyard area, fan-out ring area (estimate, formula in `fluxplace/density.py`), keep-out area
(keep-outs with `no_vias` on that side, clipped to the outline), usable area (outline minus keep-outs) and
utilisation = (courtyard + ring) / usable. Through-hole parts count on both sides. Regions (`--grid`, default 10 mm) get the
same numbers; the worst are listed first. Writes `<board>.density.json`.

| worse side | verdict |
|---|---|
| <= 60 % | OK |
| 60 - 75 % | TIGHT |
| > 75 % | FAIL (no room to re-seat or re-place; exit 1) |

`--target T` prints the growth that brings the worse side to T: per edge (L/R/T/B, bbox of the outline, new usable = old +
X * edge length, keep-outs unchanged) and uniform on all four edges. Utilisation never rises when an edge grows.
