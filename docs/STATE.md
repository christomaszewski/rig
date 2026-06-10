# rig — project state & handoff (resume here)

> Snapshot for picking the project up cold in a new session. Read this first, then `CHEATSHEET.md` /
> `RUNBOOK.md` (deploy steps), then `DESIGN.md`/`ROADMAP.md` for rationale. As of: rig **v0.1.18**, branch
> **`main`** (the `config-schema-symmetric` work is merged; feature branches deleted), 46 tests passing
> (`python3 tests/test_*.py`). Tool at `/Users/ckt/ws/bringup`; run-from-source `./rig <verb>`.

## TL;DR — where things are

**The full 4-stack deployment is UP on the Orin** (2026-06-09): all 9 containers healthy, dashboard serving,
USB camera streaming + recording, zenoh mesh connected. The cross-repo work that was open here is **done and
landed** (camera-service #25–#33, dashboard image/tag/caddy fixes). What's left: physical-world verification
(RTSP source was powered off; webrtc video in a real browser) and the small follow-ups listed below.

**The live test:**
- **Dev box:** this Mac. Local registry at **`192.168.8.149:5000`** (compose-managed container
  `docker-registry-registry-1`; Docker Desktop trusts it via `insecure-registries`). Workspace
  `/Users/ckt/ws/rig-walkthrough/` (siblings: `rig/`, `camera-service/`, `dashboard/`, `test-vehicle/` = the
  deployment).
- **Vehicle (Orin):** ssh host `orin` (10.160.1.21, user `uxv`). `vehicle: orin-test-vehicle`,
  `vehicle_id: 1`, `rmw_zenoh_cpp`, `images.tag: jp7`, `images.registry: 192.168.8.149:5000`,
  `data_dir: /home/uxv/logs`. Artifact `test1` extracted + **running** at `~/ws/test1` (brought up via
  `./run.sh up`, compose-only form).
- **Stacks (4):** infra `zenoh-router` (order 0) + `dashboard` (order 5); sensors `cam_usb` + `cam_rtsp`
  (camera-service). Configs enable **both bridges** per camera (ros2-bridge + webrtc-bridge w/ NVENC H.264,
  signalling ports 8446/8445), recording on (`/data/recordings` → RIG_DATA_DIR), USB at 1080p MJPEG
  (stable `/dev/v4l/by-id/...NexiGo...` path), RTSP at 4K (ZR30 at `rtsp://10.160.1.80:8554/main.264`).
- **Verified up (2026-06-09):** all 4 stacks / 9 containers, compose projects `<name>-vehicle-1`; dashboard
  HTTP 200 on :8080, ws :10000, webrtc signalling :8445/:8446 listening; cam_usb 30fps no drops, recordings
  growing on the host; router + dashboard-zenoh sidecar connected. cam_rtsp healthy in its designed
  reconnect loop — the physical camera was **powered off** during the deploy; it self-recovers when on.

## What rig is (one paragraph)

A vehicle-level orchestrator — "a loop + a manifest" that delegates bring-up to each service's own
`<service>-up` launcher. One-way dependency (a service never imports rig; rig learns it via `rigging.yaml` +
the launcher CLI). rig owns the cross-cutting concerns: name-uniqueness, ordering, fleet env, status,
deployment artifacts. See `DESIGN.md`.

## The fleet env rig injects into every launcher (the contract)

`ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION`, `VEHICLE_ID`, `RIG_IMAGE_REGISTRY`, `RIG_IMAGE_TAG` (e.g. `jp7`),
`RIG_DATA_DIR` (recordings/logs host dir), and per-call `COMPOSE_PROJECT_NAME=<name>-vehicle-<vehicle_id>`.
A launcher's compose opts into each (`${RIG_IMAGE_REGISTRY:+…}`, `:${RIG_IMAGE_TAG:-latest}`,
`${RIG_DATA_DIR}/…`), and a launcher honors `COMPOSE_PROJECT_NAME` by **not** passing `-p`.

## rig capabilities (v0.1.17 — all built/tested)

- Lifecycle `up/down(--purge)/status/logs/config/doctor`; tiered ordering (infra before sensors); tier-aware
  output ("2 sensors + 2 infra").
- `vehicle.yaml`: `vehicle_id` (→ ROS domain + `VEHICLE_ID`), `ros{rmw,distro}`, `images{registry,tag}`,
  `data_dir`, `infra:`, `sensors:`. Config overrides + nameless profiles (deep-merge).
- `doctor`: one-distro check, launcher-present, host-port clash (enabled-aware `plugins[name=x,enabled=true].params.port`
  selector), **non-ROS-safe name warning** (hyphens → invalid ROS namespace), zenoh-router guardrail.
- `rig build [-j N] [--registry] [--tag]`: per-unique-service **build** (`rigging.yaml build:`) + **mirror**
  (`mirror:`, via `docker pull/tag/push` so a plain-HTTP registry works). Concurrent with `-j`.
- `rig vendor` (copy launch surface, files **and dirs**), `rig bake [--registry] --tag` / `rig unbake`:
  tagged artifact = resolved configs + complete vehicle.yaml + vendored surfaces + rig + a **compose-only**
  form (build-stripped, registry-pinned, runs on just Docker). Built images digest-pinned; **mirrored
  images kept as registry tags** (multi-arch digests are fragile). `run.sh` prefers the compose-only form.
- `rig init` + cwd deployment detection (tool and deployment can be separate dirs).
- Templates: `templates/zenoh-router/` (a ready shared-router infra service; honors `COMPOSE_PROJECT_NAME`).
- `rig certify [name…|--repo R --config C] [--emit F|--diff A B]` + `rig doctor --deep` (v0.1.18): the
  launcher contract as executable checks (poison env; project-name/registry/tag/ros-env/determinism/
  identity/discipline/status). `--emit` on two hosts + `--diff` proves `config` output host-independence.
  On its first live run it caught cam-up + dash-up overriding `COMPOSE_PROJECT_NAME` (masked until then by
  the baked scripts' explicit `-p`) — both fixed + re-certified, 0 errors.

## Deploy recipe (current)

```
# Dev box (Mac): trust the registry in Docker Desktop (insecure-registries: ["192.168.8.149:5000"])
rig build -j 3                       # build + push + mirror images
curl -s http://192.168.8.149:5000/v2/_catalog
rig bake --tag testN                 # compose-only, pinned, complete vehicle.yaml
scp var/artifacts/testN.tar.gz orin:/tmp/
# Orin: MERGE insecure-registries (KEEP nvidia runtime) into /etc/docker/daemon.json, with the :5000 PORT;
#       restart docker; tar xzf; ./run.sh up   (uses the compose-only scripts; pulls by digest/tag)
```

## OPEN ITEMS

The big cross-repo batch from the previous handoff is **all landed** (verified live 2026-06-09). The
recurring principle held: **a launcher's `config` output must be host-independent** so a dev-box bake is
correct for the target. For the record — camera-service: `RIG_IMAGE_TAG` as platform (#26), v4l2 device
mapping (#25) + host-independent config (#29), `RIG_DATA_DIR` recordings (#27), numeric-coerce + fail-fast
(#28), webrtc H.264 level/profile + NVENC rank (#30–#32), 1080p example (#33). dashboard: built
`dashboard-web` image (Caddy + bundle baked in), `RIG_IMAGE_TAG`-tagged pulls, `vehicleHost` signalling fix,
reworked `rigging.yaml` (infra, no "BUILD phase" framing). `COMPOSE_PROJECT_NAME`: genuinely honored as of
2026-06-09 — `rig certify` caught cam-up (`tools/sensor_env.py`) and dash-up overriding it (the baked
scripts' explicit `-p` had masked this; an un-baked `rig up` would have made orphan projects); one-line
fallback fixes in both repos, re-certified clean.

Still open:
1. **Live-deploy verification (physical world):** power the RTSP camera (ZR30 at `10.160.1.80`) and watch
   cam_rtsp self-recover; open `http://10.160.1.21:8080` in Chrome and confirm both webrtc streams render
   (NVENC H.264). One startup-time `listConsumers` parse warning in the webrtc signalling log is a known
   benign dialect probe.
2. **boilerplate `<device>-up` (novatel/sbg launchers):** honor `COMPOSE_PROJECT_NAME` (drop `-p`, standalone
   fallback) — same one-liner the other launchers got. Find + prove it with
   `rig certify --repo ../novatel --config <example.yaml>` (the project-name check fails until fixed).
3. **rig follow-ups (`ROADMAP.md`):** `bake --bundle-images` (air-gap), OCI artifact format, ROS
   `/diagnostics` as a 2nd health layer, boot-time systemd unit, `rig adopt/verify`.

## Gotchas learned the hard way (deployment debugging)

- **Registry trust is needed on BOTH machines, with the port.** Mac (Docker Desktop `insecure-registries`)
  for push; Orin (`/etc/docker/daemon.json`, **MERGE** — keep the `nvidia` runtime!) for pull. A bare IP
  (`192.168.8.149`) does NOT match a registry on `:5000` — use `192.168.8.149:5000`.
- **`buildx imagetools` ignores `insecure-registries`;** rig mirrors via `docker pull/tag/push` instead.
- **The baked artifact runs the compose-only form, not rig+vendored-launchers** (those have `build:` sections
  and would try to build from absent source). `run.sh` prefers `up.sh`/`down.sh`/`status.sh`.
- **Mirrored multi-arch image digests are fragile** (index vs per-arch manifest, re-push churn) → rig keeps
  them as registry **tags**; only built single-arch images are digest-pinned.
- **`rig build` and `rig bake` must see one consistent registry state**, and the registry must **persist**
  (`-v registry-data:/var/lib/registry`) so digests survive until the vehicle pulls.
- **ROS 2 names allow no hyphens** — `cam_usb`, not `cam-usb` (the camera namespaces a node by the name).
- **`images.tag: jp7` is the deployment tag** — platform-specific services (camera) use it; platform-agnostic
  ones (dashboard) must still pull the *same* tag or they won't find their image.

## Resume checklist

1. The vehicle is **already up** — check it first: `ssh orin 'cd ~/ws/test1 && ./run.sh status'` (or
   `docker ps`). Dashboard: `http://10.160.1.21:8080`.
2. Finish the physical verification (open item 1): RTSP camera power, webrtc streams in a browser.
3. To iterate: edit configs/repos in `/Users/ckt/ws/rig-walkthrough/` → `rig build -j 3` (if images changed)
   → `rig bake --tag testN` → `scp` to the Orin → `tar xzf` → `./run.sh up`. Verify
   `curl -s http://192.168.8.149:5000/v2/_catalog` between build and bake.
4. Teardown when done testing: `ssh orin 'cd ~/ws/test1 && ./run.sh down'`; stop the dev-box registry.
