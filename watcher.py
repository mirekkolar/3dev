"""
watcher.py — Hardware-as-code file watcher.

Continuously watches the working directory (default: /app, mounted by the user)
for KiCad schematic (.kicad_sch) and PCB (.kicad_pcb) file changes and converts
the changed file to SVG next to it, using `kicad-cli`.

This is one of the 3 processes managed by supervisord in this container image,
alongside `cadquery-server` (CadQuery live 3D preview, which has its own
built-in watcher) and `kicad-web` (KiCad live schematic/PCB preview, which
scans the working directory on every page load so it always reflects the
current file set without needing to be notified by this script).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

WATCH_DIR = os.environ.get("APP_DIR", "/app")
DEBOUNCE_SECONDS = float(os.environ.get("WATCHER_DEBOUNCE_SECONDS", "1.0"))
KICAD_EXTENSIONS = (".kicad_sch", ".kicad_pcb")

logging.basicConfig(
    level=logging.INFO,
    format="[watcher] %(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("watcher")


def export_svg(path: str) -> None:
    """Convert a single KiCad schematic or PCB file to SVG via kicad-cli."""

    if not os.path.exists(path):
        return  # file was removed/renamed before the debounce fired

    out_dir = os.path.dirname(path) or "."
    if path.endswith(".kicad_sch"):
        cmd = ["kicad-cli", "sch", "export", "svg", path, "-o", out_dir]
    elif path.endswith(".kicad_pcb"):
        cmd = [
            "kicad-cli", "pcb", "export", "svg", path, "-o", out_dir,
            "--layers", "F.Cu,B.Cu,Edge.Cuts,F.SilkS,B.SilkS",
        ]
    else:
        return

    log.info("Exporting SVG for %s", path)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            log.error("kicad-cli failed for %s:\n%s", path, result.stderr.strip())
        else:
            log.info("SVG export OK for %s", path)
    except FileNotFoundError:
        log.error("kicad-cli not found on PATH; is KiCad installed in this image?")
    except subprocess.TimeoutExpired:
        log.error("kicad-cli timed out exporting %s", path)


def svg_path_for(path: str) -> str:
    """Return the expected SVG output path for a given KiCad source file."""
    return os.path.splitext(path)[0] + ".svg"


def export_missing_svgs(root: str) -> None:
    """Walk `root` and export SVGs for any KiCad schematic/PCB file that doesn't
    already have a corresponding .svg next to it. Intended to run once on startup
    so pre-existing files (checked out before the watcher started) get an SVG
    without needing to be touched first."""

    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if not filename.endswith(KICAD_EXTENSIONS):
                continue
            path = os.path.join(dirpath, filename)
            if os.path.exists(svg_path_for(path)):
                continue
            log.info("No SVG found for %s, exporting on startup", path)
            export_svg(path)


class Debouncer:
    """Coalesces bursts of filesystem events (e.g. editors that save multiple times)
    into a single action per file, after a short quiet period."""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def trigger(self, path: str) -> None:
        with self._lock:
            timer = self._timers.get(path)
            if timer is not None:
                timer.cancel()

            def run():
                with self._lock:
                    self._timers.pop(path, None)
                export_svg(path)

            timer = threading.Timer(self._delay, run)
            timer.daemon = True
            self._timers[path] = timer
            timer.start()


class KicadFileHandler(FileSystemEventHandler):
    def __init__(self, debouncer: Debouncer) -> None:
        self._debouncer = debouncer

    def _handle(self, path: str) -> None:
        if path.endswith(KICAD_EXTENSIONS):
            self._debouncer.trigger(path)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)


def main() -> None:
    os.makedirs(WATCH_DIR, exist_ok=True)
    log.info("Watching %s for KiCad file changes (debounce=%.1fs)", WATCH_DIR, DEBOUNCE_SECONDS)

    export_missing_svgs(WATCH_DIR)

    debouncer = Debouncer(DEBOUNCE_SECONDS)
    handler = KicadFileHandler(debouncer)
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
