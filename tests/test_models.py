import pytest

from bdh_supervisor.config import Config
from bdh_supervisor.models import (
    clamp_memory,
    effective_network,
    manifest_companions,
    valid_app_id,
    valid_version,
)


@pytest.mark.parametrize(("approved", "requested", "allow_lan", "expected"), [
    ("none", "outbound", False, "none"),
    ("outbound", "none", False, "none"),
    ("outbound", "outbound", False, "outbound"),
    ("outbound", "lan", True, "outbound"),
    ("lan", "lan", False, "outbound"),
    ("lan", "lan", True, "lan"),
    ("lan", "outbound", True, "outbound"),
    ("bogus", "lan", True, "none"),
    ("lan", "bogus", True, "none"),
])
def test_effective_network_is_least_privileged(approved, requested, allow_lan, expected):
    assert effective_network(approved, requested, allow_lan) == expected


@pytest.mark.parametrize(("value", "expected"), [(None, 96), (10, 48), (128, 128), (4096, 256), (True, 96),
                                                 ("200", 96)])
def test_clamp_memory(value, expected):
    assert clamp_memory(value) == expected


def test_companions_are_allowlisted():
    assert manifest_companions({"companions": ["go2rtc", "docker-in-docker"]}) == ("go2rtc",)


@pytest.mark.parametrize("value", ["../x", "Hello", "a b", "", "-x", "a" * 65])
def test_bad_app_ids(value):
    assert not valid_app_id(value)


@pytest.mark.parametrize("value", ["1.0.0", "2026.1.2+local", "1.0rc1"])
def test_good_versions(value):
    assert valid_version(value)


@pytest.mark.parametrize("value", ["../1", "1..2", "/1", "", "1 0"])
def test_bad_versions(value):
    assert not valid_version(value)


def test_config_validation():
    with pytest.raises(ValueError, match="TOKEN"):
        Config(token="short").validate()
    with pytest.raises(ValueError, match="FIREWALL"):
        Config(token="x" * 32, firewall="maybe").validate()
    with pytest.raises(ValueError, match="private"):
        Config(token="x" * 32, subnet_prefix="8.8").validate()
    cfg = Config(token="x" * 32).validate()
    assert cfg.apps_cidr == "10.213.0.0/16"
    assert cfg.egress_cidr == "10.213.255.0/24"


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("BDH_SUPERVISOR_TOKEN", "t" * 40)
    monkeypatch.setenv("BDH_EGRESS_PORTS", "443")
    monkeypatch.setenv("BDH_IDLE_TIMEOUT", "60")
    cfg = Config.from_env()
    assert cfg.egress_ports == (443,)
    assert cfg.idle_timeout == 60
    assert cfg.firewall == "required"
