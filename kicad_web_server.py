"""
kicad_web_server.py — KiCad live web preview server.

Serves a small web UI (backed by the KiCanvas viewer, https://kicanvas.org) that
lets you browse and preview every KiCad schematic (.kicad_sch) and PCB
(.kicad_pcb) file found under the working directory (default: /app). The file
listing is (re)generated on every request, so it always reflects the current
state of the mounted project directory — no explicit "reload" step needed
after the watcher or the user edits a file.

Routes:
  GET /                      -> generated index page listing/embedding all KiCad files
  GET /kicanvas/*            -> vendored KiCanvas static assets (js/css/wasm)
  GET /files/<relative path> -> raw file content from the working directory
                                 (so KiCanvas can fetch .kicad_sch/.kicad_pcb/.kicad_pro files)

This is one of the 3 processes managed by supervisord in this container image,
alongside `cadquery-server` (CadQuery live 3D preview) and `watcher`
(auto SVG conversion of edited KiCad files).
"""

from __future__ import annotations

import html
import os
import posixpath
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.environ.get("APP_DIR", "/app")
VENDOR_DIR = os.environ.get("KICANVAS_VENDOR_DIR", "/opt/kicad-web/vendor/kicanvas")
PORT = int(os.environ.get("KICAD_PORT", "8000"))
HOST = os.environ.get("KICAD_HOST", "0.0.0.0")

KICAD_EXTENSIONS = (".kicad_sch", ".kicad_pcb", ".kicad_pro")


def find_kicad_files(root: str) -> list[str]:
    """Return sorted, root-relative POSIX paths of KiCad files under root."""

    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        # skip hidden/version-control directories
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.endswith(KICAD_EXTENSIONS):
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                found.append(rel.replace(os.sep, "/"))
    return sorted(found)


def render_index(root: str) -> bytes:
    files = find_kicad_files(root)
    sch_pcb_files = [f for f in files if f.endswith((".kicad_sch", ".kicad_pcb"))]

    if sch_pcb_files:
        embeds = "\n".join(
            f'<section class="doc">'
            f'<h2>{html.escape(path)}</h2>'
            f'<kicanvas-embed src="/files/{urllib.parse.quote(path)}" '
            f'controls="full"></kicanvas-embed>'
            f"</section>"
            for path in sch_pcb_files
        )
    else:
        embeds = (
            "<p>No KiCad schematic (.kicad_sch) or PCB (.kicad_pcb) files found yet "
            f"under <code>{html.escape(root)}</code>. Add some and reload this page.</p>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KiCad live preview</title>
<script type="module" src="/kicanvas/kicanvas.js"></script>
<style>
  body {{ font-family: sans-serif; margin: 0; padding: 1rem; background: #1e1e1e; color: #eee; }}
  h1 {{ font-size: 1.2rem; }}
  h2 {{ font-size: 0.95rem; font-weight: normal; opacity: 0.8; }}
  .doc {{ margin-bottom: 2rem; }}
  kicanvas-embed {{ display: block; width: 100%; height: 70vh; background: #131313; }}
</style>
</head>
<body>
<h1>KiCad live preview &mdash; {html.escape(root)}</h1>
{embeds}
</body>
</html>
""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "kicad-web-preview/1.0"

    def log_message(self, fmt, *args):  # quieter, structured logging to stdout
        sys.stdout.write("[kicad-web] " + (fmt % args) + "\n")

    def _safe_join(self, base: str, rel_path: str) -> str | None:
        rel_path = urllib.parse.unquote(rel_path)
        rel_path = posixpath.normpath("/" + rel_path).lstrip("/")
        full = os.path.join(base, rel_path)
        if not os.path.abspath(full).startswith(os.path.abspath(base) + os.sep) and \
                os.path.abspath(full) != os.path.abspath(base):
            return None
        return full

    def _send_file(self, path: str, content_type: str | None = None) -> None:
        if not os.path.isfile(path):
            self.send_error(404, "File not found")
            return
        if content_type is None:
            content_type = guess_content_type(path)
        with open(path, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - required name by BaseHTTPRequestHandler
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            body = render_index(APP_DIR)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/kicanvas/"):
            rel = path[len("/kicanvas/"):]
            full = self._safe_join(VENDOR_DIR, rel)
            if full is None:
                self.send_error(403, "Forbidden")
                return
            self._send_file(full)
            return

        if path.startswith("/files/"):
            rel = path[len("/files/"):]
            full = self._safe_join(APP_DIR, rel)
            if full is None:
                self.send_error(403, "Forbidden")
                return
            self._send_file(full)
            return

        self.send_error(404, "Not found")


def guess_content_type(path: str) -> str:
    if path.endswith(".js"):
        return "text/javascript; charset=utf-8"
    if path.endswith(".css"):
        return "text/css; charset=utf-8"
    if path.endswith(".wasm"):
        return "application/wasm"
    if path.endswith(".svg"):
        return "image/svg+xml"
    if path.endswith((".kicad_sch", ".kicad_pcb", ".kicad_pro")):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


def main() -> None:
    print(f"[kicad-web] serving KiCad live preview for {APP_DIR} on {HOST}:{PORT}", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
