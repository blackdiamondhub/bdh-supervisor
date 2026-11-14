import pytest

from bdh_supervisor.config import Config
from bdh_supervisor.driver import ContainerState, DriverError, RuntimeDriver
from bdh_supervisor.models import Wheel

TOKEN = "ab" * 32
SUP_TOKEN = "s" * 40
SHA = "0" * 64


class FakeCatalog:
    def __init__(self):
        self.calls = []
        self.error = None

    def wheels(self, app_id, version, hub_api):
        self.calls.append((app_id, version, hub_api))
        if self.error:
            raise self.error
        return [Wheel(f"https://example.test/{app_id}-{version}-py3-none-any.whl", SHA)]


class FakeDriver(RuntimeDriver):
    """Keeps containers as dicts; tests flip ``crash``/``healthy`` to drive it."""

    def __init__(self):
        self.calls = []
        self.containers = {}
        self.manifests = {}
        self.install_error = None
        self.start_error = None
        self.healthy = True
        self.image_id = "sha256:runtime1"

    def manifest_for(self, app_id, **extra):
        manifest = {"id": app_id, "api_version": 2, "network": "none", "lifecycle": "on_demand",
                    "memory_mb": 96, "companions": [], "permissions": []}
        manifest.update(extra)
        self.manifests[app_id] = manifest
        return manifest

    def prepare(self):
        self.calls.append(("prepare",))
        return {"firewall": True, "runtime_image": self.image_id}

    def refresh_runtime(self):
        self.calls.append(("refresh_runtime",))
        return self.image_id

    def install(self, app_id, version, wheels):
        self.calls.append(("install", app_id, version, tuple(wheels)))
        if self.install_error:
            raise DriverError(self.install_error)
        return self.manifests.get(app_id) or self.manifest_for(app_id)

    def start(self, plan):
        self.calls.append(("start", plan))
        if self.start_error:
            raise DriverError(self.start_error)
        self.containers[plan.app_id] = {"running": True, "plan": plan, "exit": None, "oom": False,
                                        "image": self.image_id}

    def stop(self, app_id):
        self.calls.append(("stop", app_id))
        if app_id in self.containers:
            self.containers[app_id]["running"] = False
            self.containers[app_id]["exit"] = 0

    def remove(self, app_id, purge_data=False):
        self.calls.append(("remove", app_id, purge_data))
        self.containers.pop(app_id, None)

    def crash(self, app_id, code=1, oom=False):
        self.containers[app_id].update(running=False, exit=code, oom=oom)

    def state(self, app_id):
        c = self.containers.get(app_id)
        if c is None:
            return ContainerState()
        return ContainerState(exists=True, running=c["running"], exit_code=c["exit"], oom_killed=c["oom"],
                              image_id=c["image"], status="running" if c["running"] else "exited")

    def logs(self, app_id, lines):
        return f"{app_id} log line\n" * min(lines, 3)

    def stats(self, app_id):
        return {"memory_bytes": 50 << 20, "working_set_bytes": 40 << 20} if app_id in self.containers else {}

    def health(self, plan, timeout=2.0):
        c = self.containers.get(plan.app_id)
        return bool(c and c["running"] and self.healthy)

    def started(self):
        return [c[1] for c in self.calls if c[0] == "start"]


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def cfg(tmp_path):
    return Config(token=SUP_TOKEN, state_dir=str(tmp_path / "state"), catalog_url="https://example.test/c.json",
                  start_timeout=2, reconcile_interval=1)


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def catalog():
    return FakeCatalog()


@pytest.fixture
def clock():
    return Clock()
