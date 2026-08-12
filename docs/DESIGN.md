# rig — design & decision log

## Problem

A vehicle computer (Jetson) runs several heterogeneous sensor/autonomy stacks: a rich GigE Vision camera
service (`camera-service`) and thin in-house ROS 2 nav drivers (`novatel`, `sbg`, and more to come,
all scaffolded from a shared Copier template). Each is its own repo with its own image and a per-sensor
launcher. We need one machine-level tool to bring the whole vehicle up/down/observe — without coupling to
any one sensor type, and without re-implementing per-stack logic.

## Core decision: a loop + a manifest that delegates

`rig` is deliberately thin. The hard, stack-specific work (capture/timestamp/record/shm for the camera;
lifecycle + params + transport for the nav drivers) already lives in each service's launcher. `rig`:

1. reads `vehicle.yaml` (which sensors, order, fleet ROS env) + `services.yaml` (where each repo lives),
2. for each sensor, reads the repo's `rigging.yaml` and invokes `<launcher> <config> <verb>`,
3. owns only what is genuinely vehicle-wide (below).

This keeps the one-way dependency clean: **a service never imports or knows about rig.** New services
(a lidar, an autonomy stack, a ported third-party driver) join by adding a launcher + a `rigging.yaml`
(`rig rigify` scaffolds both onto existing software),
not by changing rig.

## The launcher contract (rig-compatible)

A launcher must: expose `up/down/status/logs/config` on **one** config; accept a config at an **arbitrary
host path**; derive **all identity from the config's `name`**; **honor** `ROS_DOMAIN_ID`/`RMW_IMPLEMENTATION`
from the environment; observe **stdout/stderr discipline** (machine output — `status`→`ps --format json`,
`config` — on stdout; human lines on stderr, so rig parses clean JSON); and ship a `rigging.yaml`.

`cam-up` (the camera launcher) is the exemplar; the Copier template (`boilerplate`) emits a `<device>-up`
that satisfies the same contract for every thin driver. rig does **not** reshape services toward one
template — it adapts to each via `rigging.yaml`'s `verbs` map (e.g. cam-up takes compose subcommands, so
`status → ps`).

## What rig owns

- **Instance-`name` uniqueness — the top correctness check.** Identity (compose project, external volumes,
  ROS namespace, ports) all derive from `name`; rig rejects a manifest with duplicates before doing
  anything. It also cross-checks each manifest entry against the config's own `service`/`name`.
- **Bring-up order: producers → consumers.** Ascending `order` for `up`, reversed for `down`, so shm/topics
  exist before consumers attach. Consumers are best-effort/retry regardless (`restart: unless-stopped`).
- **Fleet ROS env.** rig exports one `ROS_DOMAIN_ID` + `RMW_IMPLEMENTATION` before each launcher call; the
  launchers pass them into their containers, so every stack shares one DDS graph. Topics are namespaced
  `/<name>/…`.
- **Status/health.** rig calls each launcher's `status` (`ps --format json`) and rolls a project up to one
  row: healthy iff every *healthchecked* container is healthy and all are running (a plugin without a probe
  doesn't drag the sensor to "unknown"). ROS `/diagnostics` aggregation is a planned second layer.
- **Lifecycle/cleanup.** Restart/boot/teardown are the substrate's job (Docker Compose now;
  systemd/Quadlet/k3s later). External volumes survive `down` by design (a consumer may still be attached);
  `rig down --purge` removes the `rigging.yaml`-declared `external_volumes` on **final** teardown only —
  `docker volume rm` refuses an in-use volume, which is the safety we want.
- **Resource budgets (advisory).** `rig doctor` warns about `/dev/shm` aggregate and NVENC session budgets;
  it never blocks (rig treats driver configs as opaque).

## Decisions carried from the camera service (do not re-litigate)

- **Docker Compose per sensor** (one project each); delegate supervision to the substrate. Rejected: a
  Python supervisor driving the Docker socket.
- **Static compose, selected + parameterized** by each launcher; never generate compose.
- **shm is host-level**: an external named volume (`cam_<name>_sock`, the socket/*address*) + `--ipc=host`
  (the `/dev/shm` frame *data*). A consumer needs both. Rejected: podman/k8s pods (pod-scoped IPC walls shm
  off from other stacks).
- **Host networking** for sensor discovery; per-instance ports/topics namespaced by `name`.
- **One ROS distro fleet-wide** (Lyrical) + one RMW, so all stacks interoperate on one graph.

## The package-registry layer (v0.1.35–v0.1.44)

Services, sensor **profiles**, config **overlays**, and **suites** publish to git-repo registries
(`registry.yaml` + `<kind>s/<name>/manifest.yaml` + a GENERATED `index.json`; validation lives in
rig itself — `rig registry validate` — with thin GHA/GitLab CI wrappers). The public seed registry:
**https://github.com/christomaszewski/rig-registry-public** (namespace `public`). Client side:
`~/.rig/registries.yaml` is an ORDERED list (priority; `--front` lets a dev checkout shadow public),
git registries are managed full clones with ff-only sync, local-dir registries are used in place,
and a broken/too-new registry degrades with a warning instead of bricking field ops.

Key design points (full decision log: the registry plan document):

- **Terminology**: the tree `rig init` creates is a **deployment**; a manifest row is an
  **instance**; `rig bake` emits a **deployment artifact**.
- **Working-copy model**: install materializes a profile's payload as the instance's EDITABLE
  config; the pristine copy is pinned at `config/.pins/` and hash-anchored in `rig.lock`. The one
  primitive `structural_diff` (deep_merge's inverse) powers `config diff` (per-key attribution),
  `pkg upgrade` (three-way, local wins, conflicts loud), and `pkg promote` (the delta IS the
  overlay payload — round-trip law: promote → `overlay apply --clear-local` renders identically).
- **Four config layers, local beats overlays**: pin ⊕ bound overlays (ordered, last wins) ⊕ local
  file-delta ⊕ row overrides → `var/rendered/`.
- **Instance names stay ROS-safe and operator-chosen** (`--as`); `service@profile` is display
  only; registry provenance is a row field (`profile:`) + lock anchors.
- **Exact pins everywhere**: full-SHA sources, digest-only images, one `rig.lock`
  (registries@commit / package pins+hashes / instance anchors / bake's image digests);
  `--locked` reproduces byte-identically. Suites install atomically (any failure rolls the
  deployment back untouched).
- **rig never pushes**: promotion writes + validates into a registry checkout (git targets get a
  local commit on a `promote/` branch); publishing is plain git. `rig setup` owns all user state
  (`~/.rig`, default registry, shell PATH block, `--purge`) — package managers never touch $HOME.

## Status & roadmap

Implemented (see `ROADMAP.md` for the per-version log): manifest/catalog/descriptor loaders with
validation; overrides + nameless profiles; dispatch with fleet env + dry-run + tiered ordering
(`infra:` → `sensors:` → `autonomy:`; down reversed, so the decider dies before its eyes); `status`
roll-up; run directories (one session = one folder, `new-run`/`end-run`/`runs`); `doctor` (incl.
enabled-aware host-port clash checks, distro agreement as ERROR) + `doctor --deep`;
`up/down/--purge/logs/config/pull`; `certify` (the launcher contract as executable checks, `--repo` CI
mode, `--emit/--diff` host-independence proof); `build` (per-service build + mirror; exports
`ros.distro` as ROS_DISTRO); `vendor` (with provenance) and `bake/unbake` (digest pinning, compose-only
form, `--bundle-images` air-gap bundles, parent provenance on re-bake); the authoring family — `init`
(name seed, `--infra` path/bare-name workspace resolution, `--discover` one-level scan), `add` (wire a
service into an existing deployment), `fetch` (materialize configs for hand-authored rows), `rigify`
(retrofit the contract onto existing software, analysis-seeded, certify-green out of the box).
Validated against the real launchers (cam-up, dash-up, novatel-up, sbg-up, vectornav-up, the rig-infra
services) — and continuously, by `rig certify` in each repo's CI.

Open items: a **dev-vs-prod** affordance (cam-up's `--dev` vs config-driven replay for thin drivers); a
service-defined **`health` verb** (supersedes the ROS-`/diagnostics`-only idea — covers non-ROS stacks);
boot-time bring-up via a systemd unit + a `rig verify --fix` reconciler; fleet mode (one artifact, N
vehicles, id resolved on-vehicle); OCI artifact format.

See **`docs/ROADMAP.md`** for config overrides & reusable profiles (✅ implemented, §1) and the **SIL/HIL**
model (per-sensor source × per-run footprint — still open, §2).
