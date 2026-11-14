"""App state and lifecycle: installs, starts, idle stops, crash backoff."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from . import templates as T
from .catalog import CatalogError
from .driver import DriverError
from .models import (
    AppPlan,
    clamp_memory,
    effective_network,
    manifest_companions,
)

log = logging.getLogger("bdh.supervisor")

INSTALLING, INSTALLED, FAILED = "installing", "installed", "failed"
RUNNING, STOPPED, IDLE = "running", "stopped", "idle"
PUBLIC_FIELDS = (
    "app_id", "version", "slot", "approved_network", "allow_lan", "status", "error",
    "installed_version", "desired", "failures", "last_exit", "last_activity", "manifest",
)


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class Manager:
    def __init__(self, cfg, driver, catalog, clock=time.time):
        self.cfg = cfg
        self.driver = driver
        self.catalog = catalog
        self.clock = clock
        self.path = Path(cfg.state_dir) / "apps.json"
        self.apps = self._load()
        self.locks = {}
        self.tasks = set()
        self.prepared = False
        self.prepare_info = {}
        self.prepare_error = ""
        self.runtime_image_id = ""

    # -- persistence -----------------------------------------------------

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except ValueError:
            log.error("%s is corrupt; starting empty", self.path)
            return {}
        return data.get("apps", {}) if isinstance(data, dict) else {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"apps": self.apps}, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def lock(self, app_id):
        return self.locks.setdefault(app_id, asyncio.Lock())

    def get(self, app_id):
        rec = self.apps.get(app_id)
        if rec is None:
            raise NotFoundError(app_id)
        return rec

    # -- plans and views -------------------------------------------------

    def plan(self, rec):
        manifest = rec.get("manifest") or {}
        return AppPlan(
            app_id=rec["app_id"],
            version=rec.get("installed_version") or rec["version"],
            slot=rec["slot"],
            network=effective_network(rec["approved_network"], manifest.get("network", rec["approved_network"]),
                                      rec.get("allow_lan", False)),
            memory_mb=clamp_memory(manifest.get("memory_mb")),
            token=rec["token"],
            lifecycle=manifest.get("lifecycle", "on_demand"),
            companions=manifest_companions(manifest),
            time_zone=rec.get("time_zone") or "UTC",
            hub_brand=json.dumps(rec.get("hub_brand") or {}, separators=(",", ":")),
        )

    def view(self, rec, state=None):
        out = {k: rec.get(k) for k in PUBLIC_FIELDS}
        plan = self.plan(rec)
        out["network"] = plan.network
        out["lifecycle"] = plan.lifecycle
        out["memory_mb"] = plan.memory_mb
        out["upstream"] = T.upstream(self.cfg, plan)
        if state is not None:
            out["running"] = state.running
            out["container_status"] = state.status
        return out

    # -- install ---------------------------------------------------------

    async def put(self, app_id, req):
        """Record what the hub wants and install it in the background."""
        for other_id, other in self.apps.items():
            if other_id != app_id and other["slot"] == req["slot"]:
                raise ConflictError(f"slot {req['slot']} is used by {other_id}")
        async with self.lock(app_id):
            rec = self.apps.get(app_id)
            fresh = rec is None
            if fresh:
                rec = {"app_id": app_id, "installed_version": "", "manifest": {}, "desired": IDLE,
                       "failures": 0, "next_start": 0, "last_start": 0, "last_activity": 0,
                       "last_exit": None, "error": "", "status": INSTALLING}
            new = {"token": req["token"], "slot": req["slot"], "approved_network": req["network"],
                   "allow_lan": req["allow_lan"]}
            changed_runtime = not fresh and any(rec.get(k) != v for k, v in new.items())
            rec.update({
                "version": req["version"], **new,
                "time_zone": req["time_zone"], "hub_brand": req["hub_brand"],
            })
            self.apps[app_id] = rec
            needs_install = rec.get("installed_version") != req["version"] or rec["status"] == FAILED
            if needs_install:
                rec["status"], rec["error"] = INSTALLING, ""
            self.save()
        if needs_install:
            self._spawn(self._install(app_id))
        elif changed_runtime and rec.get("desired") == RUNNING:
            self._spawn(self._restart_quietly(app_id))
        return self.view(rec)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _install(self, app_id):
        async with self.lock(app_id):
            rec = self.apps.get(app_id)
            if rec is None:
                return
            version = rec["version"]
            try:
                wheels = await asyncio.to_thread(self.catalog.wheels, app_id, version, self.cfg.hub_api)
                manifest = await asyncio.to_thread(self.driver.install, app_id, version, wheels)
                if manifest.get("id") != app_id:
                    raise DriverError(f"installed manifest is for {manifest.get('id')!r}")
            except (CatalogError, DriverError, OSError) as exc:
                log.error("install %s %s failed: %s", app_id, version, exc)
                if self.apps.get(app_id) is rec:
                    rec["status"], rec["error"] = FAILED, str(exc)[:2000]
                    self.save()
                return
            if self.apps.get(app_id) is not rec or rec["version"] != version:
                return
            rec.update({"status": INSTALLED, "error": "", "installed_version": version,
                        "manifest": manifest, "failures": 0, "next_start": 0})
            self.save()
            log.info("installed %s %s", app_id, version)
            restart = rec.get("desired") == RUNNING or manifest.get("lifecycle") == "always"
        if restart:
            await self._restart_quietly(app_id)

    async def _restart_quietly(self, app_id):
        try:
            await self.start(app_id, wait=False)
        except (ConflictError, DriverError, NotFoundError) as exc:
            log.warning("start %s failed: %s", app_id, exc)

    # -- lifecycle -------------------------------------------------------

    async def start(self, app_id, wait=True):
        async with self.lock(app_id):
            rec = self.get(app_id)
            if rec["status"] != INSTALLED and not rec.get("installed_version"):
                raise ConflictError(f"{app_id} is not installed ({rec['status']})")
            now = self.clock()
            rec.update({"desired": RUNNING, "last_activity": now, "last_start": now, "next_start": 0})
            plan = self.plan(rec)
            try:
                await asyncio.to_thread(self.driver.start, plan)
            except (DriverError, OSError) as exc:
                self._record_failure(rec, str(exc))
                self.save()
                raise DriverError(str(exc)) from exc
            self.save()
        healthy = await self.wait_healthy(plan) if wait else None
        return {**self.view(rec), "healthy": healthy}

    async def wait_healthy(self, plan, timeout=None):
        deadline = time.monotonic() + (timeout or self.cfg.start_timeout)
        while time.monotonic() < deadline:
            if await asyncio.to_thread(self.driver.health, plan):
                return True
            state = await asyncio.to_thread(self.driver.state, plan.app_id)
            if not state.running:
                return False
            await asyncio.sleep(0.5)
        return False

    async def stop(self, app_id, desired=STOPPED):
        async with self.lock(app_id):
            rec = self.get(app_id)
            rec["desired"] = desired
            rec["next_start"] = 0
            self.save()
            await asyncio.to_thread(self.driver.stop, app_id)
        return self.view(rec)

    async def activity(self, app_id):
        rec = self.get(app_id)
        rec["last_activity"] = self.clock()
        state = await asyncio.to_thread(self.driver.state, app_id)
        return {"app_id": app_id, "running": state.running, "desired": rec["desired"]}

    async def delete(self, app_id, purge_data=False):
        async with self.lock(app_id):
            self.get(app_id)
            await asyncio.to_thread(self.driver.remove, app_id, purge_data)
            del self.apps[app_id]
            self.save()
        self.locks.pop(app_id, None)

    async def status(self, app_id):
        rec = self.get(app_id)
        state = await asyncio.to_thread(self.driver.state, app_id)
        return self.view(rec, state)

    async def health(self, app_id):
        rec = self.get(app_id)
        state = await asyncio.to_thread(self.driver.state, app_id)
        healthy = state.running and await asyncio.to_thread(self.driver.health, self.plan(rec))
        return {"app_id": app_id, "running": state.running, "healthy": bool(healthy),
                "status": state.status, "exit_code": state.exit_code, "oom_killed": state.oom_killed,
                "failures": rec.get("failures", 0)}

    def _record_failure(self, rec, reason):
        rec["failures"] = rec.get("failures", 0) + 1
        delay = min(self.cfg.backoff_base * 2 ** (rec["failures"] - 1), self.cfg.backoff_max)
        rec["next_start"] = self.clock() + delay
        rec["error"] = reason[:2000]
        log.warning("%s failed (%s); retry in %ss", rec["app_id"], reason, delay)

    # -- background ------------------------------------------------------

    async def prepare(self):
        try:
            self.prepare_info = await asyncio.to_thread(self.driver.prepare)
            self.prepared, self.prepare_error = True, ""
            self.runtime_image_id = self.prepare_info.get("runtime_image", "") or self.runtime_image_id
        except (DriverError, OSError, Exception) as exc:  # noqa: BLE001 - keep the loop alive
            self.prepare_error = str(exc)
            log.error("prepare failed: %s", exc)
        return self.prepared

    async def reconcile_app(self, app_id):
        rec = self.apps.get(app_id)
        if rec is None or not rec.get("installed_version"):
            return
        lock = self.lock(app_id)
        if lock.locked():
            return
        async with lock:
            state = await asyncio.to_thread(self.driver.state, app_id)
            now = self.clock()
            plan = self.plan(rec)
            if rec["desired"] != RUNNING:
                return
            if state.running:
                if rec.get("failures") and now - rec.get("last_start", 0) > self.cfg.stable_after:
                    rec["failures"], rec["error"] = 0, ""
                    self.save()
                if plan.lifecycle == "on_demand" and now - rec.get("last_activity", 0) > self.cfg.idle_timeout:
                    log.info("%s idle; stopping", app_id)
                    rec["desired"] = IDLE
                    self.save()
                    await asyncio.to_thread(self.driver.stop, app_id)
                return
            if not rec.get("next_start"):
                reason = "out of memory" if state.oom_killed else f"exited with {state.exit_code}"
                rec["last_exit"] = {"code": state.exit_code, "oom_killed": state.oom_killed, "at": now}
                self._record_failure(rec, f"{app_id} {reason}" if state.exists else f"{app_id} is missing")
                self.save()
                return
            if now < rec["next_start"]:
                return
            rec["last_start"] = now
            rec["next_start"] = 0
            try:
                await asyncio.to_thread(self.driver.start, plan)
            except (DriverError, OSError) as exc:
                self._record_failure(rec, str(exc))
            self.save()

    async def refresh_runtime(self):
        new_id = await asyncio.to_thread(self.driver.refresh_runtime)
        restarted = []
        if new_id:
            self.runtime_image_id = new_id
            for app_id, rec in list(self.apps.items()):
                if rec.get("desired") != RUNNING:
                    continue
                state = await asyncio.to_thread(self.driver.state, app_id)
                if state.running and state.image_id and state.image_id != new_id:
                    await self._restart_quietly(app_id)
                    restarted.append(app_id)
        return {"runtime_image": new_id, "restarted": restarted}

    async def run_forever(self):
        last_prepare = last_pull = 0.0
        while True:
            now = time.monotonic()
            try:
                if not self.prepared or now - last_prepare > self.cfg.firewall_interval:
                    if await self.prepare():
                        last_prepare = now
                        if not last_pull:
                            last_pull = now
                if self.prepared and now - last_pull > self.cfg.runtime_pull_interval:
                    last_pull = now
                    await self.refresh_runtime()
                for app_id in list(self.apps):
                    await self.reconcile_app(app_id)
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("reconcile failed")
            await asyncio.sleep(self.cfg.reconcile_interval)
