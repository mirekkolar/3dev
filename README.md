# CadQuery + KiCad — Hardware-as-Code Dev Container

A Docker image for developing electronics projects as code:

- **[CadQuery](https://cadquery.readthedocs.io/)** — write parametric 3D models in Python,
  previewed live in your browser via [`cq-server`](https://github.com/roipoussiere/cadquery-server).
- **[KiCad](https://www.kicad.org/)** — capture schematics/PCBs as plain-text `.kicad_sch` /
  `.kicad_pcb` files, previewed live in your browser via [KiCanvas](https://kicanvas.org).
- A **watcher** that automatically converts any edited (or pre-existing) KiCad schematic/PCB
  file to `.svg` next to it (using `kicad-cli`), and any edited (or pre-existing) CadQuery model
  `.py` file to a print-optimized `.stl` plus a preview `.png` next to it.

All three run continuously inside the container, managed by `supervisord`. You edit files with
your own editor/IDE on the host — nothing needs to run there except the bind-mounted project
directory.

## Usage

```bash
docker run --rm -it \
  -p 5000:5000 -p 8000:8000 \
  -v "$(pwd):/app" \
  mirekkolar/3dev
```

This mounts the current directory to `/app` and exposes both live-preview servers on the host.
(To build the image yourself instead of pulling it, run `docker build -t mirekkolar/3dev .`
from this repository first.)

### CadQuery — http://localhost:5000

Write a `.py` file under `/app/models` (see [Environment variables](#environment-variables) to
change this path) that uses `show_object()`:

```python
import cadquery as cq
from cq_server.ui import ui, show_object

model = cq.Workplane("XY").box(10, 10, 10)
show_object(model, name="my_part")
```

Select it from the dropdown to render it live:

![CadQuery live 3D preview](https://raw.githubusercontent.com/mirekkolar/3dev/master/docs/screenshots/cadquery-preview.png)

#### Automatic STL/PNG export

Whenever a model `.py` file directly inside `CQ_MODELS_DIR` is created or changed, the watcher
automatically exports:

- `<model>.stl` — a print-optimized, binary STL (fine tessellation tolerance by default; see
  `STL_TOLERANCE_MM`/`STL_ANGULAR_TOLERANCE_RAD` below).
- `<model>.png` — a static preview image of the model.

next to the model file. It also does an initial pass on container startup, so pre-existing
models (e.g. freshly checked out from git) get their `.stl`/`.png` too, without needing to be
touched first.

Before exporting, the model is validated:
- it must import/execute without errors and call `show_object()`;
- every resulting solid, and the model as a whole, must pass a geometry validity check;
- if the model produces multiple disjoint solids (e.g. an assembly of separate parts), each
  pair of solids is also checked to make sure they don't overlap/intersect each other — this
  check is skipped (with a warning) above `MODEL_MAX_SOLIDS_FOR_OVERLAP_CHECK` solids, since it's
  `O(n²)` in the number of disjoint bodies.

If any of this fails, a `<model>.error.txt` file is written next to the model with the full
error/traceback, and any previously-exported `.stl`/`.png` is left untouched (so a broken edit
never destroys your last good export). Fixing the model and saving again clears `.error.txt`
automatically. The same `.error.txt` behavior also applies to KiCad SVG export (see below).

Because a complex model's STL/PNG export can take a while, and you might keep editing during
that time, the watcher guarantees it will never run two exports of the same file concurrently,
and that the exported files will eventually catch up to your latest saved version once you stop
editing for `WATCHER_DEBOUNCE_SECONDS`.

You can also run the exporter standalone (e.g. from a CI job, with no watcher/container needed —
just the same Python environment the container image uses, which already has `cadquery`,
`cq_server`, and `cairosvg` installed):

```bash
python3 model_export.py models/my_part.py
# -> models/my_part.stl, models/my_part.png (or a non-zero exit + traceback on stderr)
```

Run `python3 model_export.py --help` for all flags (e.g. `--stl`/`--png` for explicit output
paths, `--skip-stl`/`--skip-png`, or per-run overrides of the tolerance env vars below).

Non-model helper `.py` files in the models directory (e.g. shared constants imported by several
models) can be excluded with an optional `.cqsignore` file in `CQ_MODELS_DIR` — one glob pattern
per line, `#` for comments — the same convention `cq-server` itself uses to exclude files from
its own dropdown.

### KiCad — http://localhost:8000

Create/edit `.kicad_sch` and `.kicad_pcb` files anywhere under `/app` with KiCad on your host
machine (or any text editor, since KiCad files are S-expression text) — this container does not
run the KiCad GUI. Every file found is listed and rendered read-only in the browser:

![KiCad live schematic/PCB preview](https://raw.githubusercontent.com/mirekkolar/3dev/master/docs/screenshots/kicad-preview.png)

Whenever a `.kicad_sch`/`.kicad_pcb` file is created or changed, the watcher automatically
exports a matching `.svg` file next to it. It also does an initial pass on container startup, so
files that already existed before the watcher started (e.g. freshly checked out from git) get
their `.svg` too, without needing to be touched first. Like the CadQuery model export above, a
failed export writes a `<file>.error.txt` (e.g. if `kicad-cli` itself fails) which is cleared on
the next successful export.

`kicad-cli` is also available inside the container for any other headless KiCad operations you
want to script (netlist/BOM export, DRC, Gerbers, etc.):

```bash
docker exec <container> kicad-cli sch export svg /app/kicad/example_circuit/example_circuit.kicad_sch
```

## Development / testing

`tests/test_watcher.py` unit-tests the watcher's debounce/concurrency/`.error.txt` framework
(`JobRunner`, `WatchSpec` matching) against fake export callables — no Docker, CadQuery, or
KiCad required. Run with:

```bash
pip install pytest watchdog
pytest tests/test_watcher.py -v
```

## Environment variables

Pass these with `docker run -e VAR=value ...`.

| Variable                    | Default        | Description                                             |
|------------------------------|----------------|-----------------------------------------------------------|
| `CQ_PORT`                    | `5000`         | Port the CadQuery live 3D viewer listens on (in-container). |
| `KICAD_PORT`                 | `8000`         | Port the KiCad live preview server listens on (in-container). |
| `APP_DIR`                    | `/app`         | Working directory watched/served by all 3 services. Should match the container-side path of your bind mount. |
| `CQ_MODELS_DIR`               | `/app/models`  | Directory (or single `.py` file) `cq-server` scans for CadQuery scripts, and the watcher scans for STL/PNG auto-export. Must live under `APP_DIR` to be visible in the container. |
| `WATCHER_DEBOUNCE_SECONDS`   | `1.0`          | Quiet period before the watcher re-exports a changed file (applies to both KiCad SVG and CadQuery STL/PNG export). |
| `STL_TOLERANCE_MM`            | `0.01`         | Linear tessellation tolerance (mm) for the exported STL — lower is smoother/larger, higher is coarser/smaller. |
| `STL_ANGULAR_TOLERANCE_RAD`   | `0.05`         | Angular tessellation tolerance (radians) for the exported STL. |
| `MODEL_MAX_SOLIDS_FOR_OVERLAP_CHECK` | `20`   | Skip (with a warning) the pairwise disjoint-solid overlap check above this many solids in a model. |

`CQ_PORT`/`KICAD_PORT` only change the port the service listens on *inside* the container — map
it to whichever host port you like with `-p <host-port>:<in-container-port>`, e.g.:

```bash
docker run --rm -it \
  -p 15000:5000 -p 18000:8000 \
  -e CQ_MODELS_DIR=/app/cad \
  -v "$(pwd):/app" \
  mirekkolar/3dev
```

A ready-to-use `docker-compose.yml` is included in this repository as a self-contained example
of the equivalent setup.

