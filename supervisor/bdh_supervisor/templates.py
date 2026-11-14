"""Platform-owned container templates.

Pure functions that turn an :class:`~bdh_supervisor.models.AppPlan` into
container specs. Nothing an app or the hub sends can add mounts, capabilities,
devices or host networking: the only inputs are the validated plan fields.

A spec is a plain dict the driver maps onto the Docker API::

    {"name", "image", "entrypoint", "command", "user", "environment", "labels",
     "host_config": {...docker create_host_config kwargs...},
     "mounts": [{"type": "volume"|"tmpfs", "source", "target", "read_only"}],
     "network": <network name or None>, "ipv4_address": <ip or None>}

Addressing: each app gets the internal bridge ``bdh-app-<id>`` with subnet
``<prefix>.<slot>.0/24``. The app is ``.2``, a companion ``.3``, and the
shared gateway joins as ``.254`` (outbound apps: broker relay + egress proxy)
or ``.253`` (network ``none``: broker relay only).
"""
import json

from .models import NETWORK_LAN, NETWORK_OUTBOUND

UID = "10001:10001"
APP_PORT = 8000
GATEWAY_RELAY_PORT = 8099
GATEWAY_PROXY_PORT = 3128
GATEWAY_NAME = "bdh-gateway"
EGRESS_NETWORK = "bdh-egress"
MANAGED = "bdh.managed"
LOG_CONFIG = {"type": "json-file", "config": {"max-size": "1m", "max-file": "2"}}
WORKER_MEMORY_MB = 512
WORKER_PIDS = 256
COMPANION_MEMORY_MB = 64
GO2RTC_PORT = 1984


def app_network(app_id):
    return f"bdh-app-{app_id}"


def container_name(app_id, role="app"):
    return f"bdh-app-{app_id}" if role == "app" else f"bdh-app-{app_id}-{role}"


def code_volume(app_id):
    return f"bdh-app-{app_id}-code"


def data_volume(app_id):
    return f"bdh-app-{app_id}-data"


def subnet(cfg, slot):
    return f"{cfg.subnet_prefix}.{slot}.0/24"


def host_ip(cfg, slot, host):
    return f"{cfg.subnet_prefix}.{slot}.{host}"


def app_ip(cfg, slot):
    return host_ip(cfg, slot, 2)


def companion_ip(cfg, slot):
    return host_ip(cfg, slot, 3)


def gateway_ip(cfg, slot, network):
    """The gateway's address on the app's network; the proxy only serves .254."""
    return host_ip(cfg, slot, 254 if network == NETWORK_OUTBOUND else 253)


def lan_port(cfg, slot):
    return cfg.lan_port_base + slot


def upstream(cfg, plan):
    """Where the hub proxy reaches the app."""
    if plan.network == NETWORK_LAN:
        return f"http://127.0.0.1:{lan_port(cfg, plan.slot)}"
    return f"http://{app_ip(cfg, plan.slot)}:{APP_PORT}"


def health_url(cfg, plan):
    return upstream(cfg, plan) + "/_bdh/health"


def labels(app_id=None, role="app", extra=None):
    out = {MANAGED: "1", "bdh.role": role, "com.centurylinklabs.watchtower.enable": "false"}
    if app_id:
        out["bdh.app"] = app_id
    out.update(extra or {})
    return out


def network_spec(cfg, plan):
    """The app's own bridge, or None for lan apps (host network)."""
    if plan.network == NETWORK_LAN:
        return None
    return {
        "name": app_network(plan.app_id),
        "driver": "bridge",
        "internal": True,
        "subnet": subnet(cfg, plan.slot),
        "labels": labels(plan.app_id, "network"),
        "options": {"com.docker.network.bridge.enable_icc": "true"},
    }


def egress_network_spec(cfg):
    return {
        "name": EGRESS_NETWORK,
        "driver": "bridge",
        "internal": False,
        "subnet": cfg.egress_cidr,
        "labels": labels(None, "network"),
        "options": {},
    }


def _hardened(memory_mb, pids, tmp_mb, network_mode, cpus=None):
    host = {
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": pids,
        "mem_limit": f"{memory_mb}m",
        "memswap_limit": f"{memory_mb}m",
        "tmpfs": {"/tmp": f"rw,noexec,nosuid,nodev,size={tmp_mb}m,uid=10001,gid=10001,mode=0700"},
        "network_mode": network_mode,
        "init": True,
        "log_config": LOG_CONFIG,
        "restart_policy": {"Name": "no"},
        "ipc_mode": "private",
    }
    if cpus:
        host["nano_cpus"] = int(cpus * 1e9)
    return host


def app_env(cfg, plan):
    env = {
        "BDH_APP_ID": plan.app_id,
        "BDH_APP_TOKEN": plan.token,
        "BDH_DATA_DIR": "/data",
        "BDH_TIME_ZONE": plan.time_zone,
        "BDH_HUB_BRAND": plan.hub_brand,
        "BDH_APP_VERSION": plan.version,
    }
    if plan.network == NETWORK_LAN:
        env["BDH_HOST"] = "127.0.0.1"
        env["BDH_PORT"] = str(lan_port(cfg, plan.slot))
        env["BDH_BROKER_URL"] = f"http://{cfg.broker_host}:{cfg.broker_port}{cfg.broker_path}"
        if "go2rtc" in plan.companions:
            env["BDH_GO2RTC_URL"] = f"http://127.0.0.1:{cfg.lan_go2rtc_port_base + plan.slot}"
        return env
    gw = gateway_ip(cfg, plan.slot, plan.network)
    env["BDH_HOST"] = "0.0.0.0"
    env["BDH_PORT"] = str(APP_PORT)
    env["BDH_BROKER_URL"] = f"http://{gw}:{GATEWAY_RELAY_PORT}{cfg.broker_path}"
    no_proxy = [gw, host_ip(cfg, plan.slot, 253), companion_ip(cfg, plan.slot), "localhost", "127.0.0.1"]
    if plan.network == NETWORK_OUTBOUND:
        proxy = f"http://{gw}:{GATEWAY_PROXY_PORT}"
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[key] = proxy
    env["NO_PROXY"] = env["no_proxy"] = ",".join(no_proxy)
    if "go2rtc" in plan.companions:
        env["BDH_GO2RTC_URL"] = f"http://{companion_ip(cfg, plan.slot)}:{GO2RTC_PORT}"
    return env


def app_container(cfg, plan, image):
    lan = plan.network == NETWORK_LAN
    return {
        "name": container_name(plan.app_id),
        "image": image,
        "entrypoint": ["python", "-m", "bdh_sdk.runtime"],
        "command": None,
        "user": UID,
        "environment": app_env(cfg, plan),
        "labels": labels(plan.app_id, "app", {"bdh.version": plan.version}),
        "host_config": _hardened(plan.memory_mb, cfg.pids_limit, 64,
                                 "host" if lan else app_network(plan.app_id), cfg.cpus),
        "mounts": [
            {"type": "volume", "source": data_volume(plan.app_id), "target": "/data", "read_only": False},
            {"type": "volume", "source": code_volume(plan.app_id), "target": "/app", "read_only": True},
        ],
        "network": None if lan else app_network(plan.app_id),
        "ipv4_address": None if lan else app_ip(cfg, plan.slot),
    }


def go2rtc_config(cfg, plan):
    lan = plan.network == NETWORK_LAN
    listen = f"127.0.0.1:{cfg.lan_go2rtc_port_base + plan.slot}" if lan else f":{GO2RTC_PORT}"
    return {"api": {"listen": listen}, "rtsp": {"listen": ""}, "webrtc": {"listen": ""}, "log": {"level": "warn"}}


def companion_container(cfg, plan, name):
    if name != "go2rtc":
        raise ValueError(f"unknown companion {name!r}")
    lan = plan.network == NETWORK_LAN
    return {
        "name": container_name(plan.app_id, name),
        "image": cfg.go2rtc_image,
        "entrypoint": ["go2rtc"],
        "command": ["-config", json.dumps(go2rtc_config(cfg, plan), separators=(",", ":"))],
        "user": UID,
        "environment": {},
        "labels": labels(plan.app_id, name),
        "host_config": _hardened(COMPANION_MEMORY_MB, 64, 16,
                                 "host" if lan else app_network(plan.app_id), 0.5),
        "mounts": [],
        "network": None if lan else app_network(plan.app_id),
        "ipv4_address": None if lan else companion_ip(cfg, plan.slot),
    }


def install_worker(cfg, image, app_id, version, wheels, step):
    """A disposable container for one install step (see ``bdh_runtime.install``).

    ``fetch`` downloads on the egress bridge (no app code runs); ``verify``
    imports the app's PluginSpec with no network at all.
    """
    if step not in ("fetch", "verify"):
        raise ValueError(step)
    request = {"app_id": app_id, "version": version,
               "wheels": [{"url": w.url, "sha256": w.sha256} for w in wheels]}
    env = {"BDH_INSTALL": json.dumps(request, separators=(",", ":")), "BDH_APP_ROOT": "/app"}
    if cfg.allow_http:
        env["BDH_ALLOW_HTTP"] = "1"
    network = EGRESS_NETWORK if step == "fetch" else "none"
    host = _hardened(WORKER_MEMORY_MB, WORKER_PIDS, 256, network, 1.0)
    return {
        "name": f"bdh-install-{app_id}-{step}",
        "image": image,
        "entrypoint": ["python", "-m", "bdh_runtime.install"],
        "command": [step],
        "user": UID,
        "environment": env,
        "labels": labels(app_id, f"install-{step}"),
        "host_config": host,
        "mounts": [{"type": "volume", "source": code_volume(app_id), "target": "/app", "read_only": False}],
        "network": None if network == "none" else EGRESS_NETWORK,
        "ipv4_address": None,
    }


def gateway_container(cfg, image):
    env = {
        "BDH_GATEWAY_RELAY_PORT": str(GATEWAY_RELAY_PORT),
        "BDH_GATEWAY_PROXY_PORT": str(GATEWAY_PROXY_PORT),
        "BDH_BROKER_TARGET": f"{cfg.broker_host}:{cfg.broker_port}",
        "BDH_SUBNET_PREFIX": cfg.subnet_prefix,
        "BDH_EGRESS_PORTS": ",".join(str(p) for p in cfg.egress_ports),
    }
    return {
        "name": GATEWAY_NAME,
        "image": image,
        "entrypoint": ["python", "-m", "bdh_supervisor.gateway"],
        "command": None,
        "user": UID,
        "environment": env,
        "labels": labels(None, "gateway"),
        "host_config": {**_hardened(64, 64, 8, EGRESS_NETWORK, 0.5), "restart_policy": {"Name": "unless-stopped"}},
        "mounts": [],
        "network": EGRESS_NETWORK,
        "ipv4_address": host_ip(cfg, 255, 254),
    }


def firewall_container(cfg, image, action="apply"):
    """One-shot helper that installs the host INPUT rules (needs NET_ADMIN on
    the host network namespace; nothing else)."""
    return {
        "name": "bdh-firewall",
        "image": image,
        "entrypoint": ["python", "-m", "bdh_supervisor.firewall"],
        "command": [action, "--apps", cfg.apps_cidr, "--egress", cfg.egress_cidr,
                    "--broker", f"{cfg.broker_host}:{cfg.broker_port}"],
        "user": "0:0",
        "environment": {},
        "labels": labels(None, "firewall"),
        "host_config": {
            "read_only": True,
            "cap_drop": ["ALL"],
            "cap_add": ["NET_ADMIN", "NET_RAW"],
            "security_opt": ["no-new-privileges:true"],
            "pids_limit": 32,
            "mem_limit": "64m",
            "network_mode": "host",
            "tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=8m", "/run": "rw,noexec,nosuid,nodev,size=1m"},
            "log_config": LOG_CONFIG,
            "restart_policy": {"Name": "no"},
        },
        "mounts": [],
        "network": None,
        "ipv4_address": None,
    }
