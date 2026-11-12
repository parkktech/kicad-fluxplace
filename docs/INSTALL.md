# Requirements and install

## Requirements

fluxplace is a **KiCad 10 engineering suite**, not a standalone python package. It
drives KiCad, an autorouter, an office suite and two distributor APIs. Before doing
anything else, run the preflight — it checks every requirement and tells you exactly
what is missing and how to get it:

```
python3 cli.py doctor            # full report
python3 cli.py doctor --install  # install what it can
```

The KiCad plugin runs the same check the first time you invoke it, and offers to
install the missing python packages for you.

| Tier | Requirement | Needed for |
|---|---|---|
| **core** | **KiCad 10.0+** (`pcbnew` + `kicad-cli`) | everything — there is no fallback |
| **core** | numpy | quadratic placement solve, geometry |
| fab | python-docx, openpyxl, Pillow | fab brief, order worksheet, `.xlsx` upload twins, renders |
| fab | LibreOffice (`soffice`) | assembly instructions as PDF — PCBWay rejects `.docx` on that field |
| route | Java 17+ and **freerouting 2.3.0+** | the autorouter. 2.2.4 dies silently on headless jobs — pin 2.3.0 |
| sourcing | `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET`, `MOUSER_API_KEY` | the availability gate and real-STEP 3D fetch |

**Sourcing policy: DigiKey and Mouser, nothing else.** Two APIs that answer
authoritatively, with credentials, and can be held to account for stock and
price. No LCSC/jlcparts, no SnapEDA, no vendor CDNs — the jlcparts index behind
LCSC search has returned HTTP 404 for weeks at a time, mid-project, twice, and a
gate whose answer depends on a third-party mirror being up is not a gate.
JLCPCB and PCBWay still appear throughout as **fabricators** — DFM profiles,
trace floors, stackups, order worksheets. A fab is where the board is made, not
where the parts are bought.

Put the router jar at `~/tools/freerouting-2.3.0.jar`, or point `FREEROUTING_JAR` at it.

### The interpreter trap — read this one

`pcbnew` ships with KiCad, is **not on PyPI**, and is importable only from the
interpreter KiCad installed it for. On Linux/WSL that is `/usr/bin/python3` — *not*
conda, *not* pyenv, *not* a fresh venv.

That interpreter is usually PEP 668 externally-managed, so `pip install` refuses it.
Installing `openpyxl` into a conda python instead appears to work and then fails at
run time, because that python has no `pcbnew`. **This is the single most common way
to break this suite.**

**The fix, and it needs no root.** Create a venv that *inherits* system
site-packages. A plain venv is useless here — it would not have `pcbnew` — but
`--system-site-packages` inherits `pcbnew`, `wx`, `numpy` and `Pillow` from the
system interpreter while giving you a writable `site-packages` that PEP 668 does
not police. One interpreter ends up with everything, and nothing on the system
python is touched:

```
/usr/bin/python3 -m venv --system-site-packages ~/.fluxplace-venv
~/.fluxplace-venv/bin/pip install python-docx openpyxl
~/.fluxplace-venv/bin/python cli.py doctor      # -> All checks passed
```

`fluxplace doctor --install` does exactly this for you, and once the venv exists
it installs into it automatically.

If you would rather use root, the distro packages work too:

```
sudo apt-get install -y python3-docx python3-openpyxl python3-numpy python3-pil
```

### Wider toolchain

These are configured in your MCP/plugin host, not by pip. `doctor` lists them but
cannot autodetect them:

- **MCP `kicad`** — project analysis, ERC/DRC, BOM, netlist, thumbnails
- **MCP `kicad-pro`** — DFM checks, BOM-with-pricing, component-contract verification
- **MCP `kicad-jlcpcb`** — board generation from a netlist spec, JLCPCB fab packaging
- **`kicad-happy`** — datasheets plus the digikey/mouser skills
- **`pcb-designer`** — DFM, stackups, RF layout guidance

## Install

**Manual (dev):** clone this repo into KiCad's 3rd-party plugin directory (or symlink it):

- Linux: `~/.local/share/kicad/10.0/3rdparty/plugins/`
- Windows: `%APPDATA%\kicad\10.0\3rdparty\plugins\`
- macOS: `~/Documents/KiCad/10.0/3rdparty/plugins/`

Restart KiCad → Tools → External Plugins → *fluxplace*. Installed once, it's available in
**every** project. On first run it preflights the requirements above and offers to
install what it can.

**PCM:** add this repo's `metadata.json` as a Plugin & Content Manager repository for
one-click install + updates.
