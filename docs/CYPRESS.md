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
| pairs, groups, templates, height regions | not expressed; pairs only through the weight of their two nets |

After Cypress: `legalise.legalise` (courtyards, real outline, keep-outs, escape allowances), `clusters.reseat` on legal
seats only, then a check that refuses the board unless there are 0 overlaps, nothing outside and no fixed part moved.
A Cypress result is a global placement with overlaps left in it, so the legaliser always has work.

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
