import json

import pytest

from bdh_supervisor import templates as T
from bdh_supervisor.models import AppPlan, Wheel

TOKEN = "ab" * 32


def plan(network="none", companions=(), memory_mb=96, slot=7):
    return AppPlan(app_id="hello", version="1.0.0", slot=slot, network=network, memory_mb=memory_mb,
                   token=TOKEN, companions=companions)


def all_specs(cfg):
    wheels = [Wheel("https://example.test/a-1-py3-none-any.whl", "0" * 64)]
    specs = []
    for net in ("none", "outbound", "lan"):
        p = plan(net, companions=("go2rtc",))
        specs.append(T.app_container(cfg, p, "runtime"))
        specs.append(T.companion_container(cfg, p, "go2rtc"))
    specs.append(T.install_worker(cfg, "runtime", "hello", "1.0.0", wheels, "fetch"))
    specs.append(T.install_worker(cfg, "runtime", "hello", "1.0.0", wheels, "verify"))
    specs.append(T.gateway_container(cfg, "supervisor"))
    return specs


def test_every_unprivileged_container_is_hardened(cfg):
    for spec in all_specs(cfg):
        host = spec["host_config"]
        assert spec["user"] == "10001:10001", spec["name"]
        assert host["read_only"] is True, spec["name"]
        assert host["cap_drop"] == ["ALL"], spec["name"]
        assert "cap_add" not in host, spec["name"]
        assert host["security_opt"] == ["no-new-privileges:true"], spec["name"]
        assert host["pids_limit"] > 0, spec["name"]
        assert host["mem_limit"] == host["memswap_limit"], spec["name"]
        assert "/tmp" in host["tmpfs"], spec["name"]
        for key in ("privileged", "devices", "binds", "volumes_from", "pid_mode", "userns_mode"):
            assert key not in host, (spec["name"], key)
        for mount in spec["mounts"]:
            assert mount["type"] == "volume", spec["name"]
            assert mount["source"].startswith("bdh-app-hello-"), spec["name"]
            assert not mount["source"].startswith("/"), spec["name"]


def test_firewall_helper_only_gets_net_admin(cfg):
    spec = T.firewall_container(cfg, "supervisor")
    host = spec["host_config"]
    assert host["cap_drop"] == ["ALL"]
    assert host["cap_add"] == ["NET_ADMIN", "NET_RAW"]
    assert host["read_only"] is True
    assert spec["mounts"] == []
    assert "--apps" in spec["command"] and cfg.apps_cidr in spec["command"]


def test_app_container_none(cfg):
    spec = T.app_container(cfg, plan("none"), "runtime")
    assert spec["host_config"]["network_mode"] == "bdh-app-hello"
    assert spec["ipv4_address"] == "10.213.7.2"
    assert spec["host_config"]["mem_limit"] == "96m"
    env = spec["environment"]
    assert env["BDH_BROKER_URL"] == "http://10.213.7.253:8099/_broker"
    assert "HTTP_PROXY" not in env and "HTTPS_PROXY" not in env
    assert env["BDH_APP_TOKEN"] == TOKEN
    assert env["BDH_HOST"] == "0.0.0.0" and env["BDH_PORT"] == "8000"
    data, code = spec["mounts"]
    assert (data["target"], data["read_only"]) == ("/data", False)
    assert (code["target"], code["read_only"]) == ("/app", True)


def test_app_container_outbound_uses_proxy(cfg):
    env = T.app_container(cfg, plan("outbound"), "runtime")["environment"]
    assert env["HTTPS_PROXY"] == "http://10.213.7.254:3128"
    assert env["BDH_BROKER_URL"] == "http://10.213.7.254:8099/_broker"
    assert "10.213.7.254" in env["NO_PROXY"]


def test_app_container_lan_uses_host_network(cfg):
    p = plan("lan")
    spec = T.app_container(cfg, p, "runtime")
    assert spec["host_config"]["network_mode"] == "host"
    assert spec["network"] is None
    env = spec["environment"]
    assert env["BDH_HOST"] == "127.0.0.1"
    assert env["BDH_PORT"] == "9207"
    assert env["BDH_BROKER_URL"] == "http://172.17.0.1:8099/_broker"
    assert T.upstream(cfg, p) == "http://127.0.0.1:9207"
    assert T.network_spec(cfg, p) is None


def test_upstream_and_network(cfg):
    p = plan("outbound")
    assert T.upstream(cfg, p) == "http://10.213.7.2:8000"
    net = T.network_spec(cfg, p)
    assert net["internal"] is True
    assert net["subnet"] == "10.213.7.0/24"
    assert T.gateway_ip(cfg, 7, "outbound") == "10.213.7.254"
    assert T.gateway_ip(cfg, 7, "none") == "10.213.7.253"


def test_memory_limit_follows_plan(cfg):
    assert T.app_container(cfg, plan(memory_mb=256), "rt")["host_config"]["mem_limit"] == "256m"


def test_install_worker_networks(cfg):
    wheels = [Wheel("https://example.test/a-1-py3-none-any.whl", "0" * 64)]
    fetch = T.install_worker(cfg, "runtime", "hello", "1.0.0", wheels, "fetch")
    verify = T.install_worker(cfg, "runtime", "hello", "1.0.0", wheels, "verify")
    assert fetch["host_config"]["network_mode"] == T.EGRESS_NETWORK
    assert verify["host_config"]["network_mode"] == "none"
    assert verify["network"] is None
    req = json.loads(fetch["environment"]["BDH_INSTALL"])
    assert req == {"app_id": "hello", "version": "1.0.0",
                   "wheels": [{"url": wheels[0].url, "sha256": "0" * 64}]}
    assert [m["source"] for m in fetch["mounts"]] == ["bdh-app-hello-code"]
    with pytest.raises(ValueError):
        T.install_worker(cfg, "runtime", "hello", "1.0.0", wheels, "shell")


def test_unknown_companion_is_refused(cfg):
    with pytest.raises(ValueError):
        T.companion_container(cfg, plan(), "socat")


def test_labels_opt_out_of_watchtower():
    assert T.labels("hello")["com.centurylinklabs.watchtower.enable"] == "false"
