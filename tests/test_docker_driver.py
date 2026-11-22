from unittest import mock

import pytest

from bdh_supervisor.docker_driver import DockerDriver
from bdh_supervisor.driver import DriverError
from bdh_supervisor.models import AppPlan

IMAGE = "ghcr.io/blackdiamondlodge/bdh-app-cameras@sha256:" + "c" * 64


def _driver(cfg, missing=()):
    d = DockerDriver(cfg, client=mock.MagicMock())
    calls = []

    def ensure(image):
        calls.append(("pull", image))
        if image in missing:
            raise DriverError(f"No such image: {image}")

    d._ensure_image = ensure
    d._ensure_volume = lambda *a: None
    d._remove_app_containers = lambda app_id: calls.append(("remove", app_id))
    d._remove_network = lambda app_id: None
    d._create = lambda spec, start=False: calls.append(("create", spec["image"]))
    return d, calls


def _plan():
    return AppPlan(app_id="cameras", version="2.0.0", image=IMAGE, slot=0, network="lan",
                   memory_mb=96, token="ab" * 32, companions=("go2rtc", "aec"))


def test_start_pulls_companion_images(cfg):
    d, calls = _driver(cfg)
    d.start(_plan())
    assert ("pull", cfg.go2rtc_image) in calls
    assert calls.index(("pull", cfg.go2rtc_image)) < calls.index(("create", cfg.go2rtc_image))
    assert calls.index(("pull", cfg.aec_image)) < calls.index(("remove", "cameras"))
    assert calls.index(("create", cfg.aec_image)) < calls.index(("create", IMAGE))


def test_missing_companion_image_leaves_running_app_alone(cfg):
    d, calls = _driver(cfg, missing={cfg.go2rtc_image})
    with pytest.raises(DriverError):
        d.start(_plan())
    assert not [c for c in calls if c[0] in ("remove", "create")]
