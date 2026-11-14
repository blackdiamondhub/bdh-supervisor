import io
import json

import pytest

from bdh_supervisor.catalog import Catalog, CatalogError, find_version

SHA = "a" * 64
REL = "https://github.com/blackdiamondlodge/bdh-app-hello/releases/download"
CATALOG = {"schema": 1, "apps": {"hello": {"repo": "blackdiamondlodge/bdh-app-hello", "versions": [
    {"version": "1.0.0", "hub_api": 2, "wheel_url": f"{REL}/v1.0.0/bdh_app_hello-1.0.0-py3-none-any.whl",
     "sha256": SHA, "requires": []},
    {"version": "0.9.1", "hub_api": 2, "wheel_url": f"{REL}/v0.9.1/bdh_app_hello-0.9.1-py3-none-any.whl",
     "sha256": SHA, "yanked": "broken"},
    {"version": "0.9.0", "hub_api": 1, "wheel_url": f"{REL}/v0.9.0/bdh_app_hello-0.9.0-py3-none-any.whl",
     "sha256": SHA},
]}}}


def test_find_version_returns_the_app_wheel():
    wheels = find_version(CATALOG, "hello", "1.0.0", 2)
    assert [(w.url, w.sha256) for w in wheels] == [(f"{REL}/v1.0.0/bdh_app_hello-1.0.0-py3-none-any.whl", SHA)]


@pytest.mark.parametrize(("app_id", "version", "match"), [
    ("nope", "1.0.0", "not in the catalog"),
    ("hello", "2.0.0", "not in the catalog"),
    ("hello", "0.9.1", "yanked"),
    ("hello", "0.9.0", "hub API 1"),
])
def test_find_version_errors(app_id, version, match):
    with pytest.raises(CatalogError, match=match):
        find_version(CATALOG, app_id, version, 2)


def test_unknown_schema_is_rejected():
    with pytest.raises(CatalogError, match="schema"):
        find_version(dict(CATALOG, schema=2), "hello", "1.0.0", 2)


def _one(url, sha=SHA, repo="o/a"):
    return {"schema": 1, "apps": {"a": {"repo": repo, "versions": [
        {"version": "1", "hub_api": 2, "wheel_url": url, "sha256": sha}]}}}


@pytest.mark.parametrize(("url", "sha"), [
    ("http://github.com/o/a/releases/download/v1/a-1-py3-none-any.whl", SHA),
    ("file:///etc/a.whl", SHA),
    ("https://github.com/o/a/releases/download/v1/a.tar.gz", SHA),
    ("https://github.com/o/a/releases/download/v1/a-1-py3-none-any.whl", "nothex"),
    ("https://github.com/o/a/releases/download/v1/a-1-py3-none-any.whl", "A" * 64),
    ("https://github.com/o/other/releases/download/v1/a-1-py3-none-any.whl", SHA),
    ("https://github.com/o/a/releases/download/v2/a-1-py3-none-any.whl", SHA),
    ("https://evil.test/o/a/releases/download/v1/a-1-py3-none-any.whl", SHA),
])
def test_bad_entries_are_rejected(url, sha):
    with pytest.raises(CatalogError):
        find_version(_one(url, sha), "a", "1", 2)


def test_http_only_when_allowed():
    cat = _one("http://x.test/a-1-py3-none-any.whl")
    assert find_version(cat, "a", "1", 2, allow_http=True)[0].url.startswith("http://")

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
    assert len(cat.wheels("hello", "1.0.0", 2)) == 1
    cat.load()
    assert len(urls) == 1  # cached
    cat.wheels("hello", "1.0.0", 2)
    assert len(urls) == 2  # installs always re-read

    with pytest.raises(CatalogError, match="https"):
        Catalog("http://x.test/c.json", opener=opener).load()
    with pytest.raises(CatalogError, match="not set"):
        Catalog("", opener=opener).load()
    with pytest.raises(CatalogError, match="too large"):
        Catalog("https://x.test/c.json", opener=lambda u, timeout: Resp(b" " * (5 << 20))).load()
