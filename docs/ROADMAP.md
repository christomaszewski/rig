# rig — roadmap

## 1. Config overrides & reusable profiles — ✅ implemented (v0.1.1)

### Motivation
Two needs, **one mechanism**:
1. **Multiple instances of one sensor type** that differ only in a physical id (camera serial, serial
   `by_id`, receiver IP) — without copying a whole config per instance.
2. **Flipping a sensor's data source per run** (e.g. replay GNSS while everything else runs live on real
   hardware — see §2) — without editing its deploy config.

Both are "a shared base + a small per-instance/per-run patch." This is a **rig-only** feature: the launchers
and the launcher contract do not change.

### Manifest schema
A sensor entry gains an optional `overrides:` mapping. `config:` may point at a full **named instance
config** (today's behavior) OR a **nameless profile** (a config with `service:` but no `name:`):

```yaml
# config/sensors/camera.profile.yaml   — reusable profile, NO `name`
service: camera-service
camera: { fake: false, pixel_format: Mono8, frame_rate: 20.0, ptp_enable: true }
recording: { enabled: true }
```
```yaml
# vehicle.yaml
sensors:
  - { name: cam_front, service: camera-service, config: config/sensors/camera.profile.yaml,
      overrides: { camera: { camera_id: "Lucid-2448-AAA" } } }
  - { name: cam_rear,  service: camera-service, config: config/sensors/camera.profile.yaml,
      overrides: { camera: { camera_id: "Lucid-2448-BBB" } } }
```

### Resolution pipeline (rig-side, per sensor)
1. Load the base config at `config:`.
2. **Name**: if the base has `name`, it must equal the manifest `name` (current cross-check); otherwise rig
   injects the manifest `name`. `service` must be present in the base and match the manifest `service`.
3. **Deep-merge** `overrides` onto the base (semantics below).
4. **Render only when needed**: if the base already has the matching `name` AND there are no `overrides`,
   pass the original file path unchanged (no render — keeps the common case file-for-file). Otherwise write
   the merged document to `var/rendered/<name>.yaml` and pass *that* path to the launcher.
5. The launcher receives a complete, named config exactly as today and never knows templating happened.

rig reads only `service` + `name`; the merge is a mechanical key overlay, so rig stays **schema-opaque** —
it never interprets what `camera_id` (or anything else) means.

### Merge semantics
- **Mappings**: recursive deep-merge; override keys win.
- **Scalars**: override replaces.
- **Lists**: **replace the whole list (v1)**. Predictable, and it covers the real cases (ids / IPs /
  `connection` are scalars-in-maps, not lists). *Keyed* list-merge — match items by their `name` field so you
  can tweak one `plugins:` entry without restating the list — is a v2 enhancement.
- **Deletion**: an override value of `null` deletes that key (so a profile default can be removed).

### Where rendered configs land
`var/rendered/<name>.yaml` (gitignored, mirroring the launchers' own `var/run/`). Overwritten each run.
`rig --dry-run` / `rig config` print the rendered path so a run is inspectable.

### Cross-checks & doctor
- `service` required and matches the manifest; instance `name` unique across the vehicle (unchanged).
- A nameless profile is valid only when referenced by a manifest row that supplies the `name`.
- `rig doctor` surfaces the resolved per-instance id/source so a run is self-documenting, and warns on
  dangerous combinations (e.g. a replay/sim source under a vehicle footprint — see §2).

### Phasing
- **v1 ✅**: per-sensor `overrides` (dict deep-merge, list-replace, `null`-delete) + nameless profiles +
  render-to-`var/rendered/`. Rig-only; no launcher changes. (`rig_cli/resolve.py`; tests in `tests/`.)
- **v2**: keyed list-merge; run-level override layers (apply one patch across many sensors, for §2).

### Open decisions
- Confirm **list = replace** for v1 (vs keyed-merge now).
- **Layering**: single per-entry override (v1) vs a profile + shared + per-entry override stack (later).
- Keep **both styles** (named instance files AND profiles+overrides) indefinitely? (Recommended: yes —
  named files stay valid and are simplest for one-offs.)

### Non-goals (v1)
No launcher changes; no semantic interpretation of config bodies by rig; no simulator integration.

---

## 2. SIL / HIL via per-sensor source × per-run footprint (related)

Not modeled as enforced vehicle-wide modes. Two independent axes:
- **Data source** — per *sensor*: live | replay | sim (a config / override concern, §1).
- **Footprint** — per *run*: vehicle | bench | laptop (images / runtime / net; cam-up's existing `--dev`).

"HIL test" = real-hardware footprint with a chosen per-sensor source mix (e.g. **replay GNSS while the
algorithm stack runs live on the real Jetson** to characterize compute/memory). Replay runs *through the
real driver* (file transport), so the load is faithful.

rig owns: the per-run footprint token (passed to launchers), **surfacing/validating the source mix** (refuse
or warn a replay/sim source under a vehicle footprint), and **source-aware doctor** (check "receiver
pingable" only for live stacks; "capture exists" only for replay stacks). Launchers own the mechanics
(mounting the replay capture; the footprint image/net swap). Named presets (`deploy`/`hil`/`sil`) are
overridable shorthands, never straitjackets.

**Caveat to design for**: under mixed live/replay, **time-base coherence** breaks for any node that *fuses*
sources (replayed GNSS timestamps vs live camera PTP time). Sound for resource/perf characterization; for
fusion fidelity, needs a coherent clock (`use_sim_time` + paced replay, or a simulator generating all
sources on one clock). Replay (single recorded source, own time) vs sim (generated, can be multi-source
coherent) is the real fidelity fork.

## 3. Deployment model — launch surfaces, vendoring, bake/unbake — ✅ core implemented (v0.1.2)

> **Status:** `rig vendor`, `rig bake`, `rig unbake` implemented (`rig_cli/{vendor,bake}.py`). bake produces a
> tagged, content-addressed `.tar.gz` with the resolved configs + vendored surfaces + rig + the compose-only
> form (validated self-contained for all three services — nav + camera — incl. profile-stripping and
> staging-bind localization). `rig bake --registry <host>` digest-pins images against a registry (via
> `docker buildx imagetools`) and the compose-only form references `<host>/<repo>@sha256:…` —
> validated end-to-end against a real local registry. **`rig build [--registry]`** populates the registry by
> running each service's declared `build:` command (build + push its own images) and mirroring its `mirror:`
> third-party images (`docker buildx imagetools`); specifying a full image ref directly stays the per-service
> `${<SVC>_IMAGE}` override. **Partial / next:** `--bundle-images` (air-gap docker save/load) + OCI artifact
> format remain.


The vehicle holds the **launch surface + configs**, never driver source. Flow: develop drivers (own repos)
→ push images (registry) + **vendor** launch surfaces into the rig repo → **bake** a tagged artifact →
ship/**unbake** on the vehicle. No submodules or source on the vehicle. rig's Python is a build/authoring/
observability tool; the runtime is `docker compose` (see "compose-only" below).

### Launch surface
The minimal file set rig needs to *launch* a service — never its source. Each service **declares its own**
in `rigging.yaml`:
```yaml
launch_surface:
  - novatel-up
  - tools/render_params.py
  - docker/compose/compose.deploy.yaml
  - docker/compose/compose.deploy.serial.yaml
```
(rig always vendors the `rigging.yaml` descriptor itself, so it's not listed.)
(The copier template emits this for thin drivers; the camera service lists its composes + `plugins/*/compose.yml` +
`tools/sensor_env.py`.) Typically a few KB of text.

### `rig vendor`
Copies a service's declared surface into the rig repo's `services/<name>/`, with provenance:
```
rig vendor novatel --from ../novatel
  → services/novatel/{novatel-up, tools/render_params.py, docker/compose/*, rigging.yaml, .vendored.yaml}
```
`.vendored.yaml` records `{source, ref(SHA), when}`; `services.yaml` points at the vendored path. The rig
repo is now self-contained. Source: a local checkout now → a published OCI **launch-surface bundle** later
(so no machine needs driver source).

### Vehicle deployment tree
`rig`, `vehicle.yaml`/`services.yaml`, `config/sensors/*`, `services/<name>/` (vendored), `rig.lock`. No git,
no submodules, no source — editable text + pulled images.

### `rig bake` / `rig unbake` (inverse operations)
`bake --tag <t>` snapshots the live tree → renders override/profile configs to final, pins image **digests**,
bundles vendored surfaces + rig itself + metadata `{tag, vehicle, source SHAs, timestamp}` → one
**content-addressed, tagged artifact** (OCI or tarball). `unbake` restores it to an editable tree. Both run
**on the vehicle** (bake the tweaked field state; unbake to tweak). One artifact, two run modes: immutable
(run as-is) or mutable (unbake → tweak → up → re-bake).

### Compose-only resolved output (runs with just Docker — no Python/PyYAML)
bake also compiles the dynamic orchestration to static, so the artifact degrades gracefully on a host
lacking Python/PyYAML. Per sensor it captures the launcher's `config` verb output (`docker compose config`
= a fully-resolved, interpolated, includes-flattened compose) + the rendered params into `compose/<name>/`,
plus flat `up.sh`/`down.sh`/`status.sh` (order baked into line order). A POSIX-`sh` bootstrap runs `rig` when
Python+PyYAML are present, else the static scripts. Lost in compose-only mode: only bake-time/observability
sugar (rolled-up status table, doctor); run-time essentials (ordered up/down, per-project ps/logs, devices,
digest-pinned images) all work.

bake-time transforms required for the compose-only form:
- **Relocate + rewrite** rendered-config/params mounts to artifact-relative paths (copy the files in); leave
  device / `/dev/shm` mounts literal.
- **Emit `docker volume create`** for `external: true` volumes (the camera's `cam_<name>_sock`) — `up` won't self-create them.
- **Strip `build:` and pin `image:` to `@sha256:` digests** (the camera's `core-driver` carries a build block beside a local `image:` tag).
- **Capture `COMPOSE_PROFILES`** into the script env (the camera's active plugins).

### Images & offline / local-registry deployment
Digests are content-addressed → the **same `sha256` is portable across registries**, but the **host in the
pinned ref must be one the vehicle can reach**:
- `rig bake --registry <host>` pins as `<reachable-registry>/<repo>@sha256:<digest>` — e.g. a **local
  registry on the dev box** (`devbox:5000/...`), not public `ghcr.io`.
- **Mirror** the pinned, arch-correct (arm64/Jetson) images in with **`skopeo copy` / `crane cp`** (they copy
  blobs+manifest verbatim so the digest is preserved, and can copy the whole multi-arch index — `docker
  tag`+`push` may re-serialize).
- **Once pulled, images live in the vehicle's local Docker store**, so after the first pull the vehicle runs
  fully **offline** — the registry is only needed for initial pull/updates, not at `up` time.
- One-time vehicle host config: allow the registry (`insecure-registries` for plain-HTTP LAN, or a TLS cert). → HOST_SETUP.
- **`rig bake --bundle-images`** (true air-gap): `docker save` the pinned images into the artifact; `unbake`
  `docker load`s them → zero registry at deploy time, at the cost of a much larger artifact.

### Open decisions
- Artifact format: **OCI** (registry-native, pull-by-digest) vs **tarball** (zero-infra) — likely both.
- What bake resolves: fully-resolved configs (lean) vs raw+lock.
- Default image distribution: local-registry pin (the offline case) vs `--bundle-images` for full air-gap.
- Launch-surface source: local checkout (v1) → published OCI launch bundle (v2).

## 3b. Shared infra tier + vehicle identity — ✅ implemented (v0.1.6)

- **`infra:` tier** in vehicle.yaml — shared vehicle-wide services (a zenoh router, brokers, time-sync, …)
  brought up **before** sensors and torn down **after**, on the same delegated model (`rigging.yaml` +
  launcher). Names are unique across infra + sensors; vendor/bake/status include them. Omit (or
  `enabled: false`) for a DDS RMW or a ROS-less vehicle.
- **Vehicle identity**: `vehicle_id` decides the ROS domain (explicit `ros.domain_id` overrides) and is
  exported to every stack as `VEHICLE_ID` (alongside `ROS_DOMAIN_ID`/`RMW_IMPLEMENTATION`/`RIG_IMAGE_REGISTRY`).
- **Zenoh guardrail**: `rig doctor` warns if `ros.rmw` is zenoh but no zenoh router is declared in `infra:`.
- **`templates/zenoh-router/`**: a ready-to-use shared router service (rigging.yaml + launcher + compose,
  host net :7447). Point `services.yaml` at it + add an `infra:` entry; adjust the image/command for your
  exact rmw_zenoh router (e.g. `ros2 run rmw_zenoh_cpp rmw_zenohd` on a ROS image).

## 4. Other tracked items
- **Boot-time bring-up**: a systemd unit running `rig up` (Compose handles per-stack restart thereafter).
- **ROS `/diagnostics`** as the second health layer in `rig status`.
- **Host-facing port-clash** extraction for list-structured configs (the camera's WebRTC port), via the
  `host_ports` path syntax or a launcher `ports` query.
- **camera image publishing** — ✅ now via `rig build` (the camera's `tools/build-images.sh` pushes
  `cam-core`/`cam-dev` to the registry; `rig bake --registry` pins them by digest).
- **cam-up `SENSORS_DIR` robustness** — ✅ done (cam-up makes the `cd` tolerant of a missing dir, so the
  vendored camera surface runs standalone and bakes its compose-only form).
- **bake follow-ups**: `--bundle-images` (docker save/load for full air-gap, no registry) + OCI artifact
  format. (`--registry` + digest pinning + `rig build` done.)
- **camera-service `rigging.yaml`** — ✅ done: declares `external_volumes: ["cam_{name}_sock"]` so
  `rig down --purge` GCs it.
