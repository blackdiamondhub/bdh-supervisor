# bdh-app-runtime: the image every hub app container runs.
#
# The app itself is not in the image. The supervisor's install worker (this
# image, `python -m bdh_runtime.install`) puts the app's wheel in the app's
# code volume, mounted read-only at /app; /app/current points at the
# installed version and is on PYTHONPATH. The app's SQLite database and
# media live in its data volume at /data. Everything else is read-only.
FROM python:3.13-slim

ARG HUB_API=2
LABEL org.opencontainers.image.source="https://github.com/blackdiamondlodge/bdh-supervisor" \
      org.opencontainers.image.description="Black Diamond Hub app runtime (hub API ${HUB_API})" \
      org.opencontainers.image.licenses="MIT" \
      bdh.hub_api="${HUB_API}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1 \
    HOME=/tmp \
    PYTHONPATH=/app/current \
    BDH_HUB_API=${HUB_API} \
    BDH_DATA_DIR=/data \
    BDH_PORT=8000 \
    BDH_STATIC_ROOT=/tmp/bdh-static

# PyAV (a uiprotect dependency, ~130 MB of FFmpeg) is removed again: it is
# only needed for Protect talkback/streaming, which no app uses, and the
# cameras app stubs it out (cameras/events.py: _without_av).
COPY runtime/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
    && pip uninstall --yes av \
    && rm /tmp/requirements.txt \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin app \
    && mkdir -p /data /app \
    && chown 10001:10001 /data /app \
    && chmod 0750 /data /app

COPY runtime/bdh_runtime /usr/local/lib/python3.13/site-packages/bdh_runtime
RUN python -m compileall -q /usr/local/lib/python3.13/site-packages/bdh_runtime \
    && python -c "import bdh_sdk, bdh_sdk.plugins as p; assert p.API_VERSION == ${HUB_API}, p.API_VERSION"

USER 10001:10001
WORKDIR /tmp
EXPOSE 8000

# Migrate the app's database, then serve it with uvicorn (one worker).
ENTRYPOINT ["python", "-m", "bdh_sdk.runtime"]
