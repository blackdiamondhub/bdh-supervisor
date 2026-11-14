"""The seam between the supervisor and a container runtime.

The manager only talks to a :class:`RuntimeDriver`; :class:`DockerDriver`
(in :mod:`bdh_supervisor.docker_driver`) is the one real implementation, and
tests use a fake. Drivers are blocking; the manager calls them from threads.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


class DriverError(Exception):
    pass


@dataclass
class ContainerState:
    exists: bool = False
    running: bool = False
    exit_code: int | None = None
    oom_killed: bool = False
    started_at: str = ""
    image_id: str = ""
    status: str = "missing"


class RuntimeDriver(ABC):
    @abstractmethod
    def prepare(self):
        """Pull images and set up shared pieces (egress network, firewall, gateway).

        Returns ``{"firewall": bool, ...}``. Called at startup and periodically.
        """

    @abstractmethod
    def refresh_runtime(self):
        """Pull the runtime image; return its (possibly new) image id."""

    @abstractmethod
    def install(self, app_id, version, wheels):
        """Install ``wheels`` as ``version`` in the app's code volume in disposable
        worker containers; return the manifest dict the app's PluginSpec reports."""

    @abstractmethod
    def start(self, plan):
        """(Re)create and start the app's container(s) for ``plan``."""

    @abstractmethod
    def stop(self, app_id):
        """Stop the app's containers (they are recreated on the next start)."""

    @abstractmethod
    def remove(self, app_id, purge_data=False):
        """Remove the app's containers, network and code volume (and data volume
        when ``purge_data``)."""

    @abstractmethod
    def state(self, app_id) -> ContainerState:
        """The app container's state."""

    @abstractmethod
    def logs(self, app_id, lines):
        """The last ``lines`` lines of the app container's output."""

    @abstractmethod
    def stats(self, app_id):
        """``{"memory_bytes", "memory_limit_bytes", "working_set_bytes", ...}`` or {}."""

    @abstractmethod
    def health(self, plan, timeout=2.0):
        """True if the app answers its health check."""
