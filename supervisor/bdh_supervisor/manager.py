"""App state and lifecycle: installs/upgrades, rollback, starts, idle stops, crash backoff."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from . import templates as T
from .attest import AttestationError, NoVerification
from .catalog import CatalogError
from .driver import DriverError
from .models import (
    AppPlan,
    ImageRef,
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
    "image", "build", "pinned",
)


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class Manager:
    def __init__(self, cfg, driver, catalog, verifier=None, clock=time.time):
        self.cfg = cfg
        self.driver = driver
        self.catalog = catalog
        self.verifier = verifier or NoVerification()
        self.clock = clock
        self.path = Path(cfg.state_dir) / "apps.json"
        self.apps = self._load()
        # Activity isn't persisted, so give each app a full idle window after a
        # restart instead of stopping every on-demand app on the first pass.
        for rec in self.apps.values():
            rec["last_activity"] = clock()
        self.locks = {}
        self.tasks = set()
        self.prepared = False
        self.prepare_info = {}
        self.prepare_error = ""
        self.resumed = False
        self.last_poll = None
        self._last_prepare = 0.0
        self._polled_at = None

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
            image=rec.get("image") or "",
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
        out["previous_version"] = (rec.get("previous") or {}).get("version")
        out["rollback_available"] = bool((rec.get("previous") or {}).get("snapshot"))
        out["upgrade_stage"] = (rec.get("upgrade") or {}).get("stage")
        if state is not None:
            out["running"] = state.running
            out["container_status"] = state.status
        return out

    # -- install / upgrade -----------------------------------------------

    async def put(self, app_id, req):
        """Record what the hub wants and sync it (install/upgrade) in the background."""
        for other_id, other in self.apps.items():
            if other_id != app_id and other["slot"] == req["slot"]:
                raise ConflictError(f"slot {req['slot']} is used by {other_id}")
        async with self.lock(app_id):
            rec = self.apps.get(app_id)
            fresh = rec is None
            if fresh:
                rec = {"app_id": app_id, "installed_version": "", "manifest": {}, "desired": IDLE,
                       "image": "", "image_id": "", "base": "", "build": "", "previous": None,
                       "upgrade": None, "failures": 0, "next_start": 0, "last_start": 0,
                       "last_activity": 0, "last_exit": None, "error": "", "status": INSTALLING}
            new = {"token": req["token"], "slot": req["slot"], "approved_network": req["network"],
                   "allow_lan": req["allow_lan"]}
            changed_runtime = not fresh and any(rec.get(k) != v for k, v in new.items())
            rec.update({
                "version": req["version"], **new, "pinned": False,
                "time_zone": req["time_zone"], "hub_brand": req["hub_brand"],
            })
            self.apps[app_id] = rec
            if rec.get("installed_version") != req["version"] or rec["status"] == FAILED:
                rec["status"], rec["error"] = INSTALLING, ""
            self.save()
        # Always sync: the same version may have been rebuilt (new digest).
        self._spawn(self._sync(app_id, restart=changed_runtime))
        return self.view(rec)

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _keep(self):
        """Image ids GC must keep: every app's current, previous and in-flight image."""
        keep = set()
        for rec in self.apps.values():
            keep.add(rec.get("image_id"))
            keep.add((rec.get("previous") or {}).get("image_id"))
            keep.add((rec.get("upgrade") or {}).get("image_id"))
        keep.discard(None)
        keep.discard("")
        return keep

    async def _gc(self, app_id):
        try:
            removed = await asyncio.to_thread(self.driver.gc, app_id, self._keep())
            if removed:
                log.info("gc %s: removed %s", app_id, ", ".join(removed))
        except (DriverError, OSError, Exception) as exc:  # noqa: BLE001 - GC must never break an install
            log.warning("gc %s failed: %s", app_id, exc)

    async def _sync(self, app_id, restart=False, force=True):
        """Bring the app to the catalog's image for its version.

        Upgrades are staged in ``rec["upgrade"]`` and saved after every step
        (``pending`` -> ``verified`` -> ``pulled`` -> ``snapshot``), so a
        supervisor restart in the middle resumes where it stopped. Every step
        is idempotent.
        """
        start_after = False
        async with self.lock(app_id):
            rec = self.apps.get(app_id)
            if rec is None:
                return
            version = rec["version"]
            up = rec.get("upgrade")
            try:
                if up and up.get("version") == version:
                    ref = ImageRef(up["version"], up["image"], up["base"], up.get("build", ""))
                else:
                    ref = await asyncio.to_thread(self.catalog.image, app_id, version, self.cfg.hub_api, force)
            except (CatalogError, OSError) as exc:
                if rec.get("installed_version") == version and rec.get("image"):
                    # Offline (or the catalog dropped it): keep running what we have.
                    log.warning("sync %s: catalog unavailable, keeping %s: %s", app_id, rec["image"], exc)
                    if rec["status"] != INSTALLED:
                        rec["status"], rec["error"] = INSTALLED, ""
                        self.save()
                    start_after = restart and rec.get("desired") == RUNNING
                else:
                    self._fail(rec, f"catalog: {exc}")
                    return
            else:
                if rec.get("installed_version") == version and rec.get("image") == ref.image:
                    if rec.get("upgrade") or rec["status"] != INSTALLED:
                        rec.update({"upgrade": None, "status": INSTALLED, "error": ""})
                        self.save()
                    start_after = restart and rec.get("desired") == RUNNING
                else:
                    start_after = await self._upgrade(rec, ref)
        if start_after:
            await self._restart_quietly(app_id)

    async def _upgrade(self, rec, ref):
        app_id = rec["app_id"]
        up = rec.get("upgrade")
        if not up or up.get("image") != ref.image:
            up = {"version": ref.version, "image": ref.image, "base": ref.base, "build": ref.build,
                  "stage": "pending"}
            rec["upgrade"] = up
        rec["status"], rec["error"] = INSTALLING, ""
        self.save()
        stopped = False
        try:
            if up["stage"] == "pending":
                await asyncio.to_thread(self.verifier.verify, ref)
                up["stage"] = "verified"
                self.save()
            if up["stage"] == "verified":
                result = await asyncio.to_thread(self.driver.pull, app_id, ref)
                up.update(manifest=result["manifest"], image_id=result["image_id"], stage="pulled")
                self.save()
            if up["stage"] == "pulled":
                if rec.get("image"):
                    await asyncio.to_thread(self.driver.stop, app_id)
                    stopped = True
                    await asyncio.to_thread(self.driver.snapshot, app_id, rec["image"])
                up["stage"] = "snapshot"
                self.save()
        except (AttestationError, CatalogError, DriverError, OSError) as exc:
            log.error("install %s %s failed: %s", app_id, ref.version, exc)
            rec["upgrade"] = None
            self._fail(rec, str(exc))
            # The old version (if any) is untouched; bring it back if it was up.
            return stopped and rec.get("desired") == RUNNING
        previous = None
        if rec.get("image"):
            previous = {"version": rec.get("installed_version"), "image": rec["image"],
                        "image_id": rec.get("image_id", ""), "base": rec.get("base", ""),
                        "build": rec.get("build", ""), "manifest": rec.get("manifest") or {},
                        "snapshot": True}
        rec.update({
            "previous": previous or rec.get("previous"), "installed_version": ref.version,
            "image": ref.image, "image_id": up.get("image_id", ""), "base": ref.base, "build": ref.build,
            "manifest": up.get("manifest") or {}, "upgrade": None, "status": INSTALLED, "error": "",
            "failures": 0, "next_start": 0,
        })
        self.save()
        log.info("installed %s %s (%s)", app_id, ref.version, ref.image)
        await self._gc(app_id)
        return rec.get("desired") == RUNNING or rec["manifest"].get("lifecycle") == "always"

    def _fail(self, rec, reason):
        if self.apps.get(rec["app_id"]) is rec:
            rec["status"], rec["error"] = FAILED, reason[:2000]
            self.save()

    async def rollback(self, app_id):
        """Go back to the previous image and the data snapshot taken before the upgrade."""
        async with self.lock(app_id):
            rec = self.get(app_id)
            prev = rec.get("previous")
            if rec.get("upgrade"):
                raise ConflictError(f"{app_id} is being upgraded")
            if not prev or not prev.get("snapshot"):
                raise ConflictError(f"{app_id} has no previous version with a data snapshot")
            await asyncio.to_thread(self.driver.stop, app_id)
            await asyncio.to_thread(self.driver.restore, app_id, prev["image"])
            current = {"version": rec.get("installed_version"), "image": rec.get("image", ""),
                       "image_id": rec.get("image_id", ""), "base": rec.get("base", ""),
                       "build": rec.get("build", ""), "manifest": rec.get("manifest") or {},
                       "snapshot": False}
            rec.update({
                "version": prev["version"], "installed_version": prev["version"], "image": prev["image"],
                "image_id": prev.get("image_id", ""), "base": prev.get("base", ""),
                "build": prev.get("build", ""), "manifest": prev.get("manifest") or {},
                "previous": current, "status": INSTALLED, "error": "", "failures": 0, "next_start": 0,
                # Until the hub PUTs again, catalog polls must not re-upgrade it.
                "pinned": True,
            })
            self.save()
            log.warning("rolled %s back to %s", app_id, prev["version"])
            restart = rec.get("desired") == RUNNING or rec["manifest"].get("lifecycle") == "always"
        if restart:
            await self._restart_quietly(app_id)
        return self.view(rec)

    async def _restart_quietly(self, app_id):
        try:
            await self.start(app_id, wait=False)
        except (ConflictError, DriverError, NotFoundError) as exc:
            log.warning("start %s failed: %s", app_id, exc)

    # -- lifecycle -------------------------------------------------------

    async def start(self, app_id, wait=True):
        async with self.lock(app_id):
            rec = self.get(app_id)
            if not rec.get("installed_version") or not rec.get("image"):
                raise ConflictError(f"{app_id} is not installed ({rec['status']})")
            if rec.get("upgrade"):
                raise ConflictError(f"{app_id} is being upgraded")
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
            await self._gc(app_id)
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
        except (DriverError, OSError, Exception) as exc:  # noqa: BLE001 - keep the loop alive
            self.prepare_error = str(exc)
            log.error("prepare failed: %s", exc)
        return self.prepared

    async def reconcile_app(self, app_id):
        rec = self.apps.get(app_id)
        if rec is None or not rec.get("installed_version") or rec.get("upgrade"):
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
            down = [n for n in plan.companions if not state.companions.get(n)]
            if state.running and down and not rec.get("next_start"):
                # A dead companion (e.g. go2rtc OOM) leaves the app up but
                # useless; restart the set with the usual backoff.
                rec["last_exit"] = {"code": None, "oom_killed": False, "companions": down, "at": now}
                self._record_failure(rec, f"{app_id} companion {', '.join(down)} is not running")
                self.save()
                return
            if state.running and not down:
                if state.stale_companions:
                    # prepare() re-pulled a companion tag (e.g. a bdh-aec fix): pick it up.
                    log.info("%s: newer %s image; restarting", app_id, ", ".join(state.stale_companions))
                    rec["last_start"] = now
                    try:
                        await asyncio.to_thread(self.driver.start, plan)
                    except (DriverError, OSError) as exc:
                        self._record_failure(rec, str(exc))
                    self.save()
                    return
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

    async def resume(self):
        """Finish upgrades a restart interrupted (Watchtower may restart us mid-way)."""
        for app_id, rec in list(self.apps.items()):
            if rec.get("upgrade") or rec.get("status") == INSTALLING:
                log.info("resuming sync of %s", app_id)
                self._spawn(self._sync(app_id))
        self.resumed = True

    async def poll_catalog(self):
        """Sync every installed app with the catalog: a new digest for its version
        (a platform rebuild, e.g. on a new base) is an upgrade."""
        synced = []
        for app_id, rec in list(self.apps.items()):
            if (rec.get("installed_version") and not rec.get("upgrade") and rec.get("status") != FAILED
                    and not rec.get("pinned")):
                before = rec.get("image")
                await self._sync(app_id, force=False)
                if rec.get("image") != before:
                    synced.append(app_id)
        self.last_poll = self.clock()
        return {"upgraded": synced}

    async def tick(self, now):
        """One reconcile pass; ``now`` is a monotonic timestamp."""
        if not self.prepared or now - self._last_prepare > self.cfg.firewall_interval:
            if await self.prepare():
                self._last_prepare = now
        if self.prepared and not self.resumed:
            await self.resume()
        # Poll on the first prepared pass, not one interval later: Watchtower restarts
        # us on every supervisor release, which would otherwise postpone it indefinitely.
        if self.prepared and (self._polled_at is None
                              or now - self._polled_at > self.cfg.catalog_poll_interval):
            self._polled_at = now
            await self.poll_catalog()
        for app_id in list(self.apps):
            await self.reconcile_app(app_id)

    async def run_forever(self):
        while True:
            try:
                await self.tick(time.monotonic())
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("reconcile failed")
            await asyncio.sleep(self.cfg.reconcile_interval)
