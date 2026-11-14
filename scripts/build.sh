#!/bin/sh
# Build the images and the hello wheel locally (what CI does):
#   sh scripts/build.sh
set -eu
cd "$(dirname "$0")/.."
docker build -q -f runtime/Dockerfile -t "${RUNTIME_IMAGE:-bdh-app-runtime:test}" . >/dev/null
docker build -q -f supervisor/Dockerfile -t "${SUPERVISOR_IMAGE:-bdh-supervisor:test}" . >/dev/null
rm -rf dist && mkdir -p dist
docker run --rm -v "$PWD":/src -w /src -u "$(id -u):$(id -g)" -e HOME=/tmp python:3.13-slim \
    sh -c 'pip install --quiet --user build && python -m build --wheel --outdir dist examples/hello >/dev/null'
ls dist/*.whl