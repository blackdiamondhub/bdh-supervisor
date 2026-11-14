import asyncio
import json
import os

import pytest

from bdh_supervisor.catalog import CatalogError
from bdh_supervisor.driver import DriverError
from bdh_supervisor.manager import (
    FAILED,
    IDLE,
    INSTALLED,
    RUNNING,
    STOPPED,
    ConflictError,
    Manager,
    NotFoundError,
)

from .conftest import TOKEN


def request(**kw):
    base = {"version": "1.0.0", "token": TOKEN, "slot": 3, "network": "outbound", "allow_lan": False,
            "time_zone": "America/Vancouver", "hub_brand": {"name": "BDL"}}
    base.update(kw)
    return base


@pytest.fixture
def manager(cfg, driver, catalog, clock):
    return Manager(cfg, driver, catalog, clock=clock)


async def settle(manager):
    while manager.tasks:
        await asyncio.gather(*list(manager.tasks))


async def installed(manager, app_id="hello", **kw):
    await manager.put(app_id, request(**kw))
    await settle(manager)
    return manager.apps[app_id]


async def test_put_installs_in_background(manager, driver, catalog):
    view = await manager.put("hello", request())
    assert view["status"] == "installing"
    assert view["upstream"] == "http://10.213.3.2:8000"
    assert "token" not in view
    await settle(manager)
    rec = manager.apps["hello"]
    assert rec["status"] == INSTALLED and rec["installed_version"] == "1.0.0"
    assert catalog.calls == [("hello", "1.0.0", 2)]
    assert [c[0] for c in driver.calls] == ["install"]


async def test_state_is_persisted_privately(manager, cfg, driver, catalog, clock):
    await installed(manager)
    path = os.path.join(cfg.state_dir, "apps.json")
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert json.load(open(path))["apps"]["hello"]["installed_version"] == "1.0.0"
    again = Manager(cfg, driver, catalog, clock=clock)
    assert again.apps["hello"]["status"] == INSTALLED


async def test_install_failure_is_reported_and_retried(manager, driver, catalog):
    catalog.error = CatalogError("hello 1.0.0 is not in the catalog")
    rec = await installed(manager)
    assert rec["status"] == FAILED and "not in the catalog" in rec["error"]
    with pytest.raises(ConflictError):
        await manager.start("hello")
    catalog.error = None
    rec = await installed(manager)
    assert rec["status"] == INSTALLED


async def test_manifest_for_another_app_is_rejected(manager, driver):
    driver.manifests["hello"] = {"id": "evil"}
    rec = await installed(manager)
    assert rec["status"] == FAILED and "evil" in rec["error"]


async def test_same_version_put_does_not_reinstall(manager, driver):
    await installed(manager)
    await installed(manager)
    assert [c[0] for c in driver.calls] == ["install"]


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
    assert driver.started()[-1].version == "1.1.0"


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
    with pytest.raises(NotFoundError):
        await manager.delete("hello")


async def test_refresh_runtime_restarts_apps_on_old_image(manager, driver):
    await installed(manager)
    await manager.start("hello")
    driver.image_id = "sha256:runtime2"
    out = await manager.refresh_runtime()
    assert out == {"runtime_image": "sha256:runtime2", "restarted": ["hello"]}
    assert driver.containers["hello"]["image"] == "sha256:runtime2"


async def test_prepare_failure_is_kept(manager, driver):
    def boom():
        raise DriverError("firewall missing")

    driver.prepare = boom
    assert await manager.prepare() is False
    assert manager.prepare_error == "firewall missing"
