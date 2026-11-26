<!-- Outside reviewer (OpenAI) round-2 answers, 2026-11-26, verbatim. -->

Fable’s revised plan is much closer to the remaining problems. I would make two adjustments:

- **Apply width/layer contracts to all newly added controlled copper**, including unprotected RF and power nets. Limiting the check to locked nets leaves another fallback path open.
- **Separate zone-error enforcement from repair-rule relaxation.** Start measuring zone-inclusive results immediately. Once comparisons are reproducible, enforce zone errors independently; a restrictive repair policy is not a reason to ignore them.

With Fable’s confirmation, silent manifest reseeding is now a concrete correctness defect. Refuse unexplained changes, preserve the previous manifest and report the item differences. An intentional upstream change should explicitly invalidate the affected protection and downstream evidence.

**1. Bundle scope: generate locally, select jointly, verify through routing.**

I would avoid choosing each component’s escape independently. I would also avoid immediately enumerating every complete geometric combination across three components.

Use three levels:

1. **Local candidate generation.** Generate legal escape bundles at each component. At the protection part, consider P0 and P1 together, including neighbouring pin access.
2. **Joint compatibility selection.** Connect those candidates through a small channel model: exit location, layer, pair ordering, usable channel width and remaining length budget. Eliminate incompatible combinations before invoking a router.
3. **Route verification of the best remaining assignments.** Route affected connections and validate the resulting board through the production checks.

For your case, the first decision is whether P0 and P1 can both leave the protection part through compatible exits. The next is whether those exits can connect to their destinations without consuming channels needed by higher-priority pairs.

**The verification set must include potentially displaced PCIe and USB3 routes.** A ghost route containing only the Ethernet pairs could reproduce the previous misleading success: P1 closes while the more important connections become impossible.

Start with the smallest coupled neighbourhood. Expand it when a failed probe identifies a blocker outside that neighbourhood. This gives you evidence for expanding scope instead of assuming every nearby component must participate.

For search, I would use inexpensive backtracking with early conflict pruning. A nominal product of \(k^6\) does not mean you must visit \(k^6\) assignments: incompatible choices can eliminate whole branches.

**When to add a solver dependency:** use measured search behaviour, rather than a universal candidate-count threshold. As an initial engineering limit—not a validated performance target—I would allow roughly **10,000 cheap partial-assignment expansions**, then route only a small shortlist. If representative cases repeatedly exhaust that search, or require complicated backtracking, the solver dependency is justified. Do not perform thousands of ghost routes.

The candidate representation and conflict model should remain the same whichever search engine you use. That makes a later solver substitution small.

One important distinction when learning from failed probes:

- Proven geometric conflict → forbid that combination.
- Routing timeout or budget exhaustion → deprioritise it or retry differently.
- Contract rejection → record the specific violation.

A router failing to find a path does not establish that the assignment is impossible.

**Acceptance test:** the P0/P1 fixture selects compatible exits, routes both, and preserves the required PCIe/USB3 results. A second fixture should force a far-end conflict so the planner must reconsider its initial local choice.

**2. Equal-score refusals: allow bounded exploration without replacing the incumbent.**

I would keep **strict improvement for the accepted board**, while permitting a small, disposable search frontier of legal equal-score candidates.

That separates two decisions:

- Is this board better enough to publish as progress?
- Is this board worth exploring because it might enable progress?

For example, moving an ordinary trace sideways may leave the score unchanged but open a usable pair escape. Discarding it immediately can prevent the next successful operation.

A practical first implementation:

- Keep the incumbent unchanged.
- Retain a few equal-score candidates.
- Explore a short chain of further operations from each.
- Apply the hard contracts at completed candidate boundaries.
- Promote a descendant only when it strictly improves the incumbent.
- Stop exploration at a bounded depth or work budget.

A beam of three candidates and depth two would be a reasonable **starting experiment**, not a promised optimum. Prefer sideways candidates with evidence of benefit: freed terminal access, fewer blockers or a compatible escape assignment.

**A no-revisit memo is necessary but insufficient.** There can be an enormous number of different equal-score boards. Bound the search as well. Identify states by canonical relevant geometry and applicable constraints, rather than filename or raw serialization.

Before adding this machinery, inspect equal-score outcomes. Some may reveal a measurement problem:

- The intended target improved, but the score does not represent that improvement.
- Another metric worsened and cancelled the benefit.
- The operation changed geometry without creating a useful opportunity.

Those cases need different fixes.

**There is no percentage of “new error key” refusals that proves the gate is wrong.**

Instead, examine the subset that would otherwise improve the target:

| Refusal pattern | Likely response |
|---|---|
| Repeated clearance or hole-spacing errors | Fix candidate generation or rule propagation |
| Repeated off-centre via errors | Fix geometry generation or add guarded cleanup |
| Warning-only additions | Apply the explicit warning policy |
| Apparent new errors caused by unstable identity or fill | Fix measurement comparison |
| One recurring error across many attempts | Investigate one shared defect before changing acceptance |

Record **all independently evaluable failures**, but mark checks that could not run as unknown. A crashed connectivity measurement should not appear as either “connectivity passed” or an ordinary connectivity violation.

For each refusal, retain the score delta, target delta, added violations, changed items and blocker set. That will explain much more than a histogram alone.

Finally, fold recovery testing into these changes: interrupt bundle verification and sideways exploration, then resume. The checkpoint must retain the verified incumbent, discard incomplete candidates and preserve its protection record. That directly tests whether these improvements increase **end-to-end completion reliability**, rather than merely making individual repairs more capable.
