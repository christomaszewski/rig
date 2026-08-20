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
  suites refresh every member (registry law: in-registry members sit at head).
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
