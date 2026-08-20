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
- **Platform targeting.** vehicle.yaml `platform:` (a HOST fact) → `RIG_TARGET_PLATFORM` + each service's
  declared `platform.override_env`; services with a `build.platforms` matrix pull the composed
  `<image>:<tag>-<platform>`. `images.tag` means VERSION only. (Full section below.)
- **Status/health.** rig calls each launcher's `status` (`ps --format json`) and rolls a project up to one
  row: healthy iff every *healthchecked* container is healthy and all are running (a plugin without a probe
  doesn't drag the sensor to "unknown"). ROS `/diagnostics` aggregation is a planned second layer.
- **Lifecycle/cleanup.** Restart/boot/teardown are the substrate's job (Docker Compose now;
  systemd/Quadlet/k3s later). External volumes survive `down` by design (a consumer may still be attached);
  `rig down --purge` removes the `rigging.yaml`-declared `external_volumes` on **final** teardown only —
  `docker volume rm` refuses an in-use volume, which is the safety we want. `rig cleanup` (v0.2.15) is
  the decommission form: after the final down, before deleting the tree, it removes the deployment's
  image refs (rendered composes ∪ rig.lock pins, by ref, never `-f`) and its volumes (declared +
  compose-project-labeled residue; `--keep-volumes` opts out). Services never remove volumes on their
  own teardown (a consumer may still be attached) — rig owns removal, at both tempos. cleanup refuses
  while any project still has containers (that's `down`'s job) and never touches RIG_DATA_DIR. Baked
  artifacts carry the same sweep as `cleanup.sh` (`./run.sh cleanup`).
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
- **rig never pushes implicitly**: every authoring verb writes + validates into a registry
  checkout (git targets get a local commit on a `promote/` branch); publishing is plain git, or
  the explicit `rig registry push` (v0.2.8 — SYSTEM git, `promote/*` branches only, never the
  default branch; `--pr` delegates PR *creation* to the user's own gh/glab; rig holds zero
  credentials). `rig setup` owns all user state (`~/.rig`, default registry, shell PATH block,
  `--purge`) — package managers never touch $HOME.

## Vehicle-local vars & fleet artifacts (v0.1.47–48)

One artifact, N vehicles. Configs and manifest scalars reference per-vehicle values as
``{{var}}`` (deliberately not ``${VAR}`` — compose refs pass through untouched), resolved
most-specific-wins: shell (`RIG_VAR_*`) > deployment-local `vehicle.local.yaml` (bench trees) >
**`/etc/rig/vehicle.local.yaml`** (THE machine's identity, written by `sudo rig provision`) >
vehicle.yaml `vars:` defaults. A self-marker (`vehicle_id: "{{vehicle_id}}"`) declares the value
MANDATORY per vehicle — loading fails with the provision hint rather than coming up as vehicle 0.
An `env:` map rides the existing fleet_env channel (interpolated; rig-owned keys rejected) for
services that consume environment instead of config. `rig bake` needs no flag: fleet-ness is a
property of the deployment (templates in ⇒ templates out), and bake reads NEITHER local file nor
shell vars — bench identity cannot leak into an artifact, and rendering happens on-vehicle via
the bundled rig. The working-copy machinery (pins/diff/promote/upgrade) operates on raw bytes,
so registry profiles and overlays carry markers untouched: the template is the intent, the
resolution is per-vehicle.

v0.1.65 widens substitution with ONE mapping form: `{{map <list_var> <template_var>}}`
(whole-scalar only, renders a list; both args are var names so field vs SIL is a template-var
swap through the normal tiering) plus the derived built-in `fleet_peer_ids` (`fleet_ids` minus
THIS `vehicle_id`) — peer endpoints from a fleet roster, self excluded, one artifact for all.
Still no arithmetic, no conditionals, no nesting.

## The QoL & currency layer (v0.2.5–0.2.9)

The registry layer's daily-use surface (decision log: the QoL plan document; workflows:
CHEATSHEET §1.5 "the daily loop", RUNBOOK "registry maintainer loop"):

- **One add grammar, full inventory**: `pkg add` = `rig add` (paths, workspace names, refs,
  `sensor:` — dir-AND-registry ambiguity is a hard error); no-arg `pkg search` is the catalog;
  `pkg list` shows locally-routed services as `local`/`unpublished` rows — the promotion
  worklist. `pkg info --versions` enumerates git history (@old was installable, now
  discoverable).
- **The verb taxonomy** (settled): `save` = update the package an instance CAME FROM, in place
  (top-of-stack: the last bound overlay when one exists, else the pinned profile; routed
  services save their code pointer) · `promote` = something NEW (first publish, fork, kind
  change, suite, bake-down) · `repin`/`rebase` = registry-side maintenance (declared PINS vs
  fork PAYLOAD) · `upgrade` = the deployment follows (`--dry-run` runs the real sweep, then
  rolls back).
- **Currency detects → repair → propagate**: `pkg outdated` reports dependency drift across all
  four kinds (FIX column names the verb; exit 1, CI-able); `registry sync` prints a
  package-level delta digest; consumers follow repairs with `sync` + `upgrade`.
- **The publish tail + undo**: `registry pending/push/discard` and `pkg yank <ref> --from`
  (previous version restored from git history; a first publish is removed). The governing
  invariant BOTH directions: **the render never changes** — save moves tuning local→packaged
  byte-identically (deltas recomputed against the base, never patch-composed), and
  discard/yank move it back (the un-save fix-up re-anchors the cwd deployment with the delta
  as local edits again).

## Platform targeting (v0.2.14)

`images.tag` used to multiplex two orthogonal dimensions — WHICH RELEASE and WHICH HARDWARE
(camera-service ships distinct jp6/jp7 image sets: both arm64, differing in userspace — l4t base +
nvidia runtime vs ubuntu 24.04 + runc/CDI). You could not pin `v1.3.0` and declare `jp7` at once, and
cam-up carried the scar tissue ("is RIG_IMAGE_TAG a platform name?"). Now the two are first-class:

- **`platform:` is a HOST fact** — top-level in vehicle.yaml, sibling of `data_dir`. In rig's model
  vehicle.yaml plus the vehicle-local tier IS the per-host declaration: `/etc/rig/vehicle.local.yaml`
  may carry it per machine (`sudo rig provision --platform jp7`), a self-marker
  (`platform: "{{platform}}"`) makes it mandatory-from-local for fleet artifacts, and the resolved
  value joins the var context so configs may CONSUME `{{platform}}`. Never declared in sensor/instance
  configs or profiles — those stay portable across vehicles.
- **Services declare their dependency** in rigging.yaml: `build.platforms: [jp7, jp6]` (the build
  matrix — absence = platform-independent) and `platform: {auto_detect: <probe>, override_env:
  CAM_PLATFORM}` (the launcher's standalone host probe, and the env var it honors as override).
- **Routing**: `fleet_env` exports `RIG_TARGET_PLATFORM` (rig-owned, popped when undeclared) and
  every launcher invocation (up/config/pull/status, bake's compose capture, certify, build) also gets
  the service's `override_env` set to the same value. **Declared wins** over the launcher's
  auto-detect — bake renders on dev boxes that aren't the target; `auto_detect` remains the
  standalone/no-rig fallback.
- **Composed refs**: for a matrix service the per-service env carries `RIG_IMAGE_TAG=<tag>-<platform>`
  (bare `<platform>` when no tag — the platform's moving head), so its compose pulls
  `cam-core:v1.3.0-jp7`. The launcher contract is unchanged (composes still pull `:${RIG_IMAGE_TAG}`);
  bake/pull/lock inherit the composed refs (one digest per host's resolved ref). NOT multi-arch
  manifest lists: jp6/jp7 are the same arch, the tag must carry the platform. `rig build` passes the
  composed tag as the existing `<cmd> <registry> [tag]` second arg plus the platform env
  (`--platform` overrides, like `--registry`/`--tag`).
- **certify** runs the suite AS the first matrix entry (tag agreement expects the composed
  `certify-tag-x-<p>`) and adds a `platform` check: every other matrix entry must render on any host,
  pull built images as its composed tag, and differ from the first entry's render (byte-identical =
  the launcher host-probed). This makes the routing a standard, not a convention.
- **Migration**: a platform-valued `images.tag` with no `platform:` behaves EXACTLY as before (no
  composition, no export) plus a deprecation warning — data-driven (the in-use riggings' matrices
  define what counts as a platform name; rig hardcodes none). `doctor`: ERROR when the declared
  platform isn't in a service's matrix; WARN for matrix-without-platform and tag-is-a-platform cases.

## Status & roadmap

Implemented (see `ROADMAP.md` for the per-version log): manifest/catalog/descriptor loaders with
validation; overrides + nameless profiles; dispatch with fleet env + dry-run + tiered ordering
(`infra:` → `sensors:` → `autonomy:`; down reversed, so the decider dies before its eyes); `status`
roll-up; run directories (one session = one folder, `new-run`/`end-run`/`runs`); `doctor` (incl.
enabled-aware host-port clash checks, distro agreement as ERROR) + `doctor --deep`;
`up/down/--purge/logs/config/pull` + `cleanup` (decommission: images/volumes off the host, v0.2.15);
`certify` (the launcher contract as executable checks, `--repo` CI
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
boot-time bring-up via a systemd unit + a `rig verify --fix` reconciler; OCI artifact format.
(Fleet mode — one artifact, N vehicles, identity resolved on-vehicle — ✅ shipped v0.1.47–48, above.)

See **`docs/ROADMAP.md`** for config overrides & reusable profiles (✅ implemented, §1) and the **SIL/HIL**
model (per-sensor source × per-run footprint — still open, §2).
