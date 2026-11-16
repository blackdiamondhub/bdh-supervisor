"""End to end on a real Docker host: install the hello app image from a
registry by digest, run it hardened, talk to it the way the hub does, check
the isolation and the memory budget, then upgrade (platform rebuilds), roll
back, resume an interrupted upgrade and garbage-collect images.

Needs the images built and pushed to a local registry first::

    BDH_SDK=../bdh-sdk bash scripts/build.sh && bash scripts/integration.sh

It changes the host firewall (and removes its rules again), so run it on a
throwaway host or CI runner.
"""
import asyncio
import json
import os
import re
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(os.environ.get("BDH_DOCKER_TESTS") != "1", reason="set BDH_DOCKER_TESTS=1"),
    # The tests share one manager (and its tasks) and run in order.
    pytest.mark.asyncio(loop_scope="module"),
]

RSS_LIMIT_MB = float(os.environ.get("BDH_RSS_LIMIT_MB", "70"))
TOKEN = "5e" * 32
APP = "hello"
SLOT = 7
PREFIX = "localhost:5000/blackdiamondlodge/"
BROKER_HOST = os.environ.get("BDH_TEST_BROKER_HOST", "172.17.0.1")
BROKER_PORT = int(os.environ.get("BDH_TEST_BROKER_PORT", "18099"))
PROBE_PORT = int(os.environ.get("BDH_TEST_PROBE_PORT", "18098"))

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


class RegistryCatalog:
    """The catalog: ``build`` picks which of the pushed hello builds is current."""

    def __init__(self, images):
        self.images = images
        self.build = 0

    def image(self, app_id, version, hub_api, force=True):
        from bdh_supervisor.models import ImageRef

        assert app_id == APP and version == self.images["version"] and hub_api == 2
        return ImageRef(version, self.images["hello"][self.build], self.images["base"], f"build{self.build + 1}")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    import docker

    from bdh_supervisor import templates as T
    from bdh_supervisor.config import Config
    from bdh_supervisor.docker_driver import DockerDriver
    from bdh_supervisor.manager import Manager

    images = json.loads(Path(os.environ.get("BDH_TEST_IMAGES", "dist/images.json")).read_text())
    cfg = Config(
        token="t" * 40,
        state_dir=str(tmp_path_factory.mktemp("state")),
        catalog_url="http://unused.invalid/catalog.json",
        image_prefix=PREFIX,
        attestation="off",
        supervisor_image=os.environ.get("BDH_TEST_SUPERVISOR_IMAGE", "bdh-supervisor:test"),
        broker_host=BROKER_HOST,
        broker_port=BROKER_PORT,
        firewall="required",
        allow_http=True,
        start_timeout=90,
    )
    client = docker.from_env(version="auto")
    driver = DockerDriver(cfg, client)
    catalog = RegistryCatalog(images)
    broker = _serve(BROKER_HOST, BROKER_PORT)
    probe = _serve("0.0.0.0", PROBE_PORT)
    try:
        info = driver.prepare()
        assert info["firewall"], "the firewall helper failed; see the supervisor log"
        yield {"cfg": cfg, "driver": driver, "client": client, "catalog": catalog, "images": images, "T": T,
               "manager": Manager(cfg, driver, catalog)}
    finally:
        broker.shutdown()
        probe.shutdown()
        _teardown(cfg, driver, client)


def _teardown(cfg, driver, client):
    import docker

    from bdh_supervisor import templates as T

    for step in (lambda: driver.remove(APP, purge_data=True),
                 lambda: client.volumes.get(T.snapshot_volume(APP)).remove(force=True),
                 lambda: driver.gc(APP, set()),
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


# --- What a malicious app's code would try from inside its container --------

ESCAPE_PROBE = """
import json, os, stat
socks = []
for root, dirs, files in os.walk("/"):
    if root == "/" :
        dirs[:] = [d for d in dirs if d not in ("proc", "sys", "dev")]
    for name in files:
        path = os.path.join(root, name)
        try:
            if stat.S_ISSOCK(os.lstat(path).st_mode):
                socks.append(path)
        except OSError:
            pass
status = dict(l.split(":", 1) for l in open("/proc/self/status").read().splitlines() if ":" in l)
writes = {}
for d in ("/", "/etc", "/usr/local/lib", "/opt/bdh", os.path.dirname(os.__file__), "/var/tmp", "/data", "/tmp"):
    try:
        with open(os.path.join(d, ".evil"), "w") as f:
            f.write("x")
        os.unlink(os.path.join(d, ".evil"))
        writes[d] = "ok"
    except OSError as e:
        writes[d] = e.strerror
print(json.dumps({
    "sockets": socks,
    "docker_env": {k: v for k, v in os.environ.items() if k.startswith("DOCKER")},
    "uid": os.getuid(), "cap_eff": status["CapEff"].strip(), "cap_prm": status["CapPrm"].strip(),
    "no_new_privs": status["NoNewPrivs"].strip(), "writes": writes,
}))
"""

TMP_EXEC = """
import os, subprocess
exe = "/tmp/evil.sh"
with open(exe, "w") as f:
    f.write("#!/bin/sh\\necho pwned\\n")
os.chmod(exe, 0o755)
try:
    print(subprocess.run([exe], capture_output=True, text=True, timeout=5).stdout.strip() or "ran")
except OSError as e:
    print("BLOCKED", e.strerror)
"""

FORK_BOMB = """
import json, os, signal, time
cg = "/sys/fs/cgroup/"
def read(name):
    try:
        return open(cg + name).read().strip()
    except OSError:
        return None
leader = os.fork()
if leader == 0:
    os.setsid()
    try:
        open("/proc/self/oom_score_adj", "w").write("1000")
    except OSError:
        pass
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            os.fork()  # parent and child both keep forking
        except OSError:
            time.sleep(0.05)
    os._exit(0)
peak = 0
for _ in range(50):
    time.sleep(0.1)
    peak = max(peak, int(read("pids.current") or 0))
events = read("pids.events")
os.killpg(leader, signal.SIGKILL)
os.waitpid(leader, 0)
print(json.dumps({"peak": peak, "max": read("pids.max"), "events": events}))
"""

MEMORY_HOG = """
import sys
open("/proc/self/oom_score_adj", "w").write("1000")
hog = []
for mb in range(1, 1025):
    hog.append(bytearray(1 << 20))  # zero-filled, so really resident
    print(mb, flush=True)
print("SURVIVED")
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


def _put_request(version, network="none"):
    return {"version": version, "token": TOKEN, "slot": SLOT, "network": network, "allow_lan": False,
            "time_zone": "America/Vancouver", "hub_brand": {"name": "Test"}}


async def _settled(manager, app_id, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = manager.get(app_id)
        if rec["status"] in ("installed", "failed") and not rec.get("upgrade"):
            await asyncio.sleep(0)
            if not manager.tasks:
                return rec
        await asyncio.sleep(1)
    raise AssertionError(f"install of {app_id} timed out: {manager.get(app_id)}")


def _memory(client, name):
    """RSS and PSS summed over the container's processes (needs the host PID
    namespace; ``None`` without it)."""
    rss = pss = 0
    top = client.api.top(name)
    pid_col = top["Titles"].index("PID")
    try:
        for proc in top["Processes"]:
            for line in Path(f"/proc/{proc[pid_col]}/smaps_rollup").read_text().splitlines():
                key, _, value = line.partition(":")
                if key in ("Rss", "Pss"):
                    kb = int(value.split()[0])
                    rss, pss = (rss + kb, pss) if key == "Rss" else (rss, pss + kb)
    except OSError:
        return None, None
    return rss / 1024, pss / 1024


def _visits(upstream):
    status, body = _get(upstream, f"/{APP}/api/visits")
    assert status == 200, status
    return json.loads(body)["count"]


def _visit(upstream, n):
    for _ in range(n):
        status, body = _get(upstream, f"/{APP}/")
        assert status == 200 and "alice" in body


def _local_digests(client, cfg):
    repo = f"{cfg.image_prefix}bdh-app-{APP}"
    return {d for img in client.api.images(name=repo) for d in img.get("RepoDigests") or []
            if d.startswith(repo + "@")}


async def test_hello_end_to_end(env):
    cfg, driver, client, T, manager = env["cfg"], env["driver"], env["client"], env["T"], env["manager"]
    images = env["images"]
    assert await manager.prepare()

    await manager.put(APP, _put_request(images["version"]))
    rec = await _settled(manager, APP)
    assert rec["status"] == "installed", rec["error"]
    assert rec["image"] == images["hello"][0] and rec["base"] == images["base"]
    assert rec["manifest"]["id"] == APP and rec["manifest"]["network"] == "none"

    started = await manager.start(APP, wait=True)
    assert started["healthy"], driver.logs(APP, 100)
    upstream = started["upstream"]
    assert upstream == f"http://{T.app_ip(cfg, SLOT)}:8000"

    # The hardened template really is what Docker runs, whatever the image says.
    attrs = client.api.inspect_container(T.container_name(APP))
    host = attrs["HostConfig"]
    assert attrs["Config"]["User"] == "10001:10001"
    assert attrs["Config"]["Entrypoint"] == ["python", "-m", "bdh_sdk.runtime"]
    assert attrs["Config"]["Image"] == images["hello"][0]
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"] and not host.get("CapAdd")
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert host["Memory"] == 96 << 20 and host["MemorySwap"] == 96 << 20
    assert host["PidsLimit"] == cfg.pids_limit
    assert not host.get("Binds") and not host.get("Privileged")
    assert {m["Destination"]: m["RW"] for m in attrs["Mounts"]} == {"/data": True}

    # The hub's view: unsigned requests are refused, signed ones work.
    status, _ = _get(upstream, "/_bdh/health", signed=False)
    assert status == 200
    status, _ = _get(upstream, f"/{APP}/", signed=False)
    assert status in (401, 403)
    _visit(upstream, 25)
    assert _visits(upstream) >= 25

    # Memory budget (the "RSS gate"): the cgroup's working set after startup
    # and some requests; RSS/PSS over the container's processes for the record.
    time.sleep(2)
    stats = driver.stats(APP)
    ws_mb = stats["working_set_bytes"] / (1 << 20)
    rss_mb, pss_mb = _memory(client, T.container_name(APP))
    print(f"\nhello memory: working_set={ws_mb:.1f}MB usage={stats['memory_bytes'] / (1 << 20):.1f}MB "
          f"anon={stats['anon_bytes'] / (1 << 20):.1f}MB rss={rss_mb}MB pss={pss_mb}MB pids={stats['pids']}")
    report = os.environ.get("BDH_RSS_REPORT")
    if report:
        Path(report).write_text(json.dumps({
            **stats, "working_set_mb": round(ws_mb, 1), "limit_mb": RSS_LIMIT_MB,
            "rss_mb": rss_mb and round(rss_mb, 1), "pss_mb": pss_mb and round(pss_mb, 1),
        }) + "\n")
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
    assert driver.logs(APP, 50), "no logs"


async def test_outbound_app_uses_the_egress_proxy(env):
    """Same image, run as network=outbound: only proxied public http(s) gets out."""
    from bdh_supervisor.models import AppPlan

    cfg, driver, client, T = env["cfg"], env["driver"], env["client"], env["T"]
    plan = AppPlan(app_id=APP, version=env["images"]["version"], image=env["images"]["hello"][0], slot=SLOT,
                   network="outbound", memory_mb=96, token=TOKEN)
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
        # Any status from GitHub proves the TLS tunnel got out (it rate-limits shared
        # CI IPs with 403); a refused CONNECT is a URLError with no status code.
        assert re.match(r"(HTTP|ERROR) \d{3}\b", out), out
        for url in (f"http://{BROKER_HOST}:{PROBE_PORT}/", f"http://{T.host_ip(cfg, SLOT, 1)}:{PROBE_PORT}/",
                    "http://169.254.169.254/latest/meta-data/", "https://1.1.1.1:8443/"):
            _, out = _exec_py(app, FETCH.format(url=url))
            assert not out.startswith("HTTP 200"), f"{url} reachable through the proxy: {out}"
        _, out = _exec_py(app, CONNECT.format(host="1.1.1.1", port=443))
        assert out.startswith("BLOCKED"), out
    finally:
        driver.stop(APP)


def _send(url, path, headers):
    req = urllib.request.Request(url + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


async def test_a_malicious_app_stays_in_its_box(env):
    """What an evil app's code can do inside its (network=none) container, and
    what someone who reaches the app port without the hub can do to it."""
    from bdh_sdk.identity import HEADER_IDENTITY, encode_identity, signed_headers

    cfg, driver, client, T, manager = env["cfg"], env["driver"], env["client"], env["T"], env["manager"]
    upstream = (await manager.start(APP, wait=True))["upstream"]
    app = client.containers.get(T.container_name(APP))
    try:
        # No Docker or supervisor socket (or any other socket file), no capabilities.
        code, out = _exec_py(app, ESCAPE_PROBE)
        assert code == 0, out
        probe = json.loads(out.splitlines()[-1])
        assert probe["sockets"] == [], probe["sockets"]
        assert probe["docker_env"] == {}
        assert probe["uid"] == 10001
        assert int(probe["cap_eff"], 16) == 0 and int(probe["cap_prm"], 16) == 0, probe
        assert probe["no_new_privs"] == "1"
        # The root filesystem is read-only; only /data and the /tmp tmpfs take writes.
        writes = probe["writes"]
        assert writes.pop("/data") == "ok" and writes.pop("/tmp") == "ok", writes
        assert all(v != "ok" for v in writes.values()), f"wrote outside /data and /tmp: {writes}"
        # ...and /tmp is noexec.
        _, out = _exec_py(app, TMP_EXEC)
        assert out.startswith("BLOCKED"), f"ran a binary from /tmp: {out}"
        # Nor the Docker API over TCP.
        for host_, port in ((BROKER_HOST, 2375), (BROKER_HOST, 2376), (T.gateway_ip(cfg, SLOT, "none"), 2375)):
            _, out = _exec_py(app, CONNECT.format(host=host_, port=port))
            assert out.startswith("BLOCKED"), f"{host_}:{port} reachable: {out}"

        # Fork bomb: the pids limit holds and the app (and host) carry on.
        code, out = _exec_py(app, FORK_BOMB)
        bomb = json.loads(out.splitlines()[-1])
        print(f"\nfork bomb: {bomb}")
        assert bomb["max"] == str(cfg.pids_limit), bomb
        assert 0 < bomb["peak"] <= cfg.pids_limit, bomb
        assert bomb["events"] and int(bomb["events"].split()[-1]) > 0, f"the bomb never hit the limit: {bomb}"
        for _ in range(60):
            if driver.stats(APP)["pids"] < cfg.pids_limit // 2:
                break
            time.sleep(0.5)
        app.reload()
        assert app.status == "running"
        assert _get(upstream, f"/{APP}/")[0] == 200

        # Memory hog: OOM-killed at the container's limit, not the host's.
        code, out = _exec_py(app, MEMORY_HOG)
        lines = out.splitlines()
        assert "SURVIVED" not in lines and code == 137, (code, lines[-3:])
        reached = int(lines[-1]) if lines and lines[-1].isdigit() else 0
        print(f"memory hog killed at ~{reached}MB")
        assert reached <= 96, f"allocated {reached}MB past the 96MB limit"
        app.reload()
        assert app.status == "running", "the hog took the app's server down with it"
        assert _get(upstream, f"/{APP}/")[0] == 200

        # Straight to the app port, past the hub: forged identity is refused.
        path = f"/{APP}/"
        good = signed_headers(TOKEN, "GET", path, identity={"u": "alice"})
        assert _send(upstream, path, good) == 200
        forged = [
            {},
            {HEADER_IDENTITY: encode_identity({"u": "admin", "staff": True})},
            signed_headers("ff" * 32, "GET", path, identity={"u": "admin", "staff": True}),
            signed_headers(TOKEN, "GET", path, identity={"u": "alice"}, now=time.time() - 3600),
            {**good, HEADER_IDENTITY: encode_identity({"u": "admin", "staff": True})},
            {**good, "X-BDH-Signature-V2": good["X-BDH-Signature-V2"][:-1] + "0"},
            {**good, "X-BDH-Time": str(int(good["X-BDH-Time"]) + 1)},
        ]
        for headers in forged:
            assert _send(upstream, path, headers) == 403, headers
        assert _send(upstream, f"/{APP}/api/visits", good) == 403, "a signature for one path opened another"
    finally:
        await manager.stop(APP)


async def test_rebuild_upgrade_rollback_and_gc(env):
    """Platform rebuilds (same version, new digest) upgrade the app with its
    data; the N-1 image and a data snapshot allow a rollback; older images
    are garbage-collected."""
    cfg, client, catalog, manager = env["cfg"], env["client"], env["catalog"], env["manager"]
    builds = env["images"]["hello"]
    digest = [b.split("@", 1)[1] for b in builds]
    upstream = (await manager.start(APP, wait=True))["upstream"]
    before = _visits(upstream)

    catalog.build = 1
    assert (await manager.poll_catalog())["upgraded"] == [APP]
    rec = await _settled(manager, APP)
    assert rec["image"] == builds[1] and rec["previous"]["image"] == builds[0], rec
    assert (await manager.start(APP, wait=True))["healthy"]
    assert _visits(upstream) == before, "the upgrade lost /data"
    _visit(upstream, 3)
    at_build2 = _visits(upstream)

    catalog.build = 2
    assert (await manager.poll_catalog())["upgraded"] == [APP]
    rec = await _settled(manager, APP)
    assert rec["image"] == builds[2] and rec["previous"]["image"] == builds[1], rec
    local = {d.split("@", 1)[1] for d in _local_digests(client, cfg)}
    assert digest[1] in local and digest[2] in local, local
    assert digest[0] not in local, "build 1 (neither current nor N-1) was not garbage-collected"
    assert (await manager.start(APP, wait=True))["healthy"]
    _visit(upstream, 2)
    assert _visits(upstream) == at_build2 + 2

    view = await manager.rollback(APP)
    assert view["image"] == builds[1] and view["pinned"] is True
    assert (await manager.start(APP, wait=True))["healthy"]
    assert _visits(upstream) == at_build2, "the rollback did not restore the snapshot"
    assert (await manager.poll_catalog())["upgraded"] == [], "a catalog poll undid the rollback"
    await manager.stop(APP)


async def test_interrupted_upgrade_resumes(env):
    """A supervisor restart mid-upgrade (Watchtower) picks it up where it stopped."""
    from bdh_supervisor.manager import Manager

    cfg, driver, catalog, images = env["cfg"], env["driver"], env["catalog"], env["images"]
    old = env["manager"]
    rec = old.get(APP)
    rec["upgrade"] = {"version": images["version"], "image": images["hello"][2], "base": images["base"],
                      "build": "build3", "stage": "verified"}
    rec["status"] = "installing"
    old.save()

    restarted = Manager(cfg, driver, catalog)
    await restarted.resume()
    rec = await _settled(restarted, APP)
    assert rec["status"] == "installed", rec["error"]
    assert rec["image"] == images["hello"][2] and rec["previous"]["image"] == images["hello"][1]
    assert rec["upgrade"] is None
    env["manager"] = restarted


async def test_pull_rejects_an_image_not_built_on_its_base(env):
    from bdh_supervisor.driver import DriverError
    from bdh_supervisor.models import ImageRef

    images = env["images"]
    with pytest.raises(DriverError, match="not built on its catalog base"):
        env["driver"].pull(APP, ImageRef(images["version"], images["foreign"], images["base"], ""))


async def test_host_reaches_apps_only_through_their_upstream():
    """Sanity check that this host is not the LAN: nothing listens on the
    app's port on a public interface."""
    s = socket.socket()
    s.settimeout(1)
    try:
        assert s.connect_ex(("127.0.0.1", 8000)) != 0
    finally:
        s.close()
