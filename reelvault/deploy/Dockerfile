# syntax=docker/dockerfile:1.7
# ReelVault Members server image (DESIGN-SAAS.md 2.2, 12.5). One image, two processes (web + worker).
#
# Build from the project folder (the one with pyproject.toml):
#   docker build -f deploy/Dockerfile -t reelvault-members .
# deploy/Dockerfile.dockerignore keeps everything else (your .env, vault/, work/, tests) out of the build.
#
# Stages:  deps (Python packages from the hashed lock) -> models (CED-tiny, whisper small+base, YuNet; baked in)
#          -> runtime (python:3.13-slim + ffmpeg, non-root, /data volume, `reelvault-start`).

ARG PYTHON_IMAGE=python:3.13-slim

# ---------------------------------------------------------------------------------------------- deps
FROM ${PYTHON_IMAGE} AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
COPY deploy/requirements.lock /tmp/requirements.lock
RUN /opt/venv/bin/pip install --require-hashes -r /tmp/requirements.lock
WORKDIR /src
COPY pyproject.toml ./
COPY reelvault ./reelvault
RUN /opt/venv/bin/pip install --no-deps . \
 && /opt/venv/bin/python -c "import reelvault.server.app, reelvault.server.jobs; print('imports ok')"

# ---------------------------------------------------------------------------------------------- models
FROM ${PYTHON_IMAGE} AS models
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates bzip2 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=deps /opt/venv /opt/venv
COPY deploy/fetch_models.sh /tmp/fetch_models.sh
RUN MODELS_DIR=/opt/reelvault/models PYTHON=/opt/venv/bin/python bash /tmp/fetch_models.sh

# ---------------------------------------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="ReelVault Members" \
      org.opencontainers.image.description="Members-only reel research server (web + worker)"
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates sqlite3 zstd tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --create-home --home-dir /home/reelvault reelvault \
 && mkdir -p /data /opt/reelvault/deploy \
 && chown reelvault:reelvault /data

COPY --from=deps /opt/venv /opt/venv
COPY --from=models /opt/reelvault/models /opt/reelvault/models
COPY migrations /opt/reelvault/migrations
# The extension folder is WP2's; the [s] wildcard keeps the build working while it is still empty or missing.
# Docker copies the CONTENTS of the folder, so stage it with the packer script and sort it out after.
COPY deploy/build_extension.py extension-member[s] /opt/reelvault/_stage/
RUN mkdir -p /opt/reelvault/extension-members \
 && mv /opt/reelvault/_stage/build_extension.py /opt/reelvault/deploy/build_extension.py \
 && cp -a /opt/reelvault/_stage/. /opt/reelvault/extension-members/ \
 && rm -rf /opt/reelvault/_stage

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    RV_ROLE=supervise \
    HOST=0.0.0.0 \
    PORT=8000 \
    RV_MODELS_DIR=/opt/reelvault/models \
    HF_HOME=/opt/reelvault/models/whisper \
    HF_HUB_OFFLINE=1 \
    RV_MIGRATIONS_DIR=/opt/reelvault/migrations \
    RV_EXTENSION_DIR=/opt/reelvault/extension-members \
    RV_BUILD_EXTENSION=/opt/reelvault/deploy/build_extension.py

USER reelvault
WORKDIR /home/reelvault
VOLUME ["/data"]
EXPOSE 8000

# web/supervise: /healthz answers ok (and the worker heartbeat is fresh); worker: heartbeat under 3 minutes old.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["reelvault-start", "health"]

# tini reaps child processes and forwards SIGTERM; reelvault-start runs web (uvicorn) + worker and restarts them.
ENTRYPOINT ["tini", "--"]
CMD ["reelvault-start"]
