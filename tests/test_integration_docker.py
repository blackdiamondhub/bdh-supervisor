"""End to end on a real Docker host: install the hello wheel through the
catalog/install workers, run it hardened, talk to it the way the hub does, and
check the isolation and the memory budget.

Needs the images and the wheel built first (see .github/workflows/ci.yml)::

    BDH_DOCKER_TESTS=1 BDH_TEST_RUNTIME_IMAGE=bdh-app-runtime:test \
    BDH_TEST_SUPERVISOR_IMAGE=bdh-supervisor:test \
    BDH_TEST_HELLO_WHEEL=dist/bdh_hello-1.0.0-py3-none-any.whl \
    sudo -E pytest -m docker -s tests/test_integration_docker.py

It changes the host firewall (and removes its rules again), so run it on a
throwaway host or CI runner, as root (or with the docker group plus sudo for
nothing: the firewall helper itself runs in a container).
"""
import asyncio
import hashlib
import io
import json
import os
import socket
import tarfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(os.environ.get("BDH_DOCKER_TESTS") != "1", reason="set BDH_DOCKER_TESTS=1"),
]

RSS_LIMIT_MB = float(os.environ.get("BDH_RSS_LIMIT_MB", "70"))
TOKEN = "5e" * 32
APP = "hello"
SLOT = 7
BROKER_HOST = os.environ.get("BDH_TEST_BROKER_HOST", "172.17.0.1")
BROKER_PORT = int(os.environ.get("BDH_TEST_BROKER_PORT", "18099"))
PROBE_PORT = int(os.environ.get("BDH_TEST_PROBE_PORT", "18098"))
WHEEL_SERVER = "bdh-test-wheels"


class _Recorder(BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):  # noqa: N802
        type(self).hits.append((self.client_address[0], self.path))
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(host, port):
    server = ThreadingHTTPServer((host, port), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class OneWheelCatalog:
    def __init__(self, wheel):
        self.wheel = wheel

    def wheels(self, app_id, version, hub_api):
        return [self.wheel]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    import docker

    from bdh_supervisor import templates as T
    from bdh_supervisor.config import Config
    from bdh_supervisor.docker_driver import DockerDriver
    from bdh_supervisor.models import Wheel

    wheel_path = Path(os.environ["BDH_TEST_HELLO_WHEEL"])
    cfg = Config(
        token="t" * 40,
        state_dir=str(tmp_path_factory.mktemp("state")),
        catalog_url="http://unused.invalid/catalog.json",
        runtime_image=os.environ.get("BDH_TEST_RUNTIME_IMAGE", "bdh-app-runtime:test"),
        supervisor_image=os.environ.get("BDH_TEST_SUPERVISOR_IMAGE", "bdh-supervisor:test"),
        broker_host=BROKER_HOST,
        broker_port=BROKER_PORT,
        firewall="required",
        allow_http=True,
        start_timeout=90,
    )
    client = docker.from_env(version="auto")
    driver = DockerDriver(cfg, client)
    broker = _serve(BROKER_HOST, BROKER_PORT)
    probe = _serve("0.0.0.0", PROBE_PORT)
    try:
        info = driver.prepare()
        assert info["firewall"], "the firewall helper failed; see the supervisor log"

        # A plain http server on the egress bridge stands in for GitHub releases.
        try:
            client.containers.get(WHEEL_SERVER).remove(force=True)
        except docker.errors.NotFound:
            pass
        server = client.containers.create(
            cfg.runtime_image, name=WHEEL_SERVER, entrypoint=["python", "-m", "http.server", "8000", "-d", "/tmp"],
            network=T.EGRESS_NETWORK, labels={"bdh.test": "1"},
        )
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(wheel_path, arcname=wheel_path.name)
        server.put_archive("/tmp", buf.getvalue())
        server.start()
        server.reload()
        ip = server.attrs["NetworkSettings"]["Networks"][T.EGRESS_NETWORK]["IPAddress"]
        sha = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
        catalog = OneWheelCatalog(Wheel(f"http://{ip}:8000/{wheel_path.name}", sha))
        yield {"cfg": cfg, "driver": driver, "client": client, "catalog": catalog, "T": T}
    finally:
        broker.shutdown()
        probe.shutdown()
        _teardown(cfg, driver, client)


def _teardown(cfg, driver, client):
    import docker

    from bdh_supervisor import templates as T

    for step in (lambda: driver.remove(APP, purge_data=True),
                 lambda: client.containers.get(WHEEL_SERVER).remove(force=True),
                 lambda: client.containers.get(T.GATEWAY_NAME).remove(force=True),
                 lambda: driver._run_once(T.firewall_container(cfg, cfg.supervisor_image, "remove"), 60),
                 lambda: client.networks.get(T.EGRESS_NETWORK).remove()):
        try:
            step()
        except (docker.errors.APIError, docker.errors.NotFound) as exc:
            print("teardown:", exc)


def _exec_py(container, code):
    code_ = container.exec_run(["python", "-c", code], user="10001:10001")
    return code_.exit_code, code_.output.decode("utf-8", "replace").strip()


CONNECT = """
import socket
s = socket.socket(); s.settimeout(3)
try:
    s.connect(({host!r}, {port})); print("OPEN")
except OSError as e:
    print("BLOCKED", e)
"""

PROXY_CONNECT = """
import socket
s = socket.create_connection(({host!r}, 3128), timeout=5)
s.sendall(b"CONNECT example.com:443 HTTP/1.1\\r\\nHost: example.com:443\\r\\n\\r\\n")
print(s.recv(200).decode("latin-1"))
"""

FETCH = """
import urllib.request
try:
    with urllib.request.urlopen({url!r}, timeout=15) as r:
        print("HTTP", r.status, r.read(200).decode("utf-8", "replace"))
except Exception as e:
    print("ERROR", getattr(e, "code", ""), e)
"""


def _get(url, path, signed=True):
    from bdh_sdk.identity import signed_headers

    headers = signed_headers(TOKEN, "GET", path, identity={"u": "alice", "n": "Alice"}) if signed else {}
    req = urllib.request.Request(url + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def _put_request(network="none"):
    return {"version": "1.0.0", "token": TOKEN, "slot": SLOT, "network": network, "allow_lan": False,
            "time_zone": "America/Vancouver", "hub_brand": {"name": "Test"}}


async def _installed(manager, app_id, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = manager.get(app_id)
        if rec["status"] in ("installed", "failed"):
            return rec
        await asyncio.sleep(1)
    raise AssertionError(f"install of {app_id} timed out: {manager.get(app_id)}")


async def test_hello_end_to_end(env):
    from bdh_supervisor.manager import Manager

    cfg, driver, client, T = env["cfg"], env["driver"], env["client"], env["T"]
    manager = Manager(cfg, driver, env["catalog"])
    assert await manager.prepare()

    await manager.put(APP, _put_request())
    rec = await _installed(manager, APP)
    assert rec["status"] == "installed", rec["error"]
    assert rec["manifest"]["id"] == APP and rec["manifest"]["network"] == "none"

    started = await manager.start(APP, wait=True)
    assert started["healthy"], driver.logs(APP, 100)
    upstream = started["upstream"]
    assert upstream == f"http://{T.app_ip(cfg, SLOT)}:8000"

    # The hardened template really is what Docker runs.
    attrs = client.api.inspect_container(T.container_name(APP))
    host = attrs["HostConfig"]
    assert attrs["Config"]["User"] == "10001:10001"
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"] and not host.get("CapAdd")
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert host["Memory"] == 96 << 20 and host["MemorySwap"] == 96 << 20
    assert host["PidsLimit"] == cfg.pids_limit
    assert not host.get("Binds") and not host.get("Privileged")
    assert {m["Destination"]: m["RW"] for m in attrs["Mounts"]} == {"/data": True, "/app": False}

    # The hub's view: unsigned requests are refused, signed ones work.
    status, _ = _get(upstream, "/_bdh/health", signed=False)
    assert status == 200
    status, _ = _get(upstream, f"/{APP}/", signed=False)
    assert status in (401, 403)
    for _ in range(25):
        status, body = _get(upstream, f"/{APP}/")
        assert status == 200 and "alice" in body
    status, body = _get(upstream, f"/{APP}/api/visits")
    assert status == 200 and json.loads(body)["count"] >= 25

    # Memory budget (the "RSS gate"): the cgroup's working set after startup
    # and some requests.
    time.sleep(2)
    stats = driver.stats(APP)
    ws_mb = stats["working_set_bytes"] / (1 << 20)
    print(f"\nhello memory: working_set={ws_mb:.1f}MB usage={stats['memory_bytes'] / (1 << 20):.1f}MB "
          f"anon={stats['anon_bytes'] / (1 << 20):.1f}MB pids={stats['pids']}")
    report = os.environ.get("BDH_RSS_REPORT")
    if report:
        Path(report).write_text(json.dumps({**stats, "working_set_mb": round(ws_mb, 1),
                                            "limit_mb": RSS_LIMIT_MB}) + "\n")
    assert ws_mb <= RSS_LIMIT_MB, f"hello uses {ws_mb:.1f}MB, over the {RSS_LIMIT_MB}MB budget"

    # Isolation of a network=none app.
    app = client.containers.get(T.container_name(APP))
    gw = T.gateway_ip(cfg, SLOT, "none")
    _Recorder.hits.clear()
    code, out = _exec_py(app, FETCH.format(url=f"http://{gw}:8099/_broker/ping"))
    assert out.startswith("HTTP 200"), out
    assert ("/_broker/ping" in [p for _, p in _Recorder.hits]), _Recorder.hits
    for host_, port in ((T.host_ip(cfg, SLOT, 1), PROBE_PORT), (BROKER_HOST, BROKER_PORT),
                        (BROKER_HOST, PROBE_PORT), ("1.1.1.1", 443)):
        code, out = _exec_py(app, CONNECT.format(host=host_, port=port))
        assert out.startswith("BLOCKED"), f"{host_}:{port} reachable from a network=none app: {out}"
    _, out = _exec_py(app, PROXY_CONNECT.format(host=gw))
    assert out.startswith("HTTP/1.1 403"), f"the gateway proxied for a network=none app: {out}"

    # Lifecycle.
    assert (await manager.stop(APP))["app_id"] == APP
    assert not driver.state(APP).running
    logs = driver.logs(APP, 50)
    assert logs, "no logs"


async def test_outbound_app_uses_the_egress_proxy(env):
    """Same installed code, run as network=outbound: only proxied public
    http(s) gets out."""
    from bdh_supervisor.models import AppPlan

    cfg, driver, client, T = env["cfg"], env["driver"], env["client"], env["T"]
    plan = AppPlan(app_id=APP, version="1.0.0", slot=SLOT, network="outbound", memory_mb=96, token=TOKEN)
    await asyncio.to_thread(driver.start, plan)
    try:
        for _ in range(120):
            if driver.health(plan):
                break
            time.sleep(0.5)
        else:
            raise AssertionError(driver.logs(APP, 100))
        app = client.containers.get(T.container_name(APP))
        _, out = _exec_py(app, FETCH.format(url="https://api.github.com/zen"))
        assert out.startswith("HTTP 200"), out
        for url in (f"http://{BROKER_HOST}:{PROBE_PORT}/", f"http://{T.host_ip(cfg, SLOT, 1)}:{PROBE_PORT}/",
                    "http://169.254.169.254/latest/meta-data/", "https://1.1.1.1:8443/"):
            _, out = _exec_py(app, FETCH.format(url=url))
            assert not out.startswith("HTTP 200"), f"{url} reachable through the proxy: {out}"
        _, out = _exec_py(app, CONNECT.format(host="1.1.1.1", port=443))
        assert out.startswith("BLOCKED"), out
    finally:
        driver.stop(APP)


def test_host_reaches_apps_only_through_their_upstream():
    """Sanity check that this host is not the LAN: nothing listens on the
    app's port on a public interface."""
    s = socket.socket()
    s.settimeout(1)
    try:
        assert s.connect_ex(("127.0.0.1", 8000)) != 0
    finally:
        s.close()
