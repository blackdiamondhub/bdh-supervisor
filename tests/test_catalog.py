import io
import json

import pytest

from bdh_supervisor.catalog import Catalog, CatalogError, find_version

P = "ghcr.io/blackdiamondlodge/"
D1, D2 = "sha256:" + "a" * 64, "sha256:" + "b" * 64
IMG = f"{P}bdh-app-hello@{D1}"
BASE = f"{P}bdh-app-runtime@{D2}"
REL = "https://github.com/blackdiamondlodge/bdh-app-hello/releases/download"


def entry(**kw):
    e = {"version": "1.0.0", "hub_api": 2, "kind": "image", "image": IMG, "base": BASE, "build": "r1"}
    e.update(kw)
    return e


CATALOG = {"schema": 1, "apps": {"hello": {"repo": "blackdiamondlodge/bdh-app-hello", "versions": [
    entry(),
    entry(version="0.9.1", yanked="broken"),
    {"version": "0.9.0", "hub_api": 1, "kind": "wheel", "sha256": "a" * 64,
     "wheel_url": f"{REL}/v0.9.0/bdh_app_hello-0.9.0-py3-none-any.whl"},
]}}}


def one(**kw):
    return {"schema": 1, "apps": {"hello": {"versions": [entry(**kw)]}}}


def test_find_version_returns_the_pinned_image():
    ref = find_version(CATALOG, "hello", "1.0.0", 2, P)
    assert (ref.version, ref.image, ref.base, ref.build, ref.digest) == ("1.0.0", IMG, BASE, "r1", D1)


@pytest.mark.parametrize(("app_id", "version", "match"), [
    ("nope", "1.0.0", "not in the catalog"),
    ("hello", "2.0.0", "not in the catalog"),
    ("hello", "0.9.1", "yanked"),
    ("hello", "0.9.0", "hub API 1"),
])
def test_find_version_errors(app_id, version, match):
    with pytest.raises(CatalogError, match=match):
        find_version(CATALOG, app_id, version, 2, P)


def test_wheel_entries_are_not_runnable():
    with pytest.raises(CatalogError, match="not an image"):
        find_version(one(kind="wheel"), "hello", "1.0.0", 2, P)


def test_unknown_schema_is_rejected():
    with pytest.raises(CatalogError, match="schema"):
        find_version(dict(CATALOG, schema=2), "hello", "1.0.0", 2, P)


@pytest.mark.parametrize("bad", [
    {"image": f"{P}bdh-app-hello:1.0.0"},                     # tag, not digest
    {"image": f"{P}bdh-app-other@{D1}"},                     # another app's repo
    {"image": f"docker.io/evil/bdh-app-hello@{D1}"},         # another registry
    {"image": f"{P}bdh-app-hello@sha256:{'A' * 64}"},        # malformed digest
    {"image": None},
    {"base": f"{P}bdh-app-runtime:hub-api-2"},
    {"base": f"{P}bdh-app-evil@{D2}"},
    {"build": "x y"},
])
def test_bad_entries_are_rejected(bad):
    with pytest.raises(CatalogError):
        find_version(one(**bad), "hello", "1.0.0", 2, P)


def test_prefix_is_configurable():
    cat = one(image=f"localhost:5000/bdh-app-hello@{D1}", base=f"localhost:5000/bdh-app-runtime@{D2}")
    assert find_version(cat, "hello", "1.0.0", 2, "localhost:5000/").image.startswith("localhost:5000/")


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_catalog_load_and_limits():
    urls = []

    def opener(url, timeout):
        urls.append(url)
        return Resp(json.dumps(CATALOG).encode())

    cat = Catalog("https://x.test/catalog.json", opener=opener)
    assert cat.image("hello", "1.0.0", 2).image == IMG
    cat.load()
    assert len(urls) == 1  # cached
    cat.image("hello", "1.0.0", 2)
    assert len(urls) == 2  # installs always re-read
    cat.image("hello", "1.0.0", 2, force=False)
    assert len(urls) == 2

    with pytest.raises(CatalogError, match="https"):
        Catalog("http://x.test/c.json", opener=opener).load()
    assert Catalog("http://x.test/c.json", allow_http=True, opener=opener).load() == CATALOG
    with pytest.raises(CatalogError, match="not set"):
        Catalog("", opener=opener).load()
    with pytest.raises(CatalogError, match="too large"):
        Catalog("https://x.test/c.json", opener=lambda u, timeout: Resp(b" " * (5 << 20))).load()
