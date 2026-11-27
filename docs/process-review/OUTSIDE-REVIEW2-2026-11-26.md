<!-- Outside reviewer (OpenAI) answer to PROMPT-2-2026-11-26.md, verbatim. -->

For reducing the current 22 opens, I’d rank the problems 3 → 1 → 2 → 4: pair exits, close-stage execution, pin breakout, then length budgets.** I’d change the first two in parallel. The pair-order experiment identified a specific way to change which connections close. The close run showed that three of four attempts ended before an acceptance decision could help. I would defer equal-score exploration until close regularly produces valid candidates for actual opens.

| Rank | Problem | Why it ranks here |
|---|---|---|
| 1 | **3. Joint pair exits** | You can already close P1 by changing order. The opportunity is to do so while preserving PCIe RX and USB3 TX. Earlier W16 reporting attributed 12 of 22 opens to Ethernet P1/P3 legs. Verify that distribution on the exact checkpoint under test. |
| 2 | **1. Close execution** | Forty minutes produced one accepted power improvement and no reported open reduction. Recovering that time benefits several gap classes. |
| 3 | **2. Twelve pins without escapes** | This is a hard gate for the next placement. Its impact on the current 22 depends on which blocked pins belong to those disconnected components. Twelve blocked pins can represent fewer than twelve opens. |
| 4 | **4. Length budgets** | The reported violations are on routed nets. They matter for fab readiness, but shortening them has an indirect effect on open count unless relocation also frees a contested channel. |

### 1. Close barely runs

**Likely cause.** Close is spending its budget on operations that produce too few candidates relevant to connectivity. The power widen improved its deficit by 2.25. That was legitimate progress, but it closed no connection. The other three outcomes need separate diagnoses. Exit 1 can mean a handled error or an unhandled exception. An omitted board can mean the operation correctly found nothing to do or that output generation failed. Counting all three as “refused” hides where the work stopped. The earlier 32 attempts should be analysed separately because they came from a different board version.

**Measure next.** Account for all 2,400 seconds using non-overlapping wall-clock intervals. The island timeout explains 434 seconds, about 18%. The other 1,966 seconds include the other attempts and any time spent outside them. Break that down into initial measurement, diagnosis, queue wait, child startup, router execution, refill/DRC, commit and cleanup. Also record how many **distinct disconnected components** received an applicable attempt, how many attempts produced readable candidates, and how many connections those candidates closed. Those three numbers distinguish a scheduling failure from a geometry failure and a guard refusal.

There is a concrete detail to verify in the new timeout implementation. In the local `w16merge` revision `59b6cc0`, `gaps._call_rung()` gives the supervisor `ctx["budget_s"] + 120` seconds. With a 5% share of 40 minutes, the nominal island budget is 120 seconds. If the new cap changes only the child’s allowance, that child can still occupy up to 240 seconds before subsequent verification. Check the exact build used for the next run.

**Change.** Keep specialist operations and child isolation. An island fill and a coupled-pair re-lay deserve different geometry code. Give them one execution protocol: `diagnose → propose → execute → verify → commit`. Each proposal must name its target disconnected components, explain why the operation applies, and identify what improvement it could produce. Have the executor distinguish `not_applicable`, `no_change`, `candidate`, `timeout`, `child_error` and `invalid_output`. Only a measured candidate can receive `guard_refused`.

Schedule by **groups of opens sharing a blocker**. Give each eligible group a cheap first attempt before spending a second expensive attempt on one group. Reserve time for verification and use an absolute monotonic deadline shared by the parent, child and router descendants. After the first pass, spend remaining time on operations with the best measured rate of closing connections within the required electrical priority. Power widening can still qualify when diagnosis shows that fixing a rail is a prerequisite for an open. A crash should suppress an identical retry until the input, operation or configuration changes.

For diagnosis without a rerun, persist the attempt record **before launching the child**. Capture the input board/rules/manifest identifiers, target pads and blockers, operation parameters, router build/order/seed, phase timestamps and effective deadlines. On exit, attach the last completed phase, exit code or signal, stderr/traceback, candidate artifact if any, and all independently measured guard deltas. My first regression test would combine a hung island job with a cheap routable open. It passes when close reaps the hung job, gives the other open a turn, and commits its verified closure within the stage budget.

### 2. Twelve fine-pitch pins have no escape

**Likely cause.** The unchanged pin set after substantial support-placement improvement points to a local geometry or rule interaction. The thermal window is the leading suspect. The old finding was approximately 13 pins, however. Match the current 12 by `(reference, pad, net)` before assuming they are the same population.

**Measure next.** On the exact current placement, produce a per-pin breakdown showing where candidate escapes disappear: pad exit, same-layer stub, via annulus, drill or via span, mask rule, occupied site, or onward channel. Then assign escapes **jointly**. Twelve pins can each have a legal dog-bone in isolation while competing for a handful of common sites. For the matching portion, a subset of pins with fewer compatible sites than pins is a concrete explanation the placer can act on.

Use controlled probes to assign ownership:

| Result on a disposable board copy | Decision |
|---|---|
| A simultaneous production escape succeeds at the current poses using approved geometry | Routing owns materialising those escapes. Correct the placement checker if it predicted failure. |
| Changing only a movable neighbour or rotating the IC produces a verified simultaneous escape | Placement owns the repair. Return the successful poses and exits as evidence. |
| The pins gain escapes only when the thermal rule is disabled | Review the rule’s physical meaning and the allowed layout. The disabled-rule result diagnoses the blocker. |
| A finite search exhausts its sites, layers or time | Record the exhausted search domain. Expand it or escalate the specific constraint before declaring physical impossibility. |

**Change.** Make the *breakout finding* a placement-gate responsibility, but assign its *repair* from those probe results. Try alternate dog-bone directions, supported layers and approved via styles at fixed poses first. An outer-layer SMD pad still needs a legal path to its transition. Via-in-pad should enter the search only if the land pattern and selected fabrication process permit it.

For the thermal window, **strip the generated mask artwork only if the physical rule survives independently**. If a through-via is forbidden anywhere in that opening, the same prohibition must govern placement, fanout, both routers and repair even while the mask polygon is absent. If the design instead permits certain vias provided the final mask is re-cut around them, encode that narrower rule everywhere and verify the resulting opening, mask web and thermal coverage. A blanket placement exclusion would reject legal solutions in the latter case. The decisive fixture is a via at the disputed boundary taken through **placement → routing with artwork stripped → re-cut → final DRC and mask check**.

### 3. The pair planner has yet to demonstrate a real-board win

**Likely cause.** The 1.5/1.6 mm sweep shows that the synthetic fixture crosses a capacity threshold. It gives little information about which constraint matters on W16. The real board has a **choice-dependent** failure: one locally successful escape displaces another required route. My first suspects are shared transition sites or downstream channels omitted by the straight-corridor test, followed by whether KRT actually honours the selected exits. I would put more search budget or a new layer-change cost behind those checks.

**Measure next.** Freeze the board immediately before P0 escapes. Replay both observed orderings with fixed grid origin, rules, router build and settings. Then, on scratch copies, cross **two materially different forced exit geometries** with **the two orderings**. Materialise the exits as geometry KRT demonstrably consumes. Record the planned, exported and realised ports, via sites, layers and headings. Score P0, P1, PCIe RX and USB3 TX individually. Include those last two explicitly in this experiment because they are observed victims, even if their straight-line corridors miss the part.

| Result | What I would change |
|---|---|
| Planner on/off produces the same exported or realised exits | Inspect candidate diversity, reservation export, owner release and router import. Increasing the 10,000-node cap would test the wrong mechanism. |
| A forced exit preserves all required pairs, but none of the five shortlisted plans does | Find the successful bundle in the candidate pipeline. If absent, fix generation. If ranked sixth or represented by five near-duplicates above it, fix ranking or shortlist diversity. |
| Realised exits differ, but the failure moves to another layer transition or the far end | Expand the neighbourhood through observed copper, via sites and blocked channel cells. Permit a channel or a via bank to trigger expansion even when the probe names no component. |
| Fixed exits succeed or fail solely with routing order | Test single-net calls against one batch on the same checkpoint. Put the fix at the router-call boundary if KRT’s internal ordering causes the difference. |

**Construct the reproducer from the full board before cropping it.** First preserve the observed failure trade as the test predicate. Illustrative pseudocode:

```python
def reproduces_trade(board):
    a = replay(board, order="P0-first")
    b = replay(board, order="P1-first")
    return (
        a.open("ETH_P1")
        and b.closed("ETH_P1")
        and all(a.passes(p) and not b.passes(p)
                for p in ("PCIE_RX", "USB3_TX"))
    )
```

Use the exact per-pair signatures from your replays. Remove unrelated obstacles and nets in groups, retaining a deletion only while **both** signatures survive. Then crop. Keep the original coordinate system and grid phase, stackup, keep-outs, rules, protected copper and relevant via spans. At each cut, preserve net identity, layer, entry heading, P/N ordering, outside connectivity and remaining length/skew allowance. A free boundary pad can manufacture an escape that the full board never had. If the signature disappears after cutting, enlarge the region or restore the missing boundary condition.

**Change.** Make reproducing the baseline trade the first acceptance criterion. Demonstrating that the planner fixes it is a separate criterion. If the real A/B shows no difference, the first diagnostic is **“did the intervention change the copper that blocked P1?”** If yes, follow the failure downstream. If no, fix that causal link before expanding the solver.

### 4. Eight routed paths exceed their length budgets

**Likely cause.** The pull rung is treating several different situations as the same repair: fixed anchors that may already violate a bound, a chain whose parts must move together, and a legally placed chain whose route took a detour. A routed length of 62 mm against 35 mm tells you the size of the violation. It does not establish which situation produced it.

**Measure next.** For each path, report four quantities: the specified budget \(T\), the free-space lower bound at the **current poses** \(B_\mathrm{current}\), a **certified lower bound over all permitted poses** \(B_\mathrm{all}\), and the shortest **verified routed length** \(U\) found so far. The decision logic is small enough to audit:

```python
if certified_global_lower_bound > budget + tolerance:
    return "infeasible under declared fixed parts and pose domains"
if verified_route_length is not None and verified_route_length <= budget:
    return "feasible; retain placement and route as a witness"
if current_pose_lower_bound > budget + tolerance:
    return "current poses excluded; search other legal placements"
return "unresolved; inspect access constraints and routing detour"
```

The subtlety is how \(B_\mathrm{all}\) is obtained. The best pose found by sampling is **not** a certified global lower bound. Use a valid relaxation over all allowed poses, or an exact optimisation over the complete permitted placement grid. Share each footprint’s pose across all its terminals. For copper-only budgets, sum the required pad-access-to-pad-access **copper legs**, respecting their individual limits. A straight distance between fixed anchors can overstate required copper when a series component bridges part of the distance internally. A solver timeout can still prove infeasibility *if its valid lower bound already exceeds the budget*.

**Change.** Retain the cheap pull when one movable endpoint dominates the violation. For the persistent cases, re-place the source, series parts and load as a small group, allowing legal rotations and selected neighbour moves while preserving the support-placement gains. Enforce the applicable budgets as constraints. Prefer the feasible placement with the least disruption, then route it to obtain a witness. For the crystal, solve both oscillator legs, crystal orientation and load capacitors together. Trace parasitics and loop area matter independently of a scalar length target. [NXP crystal oscillator design guide](https://www.nxp.com/docs/en/application-note/AN14518.pdf)

For RF, **trace the 35 mm and 26.5 mm limits back to their source before changing them**. If a module or matching-network reference design requires a physical length, preserve it and escalate a proven conflict. If the number was derived from an insertion-loss allocation for a matched feed, re-derive it across the operating band using stackup-specific attenuation, launch/switch/connector losses and design margin. Also check return loss and any phase or matching requirement. A nominal 50 Ω specifies characteristic impedance. It supplies neither the available loss budget nor the attenuation per millimetre. The 62/35 ratio alone therefore supports neither a waiver nor a placement redesign. [TI low-power RF layout review guide](https://www.ti.com/lit/an/swra367a/swra367a.pdf) · [NI discussion of mismatch and insertion loss](https://www.ni.com/en/shop/electronic-test-instrumentation/switches/what-to-know-about-rf-switches/guide-to-understanding-and-developing-an-rf-switch-network/chapter-4--effects-of-impedance-matching-and-switch-quality-on-r.html)

The next decision I’d make from the running A/B is whether **a different realised P0 exit preserves P1, PCIe RX and USB3 TX together**. In parallel, run the close timing breakdown and join the 12 blocked pad IDs to the 22 open connections. Those results would let you replace this provisional ranking with a count of which actual opens each fix can reach.
