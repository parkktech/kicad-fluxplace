# `modules` - rigid blocks: IC + supports

```
fluxplace modules BOARD --constraints C [--spec S] [--map M.json] [--no-arrange] [--no-fanout] [--full] [--top N]
```
Groups every IC / connector / module with its support parts into one rigid block the floorplan moves as a unit. Rules, in order:
a part that is not a small passive (C R L FB Y X D LED F, <= 2 pads) is the anchor of its own module; `[placement.group.NAME]
refs` merges anchors (J10 + J11); a small passive owned by one IC (`clusters.ownership`) joins it; the rest join the module they
share most signal nets with (ground and rails do not count), else the nearest; a member farther than 15 mm from its anchor stands
alone; fixed parts (`[placement] fixed`, `[lock.at]`, anchors, mounting hardware, minus `[floorplan] movable = [refs]`) are fixed
modules. `clusters.reseat` first seats far supports at their pins (legal by `legality.Legal`) unless `--no-arrange`.

Per module (map section `modules`, read with `map --show modules`): `anchor`, `members` (pose relative to the anchor), `fixed`,
`side`, `area` (courtyards), `bbox`, `ports` (every external net: class, pins, which side of the block they face N/E/S/W),
`ports_per_side`, and `fanout` = the module's own fan-out solved IN ISOLATION (only its parts exist), as geometry relative to the
anchor. Prints module count, largest modules, ports per side and modules with blocked pins.
Pose of a block at (X, Y) turned th in {0, 90, 180, 270}: member at (X, Y) + R(th) * rel, angle + th (`modules.pose`).
