"""The app catalog: which image is an app version.

The hub only names an app and version; the supervisor looks the image up in
the catalog itself (``BDH_CATALOG_URL``, normally
``https://raw.githubusercontent.com/blackdiamondlodge/bdh-app-catalog/main/catalog.json``),
so nothing the hub sends ends up in an image reference. Format (schema 1, see
blackdiamondlodge/bdh-app-catalog)::

    {"schema": 1,
     "apps": {"hello": {"repo": "blackdiamondlodge/bdh-app-hello",
                        "versions": [{"version": "1.0.0", "hub_api": 2, "kind": "image",
                                      "image": "ghcr.io/blackdiamondlodge/bdh-app-hello@sha256:<hex>",
                                      "base": "ghcr.io/blackdiamondlodge/bdh-app-runtime@sha256:<hex>",
                                      "build": "<builder run>", ...}]}}}

Images come only from ``<image_prefix>bdh-app-<id>`` (``ghcr.io/blackdiamondlodge/``)
and are always pinned by digest; ``base`` must be a digest of
``<image_prefix>bdh-app-runtime``. Wheel entries (``kind: wheel``, hub API 1)
are not runnable here.
"""
import json
import re
import time
import urllib.parse
import urllib.request

from .models import DIGEST_RE, ImageRef

MAX_CATALOG_BYTES = 4 * 1024 * 1024
CACHE_SECONDS = 60
SCHEMA = 1
BUILD_RE = re.compile(r"^[0-9A-Za-z._:+-]{0,128}$")


class CatalogError(Exception):
    pass


def _check_url(url, allow_http):
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme != "https" and not (allow_http and parts.scheme == "http"):
        raise CatalogError(f"catalog URLs must be https: {url!r}")
    if not parts.netloc:
        raise CatalogError(f"bad URL: {url!r}")
    return url


def app_repo(prefix, app_id):
    return f"{prefix}bdh-app-{app_id}"


def base_repo(prefix):
    return f"{prefix}bdh-app-runtime"


def _pinned(ref, repo, what):
    if not isinstance(ref, str) or "@" not in ref:
        raise CatalogError(f"{what} must be pinned by digest: {ref!r}")
    name, digest = ref.rsplit("@", 1)
    if name != repo or not DIGEST_RE.match(digest):
        raise CatalogError(f"{what} must be {repo}@sha256:<digest>, not {ref!r}")
    return ref


def image_ref(entry, app_id, prefix):
    if entry.get("kind") != "image":
        raise CatalogError(f"{app_id} {entry.get('version')} is not an image (kind {entry.get('kind')!r})")
    build = entry.get("build", "")
    if not isinstance(build, str) or not BUILD_RE.match(build):
        raise CatalogError("catalog entry has a bad build")
    return ImageRef(
        version=entry["version"],
        image=_pinned(entry.get("image"), app_repo(prefix, app_id), "image"),
        base=_pinned(entry.get("base"), base_repo(prefix), "base"),
        build=build,
    )


def find_version(catalog, app_id, version, hub_api, prefix):
    """The :class:`ImageRef` of ``app_id`` ``version``."""
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
                               f"this supervisor runs {hub_api}")
        return image_ref(entry, app_id, prefix)
    raise CatalogError(f"{app_id} {version} is not in the catalog")


class Catalog:
    def __init__(self, url, prefix="ghcr.io/blackdiamondlodge/", allow_http=False, opener=None):
        self.url = url
        self.prefix = prefix
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

    def image(self, app_id, version, hub_api, force=True):
        return find_version(self.load(force=force), app_id, version, hub_api, self.prefix)
