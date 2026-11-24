# bdh-supervisor: the only process with the Docker socket. It also provides
# the gateway (`python -m bdh_supervisor.gateway`, uid 10001) and the one-shot
# host firewall helper (`python -m bdh_supervisor.firewall`, NET_ADMIN only).
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/blackdiamondhub/bdh-supervisor" \
      org.opencontainers.image.description="Black Diamond Hub app supervisor" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends iptables \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin app

COPY supervisor/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY supervisor/bdh_supervisor /usr/local/lib/python3.13/site-packages/bdh_supervisor
RUN python -m compileall -q /usr/local/lib/python3.13/site-packages/bdh_supervisor

VOLUME ["/state"]
CMD ["python", "-m", "bdh_supervisor"]
