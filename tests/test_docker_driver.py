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


def test_state_flags_companions_on_an_old_image(cfg):
    from docker.errors import NotFound

    d = DockerDriver(cfg, client=mock.MagicMock())
    containers = {
        "bdh-app-cameras": {"State": {"Running": True}, "Image": "sha256:app"},
        "bdh-app-cameras-go2rtc": {"State": {"Running": True}, "Image": "sha256:go2rtc-new"},
        "bdh-app-cameras-aec": {"State": {"Running": True}, "Image": "sha256:aec-old"},
    }

    def inspect(name):
        if name not in containers:
            raise NotFound(name)
        return containers[name]

    d.api.inspect_container.side_effect = inspect
    d._image_id = {cfg.go2rtc_image: "sha256:go2rtc-new", cfg.aec_image: "sha256:aec-new"}.get
    assert d.state("cameras").stale_companions == ["aec"]


def test_prepare_repulls_companion_tags_best_effort(cfg):
    from docker.errors import APIError

    d = DockerDriver(cfg, client=mock.MagicMock())
    d._ensure_image = d._ensure_network = lambda *a: None
    d.apply_firewall = lambda: True
    d._ensure_gateway = lambda: None
    d.api.pull.side_effect = lambda ref: (_ for _ in ()).throw(APIError("offline")) \
        if ref == cfg.go2rtc_image else None
    assert d.prepare() == {"firewall": True}
    assert [c.args[0] for c in d.api.pull.call_args_list] == [cfg.go2rtc_image, cfg.aec_image]
