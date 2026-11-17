# W8 tool fixes

## Hand-repair endpoint islands

DRC endpoint UUIDs were truncated to eight characters before model lookup. Those prefixes are not unique on real boards, so a track endpoint could resolve to another net's copper and lose its island. `open-nets` now retains the full UUID and resolves endpoints by full UUID, with net/kind/layer/geometry-constrained fallback. Pad, track, via and zone-piece endpoints are covered by regression tests.
