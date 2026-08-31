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
camera: { type: gige, frame_rate: 20.0 }              # general: type selects the source
gige:   { fake: false, pixel_format: Mono8, ptp_enable: true }
recording: { enabled: true }
```
```yaml
# vehicle.yaml
sensors:
  - { name: cam_front, service: camera-service, config: config/sensors/camera.profile.yaml,
      overrides: { gige: { camera_id: "Lucid-2448-AAA" } } }
  - { name: cam_rear,  service: camera-service, config: config/sensors/camera.profile.yaml,
      overrides: { gige: { camera_id: "Lucid-2448-BBB" } } }
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
- ~~Confirm **list = replace** for v1~~ — confirmed and shipped in v1 (keyed-merge stays a v2 item).
- **Layering**: single per-entry override (v1) vs a profile + shared + per-entry override stack (later).
- Keep **both styles** (named instance files AND profiles+overrides) indefinitely? (Recommended: yes —
  named files stay valid and are simplest for one-offs.)

### Non-goals (v1)
No launcher changes; no semantic interpretation of config bodies by rig; no simulator integration.

---

## 2. SIL / HIL via per-sensor source × per-run footprint (related)

> **Status:** the REPLAY half is implemented — §15's graph epochs (v0.2.32) select the topic set,
> and **`rig replay`** (v0.2.33; plan `rig-replay-plan.md`, player contract
> `~/ws/infra/rig-replay-player-handoff.md`) plays a sealed run's recorded inputs at the named
> instances via rig-infra's `ros2-bag-player` (≥ v1.8.0), in a new provenance-linked run
> (`replay: {of, source, with}`) with one rig-owned clock token (`RIG_SIM_TIME` → the player's
> `--clock` + adopted launchers' `use_sim_time`). The per-sensor source axis and the footprint
> token below remain open — `RIG_REPLAY_SOURCE` is deliberately fleet-general so a per-sensor
> replay source (e.g. camera-service replaying its own mkv+csv recordings) consumes the same hook.

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
> `${<SVC>_IMAGE}` override. **Partial / next:** `--bundle-images` ✅ done (v0.1.21 — docker save into the
> artifact, up.sh self-load + load.sh); OCI artifact format remains.


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
  exported to every stack as `VEHICLE_ID` (alongside `ROS_DOMAIN_ID`/`RMW_IMPLEMENTATION`/`RIG_IMAGE_REGISTRY`/`RIG_IMAGE_TAG`).
- **Fleet image tag**: `images.tag` in vehicle.yaml → `RIG_IMAGE_TAG`; composes pull `<repo>:<tag>`, and
  `rig build` defaults its `--tag` to it. Platform-agnostic services (dashboard) just ignore it.
  (Since v0.2.14 the tag means VERSION only — the hardware target moved to the first-class `platform:`
  field, §7; a platform-valued tag remains the deprecated legacy spelling.)
- **Zenoh guardrail**: `rig doctor` warns if `ros.rmw` is zenoh but no zenoh router is declared in `infra:`.
- **`templates/zenoh-router/`**: a ready-to-use shared router service (rigging.yaml + launcher + compose,
  host net :7447). Point `services.yaml` at it + add an `infra:` entry; adjust the image/command for your
  exact rmw_zenoh router (e.g. `ros2 run rmw_zenoh_cpp rmw_zenohd` on a ROS image).

## 3c. Run directories — one session, one folder — ✅ v1 implemented (v0.1.25)

### Motivation
Consolidate every service's data output (bags, camera recordings, future sensors) under **one directory
per session**, so a field test is a single `scp -r`-able folder that also records *what software produced
it*. Today the tree is service-first (`data_dir/recordings/<name>/…`, `data_dir/bags/<name>/…`) with
independent per-service timestamps; runs make it session-first.

### The two traps (why the obvious designs are wrong)
1. **"A run = a `rig up`" splits data.** `up` is imperative and partial (`rig up cam_usb` after a config
   tweak; compose restarting a crashed stack at 3am with no rig involved). Implicit rotation forks one
   stack's data into a new run while the rest keep writing to the old — split-brain trees. **Rotation is
   an operator decision, never a lifecycle side effect.**
2. **`RIG_RUN_ID` as env poisons the bake model.** A run id interpolated into compose bind paths would be
   FROZEN into the baked artifact (`bake` captures `docker compose config` under the fleet env) and would
   break the determinism `certify` enforces. **The run id lives in the filesystem, not the env** —
   composes stay static.

### Design
A tiny registry on the host, owned by rig, under `data_dir` (so it survives redeployments):
```
<data_dir>/runs/<UTCstamp>_<label|auto>/   # one dir per run; manifest.yaml inside
<data_dir>/current -> runs/<id>            # THE pointer; at most one run is ever open
```
Services write via the pointer: `<data_dir>/current/<kind>/<name>/…`. **Writers pin the run at process
start** (resolve `current` once, e.g. `readlink -f`, falling back to the flat layout when no `current`
exists — standalone compatibility). Pin-at-start is load-bearing: a live symlink flip would ENOENT a
recorder's next segment/split creation, so a running recording belongs to the run it started in, and
rotation therefore **refuses while this manifest's stacks are running** (`--force` = "I accept late
writes landing in the sealed run").

### State machine (complete)
- `up` **ensures**: no `current` → create `runs/<stamp>_auto/` + manifest + pointer, then start. Never
  rotates. (`_auto` in your registry = data collected outside a planned session — the safety net for the
  6am bring-up, reboots, systemd.)
- `new-run [label]` **rotates**: seal current (if open) + open new + repoint. Guarded while running.
- `end-run [--force]` **seals**: stamp `ended:` + the status table (`rig status` output, disk usage)
  into the manifest, remove `current`. Guarded while running; idempotent (no open run → warn, rc 0).
- `up --run <label>` names at entry: label matches the open run → join (idempotent, never mints `X_2`);
  else rotate-then-up (same guard). `down --end-run` seals after a successful full down — a partial or
  failed down leaves stacks running, so the guard refuses: you cannot seal out from under live writers.
- `runs` lists the registry (STATE: OPEN = current+no `ended:`; sealed = `ended:`; interrupted = neither —
  surfaced, not hidden). `status` shows `run: <label> (open …)` / `no active run` in its header.

### Manifest = the machine contract
`runs/<id>/manifest.yaml`: run id, label, vehicle, `vehicle_id`, rig version, artifact tag (when running
in an extracted artifact — bake's provenance chain plugs in), `deployment:` (instance id, below),
`started:`, enabled stacks; `end-run` adds `ended:` + the status table + disk usage. **`ended:` present
⇔ sealed ⇔ safe for sync tooling** — scripts read manifests, never parse the human table.
Power loss mid-session: the symlink survives reboot; the run stays open and correctly continues.

### Config snapshots (v0.1.60) — what config was this data recorded under?
Every non-dry-run `rig up` captures the EFFECTIVE config into the open run:
`runs/<id>/.rig/config/<digest12>/` holds vehicle.yaml (+ vehicle.local.yaml / services.yaml /
rig.lock when present), `vars.yaml` (the RESOLVED var/env context — the only trace of machine-local
`/etc/rig` identity and `RIG_VAR_*` shell values), and `rendered/<name>.yaml` per enabled instance.
Content-addressed: an unchanged config writes nothing. The manifest gains `config:` (latest digest,
flat/grep-able) and an `ups:` event log (`at` / `stacks` / `config` / `deployment` / `root`) — the
temporal attribution: which config each stretch of the run's data was recorded under.
- **Capture at `up`, not open/seal**: every config that governed data was live at some `up`; the tree
  at seal may hold edits that never ran — so sealing only dirty-checks (`config_dirty_at_seal: true`
  + warning), never copies.
- **Deployment-instance id** (`var/deployment-id`, minted lazily; var/ is never staged by bake or
  tracked by git, so every untar/clone is a fresh instance): the dirty check fires only when the
  sealing tree IS the instance that took the run's last snapshot — a stale open run rotated away by a
  freshly deployed artifact seals clean.
- Fail-SOFT throughout: provenance must never wedge `up` or a seal.
- Compose-only `up.sh` does not snapshot (rig-only, like the flagged forms): a resolved artifact's
  config is fully determined by its recorded `artifact:` tag, and fleet artifacts route every verb
  through the bundled rig anyway.

### Docker log capture (v0.2.31) — what did the containers say?
`down --end-run` saves `docker logs --timestamps` (stderr merged, so one file reads like the terminal)
from every container of the deployment's compose projects into the sealing run:
`runs/<id>/.rig/logs/<sensor>/<container>.log` — under `.rig/` so a data kind literally named `logs`
under `current/<kind>/` can never collide. The capture point is **before the down verb dispatches**:
`compose down` REMOVES the containers and their stdout/stderr goes with them — seal time is too late,
which is also why standalone `end-run` (guarded to run only after teardown) cannot capture. `ps -a`,
so a crashed container's logs — exactly the ones worth keeping — are captured too. A partial down
retried later composes: each capture writes only the containers that still exist, per-file overwrite
keeps earlier captures. The manifest gains `docker_logs:` (`at` / `containers` count) when anything
was captured. Fail-SOFT like snapshots (never wedges `down`); rig-only like the other flagged forms
(`run.sh down --end-run` routes through the bundled rig, so artifacts get it for free).

### Compose-only parity
bake emits `new-run.sh` / `end-run.sh` / `runs.sh` (pure sh; manifest fields are grep-able flat keys),
an ensure-guard in `up.sh`, and the run header in `status.sh` — all with `data_dir` and the project-name
guard list inlined at bake time. Flagged forms (`up --run`, `down --end-run`) route through the bundled
rig (pyyaml hosts); bare-Docker hosts compose the primitives (`./new-run.sh X && ./up.sh`).

### Adoption (incremental, no flag day)
`data_dir` unset ⇒ the feature is inert (verbs error clearly; `up` skips the ensure). Services adopt with
a one-line path change to write via `current/`: the bundled bag-logger templates ship adopted (v0.1.25);
camera-service (`output_dir`/recordings default) is a small cross-repo PR; dashboard writes nothing.
Un-adopted services keep the flat layout — both coexist under `data_dir`.

## 3d. Third tier: `autonomy:` — ✅ implemented (v0.1.27)

> **Status:** shipped as specced (manifest rank map, descriptor `tier: autonomy`, doctor warnings, init
> scaffold + discover routing, tests, docs). One deviation from the "no changes expected" prediction in
> item 5: bake's vehicle.yaml re-emission split rows into only infra/sensor, so an autonomy entry was
> demoted to `sensors:` on round-trip — fixed (tier-keyed rows) and covered by a bake round-trip test
> that also asserts up.sh/down.sh line order. The compose-only partition otherwise held for free because
> `load_manifest` concatenates infra + sensors + autonomy and bake iterates that list as-is.

### Decision & naming
A third manifest tier for graph CONSUMERS — autonomy/algorithm stacks (planners, controllers, SLAM,
perception pipelines). Named **`autonomy`** (a role in the vehicle graph, like `infra`=substrate and
`sensors`=producers — not an implementation genre; "algos" describes code and breaks summary grammar).
Scope reading: anything that consumes the graph and starts last / stops first belongs here, including
non-"autonomous" perception/estimation stacks.

### Semantics
- **Hard ordering partition**: tier rank infra=0 → sensors=1 → autonomy=2 in `Manifest.select`
  (all autonomy after ALL sensors regardless of per-entry `order`; `down` reverses ⇒ **autonomy stops
  FIRST** — the decider dies before its eyes, a safety default even for retry-tolerant stacks).
- Ordering stays a courtesy, not correctness: consumers must still retry (zenoh discovery is dynamic).
- **The future payoff this tier structure enables**: when the `health` verb lands, `up --wait-healthy`
  gates between TIER BOUNDARIES — and sensors→autonomy is the boundary that matters (no planner arming
  before GNSS has a fix). Do not build the gating in this slice; build the structure it attaches to.

### Changes (≈ half day)
1. `manifest.py`: parse a top-level `autonomy:` list (same row schema); `tier="autonomy"`; rank map in
   `select()`; `stack_summary` counts ("2 sensors + 2 infra + 1 autonomy").
2. `descriptor.py`: `tier:` validation set grows to `{sensor, infra, autonomy}` (typo still errors).
3. `doctor.py`: ROS-name warning extends to autonomy-tier names (they namespace nodes too); new WARN —
   enabled autonomy with zero enabled sensors ("brain with no eyes").
4. `init.py`: scaffold `config/autonomy/` (+ .gitkeep); `--discover` routes `tier: autonomy` repos to an
   `autonomy:` menu section (bare header, uncommentable — same rules as sensors: MENU only, never
   auto-enabled; autonomy arrives from real repos, so there is NO `--autonomy` wiring flag).
5. bake/runs/certify: no changes expected — entries flow through `select()` order (verify up.sh line
   order puts autonomy last, down.sh first; runs manifests list them via `stacks:`).
6. Tests: partition guarantee (autonomy after sensors regardless of order numbers; down reversed),
   doctor warnings, summary, discover routing fixture, init scaffold. Docs: README (vehicle.yaml +
   rigging `tier:` row), CHEATSHEET, STATE. Version bump.

### Acceptance
A three-tier manifest: `up --dry-run` shows infra → sensors → autonomy; `down --dry-run` the reverse;
doctor green; a `tier: autonomy` fixture repo lands in the right menu section with its config under
`config/autonomy/`.

## 3e. Infra spin-out: `rig-infra` repo — ✅ complete (steps 1–2 v0.1.28; templates/ stub deleted v0.1.35)

> **Status:** rig-infra repo created (services moved verbatim, provenance in the first commit; `base/`
> fleet-ros image; defaults flipped; CI certifies 3/3 + the router_config path). rig side shipped:
> `--infra` path/bare-name resolution with one-level workspace scan, `--discover` descent, deprecation
> stub + pointer error, CI template-certify steps dropped, docs swept. v0.1.29 closed the distro loop:
> `rig build` exports vehicle.yaml `ros.distro` as ROS_DISTRO to build commands (fleet-ros bakes the
> fleet's distro), and doctor raises vehicle-vs-services distro disagreement as an ERROR. Remaining:
> migration steps 3–4 below (live deployments, dashboard-on-GitHub chore, then delete the stub next
> version).

### Why now (reversal of two earlier "keep in rig" calls — the triggers fired)
Three templates exist, roscore/mavlink queued; certify-in-CI now enforces the launcher contract
mechanically (co-location was a substitute for the gate); and the **fleet-ros base image** is the forcing
function — inside rig it needs a new image-authoring subsystem (`build --base`, generator, doctor
wiring); in a separate repo it is an ordinary `build:` declaration using machinery that already exists.
When avoiding a repo split requires growing the tool a subsystem, the split is cheaper.

### Target layout — `christomaszewski/rig-infra` (public, like rig)
```
zenoh-router/       ros2-bag-logger/       ros1-bag-logger/      # moved verbatim (rigging/launcher/
                                                                 #   tools/examples per service dir)
base/Dockerfile     base/build.sh                                # fleet-ros: FROM ros:<distro>-ros-base
                                                                 #   + rmw-zenoh-cpp + rosbag2(+mcap)
.github/workflows/certify.yml                                    # camera-service pattern: checkout rig,
                                                                 #   certify each service dir (declared
                                                                 #   examples = default --config)
```
- `base/build.sh <registry> [tag]` follows the rig build contract; zenoh-router + ros2-bag-logger
  riggings declare `build: { command: ../base/build.sh, images: [fleet-ros] }` (cwd = service dir; the
  double invocation when both are in one manifest is a docker-cache no-op — or guard in the script).
- **Defaults become opinionated — this closes two open issues at once**: router default =
  `fleet-ros:${RIG_IMAGE_TAG}` running `ros2 run rmw_zenoh_cpp rmw_zenohd` (**the rmw-family/router
  version-match fix** — same distro packages as the sessions, by construction), bag-logger default =
  `fleet-ros:${RIG_IMAGE_TAG}` (**decouples from ros2-bridge** — camera-less vehicles carry a ~1 GB
  base instead of a 3.4 GB camera image). Build/pull agreement holds by construction (fleet-ros is in
  `build.images`; certify's tag check enforces). Env overrides (`ZENOH_ROUTER_IMAGE`,
  `BAG_LOGGER_IMAGE`) stay; pinned `eclipse/zenoh:x.y.z` stays as the documented standalone
  alternative (never `:latest` — mirrored tags can silently drift on re-mirror).

### rig-side changes
1. `init --infra` generalization: a value containing `/` = a repo path; a bare name resolves by scanning
   the workspace (target's parent siblings, descending ONE level into repos whose subdirs carry
   rigging.yaml), falling back to rig's `templates/` while the deprecation stub exists. Same enabled
   wiring + relpath into services.yaml as today.
2. `--discover` gains the same one-level descent (finds rig-infra's service dirs; their `tier:` hints
   route the menu). Bounded depth; skip `var/`/hidden.
3. `templates/` → deprecation stub for ONE version (README pointing at rig-infra; old services.yaml
   paths fail loudly with a pointer, not a mystery), then delete — ✅ **deleted in v0.1.35** (stale
   services.yaml paths under templates/ still fail with the rig-infra pointer). rig CI drops the
   live-template certify steps (infra CI owns them); the sh-fixture certify tests remain rig's
   contract reference.
4. Note: §3d (shipped v0.1.27) already grew descriptor `tier:` + discover routing to three tiers —
   extend those, don't re-plan them.

### Migration sequence
1. Create rig-infra (plain copy of templates/, provenance note in the first commit — history stays in
   rig), add `base/`, flip the defaults, CI green (certify all three).
2. rig: `--infra`/`--discover` generalization + deprecation stub + tests + docs sweep of every
   `templates/` reference (README/CHEATSHEET/RUNBOOK/STATE/init hints). Version bump.
3. Update deployments (test-vehicle + any new: services.yaml paths → `../rig-infra/<svc>`), sync
   walkthrough clones; **batch the standing chore: put dashboard on GitHub** while doing repo work.
4. Next version: delete the stub.

### Acceptance
Fresh workspace with rig + rig-infra as siblings: `rig init x --infra zenoh-router --infra
ros2-bag-logger` (bare names) → doctor green, zero edits; `rig build` in that deployment builds + pushes
`fleet-ros:<tag>`; a camera-less manifest (router + bag logger + a GNSS driver) pulls no camera images;
infra repo CI certifies 3/3; old bundled-template `--infra` still works during the deprecation version.

### Estimate
~1–1.5 days total (repo + base + CI ≈ half day; rig generalization + deprecation + docs ≈ half day;
deployment updates + validation ≈ the rest). Requires creating the GitHub repo (public, to keep the
CI checkout secret-free like rig/camera-service).

## 4. Other tracked items
- **`rig fleet` verb group — ✅ phases 1+2 implemented (v0.1.66)**: GCS-side fan-out over the
  per-vehicle surface — NEVER a control plane. Shipped: `list`/`status` (aggregating the new
  `rig status --format json` machine contract)/`sync` (sealed-run harvest into
  `<into>/<label>/<vehicle>/<run-id>`, keyed off `ended:` ⇔ safe-to-sync) and
  `up --run <label>`/`down [--end-run]` (correlated labels; `--var` rides `RIG_VAR_*` into
  every run snapshot). Invariants held: system ssh/scp only (BatchMode, no credentials, no
  agent/daemon); remote end = the deployment's own `./run.sh`/`./rig`; fail-SOFT per vehicle
  (ssh 255 = UNREACHABLE mars the row, never aborts — DDIL); explicit roster only; no config
  side-channel (one provenance carve-out: `fleet up` pushes fleet.yaml beside each deployment
  so the run snapshot records the roster). SIL: local rows skip ssh; each row is a SIMULATED
  MACHINE (`<data_root>/.identity/<name>.yaml` via RIG_VEHICLE_LOCAL — the /etc/rig tier);
  the "shared run dir" is a VIEW (`<data_root>/runs/<label>-<date>/<vehicle>` symlinks into
  the per-vehicle registries), and `sync` materializes the identical tree from real vehicles;
  the docker network is create/rm + `RIG_NETWORK`/`RIG_VEHICLE_IP` env — services join it in
  their own composes. Still deferred: `provision`/`deploy` (sudo + fresh machines — after the
  plumbing hardens in the field), `fleet doctor` (cross-deployment host_ports aggregation).
- **Boot-time bring-up**: a systemd unit running `rig up` (Compose handles per-stack restart thereafter).
- **ROS `/diagnostics`** as the second health layer in `rig status`.
- **Host-facing port-clash** extraction for list-structured configs — ✅ done: `host_ports` supports an
  enabled-aware `plugins[name=webrtc-bridge,enabled=true].params.port` selector, and rig flags clashes
  across instances. The service must declare `host_ports` (camera-service: ✅ declares the webrtc port path).
- **camera image publishing** — ✅ now via `rig build` (the camera's `tools/build-images.sh` pushes
  `cam-core`/`cam-dev` to the registry; `rig bake --registry` pins them by digest).
- **cam-up `SENSORS_DIR` robustness** — ✅ done (cam-up makes the `cd` tolerant of a missing dir, so the
  vendored camera surface runs standalone and bakes its compose-only form).
- **bake follow-ups**: OCI artifact format. (`--registry` + digest pinning + `rig build` +
  `--bundle-images` (v0.1.21, docker save/load for full air-gap) done.)
- **camera-service `rigging.yaml`** — ✅ done: declares `external_volumes: ["cam_{name}_sock"]` so
  `rig down --purge` GCs it.

## 5. Package registry, distribution & fleet vars — ✅ implemented (v0.1.35–v0.1.48)

The full arc (design record: the registry plan doc; summaries in DESIGN.md; workflows in
CHEATSHEET §1.5–1.6):

- **v0.1.35** deprecated bundled `templates/` deleted (rig-infra owns those services).
- **v0.1.36** packaging foundation: pyproject + `rig` console entry point (pipx installs work).
- **v0.1.37** registry core: four manifest kinds + validators + deterministic index;
  `rig registry init|validate|index`; live seed registry **rig-registry-public** (GitHub).
- **v0.1.38** CLI noun taxonomy (config/run/registry/pkg/overlay/service/artifact/image) —
  every flat spelling stays a permanent alias.
- **v0.1.39** client registries: `~/.rig` cache + ff-only sync + degrade-not-fail; `rig setup`;
  `pkg search|info`; deployments born as git repos.
- **v0.1.40** one `rig.lock` (registries@commit / packages+hashes / instance anchors / images).
- **v0.1.41** `pkg install` + `rig add` unification (path | name | registry ref | `sensor:<id>`),
  vendored-at-pin self-contained deployments, `--locked` byte-identical reproduction.
- **v0.1.42** working-copy layer: `config/.pins/` anchors, `config diff` attribution,
  `pkg upgrade` three-way (local wins, conflicts loud), `pkg lock`.
- **v0.1.43** ordered overlay bindings + four-layer render (local beats overlays).
- **v0.1.44** `pkg promote` (overlay/profile/suite; round-trip law proven) + atomic suite
  install with rollback.
- **v0.1.45** docs sweep; embedded example names accepted verbatim (live-E2E catch).
- **v0.1.46** distribution: release-on-tag (tests → wheel/sdist → arch=all deb → GitHub
  Release), Homebrew tap with auto-bump (TAP_PUSH_TOKEN).
- **v0.1.47** vehicle-local vars: `{{var}}` interpolation, source precedence
  (shell > local > /etc/rig > vehicle.yaml), mandatory self-markers, `env:` passthrough.
- **v0.1.48** flagless fleet artifacts (templates in ⇒ templates out; bake blind to local
  sources) + `rig provision` with the `--force` re-identification gate.
- **v0.1.61** promote update-flow: manifest carry-forward on re-promote, package name from
  provenance, `--kind` inference for hand-authored instances, provenance-gated auto-bump,
  alias→namespace requalification in emitted refs, restore-not-delete rollback.
- **v0.1.62** correctness batch: `pkg upgrade` covers bound overlays (rebound in place) and is
  all-or-nothing (content-level rollback, install too); service pin collisions error; overlay
  binding hygiene; `--locked` verifies `source.rev`; sync warns on a stale index.
- **v0.1.68** fleet.yaml = the fleet-vars TIER (shell > local > machine > fleet.yaml >
  vehicle.yaml): `{{fleet_ids}}` derived from the roster (single source of truth), `{{gcs_ip}}`
  / `{{fleet_mode}}` from its keys, fleet `vars:` for policy like peer_endpoint; the pushed
  file persists, so mid-test reboots render current fleet values standalone (DDIL gap closed).
- **v0.1.67** profile lineage: forks record `based_on:` (parent@ver, namespace-qualified);
  `pkg rebase` three-ways a fork onto its parent's current version (old parent payload from
  registry git history; conflicts keep yours, loudly; requires adopted + re-qualified);
  `promote --adopt` closes the profile round-trip (re-pin + reset + unbind, render identical —
  a hand-authored instance GAINS provenance); `pkg list` ROLE column (active vs
  `dependency of <profile>`); `pkg info` shows lineage + a rebase hint when the parent moved;
  registry validate warns on in-registry parent drift.
- **v0.1.66** `rig fleet` — see §4 (fan-out verbs, SIL fleets, status --format json).
- **v0.1.65** `{{map <list_var> <template_var>}}` (whole-scalar, renders a LIST; template-as-var
  makes field vs SIL a tiering swap) + derived `fleet_peer_ids` (fleet_ids minus THIS vehicle) —
  peer endpoints, self excluded, one artifact for all; MAP-aware fleet detection; `rig init`
  scaffolds the vars/env convention (gcs_ip) and gitignores vehicle.local.yaml + fleet.yaml.
- **v0.1.64** UX batch: `pkg info @version` + authored_against + local state; dirty markers in
  `pkg list`; upgrade hints in `config diff`; search covers project tags/targets; `overlay
  list` as a status view; `pkg lock` on stdout + overlay payload verification; one parse_ref.
- **v0.1.63** **git history as the version archive** (capability-detected, read-only): the
  "ONE current version" model keeps the index simple while git-type caches — FULL clones —
  carry every past version. `pkg add ns/name@<old>` resolves from history (`git log`/`git
  show`, never a checkout); `--locked` re-resolves packages at the locked registry commit, so
  reproduction actually reproduces (hashes still gate — rewritten history fails loudly). A
  local-dir folder without `.git` keeps the exact old behavior; a local-dir that IS a git
  checkout gets the feature for free. No authoring-side changes (no tags, no push).

## 6. QoL & registry currency — ✅ implemented (v0.2.5–v0.2.9)

The registry layer's daily-use surface (plan doc: `rig-qol-plan.md`, untracked; design summary
in DESIGN.md; workflows in CHEATSHEET §1.5 + RUNBOOK "registry maintainer loop"):

- **v0.2.5** discovery & inventory: no-arg `pkg search` catalog (+`--kind`/`--registry`); ONE
  add grammar under both spellings (paths in `pkg add`; dir-vs-registry ambiguity = hard
  error); `pkg list` full inventory (local/unpublished rows = the promotion worklist).
- **v0.2.6** currency: `pkg outdated` (four kinds, FIX column, exit 1 on drift), sync delta
  digest, one shared namespace resolver.
- **v0.2.7** `pkg repin`: declared PINS advance registry-side (payloads stay `rebase`'s);
  suites refresh every member (in-registry members at head; since v0.2.19 a stale member is a
  legal snapshot — validate warns, install serves the pin from git history).
- **v0.2.8** `pkg save` (top-of-stack update-in-place; render-identity invariant) + the
  publish tail: `registry pending|push[--pr]|discard`, `pkg yank --from` — discard/yank run
  save's inverse (the delta returns as local edits, render byte-identical).
- **v0.2.9** `pkg info --versions` (history enumeration), `pkg upgrade --dry-run` (real sweep,
  rolled back), docs sweep.

## 7. First-class platform targeting — ✅ implemented (v0.2.14)

`images.tag` used to multiplex WHICH RELEASE and WHICH HARDWARE (camera-service's jp6/jp7 image
sets — same arch, different userspace), so a version pin and a platform declaration were mutually
exclusive. Plan doc: `rig-platform-plan.md` (untracked); design summary in DESIGN.md §"Platform
targeting".

- **v0.2.14** the `platform:` host field (vehicle.yaml + the vehicle-local tier; `{{platform}}`
  joins the var context; self-marker = mandatory-from-local; `rig provision --platform`);
  descriptor `build.platforms` matrix + `platform.{auto_detect,override_env}`;
  `RIG_TARGET_PLATFORM` export + per-service override_env mirror (declared beats auto-detect);
  composed `<tag>-<platform>` pull refs for matrix services (tag = VERSION only; bake/lock/pull
  inherit — one digest per host's resolved ref); `rig build` passes the composed tag + platform
  env (`--platform` override); certify's `platform` check (every matrix entry renders anywhere,
  pulls its composed tag, differs from the first entry); doctor platform/matrix validation;
  deprecated-but-working legacy path for platform-valued tags (data-driven detection).

## 8. Decommission sweep — ✅ implemented (v0.2.15)

- **v0.2.15** `rig cleanup` — after the final `rig down`, before deleting the tree: remove the
  deployment's docker images (rendered compose refs — disabled rows included, composed platform
  tags included — ∪ rig.lock tag+digest pins; by ref, never `-f`: docker's in-use refusal is the
  safety) and its volumes (the declared external set, idempotent with `down --purge`, plus
  compose-project-labeled residue; `--keep-volumes` opts out). Refuses while any project still
  has containers (`down` owns those); RIG_DATA_DIR never touched. Settled: services never remove
  volumes on their own teardown (a consumer may still be attached) — rig owns removal at both
  tempos (`down --purge` routine, `cleanup` decommission). Baked artifacts ship the sh parity
  form `cleanup.sh`, routed by `./run.sh cleanup`. Known caveat: image refs are daemon-global —
  two deployments pulling the SAME ref share one tag, so cleaning one untags it for both
  (`--dry-run` first on shared SIL boxes).

## 9. Suite closure — ✅ implemented (v0.2.16)

- **v0.2.16** suites capture BARE (service-backed, profile-less) instances: `promote --all
  --suite` emits a `services:` member from the row's lock service pin, so install can recreate
  the instance (from the service's example) and bind its promoted overlay — previously the suite
  published clean but every fresh `pkg add` died with "no instance created by this suite matches
  its targets". A bare instance whose service a profile member already covers is skipped with a
  note (a services: member would duplicate the instance); one with no registry pin gets a loud
  WARNING (adopt it first: `promote <svc> --kind service --adopt`). `registry validate` now
  enforces closure: an in-registry overlay member whose service targets no profile/service
  member covers is an ERROR at publish/CI time (instance-scoped-only overlays warn — suite
  installs create default-named instances).

## 10. The `vehicle` kind — ✅ implemented (v0.2.17)

Suites reproduce the package layer only — default instance names, one instance per profile,
service-wide overlay binding, no vehicle.yaml composition. Plan doc: `rig-vehicle-kind-plan.md`
(untracked).

- **v0.2.17** kind `vehicle` — a suite's instance PLAN: a TEMPLATE-form vehicle.yaml
  (`vehicles/<name>/config/vehicle.yaml`). Captured ONLY with the suite
  (`promote --all --suite S --vehicle V` — the one moment rows and members close by
  construction; a suite already carrying a plan re-captures it by name on later promotes),
  installed ONLY through the suite (standalone add refused), into an EMPTY deployment. Rows
  drive the install: custom names, order, enabled, tier placement, per-row overlay bindings in
  row order — plus N instances per profile, previously impossible. Identity that must be
  DISTINCT per host (`vehicle`/`vehicle_id`) is markers-or-absent (validate ERROR on literals);
  fleet DEFAULTS (`platform`, `data_dir`, `images`, `ros`, `vars`, `env`) stay literal — the
  vehicle-local tier overrides per host, and the target's pre-install identity survives the
  plan verbatim. Row refs are UNVERSIONED (the suite's members are the only pin authority —
  repin untouched); validate enforces plan closure both directions (row ref without member =
  ERROR; member no row references = dead-weight WARN). An overlay emitted by the same promote
  is folded into its source row. `repin <suite>` refreshes the vehicle member; direct vehicle
  repin is refused (re-capture is the update path).
- **v0.2.18** hand-authored instances survive the capture: an instance with NO registry anchor
  (e.g. its service declares no `examples:`, so install never materialized one) used to be
  skipped by `--all` — the plan row then couldn't rebuild (no example) or silently reproduced
  the example instead of the hand config. With `--adopt` (composable with `--all --suite` now —
  the CONSENT flag, since this mutates the origin and auto-derives a package name; rig never
  prompts, fleet/CI BatchMode), the capture promotes it as a PROFILE (the full config — an
  overlay is impossible with no base to diff, the same inference the named form makes) and
  ADOPTS it (row gains provenance, render unchanged); the profile joins the suite's members,
  the plan row references it (overrides/overlays stripped — the payload baked them in), and
  later captures see a normal pinned instance. WITHOUT --adopt the instance is skipped LOUDLY,
  the plan omits its row (the published suite stays consistent, just smaller), and the warning
  prints both fixes: re-run with --adopt, or name it yourself first
  (`promote <inst> --kind profile --name <short> --adopt`) and re-capture. No service pin at
  all still warns and skips (adopt the service first).

## 11. Stale pins are snapshots — ✅ implemented (v0.2.19)

- **v0.2.19** a member's release no longer breaks what pins it. Before: a suite member or a
  profile's exact `requires.service` behind registry-current was a `registry validate` ERROR
  and an install refusal ("uninstallable at this sync state") — so a service repo's
  registry-release CI failed validation because of suites/profiles it doesn't own, and those
  packages were uninstallable until someone repinned. Now: validate WARNS ("stale … installs
  from git history; repin refreshes"; a caret base above head stays an ERROR — nothing can
  satisfy it), suite members and exact profile requires install at their PINNED versions from
  the registry's git history (the same `resolve_ref(history=True)` path `pkg add ns/name@old`
  uses; overlay bindings land at the pinned version), and a non-git registry fails with the
  pointed hint. Currency is `pkg outdated`'s job (exit 1 on drift — the suite owner's CI
  signal) and `pkg repin` the refresh. `repin --dep <older>` is now an honored explicit pin.

## 12. Identity defaults to per-host markers — ✅ implemented (v0.2.20)

- **v0.2.20** `rig init` scaffolds `vehicle: "{{vehicle}}"` / `vehicle_id: "{{vehicle_id}}"` by
  default — per-host identity is supplied per machine (`sudo rig provision --id N --name X`,
  RIG_VEHICLE_ID/RIG_VEHICLE_NAME, or a bench vehicle.local.yaml); `--vehicle-id N` pins literals
  (dir name + id — the single-vehicle shape). Why: the old `vehicle_id: 1` default was "vehicle 1
  by accident" — the exact bug class the marker design exists to prevent — and it defeated
  vehicle-plan reproduction: a plan with markers installed onto an init'd tree inherited the
  scaffold literal (the plan install preserves a target's identity because it cannot tell a
  scaffold placeholder from a deliberate choice). With markers the default, a plan's markers
  propagate, and a deliberate `--vehicle-id` still wins. Quick-start docs pass `--vehicle-id 1`.

## 13. One base image per deployment + image audit — ✅ implemented (v0.2.21)

The motivating failure: two images in one deployment apt-installed `rmw_zenoh_cpp` at different
times → two package versions → zenoh sessions that can't talk. Prevention, detection, remediation:

- **`RIG_BASE_IMAGE` (prevention)** — the deployment's ONE base image, resolved as: vehicle.yaml
  `images.base` / `rig build --base-image REF` (an explicit full ref, verbatim) → else a service
  whose rigging declares `build: {…, provides: base}` (base = `build.images[0]`, composed like the
  pull side: `<registry>/<repo>:<tag>`, platform-composed for a matrix provider) → else none
  (advisory INFO in doctor when ROS services exist). Several providers naming the SAME image
  (fleet-ros from zenoh-router + bag-logger, one shared `../base/build.sh`) are one base — `rig
  build` stages it FIRST (dedup'd by resolved script path) and exports `RIG_BASE_IMAGE` to every
  other build, so dependents `FROM ${RIG_BASE_IMAGE}` and the distro+rmw layer is shared by
  construction; a base-stage failure stops the build. Providers naming DIFFERENT images are an
  ERROR (doctor + build) — rig never guesses by manifest order. An explicit external base skips a
  provider's build when the base is the only image it produces (nothing would pull it). fleet_env
  exports the same resolution to every launcher (a router compose RUNS the base directly), bake
  carries `images.base` into the artifact, certify never inherits a shell value, and `env:` maps
  can't shadow it (rig-owned).
- **`rig image audit` (detection)** — renders each enabled stack's compose (the launcher's `config`
  verb under the real fleet env, so `${<SVC>_IMAGE}` overrides and composed platform tags are
  honored), collects the `image:` refs, and inspects each unique image via
  `docker run --entrypoint /bin/sh` (`ls /opt/ros` + dpkg `ros-*` listing). Checks: every ROS image
  carries `ros.distro` (ERROR), the declared rmw package is installed in matching images (ERROR),
  and every ros-* package shared by ≥2 images has ONE version (ERROR — the rmw_zenoh_cpp case).
  Non-ROS images excluded; shell-less images reported as uninspectable (WARN); a ROS tree with zero
  ros-* dpkg packages WARNs (source-built — dpkg can't see it). Canonical spelling `rig image
  audit`; flat alias `image-audit`.
- **`rig build --no-cache` (remediation)** — exports `RIG_BUILD_NO_CACHE=1` to every build command
  (rig-owned, set-or-popped; scripts opt in with `docker build ${RIG_BUILD_NO_CACHE:+--no-cache}`)
  for the full re-converge after audit finds drift. Env, not a positional arg — the
  `<cmd> <registry> [tag]` contract is untouched.
- rig-infra side — ✅ done (v0.2.21, reference provider): zenoh-router + ros2-bag-logger riggings
  mark `provides: base` (both, so either alone still provides one); `base/build.sh` honors
  RIG_BUILD_NO_CACHE. **The provider build never receives RIG_BASE_IMAGE** — fleet-ros IS the base,
  the root of the FROM chain, and rig pops the var for stage 0; that half of this bullet applies to
  DEPENDENT service build scripts only (the earlier wording said otherwise and was wrong).
- **v0.2.22** closed two order-dependence holes the rig-infra adoption surfaced, plus the audit's
  missing counterpart. Agreeing on the base image NAME is not agreeing on the base: (a) providers
  declaring different `build.platforms` composed `<tag>-<platform>` or not depending on descriptor
  order — both orders reported success, one ref didn't exist; (b) providers naming one image from
  DIFFERENT build scripts survived the stage-0 dedup (keyed on resolved script path), so both ran
  as `[base]` and pushed the same tag — two images racing, last writer wins, the exact skew §13
  exists to prevent, produced by rig itself. Now: platform disagreement is an error in
  `resolve_base_image` (doctor gets it free), and `len(stage0) > 1` after dedup refuses in `build()`
  before anything is built. Also **RIG_ROS_RMW**: vehicle.yaml `ros.rmw` is now build-visible, so
  audit's rmw check has a prevention counterpart instead of flagging an image whose builder was
  never told which rmw the fleet runs. Rig-owned (set-or-popped), deliberately NOT named
  RMW_IMPLEMENTATION — that name is exported in most ROS shells, and a dev box's `.bashrc` must not
  decide what a fleet image contains.
- **v0.2.24** closed the consumer-side hole the camera-service adoption surfaced: `FROM
  ${RIG_BASE_IMAGE}` is necessary but NOT sufficient — a consumer that then plain-`apt-get install`s
  a package the base already carries silently upgrades it to the repo's current candidate (base
  built earlier, ROS apt repo moved in between), re-creating the skew under a new package name.
  Docs now state the precondition and the fix (`apt-get install --no-upgrade`; base packages then
  update only through a base rebuild — deliberate fleet posture). Audit gained two things: the
  version-skew ERROR is base-AWARE (when a skewed ref IS the resolved base, the message diagnoses
  the consumer reinstall and names `--no-upgrade`, instead of generic advice), and the cross-image
  check widened past `ros-*` — non-ROS packages diverging across the ROS images (the `libtiff6`
  case: a transitive dep moved even under `--no-upgrade`) are reported as ONE summarized WARN,
  never an error and never per-package spam. Rationale: ubuntu revision bumps are usually benign,
  but a diverging libstdc++/boost/codec that ROS nodes link against is a real ABI hazard the old
  audit certified as "versions agree"; a rejected alternative was an allowlist of ABI-relevant
  libs (ongoing curation of per-distro package names that goes stale). The probe now captures the
  full dpkg list; ROS-ness still keys on /opt/ros + ros-* only, so plain debian images stay
  excluded. Declined: a certify-level Dockerfile lint for missing `--no-upgrade` (certify executes
  launchers, doesn't parse Dockerfiles; trivially evaded, false confidence both ways — the dynamic
  audit already catches the real thing).

## 14. The msgs overlay: `fleet-ros-msgs` — ✅ implemented (v0.2.28)

Plan doc: `rig-msgs-plan.md` (untracked); frozen provider/consumer contract:
`~/ws/infra/rig-msgs-image-handoff.md` (rig-infra `ed94cbc`). The motivating failure, verified
live: rosbag2 cannot record a topic whose message package isn't installed in the recorder's image
— even though it never deserializes, the generic subscription needs the typesupport `.so` and the
mcap writer needs the `.msg`/`.idl` sources. It logs "unknown type" and KEEPS GOING, so a fleet
with custom types silently gets bags missing them. (REP 2011 plumbing ships in the distro but
rosbag2 doesn't use it, and its dynamic backend is FastRTPS-only.)

The fix is ONE thin image per deployment: `fleet-ros-msgs` = base + the union of the interface
packages the fleet's services declare. rig-infra ships the builder (`msgs/build-msgs.sh`, container-
side validation in `build_msgs.py`, the manifest baked at `/opt/fleet-msgs/manifest.yaml` as
provenance) and the consumer (the logger compose's fallback chain `BAG_LOGGER_IMAGE →
RIG_MSGS_IMAGE → RIG_BASE_IMAGE → composed fleet-ros ref`). rig owns the aggregation — only rig
knows the deployment's resolved service set:

- **`msgs:`** (rigging, top-level — independent of `build:`/`mirror:`; mirror-only services publish
  types too): `apt:` distro-released interface packages (ROS names, underscores — the build maps
  `ros-<distro>-<'_'→'-'>` itself), `source:` pinned from-source repos (repo/ref/packages all
  mandatory; the ref MUST equal the pin the service builds against — a drifted pin is a silent
  schema mismatch in the bag). Strictly validated: unknown keys fail loudly (the `platform:`
  pattern).
- **`build.msgs_overlay: {command, image}`** — the trigger, an optional sub-block on base-provider
  riggings (`provides: base` required: the overlay builds FROM the base, and the provider of the
  base also knows how to overlay it). Chosen over a launcher-less build-only rigging (needs certify
  surgery) and over convention discovery (against rig's grain). Several providers dedupe by script
  identity exactly like the base build.
- **Union + refusals** (`build.msgs_union`/`resolve_msgs_image`): `apt` dedupes; `source` merges by
  repo — same ref unions the package lists, DIFFERENT refs are refused naming the declaring
  services ("align the riggings"), never a manifest-order guess. Providers disagreeing on the
  overlay image or the platform matrix: refused the same way. All refusals land BEFORE anything
  builds. Empty union ⇒ no overlay build, no export — the logger falls back to the bare base,
  which is correct.
- **The build stage**: right after stage 0, sequential — rig renders the union manifest to a temp
  file (`RIG_MSGS_MANIFEST`, rig-owned/set-or-popped like the rest of the build channel) and runs
  `<command> <registry> [tag]` with `RIG_BASE_IMAGE` et al. The tag composes `<tag>-<platform>`
  through the provider's matrix — the overlay inherits the base's. An EXTERNAL `images.base` skips
  the provider's base build but NOT the overlay (an external base still needs one; the build runs
  FROM the external ref). Overlay failure flips rc but doesn't stop dependents — nothing builds
  FROM the overlay.
- **`RIG_MSGS_IMAGE`** — `fleet_env` composes/exports it exactly like `RIG_BASE_IMAGE` (rig-owned,
  set-or-popped, `env:` can't shadow it, certify keeps it unset so the compose's own fallback chain
  is what gets certified). The day this ships, the logger silently upgrades — no logger-side change.
- **Doctor**: OK line naming the composed ref; conflict ERRORs (block `up`); and the preflight the
  whole feature exists for — WARN when services declare `msgs:` but no provider declares
  `msgs_overlay` (the recorder would run the bare base and bags silently miss those topics).
- **The stale-overlay audit — ✅ v0.2.29**: `rig image audit` probes the resolved RIG_MSGS_IMAGE
  (even when no rendered compose pulls it): baked `/opt/fleet-msgs/manifest.yaml` vs the CURRENT
  union (drift = ERROR — declaration changed, build forgotten, `up` pulls the old image under the
  same tag), and every declared `apt` package installed (shared name mapping). Absent/malformed
  baked manifest = WARN, never ERROR.
- **The provenance pin-skew tiers — ✅ v0.2.30**: the deeper check — each `source:` pin against the
  declaring service's own image — consumes the provenance convention rig-infra froze
  (`~/ws/infra/rig-msgs-provenance-handoff.md` ADDENDUM; rig-infra v1.6.0: every participating
  image bakes `/opt/fleet-msgs/provenance.yaml` v1 — repo/ref/rev(+packages, cloned_from), the
  overlay always, services via `provenance-record.sh`). Tiers: absent → WARN (unadopted);
  declared repo missing from a present file → ERROR; ref mismatch → ERROR; both revs real and
  unequal → ERROR even under equal refs (the moved-tag tier only SHAs give); `rev: unknown` or a
  malformed file → WARN unverifiable, never ERROR. Repo join normalized per contract §A3.
- **rig-infra follow-ups** (tracked in `rig-msgs-plan.md`): declare `msgs_overlay` on the
  zenoh-router + ros2-bag-logger riggings, drop the "rig does not export this var yet" caveats
  (logger compose comment, README §Custom-message-types, build-msgs.sh header), one registry
  release carrying the complete feature.

## 15. Graph topology capture — ✅ implemented (v0.2.32 + rig-infra v1.7.0)

Plan doc: `rig-graph-plan.md` (untracked); frozen artifact contract:
`~/ws/infra/rig-graph-capture-handoff.md` (rig-infra `30c8bf1`, released v1.7.0). Bags record
data, not topology — rosbag2 keeps no node identity (ROS 1's `callerid` didn't survive), nothing
about subscribers, no services — so a run couldn't answer WHO talked to WHAT, and a service's
inputs were invisible from its own bag (exactly what §2's replay needs). Static source analysis
was rejected (remaps, parameterized names, lazy subscriptions); the live NodeGraph API is the
instrument, and it works under rmw_zenoh.

Split per the handoff: **rig-infra** ships the capture (a `graph-snapshotter` SIDECAR container in
ros2-bag-logger, compose-profile-gated by the logger config's `graph:` block — not a standalone
service, and not rig-run docker: rig stays ROS-free, and a fleet with no ROS keeps a ROS-free
rig). It appends append-only, change-deduped EPOCH files (`<run>/graph/<name>/epoch_<stamp>.yaml`,
schema 1: per-node pubs/subs/service servers/clients with types, `first`/`last` validity window;
unchanged graph bumps `last:` in place — the liveness signal; any change opens a new file; restart
always opens a new epoch; run pinned at start per record.sh doctrine). **rig** ships the reader
(`rig_cli/graph.py` — pure YAML, no ROS): union + namespace→instance grouping derived at READ time
(no union at seal — one code path serves sealed/unsealed/crashed runs), the `rig graph [run]
[--check] [--contract INSTANCE] [-o FILE]` verb, the rigging **`interface:`** block
(publishes/subscribes/provides/requires; relative = instance-namespace, absolute = shared-bus),
and WARN-only declared-vs-observed checks in both directions (an observed graph is per-config
truth; the declaration is the superset). Contracts bootstrap from observation — `--contract`
PRINTS the scaffold, never auto-edits a vendored rigging. Universal node plumbing (rosout,
parameter/type-description services) is recorded raw but hidden from derived views.

Downstream: §2's replay arc (`rig-replay-plan.md`) consumes the source run's epochs as its topic
selector — with-set observed subscribes minus observed publishes. Queued in the plan: actions as
first-class contract entries, QoS in contracts, `rig graph diff <A> <B>`, `--contract --write`.

## 16. Operational states: standby/active — ✅ implemented (v0.2.35)

Plan doc: `rig-state-plan.md` (untracked); service-side contract + first-adoption feedback:
`~/ws/infra/service-state-adoption-{prompt,feedback-ouster}.md` (reference adoption **ouster
v0.2.0**: lifecycle `inactive` + the sensor's own STANDBY operating mode — motor stopped, laser
off; upstream deactivate alone leaves the sensor spinning). A vehicle had only up and down;
parking — sensors in low-power modes, trigger-style compute (SLAM, planning) idle until a mission
phase — is a per-service verb trio (`standby`/`activate`/`state`, declared all-three-or-none in
rigging `verbs:`; the declaration IS the support claim) that rig commands and observes but never
supervises.

rig-side (this release): declaration-GATED `rig standby`/`rig activate` fan-out (never
`verb_args`' bare-token fallback — that would hand `standby` to a compose-forwarding launcher as
a compose subcommand; undeclared = always-active, skipped) in up/down order — activate
producers-first, standby reversed — with NO rig timeouts (launcher budgets, like `up`). `rig up
--standby|--active` exports RIG_TARGET_STATE for the up dispatch only (rig-owned, popped
everywhere else; launcher precedence RIG_TARGET_STATE > `initial_state` > active, validated at
`up` only). `rig status`: OP column + ADDITIVE `op_state` JSON key (the `state` key stays the
compose rollup) — OP and HEALTH read as a PAIR (transitioning+healthy = wait, e.g. the legitimate
post-activate self-reset; transitioning+unhealthy = stuck); health itself is state-INDEPENDENT
(readiness, never data flow — a healthy standby produces nothing by design). certify: the
`state-verbs` check (trio completeness; `state` answers a down project with exit 0 + one JSON
object in vocabulary, daemon-caveat like `status`) and a poisoned RIG_TARGET_STATE across the
suite; doctor WARNs on a partial trio. Known limitation (ratified): device modes are applied not
persisted — after a vehicle power event a parked deployment re-parks with `rig standby` (repeat =
CONVERGE, by contract). Mission-layer lifecycle transitions are first-class, not drift; rig
records no commanded state.

Queued: `/diagnostics` aggregation as the dashboard-facing status layer (separate arc, rig-infra
sidecar shape); the boilerplate template growing the trio for new services; `state --deep`
(launcher verifies the physical device mode) if power-event drift bites in practice.

## 17. Run capture + reconstruct — ✅ implemented (v0.2.36)

Plan doc: `rig-reconstruct-plan.md` (untracked; the design settled across five rounds on
2026-08-31 — the final shape is the operator's). Problem: a run dir held the data and the
effective configs but not the machinery, so replaying an archived run required the deployment.
Rejected along the way, deliberately: bake-at-SEAL (seal stays minimal; a seal-time tree may
contain edits that never ran), a shared/deduped artifact store (pointers outside the run dir
break self-containment; per-run config changes defeat dedup anyway), and synthesizing a
deployment from the config snapshot alone (snapshots carry no launch surfaces).

Shipped: at run OPEN, a LEAN bake (`bake.capture_run`, sharing `_stage_tree` with the deploy
bake) lands the tree in `<run>/.rig/artifact.tar.gz` — all rows' surfaces and configs, bundled
rig, no compose-only render, no registry pinning, no image bytes; local-daemon image DIGESTS to
`.rig/images.yaml` (identity-not-bytes; the platform boundary is §2's footprint axis, still
open). Fail-soft, `run_capture:` opt-out. `rig reconstruct` extracts anywhere with sha + snapshot
content-address verification, config-snapshot overlay, and tree-local localization;
`rig run retrofit` stamps pre-capture runs with the deploy artifact their manifests name (the
`retrofitted:` marker flips the overlay default to the run's last `ups:` snapshot — retrofit
tarballs are as-shipped). Reproduction (reconstructed tree) vs SIL (current tree) stays an
explicit distinction; replay's config-drift report is the faithfulness proof.
