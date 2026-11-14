#!/bin/sh
# Run the Docker integration test (installs, runs and probes the hello app,
# enforces the memory budget). Needs the images and wheel from scripts/build.sh.
# The test runs in a container on the host network with the Docker socket,
# like the supervisor; it adds (and removes) host firewall rules.
set -eu
cd "$(dirname "$0")/.."
wheel=$(ls dist/bdh_hello-*.whl | head -n1)
docker run --rm --network host -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD":/src -w /src \
    -e BDH_DOCKER_TESTS=1 \
    -e BDH_TEST_RUNTIME_IMAGE="${RUNTIME_IMAGE:-bdh-app-runtime:test}" \
    -e BDH_TEST_SUPERVISOR_IMAGE="${SUPERVISOR_IMAGE:-bdh-supervisor:test}" \
    -e BDH_TEST_HELLO_WHEEL="$wheel" \
    -e BDH_RSS_LIMIT_MB="${BDH_RSS_LIMIT_MB:-70}" \
    -e BDH_RSS_REPORT=dist/rss.json \
    python:3.13 sh -c '
        pip install --quiet --root-user-action=ignore --require-hashes -r requirements-dev.txt \
        && pip install --quiet --root-user-action=ignore --no-deps \
            "$(grep -o "https://github.com/blackdiamondlodge/bdh-sdk/releases/download/[^ ]*\.whl" runtime/requirements.in)" \
        && python -m pytest -m docker -s -p no:cacheprovider "$@" tests/test_integration_docker.py' sh "$@"