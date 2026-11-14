#!/bin/sh
# Regenerate the hashed lock files. Needs network access to PyPI; run it in a
# throwaway container so the result matches the images' Python:
#   docker run --rm -v "$PWD":/src -w /src python:3.13-slim sh scripts/lock.sh
set -eu
pip install --quiet pip-tools
for f in runtime/requirements supervisor/requirements requirements-dev; do
    pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras \
        --output-file "$f.txt" "$f.in"
done
