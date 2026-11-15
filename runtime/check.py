"""Build-time checks for bdh-app-runtime and the app images built on it.

``python /opt/bdh/check.py base``
    The base image is consistent: every distribution pinned in
    /opt/bdh/constraints.txt is installed once, at that version, with its
    files matching its RECORD.

``python /opt/bdh/check.py app [--exclude av,...]``
    Run last in an app image build: the base checks again (so the app layer
    changed nothing the base owns), ``pip check`` passes (except for
    requirements on ``--exclude``-d distributions, which the app opted out
    of), exactly one hub app is installed and its spec loads. Writes the
    app's manifest to /opt/bdh/app.json, which the supervisor reads.
"""
import base64
import hashlib
import importlib
import json
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

OPT = Path(__file__).resolve().parent
CONSTRAINTS = OPT / "constraints.txt"
APP_JSON = OPT / "app.json"
GROUP = "blackdiamondhub.plugins"


class CheckError(Exception):
    pass


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def pinned(path=CONSTRAINTS):
    pins = {}
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, version = line.partition("==")
            pins[canonical(name)] = version.strip()
    return pins


def _record_ok(dist):
    """Paths of ``dist``'s files that no longer match its RECORD."""
    bad = []
    for f in dist.files or ():
        if not f.hash or f.hash.mode != "sha256":
            continue
        path = Path(dist.locate_file(f))
        try:
            digest = hashlib.sha256(path.read_bytes()).digest()
        except OSError:
            bad.append(str(f))
            continue
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != f.hash.value:
            bad.append(str(f))
    return bad


def check_base(pins=None, dists=None):
    pins = pinned() if pins is None else pins
    dists = list(metadata.distributions()) if dists is None else dists
    seen = {}
    for dist in dists:
        seen.setdefault(canonical(dist.metadata["Name"]), []).append(dist)
    problems = []
    for name, version in sorted(pins.items()):
        found = seen.get(name, [])
        if len(found) != 1:
            problems.append(f"{name}: {len(found)} installed copies (want 1 at {version})")
            continue
        dist = found[0]
        if dist.version != version:
            problems.append(f"{name}: {dist.version} installed, the base pins {version}")
            continue
        changed = _record_ok(dist)
        if changed:
            problems.append(f"{name}: files changed since install: {', '.join(changed[:5])}")
    if problems:
        raise CheckError("base distributions were modified:\n  " + "\n  ".join(problems))


def pip_check(exclude=(), run=subprocess.run):
    """``pip check``, ignoring missing requirements the app excluded."""
    excluded = {canonical(n) for n in exclude}
    out = run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
    problems = []
    for line in (out.stdout + out.stderr).splitlines():
        line = line.strip()
        if not line or line.startswith("No broken requirements"):
            continue
        m = re.match(r"\S+ \S+ requires ([A-Za-z0-9._-]+), which is not installed\.$", line)
        if m and canonical(m.group(1)) in excluded:
            continue
        problems.append(line)
    if problems:
        raise CheckError("pip check failed:\n  " + "\n  ".join(problems))


def app_manifest(eps=None):
    eps = list(metadata.entry_points(group=GROUP)) if eps is None else eps
    if len(eps) != 1:
        raise CheckError(f"expected exactly one {GROUP} entry point, found {len(eps)}")
    ep = eps[0]
    spec = ep.load()
    if spec.id != ep.name:
        raise CheckError(f"entry point {ep.name!r} loads app {spec.id!r}")
    hub_api = int((OPT / "hub_api").read_text())
    if spec.api_version != hub_api:
        raise CheckError(f"{spec.id} targets hub API {spec.api_version}, this base is {hub_api}")
    # spec.app is a package or a dotted AppConfig path; the smoke test
    # imports the rest once Django is configured.
    module, _, last = spec.app.rpartition(".")
    importlib.import_module(module if module and last[:1].isupper() else spec.app)
    manifest = spec.manifest()
    manifest["version"] = ep.dist.version
    manifest["distribution"] = ep.dist.metadata["Name"]
    return manifest


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("base", "app"):
        print(__doc__, file=sys.stderr)
        return 2
    exclude = []
    if "--exclude" in argv:
        exclude = [n for n in argv[argv.index("--exclude") + 1].split(",") if n]
    try:
        check_base()
        if argv[0] == "app":
            pip_check(exclude)
            manifest = app_manifest()
            APP_JSON.write_text(json.dumps(manifest, sort_keys=True) + "\n")
            print("BDH-MANIFEST " + json.dumps(manifest, sort_keys=True))
    except CheckError as exc:
        print(f"check.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
