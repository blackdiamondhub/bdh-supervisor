#!/bin/sh
# Run the Docker integration test: installs, upgrades, rolls back and probes
# the hello app, and enforces the memory budget. Needs scripts/build.sh first.
# The test runs in a container on the host network (and PID namespace, to
# measure PSS, hence also SYS_PTRACE) with the Docker socket, like the supervisor; it adds (and
# removes) host firewall rules.
set -eu
cd "$(dirname "$0")/.."
docker run --rm --network host --pid host --cap-add SYS_PTRACE -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD":/src -w /src \
    -e BDH_DOCKER_TESTS=1 \
    -e BDH_TEST_IMAGES=dist/images.json \
    -e BDH_TEST_SUPERVISOR_IMAGE="${SUPERVISOR_IMAGE:-bdh-supervisor:test}" \
    -e BDH_RSS_LIMIT_MB="${BDH_RSS_LIMIT_MB:-70}" \
    -e BDH_RSS_REPORT=dist/rss.json \
    python:3.13 sh -c '
        pip install --quiet --root-user-action=ignore --require-hashes -r requirements-dev.txt \
        && pip install --quiet --root-user-action=ignore --no-deps \
            "$(grep -o "https://github.com/blackdiamondlodge/bdh-sdk/releases/download/[^ ]*\.whl" runtime/requirements.in)" \
        && python -m pytest -m docker -s -p no:cacheprovider "$@" tests/test_integration_docker.py' sh "$@"
