# rig — project state & handoff (resume here)

> Snapshot for picking the project up cold in a new session. Read this first, then `RUNBOOK.md` (the deploy
> steps), then `DESIGN.md`/`ROADMAP.md` for rationale. As of: rig **v0.1.17**, branch
> **`config-schema-symmetric`**, ~33 tests passing (`python3 tests/test_*.py`). Tool at
> `/Users/ckt/ws/bringup`; run-from-source `./rig <verb>`.

## TL;DR — where things are

A real deployment to a Jetson Orin from a local registry is **partway up**. The rig tooling is solid and
well-tested; the remaining work is **cross-repo** (camera-service + dashboard need a few changes to be fully
bake-friendly), captured as ready-to-paste prompts below.

**The live test:**
- **Dev box:** this Mac. Local registry at **`192.168.8.149:5000`** (Docker Desktop must trust it via
  `insecure-registries`). Workspace `~/rig-walkthrough/` (siblings: `rig/`, `camera-service/`, `dashboard/`,
  `test-vehicle/` = the deployment).
- **Vehicle (Orin):** `vehicle: orin-test-vehicle`, `vehicle_id: 1`, `rmw_zenoh_cpp`, `images.tag: jp7`,
  `images.registry: 192.168.8.149:5000`. Artifact unbaked at `~/ws/test1` on the Orin.
- **Stacks (4):** infra `zenoh-router` (order 0) + `dashboard` (order 5); sensors `cam_usb` + `cam_rtsp`
  (camera-service, `camera.type: usb`/`rtsp`).
- **Working:** zenoh-router (up, pulls eclipse/zenoh), the two cameras (USB device passthrough was the last
  thing being sorted — see open items). **Not deployed:** the dashboard (image gaps, below).

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

## OPEN ITEMS — cross-repo (prompts ready)

These are the remaining gaps; the rig side of each is done. The recurring principle: **a launcher's `config`
output must be host-independent** so a bake on the dev box (no camera, wrong JetPack) captures a compose
correct for the *target* — drive everything off the config + rig's env, never probe the bake host.

**camera-service (`cam-up`):**
1. **Platform from `RIG_IMAGE_TAG`, not host detection** — image tag (`cam-core:${RIG_IMAGE_TAG}`) AND the
   runtime overlay (`docker-compose.<platform>.yml`). (Was resolving `jp6` on the Mac.)
2. **USB device passthrough host-independent** — always render `devices: ["${usb.device}:${usb.device}"]`
   from the config (a `/dev/v4l/by-id/...` path mapped to itself, so it exists in the container without
   udev), regardless of whether the device exists at config time. (Last thing being debugged.)
3. **Recordings → `${RIG_DATA_DIR:-/data}/recordings/<name>`** (absolute, so bake leaves it literal instead
   of pulling it into `compose/<name>/core-driver__recordings/`).
4. **Honor `COMPOSE_PROJECT_NAME`** — drop `-p`, set a standalone fallback.

**dashboard (`dash-up`):**
1. **`dashboard-web`** isn't built (registry has no such image) — either build it (Caddy + the React `dist/`)
   or default the web layer to the **mirrored caddy** (`mirror: [caddy:2-alpine]`).
2. **Tag** — pull with `${RIG_IMAGE_TAG:-arm64}` so build (jp7) and pull agree (was building `:jp7`, pulling
   `:arm64`).
3. Honor `COMPOSE_PROJECT_NAME`; confirm infra placement; drop the "rig BUILD phase" framing (rig has one now).

**boilerplate `<device>-up` (novatel/sbg launchers):** honor `COMPOSE_PROJECT_NAME` (drop `-p`).

**rig follow-ups (`ROADMAP.md`):** `bake --bundle-images` (air-gap), OCI artifact format, ROS `/diagnostics`
as a 2nd health layer, boot-time systemd unit, `rig adopt/verify`.

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

1. `cd ~/rig-walkthrough/rig && git pull` (latest rig). Read `RUNBOOK.md`.
2. Apply the open camera-service + dashboard prompts above (or `git log` those repos to see what's done).
3. On the Mac: `rig build -j 3` → verify `/v2/_catalog` → `rig bake --tag testN` → ship to the Orin → `./up.sh`.
4. The cameras are the deliverable; the dashboard bolts on once its image story is fixed.
