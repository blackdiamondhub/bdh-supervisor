import json

import pytest

from bdh_supervisor import templates as T
from bdh_supervisor.models import AppPlan

TOKEN = "ab" * 32
IMAGE = "ghcr.io/blackdiamondlodge/bdh-app-hello@sha256:" + "c" * 64


def plan(network="none", companions=(), memory_mb=96, slot=7):
    return AppPlan(app_id="hello", version="1.0.0", image=IMAGE, slot=slot, network=network,
                   memory_mb=memory_mb, token=TOKEN, companions=companions)


def all_specs(cfg):
    specs = []
    for net in ("none", "outbound", "lan"):
        p = plan(net, companions=("go2rtc",))
        specs.append(T.app_container(cfg, p))
        specs.append(T.companion_container(cfg, p, "go2rtc"))
    specs.append(T.data_copy_container("hello", IMAGE, "snapshot"))
    specs.append(T.data_copy_container("hello", IMAGE, "restore"))
    specs.append(T.manifest_probe("hello", IMAGE))
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
    spec = T.app_container(cfg, plan("none"))
    assert spec["image"] == IMAGE
    # The supervisor, not the image, decides how the app runs.
    assert spec["entrypoint"] == ["python", "-m", "bdh_sdk.runtime"] and spec["command"] == []
    assert spec["user"] == "10001:10001" and spec["working_dir"] == "/tmp"
    assert spec["healthcheck"] == {"test": ["NONE"]}
    assert spec["host_config"]["network_mode"] == "bdh-app-hello"
    assert spec["ipv4_address"] == "10.213.7.2"
    assert spec["host_config"]["mem_limit"] == "96m"
    env = spec["environment"]
    assert env["BDH_BROKER_URL"] == "http://10.213.7.253:8099/_broker"
    assert "HTTP_PROXY" not in env and "HTTPS_PROXY" not in env
    assert env["BDH_APP_TOKEN"] == TOKEN
    assert env["BDH_HOST"] == "0.0.0.0" and env["BDH_PORT"] == "8000"
    [data] = spec["mounts"]
    assert (data["source"], data["target"], data["read_only"]) == ("bdh-app-hello-data", "/data", False)


def test_app_container_outbound_uses_proxy(cfg):
    env = T.app_container(cfg, plan("outbound"))["environment"]
    assert env["HTTPS_PROXY"] == "http://10.213.7.254:3128"
    assert env["BDH_BROKER_URL"] == "http://10.213.7.254:8099/_broker"
    assert "10.213.7.254" in env["NO_PROXY"]


def test_app_container_lan_uses_host_network(cfg):
    p = plan("lan")
    spec = T.app_container(cfg, p)
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
    assert T.app_container(cfg, plan(memory_mb=256))["host_config"]["mem_limit"] == "256m"


def test_data_copy_helpers(cfg):
    snap = T.data_copy_container("hello", IMAGE, "snapshot")
    restore = T.data_copy_container("hello", IMAGE, "restore")
    for spec in (snap, restore):
        assert spec["host_config"]["network_mode"] == "none" and spec["network"] is None
        assert spec["entrypoint"] == ["sh", "-c"]
        # The writable side is always /data so a new volume gets the app uid.
        assert [(m["target"], m["read_only"]) for m in spec["mounts"]] == [("/src", True), ("/data", False)]
    assert [m["source"] for m in snap["mounts"]] == ["bdh-app-hello-data", "bdh-app-hello-data-snap"]
    assert [m["source"] for m in restore["mounts"]] == ["bdh-app-hello-data-snap", "bdh-app-hello-data"]
    assert "find /data -mindepth 1 -delete" in restore["command"][0]
    with pytest.raises(ValueError):
        T.data_copy_container("hello", IMAGE, "shell")

def _go2rtc_args(cfg, p):
    spec = T.companion_container(cfg, p, "go2rtc")
    return spec, json.loads(spec["command"][1])


def test_go2rtc_lan_serves_browsers(cfg):
    p = plan("lan", companions=("go2rtc",))
    spec, conf = _go2rtc_args(cfg, p)
    assert spec["host_config"]["network_mode"] == "host"
    assert conf["api"] == {"listen": ":11907", "origin": "*"}
    assert conf["webrtc"] == {"listen": ":8555"}
    assert conf["rtsp"] == {"listen": ""}
    assert T.app_env(cfg, p)["BDH_GO2RTC_URL"] == "http://127.0.0.1:11907"


def test_go2rtc_lan_candidates_and_port(cfg):
    import dataclasses

    cfg = dataclasses.replace(cfg, go2rtc_webrtc_port=8556,
                              go2rtc_candidates=("192.168.10.216:8556",))
    _, conf = _go2rtc_args(cfg, plan("lan", companions=("go2rtc",)))
    assert conf["webrtc"] == {"listen": ":8556", "candidates": ["192.168.10.216:8556"]}


def test_go2rtc_bridged_has_no_webrtc(cfg):
    spec, conf = _go2rtc_args(cfg, plan("none", companions=("go2rtc",)))
    assert spec["ipv4_address"] == "10.213.7.3" or spec["ipv4_address"]
    assert conf["api"] == {"listen": ":1984"}
    assert conf["webrtc"] == {"listen": ""}


def test_config_reads_go2rtc_env(monkeypatch):
    from bdh_supervisor.config import Config

    monkeypatch.setenv("BDH_GO2RTC_WEBRTC_PORT", "8600")
    monkeypatch.setenv("BDH_GO2RTC_CANDIDATES", "10.0.0.5:8600, stun:8600")
    cfg = Config.from_env()
    assert cfg.go2rtc_webrtc_port == 8600
    assert cfg.go2rtc_candidates == ("10.0.0.5:8600", "stun:8600")


def test_unknown_companion_is_refused(cfg):
    with pytest.raises(ValueError):
        T.companion_container(cfg, plan(), "socat")


def test_labels_opt_out_of_watchtower():
    assert T.labels("hello")["com.centurylinklabs.watchtower.enable"] == "false"
