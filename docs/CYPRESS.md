# Placement engine `cypress` (NVIDIA Cypress on the GPU)

[Cypress](https://github.com/NVlabs/Cypress) (Apache-2.0, ISPD 2025) is a DREAMPlace fork for PCBs: analytic global
placement on the GPU. fluxplace can use it as the global placer; everything a board needs beyond that stays in fluxplace.
The default engine is `builtin`. Numbers and the reason for that default: section "Measured" below.

## Select it

    fluxplace place --board B --strategy routable --engine cypress --constraints C --spec S --out O
    fluxplace pipeline B --workdir W --engine cypress ...        # classic: place stage; constructive: floorplan stage

or in the constraints, which `place` and `pipeline` both read:

    [placement]
    engine = "cypress"            # builtin (default) | cypress
    [placement.cypress]           # all optional
    trials = 4                    # Cypress runs; each is legalised and scored by the gate, the best is kept
    unit_mm = 0.1                 # grid
    gap_mm = 0.2                  # courtyard-to-courtyard spacing given to Cypress
    cluster_weight = 8.0          # weight of the IC <-> support part nets
    timeout_s = 900
    params = { net_crossing_weight = 0.1 }     # any Cypress/DREAMPlace parameter
    # intent-aware flow only (an [intent] table is present; section "With an [intent] table"):
    macros = true                 # an IC + its support parts as ONE cell: true | false | "auto" (trial 0 with, trial 1 without, then the better)
    feedback = true               # judge every trial with the gate and inflate what is crowded for the next one
    near_weight = 1.0             # x the `near` item's weight x a factor from its budget
    pair_weight = 2.0             # x the class weight of both nets of a pair, and of the partner-part ties
    block_share = 0.5             # a zone is a blockage when it concerns at least this share of the movable area on a side
    reserve_gain = 1.0            # x the escape-reservation margin of fine-pitch parts
    inflate_step_mm = 0.4   inflate_max_mm = 1.6   inflate_share = 0.12      # feedback: per round, cap, share of the parts
    blocked_pin_cost = 40.0   soft_intent_cost = 20.0                        # the trial score beside PLAN_SCORE

The tool is found from `--cypress-home`, then `$CYPRESS_HOME`, then `~/tools/Cypress*` (a checkout with a built
`install/dreamplace`); its interpreter from `--cypress-python`, `$CYPRESS_PYTHON`, `<home>/env`, `<home>/.venv` or
`<home>/../cypress-env`. Not installed, not converged or no legal result: one line says why and the built-in placer runs.
The engine and its version are written to `<out>.place.json` and to the stage record in `state.json` (`engine`).

## What happens (`fluxplace/cypress_io.py`, `fluxplace/placer_cypress.py`)

| board fact | how Cypress gets it |
|---|---|
| courtyard + spacing | one rectangle per part (courtyard box + `gap_mm`), pins at their real offsets |
| fine-pitch escape room | the legaliser's own escape allowance is added to the rectangle |
| side | fixed per part (`F` prefix = bottom); Cypress never changes a side |
| rotation | Cypress may turn a part 0/90/180/270 about the courtyard centre |
| locked parts, anchors, connectors | fixed cells; a through-hole one is fixed on both sides |
| keep-outs, rule areas, non-rectangular outline | fixed cells without pins |
| net classes | `netclass.classify` weights as net weights; weight-0 rails are left out |
| IC support clusters | one extra 2-pin net per support part to its IC pin (`cluster_weight`) |
| pairs, groups, templates, height regions | not expressed; pairs only through the weight of their two nets (with an `[intent]` table: next section) |

## With an `[intent]` table (docs/INTENT.md)

`trials` defaults to 6 here. Nothing board specific: every item comes from the constraints, the spec and the board.

| intent / rule | how Cypress gets it |
|---|---|
| hard items (edge parts, rigid groups, hard `near` chains from a held part) | `intent.prepare` seats and holds them first: fixed cells. What is held is the owner's list (`modules.fixed_set`), as in `floorplan` |
| `region`, swept card area, clear depth in front of an edge part, hard `away` halo of a held part | a pinless fixed blockage on the side it concerns, when it concerns at least `block_share` of the movable area there; a zone for a few parts only (height limit, `refs`, the `away` offenders) cannot be said to Cypress: the legaliser keeps it per part and the gate checks it |
| `near` (chain links, refs + to) | a two-pin net per link, weight = item weight x min(8, board diagonal / (4 x budget per link)) |
| support parts | `macros`: the IC is laid out with its sure same-side support parts by the support seating (`clusters.seat`, isolate) and the cell moves and turns as one body; the others get the two-pin net, weight x min(4, 5 mm / limit) |
| rigid `[placement.group]` of movable parts (intent bodies) | one macro, never split by the legaliser |
| pairs | both nets x `pair_weight`; the two-pad parts in series on the two halves (same footprint) tied pad to pad |
| escape reservations (`escres.needy`) | fine-pitch parts and ICs get the margin whose ring holds one via site per pin that must own an escape |

After each trial: legaliser (macros as rigid units), support re-seat, **support repair** (an IC whose decoupling cap found no
legal seat is laid out with all its support parts as one cell and the legaliser makes the neighbours give way), then the
gate: INTENT rows (a hard row unmet = the trial is **rejected**), `fanout` check, `plan-route`. Score = PLAN_SCORE +
`blocked_pin_cost` x blocked pins + `soft_intent_cost` x unmet soft rows. **Feedback**: parts with a blocked pin, the parts
blamed for it and the parts bordering a plan-route overflow get `inflate_step_mm` more room in the next trial; an overflow
beside a fixed part becomes a blockage. The best accepted trial is written, the INTENT gate runs on the file, and
`<out>.place.json` records `flow`, `params`, every trial (`score`, `plan`, `overflow`, `risk_nets`, `blocked`, `hpwl`,
`intent`, `rejected` / `failed`), GPU seconds; the pipeline stage record carries `engine: "cypress <commit>; trial N of M kept ..."`.
No accepted trial: one line says why and the built-in placer runs.

After Cypress: `legalise.legalise` (courtyards, real outline, keep-outs, escape allowances), `clusters.reseat` on legal
seats only, then a check that refuses the board unless there are 0 overlaps, nothing outside and no fixed part moved.
A Cypress result is a global placement with overlaps left in it, so the legaliser always has work.

## Measured (2026-11-13)
A 337-part, 270-net, 6-layer two-sided board, 26 fixed parts, copper stripped. (b) and (c) start from the same random
scramble of the 311 movable parts; every number comes from the same fluxplace code (`plan-route --ignore-routing`,
`fanout --check-only`, `preroute.run`, `clusters.build`, `legalise.verify`).

| metric | (a) hand placement | (b) builtin `routable` | (c) cypress + legaliser |
|---|---|---|---|
| HPWL all nets / class-weighted, mm | 6601 / 12313 | 8028 / 13892 | 7214 / 12975 |
| courtyard overlaps, parts outside, fixed parts moved | 0, 0, 0 | 0, 0, 0 | 0, 0, 0 |
| moved by the legaliser | - | (inside the placer) | 197 parts, max 22.5 mm, mean 3.8 mm |
| fan-out: required pins blocked (of 826) | 7 | 12 | 37 |
| plan-route PLAN_SCORE / overflow / risk nets | 2038 / 19.8 / 21 | 2688 / 24.2 / 17 | 3346 / 56.8 / 41 |
| 70 pair nets: total / longest, mm | 1537 / 80.2 | 1664 / 69.1 | 1596 / 69.2 |
| 97 decoupling caps to IC pin: mean mm / over limit | 6.2 / 15 | 18.3 / 41 | 5.7 / 25 |
| support parts beyond their limit (gate) | 2 | 0 | 27 |
| gate score (lower is better) / ratsnest crossings | 15887 / 1565 | 17677 / 1847 | 19256 / 2017 |
| run time | - | 219 s, one CPU core | 166 s wall; GPU 51 s for 4 trials |

Two KiCad demo boards, no constraints, same method (gate score, HPWL mm): `video` builtin 67182, 32276 against
cypress 83866, 39263; `kit-dev-coldfire` builtin 26178, 6629 against cypress 21921, 7027. `StickHub` (16 x 40 mm,
full): no legal result from either engine.

Reading: Cypress gives shorter wires than the built-in placer on the main board and puts decoupling caps closer on
average, but it knows nothing about escapes or channels: three times the blocked fan-out pins and twice the planned
overflow. Its speed is not an advantage at this size (12 to 16 GPU seconds a run, and several runs are needed).
A built-in pass after Cypress changed little (gate 19256 to 18894).

## Measured with an `[intent]` table (2026-11-13)
A 339-part two-sided 6-layer board, 29 intent rows (edge modules, a rigid card group, RF `near` chains, an `away`, regions,
hard decoupling), 33 parts seated and held by the intent, same seed board and constraints for both engines, same
measuring code (`intent`, `fanout --check-only`, `plan-route --ignore-routing`, map relations).

| metric | builtin (`floorplan`) + intent | cypress + intent, best of 6 trials | cypress + intent, then `floorplan --start source` |
|---|---|---|---|
| courtyard overlaps / outside | 0 / 0 | 0 / 0 | 0 / 0 |
| INTENT rows met / soft unmet / hard unmet | 29 / 0 / 0 | 29 / 0 / 0 | 29 / 0 / 0 |
| fan-out: required pins blocked (of 875) | 3 | 4 | 4 |
| plan-route PLAN_SCORE / overflow / over-budget mm | 4129 / 12.2 / 135.8 | 4213 / 21.8 / 142.9 | 4241 / 14.2 / 137.8 |
| HPWL all nets / class-weighted, mm | 7583 / 11890 | 8983 / 13650 | 8319 / 12721 |
| 76 decoupling caps: mean mm / over limit (bulk caps included) | 7.7 / 10 | 5.9 / 15 | 6.3 / 15 |
| pair nets, total span mm | 1816 | 2043 | 1922 |
| run time | 555 s, one core | 286 s wall, GPU 93 s | + 226 s |

The cypress column is the best of six 6-trial runs. Their kept trial scores (PLAN_SCORE + 40 x blocked pins + 20 x soft
rows): 4373, 4480, 4787 (through `pipeline --flow constructive`), 4848, 4883, 5020; one 10-trial run: 4514. The built-in
result scores 4249. First trial of a run (no feedback yet): 5114 to 7097; the feedback brings a run down by 15 to 35 %, and
stops helping after about five trials. 52 trials: Cypress converged in all (3 after one re-run at the overflow it settled
at), 3 were rejected by the INTENT gate (a decoupling cap beyond its limit that the repair could not seat).

Two KiCad demo boards, copper stripped, `intent-draft` constraints as written (only `[intent.support]` survives a draft that
is not edited), PLAN_SCORE / overflow / fan-out pins escaped / HPWL mm:
- `kit-dev-coldfire`: builtin `place` 906 / 11.0 / 351 of 469 / 8121 (8 legality issues); builtin `floorplan`
  673 / 15.0 / 352 / 6398; cypress 586 / 10.6 / 352 / 6086 (0 legality issues; 4 of 6 trials accepted, 1 rejected by the
  gate, 1 not legal after the legaliser; one run). INTENT: no hard row unmet with any of them.
- `video` (dense, through-hole): no Cypress trial was accepted (5 not legal after the legaliser, 1 rejected by the gate), so
  the built-in placer ran; its result fails the INTENT gate as well (decoupling caps beyond their limit, as on the input
  board): 17866 / 158.4 / 823 of 1352 / 31825; builtin `floorplan` 18190 / 155.6 / 820 / 31803,
  the same gate failure.

Reading: with the intent the Cypress flow is legal, meets every hard row and lands within 3 % (best run) to 18 % (worst)
of the built-in floorplan on the gate score, but it does not beat it on this board: wires are 12 to 18 % longer, planned
overflow is higher, and the result differs from run to run. Global placement itself is about as short as the built-in
result (7.5 to 7.7 m before legalising against 7.6 m final); the legaliser and the support repair then add 0.5 to 1.5 m.
No intent zone of this board became a Cypress blockage (each concerns a few parts only); they were all kept by the legaliser.
The default engine stays `builtin`.

## Limits found
- Cypress has no keep-outs, regions, groups, pairs, non-rectangular outlines or side choice; fixed cells are in the
  file format but the released code crashed on the first one (no shipped benchmark has any).
- A movable through-hole part is modelled on its own side only.
- Results vary strongly with a few parameters and with the seed, and a run can fail to converge; it then writes the
  start positions. The authors tune each board with a search over many runs. The engine detects a failed run, runs
  `trials` variants and keeps the best legal one.
- `net_crossing_weight = 10` from the shipped example stalls the wirelength term in this build; the engine uses 0.1.

## Install that worked (WSL2 Ubuntu 24.04, RTX 4070 SUPER, driver 616)
No sudo, nothing outside the two directories. About 11.5 GB.

    git clone --depth 1 https://github.com/NVlabs/Cypress.git ~/tools/Cypress          # commit 743b07c
    cd ~/tools/Cypress && git submodule update --init --depth 1 thirdparty/Limbo thirdparty/munkres-cpp thirdparty/pybind11
    git apply /path/to/fluxplace/docs/cypress-build.patch
    conda create -y -p ~/tools/cypress-env --override-channels -c nvidia/label/cuda-11.7.1 -c conda-forge \
        python=3.9 cuda-toolkit=11.7 gcc_linux-64=10 gxx_linux-64=10 cmake=3.24 make boost-cpp=1.74 zlib flex bison \
        cairo pkg-config "numpy<1.24" scipy matplotlib shapely
    E=~/tools/cypress-env
    $E/bin/pip install torch==2.0.1+cu118 torchvision==0.15.2+cu118 "numpy<1.24" requests cairocffi pkgconfig \
        --extra-index-url https://download.pytorch.org/whl/cu118
    $E/bin/pip uninstall -y cmake lit          # torch pulls a pip cmake 4 that shadows the conda one (no FindCUDA)
    conda install -y -p $E --override-channels -c conda-forge --force-reinstall --no-deps cmake=3.24.3
    mkdir build && cd build && export PATH=$E/bin:$PATH CC=$E/bin/x86_64-conda-linux-gnu-gcc CXX=$E/bin/x86_64-conda-linux-gnu-g++
    cmake .. -DCMAKE_INSTALL_PREFIX=$HOME/tools/Cypress/install -DPYTHON_EXECUTABLE=$E/bin/python \
        -DCUDA_TOOLKIT_ROOT_DIR=$E -DCUDA_HOST_COMPILER=$CXX -DCMAKE_PREFIX_PATH=$E -DBOOST_ROOT=$E -DCUDA_ARCH_LIST=8.6
    make -j8 && make install

Why not the documented versions (PyTorch 1.6-1.10, CUDA 11.3): cuFFT before CUDA 11.8 fails on RTX 40 cards
(`CUFFT_INTERNAL_ERROR`), so PyTorch must be a cu118 build, 2.0 or later. `docs/cypress-build.patch` holds the six source
changes that needed: dispatch macros for PyTorch >= 1.13, the CUB namespace for CUDA 11.7, linking cuRAND,
`zero_grad(set_to_none=False)` for PyTorch 2.0, and the fixed-cell index fault. The repo Dockerfile is not usable as
shipped (it clones over SSH with a private key and stops before the build).

Proof of install: `python dreamplace/Placer.py` on the shipped `benchmarks/small-9` (560 parts, 1513 nets) runs
700 iterations in 25 s on the GPU.
