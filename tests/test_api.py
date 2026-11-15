import pytest
from fastapi.testclient import TestClient

from bdh_supervisor.api import create_app

from .conftest import SUP_TOKEN, TOKEN


@pytest.fixture
def client(cfg, driver, catalog, verifier):
    app = create_app(cfg, driver, catalog, verifier, run_background=False)
    with TestClient(app, headers={"Authorization": f"Bearer {SUP_TOKEN}"}) as c:
        c.manager = app.state.manager
        yield c


def body(**kw):
    base = {"version": "1.0.0", "token": TOKEN, "slot": 1, "network": "none"}
    base.update(kw)
    return base


def test_auth_required(client):
    for headers in ({"Authorization": ""}, {"Authorization": "Bearer wrong"}, {"Authorization": SUP_TOKEN}):
        assert client.get("/healthz", headers=headers).status_code == 401
    health = client.get("/healthz")
    assert health.status_code == 200 and health.json()["attestation"] == "required"


def test_docs_are_disabled(client):
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("bad", [
    {"token": "x" * 64}, {"slot": 254}, {"slot": -1}, {"network": "host"}, {"version": "../1"},
    {"time_zone": "$(reboot)"}, {"hub_brand": {"x": "y" * 5000}},
    {"image": "alpine"}, {"volumes": ["/:/host"]}, {"privileged": True},
])
def test_put_validation(client, bad):
    assert client.put("/apps/hello", json=body(**bad)).status_code == 422


@pytest.mark.parametrize("app_id", ["Hello", "a.b", "-x"])
def test_bad_app_id(client, app_id):
    assert client.put(f"/apps/{app_id}", json=body()).status_code == 422


def test_full_lifecycle(client, driver):
    r = client.put("/apps/hello", json=body(hub_brand={"name": "BDL"}))
    assert r.status_code == 202
    assert r.json()["upstream"] == "http://10.213.1.2:8000"
    assert client.get("/apps").json()["apps"][0]["app_id"] == "hello"
    # TestClient runs background tasks on its own loop; wait for the install.
    for _ in range(100):
        if client.get("/apps/hello").json()["status"] == "installed":
            break
    assert client.get("/apps/hello").json()["status"] == "installed"

    r = client.post("/apps/hello/start")
    assert r.status_code == 200 and r.json()["healthy"] is True
    assert client.get("/apps/hello").json()["running"] is True
    assert client.get("/apps/hello/health").json()["healthy"] is True
    assert client.post("/apps/hello/activity").json()["running"] is True
    assert client.get("/apps/hello/logs?lines=2").text == "hello log line\n" * 2
    assert client.get("/apps/hello/logs?lines=5000").status_code == 422
    assert client.get("/apps/hello/stats").json()["working_set_bytes"] == 40 << 20
    assert client.post("/apps/hello/restart").json()["healthy"] is True
    assert client.post("/apps/hello/stop").json()["desired"] == "stopped"
    assert client.put("/apps/other", json=body()).status_code == 409
    assert client.delete("/apps/hello?purge_data=true").json() == {"deleted": "hello", "purged_data": True}
    assert client.get("/apps/hello").status_code == 404
    assert client.get("/apps/hello/logs").status_code == 404


def test_start_not_installed_is_conflict(client, catalog):
    from bdh_supervisor.catalog import CatalogError

    catalog.error = CatalogError("missing")
    client.put("/apps/hello", json=body())
    for _ in range(100):
        if client.get("/apps/hello").json()["status"] == "failed":
            break
    assert client.post("/apps/hello/start").status_code == 409


def test_driver_errors_are_502(client, driver):
    client.put("/apps/hello", json=body())
    for _ in range(100):
        if client.get("/apps/hello").json()["status"] == "installed":
            break
    driver.start_error = "docker said no"
    r = client.post("/apps/hello/start")
    assert r.status_code == 502 and "docker said no" in r.json()["detail"]


def wait_status(client, status):
    for _ in range(100):
        if client.get("/apps/hello").json()["status"] == status:
            break
    assert client.get("/apps/hello").json()["status"] == status


def test_rollback_and_catalog_refresh(client, driver, catalog):
    client.put("/apps/hello", json=body())
    wait_status(client, "installed")
    assert client.post("/apps/hello/rollback").status_code == 409
    client.put("/apps/hello", json=body(version="1.1.0"))
    wait_status(client, "installed")
    r = client.post("/apps/hello/rollback")
    assert r.status_code == 200 and r.json()["installed_version"] == "1.0.0"
    assert client.post("/catalog/refresh").json() == {"upgraded": []}
    assert client.post("/runtime/refresh").status_code == 404
