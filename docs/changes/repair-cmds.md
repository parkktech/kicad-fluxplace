# repair-cmds

- `suggest` carries the path through its final via and onto the target pad: ends are extended to the pad (or via) centre on a layer the island is on, in 45/90 segments; the probe + endpoint join check stays the safety net; failure reports `endpoint extension failed` and still returns `path`.
- `suggest --explore`: the exact probe now ignores the removed blocker set, so `then_found` can be true for corridors that need a removal (it was always false before).
- `probe`: a path that starts or ends on a via joins islands at the via centre on any layer.
- New `copy-net SRC DST --net N [--out O]`: restore a net by name from another board, count-verified, guarded like `lay`. Help strings of the hand-route commands shortened so the README index stays inside its byte budget.
