"""The install worker: puts one app version into the app's code volume.

The supervisor runs this in a disposable container made from this image,
never in its own process, with only the app's code volume mounted at /app
(no data volume, no docker socket). It runs in two steps, each in its own
container:

``fetch`` (network: the egress bridge, no app code runs)
    Downloads every wheel in ``BDH_INSTALL`` (the app's wheel plus any extra
    requirement wheels the catalog lists), checks each one's sha256, then
    ``pip install --no-deps --no-index --only-binary=:all: --require-hashes``
    into ``/app/.staging-<version>``. Wheels only, so no build hooks run.

``verify`` (network: none)
    Reads the staged distributions' metadata to find exactly one
    ``blackdiamondhub.plugins`` entry point, named after the app; loads it,
    checks it is that app's ``PluginSpec`` for this hub API, then moves the
    staging directory to ``/app/<version>``, points ``/app/current`` at it
    (atomically) and keeps only the previous version beside it. The last line
    of output is ``BDH-MANIFEST <json>``.

Any failure exits non-zero and leaves ``/app/current`` untouched.
"""
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

APP_ROOT = Path(os.environ.get("BDH_APP_ROOT", "/app"))
MANIFEST_MARK = "BDH-MANIFEST "
INSTALL_ENV = "BDH_INSTALL"
MAX_WHEEL_BYTES = 200 * 1024 * 1024
MAX_WHEELS = 32
DOWNLOAD_TIMEOUT = 60

APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
WHEEL_NAME_RE = re.compile(r"^[A-Za-z0-9_.+!-]+\.whl$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InstallError(Exception):
    pass


def load_request(raw=None):
    """The validated install request from ``BDH_INSTALL``."""
    try:
        req = json.loads(raw if raw is not None else os.environ.get(INSTALL_ENV, ""))
    except ValueError as exc:
        raise InstallError(f"{INSTALL_ENV} is not JSON") from exc
    if not isinstance(req, dict):
        raise InstallError(f"{INSTALL_ENV} is not an object")
    app_id, version = req.get("app_id"), req.get("version")
    if not isinstance(app_id, str) or not APP_ID_RE.match(app_id):
        raise InstallError("bad app_id")
    if not isinstance(version, str) or not VERSION_RE.match(version) or ".." in version:
        raise InstallError("bad version")
    wheels = req.get("wheels", [])
    if not isinstance(wheels, list) or not 1 <= len(wheels) <= MAX_WHEELS:
        raise InstallError(f"wheels must list 1..{MAX_WHEELS} wheels")
    for wheel in wheels:
        if not isinstance(wheel, dict):
            raise InstallError("bad wheel entry")
        if not isinstance(wheel.get("sha256"), str) or not SHA256_RE.match(wheel["sha256"]):
            raise InstallError("bad sha256")
        wheel_filename(wheel.get("url"))
    return {"app_id": app_id, "version": version, "wheels": wheels}


def wheel_filename(url):
    if not isinstance(url, str):
        raise InstallError("bad wheel url")
    parts = urllib.parse.urlsplit(url)
    allow_http = os.environ.get("BDH_ALLOW_HTTP") == "1"
    if parts.scheme != "https" and not (allow_http and parts.scheme == "http"):
        raise InstallError(f"wheel url must be https: {url}")
    if not parts.netloc:
        raise InstallError(f"bad wheel url: {url}")
    name = urllib.parse.unquote(posixpath.basename(parts.path))
    if not WHEEL_NAME_RE.match(name) or "/" in name or "\\" in name:
        raise InstallError(f"not a wheel: {url}")
    return name


def download(url, sha256, dest_dir):
    """Download ``url`` into ``dest_dir`` and check its sha256."""
    dest = Path(dest_dir) / wheel_filename(url)
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp, open(dest, "wb") as out:
        while chunk := resp.read(1 << 16):
            total += len(chunk)
            if total > MAX_WHEEL_BYTES:
                raise InstallError(f"{dest.name} is larger than {MAX_WHEEL_BYTES} bytes")
            digest.update(chunk)
            out.write(chunk)
    if not hmac_equal(digest.hexdigest(), sha256):
        dest.unlink(missing_ok=True)
        raise InstallError(f"sha256 mismatch for {dest.name}")
    return dest


def hmac_equal(a, b):
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())


def staging_dir(version):
    return APP_ROOT / f".staging-{version}"


def fetch(req):
    staging = staging_dir(req["version"])
    shutil.rmtree(staging, ignore_errors=True)
    with tempfile.TemporaryDirectory(prefix="bdh-install-") as tmp:
        lines = []
        for wheel in req["wheels"]:
            path = download(wheel["url"], wheel["sha256"], tmp)
            lines.append(f"{path} --hash=sha256:{wheel['sha256']}")
        reqfile = Path(tmp) / "requirements.txt"
        reqfile.write_text("\n".join(lines) + "\n")
        cmd = [
            sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
            "--only-binary=:all:", "--require-hashes", "--no-cache-dir",
            "--disable-pip-version-check", "--target", str(staging), "-r", str(reqfile),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        sys.stdout.write(result.stdout[-4000:])
        if result.returncode != 0:
            sys.stderr.write(result.stderr[-4000:])
            shutil.rmtree(staging, ignore_errors=True)
            raise InstallError("pip install failed")
    print(f"fetched {req['app_id']} {req['version']} into {staging}")


def plugin_entry_points(path):
    """``(distribution, entry point)`` for every hub plugin installed in ``path``,
    read from the metadata only (nothing is imported)."""
    from importlib.metadata import distributions

    from bdh_sdk.plugins import ENTRY_POINT_GROUP

    found = []
    for dist in distributions(path=[str(path)]):
        for ep in dist.entry_points:
            if ep.group == ENTRY_POINT_GROUP:
                found.append((dist, ep))
    return found


def _packaging():
    try:
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name
    except ImportError:  # pip vendors it; the runtime image always has pip
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.utils import canonicalize_name
    return Requirement, canonicalize_name


def unmet_requirements(dist, staging):
    """``dist``'s requirements that neither the staged wheels nor the runtime
    image (``sys.path``) satisfy. Wheels go in with ``--no-deps``, so anything
    missing here would only fail later, at import time."""
    from importlib.metadata import distributions

    Requirement, canonicalize_name = _packaging()
    installed = {}
    for found in [*distributions(path=[str(staging)]), *distributions()]:
        installed.setdefault(canonicalize_name(found.metadata["Name"] or ""), found.version)
    missing = []
    for raw in dist.requires or []:
        req = Requirement(raw)
        if req.marker is not None and not req.marker.evaluate({"extra": ""}):
            continue
        have = installed.get(canonicalize_name(req.name))
        if have is None or (req.specifier and not req.specifier.contains(have, prereleases=True)):
            missing.append(str(req))
    return missing


def verify(req):
    from bdh_sdk.plugins import API_VERSION, PluginSpec

    app_id, version = req["app_id"], req["version"]
    staging = staging_dir(version)
    if not staging.is_dir():
        raise InstallError(f"{staging} is missing; run fetch first")
    found = plugin_entry_points(staging)
    names = sorted(ep.name for _dist, ep in found)
    if names != [app_id]:
        raise InstallError(f"the wheels must declare exactly one hub plugin named {app_id!r}, found {names}")
    dist, ep = found[0]
    missing = unmet_requirements(dist, staging)
    if missing:
        raise InstallError("requirements not in the runtime image or the catalog wheels: " + ", ".join(missing))

    sys.path.insert(0, str(staging))
    spec = ep.load()
    if not isinstance(spec, PluginSpec) or spec.id != app_id:
        raise InstallError(f"entry point {ep.value!r} is not the PluginSpec for {app_id!r}")
    if spec.api_version != API_VERSION:
        raise InstallError(f"{app_id} targets hub API {spec.api_version}, this runtime is {API_VERSION}")
    manifest = spec.manifest()

    promote(version)
    print(MANIFEST_MARK + json.dumps({"app_id": app_id, "version": version, "manifest": manifest},
                                     separators=(",", ":")))


def promote(version):
    """Make the staged ``version`` current; keep only it and the one before."""
    staging = staging_dir(version)
    final = APP_ROOT / version
    current = APP_ROOT / "current"
    previous = os.readlink(current) if current.is_symlink() else None
    if final.exists():
        if previous == version:
            # Reinstalling the running version: swap through a temporary name.
            old = APP_ROOT / f".old-{version}"
            shutil.rmtree(old, ignore_errors=True)
            final.rename(old)
            staging.rename(final)
            shutil.rmtree(old, ignore_errors=True)
        else:
            shutil.rmtree(final)
            staging.rename(final)
    else:
        staging.rename(final)
    link = APP_ROOT / ".current.tmp"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(version)
    os.replace(link, current)
    keep = {"current", version, previous}
    for entry in APP_ROOT.iterdir():
        if entry.name in keep:
            continue
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        else:
            shutil.rmtree(entry, ignore_errors=True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in ("fetch", "verify"):
        print("usage: python -m bdh_runtime.install fetch|verify", file=sys.stderr)
        return 2
    try:
        req = load_request()
        (fetch if argv[0] == "fetch" else verify)(req)
    except InstallError as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
