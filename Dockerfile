# DBBASIC Object Server — container image.
#
# This is a second deployment path alongside the bare-VM systemd installer
# (scripts/install.sh, docs/single-vm-deployment.md). See
# docs/docker-deployment.md for the full story, especially what ships baked
# into this image versus what must live on a mounted volume.
FROM python:3.12-slim

# curl is added deliberately, only so the HEALTHCHECK below can make a plain
# HTTP call to /health without pulling in a Python HTTP client at build time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root service user, mirroring the `dbbasic` service user that
# scripts/install.sh creates for the bare-VM systemd unit -- the process
# does not run as root in the container either.
RUN useradd --system --create-home --home-dir /home/dbbasic --shell /usr/sbin/nologin dbbasic

WORKDIR /opt/dbbasic-object-server

# Install the runtime dependencies (the `server` extra) in their own layer,
# before the source is copied, so ordinary code changes do not re-download
# them on every rebuild. Keep these pins in sync with pyproject.toml's
# [project.optional-dependencies] server list.
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir "uvicorn>=0.30.0" "websockets>=12.0"

COPY . .
# .dockerignore keeps git history, tests, docs, caches, and any local
# data/objects/ runtime state out of this layer -- see its comments.
RUN python -m pip install --no-cache-dir -e ".[server]" \
    && chmod +x scripts/docker-entrypoint.sh

# Persistent state lives outside the checkout, the same separation
# docs/single-vm-deployment.md keeps between /opt (code) and /var/lib (data).
# These are the paths an operator mounts as volumes; see docker-compose.yml.
ENV DBBASIC_OBJECTS_DIR=/data/objects \
    DBBASIC_DATA_DIR=/data/state \
    DBBASIC_PACKAGES_DIR=/opt/dbbasic-object-server/packages

# Pre-create the mount points and own them as the service user. When Docker
# mounts an empty named volume over one of these paths, it copies this
# layer's ownership into the new volume on first run, so the app never has
# to write into a root-owned directory.
RUN mkdir -p /data/objects /data/state \
    && chown -R dbbasic:dbbasic /data /opt/dbbasic-object-server

USER dbbasic

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8001/health || exit 1

ENTRYPOINT ["scripts/docker-entrypoint.sh"]
# Bind 0.0.0.0, unlike the bare-VM unit's 127.0.0.1 -- there, Caddy runs on
# the same host and reaches uvicorn over loopback; here, the container's own
# network namespace means Docker's port mapping / Coolify's proxy need
# 0.0.0.0 to reach this process at all.
CMD ["uvicorn", "object_server:app", "--host", "0.0.0.0", "--port", "8001"]
