# CadQuery + KiCad — Hardware-as-Code Dev Container

A Docker image for developing electronics projects as code:

- **[CadQuery](https://cadquery.readthedocs.io/)** — write parametric 3D models in Python,
  previewed live in your browser via [`cq-server`](https://github.com/roipoussiere/cadquery-server).
- **[KiCad](https://www.kicad.org/)** — capture schematics/PCBs as plain-text `.kicad_sch` /
  `.kicad_pcb` files, previewed live in your browser via [KiCanvas](https://kicanvas.org).
- A **watcher** that automatically converts any edited (or pre-existing) KiCad schematic/PCB
  file to `.svg` next to it, using `kicad-cli`.

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

### KiCad — http://localhost:8000

Create/edit `.kicad_sch` and `.kicad_pcb` files anywhere under `/app` with KiCad on your host
machine (or any text editor, since KiCad files are S-expression text) — this container does not
run the KiCad GUI. Every file found is listed and rendered read-only in the browser:

![KiCad live schematic/PCB preview](https://raw.githubusercontent.com/mirekkolar/3dev/master/docs/screenshots/kicad-preview.png)

Whenever a `.kicad_sch`/`.kicad_pcb` file is created or changed, the watcher automatically
exports a matching `.svg` file next to it. It also does an initial pass on container startup, so
files that already existed before the watcher started (e.g. freshly checked out from git) get
their `.svg` too, without needing to be touched first.

`kicad-cli` is also available inside the container for any other headless KiCad operations you
want to script (netlist/BOM export, DRC, Gerbers, etc.):

```bash
docker exec <container> kicad-cli sch export svg /app/kicad/example_circuit/example_circuit.kicad_sch
```

## Environment variables

Pass these with `docker run -e VAR=value ...`.

| Variable                    | Default        | Description                                             |
|------------------------------|----------------|-----------------------------------------------------------|
| `CQ_PORT`                    | `5000`         | Port the CadQuery live 3D viewer listens on (in-container). |
| `KICAD_PORT`                 | `8000`         | Port the KiCad live preview server listens on (in-container). |
| `APP_DIR`                    | `/app`         | Working directory watched/served by all 3 services. Should match the container-side path of your bind mount. |
| `CQ_MODELS_DIR`               | `/app/models`  | Directory (or single `.py` file) `cq-server` scans for CadQuery scripts. Must live under `APP_DIR` to be visible in the container. |
| `WATCHER_DEBOUNCE_SECONDS`   | `1.0`          | Quiet period before the watcher re-exports a changed file. |

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

