<!-- Outside review (ChatGPT, via owner) of PROMPT-2026-11-26.md, received 2026-11-26. Verbatim; Fable cross-check in FABLE-REVIEW-2026-11-26.md. -->

# Fluxplace — Expert Architecture and Process Review

## Executive assessment

I would change the architecture before investing substantially more time in individual routing algorithms.

Fluxplace has demonstrated that it can place a complex board and route most of it. Going from 452 opens to 22 with zero shorts is meaningful progress. However, the remaining problems suggest that the system is solving the wrong optimization problem at several points in the flow.

The objective should not be to produce a placed board, route it, and then repair whatever electrical or physical problems remain. It should be to progressively construct an electrically valid, manufacturable board while retaining the ability to reconsider earlier decisions.

I reviewed your supplied measurements alongside the actual [Fluxplace repository](https://github.com/parkktech/kicad-fluxplace?utm_source=chatgpt.com), including its [flow redesign](https://github.com/parkktech/kicad-fluxplace/blob/main/docs/FLOW-REDESIGN.md?utm_source=chatgpt.com), [failure ledger](https://github.com/parkktech/kicad-fluxplace/blob/main/docs/ONE-SHOT-LEDGER.md?utm_source=chatgpt.com), [constructive flow](https://github.com/parkktech/kicad-fluxplace/blob/main/docs/FLOW.md?utm_source=chatgpt.com), and [Quilter adoption plan](https://github.com/parkktech/kicad-fluxplace/blob/main/docs/QUILTER-PARITY-PLAN.md?utm_source=chatgpt.com).

Several of the improvements below have already been recognized in your repository. Where that is the case, I am recommending architectural enforcement and measurable acceptance criteria rather than another implementation of the same idea.

My five principal findings are:

- Placement is being accepted too early. Zero KiCad errors establishes geometric legality, not electrical suitability or routability.
- The guards are enforcing local monotonic improvement at the expense of global convergence. Some valid solutions require temporarily undoing previous work.
- Routing intent is advisory in too many places. A general-purpose router should never be able to silently downgrade an RF, differential, or high-current net.
- The test suite does not adequately represent the actual production execution environment. Passing 3,100 tests is insufficient if stage composition and transaction boundaries are not tested.
- The pipeline lacks effective feedback between routing and placement. A fundamentally bad component arrangement cannot reliably be repaired by increasingly expensive routing attempts.

## 1. Top 10 prioritized improvements

Effort estimates are approximate focused engineering time, not measured delivery estimates. They assume reuse of your existing modules and may overlap.

| Priority | Improvement                                                                                              | Expected impact                                                  | Effort    |
| -------- | -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | --------- |
| 1        | Mandatory, centralized electrical constraint enforcement for every copper writer                         | Critical — prevents electrically invalid routing                 | 3–5 days  |
| 2        | Move electrical placement checks upstream; validate critical routes before freezing placement            | Critical — eliminates major late-stage failures                  | 4–8 days  |
| 3        | Transactional, multi-net repair with controlled unlocking                                                | Critical — breaks closure deadlocks                              | 4–7 days  |
| 4        | Real-board stage integration tests using production guards                                               | Very high — prevents recurring and combinational failures        | 2–4 days  |
| 5        | One authoritative board-state measurement and reporting system                                           | Very high — eliminates false acceptance and rejection            | 3–5 days  |
| 6        | Joint fanout, critical-path and power-corridor planning                                                  | Very high — prevents self-inflicted routing obstructions         | 5–10 days |
| 7        | Isolate pcbnew mutation in fresh subprocesses with atomic checkpoints                                    | High — eliminates state corruption and enables rollback          | 2–4 days  |
| 8        | Incremental, region-based routing and content-addressed caching                                          | High — substantially reduces iteration time                      | 5–10 days |
| 9        | Deterministic placement tournaments with progressive evaluation budgets                                  | High — improves selection quality and avoids wasted routing time | 3–5 days  |
| 10       | Replace passive AI packets and manual fixes with executable recovery policies and regression obligations | High — improves autonomy and prevents repeated failures          | 2–4 days  |

### Current routing progress

Initial board

452 opens

First full route

39 opens

Best current board

22 opens

Opens eliminated

# 95.1%

Shorts

# 0

Remaining DRC errors

# 7

The last 5% is not necessarily another 5% of the work. It contains the hardest connectivity problems, electrically constrained routes, and consequences of earlier placement decisions.

I would stop treating total open count as the principal indicator of fab readiness.

# 2. Architecture: Replace the linear pipeline with a feedback-driven solver

### Answer to A

A sequential pipeline is appropriate for artifact production and dependency management. It is not appropriate as the sole mechanism for solving placement and routing.

Keep the stage framework. Change the orchestration model.

Your current architecture effectively says:

Current model: expensive discovery of earlier design mistakes near the end.

The better model is:

Forward execution

Feedback and repair

The key distinction is that a completed stage produces a validated checkpoint, not an irreversible design decision.

If closure finds that a connector escape is physically impossible, the solver should invalidate that local escape, move the responsible component if permitted, and rebuild the affected region. It should not spend another 45 minutes trying different router parameters against the same impossible geometry.

### 2.1 Introduce three types of gates

| Gate              | Purpose                               | Example                                                                 |
| ----------------- | ------------------------------------- | ----------------------------------------------------------------------- |
| Invariant         | Must hold for every committed board   | No new shorts, unchanged schematic connectivity, permitted copper edits |
| Phase feasibility | Proves sufficient progress to advance | Every critical interface has a feasible escape and routing corridor     |
| Release           | Must pass before fabrication          | Zero unapproved DRC errors, zero opens, all required electrical checks  |

Currently, the guard appears to perform parts of all three jobs. That conflates safety with progress.

An incomplete board can be a valid intermediate checkpoint. A board containing an unauthorized change to its electrical requirements cannot.

### 2.2 Placement should be validated electrically before bulk routing

The 10 decoupling capacitors at distances of 16–56 mm and the nine excessive crystal/RF/clock lengths should largely have been identified before full routing.

For placement-dependent requirements, distinguish between:

- Geometric feasibility: Can the components be placed close enough?
- Routing feasibility: Is there a legal path within the required length, layer and return-path constraints?
- Realized electrical compliance: Does the actual copper meet those constraints?

For example, if a crystal has a maximum permitted route length of 8 mm, and the pin-to-pin straight-line distance is already 14 mm, no router can solve that. Eight millimeters here is illustrative; the actual limit must come from the device requirements.

Your placement checker should reject such a candidate immediately.

| Placement constraint | Early validation                                                             |
| -------------------- | ---------------------------------------------------------------------------- |
| Decoupling           | Exact capacitor-to-power-pin association, pad distance, ground-return access |
| Crystals             | Oscillator pins, crystal and load-capacitor geometry                         |
| Switching regulators | Input and switching-loop geometry, feedback isolation                        |
| RF                   | SMA-to-module path, matching network, permitted outer-layer corridor         |
| PCIe / USB3          | Pair escape orientation, coupling clearance, layer feasibility               |
| Ethernet             | PHY-to-magnetics and magnetics-to-connector separation and orientation       |
| Power rails          | Physical width capacity, via sites, current-path topology                    |
| Audio                | Transformer orientation, separation from switching and digital noise sources |

A rigid module is not necessarily a valid module. Validate its internal placement against these requirements before allowing the floorplanner to use it.

### 2.3 Do not perform a two-hour full route for every placement

Use a multi-fidelity placement tournament.

| Evaluation tier | What it proves                                                | Initial time budget per candidate |
| --------------- | ------------------------------------------------------------- | --------------------------------- |
| 0               | Collision, keepout and electrical distance feasibility        | 1–5 sec                           |
| 1               | Pin access, pair escape feasibility, routing-channel capacity | 5–30 sec                          |
| 2               | Actual critical-path routing on a disposable candidate        | 1–3 min                           |
| 3               | Constraint-aware ghost routing and congestion analysis        | 2–5 min                           |
| 4               | Complete production routing of the best candidates            | As required                       |

These are proposed initial budgets to benchmark, not existing Fluxplace performance measurements.

For the current board, I would evaluate perhaps 30 cheap candidates, six detailed candidates and two full-route candidates rather than extensively tuning a single placement.

The critical addition is that the ghost router must use the actual widths, layer restrictions, relevant obstacles and reservations. A successful 0.15 mm ghost route does not demonstrate that a much wider 5 A power path is feasible.

Similarly, do not discard useful critical copper from the winning placement if it was constructed using production rules and can be independently validated.

# 3. Correct routing order for this particular board

Your current `fanout → pairs → tune → critroute → power/general routing` sequence is problematic.

However, simply moving pairs before fanout is not sufficient. A differential pair needs an escape of its own, and a high-current power path needs enough physical space before those escapes occupy the available channels.

Plan these resources jointly, then commit their copper in a deliberate order.

| Order | Work                                 | Important behavior                                                                         |
| ----- | ------------------------------------ | ------------------------------------------------------------------------------------------ |
| 1     | Establish final stackup and rules    | Verify dielectric thicknesses, copper thickness, impedance geometries and fab capabilities |
| 2     | Place electrically sensitive modules | Include decaps, RF matching parts, crystals and power stages                               |
| 3     | Plan critical escapes                | Allocate paired via sites, power via arrays, RF launch geometry and free pin exits         |
| 4     | Reserve corridors                    | Reserve space for RF, pair families, rail trunks, tuning and return paths                  |
| 5     | Route local electrical structures    | Decaps, oscillators, regulator loops and essential GND connections                         |
| 6     | Construct constrained interconnect   | PCIe, USB3, Ethernet, RF and large-current paths according to difficulty and reservations  |
| 7     | Route remaining fanout               | Do not consume previously reserved critical resources                                      |
| 8     | Bulk route ordinary signals          | KRT first; eligible Freerouting fallback                                                   |
| 9     | Negotiate remaining congestion       | Multi-net rip-up, reroute and local placement repair                                       |
| 10    | Final tune and finish                | Retune changed paths; refill, verify, finalize mask and silk                               |

Steps 5–7 should allow controlled iteration rather than establish another rigid ordering.

### Use a difficulty-based scheduler within each priority class

For example, a PCIe pair with only one viable escape should be routed before another pair with four possible paths.

A useful priority model would consider:

- Electrical criticality.
- Number of feasible escape sites.
- Number of permitted routing layers.
- Corridor width required.
- Remaining path-length budget.
- Pair symmetry and via constraints.
- Competition for shared routing resources.
- Previous failures and their actual causes.

A failed critical pair should not be demoted behind ordinary nets just because it was difficult. It should become a congestion-resolution task with its own blocker set.

### Particular concern: your six-layer stackup

Layer topology only, not physical dielectric thicknesses.

This stackup gives you two ground reference planes, which is useful. But your routing rules need to be more intelligent than simply marking four layers as signal-capable.

For high-speed routing:

- Preserve reference-plane continuity.
- Account for broadside coupling between L3 and L4, depending on their dielectric separation.
- Provide appropriate nearby GND return vias for critical signal layer transitions.
- Restrict RF to its approved layers and geometries.
- Include through-via barrel length, stubs and layer-dependent propagation delays when evaluating critical paths.
- Avoid allowing routing or power pours to create narrow or interrupted ground-return paths.

For the four RF traces currently reported at 71 Ω on inner layers, I would not simply widen them until the calculated impedance reaches 50 Ω. That still violates the specified outer-layer architecture. Reroute them on an approved layer using the fabricator's actual impedance geometry.

Do not permit the general-purpose fallback router to select an unapproved layer.

# 4. Invariants and contracts: Make incorrect routing impossible

### Answer to B

This is probably the highest-leverage software engineering improvement available to Fluxplace.

I would introduce a single compiled electrical constraint model consumed by every placer, router, repair tool and verification stage.

At present, the repository has constraint handling distributed among modules such as `constraints.py`, `netclass.py`, `review.py`, `drcguard.py`, `krtchain.py`, and `lockpolicy.py`.

Having multiple consumers is fine. Having multiple independent interpretations of what constitutes a valid route is not.

## 4.1 Compile a mandatory routing contract

Every net, pair and logical signal path should receive an explicit contract.

For example, an RF routing contract could look like this:

```
net: BT1_RF
class: RF_50OHM

routing:
  allowed_layers: [F.Cu, B.Cu]
  impedance_target_ohm: 50
  impedance_tolerance: from_approved_spec
  width_by_layer: from_validated_stackup
  max_length: from_device_requirements
  via_policy: explicit
  reference_plane: GND

permissions:
  generic_router: false
  pair_router: false
  rf_router: true
  repair_policy: preserve_electrical_intent

verification:
  require_connected: true
  require_impedance_check: true
  require_return_path_check: true
```

This is a proposed schema, not your existing constraint-file syntax.

Contracts should be compiled before placement begins. They should also have provenance identifying the datasheet, fabrication requirement, engineering decision or schematic information from which each requirement originated.

Generate native KiCad custom design rules from this model wherever possible, then independently check requirements KiCad cannot adequately represent. KiCad supports custom routing width, clearance, differential-pair, skew and other design constraints.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://docs.kicad.org\&sz=32)

KiCad+1



A router must not be responsible for deciding whether it is qualified to route a net. The dispatcher must make that decision.

## 4.2 Mandatory invariants

I would implement the following as production transaction contracts.

| Invariant                                          | Enforcement                                                                 |
| -------------------------------------------------- | --------------------------------------------------------------------------- |
| Schematic connectivity is immutable                | Compare required pad-to-net assignments before and after every transaction  |
| Every net has a resolved routing class             | Ambiguous critical nets fail preflight                                      |
| All copper obeys its routing contract              | Inspect actual layer, width, clearance and via geometry                     |
| Routers cannot downgrade a net                     | Dispatcher eligibility checks and independent output validation             |
| Differential pairs are atomic routing groups       | Route, rip and validate both legs together                                  |
| Protected objects cannot change without permission | Compare full object identities and geometry against an authorized write set |
| Every report belongs to an exact board revision    | Require board and rule fingerprints                                         |
| Required measurements cannot silently fail         | Return UNKNOWN or ERROR, never an empty successful result                   |
| Zone-dependent measurements require current fills  | Refill or explicitly mark results provisional                               |
| A rejected operation leaves no changes             | Atomic transaction rollback                                                 |
| Every committed artifact can be reloaded           | Fresh-process board reload                                                  |
| Critical routing priorities survive retries        | Preserve class and resource priority                                        |
| Identical inputs produce reproducible evaluation   | Canonical ordering, controlled seeds and pinned tool versions               |
| Fabrication rules cannot be weakened by a router   | Hash and compare the compiled rules                                         |
| All stage outputs are independently verifiable     | Read back saved geometry instead of trusting router statistics              |

### 4.3 Specific problems I found in the repository

These deserve targeted fix items.

| Location                                                                                                                              | Concern                                                                             | Recommended change                                                                              |
| ------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| [`drcguard.py`](https://github.com/parkktech/kicad-fluxplace/blob/main/fluxplace/drcguard.py?utm_source=chatgpt.com) — `measure()`    | Cache identity uses board path, modification time and size                          | Include project rules, constraints, tool versions and content hashes                            |
| `drcguard.py` — `keys_from_report()`                                                                                                  | Excludes certain violations involving zones; other functions partially restore them | Eliminate blanket zone exclusions from authoritative verification                               |
| `drcguard.py` — `accept()`                                                                                                            | Lock validation depends on an optional `locked_ok` callback                         | Require the transaction layer to enforce authorization unconditionally                          |
| [`gaps.py`](https://github.com/parkktech/kicad-fluxplace/blob/main/fluxplace/gaps.py?utm_source=chatgpt.com) — `impedance_off_nets()` | An exception can return an empty set of failures                                    | Return explicit measurement failure; prove downstream handling fails closed                     |
| [`pipeline.py`](https://github.com/parkktech/kicad-fluxplace/blob/main/fluxplace/pipeline.py?utm_source=chatgpt.com)                  | Stage state supports `warn`, `na`, `skipped` and approximate open measurements      | Define which outputs are mandatory and forbid approximate results from satisfying release gates |
| [`lockpolicy.py`](https://github.com/parkktech/kicad-fluxplace/blob/main/fluxplace/lockpolicy.py?utm_source=chatgpt.com)              | Protection depends partly on inferred net names and classes                         | Use the compiled routing contract as the authoritative classification                           |
| [`placetour.py`](https://github.com/parkktech/kicad-fluxplace/blob/main/fluxplace/placetour.py?utm_source=chatgpt.com)                | Ghost-route judging and placement ranking need consistent electrical context        | Require the same constraint manifest for every candidate                                        |

These are based on inspected source code. I did not trace every caller or execute the code, so some may already be mitigated elsewhere. They are specific audit targets rather than confirmed new production defects.

### 4.4 Replace binary “routed” status with meaningful states

A PCIe pair can be physically connected and electrically unacceptable.

It should not be classified as complete simply because there are no ratsnest lines.

UNROUTED

No complete copper path

PARTIAL

Some connectivity exists

CONNECTED_INVALID

Electrical requirement failed

UNVERIFIED

Verification unavailable

VERIFIED

All required checks passed

Only `VERIFIED` should count as completed critical routing.

That single change would prevent the general router from artificially improving your completion score by connecting constrained nets incorrectly.

## 4.5 Fix the power verification model

Charging every branch of a rail with the entire rail's current is incorrect.

The power network needs a connectivity and load model.

### Example — current distribution on a power rail

Load A

2.5 A

Load B

2.0 A

Load C

0.5 A

Trunk current

## 5.0 A

Illustrative loads, not measured CM5 board currents.

For a tree-shaped network, propagate worst-case downstream load currents toward the source. For meshed pours and multiple parallel paths, use a resistive-network solution rather than simply summing current along an arbitrary tree.

The power contract should validate:

- Current through each segment and via group.
- Worst-case voltage drop at every load.
- Copper thickness and estimated temperature rise.
- Neck-down length and local bottlenecks.
- Connector, fuse, protection and pad current limits.
- Ground-return capacity.
- Peak or transient load conditions where relevant.

A short pad neck-down and a long undersized power trace should not necessarily receive the same verdict.

Likewise, the width check must not declare a large copper pour sufficient if all its current passes through one inadequate via or narrow connection.

# 5. Making the closure loop converge

### Answer to C

Your repository's own [redesign document](https://github.com/parkktech/kicad-fluxplace/blob/main/docs/FLOW-REDESIGN.md?utm_source=chatgpt.com) identifies the central problem: edits that close one connection at the expense of temporarily opening another are rejected.

Later code already introduces a shared guard and lexicographic scoring. That is a good improvement, but it does not completely solve the search problem.

## 5.1 Separate working state from committed state

You need three distinct board states.

Incumbent

Best validated board. Immutable.

Working

Temporary rip-up and reconstruction permitted.

Candidate

Completed repair, independently verified.

Your guard should primarily compare the completed candidate against the incumbent.

It should not reject every intermediate operation.

For example, imagine a region with three existing open connections.

| Operation                              | Opens | Result             |
| -------------------------------------- | ----- | ------------------ |
| Initial committed board                | 3     | Valid checkpoint   |
| Rip up five blocking routes            | 8     | Working state only |
| Route the two critical connections     | 6     | Working state only |
| Reroute four displaced connections     | 2     | Working state only |
| Reconnect final displaced net          | 1     | Candidate          |
| Full guard and electrical verification | 1     | Commit if valid    |

The existing board is never endangered. The working copy is disposable.

This is fundamentally different from disabling the guard.

## 5.2 Protect electrical intent, not necessarily individual copper objects

The current `lockpolicy.py` already establishes hard, soft and free classes. I would extend that concept into explicit edit capabilities.

| Protection level        | Policy                                                                   |
| ----------------------- | ------------------------------------------------------------------------ |
| Immutable               | Owner-fixed placement, mechanical anchors, manually certified structures |
| Electrically protected  | May be rebuilt only by an authorized class-aware repair transaction      |
| Provisionally protected | Prefer preservation, but allow controlled removal and replacement        |
| Free                    | Ordinary copper eligible for congestion negotiation                      |

The important distinction is between:

- A PCIe pair that must retain its electrical compliance.
- The exact copper geometry currently implementing that PCIe pair.

Those are not always the same requirement.

If a valid replacement PCIe pair opens space for three blocked nets while maintaining impedance, skew, return-path quality and other requirements, the solver should be allowed to consider it.

Owner-certified RF geometry or other genuinely immutable structures remain protected.

## 5.3 Use a two-level objective system

Do not attempt to express all electrical validity in one scalar.

### Level 1: Hard invariants

Reject completed transactions involving unauthorized schematic changes, new shorts, unauthorized protected-object changes, newly invalid certified critical paths, fabrication-rule violations introduced by the transaction, or unavailable mandatory verification.

These are not tradeable against improved wirelength.

### Level 2: Search and progress objectives

Among eligible candidates, use a lexicographic vector resembling:

```
score = (    critical_interfaces_incomplete,    critical_electrical_failures,    critical_violation_magnitude,    required_pad_disconnects,    ordinary_drc_failures,    power_integrity_deficit,    congestion_overflow,    excess_vias,    total_trace_length,)
```

The precise ordering should be established through board requirements and controlled experiments. In particular, critical power failures belong in the critical-electrical tier, not merely the secondary power deficit.

Use per-interface and per-net constraints to prevent aggregation from hiding regressions. Improving two Ethernet pairs must not automatically authorize degrading a previously compliant PCIe link.

Track violation magnitude as well as count. Reducing a skew violation from 10 mm to 0.5 mm is progress even if the number of violations remains unchanged.

Also retain a bounded search frontier: a candidate that scores equally but has materially different congestion or escape geometry may be the only route to a better solution.

## 5.4 Implement negotiated congestion

Rather than trying arbitrary rip-up operations, make the solver learn which resources are oversubscribed.

Maintain a congestion history for each layer, routing tile, channel, via site or critical escape.

A conceptual routing cost would be:

\\[ C(e,n)= C\_{\text{geometry}}+ C\_{\text{via}}+ C\_{\text{present congestion}}+ C\_{\text{historical congestion}}+ C\_{\text{electrical}} \\]

Each time multiple nets compete for the same resource, increase its historical congestion cost. Flexible nets are encouraged to use alternatives while constrained nets receive precedence.

Illegal layers, forbidden geometry and immutable objects remain unavailable rather than simply expensive.

This is an adaptation of negotiated-congestion methods, not a requirement to implement an entirely new router.

## 5.5 Change how the repair ladder escalates

Your repair ladder should select methods based on the diagnosed failure rather than repeatedly increasing search effort.

| Diagnosed condition               | Correct escalation                                   |
| --------------------------------- | ---------------------------------------------------- |
| Minor track-to-via misalignment   | Coordinate normalization                             |
| Stale or disconnected pour        | Refill and connectivity analysis                     |
| Single blocked path               | Local class-aware reroute                            |
| Shared congested corridor         | Multi-net negotiated rip-up                          |
| Sealed pin                        | Rebuild escape                                       |
| Impossible pair breakout          | Reorient or move associated components               |
| Length budget infeasible          | Revisit placement                                    |
| No legal power corridor           | Reallocate copper or change placement                |
| Persistent regional infeasibility | Consider approved board-size or stackup alternatives |

A router timeout is not evidence of physical impossibility. Distinguish search-budget exhaustion from a proven local geometric obstruction and from invalid input.

Stop repeating an attempt when its input geometry, constraints, routing policy and blocker set are unchanged.

Do not just raise `max_nodes` every time a pair fails.

# 6. Reducing iteration time by 70–90%

### Answer to D

That is an optimization target for routine development iterations, not a prediction that complete board routing will become 90% faster.

I would treat a 2–3 hour full-board route as a release-validation workload, not the default development feedback loop.

## 6.1 Make the orchestrator stateless with respect to pcbnew

Your SWIG corruption is sufficient evidence that the current single-process architecture is unsafe under mutation.

I found that you already have [`freshproc.py`](https://github.com/parkktech/kicad-fluxplace/blob/main/fluxplace/freshproc.py?utm_source=chatgpt.com), which explicitly documents the corruption following `board.Remove()`.

I would make this the standard mutation boundary rather than an exception used by selected functions.

```
Orchestrator
    |
    +-- Load immutable checkpoint
    |
    +-- Start isolated mutation worker
    |       |
    |       +-- Load board
    |       +-- Apply authorized changes
    |       +-- Save candidate
    |       +-- Exit process
    |
    +-- Start independent verification worker
    |       |
    |       +-- Reload saved candidate
    |       +-- Refill zones
    |       +-- Validate
    |
    +-- Commit or discard
```

The orchestrator should exchange file paths, immutable facts and structured transaction results. It should never retain live SWIG board objects across mutation boundaries.

Also isolate the entire project bundle, not only the `.kicad_pcb` file. Your repository already documents the problem of board saves producing conflicting `.kicad_pro` settings.

This is particularly important because KiCad's SWIG Python bindings are deprecated, with removal planned for KiCad 11. The newer IPC API in KiCad 10 still requires a running GUI; headless support is documented for KiCad 11. Build an adapter boundary now rather than immediately rewriting the headless pipeline around IPC.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://dev-docs.kicad.org\&sz=32)

KiCad+1



## 6.2 Create immutable, content-addressed stage checkpoints

A useful cache key is not just a board filename and timestamp.

It should incorporate:

```
SHA256(
    input_board_content
    + schematic_netlist_hash
    + project_rules_hash
    + compiled_constraints_hash
    + datasheet_facts_hash
    + stage_implementation_hash
    + router_version_and_parameters
    + toolchain_version
    + deterministic_seed
)
```

Include only dependencies relevant to the stage, but include all of those dependencies.

For example, changing silkscreen positioning should not invalidate a previously verified PCIe routing result. Changing dielectric thickness absolutely should invalidate impedance verification.

| Change               | Invalidate                                              |
| -------------------- | ------------------------------------------------------- |
| Silkscreen text      | Silk, affected DFM checks, release                      |
| RF width rule        | RF routing, overlapping resources, SI checks            |
| Modem orientation    | Its escapes, incident nets, neighboring geometry        |
| Power branch load    | Power verification and affected routes                  |
| Pair skew limit      | Affected pair tuning and verification                   |
| Stackup              | Routing geometry, impedance, via and return-path checks |
| Datasheet correction | All checks derived from the changed fact                |

Cache successful results and deterministic failed attempts. A known impossible escape should not be searched again until one of its relevant dependencies changes.

## 6.3 Incremental regional routing

I would build this around the existing `repair-region`, `region`, `closure` and `gaps` infrastructure rather than introduce another standalone repair command.

A regional repair consists of:

- A target failure.
- A bounding region with an appropriate clearance halo.
- The exact blocking copper objects.
- Nets incident to those objects.
- Their connected components and electrical contracts.
- Legal boundary connection points.
- An authorized edit set.

Route the smallest useful region first. Expand it only when the blocker analysis shows that the original region cannot accommodate a valid solution.

The difficult part is ensuring local changes do not have nonlocal consequences.

For instance, moving a via can affect a plane return path; removing a track can disconnect a distant pad; changing one leg of a pair can alter end-to-end skew.

Therefore, check:

| Check                                  | Local or global?                |
| -------------------------------------- | ------------------------------- |
| Track spacing and geometric collisions | Local, including clearance halo |
| Required pad connectivity              | Global for affected nets        |
| Differential-pair delay and skew       | Entire affected logical path    |
| Rail voltage drop                      | Entire affected rail            |
| Ground-plane continuity                | Affected reference plane        |
| Final DRC                              | Global                          |
| Release verification                   | Global                          |

Use local geometry checks as a fast rejection filter, not as the sole authority for accepting a final board.

## 6.4 Stop spending 107 minutes on unsuccessful pair routing

I would instrument the pair router with failure classifications and separate budgets.

| Attempt                      | Suggested initial budget                  |
| ---------------------------- | ----------------------------------------- |
| Pair endpoint feasibility    | 1–5 sec                                   |
| Paired-via-site search       | 5–15 sec                                  |
| Direct coupled path          | 10–30 sec                                 |
| Alternative escape and layer | 30–90 sec                                 |
| Negotiated regional reroute  | 1–3 min                                   |
| Placement reconsideration    | Triggered by persistent geometric failure |

A difficult pair may legitimately need more time, but escalation should require evidence of continuing search progress.

Prune searches using an admissible lower bound on path length, feasible layer transitions, remaining skew budget, via compatibility and reachable corridor geometry.

For the 12–107 minute pairs stage, measure time spent on successful pairs separately from failed fallback attempts. The older repository redesign already reported that most of one 79-minute pair run was spent in fallback processing.

That suggests removing futile search may outperform optimizing the inner A\* loop.

## 6.5 Establish performance targets

### Proposed development feedback budgets

Upper-end targets compared with your reported baseline. Not benchmark results.

Full routing

Current

180 min

Target

90 min

Pairs stage

Current

107 min

Target

30 min

Routine pre-merge gate

Current

30 min

Target

5 min

Additional initial targets:

| Operation                            | Target                                    |
| ------------------------------------ | ----------------------------------------- |
| Pure geometry candidate rejection    | Under 1 second                            |
| Electrical placement validation      | Under 10 seconds                          |
| Typical regional repair attempt      | Under 60 seconds                          |
| Real-board targeted integration test | Under 3 minutes                           |
| Resume from a valid checkpoint       | Under 10 seconds, excluding required work |

Profile before optimizing. Record wall time, CPU time, time waiting for external tools, DRC invocations, zone refills, expanded search nodes, cache hits and successful repairs.

The throughput metric should be verified improvement per minute, not just how fast a router exits.

# 7. Stop regressions structurally

### Answer to E

The most alarming statistic in your entire description is not the 22 remaining opens.

It is this:

# 41

Fixed in flow with tests

# 78

Not confirmed automated and regression-tested

The other 78 may contain valuable investigations and manual fixes, but they have not met the definition of a permanent automated fix.

Your process is generating knowledge faster than it is incorporating that knowledge into reliable software.

## 7.1 Introduce six distinct test layers

| Test layer            | Purpose                                                          |
| --------------------- | ---------------------------------------------------------------- |
| Unit                  | Algorithms, parsing and calculations                             |
| Property-based        | Geometry, connectivity, ordering and mutation invariants         |
| Golden microboards    | Reproduce specific known PCB failure mechanisms                  |
| Stage integration     | Execute the actual stage with production configuration and guard |
| Stage composition     | Prove consecutive stages work together                           |
| Full-board regression | Validate global routing and final electrical quality             |

You probably have substantial coverage of the first layer. I would prioritize layers 3–5 immediately.

## 7.2 Golden microboard fixtures

Build a small library of deliberately pathological boards extracted or reduced from your actual failures.

| Fixture                   | Required assertion                                                        |
| ------------------------- | ------------------------------------------------------------------------- |
| `locked_power_widen`      | Rail widens without unauthorized copper changes; production guard accepts |
| `swig_remove_then_load`   | Next stage can load the committed board normally                          |
| `thermal_mask_window`     | No unrelated signal copper is exposed by the final opening                |
| `pair_escape_conflict`    | Both legs escape without violating pair geometry                          |
| `rf_fallback`             | Plain router cannot route RF on an illegal layer                          |
| `power_branch_tree`       | Each branch carries its actual downstream design load                     |
| `power_mesh`              | Parallel paths are evaluated consistently                                 |
| `stale_pair_report`       | Board change invalidates old report                                       |
| `footprint_order_shuffle` | Geometrically equivalent boards produce the same evaluation               |
| `sealed_pin_trade`        | Multi-net rip-up closes the target and restores displaced routes          |
| `drc_open_cap`            | Connectivity remains correct above KiCad's observed reporting limit       |
| `datasheet_missing`       | Missing evidence produces UNKNOWN/FAIL, not a successful default          |
| `uuid_prefix_collision`   | No ambiguous identifier can modify the wrong object                       |
| `zone_refill_failure`     | Candidate cannot be committed with unverified connectivity                |

The UUID, DRC-cap, datasheet-path and related cases come directly from your [repository failure ledger](https://github.com/parkktech/kicad-fluxplace/blob/main/docs/ONE-SHOT-LEDGER.md?utm_source=chatgpt.com).

Keep these fixtures small enough that the entire collection can run in a few minutes.

## 7.3 Property and metamorphic testing

These tests are particularly valuable for geometry-heavy software.

A metamorphic test changes an input in a way that should not change its meaningful outcome.

Examples:

- Reorder footprints, tracks, zones and vias in the file.
- Change numeric net IDs while preserving net identities.
- Rename references while preserving declared electrical roles.
- Translate the board and all geometry together.
- Reverse the enumeration of pair legs.
- Save and reload without making electrical changes.
- Serialize equivalent constraints with a different field order.
- Change the process working directory.

The electrical evaluation should remain equivalent, subject to explicitly defined numerical tolerances.

For routing algorithms, test geometric properties such as collision-free paths, correct connectivity, width compliance, valid pair gaps and preservation of unauthorized objects.

A particularly important property is:

```
if transaction.rejected:    assert committed_artifact_hash == original_hash
```

And another:

```
if stage_result.status == "PASS":    assert all_required_checks_are_verified
```

## 7.4 Real-board integration must exercise the production guard

The power-rail widening failure demonstrates why this matters.

This is insufficient:

```
result = widen_power_rail(board)assert result.width_improved
```

The real requirement is closer to:

```
result = execute_stage(    stage="route",    checkpoint=real_board_fixture,    production_guard=True,    production_constraints=True,)assert result.committedassert result.board_reloadsassert result.electrical_checks_passassert result.unauthorized_changes == []
```

The test should invoke the same transaction executor used by the actual pipeline.

Do not allow a fixture runner to set `--force`, bypass locks, disable DRC or manually seed a successful stage state.

If the production guard rejects the fix, the test must fail.

## 7.5 Reduce the merge gate without weakening it

I would use three CI tiers.

| Gate                    | Scope                                                                  | Proposed budget         |
| ----------------------- | ---------------------------------------------------------------------- | ----------------------- |
| Worker feedback         | Changed code, unit tests, affected golden fixtures                     | 1–3 min                 |
| Required merge gate     | Critical contract tests, affected stage integrations, dependency tests | 3–8 min                 |
| Main/release validation | Full suite, full-board tests, router benchmarks                        | 20 min to several hours |

Initially retain your full required merge suite while building parallel shards and measuring their reliability. Only move tests out of the blocking path when you can prove the faster gate covers the relevant risks.

Run tests in parallel where isolation permits. Use a merge queue to validate the combined result of concurrent worker branches, not merely each branch independently against an older version of `main`.

For example, a lock-policy change and a power-widening change might each pass individually but fail when merged together.

The merge queue must test that composition.

# 8. What commercial workflows and research do differently

### Answer to F

There are several useful techniques worth borrowing. None requires replacing Fluxplace with a machine-learning-driven autorouter.

## 8.1 Quilter — electrical comprehension before layout

Quilter explicitly models placement-dependent constraints such as bypass capacitors, crystal oscillators and switching converters, along with impedance, differential pairs and power networks. Its candidate evaluation incorporates electrical constraints in addition to completion and fabrication rules.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://docs.quilter.ai\&sz=32)

Quilter+3



Your existing Quilter parity document already captures many of these concepts.

The issue is ensuring they become mandatory dependencies of layout rather than review-time observations.

What I would borrow: make circuit comprehension a compiled, executable contract shared by placement and routing.

What I would not borrow yet: the expense and complexity of building a reinforcement-learning layout system. Your current failure data points much more strongly to integration, feasibility and constraint-handling deficiencies than to a need for a new learning algorithm.

## 8.2 Altium — logical signal paths and guided routing

Altium's differential-pair workflow supports extended signal paths through series components. ActiveRoute supports groups of selected routes and guided routing corridors, while routing and tuning use the configured design constraints.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://www.altium.com\&sz=32)

Altium Designer Technical Documentation+3



Fluxplace should introduce a first-class object beyond the individual KiCad net: the logical electrical path.

For PCIe or USB3 through AC-coupling capacitors:

Conceptual example; actual capacitor topology and location must follow the interface requirements.

The solver should understand the complete path through series elements when evaluating delay, skew and topology.

For Ethernet, preserve separate PHY-side and magnetics line-side electrical domains. Do not treat transformer boundaries as if they were ordinary series resistors.

This would be especially useful for your board because net-name-based pair identification cannot fully represent the electrical topology.

## 8.3 PathFinder — negotiated congestion

McMurchie and Ebeling's 1995 PathFinder: A Negotiation-Based Performance-Driven Router for FPGAs is directly relevant to the closure architecture, although it targets FPGAs rather than PCBs.

It uses iterative resource negotiation and historical congestion penalties, allowing the search to explore intermediate states that are not yet physically realizable.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://www.scilit.com\&sz=32)

Scilit+1



The lesson for Fluxplace is to negotiate shared resources across multiple competing nets rather than freeze every successful route permanently.

Borrow the strategy, not the FPGA-specific resource graph.

## 8.4 PCB research — simultaneous escape and area routing

Two papers are particularly relevant.

| Research                                                                                                      | Application to Fluxplace                                                                               |
| ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| A Complete PCB Routing Methodology with Concurrent Hierarchical Routing, DAC 2021                             | Joint layer assignment, escape ordering and area routing with differential-pair and length constraints |
| A Matching-Based Escape Routing Algorithm With Variable Design Rules and Multiple Constraints, IEEE TCAD 2025 | Allocate escape resources using matching, routing channels and differential-pair-aware planning        |

The 2021 work reports completion on seven commercial PCB designs in its experimental set. The 2025 paper reports improved constrained escape routing relative to the comparison tools in its tests. These are research benchmark results, not guarantees on your CM5 board.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://ieeexplore.ieee.org\&sz=32)

IEEE Xplore+1



This is probably the most relevant research direction for your current fanout problem.

Instead of saying:

> Every pin gets an independently feasible escape.

The goal should be:

> Every required pin or pair receives a mutually compatible escape connected to a feasible destination corridor.

That is a materially stronger guarantee.

## 8.5 Make routing a resource allocation problem first

For the CM5 and M.2 connectors, model candidate escape sites and routing channels before generating detailed copper.

For each pin or pair, identify possible:

- Escape direction.
- Via site or via pair.
- Routing layer.
- Corridor assignment.
- Destination entry direction.

Then select mutually compatible assignments using matching, constraint programming or a limited optimization solver.

A CP-SAT model could be appropriate for the discrete assignment portion of a particularly congested connector escape. You would not need to represent every trace segment on the entire PCB.

Let KRT handle detailed geometry where it performs well. Use optimization to make better decisions about scarce resources before handing those decisions to KRT.

# 9. Fix the AI-driven engineering process

### Answer to G

Your top-level AI and worker AI architecture is reasonable. The weakness is that agents are incentivized to fix the current symptom rather than eliminate the entire failure mechanism.

I would make the following changes.

## 9.1 Change the definition of done

A ledger item should not be marked fixed unless all six conditions are satisfied.

### Fix acceptance checklist

0 of 6

Original failure reproduced in a regression test

Root cause identified rather than only the symptom

Fix incorporated into the default automated flow

Production stage guard accepts the resulting board

Regression suite passes, including affected stage combinations

Before/after measurements recorded on a real board

Not yet a permanent fix

&#x20;Copy checklist

A hand script may be an excellent diagnostic tool. It may demonstrate that the proposed algorithm works.

But it is not a permanent fix until the default pipeline can perform the same operation without manual intervention.

## 9.2 Turn the ledger into an executable regression registry

Keep the Markdown ledger for human readability, but back it with structured records.

```
id: FP-0120
category: stage_integration
root_cause: unauthorized_locked_copper_mutation

reproduction:
  fixture: locked_power_widen
  entrypoint: constructive.route

expected:
  transaction_committed: true
  unauthorized_changes: 0
  power_violation_improved: true

regression:
  test: test_power_widen_production_guard
  required_in_ci: true

status: VERIFIED
fix_commit: commit_sha

real_board_evidence:
  input_hash: sha256
  output_hash: sha256
  measurement_report: artifact_path
```

I would support distinct statuses:

`DISCOVERED → REPRODUCED → FIXED → INTEGRATED → VERIFIED`

Also track `WORKAROUND`, `BLOCKED` and `DEFERRED` independently.

Never conflate `WORKAROUND` with `VERIFIED`.

For the 78 failures that were not confirmed automated, categorize them by underlying mechanism. You may find that a much smaller number of architectural fixes permanently resolves many ledger rows.

## 9.3 Stop requiring an AI to answer routine recovery packets

The fact that the pipeline emits a packet and exits with code 5 is not inherently wrong. The problem is that it lacks an orchestrator responsible for consuming that event.

I would divide packets into three categories.

| Category                   | Example                                                                          | Resolution                                |
| -------------------------- | -------------------------------------------------------------------------------- | ----------------------------------------- |
| Deterministic recovery     | Stale zone fill, blocked ordinary escape                                         | Execute registered recovery policy        |
| Bounded engineering search | Move a component or rebuild a congested region                                   | Search authorized candidate actions       |
| Owner decision             | Increase board dimensions, change stackup, violate a fixed mechanical constraint | Suspend with a resumable decision request |

AI should be an optional proposal generator for the second category.

It should propose structured operations rather than arbitrary scripts.

```
{
  "action": "reroute_region",
  "base_revision": "sha256",
  "target_nets": ["NET_A", "NET_B"],
  "authorized_rips": ["object-uuid"],
  "allow_component_moves": false,
  "max_runtime_seconds": 120,
  "required_checks": [
    "connectivity",
    "drc",
    "routing_contract",
    "protected_objects"
  ]
}
```

The deterministic executor validates the proposal and runs it on an isolated candidate.

The AI does not get to decide whether its own solution is safe.

## 9.4 Reduce the number of independent repair implementations

Your November 19 redesign identified 25 repair entry points, multiple path-search implementations and 17 functions that count opens.

That is an architectural warning.

A reasonable target would be to consolidate the production flow around five core operations:

| Core operation        | Responsibility                             |
| --------------------- | ------------------------------------------ |
| `measure()`           | Authoritative board facts                  |
| `diagnose()`          | Classify failures and identify blockers    |
| `propose()`           | Generate candidate repair operations       |
| `execute()`           | Isolated, authorized mutation              |
| `verify_and_commit()` | Independent verification and atomic commit |

Different algorithms can remain available behind those operations.

I would prohibit workers from creating another measurement function, independent DRC acceptance rule or standalone repair orchestrator without architectural review.

## 9.5 Make measurements authoritative and reproducible

This deserves a dedicated work item, independent of the router.

For connectivity, derive an authoritative metric from connected components containing required terminals. For each net:

\\[ \text{Connectivity deficit}\_n = \max(0,k_n-1) \\]

Here, \\(k_n\\) is the number of electrically disconnected components containing required terminals.

Track floating copper islands, dangling objects and unnecessary copper separately. They should not inflate or conceal required terminal connectivity.

Keep KiCad's native DRC and connectivity reports as independent validation, and investigate disagreements rather than selecting whichever count looks better.

For every report, record:

```
Board hash
Schematic/netlist hash
Rule hash
Zone-fill state
Tool versions
Measurement algorithm version
Complete / approximate / failed status
```

Your current measurements already illustrate why this matters: the best board is reported as having 22 opens, but the review preview reports 23 unrouted findings. That may be a legitimate difference in definitions, a different board revision or a stale report. The system should be able to explain it automatically.

It should never require the AI to speculate.

# 10. What I would do next on the actual CM5 board

I would temporarily stop spending the majority of development time chasing the remaining 22 opens.

There are more fundamental blockers that are likely causing those opens or will require rerouting after they are corrected.

### Known review findings requiring attention

Selected categories from your supplied review; not a complete breakdown of all 281 findings.

Unrouted

23

Decoupling placement

10

Crystal/RF/clock lengths

9

Power widths

5

Ethernet/PCIe skew

5

RF impedance/layer

4

Datasheet evidence (updated)

3

The 186 unfinished silkscreen findings are excluded because the silk stage has not yet run.

## Phase 1 — Correct electrical feasibility

First, address the 10 decoupling placement violations and nine critical path-length violations.

Do this at the module and floorplan stages. Add the relevant constraints to the placement acceptance gate.

Resolve the remaining datasheet evidence requirements. Confirm the requirements are being loaded from the correct location, not silently replaced by defaults.

Then establish legal outer-layer RF corridors and realistic current-dependent power corridors.

Exit criterion: no known placement-dependent electrical requirement is geometrically impossible.

## Phase 2 — Fix routing ownership

Implement the compiled routing contracts and reject unauthorized class fallback.

Make RF, high-speed pairs, power branches and ordinary nets explicitly different routing tasks.

Add regression fixtures for all the known class downgrade failures.

Exit criterion: no router or repair operation can produce newly committed copper that violates its assigned electrical contract.

## Phase 3 — Rebuild critical routes

Perform paired breakout and critical corridor planning on the corrected placement.

Route and verify PCIe, USB3, Ethernet, RF and high-current paths before unrestricted general routing.

Reserve the space required to tune pairs later.

Exit criterion: critical routes are verified, not merely connected.

## Phase 4 — Close the ordinary board

Use transactional regional rerouting and controlled unlocking to resolve remaining conflicts.

Make the solver escalate from routing to escape reconstruction to local placement changes when necessary.

Exit criterion: zero required disconnected terminals and zero unapproved copper DRC violations.

## Phase 5 — Finish and release

Correct via centering and copper hygiene, finalize tuning, apply thermal solder-mask openings, refill and validate planes, complete silkscreen, and run independent release verification.

The thermal mask operation needs a specific policy: either its eventual geometry is reserved during routing or it is created afterward with a guard proving that it exposes only permitted copper. Merely moving it to the end is insufficient if unrelated tracks already occupy the opening.

Exit criterion: the release artifact passes electrical, mechanical and manufacturing checks against the approved fabrication process.

That should include schematic parity, Gerber and drill consistency, stackup and impedance requirements, solder-mask and paste review, component orientation and mechanical fit.

DRC-clean is necessary for fabrication readiness, but it is not proof that the manufactured board will function correctly. SI, PI and thermal analysis should be applied where the electrical margins justify them, followed by prototype bring-up and interface testing.

# 11. Experiments I would require before declaring the redesign successful

I would establish controlled A/B experiments using immutable copies of the same board and input configuration.

| Experiment                  | A — Existing behavior            | B — Proposed behavior                  | Success metric                                |
| --------------------------- | -------------------------------- | -------------------------------------- | --------------------------------------------- |
| E1: Placement gate          | Current accepted placement       | Electrical feasibility-aware placement | Placement-dependent review failures           |
| E2: Escape ordering         | Independent fanout               | Joint pair/corridor allocation         | Critical pairs completed, time                |
| E3: Closure                 | Existing guard and repair ladder | Transactional multi-net search         | Verified failures removed per minute          |
| E4: Fallback ownership      | Existing router dispatch         | Contract-enforced dispatch             | Unauthorized routes eliminated                |
| E5: Determinism             | Shuffled footprint order         | Canonical evaluation                   | Identical scores and stable candidate ranking |
| E6: Power verification      | Full rail load on branches       | Network-based current model            | Agreement with analytical fixtures            |
| E7: Incremental repair      | Restart full routing             | Resume from checkpoint                 | Equivalent validity, reduced runtime          |
| E8: CI performance          | Current full merge gate          | Parallel and targeted gates            | Feedback time and regression detection        |
| E9: Process isolation       | Shared mutation process          | Fresh-process transaction workers      | Corruption failures, reproducible output      |
| E10: Regression containment | Existing ledger process          | Executable regression registry         | Recurrence of previously verified failures    |

For the placement and routing experiments, use multiple controlled seeds or orderings where the algorithms are nondeterministic. Measure both typical and worst-case results, not just the best run.

Test on more than the CM5 carrier. Otherwise, the worker AIs may inadvertently optimize the tool for this specific board rather than build a generalized system.

Your existing corpus infrastructure is a good foundation for that.

# 12. Development scorecard

I would have your top-level AI track these metrics on every significant board revision.

| KPI                                   | Current supplied baseline | Proposed target                |
| ------------------------------------- | ------------------------- | ------------------------------ |
| Required open connections             | 22 reported               | 0                              |
| Shorts                                | 0                         | 0                              |
| DRC errors                            | 7                         | 0 unapproved                   |
| RF impedance/layer failures           | 4                         | 0                              |
| Decoupling placement failures         | 10                        | 0                              |
| Critical length failures              | 9                         | 0                              |
| Pair skew failures                    | 5                         | 0                              |
| Power width findings                  | 5 rails                   | 0 verified violations          |
| Missing datasheet evidence            | 3 updated                 | 0 required                     |
| Automated, tested ledger resolution   | 34.5%                     | 100% of items marked verified  |
| Manual intervention                   | Recurring                 | 0 routine interventions        |
| Full route runtime                    | 120–180 min               | Under 90 min initially         |
| Pair stage runtime                    | 12–107 min                | Under 30 min on this benchmark |
| Routine development gate              | 20–30 min                 | 3–8 min                        |
| Known verified regression recurrence  | Not quantified            | 0                              |
| Unexplained measurement disagreement  | Present                   | 0                              |
| Unauthorized copper changes committed | Not established           | 0                              |

These targets should be evaluated on matched board revisions, constraints and hardware. Do not count an apparent speed improvement obtained by weakening checks, skipping critical routes or changing the problem being solved.

# 13. What I would explicitly NOT do

There are several tempting directions that I think would consume development resources without addressing the main problems.

- Do not write an entirely new autorouter yet. KRT has already demonstrated substantial connectivity completion.
- Do not keep adding more stages. Make the existing stages composable, transactional and revisitable.
- Do not globally weaken guards. Change transaction boundaries and authorize specific operations.
- Do not optimize for wirelength first. A short electrically invalid trace has no value.
- Do not introduce reinforcement learning yet. First establish trustworthy constraints, measurements and candidate evaluation.
- Do not increase search budgets indefinitely. Classify failures before escalating.
- Do not add board-specific reference designators or net names to generalized repair algorithms. Put genuine board-specific intent in declarative constraints.
- Do not let successful unit tests substitute for successful execution of the production stage.
- Do not blindly optimize toward zero DRC findings. Electrical compliance and manufacturing quality need independent verification.
- Do not treat a one-time manual success as proof the automated pipeline works.

# Final assessment

I think Fluxplace's greatest opportunity is not becoming a better autorouter. It is becoming a better constraint-driven PCB construction and optimization engine that intelligently orchestrates autorouters.

You already have placement algorithms, fanout builders, pair routers, repair ladders, critical-net routing, review infrastructure and multiple routing backends. Much of the necessary functionality exists.

The missing capability is making all those components operate under the same electrical model, transaction system and feedback-driven optimization process.

| Current design philosophy    | Recommended design philosophy                                                        |
| ---------------------------- | ------------------------------------------------------------------------------------ |
| Place, route, repair         | Jointly optimize placement and routing                                               |
| Preserve successful copper   | Preserve electrical compliance                                                       |
| Each operation must improve  | Each committed transaction must improve or serve an explicitly bounded search policy |
| Route completion             | Verified electrical completion                                                       |
| Fixed stage order            | Reusable stages with feedback                                                        |
| AI fixes failures            | AI proposes; deterministic engine executes and validates                             |
| Unit tests prove correctness | Production execution proves correctness                                              |
| Ledger records lessons       | Regression registry enforces lessons                                                 |
| One-pass execution           | One-command autonomous execution                                                     |

That last distinction is particularly important.

One command should be the goal. One pass should not be.

A sophisticated automated PCB design system should be able to backtrack, reconsider placement, negotiate routing resources, explore alternatives, reject candidates and recover without requiring an engineer or AI operator to intervene at every step.

If I were directing the next development cycle, my first five engineering tickets would be:

| Ticket     | Deliverable                                                                    |
| ---------- | ------------------------------------------------------------------------------ |
| FP-ARCH-01 | Centralized compiled routing contracts and router capability enforcement       |
| FP-ARCH-02 | Mandatory electrical placement gate before bulk routing                        |
| FP-ARCH-03 | Isolated transactional executor with multi-net repair and authorized unlocking |
| FP-ARCH-04 | Production-guard integration fixture suite derived from the failure ledger     |
| FP-ARCH-05 | Authoritative board measurement, provenance and cache invalidation system      |

I would prioritize these over further isolated routing fixes.

Review limitations: I inspected the Fluxplace repository documentation, failure ledger and selected implementation files, and compared the architecture with published research and commercial PCB design workflows. I did not execute Fluxplace, inspect the actual CM5 board geometry, independently verify its datasheets or benchmark either router. The proposed runtime budgets, engineering effort and optimization targets are estimates to validate experimentally.

The underlying architectural changes, however, directly address the recurring failures you documented. They should also make every subsequent routing improvement more reliable and easier to prove.
