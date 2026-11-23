import pytest

from bdh_supervisor.config import Config
from bdh_supervisor.driver import ContainerState, DriverError, RuntimeDriver
from bdh_supervisor.models import ImageRef

TOKEN = "ab" * 32
SUP_TOKEN = "s" * 40
PREFIX = "ghcr.io/blackdiamondlodge/"
BASE = PREFIX + "bdh-app-runtime@sha256:" + "b" * 64


def image_for(app_id, version, build=""):
    digest = format(abs(hash((app_id, version, build))), "x").rjust(64, "0")[:64]
    return f"{PREFIX}bdh-app-{app_id}@sha256:{digest}"


class FakeCatalog:
    """Each (app, version) is one image; bump ``builds[app]`` to simulate a rebuild."""

    def __init__(self):
        self.calls = []
        self.error = None
        self.builds = {}

    def image(self, app_id, version, hub_api, force=True):
        self.calls.append((app_id, version, hub_api))
        if self.error:
            raise self.error
        build = self.builds.get(app_id, "")
        return ImageRef(version, image_for(app_id, version, build), BASE, build)


class FakeVerifier:
    def __init__(self):
        self.verified = []
        self.error = None

    def verify(self, ref):
        if self.error:
            raise self.error
        self.verified.append(ref.image)


class FakeDriver(RuntimeDriver):
    """Keeps containers as dicts; tests flip ``crash``/``healthy`` to drive it."""

    def __init__(self):
        self.calls = []
        self.containers = {}
        self.manifests = {}
        self.pull_error = None
        self.snapshot_error = None
        self.start_error = None
        self.healthy = True
        self.images = {}  # image id -> app id
        self.removed_images = []

    def manifest_for(self, app_id, **extra):
        manifest = {"id": app_id, "api_version": 2, "network": "none", "lifecycle": "on_demand",
                    "memory_mb": 96, "companions": [], "permissions": []}
        manifest.update(extra)
        self.manifests[app_id] = manifest
        return manifest

    def prepare(self):
        self.calls.append(("prepare",))
        return {"firewall": True}

    @staticmethod
    def image_id(image):
        return "sha256:id-" + image.rsplit(":", 1)[1][-12:]

    def pull(self, app_id, ref):
        self.calls.append(("pull", app_id, ref.image))
        if self.pull_error:
            raise DriverError(self.pull_error)
        image_id = self.image_id(ref.image)
        self.images[image_id] = app_id
        manifest = dict(self.manifests.get(app_id) or self.manifest_for(app_id))
        manifest["version"] = ref.version
        return {"manifest": manifest, "image_id": image_id}

    def snapshot(self, app_id, image):
        self.calls.append(("snapshot", app_id, image))
        if self.snapshot_error:
            raise DriverError(self.snapshot_error)

    def restore(self, app_id, image):
        self.calls.append(("restore", app_id, image))

    def gc(self, app_id, keep):
        self.calls.append(("gc", app_id, frozenset(keep)))
        removed = [i for i, a in self.images.items() if a == app_id and i not in keep]
        for i in removed:
            del self.images[i]
        self.removed_images += removed
        return removed

    def start(self, plan):
        self.calls.append(("start", plan))
        if self.start_error:
            raise DriverError(self.start_error)
        self.containers[plan.app_id] = {"running": True, "plan": plan, "exit": None, "oom": False,
                                        "image": self.image_id(plan.image),
                                        "companions": {n: True for n in plan.companions}, "stale": []}

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
                              image_id=c["image"], status="running" if c["running"] else "exited",
                              companions=dict(c["companions"]), stale_companions=list(c["stale"]))

    def logs(self, app_id, lines):
        return f"{app_id} log line\n" * min(lines, 3)

    def stats(self, app_id):
        return {"memory_bytes": 50 << 20, "working_set_bytes": 40 << 20} if app_id in self.containers else {}

    def health(self, plan, timeout=2.0):
        c = self.containers.get(plan.app_id)
        return bool(c and c["running"] and self.healthy)

    def started(self):
        return [c[1] for c in self.calls if c[0] == "start"]

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


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


@pytest.fixture
def verifier():
    return FakeVerifier()
