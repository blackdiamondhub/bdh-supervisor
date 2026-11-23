"""The seam between the supervisor and a container runtime.

The manager only talks to a :class:`RuntimeDriver`; :class:`DockerDriver`
(in :mod:`bdh_supervisor.docker_driver`) is the one real implementation, and
tests use a fake. Drivers are blocking; the manager calls them from threads.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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
    # Companion name -> running, for the companions that exist.
    companions: dict = field(default_factory=dict)
    # Companions whose container runs an older image than their tag now points at.
    stale_companions: list = field(default_factory=list)


class RuntimeDriver(ABC):
    @abstractmethod
    def prepare(self):
        """Pull images (re-pulling companion tags) and set up shared pieces
        (egress network, firewall, gateway).

        Returns ``{"firewall": bool, ...}``. Called at startup and periodically.
        """

    @abstractmethod
    def pull(self, app_id, ref):
        """Pull ``ref`` (an :class:`~bdh_supervisor.models.ImageRef`) by digest and
        check it can run here: built on ``ref.base`` (its layers extend the
        base's), no ``VOLUME``s, no ``ENV`` beyond the base's, and its
        ``/opt/bdh/app.json`` names this app and version. Return
        ``{"manifest": {...}, "image_id": "sha256:..."}``."""

    @abstractmethod
    def snapshot(self, app_id, image):
        """Copy the app's data volume into its snapshot volume (using ``image``)."""

    @abstractmethod
    def restore(self, app_id, image):
        """Replace the app's data volume contents with its snapshot."""

    @abstractmethod
    def gc(self, app_id, keep):
        """Remove local images of the app that are not in ``keep`` (image ids);
        never prunes, never touches other repositories."""

    @abstractmethod
    def start(self, plan):
        """(Re)create and start the app's container(s) for ``plan``."""

    @abstractmethod
    def stop(self, app_id):
        """Stop the app's containers (they are recreated on the next start)."""

    @abstractmethod
    def remove(self, app_id, purge_data=False):
        """Remove the app's containers and network (and its data and snapshot
        volumes when ``purge_data``)."""

    @abstractmethod
    def state(self, app_id) -> ContainerState:
        """The app container's state (and whether its companions run)."""

    @abstractmethod
    def logs(self, app_id, lines):
        """The last ``lines`` lines of the app container's output."""

    @abstractmethod
    def stats(self, app_id):
        """``{"memory_bytes", "memory_limit_bytes", "working_set_bytes", ...}`` or {}."""

    @abstractmethod
    def health(self, plan, timeout=2.0):
        """True if the app answers its health check."""
