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
    # App images must be <image_prefix>bdh-app-<id>@sha256:..., built on
    # <image_prefix>bdh-app-runtime@sha256:... (see catalog.py).
    image_prefix: str = "ghcr.io/blackdiamondlodge/"
    attestation: str = "required"  # or "off" (tests only)
    attestation_repo: str = "blackdiamondlodge/bdh-app-catalog"
    attestation_identity: str = (
        "https://github.com/blackdiamondlodge/bdh-app-catalog/.github/workflows/build.yml@refs/heads/main"
    )
    github_token: str = field(default="", repr=False)
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
    # WebRTC of a lan app's go2rtc (host-wide; see templates.go2rtc_config).
    go2rtc_webrtc_port: int = 8555
    go2rtc_candidates: tuple = ()
    firewall: str = "required"  # or "off"
    allow_http: bool = False  # http:// catalog URLs (tests only)
    idle_timeout: int = 600
    stable_after: int = 600
    backoff_base: int = 5
    backoff_max: int = 300
    reconcile_interval: int = 15
    catalog_poll_interval: int = 6 * 3600
    firewall_interval: int = 3600
    start_timeout: int = 30
    pull_timeout: int = 900
    snapshot_timeout: int = 600
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
            image_prefix=os.environ.get("BDH_IMAGE_PREFIX", cls.image_prefix),
            attestation=os.environ.get("BDH_ATTESTATION", cls.attestation),
            attestation_repo=os.environ.get("BDH_ATTESTATION_REPO", cls.attestation_repo),
            attestation_identity=os.environ.get("BDH_ATTESTATION_IDENTITY", cls.attestation_identity),
            github_token=os.environ.get("BDH_GITHUB_TOKEN", ""),
            supervisor_image=os.environ.get("BDH_SUPERVISOR_IMAGE", cls.supervisor_image),
            go2rtc_image=os.environ.get("BDH_GO2RTC_IMAGE", cls.go2rtc_image),
            hub_api=_int("BDH_HUB_API", cls.hub_api),
            subnet_prefix=os.environ.get("BDH_SUBNET_PREFIX", cls.subnet_prefix),
            broker_host=os.environ.get("BDH_BROKER_HOST", cls.broker_host),
            broker_port=_int("BDH_BROKER_PORT", cls.broker_port),
            broker_path=os.environ.get("BDH_BROKER_PATH", cls.broker_path),
            lan_port_base=_int("BDH_LAN_PORT_BASE", cls.lan_port_base),
            lan_go2rtc_port_base=_int("BDH_LAN_GO2RTC_PORT_BASE", cls.lan_go2rtc_port_base),
            go2rtc_webrtc_port=_int("BDH_GO2RTC_WEBRTC_PORT", cls.go2rtc_webrtc_port),
            go2rtc_candidates=tuple(
                c.strip() for c in os.environ.get("BDH_GO2RTC_CANDIDATES", "").split(",") if c.strip()
            ),
            firewall=os.environ.get("BDH_FIREWALL", cls.firewall),
            allow_http=os.environ.get("BDH_ALLOW_HTTP") == "1",
            idle_timeout=_int("BDH_IDLE_TIMEOUT", cls.idle_timeout),
            catalog_poll_interval=_int("BDH_CATALOG_POLL_INTERVAL", cls.catalog_poll_interval),
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
        if self.attestation not in ("required", "off"):
            raise ValueError("BDH_ATTESTATION must be 'required' or 'off'")
        if not self.image_prefix.endswith("/"):
            raise ValueError("BDH_IMAGE_PREFIX must end with '/'")
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
