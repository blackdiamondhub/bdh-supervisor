"""The Docker implementation of :class:`~bdh_supervisor.driver.RuntimeDriver`.

Every container it creates comes from :mod:`bdh_supervisor.templates`; this
module only maps those specs onto the Docker API.
"""
import io
import json
import logging
import tarfile
import urllib.request

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.types import IPAMConfig, IPAMPool, Mount

from . import templates as T
from .driver import ContainerState, DriverError, RuntimeDriver
from .models import NETWORK_LAN

log = logging.getLogger("bdh.supervisor.docker")

GATEWAY_IP_LABEL = "bdh.gateway_ip"
APP_JSON = "/opt/bdh/app.json"
BASE_LABEL = "org.blackdiamondlodge.bdh.runtime"
HUB_API_LABEL = "org.blackdiamondlodge.bdh.hub_api"
MAX_MANIFEST_BYTES = 256 * 1024


class DockerDriver(RuntimeDriver):
    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.client = client or docker.from_env(version="auto", timeout=120)
        self.api = self.client.api
        self.firewall_ok = False

    # -- shared pieces -------------------------------------------------

    def prepare(self):
        self._ensure_image(self.cfg.supervisor_image)
        self._ensure_network(T.egress_network_spec(self.cfg))
        self.firewall_ok = self.apply_firewall()
        self._ensure_gateway()
        return {"firewall": self.firewall_ok}

    def apply_firewall(self):
        if self.cfg.firewall == "off":
            return False
        try:
            code, output = self._run_once(T.firewall_container(self.cfg, self.cfg.supervisor_image), 60)
        except DriverError as exc:
            log.error("firewall helper failed: %s", exc)
            return False
        if code != 0:
            log.error("firewall helper exited %s: %s", code, output[-2000:])
            return False
        return True

    def _image_id(self, ref):
        try:
            return self.client.images.get(ref).id
        except ImageNotFound:
            return ""

    def _ensure_image(self, ref):
        if self._image_id(ref):
            return
        try:
            self.api.pull(ref)
        except APIError as exc:
            raise DriverError(f"cannot pull {ref}: {exc}") from exc

    def _ensure_network(self, spec, extra_labels=None):
        wanted_labels = {**spec["labels"], **(extra_labels or {})}
        try:
            net = self.client.networks.get(spec["name"])
            attrs = net.attrs
            subnets = [c.get("Subnet") for c in (attrs.get("IPAM") or {}).get("Config") or []]
            same = (subnets == [spec["subnet"]] and bool(attrs.get("Internal")) == spec["internal"]
                    and all((attrs.get("Labels") or {}).get(k) == v for k, v in wanted_labels.items()))
            if same:
                return net
            log.info("recreating network %s", spec["name"])
            net.reload()
            for cid in list((net.attrs.get("Containers") or {}).keys()):
                try:
                    net.disconnect(cid, force=True)
                except APIError:
                    pass
            net.remove()
        except NotFound:
            pass
        return self.client.networks.create(
            spec["name"], driver=spec["driver"], internal=spec["internal"], check_duplicate=True,
            ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=spec["subnet"])]),
            labels=wanted_labels, options=spec["options"],
        )

    def _ensure_gateway(self):
        image_id = self._image_id(self.cfg.supervisor_image)
        try:
            c = self.client.containers.get(T.GATEWAY_NAME)
            if c.status == "running" and c.image.id == image_id:
                self._connect_gateway_everywhere()
                return
            c.remove(force=True)
        except NotFound:
            pass
        self._create(T.gateway_container(self.cfg, self.cfg.supervisor_image), start=True)
        self._connect_gateway_everywhere()

    def _connect_gateway_everywhere(self):
        for net in self.client.networks.list(filters={"label": [f"{T.MANAGED}=1", "bdh.role=network"]}):
            ip = (net.attrs.get("Labels") or {}).get(GATEWAY_IP_LABEL)
            if ip:
                self._connect_gateway(net, ip)

    def _connect_gateway(self, net, ip):
        net.reload()
        for cid, info in (net.attrs.get("Containers") or {}).items():
            if info.get("Name") == T.GATEWAY_NAME:
                if (info.get("IPv4Address") or "").split("/")[0] == ip:
                    return
                net.disconnect(cid, force=True)
        net.connect(T.GATEWAY_NAME, ipv4_address=ip)

    # -- containers ----------------------------------------------------

    def _create(self, spec, start=False):
        self._remove_container(spec["name"])
        host_kwargs = dict(spec["host_config"])
        mounts = [Mount(m["target"], m["source"], type=m["type"], read_only=m["read_only"])
                  for m in spec["mounts"]]
        host_config = self.api.create_host_config(mounts=mounts or None, **host_kwargs)
        networking = None
        if spec["network"]:
            endpoint = self.api.create_endpoint_config(ipv4_address=spec["ipv4_address"]) \
                if spec["ipv4_address"] else self.api.create_endpoint_config()
            networking = self.api.create_networking_config({spec["network"]: endpoint})
        created = self.api.create_container(
            spec["image"], command=spec["command"], name=spec["name"], user=spec["user"],
            environment=spec["environment"], labels=spec["labels"], entrypoint=spec["entrypoint"],
            working_dir=spec.get("working_dir"), healthcheck=spec.get("healthcheck"),
            host_config=host_config, networking_config=networking, detach=True,
            network_disabled=spec["host_config"].get("network_mode") == "none",
        )
        if start:
            self.api.start(created["Id"])
        return created["Id"]

    def _remove_container(self, name):
        try:
            self.api.remove_container(name, force=True)
        except NotFound:
            pass

    def _run_once(self, spec, timeout):
        cid = self._create(spec, start=True)
        try:
            try:
                result = self.api.wait(cid, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - requests timeout
                raise DriverError(f"{spec['name']} timed out after {timeout}s") from exc
            output = self.api.logs(cid, stdout=True, stderr=True).decode("utf-8", "replace")
            return result.get("StatusCode", 1), output
        finally:
            self._remove_container(cid)

    def _ensure_volume(self, name, app_id, role):
        try:
            self.client.volumes.get(name)
        except NotFound:
            self.client.volumes.create(name, labels=T.labels(app_id, role))

    # -- images ----------------------------------------------------------

    def _pull(self, ref):
        try:
            self.api.pull(ref)
            return self.api.inspect_image(ref)
        except (APIError, NotFound) as exc:
            raise DriverError(f"cannot pull {ref}: {exc}") from exc

    def pull(self, app_id, ref):
        base = self._pull(ref.base)
        app = self._pull(ref.image)
        check_image(app, base, self.cfg.hub_api)
        manifest = self._read_manifest(app_id, ref.image)
        if manifest.get("id") != app_id:
            raise DriverError(f"{ref.image} is app {manifest.get('id')!r}, not {app_id!r}")
        if manifest.get("version") != ref.version:
            raise DriverError(f"{ref.image} is version {manifest.get('version')!r}, not {ref.version!r}")
        return {"manifest": manifest, "image_id": app["Id"]}

    def _read_manifest(self, app_id, image):
        spec = T.manifest_probe(app_id, image)
        cid = self._create(spec)
        try:
            stream, _ = self.api.get_archive(cid, APP_JSON)
            raw = b"".join(stream)
        except NotFound as exc:
            raise DriverError(f"{image} has no {APP_JSON}; was it built by the bdh builder?") from exc
        finally:
            self._remove_container(cid)
        try:
            with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                member = tar.next()
                if member is None or not member.isfile() or member.size > MAX_MANIFEST_BYTES:
                    raise DriverError(f"{image}: bad {APP_JSON}")
                data = json.loads(tar.extractfile(member).read())
        except (tarfile.TarError, ValueError) as exc:
            raise DriverError(f"{image}: unreadable {APP_JSON}: {exc}") from exc
        if not isinstance(data, dict):
            raise DriverError(f"{image}: {APP_JSON} is not an object")
        return data

    def snapshot(self, app_id, image):
        snap = T.snapshot_volume(app_id)
        try:
            self.client.volumes.get(snap).remove(force=True)
        except NotFound:
            pass
        self._ensure_volume(T.data_volume(app_id), app_id, "data")
        self._ensure_volume(snap, app_id, "snapshot")
        self._copy(app_id, image, "snapshot")

    def restore(self, app_id, image):
        try:
            self.client.volumes.get(T.snapshot_volume(app_id))
        except NotFound as exc:
            raise DriverError(f"{app_id} has no data snapshot") from exc
        self._ensure_volume(T.data_volume(app_id), app_id, "data")
        self._copy(app_id, image, "restore")

    def _copy(self, app_id, image, action):
        self._ensure_image(image)
        code, output = self._run_once(T.data_copy_container(app_id, image, action), self.cfg.snapshot_timeout)
        if code != 0:
            raise DriverError(f"{action} of {app_id} data failed ({code}): {output.strip()[-1500:]}")

    def gc(self, app_id, keep):
        name = f"{self.cfg.image_prefix}bdh-app-{app_id}"
        removed = []
        for image in self.api.images(name=name, all=False):
            if image["Id"] in keep:
                continue
            digests = image.get("RepoDigests") or []
            if not digests or not all(d.startswith(name + "@") for d in digests):
                continue  # shared with another repository (e.g. a base): leave it
            # (The containerd image store lists ``repo@digest`` as a tag too.)
            if any(t for t in image.get("RepoTags") or [] if not t.startswith((name + ":", name + "@"))):
                continue
            try:
                self.api.remove_image(image["Id"], force=False, noprune=False)
                removed.append(image["Id"])
            except (APIError, NotFound) as exc:
                log.info("gc: keeping %s: %s", image["Id"], exc)
        return removed

    # -- running -------------------------------------------------------

    def start(self, plan):
        if plan.network != NETWORK_LAN and self.cfg.firewall == "required" and not self.firewall_ok:
            self.firewall_ok = self.apply_firewall()
            if not self.firewall_ok:
                raise DriverError("the host firewall is not in place; refusing to start app containers")
        self._ensure_image(plan.image)
        self._ensure_volume(T.data_volume(plan.app_id), plan.app_id, "data")
        self._remove_app_containers(plan.app_id)
        net_spec = T.network_spec(self.cfg, plan)
        if net_spec:
            gw_ip = T.gateway_ip(self.cfg, plan.slot, plan.network)
            net = self._ensure_network(net_spec, {GATEWAY_IP_LABEL: gw_ip})
            self._ensure_gateway_running()
            self._connect_gateway(net, gw_ip)
        else:
            self._remove_network(plan.app_id)
        for name in plan.companions:
            self._create(T.companion_container(self.cfg, plan, name), start=True)
        self._create(T.app_container(self.cfg, plan), start=True)

    def _ensure_gateway_running(self):
        try:
            c = self.client.containers.get(T.GATEWAY_NAME)
            if c.status == "running":
                return
        except NotFound:
            pass
        self._ensure_gateway()

    def _app_containers(self, app_id):
        return self.client.containers.list(all=True, filters={"label": [f"{T.MANAGED}=1", f"bdh.app={app_id}"]})

    def _remove_app_containers(self, app_id):
        for c in self._app_containers(app_id):
            try:
                c.remove(force=True)
            except NotFound:
                pass

    def _remove_network(self, app_id):
        try:
            net = self.client.networks.get(T.app_network(app_id))
        except NotFound:
            return
        net.reload()
        for cid in list((net.attrs.get("Containers") or {}).keys()):
            try:
                net.disconnect(cid, force=True)
            except APIError:
                pass
        net.remove()

    def stop(self, app_id):
        for c in self._app_containers(app_id):
            if c.status == "running":
                c.stop(timeout=10)

    def remove(self, app_id, purge_data=False):
        self._remove_app_containers(app_id)
        self._remove_network(app_id)
        names = [T.data_volume(app_id), T.snapshot_volume(app_id)] if purge_data else []
        for name in names:
            try:
                self.client.volumes.get(name).remove(force=True)
            except NotFound:
                pass

    def state(self, app_id):
        try:
            attrs = self.api.inspect_container(T.container_name(app_id))
        except NotFound:
            return ContainerState()
        st = attrs.get("State") or {}
        return ContainerState(
            exists=True, running=bool(st.get("Running")), exit_code=st.get("ExitCode"),
            oom_killed=bool(st.get("OOMKilled")), started_at=st.get("StartedAt") or "",
            image_id=attrs.get("Image") or "", status=st.get("Status") or "",
        )

    def logs(self, app_id, lines):
        try:
            raw = self.api.logs(T.container_name(app_id), stdout=True, stderr=True, tail=lines)
        except NotFound:
            return ""
        return raw.decode("utf-8", "replace")

    def stats(self, app_id):
        try:
            # one_shot skips the second sample Docker takes for CPU (~1 s); we only read memory.
            data = self.api.stats(T.container_name(app_id), stream=False, one_shot=True)
        except NotFound:
            return {}
        mem = data.get("memory_stats") or {}
        if not mem.get("usage"):
            return {}
        detail = mem.get("stats") or {}
        inactive = detail.get("inactive_file", detail.get("total_inactive_file", 0))
        return {
            "memory_bytes": mem.get("usage", 0),
            "memory_limit_bytes": mem.get("limit", 0),
            "working_set_bytes": max(0, mem.get("usage", 0) - inactive),
            "anon_bytes": detail.get("anon", detail.get("total_rss", 0)),
            "pids": (data.get("pids_stats") or {}).get("current", 0),
        }

    def health(self, plan, timeout=2.0):
        try:
            with urllib.request.urlopen(T.health_url(self.cfg, plan), timeout=timeout) as resp:
                return resp.status == 200 and json.loads(resp.read(4096)).get("ok") is True
        except (OSError, ValueError):
            return False


def check_image(app, base, hub_api):
    """Can ``app`` (``docker inspect`` of the app image) run on this supervisor?

    This is a compatibility check, not a security one (attestation is): the
    base must be a bdh runtime for this hub API, the app image's layers must
    extend the base's (so it really was built ``FROM`` it), and the image must
    not declare volumes or change the base's environment.
    """
    base_cfg = base.get("Config") or {}
    labels = base_cfg.get("Labels") or {}
    if labels.get(BASE_LABEL) != "1":
        raise DriverError("the base is not a bdh-app-runtime image")
    if labels.get(HUB_API_LABEL) != str(hub_api):
        raise DriverError(f"the base is for hub API {labels.get(HUB_API_LABEL)}, this supervisor runs {hub_api}")
    base_layers = (base.get("RootFS") or {}).get("Layers") or []
    app_layers = (app.get("RootFS") or {}).get("Layers") or []
    if not base_layers or app_layers[:len(base_layers)] != base_layers:
        raise DriverError("the app image is not built on its catalog base (layers differ)")
    app_cfg = app.get("Config") or {}
    if app_cfg.get("Volumes"):
        raise DriverError(f"the app image declares volumes: {sorted(app_cfg['Volumes'])}")
    if sorted(app_cfg.get("Env") or []) != sorted(base_cfg.get("Env") or []):
        raise DriverError("the app image changes its base's environment")
