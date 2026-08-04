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
- **Fleet image tag**: `images.tag` in vehicle.yaml (e.g. the Orin's JetPack `jp7`) → `RIG_IMAGE_TAG`;
  platform-specific composes pull `<repo>:<tag>`, and `rig build` defaults its `--tag` to it. Platform-agnostic
  services (dashboard) just ignore it.
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
- `end-run [--force]` **seals**: stamp `ended:` + snapshot (`rig status` output, disk usage) into the
  manifest, remove `current`. Guarded while running; idempotent (no open run → warn, rc 0).
- `up --run <label>` names at entry: label matches the open run → join (idempotent, never mints `X_2`);
  else rotate-then-up (same guard). `down --end-run` seals after a successful full down — a partial or
  failed down leaves stacks running, so the guard refuses: you cannot seal out from under live writers.
- `runs` lists the registry (STATE: OPEN = current+no `ended:`; sealed = `ended:`; interrupted = neither —
  surfaced, not hidden). `status` shows `run: <label> (open …)` / `no active run` in its header.

### Manifest = the machine contract
`runs/<id>/manifest.yaml`: run id, label, vehicle, `vehicle_id`, rig version, artifact tag (when running
in an extracted artifact — bake's provenance chain plugs in), `started:`, enabled stacks; `end-run` adds
`ended:` + the snapshot. **`ended:` present ⇔ sealed ⇔ safe for sync tooling** — scripts read manifests,
never parse the human table.
Power loss mid-session: the symlink survives reboot; the run stays open and correctly continues.

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

## 3e. Infra spin-out: `rig-infra` repo — ⚙ steps 1–2 done (v0.1.28); deployment migration + stub deletion remain

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
   paths fail loudly with a pointer, not a mystery), then delete. rig CI drops the live-template certify
   steps (infra CI owns them); the sh-fixture certify tests remain rig's contract reference.
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
