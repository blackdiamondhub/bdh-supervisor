# bdh-supervisor

Runs Black Diamond Hub apps in their own hardened containers. The repo builds two images:

| Image | Tags | What it is |
|---|---|---|
| `ghcr.io/blackdiamondlodge/bdh-app-runtime` | `hub-api-2`, `sha-<commit>` | The image every app container runs: Python 3.13, Django, uvicorn (one worker), whitenoise, requests, websocket-client and the pinned `bdh-sdk`. It also contains the install worker (`python -m bdh_runtime.install`). |
| `ghcr.io/blackdiamondlodge/bdh-supervisor` | `latest`, `sha-<commit>` | The supervisor: a small FastAPI service and the **only** process with the Docker socket. The same image also provides the gateway container and the one-shot firewall helper. |

The hub never talks to Docker. It tells the supervisor *which app and which version*, plus what the admin approved. Images, mounts, users, capabilities, limits and networking all come from platform templates in [`templates.py`](supervisor/bdh_supervisor/templates.py) and are never taken from the request.

## How an app runs

```
 browser ─► hub (djangoapp, host network) ──HTTP──► http://10.213.<slot>.2:8000   bdh-app-<id>
              │      ▲  unix socket                                              (runtime image)
              │      └──────────── bdh-supervisor ── docker.sock                     │
              │                                                                      │ BDH_BROKER_URL
              └── broker 172.17.0.1:8099/_broker ◄── relay :8099 ── bdh-gateway ◄────┘
                                                      proxy :3128 (outbound apps only) ──► internet :80/:443
```

* **Install** runs in a throwaway worker container built from the runtime image. The worker:
  1. downloads the wheel from the catalog URL (https only) and checks its `sha256` against the catalog;
  2. runs `pip install --no-deps --no-index --only-binary=:all: --require-hashes` into `/app/<version>` on the app's code volume;
  3. imports the `bdh.plugins` entry point and checks that its `PluginSpec` id matches;
  4. checks that the app's requirements are already provided by the runtime image;
  5. flips `/app/current` to the new version.

  The worker has no capabilities, a read-only rootfs, and is removed afterwards.
* **Run**: the app container
  * runs the runtime image as uid `10001` with a read-only rootfs and `cap_drop: ALL`;
  * sets `no-new-privileges`, `pids_limit` 128, 1 CPU, and `mem_limit` = `memory_mb` from the manifest (default 96 MB, clamped to 48–256 MB, no swap);
  * uses `init`, a private IPC namespace and no host mounts;
  * mounts the code volume `bdh-app-<id>-code` read-only at `/app`, the data volume `bdh-app-<id>-data` at `/data` (SQLite `app.sqlite3`, media), and a `noexec` tmpfs at `/tmp`.

  The entrypoint is `python -m bdh_sdk.runtime`, which runs the migrations and then starts uvicorn.
* **Networks.** The admin approves a network. The effective network is the less privileged of what the admin approved and what the manifest asks for.

  | Network | What the app gets |
  |---|---|
  | `none` (default) | Its own internal bridge `10.213.<slot>.0/24`, with no route out. It can reach only the gateway's broker relay (`.253:8099`). |
  | `outbound` | The same, plus `HTTP(S)_PROXY=http://10.213.<slot>.254:3128`. The gateway's CONNECT/HTTP proxy allows only ports 80/443 (`BDH_EGRESS_PORTS`) to public addresses. Private, loopback, link-local and metadata addresses are refused, after DNS resolution. |
  | `lan` | Host networking, bound to `127.0.0.1:9200+slot`. This needs **both** `network: lan` and `allow_lan: true` in the request; without `allow_lan` it drops to `outbound`. |

  In every mode:
  * Host firewall rules (chain `BDH-APPS`, installed by a short-lived `NET_ADMIN` helper) stop app subnets from reaching the host itself, other than the hub's proxy traffic flowing back.
  * The gateway is the only container attached to the egress bridge `bdh-egress` (`10.213.255.0/24`).
  * Apps cannot reach each other, the LAN, the Docker host or the cloud metadata address.
* **Companions** such as `go2rtc` are started from a platform template on the app's network at `.3`, with the same hardening and a 64 MB limit. The app receives the companion's address as `BDH_GO2RTC_URL`.
* **Lifecycle**
  * `on_demand` (default): the app is stopped after 10 minutes without activity (`BDH_IDLE_TIMEOUT`).
  * `always`: the app is kept running.
  * Crashed apps are restarted with exponential backoff from 5 s to 300 s. The failure count resets after 10 minutes of stable running.
  * Every 6 hours the supervisor pulls the runtime image (also on demand via `POST /runtime/refresh`) and restarts running apps whose image changed.

### Memory budget

The CI `integration` job installs and runs the `examples/hello` app, sends it 25 requests, and fails if the container's working set (cgroup `memory.current` minus inactive file pages) is over **70 MB**. The last measurement was **~48 MB** (anon 45.6 MB, 4 pids). The figure is published in the job summary and in the `rss` artifact.

## Supervisor API

The API is served over HTTP on the unix socket `BDH_SUPERVISOR_SOCKET` (default `/run/bdh-supervisor/supervisor.sock`). Every request needs `Authorization: Bearer $BDH_SUPERVISOR_TOKEN`. OpenAPI and docs are disabled.

| Method and path | Body / query | Returns |
|---|---|---|
| `GET /healthz` | | `{ok, prepared, error, firewall, hub_api, runtime_image, apps}` |
| `GET /apps` | | `{apps: [view…]}` |
| `PUT /apps/{id}` | `{version, token, slot, network, allow_lan, time_zone, hub_brand}` | `202` + view. Records the desired state and installs in the background; it upgrades or reinstalls when the version changes. `409` if another app already has the slot. |
| `GET /apps/{id}` | | view + `running`, `container_status` |
| `DELETE /apps/{id}` | `?purge_data=true` also deletes the data volume | `{deleted, purged_data}` |
| `POST /apps/{id}/start` | `?wait=true` (default) waits for `/_bdh/health` | `{running, healthy, upstream, …}` |
| `POST /apps/{id}/stop` | | |
| `POST /apps/{id}/restart` | `?wait=true` | |
| `POST /apps/{id}/activity` | | `{running, desired}`. Resets the idle timer and does **not** start the app. |
| `GET /apps/{id}/health` | | `{running, healthy, status, exit_code, oom_killed, failures}` |
| `GET /apps/{id}/logs` | `?lines=200` (max 1000) | text/plain, the last N lines of the app and its companions |
| `GET /apps/{id}/stats` | | `{memory_bytes, memory_limit_bytes, working_set_bytes, anon_bytes, pids, …}` |
| `POST /runtime/refresh` | | `{runtime_image, restarted: [...]}` |

Request fields:
* `token` is the app's broker token, 64 hex characters. The hub computes it as `app_token(SECRET_KEY, id, gen)`.
* `slot` (0–253) is the app's hub port minus 9100 (`AppInstall.port - APP_PORT_BASE`). It picks the subnet `10.213.<slot>.0/24`.
* `network` is one of `none`, `outbound` or `lan`.
* `time_zone` is an IANA name.
* `hub_brand` is a JSON object of at most 4 KB.

View fields: `app_id`, `version`, `installed_version`, `slot`, `approved_network`, `allow_lan`, `status` (`installing`, `installed` or `failed`), `error`, `desired` (`running`, `stopped` or `idle`), `failures`, `last_exit`, `last_activity`, `manifest`. `manifest` is the validated PluginSpec, including `network`, `lifecycle`, `memory_mb` and `companions`. The effective values are also given as `network`, `lifecycle` and `memory_mb`, together with **`upstream`**, the URL the hub proxies to (`http://10.213.<slot>.2:8000`, or `http://127.0.0.1:<9200+slot>` for `lan`).

Apps are identified by catalog id (`[a-z0-9][a-z0-9_-]{0,63}`). The wheel URL and sha256 always come from the catalog (`BDH_CATALOG_URL`, schema 1). The catalog entry's `hub_api` must equal the runtime's (`2`).

## Environment contract

### Supervisor (`bdh-supervisor` container)

| Variable | Default | |
|---|---|---|
| `BDH_SUPERVISOR_TOKEN` | — (required, ≥ 32 chars) | Shared secret with the hub. |
| `BDH_CATALOG_URL` | — (required) | e.g. `https://raw.githubusercontent.com/blackdiamondlodge/bdh-app-catalog/main/catalog.json` |
| `BDH_SUPERVISOR_SOCKET` | `/run/bdh-supervisor/supervisor.sock` | |
| `BDH_STATE_DIR` | `/state` | Holds `apps.json`, the desired state (mode 0600). Mount a volume here. |
| `BDH_RUNTIME_IMAGE` | `ghcr.io/blackdiamondlodge/bdh-app-runtime:hub-api-2` | |
| `BDH_SUPERVISOR_IMAGE` | `ghcr.io/blackdiamondlodge/bdh-supervisor:latest` | Used for the gateway and firewall helper. |
| `BDH_GO2RTC_IMAGE` | `ghcr.io/blackdiamondlodge/go2rtc-nack:latest` | |
| `BDH_HUB_API` | `2` | |
| `BDH_BROKER_HOST` / `BDH_BROKER_PORT` / `BDH_BROKER_PATH` | `172.17.0.1` / `8099` / `/_broker` | Where the hub's broker listens (`BROKER_BIND`/`BROKER_PORT`). |
| `BDH_SUBNET_PREFIX` | `10.213` | A private `/16` reserved for apps. |
| `BDH_LAN_PORT_BASE` | `9200` | |
| `BDH_FIREWALL` | `required` | `off` is for development only. Apps are not started if the firewall could not be applied. |
| `BDH_EGRESS_PORTS` | `80,443` | |
| `BDH_IDLE_TIMEOUT` | `600` | Seconds. |
| `BDH_START_TIMEOUT` | `30` | Seconds to wait for health. |
| `BDH_RUNTIME_PULL_INTERVAL` | `21600` | Seconds. |
| `BDH_LOG_LEVEL` | `INFO` | |

### App containers (set by the supervisor; read by `bdh_sdk.runtime`)

| Variable | Value |
|---|---|
| `BDH_APP_ID` | Catalog id. |
| `BDH_APP_VERSION` | Installed version. |
| `BDH_APP_TOKEN` | Broker token. It is also the HMAC key for the identity headers the hub signs (`bdh_sdk.identity`). |
| `BDH_BROKER_URL` | `http://10.213.<slot>.253:8099/_broker` (`.254` for outbound; the host broker URL for `lan`) |
| `BDH_HOST` / `BDH_PORT` | `0.0.0.0` / `8000` (`127.0.0.1` / `9200+slot` for `lan`) |
| `BDH_DATA_DIR` | `/data` |
| `BDH_TIME_ZONE`, `BDH_HUB_BRAND` | From the hub. |
| `HTTP(S)_PROXY`, `NO_PROXY` | Outbound apps only. |
| `BDH_GO2RTC_URL` | When a go2rtc companion is declared. |
| baked in the image | `PYTHONPATH=/app/current`, `HOME=/tmp`, `MALLOC_ARENA_MAX=2`, `BDH_HUB_API=2` |

The app's code lives in the code volume at `/app/<version>`, with `/app/current` as a symlink, mounted read-only. Its data lives in the data volume at `/data`. Upgrades install the new version next to the old one, flip the symlink, then restart the app. Purging the data volume is a separate, explicit `?purge_data=true`.

## Hub compose

```yaml
services:
  bdh-supervisor:
    image: ghcr.io/blackdiamondlodge/bdh-supervisor:latest
    container_name: bdh-supervisor
    restart: unless-stopped
    network_mode: host            # needed for the BDH-APPS firewall helper and the 10.213/16 routes
    environment:
      BDH_SUPERVISOR_TOKEN: ${BDH_SUPERVISOR_TOKEN}
      BDH_CATALOG_URL: https://raw.githubusercontent.com/blackdiamondlodge/bdh-app-catalog/main/catalog.json
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - bdh-supervisor-socket:/run/bdh-supervisor
      - bdh-supervisor-state:/state

  djangoapp:
    # …existing…
    environment:
      BDH_SUPERVISOR_SOCKET: /run/bdh-supervisor/supervisor.sock
      BDH_SUPERVISOR_TOKEN: ${BDH_SUPERVISOR_TOKEN}
    volumes:
      - bdh-supervisor-socket:/run/bdh-supervisor

  watchtower:
    # add the supervisor to the watched containers. Apps pick up new runtime
    # images through the supervisor's own pull loop, not watchtower.
    command: ["BlackDiamondHub", "go2rtc", "bdh-supervisor"]

volumes:
  bdh-supervisor-socket:
  bdh-supervisor-state:
```

The supervisor creates and labels (`bdh.managed=1`) the `bdh-gateway` container, the `bdh-egress` and `bdh-app-<id>` networks, and the app and companion containers and volumes. These must not be added to compose or to watchtower.

## Security notes and limits

* The supervisor runs as root because it holds the Docker socket. That makes it the trust boundary, so it only accepts ids and versions; everything privileged is decided by the templates.
* The broker relay on the gateway is a plain TCP relay to the hub's dedicated broker listener. The broker authenticates every call with the app token, so the relay does not filter paths.
* The app's own Python dependencies must already be in the runtime image. The install worker installs only the app wheel (`--no-deps`) and fails the install if the requirements are not met. To add a library, release a new runtime image.
* The supervisor checks the wheel's sha256 against the catalog. It does **not** verify the catalog's build attestations; that remains the catalog CI's job.

## Development

```sh
pip install --require-hashes -r requirements-dev.txt
pip install --no-deps "$(grep -o 'https://[^ ]*bdh_sdk[^ ]*\.whl' runtime/requirements.in)"
ruff check . && pytest --deselect tests/test_integration_docker.py   # unit tests, fake driver
sh scripts/build.sh && sh scripts/integration.sh                     # real Docker: images, hello app, sandbox probes, RSS gate
sh scripts/lock.sh                                                    # re-pin the *.txt lock files from *.in
```

`RuntimeDriver` ([`driver.py`](supervisor/bdh_supervisor/driver.py)) is the interface the manager uses. `DockerDriver` is the only implementation; the tests use a `FakeDriver`.