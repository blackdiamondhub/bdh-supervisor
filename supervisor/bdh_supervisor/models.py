"""Validation and the plain data the manager hands to a driver."""
import re
from dataclasses import dataclass, field

APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

NETWORK_NONE = "none"
NETWORK_OUTBOUND = "outbound"
NETWORK_LAN = "lan"
NETWORKS = (NETWORK_NONE, NETWORK_OUTBOUND, NETWORK_LAN)  # least to most privileged
LIFECYCLES = ("always", "on_demand")
COMPANIONS = ("go2rtc",)
MAX_SLOT = 253
MIN_MEMORY_MB, DEFAULT_MEMORY_MB, MAX_MEMORY_MB = 48, 96, 256


def valid_app_id(value):
    return isinstance(value, str) and bool(APP_ID_RE.match(value))


def valid_version(value):
    return isinstance(value, str) and bool(VERSION_RE.match(value)) and ".." not in value


def effective_network(approved, requested, allow_lan):
    """The less privileged of what the admin approved and what the app asked for;
    ``lan`` also needs the explicit ``allow_lan`` grant."""
    approved = approved if approved in NETWORKS else NETWORK_NONE
    requested = requested if requested in NETWORKS else NETWORK_NONE
    net = min(approved, requested, key=NETWORKS.index)
    if net == NETWORK_LAN and not allow_lan:
        net = NETWORK_OUTBOUND
    return net


def clamp_memory(value):
    if not isinstance(value, int) or isinstance(value, bool):
        return DEFAULT_MEMORY_MB
    return max(MIN_MEMORY_MB, min(MAX_MEMORY_MB, value))


def manifest_companions(manifest):
    return tuple(c for c in (manifest or {}).get("companions") or () if c in COMPANIONS)


@dataclass(frozen=True)
class Wheel:
    url: str
    sha256: str


@dataclass(frozen=True)
class AppPlan:
    """Everything needed to run one app container. Built by the manager from
    the hub's request, the catalog and the installed manifest; the driver turns
    it into hardened containers using the platform templates."""

    app_id: str
    version: str
    slot: int
    network: str
    memory_mb: int
    token: str
    lifecycle: str = "on_demand"
    companions: tuple = ()
    time_zone: str = "UTC"
    hub_brand: str = "{}"
    extra: dict = field(default_factory=dict, hash=False)
