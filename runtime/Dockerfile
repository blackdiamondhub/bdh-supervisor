# bdh-app-runtime: the base image of every hub app image.
#
# Apps are not installed here. The central app builder (bdh-sdk's
# builder/app.Dockerfile, run by bdh-app-catalog) builds FROM this image by
# digest and adds the app's wheel and hash-locked dependencies on top.
# /opt/bdh/constraints.txt pins what this image owns; /opt/bdh/check.py
# fails the app build if the app layer changed any of it. The app's SQLite
# database and media live in /data; everything else is read-only at run time.
FROM python:3.13-slim

ARG HUB_API=2
LABEL org.opencontainers.image.source="https://github.com/blackdiamondlodge/bdh-supervisor" \
      org.opencontainers.image.description="Black Diamond Hub app runtime base (hub API ${HUB_API})" \
      org.opencontainers.image.licenses="MIT" \
      org.blackdiamondlodge.bdh.runtime="1" \
      org.blackdiamondlodge.bdh.hub_api="${HUB_API}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1 \
    PIP_ROOT_USER_ACTION=ignore \
    HOME=/tmp \
    BDH_HUB_API=${HUB_API} \
    BDH_DATA_DIR=/data \
    BDH_PORT=8000 \
    BDH_STATIC_ROOT=/tmp/bdh-static

COPY runtime/requirements.txt /tmp/requirements.txt
COPY runtime/check.py /opt/bdh/check.py
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && pip list --format=freeze > /opt/bdh/constraints.txt \
    && python -m compileall -q -j0 /usr/local/lib/python3.13 /opt/bdh \
    && python /opt/bdh/check.py base \
    && python -c "import bdh_sdk.plugins as p; assert p.API_VERSION == ${HUB_API}, p.API_VERSION" \
    && echo "${HUB_API}" > /opt/bdh/hub_api \
    && chmod -R a+rX,go-w /opt/bdh \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown 10001:10001 /data \
    && chmod 0750 /data

USER 10001:10001
WORKDIR /tmp
EXPOSE 8000

# Migrate the app's database, then serve it with uvicorn (one worker). The
# supervisor sets the entrypoint, user and environment explicitly anyway.
ENTRYPOINT ["python", "-m", "bdh_sdk.runtime"]
