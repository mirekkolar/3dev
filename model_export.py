#!/usr/bin/env python3
"""
model_export.py — Standalone CadQuery model exporter.

Given a CadQuery model script (a `.py` file using the same
`from cq_server.ui import ui, show_object` convention as `cq-server`/`cadquery-server`), imports
it fresh, validates the resulting geometry, and exports a print-optimized `.stl` plus a `.png`
preview image next to it (or to explicit paths).

Design notes:
- This script is intentionally standalone and has no knowledge of `.error.txt` or any watcher
  process. It's meant to be usable directly, e.g. `python3 model_export.py models/box.py`,
  including from a CI job (it exits non-zero and prints a full error/traceback to stderr on any
  failure).
- `watcher.py` invokes this as a subprocess per model file on debounced save, and owns all
  error-reporting/state around it (see `watcher.py`'s `WatchSpec`/`JobRunner`).
- Every output file (.stl, .png) is written to a temp file in the destination directory first,
  then atomically renamed into place, so a reader never observes a partial file and a failed run
  never touches a pre-existing, previously-successful output.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile
import traceback
import uuid

DEFAULT_STL_TOLERANCE_MM = float(os.environ.get("STL_TOLERANCE_MM", "0.01"))
DEFAULT_STL_ANGULAR_TOLERANCE_RAD = float(os.environ.get("STL_ANGULAR_TOLERANCE_RAD", "0.05"))
DEFAULT_MAX_SOLIDS_FOR_OVERLAP_CHECK = int(os.environ.get("MODEL_MAX_SOLIDS_FOR_OVERLAP_CHECK", "20"))

PNG_WIDTH = 800
PNG_HEIGHT = 600
PNG_SCALE = 2  # render at 2x pixel density for a crisper preview
PNG_BACKGROUND_COLOR = "#ffffff"
PNG_SVG_OPTIONS = {
    "width": PNG_WIDTH,
    "height": PNG_HEIGHT,
    "marginLeft": 40,
    "marginTop": 40,
    "showAxes": False,
    "strokeColor": (50, 50, 50),
    "hiddenColor": (150, 150, 150),
    "showHidden": True,
    "backgroundColor": PNG_BACKGROUND_COLOR,
}


class ModelExportError(Exception):
    """Raised for any problem that should be reported as an export failure: parse/exec errors,
    missing show_object(), invalid/overlapping geometry, or the STL/PNG write itself failing.
    The message is meant to be detailed enough to stand alone in a watcher's `.error.txt`."""


def load_model_assembly(model_path: str):
    """Import `model_path` as a fresh, uniquely-named module and return its CadQuery Assembly.

    A unique module name is used (rather than the real basename) so this never collides with
    Python's sys.modules caching / importlib.reload semantics — every call gets a truly fresh
    import, with no state bleeding from a previous model exported in the same process."""

    if not os.path.isfile(model_path):
        raise ModelExportError(f"Model file not found: {model_path}")

    module_name = f"_model_export_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, model_path)
    if spec is None or spec.loader is None:
        raise ModelExportError(f"Could not create an import spec for {model_path}")

    module = importlib.util.module_from_spec(spec)

    # Some models import sibling helper modules with plain `import foo`; make that resolvable by
    # putting the model's directory on sys.path for the duration of the import.
    model_dir = os.path.dirname(os.path.abspath(model_path))
    sys.path.insert(0, model_dir)
    sys.modules[module_name] = module
    try:
        try:
            spec.loader.exec_module(module)
        except SyntaxError as error:
            raise ModelExportError(f"Model file failed to parse:\n{traceback.format_exc()}") from error
        except Exception as error:  # noqa: BLE001 - any script error is ours to report in detail
            raise ModelExportError(
                f"Model file raised an exception while executing:\n{traceback.format_exc()}"
            ) from error
    finally:
        sys.modules.pop(module_name, None)
        try:
            sys.path.remove(model_dir)
        except ValueError:
            pass

    ui_instance = getattr(module, "ui", None)
    if ui_instance is None:
        raise ModelExportError(
            "Model file does not define a `ui` object. Add "
            "`from cq_server.ui import ui, show_object` at the top of the script."
        )

    assembly = ui_instance.get_assembly()
    if not assembly.children:
        raise ModelExportError("Model file did not call show_object(); there is no object to export.")

    return assembly


def validate_compound(compound, max_solids_for_overlap_check: int = DEFAULT_MAX_SOLIDS_FOR_OVERLAP_CHECK):
    """Run solid-validity checks on `compound`, raising ModelExportError with details on any
    problem. Returns the list of solids on success (useful for logging/diagnostics)."""

    solids = compound.Solids()
    if not solids:
        raise ModelExportError("Model produced no solid geometry (empty compound).")

    invalid_indices = [i for i, solid in enumerate(solids) if not solid.isValid()]
    if invalid_indices:
        raise ModelExportError(
            f"{len(invalid_indices)} of {len(solids)} solid(s) failed geometry validity check "
            f"(0-based indices: {invalid_indices})."
        )

    if not compound.isValid():
        raise ModelExportError("Overall model geometry failed validity check.")

    if len(solids) > 1:
        if len(solids) > max_solids_for_overlap_check:
            print(
                f"warning: model has {len(solids)} disjoint solids, skipping the pairwise "
                f"overlap check (limit is {max_solids_for_overlap_check}); set "
                "--max-solids-for-overlap-check / MODEL_MAX_SOLIDS_FOR_OVERLAP_CHECK to raise "
                "this.",
                file=sys.stderr,
            )
        else:
            overlapping_pairs = []
            for i in range(len(solids)):
                for j in range(i + 1, len(solids)):
                    intersection = solids[i].intersect(solids[j])
                    if intersection.Solids():
                        overlapping_pairs.append((i, j))
            if overlapping_pairs:
                raise ModelExportError(
                    "Model has overlapping/intersecting disjoint solids at 0-based index pairs: "
                    f"{overlapping_pairs}. Each body should occupy distinct space for a clean "
                    "3D print / STL export."
                )

    return solids


def atomic_write(destination: str, write_fn) -> None:
    """Call `write_fn(tmp_path)` to produce `destination`'s content at a temp path in the same
    directory, then atomically replace `destination` with it. On any failure, remove the temp
    file and leave a pre-existing `destination` untouched."""

    out_dir = os.path.dirname(os.path.abspath(destination)) or "."
    os.makedirs(out_dir, exist_ok=True)
    suffix = os.path.splitext(destination)[1]
    fd, tmp_path = tempfile.mkstemp(dir=out_dir, prefix=".tmp-", suffix=suffix)
    os.close(fd)
    # tempfile.mkstemp() creates the file with restrictive 0600 permissions; left as-is, that
    # mode would carry over through os.replace() into the final output, making it unreadable by
    # anyone but the process that wrote it (typically root inside the container) — e.g. on the
    # host, or by other tools reading the bind-mounted directory. Widen it to a normal
    # world-readable file mode before writing/publishing it.
    os.chmod(tmp_path, 0o644)
    try:
        write_fn(tmp_path)
        os.replace(tmp_path, destination)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def export_stl(compound, destination: str, tolerance_mm: float, angular_tolerance_rad: float) -> None:
    # cadquery.exporters.export()'s STL path (Shape.exportStl) always writes ASCII STL and has
    # no option to change that, so we drive OCP's mesher/writer directly instead — this is the
    # same code Shape.exportStl() itself wraps, just with ASCIIMode explicitly disabled for a
    # much smaller, print-tool-friendly binary STL.
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.StlAPI import StlAPI_Writer

    def write(tmp_path: str) -> None:
        mesh = BRepMesh_IncrementalMesh(compound.wrapped, tolerance_mm, True, angular_tolerance_rad)
        mesh.Perform()
        writer = StlAPI_Writer()
        writer.ASCIIMode = False
        if not writer.Write(compound.wrapped, tmp_path):
            raise ModelExportError("STL writer failed to write the mesh (Write() returned False).")

    atomic_write(destination, write)


def export_png(compound, destination: str) -> None:
    from cadquery import exporters
    import cairosvg

    def write(tmp_path: str) -> None:
        svg_fd, svg_path = tempfile.mkstemp(dir=os.path.dirname(tmp_path) or ".", suffix=".svg")
        os.close(svg_fd)
        try:
            exporters.export(compound, svg_path, "SVG", opt=PNG_SVG_OPTIONS)
            cairosvg.svg2png(
                url=svg_path,
                write_to=tmp_path,
                scale=PNG_SCALE,
                background_color=PNG_BACKGROUND_COLOR,
            )
        finally:
            try:
                os.remove(svg_path)
            except OSError:
                pass

    atomic_write(destination, write)


def default_output_path(model_path: str, extension: str) -> str:
    return os.path.splitext(model_path)[0] + extension


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a CadQuery model script to a print-optimized STL and a PNG preview."
    )
    parser.add_argument("model", help="path to the CadQuery model .py file")
    parser.add_argument("--stl", metavar="PATH", help="output STL path (default: <model>.stl)")
    parser.add_argument("--png", metavar="PATH", help="output PNG path (default: <model>.png)")
    parser.add_argument("--skip-stl", action="store_true", help="don't export STL")
    parser.add_argument("--skip-png", action="store_true", help="don't export PNG")
    parser.add_argument(
        "--stl-tolerance-mm", type=float, default=DEFAULT_STL_TOLERANCE_MM,
        help=f"STL linear tessellation tolerance in mm (default: {DEFAULT_STL_TOLERANCE_MM}, "
             "env: STL_TOLERANCE_MM)",
    )
    parser.add_argument(
        "--stl-angular-tolerance-rad", type=float, default=DEFAULT_STL_ANGULAR_TOLERANCE_RAD,
        help="STL angular tessellation tolerance in radians "
             f"(default: {DEFAULT_STL_ANGULAR_TOLERANCE_RAD}, env: STL_ANGULAR_TOLERANCE_RAD)",
    )
    parser.add_argument(
        "--max-solids-for-overlap-check", type=int, default=DEFAULT_MAX_SOLIDS_FOR_OVERLAP_CHECK,
        help="skip the pairwise disjoint-solid overlap check above this many solids (default: "
             f"{DEFAULT_MAX_SOLIDS_FOR_OVERLAP_CHECK}, env: MODEL_MAX_SOLIDS_FOR_OVERLAP_CHECK)",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    stl_path = args.stl or default_output_path(args.model, ".stl")
    png_path = args.png or default_output_path(args.model, ".png")

    try:
        assembly = load_model_assembly(args.model)
        compound = assembly.toCompound()
        solids = validate_compound(compound, args.max_solids_for_overlap_check)

        if not args.skip_stl:
            export_stl(compound, stl_path, args.stl_tolerance_mm, args.stl_angular_tolerance_rad)
        if not args.skip_png:
            export_png(compound, png_path)
    except ModelExportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - unexpected error; still report in detail and fail loudly
        traceback.print_exc()
        return 1

    outputs = []
    if not args.skip_stl:
        outputs.append(stl_path)
    if not args.skip_png:
        outputs.append(png_path)
    print(f"OK: {len(solids)} solid(s) exported -> {', '.join(outputs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
