"""Settings for the supervisor, from the environment."""
import ipaddress
import os
from dataclasses import dataclass, field


def _int(name, default):
    return int(os.environ.get(name, default))


def _list(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(v.strip() for v in raw.split(",") if v.strip())


def _unique(*values):
    """``values`` without blanks and repeats, in order."""
    return tuple(dict.fromkeys(v for v in values if v))


# The GitHub org is moving from blackdiamondlodge to blackdiamondhub. GHCR
# keeps the digests, so during the move both names are the same image, and
# the catalog's builder signs as either org's workflow.
OLD_ORG, NEW_ORG = "blackdiamondlodge", "blackdiamondhub"
CATALOG_WORKFLOW = "https://github.com/{}/bdh-app-catalog/.github/workflows/build.yml@refs/heads/main"


@dataclass(frozen=True)
class Config:
    token: str = ""
    socket_path: str = "/run/bdh-supervisor/supervisor.sock"
    state_dir: str = "/state"
    catalog_url: str = ""
    # App images must be <prefix>bdh-app-<id>@sha256:..., built on
    # <prefix>bdh-app-runtime@sha256:... for one of the accepted prefixes
    # (image_prefix and image_prefixes; see catalog.py). image_prefix is the
    # primary one, for anything that names images.
    image_prefix: str = f"ghcr.io/{OLD_ORG}/"
    image_prefixes: tuple = (f"ghcr.io/{OLD_ORG}/", f"ghcr.io/{NEW_ORG}/")
    attestation: str = "required"  # or "off" (tests only)
    # Attestations are looked up in these repositories in order (a 404 or an
    # empty list falls through to the next); a bundle signed by any of the
    # identities is accepted. The singular fields (BDH_ATTESTATION_REPO /
    # _IDENTITY, the old settings) are added to the lists when set.
    attestation_repos: tuple = (f"{NEW_ORG}/bdh-app-catalog", f"{OLD_ORG}/bdh-app-catalog")
    attestation_identities: tuple = (CATALOG_WORKFLOW.format(OLD_ORG), CATALOG_WORKFLOW.format(NEW_ORG))
    attestation_repo: str = ""
    attestation_identity: str = ""
    github_token: str = field(default="", repr=False)
    supervisor_image: str = "ghcr.io/blackdiamondlodge/bdh-supervisor:latest"
    go2rtc_image: str = "ghcr.io/blackdiamondlodge/go2rtc-nack:latest"
    aec_image: str = "ghcr.io/blackdiamondlodge/bdh-aec:latest"
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
    # A lan app's aec companion: HTTP (what browsers and go2rtc use) and its
    # loopback-only talk websocket.
    lan_aec_port_base: int = 12200
    lan_aec_talk_port_base: int = 12500
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
            image_prefixes=_list("BDH_IMAGE_PREFIXES", cls.image_prefixes),
            attestation_repo=os.environ.get("BDH_ATTESTATION_REPO", cls.attestation_repo),
            attestation_identity=os.environ.get("BDH_ATTESTATION_IDENTITY", cls.attestation_identity),
            attestation_repos=_list("BDH_ATTESTATION_REPOS", cls.attestation_repos),
            attestation_identities=_list("BDH_ATTESTATION_IDENTITIES", cls.attestation_identities),
            github_token=os.environ.get("BDH_GITHUB_TOKEN", ""),
            supervisor_image=os.environ.get("BDH_SUPERVISOR_IMAGE", cls.supervisor_image),
            go2rtc_image=os.environ.get("BDH_GO2RTC_IMAGE", cls.go2rtc_image),
            aec_image=os.environ.get("BDH_AEC_IMAGE", cls.aec_image),
            hub_api=_int("BDH_HUB_API", cls.hub_api),
            subnet_prefix=os.environ.get("BDH_SUBNET_PREFIX", cls.subnet_prefix),
            broker_host=os.environ.get("BDH_BROKER_HOST", cls.broker_host),
            broker_port=_int("BDH_BROKER_PORT", cls.broker_port),
            broker_path=os.environ.get("BDH_BROKER_PATH", cls.broker_path),
            lan_port_base=_int("BDH_LAN_PORT_BASE", cls.lan_port_base),
            lan_go2rtc_port_base=_int("BDH_LAN_GO2RTC_PORT_BASE", cls.lan_go2rtc_port_base),
            go2rtc_webrtc_port=_int("BDH_GO2RTC_WEBRTC_PORT", cls.go2rtc_webrtc_port),
            lan_aec_port_base=_int("BDH_LAN_AEC_PORT_BASE", cls.lan_aec_port_base),
            lan_aec_talk_port_base=_int("BDH_LAN_AEC_TALK_PORT_BASE", cls.lan_aec_talk_port_base),
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
        if not all(p.endswith("/") for p in self.accepted_prefixes):
            raise ValueError("BDH_IMAGE_PREFIX and BDH_IMAGE_PREFIXES must end with '/'")
        if self.attestation == "required" and not (self.accepted_identities and self.accepted_repos):
            raise ValueError("attestation needs at least one repository and signer identity")
        net = ipaddress.ip_network(f"{self.subnet_prefix}.0.0/16")
        if not net.is_private:
            raise ValueError("BDH_SUBNET_PREFIX must be a private /16 prefix, e.g. 10.213")
        return self

    @property
    def accepted_prefixes(self):
        """Registry prefixes app and base images may come from; the primary first."""
        return _unique(self.image_prefix, *self.image_prefixes)

    @property
    def accepted_repos(self):
        """Repositories whose attestations are queried, in order."""
        return _unique(self.attestation_repo, *self.attestation_repos)

    @property
    def accepted_identities(self):
        """Signer identities (workflow URIs) an attestation may come from."""
        return _unique(*self.attestation_identities, self.attestation_identity)

    @property
    def apps_cidr(self):
        return f"{self.subnet_prefix}.0.0/16"

    @property
    def egress_cidr(self):
        return f"{self.subnet_prefix}.255.0/24"
