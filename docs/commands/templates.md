# `templates` - structure templates and chain detection (round 2)

A centroid placer cannot find a required SHAPE (a combining tree whose leaves must sit in pairs
around their combiner; a chain whose parts must progress along a line). `fluxplace/templates.py`:

    [placement.template.bt_rf]
    kind = "tree"
    leaves = ["U305", "U306", "U307", "U308"]
    combiners = ["U310", "U311", "U309"]     # bottom-up: [0] <- leaves[0:2], [1] <- leaves[2:4], [2] <- [0:2]
    root = "U312"                            # stays; the tree grows toward the board centre (or direction = "+x")
    # nets = [...]                           # default: signal nets between members and their passengers
    [placement.template.audio]
    kind = "chain"
    refs = ["U5", "C500", "T500", "K1"]      # first and last stay, the interior spreads along the line in order

Tree: top-down from the root, each combiner at the legal seat nearest "ahead of its parent", its two
children side by side across the growth direction, then every PASSENGER (a 2-3 pad part with >= 2 signal
nets all reaching a member: isolation resistor, DC-block cap) at the seat that minimises its
class-weighted length, then up to 4 passes re-seating each part at the legal seat nearest the weighted
centroid of its partners, kept only when the class-weighted length of the listed nets drops. Every seat
goes through `legality.Legal`. `place --strategy routable` applies templates before its rounds and
treats each group (members, passengers, support parts of member ICs) as ONE rigid unit (never split by the
legaliser, not turned); a group that contains a fixed part is fixed as a whole. `fluxplace templates BOARD
--constraints C [--out OUT]` runs them alone and prints before/after lengths.

Chain detection (`routable.chains`, used to order the interior of every chain monotonically between its
ends) was rewritten: nodes are units, links are non-rail nets reaching 2-4 nodes weighted by class, small
non-IC parts that only hang on 3+-node nets (TVS, pull-ups) are shunts, not links; a node is PASS-THROUGH when
its two strongest neighbours carry >= 60 % of its link weight; ends may be movable. The old detector needed
both ends fixed, 2-pad nets only and exactly two neighbours; on the real board that missed audio
U5 -> T500 -> K1 (neither end fixed; T500 also has a net straight to U5, not only via U5's DC-block C500)
and CAN controller -> isolated transceiver -> choke -> J400 (controller not fixed, CAN_H/L are 3-pad nets with
the ESD diode, the transceiver also has supply/enable neighbours).
