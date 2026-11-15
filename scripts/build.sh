#!/usr/bin/env bash
# Build everything the Docker integration test needs, the way CI does:
#
#   BDH_SDK=../bdh-sdk bash scripts/build.sh
#
# - the runtime base and the supervisor images;
# - a local registry (localhost:5000) holding the base and three builds of
#   the hello app made by the SDK's builder (builder/app_artifacts.sh), each
#   pinned by digest: build 1 is the builder's image, builds 2 and 3 only
#   add a label (same layers, new digests: a platform rebuild);
# - dist/images.json with their digest references.
#
# BDH_SDK is a checkout of blackdiamondlodge/bdh-sdk (CI uses the v0 tag).
set -euo pipefail
cd "$(dirname "$0")/.."
sdk=$(cd "${BDH_SDK:?set BDH_SDK to a bdh-sdk checkout}" && pwd)
registry=localhost:5000
prefix=$registry/blackdiamondlodge
runtime=${RUNTIME_IMAGE:-bdh-app-runtime:test}

docker build -q -f runtime/Dockerfile -t "$runtime" . >/dev/null
docker build -q -f supervisor/Dockerfile -t "${SUPERVISOR_IMAGE:-bdh-supervisor:test}" . >/dev/null

if ! docker inspect bdh-test-registry >/dev/null 2>&1; then
    docker run -d --name bdh-test-registry -p 127.0.0.1:5000:5000 registry:2 >/dev/null
fi
for _ in $(seq 30); do curl -fs "http://$registry/v2/" >/dev/null && break; sleep 1; done

push() {  # <local image> <repo:tag> -> repo@sha256:...
    docker tag "$1" "$2" && docker push -q "$2" >/dev/null
    docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$2" | grep "^${2%:*}@" | head -n1
}

base=$(push "$runtime" "$prefix/bdh-app-runtime:test")

rm -rf dist && mkdir -p dist
docker run --rm -v "$PWD":/src -w /src -u "$(id -u):$(id -g)" -e HOME=/tmp python:3.13-slim \
    sh -c 'pip install --quiet --user build && python -m build --wheel --outdir dist examples/hello >/dev/null'
wheel=$(ls dist/bdh_hello-*.whl)
bash "$sdk/builder/app_artifacts.sh" "$wheel" examples/hello/pyproject.toml dist/hello "$base"
version=$(python3 -c 'import json; print(json.load(open("dist/hello/bdh-app.json"))["version"])')

b1=$(push bdh-app-hello:candidate "$prefix/bdh-app-hello:$version")
builds=("$b1")
for n in 2 3; do
    printf 'FROM %s\nLABEL org.blackdiamondlodge.bdh.build="%s"\n' "$b1" "$n" \
        | docker build -q -t "bdh-app-hello:build$n" - >/dev/null
    builds+=("$(push "bdh-app-hello:build$n" "$prefix/bdh-app-hello:$version-build$n")")
done
# Not built on the base at all.
printf 'FROM python:3.13-slim\nLABEL org.blackdiamondlodge.bdh.app=hello\n' \
    | docker build -q --platform linux/amd64 -t bdh-test-foreign - >/dev/null
foreign=$(push bdh-test-foreign "$prefix/bdh-app-hello:foreign")
python3 - "$base" "$version" "$foreign" "${builds[@]}" > dist/images.json <<'EOF'
import json, sys
base, version, foreign, *builds = sys.argv[1:]
print(json.dumps({"base": base, "version": version, "foreign": foreign, "hello": builds}, indent=2))
EOF
# The supervisor must pull the app by digest, so keep none of it locally.
docker rmi -f "bdh-app-hello:build3" "bdh-app-hello:build2" bdh-app-hello:candidate \
    "$prefix/bdh-app-hello:$version-build3" "$prefix/bdh-app-hello:$version-build2" \
    "$prefix/bdh-app-hello:$version" "${builds[@]}" >/dev/null 2>&1 || true
docker rmi "$prefix/bdh-app-hello:foreign" bdh-test-foreign >/dev/null
cat dist/images.json
