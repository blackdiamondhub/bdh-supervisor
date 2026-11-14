import hashlib
import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bdh_runtime import install as I

SHA = "a" * 64


def req(**kw):
    base = {"app_id": "hello", "version": "1.0.0",
            "wheels": [{"url": "https://x.test/bdh_hello-1.0.0-py3-none-any.whl", "sha256": SHA}]}
    base.update(kw)
    return json.dumps(base)


def test_load_request_accepts_valid():
    assert I.load_request(req())["app_id"] == "hello"


@pytest.mark.parametrize("bad", [
    "not json", "[]",
    req(app_id="../x"), req(version="../../etc"), req(wheels=[]),
    req(wheels=[{"url": "http://x.test/a-1-py3-none-any.whl", "sha256": SHA}]),
    req(wheels=[{"url": "https://x.test/a.tar.gz", "sha256": SHA}]),
    req(wheels=[{"url": "https://x.test/a-1-py3-none-any.whl", "sha256": "zz"}]),
    req(wheels=[{"url": "https://x.test/..%2f..%2fa.whl", "sha256": SHA}]),
])
def test_load_request_rejects(bad):
    with pytest.raises(I.InstallError):
        I.load_request(bad)


@pytest.fixture
def app_root(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "APP_ROOT", tmp_path)
    return tmp_path


def stage(root, version, marker):
    d = root / f".staging-{version}"
    d.mkdir()
    (d / "marker").write_text(marker)


def test_promote_switches_current_and_keeps_one_previous(app_root):
    for v in ("1.0.0", "1.1.0", "1.2.0"):
        stage(app_root, v, v)
        I.promote(v)
        assert os.readlink(app_root / "current") == v
        assert (app_root / "current" / "marker").read_text() == v
    assert sorted(p.name for p in app_root.iterdir()) == ["1.1.0", "1.2.0", "current"]


def test_promote_reinstall_same_version(app_root):
    stage(app_root, "1.0.0", "old")
    I.promote("1.0.0")
    stage(app_root, "1.0.0", "new")
    I.promote("1.0.0")
    assert (app_root / "current" / "marker").read_text() == "new"
    assert sorted(p.name for p in app_root.iterdir()) == ["1.0.0", "current"]


@pytest.fixture
def wheel_server(tmp_path):
    content = b"PK not really a wheel"
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "a-1-py3-none-any.whl").write_bytes(content)
    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path / "w"))
    handler.log_message = lambda *a: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/a-1-py3-none-any.whl", hashlib.sha256(content).hexdigest()
    server.shutdown()


def test_download_checks_sha256(wheel_server, tmp_path, monkeypatch):
    monkeypatch.setenv("BDH_ALLOW_HTTP", "1")
    url, sha = wheel_server
    dest = tmp_path / "dl"
    dest.mkdir()
    assert I.download(url, sha, dest).name == "a-1-py3-none-any.whl"
    with pytest.raises(I.InstallError, match="sha256 mismatch"):
        I.download(url, "b" * 64, dest)
    assert not (dest / "a-1-py3-none-any.whl").exists() or I.download(url, sha, dest)


def test_main_usage(monkeypatch):
    assert I.main(["shell"]) == 2
    monkeypatch.setenv("BDH_INSTALL", "{}")
    assert I.main(["fetch"]) == 1


def test_unmet_requirements(tmp_path):
    info = tmp_path / "dep-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: dep\nVersion: 1.0\n")

    class Dist:
        requires = ["pytest", "dep>=1", "Dep<2", "nothere", "dep>=2",
                    "winonly; sys_platform == 'nonexistent'", "extra-dep; extra == 'x'"]

    assert I.unmet_requirements(Dist(), tmp_path) == ["nothere", "dep>=2"]
