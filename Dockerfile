# syntax=docker/dockerfile:1
#
# Hardware-as-code development image: CadQuery (3D models) + KiCad (schematics/PCB).
#
# Services (managed by supervisord):
#   - cadquery-server : live 3D model preview, http://localhost:${CQ_PORT}
#   - kicad-web       : live KiCad schematic/PCB preview, http://localhost:${KICAD_PORT}
#   - watcher         : auto-exports edited .kicad_sch/.kicad_pcb files to .svg, and edited
#                        CadQuery model .py files (under CQ_MODELS_DIR) to print-optimized
#                        .stl + preview .png (see watcher.py / model_export.py)
#
FROM cadquery/cadquery-server:latest AS build

ENV DEBIAN_FRONTEND=noninteractive \
    KICANVAS_VENDOR_DIR=/opt/kicad-web/vendor/kicanvas

# --- Upgrade Debian bullseye -> bookworm so a modern KiCad (with kicad-cli) is installable. ---
RUN set -eux; \
    curl -sSLo /tmp/keyring.deb \
        http://deb.debian.org/debian/pool/main/d/debian-archive-keyring/debian-archive-keyring_2023.3+deb12u2_all.deb; \
    dpkg -i /tmp/keyring.deb; \
    rm -f /tmp/keyring.deb; \
    # This is a container image (overlayfs root), so skip the usrmerge package's interactive
    # /usr conversion script (it refuses to run automatically on overlayfs); see usrmerge's
    # postinst script for the exact required marker text.
    echo 'this system will not be supported in the future' > /etc/unsupported-skip-usrmerge-conversion; \
    printf '%s\n' \
        'deb http://deb.debian.org/debian bookworm main' \
        'deb http://deb.debian.org/debian-security bookworm-security main' \
        'deb http://deb.debian.org/debian bookworm-updates main' \
        'deb http://deb.debian.org/debian bookworm-backports main' \
        > /etc/apt/sources.list; \
    apt-get update; \
    apt-get -y dist-upgrade; \
    rm -rf /var/lib/apt/lists/*

# --- Install KiCad (incl. kicad-cli) from bookworm-backports, plus supervisord. ---
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends supervisor ca-certificates curl; \
    apt-get install -y --no-install-recommends -t bookworm-backports \
        kicad kicad-symbols kicad-footprints; \
    rm -rf /var/lib/apt/lists/*

# --- Python watcher dependency (watchdog is not bundled with the base image). ---
RUN pip install --no-cache-dir watchdog

# --- Vendor KiCanvas (static JS viewer for KiCad files) so the container works offline. ---
RUN set -eux; \
    mkdir -p "${KICANVAS_VENDOR_DIR}"; \
    curl -sSL "https://kicanvas.org/kicanvas/kicanvas.js" -o "${KICANVAS_VENDOR_DIR}/kicanvas.js"


# scratching to avoid issues with relative "data" volume declared in cadquery server image
FROM scratch
COPY --from=build / /

# --- App scripts and process manager config directories. ---
RUN mkdir -p /opt/watcher /opt/kicad-web
COPY watcher.py /opt/watcher/watcher.py
COPY model_export.py /opt/watcher/model_export.py
COPY kicad_web_server.py /opt/kicad-web/kicad_web_server.py
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

ENV DEBIAN_FRONTEND=noninteractive \
    APP_DIR=/app \
    CQ_MODELS_DIR=/app/models \
    CQ_PORT=5000 \
    KICAD_PORT=8000 \
    KICAD_CONFIG_HOME=/tmp/kicad_config \
    DISPLAY= \
    KICANVAS_VENDOR_DIR=/opt/kicad-web/vendor/kicanvas \
    KICANVAS_VERSION=nightly \
    STL_TOLERANCE_MM=0.01 \
    STL_ANGULAR_TOLERANCE_RAD=0.05

LABEL org.opencontainers.image.title="cadquery-kicad-dev" \
      org.opencontainers.image.description="Hardware-as-code dev image: CadQuery 3D models + KiCad schematics/PCB, with live web previews and an auto SVG/STL/PNG-export watcher."

WORKDIR /app
VOLUME ["/app"]

EXPOSE 5000 8000

CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
