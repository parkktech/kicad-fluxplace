<!-- Outside reviewer (OpenAI) answers to the Fable follow-up questions, 2026-11-26, verbatim. -->

Your corrections change several recommendations. I inspected the W16 `w16merge` worktree at `e72d5b8`, including its commit guard, repair scoring, power-flow solver and rung execution. **Keep the existing pipeline; strengthen its contracts and feedback paths before attempting an architectural rewrite.** I did not modify either repository or rerun the full board.

1. **22 versus 23: reporting issue, not evidence of inconsistent measurement.**

   Agreed: one summary plus 22 details is consistent. Give the summary a separate code or `record_type`, and count findings from detail records.

   I would move “one authoritative measurement system” below placement, escape planning and routing contracts. However, two independent measurement defects remain in the inspected code:

   - `drcguard.measure()` caches by board path, modification time and size, excluding changes to project rules.
   - `measure_from_report()` includes zone errors in error keys but excludes them from its `errors` count.

   Fix these at the existing measurement seam. They do not justify replacing working measurement infrastructure.

2. **Lock enforcement: the stage manifest is sufficient for the final protection guarantee.**

   An immutable baseline manifest, complete item fingerprints and an unavoidable commit check can enforce “protected copper remains intact.” Your power-widen rejection demonstrates that it works. The unused `locked_ok` parameter is misleading API surface, not proof that locks are unenforced.

   An authorised write set answers an additional question: **did this repair modify only what it was allowed to modify?**

   Concrete example: repairing Ethernet changes an unrelated, unprotected control trace. It remains connected, creates no DRC error and leaves protected items intact. The stage manifest correctly passes, but the repair exceeded its intended scope. Another example is adding an unsuitable branch to a locked net: your locked-item policy permits additions, so width, layer and topology contracts must validate them separately.

   My recommendation:

   - Keep the stage manifest as the final protection gate.
   - Add inexpensive changed-item scope checks around risky repair transactions.
   - Require explicit, narrow releases for protected items.
   - Preserve the trusted baseline across resume; reseeding must not silently legitimise an unexplained mutation.

   Do not add another full-board DRC merely to enforce write scope.

3. **External router: history remains useful, but it is an orchestration heuristic.**

   PathFinder negotiates access to routing resources through changing costs. Using history only to select rip sets and ordering borrows that principle; it does not reproduce the algorithm or its results. [Original PathFinder paper](https://janders.eecg.utoronto.ca/1387_2015/readings/pathfinder.pdf)

   Start with a conflict graph: target connection → blocking items → owning nets → contested channel. Use repeated conflicts to favour a *different blocker set or escape assignment*, while charging heavily for displacing completed critical routes.

   Do not increase history simply because an attempt was refused. Separate:

   - Routing failure.
   - Search-budget exhaustion.
   - Successful routing rejected by a contract or DRC check.

   Only the first two provide routing-search evidence; neither alone proves geometric impossibility.

   **Synthetic keep-outs are a later option.** Without router support for soft obstacles, they have binary strength. You can decay their coverage or duration, but not their cost. Use them only in disposable candidates, away from necessary terminal access, and retain an unrestricted retry. Rip-set selection is the cheaper first experiment.

4. **Compound transactions: keep the error-key rule; classify warnings separately.**

   Judge `rip blockers → route target → restore displaced connections → cleanup` once, after the compound operation finishes.

   My default acceptance policy would be:

   - No connectivity or protected-item violation.
   - No new error-severity DRC key.
   - No regression in explicitly protected electrical requirements.
   - Strict improvement in the existing score.
   - Selected warning classes scored or capped according to their meaning.

   The alternatives allow different failures:

   | Policy | Failure it can permit |
   |---|---|
   | Error keys retained; warnings scored | A new warning can be accepted in exchange for higher-priority progress. This is appropriate only for warning classes explicitly allowed to trade. |
   | Key rule dropped; aggregate score used | Closing an open can outweigh a newly introduced clearance error because `opens` precedes `errors`. Even total errors falling can conceal a new violation elsewhere. |
   | All existing keys retained | An existing clearance violation can worsen without changing its normalised key or multiplicity. |

   That last issue matters with decimals stripped: key identity detects newly affected objects, not violation magnitude. For permitted pre-existing errors, compare the relevant measured severity where available.

   Before loosening anything, classify those 32 refusals by **all** rejection reasons and inspect their proposed improvements. Zero accepts could reflect bad candidates, an overly restrictive gate, or both.

5. **Pair order: agree with joint exit reservations.**

   Your experiment refutes “P1 first” as a useful fix for this case. Larger search budgets producing the same result strengthen the case for changing escape geometry.

   Reserve **candidate escape bundles**, rather than assigning a fixed-length corridor to each pair independently. Each bundle includes both traces, terminal access, layer, any via pair, and space for relevant neighbouring pins.

   Size them from actual geometry:

   - **Stub length:** the shortest route reaching a usable channel or legal via site outside the local pad obstruction.
   - **Pair width:** nominal copper span is `2 × trace_width + pair_gap`; test external clearances against neighbouring geometry.
   - **Via sites:** check annular-ring clearance, drill spacing, mask constraints, layers and reference-transition requirements.
   - **Neighbour access:** test ordinary pins and power exits during the same assignment.

   Avoid double-counting clearance by inflating both reservations with a full clearance allowance.

   On a 0.5 mm pitch part, begin with a small component-local region and expand only when no compatible assignment exists. A long reservation can itself create the starvation you are trying to prevent. Missing dimensions prevent a defensible universal stub length or keep-out width.

6. **Matching versus CP-SAT: the conflicts must be represented, but CP-SAT is optional.**

   Ordinary min-cost matching cannot prevent two distinct exit choices from crossing or violating clearance. A conflict-repair pass is adequate only if it **backtracks across earlier choices**, rather than repeatedly fixing the latest collision.

   For the two-pair magnetics footprint, enumerate legal *pair bundles* and test every combination. With candidate counts \(k_1\) and \(k_2\), that is only \(k_1k_2\) combinations. This is likely simpler than introducing a solver.

   For USB-C, generate complete, polarity-checked templates for the twin-pad connections and two via taps. Their internal connectivity and conflicts belong inside the template; do not treat the four pads as independent assignments. Validate the actual footprint pin identities and netlist before accepting the assumed middle-pair topology.

   If this grows beyond small enumeration, a binary model is straightforward:

   \[
   \sum_{c\in C_p}x_c=1,\qquad x_c+x_d\leq1
   \]

   The first selects one bundle for pair \(p\); the second excludes incompatible bundles. Minimise length, vias and consumption of scarce channels. CP-SAT supports integer constraint models and distinguishes feasible, infeasible and unknown results. An infeasible result still applies only to your generated candidate set. [OR-Tools documentation](https://developers.google.com/optimization/cp/cp_solver)

   **The conflict model is essential; the solver choice is secondary.**

7. **Zone noise: remove the blanket exemption for errors.**

   Include zone-related errors in acceptance keys and error counts. Historical variation in totals does not establish that zone errors are safe to ignore, and five stable repeats do not establish universal determinism.

   Use the same comparison procedure for incumbent and candidate:

   - Identical KiCad build and explicit project/rule inputs.
   - Fresh temporary project copies.
   - Refill all zones before DRC.
   - Identical severity and exclusion settings.
   - Stable violation identities using item UUIDs where available.

   KiCad documents `--refill-zones` specifically for refilling before DRC. [KiCad CLI documentation](https://docs.kicad.org/master/en/cli/cli.html)

   Canonical state means the same fill procedure and inputs, not necessarily byte-identical serialized polygon ordering. Both boards must be independently refilled because changed copper can legitimately change the fill.

   Warnings can follow the policy in answer 4. If flicker returns, capture it and narrowly address that mechanism. Do not restore an exemption for every violation mentioning a zone.

8. **Placement bounds: do not reject on Manhattan distance unless the route is truly rectilinear.**

   With diagonal tracks, Manhattan distance overestimates the shortest possible route. A displacement of 10 mm horizontally and vertically has Manhattan distance 20 mm, but a straight diagonal is about 14.14 mm.

   Use the metric permitted by the actual route geometry:

   - Arbitrary angles: Euclidean distance.
   - Horizontal/vertical only: Manhattan distance.
   - Horizontal/vertical/45°:  
     \[
     L_{\min}=\max(|dx|,|dy|)+(\sqrt2-1)\min(|dx|,|dy|)
     \]

   Measure between valid pad-access locations. Pad-centre distance can also overestimate the minimum copper length.

   **No percentage margin belongs in a mathematical infeasibility test.** Reject only when a valid lower bound exceeds the budget, allowing numerical tolerance. Use empirically measured detour margins to classify *routing risk*, not impossibility. A 0.15 mm grid alone cannot determine the obstacle detour allowance.

   For series parts, follow the requirement’s electrical endpoints:

   - If the budget covers an extended signal path through AC capacitors or a matching network, sum bounds for its mandatory copper segments.
   - Also enforce any segment-specific limits.
   - Do not blindly combine transformer primary and secondary paths into one conductor-length budget.
   - Include package delay or length only where the specification requires and supports it.

   Passing the bound establishes possibility in free space. The ghost route then tests obstacles, access and topology.

9. **Isolation: stages first, with disposable attempts for hazardous repair code.**

   Per-stage isolation prevents corruption from leaking into subsequent stages. It does not protect later attempts within the same stage.

   There is a useful correction from the inspected branch: **ordinary close rungs already run in child processes by default.** `_call_rung()` launches a rung job; `ai` and explicit `inproc` execution are exceptions. Preserve this rather than adding a duplicate boundary.

   I would use:

   - One fresh interpreter per stage.
   - Existing child isolation per close rung.
   - Additional isolation for any AI action executor that directly mutates pcbnew state.
   - Fresh candidate loads between attempts, without retaining native object references.

   There is no reason to pay an import cost for every track addition. Pay it at the attempt boundary where a mutation sequence can corrupt subsequent work. A child that corrupts itself before completing still needs internal fixes; process isolation only contains the damage.

10. **Merge gate: retain the full suite, fail early, and verify completion.**

   Yes: run the microboard stage tests first and abort on failure. Successful merges still require the full suite. Avoid running that subset twice unnecessarily.

   Beyond parallelism and a serial merge queue:

   - Test the exact proposed merged commit.
   - Pin its code and toolchain for the run.
   - Record collected, executed, skipped and completed test counts.
   - Reject interruption, timeout, collection failure and missing completion evidence.
   - Give workers unique files and constrain nested router parallelism.
   - Remove fixed baseline failures immediately.

   **I reproduced a concrete gate defect:** the inspected merge script ignores the test runner’s exit status. A synthetic runner printed `1 passed`, exited with interruption status `2`, and the merge gate returned `0` with “OK.” This was a temporary harness; no repository files were changed.

   Fix that before optimising gate speed. Accept runner status `0`, or status `1` only when completed execution and the explicitly permitted baseline explain every failure. Reject other statuses unconditionally.

   Start parallel execution conservatively and compare results with serial execution before increasing workers. Thirty-two available cores do not imply that 32 concurrent pcbnew/router workloads will be faster.

The immediate implementation order I recommend is: **merge-gate completion checks; valid placement bounds and support placement; joint escape assignment; fail-closed routing contracts; production-path regression fixtures; compound repair improvements.** Keep the magnitude-aware score, protected-item manifest, branch-current solver and existing rung isolation—they are useful foundations already present in your newer branch.
