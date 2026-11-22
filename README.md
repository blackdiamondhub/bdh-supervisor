# bdh-supervisor

Runs Black Diamond Hub apps, each in its own hardened container built from its own image. The repo builds two images:

| Image | Tags | What it is |
|---|---|---|
| `ghcr.io/blackdiamondlodge/bdh-app-runtime` | `hub-api-2`, `sha-<commit>` | The **base** every app image is built `FROM`. It contains only the common runtime: Python 3.13, Django, uvicorn (one worker), whitenoise, requests, websocket-client and the pinned `bdh-sdk`. It also ships `/opt/bdh/constraints.txt` (its exact pins), `/opt/bdh/check.py` and `/opt/bdh/hub_api`, and is labelled `org.blackdiamondlodge.bdh.runtime=1` and `org.blackdiamondlodge.bdh.hub_api=2`. App libraries are **not** in the base. |
| `ghcr.io/blackdiamondlodge/bdh-supervisor` | `latest`, `sha-<commit>` | The supervisor: a small FastAPI service and the **only** process with the Docker socket. The same image also provides the gateway container and the one-shot firewall helper. |

`publish.yml` pushes both images on every push to `main`, attests them (`actions/attest-build-provenance` with `push-to-registry`), and writes the base digest to the run summary. Both GHCR packages are public, so hubs pull them without credentials.

The hub never talks to Docker. It tells the supervisor *which app and which version*, plus what the admin approved. The image comes from the catalog. Mounts, users, capabilities, limits and networking come from platform templates in [`templates.py`](supervisor/bdh_supervisor/templates.py) and are never taken from the request or the image.

## App images

Apps don't build images. An app repo publishes a GitHub release with a wheel, a hash-locked lockfile and its manifest (the SDK's `app-release.yml`). The catalog repo's builder workflow (`bdh-app-catalog/.github/workflows/build.yml`) then, for each app:

1. builds `ghcr.io/blackdiamondlodge/bdh-app-<id>` from the SDK template, `FROM bdh-app-runtime:hub-api-2@sha256:…`;
2. installs the wheel and its locked dependencies with `pip install --require-hashes -c /opt/bdh/constraints.txt`, runs `pip check`, and fails if any base-owned distribution changed;
3. runs `compileall` on the app layer and resets `USER` to the non-root user;
4. smoke-tests the image (imports, migrations, `/_bdh/health`);
5. pushes the image, attests it, and opens one catalog PR with the new entries.

A base bump rebuilds every app in one matrix run and one catalog PR. Only apps marked `custom_base: true` in the catalog may add raw Dockerfile steps.

A catalog version entry for an image looks like this:

```json
{
  "version": "2.1.0",
  "hub_api": 2,
  "kind": "image",
  "image": "ghcr.io/blackdiamondlodge/bdh-app-cameras@sha256:<64 hex>",
  "base": "ghcr.io/blackdiamondlodge/bdh-app-runtime@sha256:<64 hex>",
  "build": "<builder run id>",
  "permissions": ["…"], "network": "lan", "lifecycle": "always", "memory_mb": 96, "companions": ["go2rtc", "aec"]
}
```

The supervisor only accepts `kind: "image"` entries whose `hub_api` equals its own. `image` must be `<BDH_IMAGE_PREFIX>bdh-app-<same id>@sha256:…` and `base` must be `<BDH_IMAGE_PREFIX>bdh-app-runtime@sha256:…`. Tags are refused.

### What the supervisor checks before running an image

1. **Attestation (trust).** Before pulling, the supervisor gets the image digest's attestations from the GitHub API (`/repos/blackdiamondlodge/bdh-app-catalog/attestations/sha256:…`). It verifies each Sigstore bundle (certificate chain, transparency log and signature) with the signer identity pinned to `https://github.com/blackdiamondlodge/bdh-app-catalog/.github/workflows/build.yml@refs/heads/main`, then checks that the signed statement is SLSA provenance for exactly that digest. If no bundle vouches for the image, it is refused. This is the equivalent of `cosign verify-attestation --type slsaprovenance --certificate-identity … --certificate-oidc-issuer https://token.actions.githubusercontent.com`. It is on by default (`BDH_ATTESTATION=required`); `off` exists only for the integration tests, which build images locally.
2. **Compatibility (not security).** After pulling the image and its base by digest:
   * the base must carry the runtime and `hub_api` labels for this supervisor;
   * the app image's rootfs `diff_ids` must start with the base's `diff_ids`, so it really was built `FROM` that base;
   * the image must not declare `VOLUME`s or change the base's `ENV`.

   The manifest (`PluginSpec`) the builder wrote into the image is copied out of a created, never-started container. Its id and version must match the catalog entry.
3. **Run template.** The supervisor always sets the user (`10001`), entrypoint (`python -m bdh_sdk.runtime`), command, working directory, environment and mounts itself. It disables the image's `HEALTHCHECK`. The only mount is the app's `/data` volume, so no anonymous volumes are created.

## How an app runs

```
 browser ─► hub (djangoapp, host network) ──HTTP──► http://10.213.<slot>.2:8000   bdh-app-<id>
              │      ▲  unix socket                                              (its own image)
              │      └──────────── bdh-supervisor ── docker.sock                     │
              │                                                                      │ BDH_BROKER_URL
              └── broker 172.17.0.1:8099/_broker ◄── relay :8099 ── bdh-gateway ◄────┘
                                                      proxy :3128 (outbound apps only) ──► internet :80/:443
```

* **Run**: the app container
  * runs the app image as uid `10001` with a read-only rootfs and `cap_drop: ALL`;
  * sets `no-new-privileges`, `pids_limit` 128, 1 CPU, and `mem_limit` = `memory_mb` from the manifest (default 96 MB, clamped to 48–256 MB, no swap);
  * uses `init`, a private IPC namespace and no host mounts;
  * mounts the data volume `bdh-app-<id>-data` at `/data` (SQLite `app.sqlite3`, media) and a `noexec` tmpfs at `/tmp`.

  The entrypoint runs the migrations and then starts uvicorn.
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
  For a `lan` app, go2rtc runs on the host network: its API listens on every interface at `11900+slot` (browsers load `video-stream.js` and its websocket from there), and WebRTC listens on `8555` UDP+TCP. WebRTC over UDP can't go through Docker's port proxy, so this is a host-wide port: only one `lan` app per host can have a go2rtc companion, and the hub's own go2rtc must be removed first. Set `BDH_GO2RTC_CANDIDATES` to the host's LAN `ip:8555` so browsers get a reachable candidate.
  `aec` ([bdh-aec](https://github.com/blackdiamondlodge/bdh-aec), doorbell echo cancellation) is for `lan` apps that also declare `go2rtc`. It runs on the host network as the same unprivileged user with a 192 MB limit. Its HTTP port, `12200+slot`, serves `/aec.ts` to go2rtc and the `/api/talk` websocket to browsers. It relays talk audio through a loopback-only port at `12500+slot` to go2rtc. The app receives its address as `BDH_AEC_URL`.
* **Lifecycle**
  * `on_demand` (default): the app is stopped after 10 minutes without activity (`BDH_IDLE_TIMEOUT`).
  * `always`: the app is kept running.
  * Crashed apps are restarted with exponential backoff from 5 s to 300 s. The failure count resets after 10 minutes of stable running.

### Installs, upgrades and rollback

* An install or upgrade happens when the hub PUTs a new version, or when the catalog's image for the installed version changes. The supervisor polls the catalog every 6 hours, and on `POST /catalog/refresh`. A new `image` digest for the same `version` (for example after a base rebuild) counts as an upgrade.
* Upgrades are staged and saved after every step, in this order:
  1. `pending`
  2. `verified` (attestation)
  3. `pulled` (image and base pulled and checked)
  4. `snapshot` (the app is stopped and `/data` is copied to `bdh-app-<id>-data-snap`)
  5. the switch to the new image

  Every step is idempotent. If the supervisor restarts mid-upgrade (for example when Watchtower updates it), it resumes from the last saved stage. A failure before the switch leaves the old version untouched and running.
* The previous image (N-1) is kept, with its manifest, as `previous`. `POST /apps/{id}/rollback` stops the app, restores `/data` from the snapshot, switches back to N-1, and **pins** the app. While an app is pinned, catalog polls don't upgrade it again. The hub's next `PUT /apps/{id}` clears the pin.
* **Image GC** runs after every upgrade and on uninstall. It only looks at images in the app's own repository (`bdh-app-<id>`), and it keeps:
  * the current image;
  * the N-1 image;
  * anything that is also tagged in another repository, such as a shared base.

  It removes images by explicit id, never with `prune -a`, and never removes images that are in use.
* **Offline.** Running apps, restarts, idle stops and on-demand starts need no network: images are local. Installs, upgrades and the catalog poll need the catalog, the GitHub attestation API and GHCR. If they are unreachable, the installed version keeps running and the poll is retried later.

### Memory budget

The CI `integration` job builds the base and the `examples/hello` app image with the SDK builder, installs it through the supervisor, and sends it 25 requests. It fails if the container's working set (cgroup `memory.current` minus inactive file pages) is over **70 MB**. Last measurement:

| | Working set | RSS | PSS |
|---|---|---|---|
| hello | 44.8 MB | 58.9 MB | 48.9 MB |

The figures are published in the job summary and in the `rss` artifact. Image sizes: base 279 MB (61.8 MB compressed); hello adds only the app wheel.

## Supervisor API

The API is served over HTTP on the unix socket `BDH_SUPERVISOR_SOCKET` (default `/run/bdh-supervisor/supervisor.sock`). Every request needs `Authorization: Bearer $BDH_SUPERVISOR_TOKEN`. OpenAPI and docs are disabled.

| Method and path | Body / query | Returns |
|---|---|---|
| `GET /healthz` | | `{ok, prepared, error, firewall, hub_api, attestation, last_catalog_poll, apps}` |
| `GET /apps` | | `{apps: [view…]}` |
| `PUT /apps/{id}` | `{version, token, slot, network, allow_lan, time_zone, hub_brand}` | `202` + view. Records the desired state, clears a rollback pin, and installs or upgrades in the background. `409` if another app already has the slot. |
| `GET /apps/{id}` | | view + `running`, `container_status` |
| `DELETE /apps/{id}` | `?purge_data=true` also deletes the data volume and its snapshot | `{deleted, purged_data}` |
| `POST /apps/{id}/start` | `?wait=true` (default) waits for `/_bdh/health` | `{running, healthy, upstream, …}` |
| `POST /apps/{id}/stop` | | |
| `POST /apps/{id}/restart` | `?wait=true` | |
| `POST /apps/{id}/activity` | | `{running, desired}`. Resets the idle timer and does **not** start the app. |
| `POST /apps/{id}/rollback` | | view. Restores N-1 and the pre-upgrade `/data` snapshot, and pins the app. `409` if there is nothing to roll back to or an upgrade is in progress. |
| `GET /apps/{id}/health` | | `{running, healthy, status, exit_code, oom_killed, failures}` |
| `GET /apps/{id}/logs` | `?lines=200` (max 1000) | text/plain, the last N lines of the app and its companions |
| `GET /apps/{id}/stats` | | `{memory_bytes, memory_limit_bytes, working_set_bytes, anon_bytes, pids, …}` |
| `POST /catalog/refresh` | | Re-reads the catalog now and upgrades apps whose image changed. Returns `{upgraded: [ids]}`. |

Request fields:
* `token` is the app's broker token, 64 hex characters. The hub computes it as `app_token(SECRET_KEY, id, gen)`.
* `slot` (0–253) is the app's hub port minus 9100 (`AppInstall.port - APP_PORT_BASE`). It picks the subnet `10.213.<slot>.0/24`.
* `network` is one of `none`, `outbound` or `lan`.
* `time_zone` is an IANA name.
* `hub_brand` is a JSON object of at most 4 KB.

View fields:
* Identity and state: `app_id`, `version`, `installed_version`, `slot`, `approved_network`, `allow_lan`, `status` (`installing`, `installed` or `failed`), `error`, `upgrade_stage`.
* Image: `image`, `build`, `pinned`.
* Runtime: `desired` (`running`, `stopped` or `idle`), `failures`, `last_exit`, `last_activity`.
* `manifest`: the validated PluginSpec, including `network`, `lifecycle`, `memory_mb` and `companions`.
* Effective values: `network`, `lifecycle` and `memory_mb`, together with **`upstream`**, the URL the hub proxies to (`http://10.213.<slot>.2:8000`, or `http://127.0.0.1:<9200+slot>` for `lan`).

Apps are identified by catalog id (`[a-z0-9][a-z0-9_-]{0,63}`).

## Environment contract

### Supervisor (`bdh-supervisor` container)

| Variable | Default | |
|---|---|---|
| `BDH_SUPERVISOR_TOKEN` | — (required, ≥ 32 chars) | Shared secret with the hub. |
| `BDH_CATALOG_URL` | — (required) | e.g. `https://raw.githubusercontent.com/blackdiamondlodge/bdh-app-catalog/main/catalog.json` |
| `BDH_SUPERVISOR_SOCKET` | `/run/bdh-supervisor/supervisor.sock` | |
| `BDH_STATE_DIR` | `/state` | Holds `apps.json`, the desired state and upgrade stages (mode 0600). Mount a volume here. |
| `BDH_IMAGE_PREFIX` | `ghcr.io/blackdiamondlodge/` | App and base images must live under this prefix. |
| `BDH_ATTESTATION` | `required` | `off` is for tests only. |
| `BDH_ATTESTATION_REPO` | `blackdiamondlodge/bdh-app-catalog` | Repository whose attestations API is queried. |
| `BDH_ATTESTATION_IDENTITY` | `https://github.com/blackdiamondlodge/bdh-app-catalog/.github/workflows/build.yml@refs/heads/main` | The only signer accepted. |
| `BDH_GITHUB_TOKEN` | — | Optional. Any read-only token; it only raises the GitHub API rate limit (60/hour without one). |
| `BDH_SUPERVISOR_IMAGE` | `ghcr.io/blackdiamondlodge/bdh-supervisor:latest` | Used for the gateway and firewall helper. |
| `BDH_GO2RTC_IMAGE` | `ghcr.io/blackdiamondlodge/go2rtc-nack:latest` | |
| `BDH_AEC_IMAGE` | `ghcr.io/blackdiamondlodge/bdh-aec:latest` | |
| `BDH_HUB_API` | `2` | |
| `BDH_BROKER_HOST` / `BDH_BROKER_PORT` / `BDH_BROKER_PATH` | `172.17.0.1` / `8099` / `/_broker` | Where the hub's broker listens (`BROKER_BIND`/`BROKER_PORT`). |
| `BDH_SUBNET_PREFIX` | `10.213` | A private `/16` reserved for apps. |
| `BDH_LAN_PORT_BASE` | `9200` | |
| `BDH_LAN_GO2RTC_PORT_BASE` | `11900` | The API port of a `lan` app's go2rtc is this plus the slot. |
| `BDH_GO2RTC_WEBRTC_PORT` | `8555` | WebRTC port of a `lan` app's go2rtc (host-wide). |
| `BDH_GO2RTC_CANDIDATES` | — | Comma-separated WebRTC candidates for a `lan` go2rtc, e.g. `192.168.10.216:8555`. |
| `BDH_LAN_AEC_PORT_BASE` / `BDH_LAN_AEC_TALK_PORT_BASE` | `12200` / `12500` | The HTTP port and the loopback talk port of a `lan` app's aec are these plus the slot. |
| `BDH_FIREWALL` | `required` | `off` is for development only. Apps are not started if the firewall could not be applied. |
| `BDH_EGRESS_PORTS` | `80,443` | |
| `BDH_IDLE_TIMEOUT` | `600` | Seconds. |
| `BDH_START_TIMEOUT` | `30` | Seconds to wait for health. |
| `BDH_CATALOG_POLL_INTERVAL` | `21600` | Seconds. |
| `BDH_LOG_LEVEL` | `INFO` | |

### App containers (set by the supervisor; read by `bdh_sdk.runtime`)

The supervisor passes only these variables. The image's own `ENV` must equal the base's, and the base's environment is fixed (`HOME=/tmp`, `MALLOC_ARENA_MAX=2`, `BDH_HUB_API=2`, `PYTHONUNBUFFERED=1`, …).

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
| `BDH_AEC_URL` | When an aec companion is declared (`lan` only). |

## Hub compose

```yaml
services:
  bdh-supervisor:
    image: ghcr.io/blackdiamondlodge/bdh-supervisor:latest
    container_name: bdh-supervisor
    restart: unless-stopped
    network_mode: host            # needed for the BDH-APPS firewall helper and the 10.213/16 routes
    labels:
      com.centurylinklabs.watchtower.enable: "true"
    environment:
      BDH_SUPERVISOR_TOKEN: ${BDH_SUPERVISOR_TOKEN}
      BDH_CATALOG_URL: https://raw.githubusercontent.com/blackdiamondlodge/bdh-app-catalog/main/catalog.json
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - bdh-supervisor-socket:/run/bdh-supervisor
      - bdh-supervisor-state:/state

  djangoapp:
    # …existing…
    labels:
      com.centurylinklabs.watchtower.enable: "true"
    environment:
      BDH_SUPERVISOR_SOCKET: /run/bdh-supervisor/supervisor.sock
      BDH_SUPERVISOR_TOKEN: ${BDH_SUPERVISOR_TOKEN}
    volumes:
      - bdh-supervisor-socket:/run/bdh-supervisor

volumes:
  bdh-supervisor-socket:
  bdh-supervisor-state:
```

### Watchtower

Watchtower must update **only** the hub, the supervisor and the hub's go2rtc. It must never touch app containers, which are pinned by digest and upgraded only through the supervisor. Run it in label mode:

```diff
   watchtower:
     image: containrrr/watchtower
     environment:
+      WATCHTOWER_LABEL_ENABLE: "true"
-    command: ["BlackDiamondHub", "go2rtc"]
```

Then add `com.centurylinklabs.watchtower.enable: "true"` to the `djangoapp`, `bdh-supervisor` and `go2rtc` services, as shown above. The supervisor labels everything it creates with `bdh.managed=1` and never sets the Watchtower label. This covers the `bdh-gateway` container, the `bdh-egress` and `bdh-app-<id>` networks, and the app and companion containers and volumes, so Watchtower ignores them. Don't add them to compose.

Watchtower restarting the supervisor in the middle of an upgrade is safe, because upgrades resume (see above).

## Security notes and limits

* The supervisor runs as root because it holds the Docker socket. That makes it the trust boundary, so it only accepts ids and versions; everything privileged is decided by the templates.
* The trust root for app code is the builder workflow's Sigstore identity (see "What the supervisor checks"). The catalog is only an index; a tampered catalog can't point at an image the builder didn't attest. The `diff_ids` base check is only a compatibility check.
* The catalog repo's CI rejects PRs that:
  * touch more than one app (except builder base bumps);
  * use an image repository other than `bdh-app-<id>`;
  * change `permissions`, `network` or `custom_base` without a human-review label.
* The broker relay on the gateway is a plain TCP relay to the hub's dedicated broker listener. The broker authenticates every call with the app token, so the relay does not filter paths.
* Attestation lookups use the unauthenticated GitHub API (60 requests/hour per IP) unless `BDH_GITHUB_TOKEN` is set. One install or upgrade costs one request plus a bundle download.

## Development

```sh
pip install --require-hashes -r requirements-dev.txt
pip install --no-deps "$(grep -o 'https://[^ ]*bdh_sdk[^ ]*\.whl' runtime/requirements.in)"
ruff check . && pytest --deselect tests/test_integration_docker.py   # unit tests, fake driver
BDH_SDK=../bdh-sdk sh scripts/build.sh && sh scripts/integration.sh   # real Docker: local registry, base + hello via the SDK builder, upgrade/rollback/GC, sandbox probes, RSS gate
sh scripts/lock.sh                                                    # re-pin the *.txt lock files from *.in
```

`RuntimeDriver` ([`driver.py`](supervisor/bdh_supervisor/driver.py)) is the interface the manager uses. `DockerDriver` is the only implementation; the tests use a `FakeDriver`.
