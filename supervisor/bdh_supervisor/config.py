"""Settings for the supervisor, from the environment."""
import ipaddress
import os
from dataclasses import dataclass, field


def _int(name, default):
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    token: str = ""
    socket_path: str = "/run/bdh-supervisor/supervisor.sock"
    state_dir: str = "/state"
    catalog_url: str = ""
    runtime_image: str = "ghcr.io/blackdiamondlodge/bdh-app-runtime:hub-api-2"
    supervisor_image: str = "ghcr.io/blackdiamondlodge/bdh-supervisor:latest"
    go2rtc_image: str = "ghcr.io/blackdiamondlodge/go2rtc-nack:latest"
    hub_api: int = 2
    # Apps get 10.213.<slot>.0/24 each; the egress bridge is 10.213.255.0/24.
    subnet_prefix: str = "10.213"
    # The hub broker as seen from the host; the gateway relays apps to it.
    broker_host: str = "172.17.0.1"
    broker_port: int = 8099
    broker_path: str = "/_broker"
    lan_port_base: int = 9200
    lan_go2rtc_port_base: int = 11900
    firewall: str = "required"  # or "off"
    allow_http: bool = False  # http:// wheel/catalog URLs (tests only)
    idle_timeout: int = 600
    stable_after: int = 600
    backoff_base: int = 5
    backoff_max: int = 300
    reconcile_interval: int = 15
    runtime_pull_interval: int = 6 * 3600
    firewall_interval: int = 3600
    start_timeout: int = 30
    install_timeout: int = 300
    pids_limit: int = 128
    cpus: float = 1.0
    egress_ports: tuple = (80, 443)
    labels: dict = field(default_factory=dict, hash=False)

    @classmethod
    def from_env(cls):
        return cls(
            token=os.environ.get("BDH_SUPERVISOR_TOKEN", ""),
            socket_path=os.environ.get("BDH_SUPERVISOR_SOCKET", cls.socket_path),
            state_dir=os.environ.get("BDH_STATE_DIR", cls.state_dir),
            catalog_url=os.environ.get("BDH_CATALOG_URL", ""),
            runtime_image=os.environ.get("BDH_RUNTIME_IMAGE", cls.runtime_image),
            supervisor_image=os.environ.get("BDH_SUPERVISOR_IMAGE", cls.supervisor_image),
            go2rtc_image=os.environ.get("BDH_GO2RTC_IMAGE", cls.go2rtc_image),
            hub_api=_int("BDH_HUB_API", cls.hub_api),
            subnet_prefix=os.environ.get("BDH_SUBNET_PREFIX", cls.subnet_prefix),
            broker_host=os.environ.get("BDH_BROKER_HOST", cls.broker_host),
            broker_port=_int("BDH_BROKER_PORT", cls.broker_port),
            broker_path=os.environ.get("BDH_BROKER_PATH", cls.broker_path),
            lan_port_base=_int("BDH_LAN_PORT_BASE", cls.lan_port_base),
            firewall=os.environ.get("BDH_FIREWALL", cls.firewall),
            allow_http=os.environ.get("BDH_ALLOW_HTTP") == "1",
            idle_timeout=_int("BDH_IDLE_TIMEOUT", cls.idle_timeout),
            runtime_pull_interval=_int("BDH_RUNTIME_PULL_INTERVAL", cls.runtime_pull_interval),
            start_timeout=_int("BDH_START_TIMEOUT", cls.start_timeout),
            egress_ports=tuple(
                int(p) for p in os.environ.get("BDH_EGRESS_PORTS", "80,443").split(",") if p.strip()
            ),
        )

    def validate(self):
        if len(self.token) < 32:
            raise ValueError("BDH_SUPERVISOR_TOKEN must be at least 32 characters")
        if self.firewall not in ("required", "off"):
            raise ValueError("BDH_FIREWALL must be 'required' or 'off'")
        net = ipaddress.ip_network(f"{self.subnet_prefix}.0.0/16")
        if not net.is_private:
            raise ValueError("BDH_SUBNET_PREFIX must be a private /16 prefix, e.g. 10.213")
        return self

    @property
    def apps_cidr(self):
        return f"{self.subnet_prefix}.0.0/16"

    @property
    def egress_cidr(self):
        return f"{self.subnet_prefix}.255.0/24"
