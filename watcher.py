"""
watcher.py — Hardware-as-code file watcher.

Continuously watches the working directory (default: /app, mounted by the user) for two file
families and auto-exports next to each changed file:

- KiCad schematic (.kicad_sch) and PCB (.kicad_pcb) files -> `.svg`, via `kicad-cli`.
- CadQuery model (.py) files under `CQ_MODELS_DIR` -> print-optimized `.stl` + preview `.png`,
  via `model_export.py` (run as a subprocess for crash isolation).

Both file families are handled by the same generic, debounced `JobRunner` (see below): each one
is declared as a small `WatchSpec` (how to recognize a file, how to export it, where its
`.error.txt` lives, how to tell if its output is missing), and the runner takes care of
debouncing bursts of filesystem events, never running two exports of the same file concurrently,
guaranteeing the output eventually reflects the last saved version even if a slow export is still
running when the file changes again, and writing/clearing `.error.txt` based on whether the
export succeeded.

This is one of the 3 processes managed by supervisord in this container image, alongside
`cadquery-server` (CadQuery live 3D preview, which has its own built-in watcher, unrelated to
this file-export watcher) and `kicad-web` (KiCad live schematic/PCB preview, which scans the
working directory on every page load so it always reflects the current file set without needing
to be notified by this script).
"""

from __future__ import annotations

import glob
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable, List, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

WATCH_DIR = os.environ.get("APP_DIR", "/app")
CQ_MODELS_DIR = os.environ.get("CQ_MODELS_DIR", os.path.join(WATCH_DIR, "models"))
DEBOUNCE_SECONDS = float(os.environ.get("WATCHER_DEBOUNCE_SECONDS", "1.0"))
KICAD_EXTENSIONS = (".kicad_sch", ".kicad_pcb")
KICAD_EXPORT_TIMEOUT_SECONDS = 120
MODEL_EXPORT_TIMEOUT_SECONDS = 300  # complex models can take a while to tessellate/export
# Same file/format convention as cq_server's own ModuleManager: one glob pattern per line,
# relative to the models directory, '#' for comments.
MODEL_IGNORE_FILE_NAME = ".cqsignore"
MODEL_EXPORT_SCRIPT = os.environ.get(
    "MODEL_EXPORT_SCRIPT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_export.py"),
)

logging.basicConfig(
    level=logging.INFO,
    format="[watcher] %(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("watcher")


# ---------------------------------------------------------------------------
# Generic, debounced, .error.txt-managing job runner
# ---------------------------------------------------------------------------

@dataclass
class WatchSpec:
    """Declarative description of one file family the watcher handles (e.g. KiCad
    schematics/PCBs, or CadQuery models). `JobRunner` drives debouncing, concurrency control,
    and `.error.txt` management generically from this, for any file family."""

    name: str
    matches: Callable[[str], bool]
    export: Callable[[str], None]  # must raise on failure, return normally on success
    error_path: Callable[[str], str]
    outputs_missing: Callable[[str], bool]


class _PathState:
    """Per-path debounce/concurrency state, guarded by its own lock."""

    __slots__ = ("lock", "timer", "running", "rerun_pending")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.timer: Optional[threading.Timer] = None
        self.running = False
        self.rerun_pending = False


class JobRunner:
    """Coalesces bursts of filesystem events into a single export per file, after a debounce
    delay, and guarantees:

      1. Atomic output writes — delegated to each spec's `export()` (e.g. write-temp-then-rename).
      2. Never two concurrent export runs for the same path — a run in progress is never
         interrupted or duplicated; a retrigger during a run just marks it for a rerun.
      3. Eventual consistency — if a file changes again while its export is still running
         (plausible for slow/complex CadQuery models), exactly one more export runs immediately
         after the current one finishes, so the output converges to the last saved version once
         edits stop for `delay` seconds, even if the user kept editing throughout a slow export.

    Also owns writing/clearing the `.error.txt` file next to each path, based on whether
    `spec.export()` raised or returned normally — this is shared by every `WatchSpec`, so KiCad
    and CadQuery model exports get identical, consistent error reporting and recovery behavior.
    """

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self._states: dict[str, _PathState] = {}
        self._states_lock = threading.Lock()

    def _state_for(self, path: str) -> _PathState:
        with self._states_lock:
            state = self._states.get(path)
            if state is None:
                state = _PathState()
                self._states[path] = state
            return state

    def trigger(self, spec: WatchSpec, path: str) -> None:
        """(Re)start the debounce timer for `path`. Safe to call repeatedly for a burst of
        filesystem events; only the last call in a `delay`-second quiet window actually runs."""

        path = os.path.abspath(path)
        state = self._state_for(path)
        with state.lock:
            if state.timer is not None:
                state.timer.cancel()

            def fire() -> None:
                self._run_or_defer(spec, path, state)

            state.timer = threading.Timer(self._delay, fire)
            state.timer.daemon = True
            state.timer.start()

    def _run_or_defer(self, spec: WatchSpec, path: str, state: _PathState) -> None:
        with state.lock:
            state.timer = None
            if state.running:
                # A run is already in flight for this path: don't start a second one
                # concurrently (that would risk two exports racing to write the same output).
                # Ask it to run once more right after it finishes instead, so the final output
                # still reflects whatever the file looks like by then.
                state.rerun_pending = True
                return
            state.running = True

        self._run_once(spec, path)

        with state.lock:
            state.running = False
            rerun = state.rerun_pending
            state.rerun_pending = False

        if rerun:
            self._run_or_defer(spec, path, state)

    def _run_once(self, spec: WatchSpec, path: str) -> None:
        if not os.path.exists(path):
            return  # file was removed/renamed before the debounce fired

        log.info("[%s] Exporting %s", spec.name, path)
        try:
            spec.export(path)
        except Exception as error:  # noqa: BLE001 - any export failure is reported via .error.txt
            detail = str(error).strip() or repr(error)
            self._write_error_file(spec.error_path(path), detail)
            log.error("[%s] Export failed for %s:\n%s", spec.name, path, detail)
        else:
            self._clear_error_file(spec.error_path(path))
            log.info("[%s] Export OK for %s", spec.name, path)

    @staticmethod
    def _write_error_file(error_path: str, detail: str) -> None:
        content = f"Export failed at {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n{detail}\n"
        tmp_path = error_path + f".tmp-{os.getpid()}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as file:
                file.write(content)
            os.replace(tmp_path, error_path)
        except OSError as io_error:
            log.error("Could not write error file %s: %s", error_path, io_error)

    @staticmethod
    def _clear_error_file(error_path: str) -> None:
        if os.path.exists(error_path):
            try:
                os.remove(error_path)
            except OSError as io_error:
                log.error("Could not remove stale error file %s: %s", error_path, io_error)


# ---------------------------------------------------------------------------
# KiCad schematic/PCB -> SVG
# ---------------------------------------------------------------------------

def is_kicad_file(path: str) -> bool:
    return path.endswith(KICAD_EXTENSIONS)


def svg_path_for(path: str) -> str:
    """Return the expected SVG output path for a given KiCad source file."""
    return os.path.splitext(path)[0] + ".svg"


def error_path_for(path: str) -> str:
    """Return the `.error.txt` path for a given source file (shared convention for every
    `WatchSpec`)."""
    return os.path.splitext(path)[0] + ".error.txt"


def export_svg(path: str) -> None:
    """Convert a single KiCad schematic or PCB file to SVG via kicad-cli. Raises on failure;
    `JobRunner` handles logging and `.error.txt` management."""

    out_dir = os.path.dirname(path) or "."
    if path.endswith(".kicad_sch"):
        cmd = ["kicad-cli", "sch", "export", "svg", path, "-o", out_dir]
    elif path.endswith(".kicad_pcb"):
        cmd = [
            "kicad-cli", "pcb", "export", "svg", path, "-o", out_dir,
            "--layers", "F.Cu,B.Cu,Edge.Cuts,F.SilkS,B.SilkS",
        ]
    else:
        raise ValueError(f"Not a KiCad schematic/PCB file: {path}")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=KICAD_EXPORT_TIMEOUT_SECONDS
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            "kicad-cli not found on PATH; is KiCad installed in this image?"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"kicad-cli timed out exporting {path}") from error

    if result.returncode != 0:
        raise RuntimeError(f"kicad-cli failed (exit {result.returncode}):\n{result.stderr.strip()}")


KICAD_SPEC = WatchSpec(
    name="kicad",
    matches=is_kicad_file,
    export=export_svg,
    error_path=error_path_for,
    outputs_missing=lambda path: not os.path.exists(svg_path_for(path)),
)


# ---------------------------------------------------------------------------
# CadQuery model .py -> STL + PNG
# ---------------------------------------------------------------------------

def load_model_ignore_patterns(models_dir: str) -> List[str]:
    """Read `.cqsignore` (same format cq_server itself uses) from `models_dir`, if present."""

    ignore_file = os.path.join(models_dir, MODEL_IGNORE_FILE_NAME)
    if not os.path.isfile(ignore_file):
        return []

    patterns = []
    with open(ignore_file, encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


def is_model_ignored(path: str, models_dir: str) -> bool:
    patterns = load_model_ignore_patterns(models_dir)
    if not patterns:
        return False

    abs_path = os.path.abspath(path)
    for pattern in patterns:
        if any(os.path.abspath(p) == abs_path for p in glob.glob(os.path.join(models_dir, pattern))):
            return True
    return False


def is_model_file(path: str) -> bool:
    if not path.endswith(".py"):
        return False
    if os.path.basename(path).startswith((".", "_")):
        return False
    if "__pycache__" in os.path.normpath(path).split(os.sep):
        return False

    abs_path = os.path.abspath(path)
    abs_models_dir = os.path.abspath(CQ_MODELS_DIR)

    if not os.path.isdir(CQ_MODELS_DIR):
        # CQ_MODELS_DIR may point at a single model file rather than a directory (cq-server
        # supports both); in that case only that exact file is a model.
        return abs_path == abs_models_dir

    # cq-server's own ModuleManager.get_modules_path() scans the models directory
    # non-recursively (a flat list of .py files); mirror that scope here.
    if os.path.dirname(abs_path) != abs_models_dir:
        return False

    return not is_model_ignored(path, CQ_MODELS_DIR)


def model_stl_path_for(path: str) -> str:
    return os.path.splitext(path)[0] + ".stl"


def model_png_path_for(path: str) -> str:
    return os.path.splitext(path)[0] + ".png"


def export_model(path: str) -> None:
    """Export a CadQuery model .py file to .stl + .png via `model_export.py`, run as a
    subprocess for crash isolation (a bad model can never take down the watcher itself, and a
    fresh interpreter means no CadQuery/OCCT state bleeds between models). Raises with the
    captured error output on failure; `JobRunner` handles logging and `.error.txt` management."""

    cmd = [sys.executable, "-B", MODEL_EXPORT_SCRIPT, path]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=MODEL_EXPORT_TIMEOUT_SECONDS
        )
    except FileNotFoundError as error:
        raise RuntimeError(f"model export script not found: {MODEL_EXPORT_SCRIPT}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"model export timed out after {MODEL_EXPORT_TIMEOUT_SECONDS}s for {path}"
        ) from error

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(detail or f"model_export.py exited with status {result.returncode}")


MODEL_SPEC = WatchSpec(
    name="model",
    matches=is_model_file,
    export=export_model,
    error_path=error_path_for,
    outputs_missing=lambda path: (
        not os.path.exists(model_stl_path_for(path)) or not os.path.exists(model_png_path_for(path))
    ),
)


WATCH_SPECS: List[WatchSpec] = [KICAD_SPEC, MODEL_SPEC]


# ---------------------------------------------------------------------------
# Filesystem event wiring
# ---------------------------------------------------------------------------

class WatchedFileHandler(FileSystemEventHandler):
    def __init__(self, job_runner: JobRunner, specs: List[WatchSpec]) -> None:
        self._job_runner = job_runner
        self._specs = specs

    def _handle(self, path: str) -> None:
        for spec in self._specs:
            if spec.matches(path):
                self._job_runner.trigger(spec, path)
                return  # a path belongs to at most one spec

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)


def run_startup_pass(job_runner: JobRunner, specs: List[WatchSpec], root: str) -> None:
    """Walk `root` once and trigger an export (through the same `JobRunner` used for live
    events, so it gets the same locking/`.error.txt` handling) for any matching file whose spec
    reports missing outputs. Covers files that already existed before the watcher started (e.g.
    freshly checked out from git)."""

    for dirpath, _dirnames, filenames in os.walk(root):
        if "__pycache__" in dirpath.split(os.sep):
            continue
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            for spec in specs:
                if not spec.matches(path):
                    continue
                if spec.outputs_missing(path):
                    log.info("[%s] No output found for %s, exporting on startup", spec.name, path)
                    job_runner.trigger(spec, path)
                break


def main() -> None:
    os.makedirs(WATCH_DIR, exist_ok=True)
    log.info(
        "Watching %s for KiCad file changes and %s for CadQuery model changes (debounce=%.1fs)",
        WATCH_DIR, CQ_MODELS_DIR, DEBOUNCE_SECONDS,
    )

    job_runner = JobRunner(DEBOUNCE_SECONDS)

    run_startup_pass(job_runner, WATCH_SPECS, WATCH_DIR)

    handler = WatchedFileHandler(job_runner, WATCH_SPECS)
    observer = Observer()
    observer.schedule(handler, WATCH_DIR, recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
