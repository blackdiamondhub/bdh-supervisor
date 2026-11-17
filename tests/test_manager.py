import asyncio
import json
import os

import pytest

from bdh_supervisor.attest import AttestationError
from bdh_supervisor.catalog import CatalogError
from bdh_supervisor.driver import DriverError
from bdh_supervisor.manager import (
    FAILED,
    IDLE,
    INSTALLED,
    INSTALLING,
    RUNNING,
    STOPPED,
    ConflictError,
    Manager,
    NotFoundError,
)

from .conftest import TOKEN, image_for


def request(**kw):
    base = {"version": "1.0.0", "token": TOKEN, "slot": 3, "network": "outbound", "allow_lan": False,
            "time_zone": "America/Vancouver", "hub_brand": {"name": "BDL"}}
    base.update(kw)
    return base


@pytest.fixture
def manager(cfg, driver, catalog, verifier, clock):
    return Manager(cfg, driver, catalog, verifier, clock=clock)


async def settle(manager):
    while manager.tasks:
        await asyncio.gather(*list(manager.tasks))


async def installed(manager, app_id="hello", **kw):
    await manager.put(app_id, request(**kw))
    await settle(manager)
    return manager.apps[app_id]


async def test_put_installs_in_background(manager, driver, catalog, verifier):
    view = await manager.put("hello", request())
    assert view["status"] == "installing"
    assert view["upstream"] == "http://10.213.3.2:8000"
    assert "token" not in view
    await settle(manager)
    rec = manager.apps["hello"]
    image = image_for("hello", "1.0.0")
    assert rec["status"] == INSTALLED and rec["installed_version"] == "1.0.0"
    assert rec["image"] == image and rec["image_id"] == driver.image_id(image)
    assert rec["previous"] is None and rec["upgrade"] is None
    assert catalog.calls == [("hello", "1.0.0", 2)]
    assert verifier.verified == [image]
    # A fresh install has no data to snapshot.
    assert [c[0] for c in driver.calls] == ["pull", "gc"]


async def test_state_is_persisted_privately(manager, cfg, driver, catalog, clock):
    await installed(manager)
    path = os.path.join(cfg.state_dir, "apps.json")
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert json.load(open(path))["apps"]["hello"]["installed_version"] == "1.0.0"
    again = Manager(cfg, driver, catalog, clock=clock)
    assert again.apps["hello"]["status"] == INSTALLED


async def test_unattested_image_is_refused(manager, driver, verifier):
    verifier.error = AttestationError("no attestations found")
    rec = await installed(manager)
    assert rec["status"] == FAILED and "no attestations" in rec["error"]
    assert not driver.named("pull") and not rec["installed_version"]


async def test_install_failure_is_reported_and_retried(manager, driver, catalog):
    catalog.error = CatalogError("hello 1.0.0 is not in the catalog")
    rec = await installed(manager)
    assert rec["status"] == FAILED and "not in the catalog" in rec["error"]
    with pytest.raises(ConflictError):
        await manager.start("hello")
    catalog.error = None
    rec = await installed(manager)
    assert rec["status"] == INSTALLED


async def test_pull_failure_is_reported(manager, driver):
    driver.pull_error = "the app image is not built on its catalog base"
    rec = await installed(manager)
    assert rec["status"] == FAILED and "catalog base" in rec["error"]


async def test_same_version_put_does_not_reinstall(manager, driver, catalog):
    await installed(manager)
    await installed(manager)
    assert len(driver.named("pull")) == 1
    assert len(catalog.calls) == 2


async def test_same_version_new_digest_is_an_upgrade(manager, driver, catalog):
    await installed(manager)
    catalog.builds["hello"] = "rebuild-2"
    rec = await installed(manager)
    assert rec["image"] == image_for("hello", "1.0.0", "rebuild-2") and rec["build"] == "rebuild-2"
    assert rec["previous"]["image"] == image_for("hello", "1.0.0")


async def test_slot_conflict(manager):
    await installed(manager)
    with pytest.raises(ConflictError):
        await manager.put("other", request(slot=3))


async def test_network_is_capped_by_admin_and_manifest(manager, driver):
    driver.manifest_for("hello", network="lan")
    rec = await installed(manager, network="outbound")
    assert manager.plan(rec).network == "outbound"
    rec = await installed(manager, network="lan", allow_lan=False)
    assert manager.plan(rec).network == "outbound"
    rec = await installed(manager, network="lan", allow_lan=True)
    assert manager.plan(rec).network == "lan"
    driver.manifest_for("other", network="none")
    rec = await installed(manager, "other", network="lan", allow_lan=True, slot=4)
    assert manager.plan(rec).network == "none"


async def test_memory_comes_from_manifest_clamped(manager, driver):
    driver.manifest_for("hello", memory_mb=1024)
    rec = await installed(manager)
    assert manager.plan(rec).memory_mb == 256


async def test_start_waits_for_health(manager, driver):
    await installed(manager)
    out = await manager.start("hello")
    assert out["healthy"] is True and out["desired"] == RUNNING
    plan = driver.started()[0]
    assert plan.token == TOKEN and plan.slot == 3 and plan.time_zone == "America/Vancouver"
    assert json.loads(plan.hub_brand) == {"name": "BDL"}


async def test_start_reports_unhealthy_when_container_dies(manager, driver):
    await installed(manager)
    driver.healthy = False
    orig = driver.start

    def start_then_die(plan):
        orig(plan)
        driver.crash(plan.app_id)

    driver.start = start_then_die
    out = await manager.start("hello")
    assert out["healthy"] is False


async def test_start_unknown_app(manager):
    with pytest.raises(NotFoundError):
        await manager.start("nope")


async def test_driver_start_error_counts_as_failure(manager, driver):
    await installed(manager)
    driver.start_error = "boom"
    with pytest.raises(DriverError):
        await manager.start("hello")
    assert manager.apps["hello"]["failures"] == 1


async def test_crash_backoff_doubles_and_resets_when_stable(manager, driver, clock, cfg):
    await installed(manager)
    await manager.start("hello")
    delays = []
    for _ in range(4):
        driver.crash("hello")
        await manager.reconcile_app("hello")
        rec = manager.apps["hello"]
        delays.append(rec["next_start"] - clock())
        await manager.reconcile_app("hello")  # still backing off
        assert driver.containers["hello"]["running"] is False
        clock.advance(delays[-1] + 0.1)
        await manager.reconcile_app("hello")
        assert driver.containers["hello"]["running"] is True
    assert delays == [cfg.backoff_base * 2 ** i for i in range(4)]
    assert manager.apps["hello"]["last_exit"]["code"] == 1
    manager.apps["hello"]["last_activity"] = clock() + cfg.stable_after
    clock.advance(cfg.stable_after + 1)
    await manager.reconcile_app("hello")
    assert manager.apps["hello"]["failures"] == 0


async def test_dead_companion_restarts_the_app(manager, driver, clock, cfg):
    driver.manifest_for("hello", companions=["go2rtc"])
    await installed(manager)
    await manager.start("hello")
    driver.containers["hello"]["companions"]["go2rtc"] = False
    await manager.reconcile_app("hello")
    rec = manager.apps["hello"]
    assert rec["failures"] == 1 and rec["last_exit"]["companions"] == ["go2rtc"]
    starts = len(driver.started())
    clock.advance(cfg.backoff_base + 0.1)
    await manager.reconcile_app("hello")
    assert len(driver.started()) == starts + 1
    assert driver.containers["hello"]["companions"] == {"go2rtc": True}


async def test_backoff_is_capped(manager, driver, clock, cfg):
    await installed(manager)
    await manager.start("hello")
    manager.apps["hello"]["failures"] = 50
    driver.crash("hello", oom=True)
    await manager.reconcile_app("hello")
    rec = manager.apps["hello"]
    assert rec["next_start"] - clock() == cfg.backoff_max
    assert "out of memory" in rec["error"]


async def test_on_demand_idle_stop_and_activity(manager, driver, clock, cfg):
    await installed(manager)
    await manager.start("hello")
    clock.advance(cfg.idle_timeout - 10)
    assert (await manager.activity("hello"))["running"] is True
    clock.advance(cfg.idle_timeout - 10)
    await manager.reconcile_app("hello")
    assert driver.containers["hello"]["running"] is True
    clock.advance(20)
    await manager.reconcile_app("hello")
    assert driver.containers["hello"]["running"] is False
    assert manager.apps["hello"]["desired"] == IDLE
    await manager.reconcile_app("hello")  # idle apps stay down
    assert driver.containers["hello"]["running"] is False


async def test_restart_gives_on_demand_apps_a_full_idle_window(manager, driver, catalog, clock, cfg):
    await installed(manager)
    await manager.start("hello")
    clock.advance(cfg.idle_timeout - 10)
    again = Manager(cfg, driver, catalog, clock=clock)  # supervisor restarts
    clock.advance(20)
    await again.reconcile_app("hello")
    assert driver.containers["hello"]["running"] is True
    clock.advance(cfg.idle_timeout)
    await again.reconcile_app("hello")
    assert driver.containers["hello"]["running"] is False


async def test_always_apps_start_after_install_and_never_idle(manager, driver, clock, cfg):
    driver.manifest_for("hello", lifecycle="always")
    await installed(manager)
    assert driver.containers["hello"]["running"] is True
    clock.advance(cfg.idle_timeout * 10)
    await manager.reconcile_app("hello")
    assert driver.containers["hello"]["running"] is True


async def test_stopped_apps_are_not_restarted(manager, driver):
    await installed(manager)
    await manager.start("hello")
    await manager.stop("hello")
    assert manager.apps["hello"]["desired"] == STOPPED
    await manager.reconcile_app("hello")
    assert driver.containers["hello"]["running"] is False


async def test_upgrade_restarts_running_app(manager, driver):
    await installed(manager)
    await manager.start("hello")
    await installed(manager, version="1.1.0")
    plan = driver.started()[-1]
    assert plan.version == "1.1.0" and plan.image == image_for("hello", "1.1.0")


async def test_upgrade_snapshots_data_keeps_previous_and_gcs(manager, driver):
    await installed(manager)
    await manager.start("hello")
    v1, v1_id = image_for("hello", "1.0.0"), driver.image_id(image_for("hello", "1.0.0"))
    driver.calls.clear()
    rec = await installed(manager, version="1.1.0")
    names = [c[0] for c in driver.calls]
    # Stop before the snapshot so /data is quiescent; snapshot with the old image.
    assert names.index("stop") < names.index("snapshot") < names.index("gc") < names.index("start")
    assert ("snapshot", "hello", v1) in driver.calls
    assert rec["previous"]["version"] == "1.0.0" and rec["previous"]["snapshot"] is True
    # GC keeps both the current and the N-1 image.
    keep = driver.named("gc")[-1][2]
    assert {v1_id, rec["image_id"]} <= keep and driver.removed_images == []
    await installed(manager, version="1.2.0")
    assert driver.removed_images == [v1_id]
    assert manager.view(manager.apps["hello"])["previous_version"] == "1.1.0"


async def test_failed_upgrade_keeps_old_version_running(manager, driver):
    await installed(manager)
    await manager.start("hello")
    driver.snapshot_error = "disk full"
    rec = await installed(manager, version="1.1.0")
    assert rec["status"] == FAILED and "disk full" in rec["error"]
    assert rec["installed_version"] == "1.0.0" and rec["upgrade"] is None
    assert driver.containers["hello"]["running"] is True
    assert driver.started()[-1].version == "1.0.0"


async def test_interrupted_upgrade_resumes(manager, cfg, driver, catalog, verifier, clock):
    await installed(manager)
    rec = manager.apps["hello"]
    v2 = image_for("hello", "1.1.0")
    # Simulate a supervisor restart after the attestation check, before the pull.
    rec.update(version="1.1.0", status=INSTALLING,
               upgrade={"version": "1.1.0", "image": v2, "base": "b", "build": "", "stage": "verified"})
    manager.save()
    again = Manager(cfg, driver, catalog, verifier, clock=clock)
    await again.reconcile_app("hello")  # leaves a mid-upgrade app alone
    verifier.verified.clear()
    await again.resume()
    await settle(again)
    rec = again.apps["hello"]
    assert rec["status"] == INSTALLED and rec["image"] == v2 and rec["upgrade"] is None
    assert verifier.verified == []  # the attestation stage was already done
    assert rec["previous"]["version"] == "1.0.0"


async def test_rollback_restores_snapshot_and_previous_image(manager, driver, catalog):
    await installed(manager)
    await manager.start("hello")
    with pytest.raises(ConflictError):
        await manager.rollback("hello")
    await installed(manager, version="1.1.0")
    view = await manager.rollback("hello")
    v1 = image_for("hello", "1.0.0")
    assert ("restore", "hello", v1) in driver.calls
    assert view["installed_version"] == "1.0.0" and view["rollback_available"] is False
    assert driver.started()[-1].image == v1
    with pytest.raises(ConflictError):
        await manager.rollback("hello")  # the snapshot is spent
    # Catalog polls leave a rolled-back app alone; the next hub PUT upgrades it again.
    assert view["pinned"] is True
    catalog.builds["hello"] = "rebuilt"
    assert (await manager.poll_catalog())["upgraded"] == []
    assert (await installed(manager, version="1.1.0"))["installed_version"] == "1.1.0"


async def test_offline_catalog_keeps_installed_app(manager, driver, catalog):
    await installed(manager)
    await manager.start("hello")
    catalog.error = CatalogError("cannot fetch the catalog: offline")
    rec = await installed(manager, token="cd" * 32)
    assert rec["status"] == INSTALLED and rec["error"] == ""
    assert driver.started()[-1].token == "cd" * 32


async def test_token_change_restarts_running_app(manager, driver):
    await installed(manager)
    await manager.start("hello")
    await installed(manager, token="cd" * 32)
    assert driver.started()[-1].token == "cd" * 32


async def test_delete(manager, driver):
    await installed(manager)
    await manager.delete("hello", purge_data=True)
    assert "hello" not in manager.apps
    assert ("remove", "hello", True) in driver.calls
    assert driver.named("gc")[-1] == ("gc", "hello", frozenset())
    with pytest.raises(NotFoundError):
        await manager.delete("hello")


async def test_catalog_poll_upgrades_rebuilt_apps(manager, driver, catalog):
    await installed(manager)
    await manager.start("hello")
    assert await manager.poll_catalog() == {"upgraded": []}
    catalog.builds["hello"] = "base-bump"
    assert await manager.poll_catalog() == {"upgraded": ["hello"]}
    assert driver.started()[-1].image == image_for("hello", "1.0.0", "base-bump")


async def test_first_tick_after_restart_polls_catalog(manager, cfg, driver, catalog, verifier, clock):
    """A restart (e.g. Watchtower) must not push the next rebuild check a full interval out."""
    await installed(manager)
    catalog.builds["hello"] = "base-bump"
    restarted = Manager(cfg, driver, catalog, verifier, clock=clock)
    await restarted.tick(1000.0)
    await settle(restarted)
    assert restarted.apps["hello"]["image"] == image_for("hello", "1.0.0", "base-bump")
    catalog.builds["hello"] = "again"
    await restarted.tick(1000.0 + cfg.catalog_poll_interval / 2)
    await settle(restarted)
    assert restarted.apps["hello"]["image"] == image_for("hello", "1.0.0", "base-bump")
    await restarted.tick(1001.0 + cfg.catalog_poll_interval)
    await settle(restarted)
    assert restarted.apps["hello"]["image"] == image_for("hello", "1.0.0", "again")


async def test_prepare_failure_is_kept(manager, driver):
    def boom():
        raise DriverError("firewall missing")

    driver.prepare = boom
    assert await manager.prepare() is False
    assert manager.prepare_error == "firewall missing"
