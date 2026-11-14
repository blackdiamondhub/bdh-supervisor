"""The app catalog: where an app version's wheel comes from.

The hub only names an app and version; the supervisor looks the wheel up in
the catalog itself (``BDH_CATALOG_URL``, normally
``https://raw.githubusercontent.com/blackdiamondlodge/bdh-app-catalog/main/catalog.json``),
so nothing the hub sends ends up in a download URL. Format (schema 1, see
blackdiamondlodge/bdh-app-catalog)::

    {"schema": 1,
     "apps": {"hello": {"repo": "blackdiamondlodge/bdh-app-hello",
                        "versions": [{"version": "1.0.0", "hub_api": 2,
                                      "wheel_url": "https://github.com/blackdiamondlodge/bdh-app-hello/releases/download/v1.0.0/bdh_app_hello-1.0.0-py3-none-any.whl",
                                      "sha256": "<hex>", "requires": [], ...}]}}}

Only the app's own wheel is installed (``--no-deps``). Its other
requirements (``requires``) must already be in the runtime image; the install
worker's ``verify`` step checks that and fails the install otherwise.
"""
import json
import time
import urllib.parse
import urllib.request

from .models import SHA256_RE, Wheel

MAX_CATALOG_BYTES = 4 * 1024 * 1024
CACHE_SECONDS = 60
SCHEMA = 1


class CatalogError(Exception):
    pass


def _check_url(url, allow_http):
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme != "https" and not (allow_http and parts.scheme == "http"):
        raise CatalogError(f"catalog URLs must be https: {url!r}")
    if not parts.netloc:
        raise CatalogError(f"bad URL: {url!r}")
    return url


def _wheel(url, sha256, repo, version, allow_http):
    if not isinstance(sha256, str) or not SHA256_RE.match(sha256):
        raise CatalogError("catalog entry has a bad sha256")
    if not isinstance(url, str) or not urllib.parse.urlsplit(url).path.endswith(".whl"):
        raise CatalogError("catalog entries must be wheels")
    _check_url(url, allow_http)
    # Same rule as the catalog's validator: wheels come only from the app's
    # own GitHub release. http:// test catalogs (allow_http) are exempt.
    if not allow_http:
        prefix = f"https://github.com/{repo}/releases/download/v{version}/"
        if not isinstance(repo, str) or not url.startswith(prefix):
            raise CatalogError(f"wheel_url must be under {prefix}")
    return Wheel(url, sha256)


def find_version(catalog, app_id, version, hub_api, allow_http=False):
    """The wheels for ``app_id`` ``version`` (just the app's own wheel)."""
    if not isinstance(catalog, dict) or catalog.get("schema") != SCHEMA:
        raise CatalogError(f"unknown catalog schema {catalog.get('schema') if isinstance(catalog, dict) else None!r}")
    apps = catalog.get("apps")
    app = apps.get(app_id) if isinstance(apps, dict) else None
    if not isinstance(app, dict):
        raise CatalogError(f"{app_id} is not in the catalog")
    for entry in app.get("versions") or []:
        if not isinstance(entry, dict) or entry.get("version") != version:
            continue
        if entry.get("yanked"):
            raise CatalogError(f"{app_id} {version} was yanked: {entry['yanked']}")
        if entry.get("hub_api") != hub_api:
            raise CatalogError(f"{app_id} {version} targets hub API {entry.get('hub_api')}, "
                               f"this runtime is {hub_api}")
        return [_wheel(entry.get("wheel_url"), entry.get("sha256"), app.get("repo"), version, allow_http)]
    raise CatalogError(f"{app_id} {version} is not in the catalog")

class Catalog:
    def __init__(self, url, allow_http=False, opener=None):
        self.url = url
        self.allow_http = allow_http
        self.opener = opener or urllib.request.urlopen
        self._cached = None
        self._fetched = 0.0

    def load(self, force=False):
        if not self.url:
            raise CatalogError("BDH_CATALOG_URL is not set")
        if not force and self._cached is not None and time.monotonic() - self._fetched < CACHE_SECONDS:
            return self._cached
        _check_url(self.url, self.allow_http)
        try:
            with self.opener(self.url, timeout=20) as resp:
                raw = resp.read(MAX_CATALOG_BYTES + 1)
        except OSError as exc:
            raise CatalogError(f"cannot fetch the catalog: {exc}") from exc
        if len(raw) > MAX_CATALOG_BYTES:
            raise CatalogError("catalog is too large")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise CatalogError("catalog is not JSON") from exc
        self._cached, self._fetched = data, time.monotonic()
        return data

    def wheels(self, app_id, version, hub_api):
        return find_version(self.load(force=True), app_id, version, hub_api, self.allow_http)
