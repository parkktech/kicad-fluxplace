# `parts-model`

### 3D model source chain — `fluxplace parts-model REF ... --spec S --lib DIR [--sources model_sources.json] [--board B] [--register] [--force] [--json]`

Order: (1) the project's library and registry, (2) DigiKey media API (manufacturer STEP links, zips opened), (3) EasyEDA/LCSC by LCSC code,
(4) manufacturer templates (`modelchain.MFR_MODEL_TEMPLATES`, empty until a host is measured to serve plain downloads). A file is accepted
only if it is a real STEP (ISO-10303 header, geometry) and its bounding box is plausible against the footprint's courtyard (0.4x..3x per
axis, either orientation). A part with none gets `NO FETCHABLE MODEL` and the list of what was tried (sign-in walls and challenges are
recorded, not worked around); a stand-in is never generated. `--register` copies an accepted model into `--lib` and records it in the
registry; the board is not touched (wire it with the project's model tools).
