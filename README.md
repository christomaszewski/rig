# rig — vehicle-level stack orchestrator (infra · sensors · autonomy)

`rig` brings up and manages every stack on a single robot/vehicle computer (an NVIDIA Jetson) —
shared **infra** (zenoh router, bag loggers, dashboard), **sensors** (cameras, GNSS/INS, lidar), and
**autonomy** (planners, SLAM, perception) — driven by config. It is **"a loop + a manifest"**: it
reads a vehicle manifest and *delegates* the bring-up of each stack to that service's own launcher
(`<service>-up`), in tier order. It never reimplements per-stack logic. Stacks come from sibling
checkouts or from **package registries** (pinned services, profiles, overlays, suites, and — since
v0.2.17 — whole-vehicle plans), and a deployment bakes into a tagged artifact that runs on bare Docker.

The dependency is strictly one-way: `rig` depends on the service repos; **a service never knows about
`rig`**. `rig` learns each service only through its `rigging.yaml` descriptor + the launcher CLI, so
services evolve independently and new ones drop in by adding two files (a launcher + a `rigging.yaml` —
`rig rigify` generates both onto existing software).

```
   registries (public / internal)                vehicle.yaml                 services.yaml
   services · profiles · overlays ·            which stacks, in 3 tiers;     where each repo lives
   suites · vehicle plans (pinned)             identity, platform, ROS env   (checkout or vendored)
                │  rig pkg add / upgrade               │                            │
                └──────────────────────────────────────► rig ◄──────────────────────┘
                                                         │   per stack: <launcher> <config> <verb>
                                                         │   + fleet env (ROS_DOMAIN_ID, RMW, VEHICLE_ID,
                                                         │     RIG_IMAGE_REGISTRY/TAG, RIG_TARGET_PLATFORM, RIG_DATA_DIR)
        ┌────────────────────────────────────────────────┼────────────────────────────────────────────────┐
        ▼ infra (up FIRST, down last)                    ▼ sensors (producers)                            ▼ autonomy (up LAST, down FIRST)
   zenoh-router-up · dash-up · bag-logger-up        cam-up · novatel-up · sbg-up · lidar-up          planner-up · slam-up · perception-up
   docker compose — one project per instance        docker compose — one project per instance        docker compose — one project per instance
                                                         │
                                 rig bake ──► tagged artifact (configs + vendored launchers + compose-only
                                              scripts + rig) ──► ./run.sh up on the vehicle, bare Docker
```

Each launcher is the service's own (`cam-up` is the exemplar; `rig rigify` scaffolds one onto existing
software); rig only sequences the tiers, exports the fleet env, and aggregates status.

## What rig owns vs. what the launcher owns

- **Launcher (`<service>-up`)** owns everything per-stack: parse the config, derive the ROS namespace
  (`/<name>`), render driver params, select/parameterize its static compose, wire devices/network, run
  `docker compose`. It **honors** the rig-provided `COMPOSE_PROJECT_NAME` (never passes `-p`), falling
  back to its own `<service>_<name>` only when run standalone.
- **rig** owns the cross-cutting concerns: which stacks run (the manifest, in three tiers),
  **globally-unique instance names**, the compose project name (`<name>-vehicle-<vehicle_id>`),
  bring-up order (infra → sensors → autonomy; reversed on the way down, so the decider dies before
  its eyes), the fleet env (`ROS_DOMAIN_ID`/`RMW_IMPLEMENTATION`, `VEHICLE_ID`, `RIG_IMAGE_REGISTRY`/
  `RIG_IMAGE_TAG`, `RIG_TARGET_PLATFORM` + each service's declared platform override env,
  `RIG_DATA_DIR`), per-host **identity** and **platform** (vehicle.yaml + the machine tier written by
  `rig provision`), run directories (one session = one folder), status/health aggregation, and
  lifecycle: external-volume GC on final teardown (`down --purge`) and full decommission
  (`rig cleanup` — images + volumes off the host, never containers, never data).

## Install

- **Ubuntu/Debian** (incl. Jetson, works offline): download `rig_<v>_all.deb` from the
  [latest release](https://github.com/christomaszewski/rig/releases/latest) →
  `sudo dpkg -i rig_<v>_all.deb` (pulls in `python3` + `python3-yaml`; nothing else)
- **macOS**: `brew install christomaszewski/rig/rig-cli` (formula `rig-cli`, binary `rig`; the tap tracks releases automatically)
- **anywhere with pipx/uv**: `pipx install git+https://github.com/christomaszewski/rig`
- **from a checkout**: `./rig …` works as-is; `rig setup --shell` puts it on PATH

Then, per user: `rig setup` (creates `~/.rig` + subscribes the default `public` registry). On a
**vehicle**, also provision its identity once: `sudo rig provision --id 7 --name skiff-07`
(writes `/etc/rig/vehicle.local.yaml` — every fleet artifact on the machine reads it).

**TAB completion** (verbs, instance names, package refs, registries): deb and brew installs ship
it for bash + zsh — new shells just have it. pipx/checkout installs: `rig setup --shell` wires it
into your rc (or eval `rig completion bash|zsh` yourself).

Upgrades ride the package manager (`apt`/`brew upgrade`) — rig never self-updates. Uninstall:
`rig setup --purge` (user state), then the package manager (the package never touches `~/.rig`).
Releases are cut by tag: every `vX.Y.Z` tag publishes the deb/wheel/sdist and bumps the Homebrew
formula automatically.

## Quick start

```bash
# host needs Python 3 + PyYAML and Docker (compose v2). For local dev:
python3 -m venv .venv && .venv/bin/pip install pyyaml

# authoring — build a deployment (see docs/CHEATSHEET.md for the full flow)
./rig init my-vehicle --vehicle-id 1 --infra zenoh-router --discover   # scaffold: wired infra + a menu
                          #   (--vehicle-id pins a single-vehicle identity; without it the tree
                          #   carries per-host MARKERS, supplied by `rig provision`/RIG_VEHICLE_ID)
./rig add ../novatel      # wire ONE more service into an existing deployment (path or bare name)
./rig fetch               # hand-wrote vehicle.yaml rows? materialize their configs from examples
./rig rigify ../my-sw     # make EXISTING software rig-compatible (descriptor + launcher + example)

# the package registry — no checkouts needed (CHEATSHEET §1.5; public seed registry on GitHub)
./rig setup               # once per machine: ~/.rig + the default `public` registry
./rig registry sync       # then fully offline: pinned installs, vendored surfaces, rig.lock
./rig add public/zenoh-router          # infra from the registry, at an exact pin
./rig add sensor:zr30                  # profile match -> service + editable config, hash-anchored
./rig config diff         # git-status for configs
./rig pkg save zr30       # publish the edits into the package they CAME FROM (render identical);
                          #   `pkg promote` is for NEW packages/forks; `pkg list` = the inventory
./rig pkg outdated        # dependency drift across the registries (repair: pkg repin / rebase)

# fleet vehicles — one artifact, N vehicles (CHEATSHEET §1.6)
# reference per-vehicle values as {{vehicle_id}} etc. in configs; `rig bake` auto-detects the
# templates and defers rendering to the vehicle, which supplies identity via `rig provision`

# lifecycle — run the vehicle
./rig doctor              # read-only preflight (unique names, one ROS distro, launchers present, ...)
./rig doctor --deep       # + certify each service's launcher against the contract (runs `config`)
./rig certify             # launcher-contract conformance under a poisoned fleet env (see below)
./rig up --dry-run        # print the exact launcher invocations + fleet ROS env, run nothing
./rig up                  # bring everything up: infra → sensors → autonomy (order within a tier)
./rig pull                # pre-pull every stack's images, NO container changes (prime a cache, then run offline)
./rig status             # one rolled-up row per sensor (state + health from compose ps; OP =
                          #   observed operational state for services declaring the state verbs —
                          #   read OP and HEALTH as a pair)
./rig status -v           # expand per-container detail
./rig standby             # park declared stacks (reverse order): ready but quiet — lifecycle idle,
                          #   devices low-power (ouster: motor stopped, laser off); HEALTH unaffected.
                          #   Services without the verbs are always-active, skipped. `up --standby`
                          #   comes up parked (RIG_TARGET_STATE beats each config's initial_state)
./rig activate            # wake declared stacks (producers first; launchers own the budgets —
                          #   device spin-up can take a minute). Trigger-style stacks (SLAM) may
                          #   instead be activated by the mission layer via ROS lifecycle directly
./rig logs cam_front -f   # follow one sensor's logs
./rig config gnss_primary # render a sensor's merged compose (delegates to the launcher's `config`)
./rig graph               # observed pub/sub/service topology from a run's graph epochs (the bag
                          #   logger's `graph:` sidecar, rig-infra ≥ v1.7.0); --check compares vs the
                          #   riggings' declared interface: (WARN-only), --contract <instance>
                          #   scaffolds that block from observation, -o writes the union YAML
./rig run rm <id> / run import <path>  # registry lifecycle: reclaim disk (sealed freely,
                          #   interrupted --force, the OPEN run never) / adopt archived runs so
                          #   id-based verbs + TAB cover them. The registry home is a machine
                          #   fact: `sudo rig provision --data-dir /data/rig` (minted lazily)
./rig reconstruct <run-dir>  # a run dir back into a runnable tree, anywhere: every opened run
                          #   captures the deployment (surfaces + configs + rig, no image bytes)
                          #   into .rig/artifact.tar.gz — extract, verify, overlay a config
                          #   snapshot, localize. Pre-capture runs: `rig run retrofit` stamps them
                          #   with the deploy artifact their manifest names. Opt-out: run_capture
                          #   --registry HOST localizes images.registry too (a bench's mirror;
                          #   machine-wide: `provision --registry`); --enable-replay <path|ref>
                          #   wires the SIL player into a tree that flew without it (opt-in)
./rig replay <run> planner  # SIL: NEW provenance-linked run; the named instances come up LIVE and
                          #   the ros2-bag-player (rig-infra ≥ v1.8.0) plays the topics they
                          #   consumed in <run> (selected from its graph epochs — observed
                          #   subscribes minus publishes; namespace fallback for pre-epoch runs).
                          #   The bag-logger records the new outputs: source bag vs replay bag is
                          #   the A/B pair. Refuses while stacks run; teardown = down --end-run
                          #   --from S / --to S replay a SECTION (seconds from bag start — the
                          #   call-script zero; latches restored; --auto-end composes for sweeps)
./rig down                # tear down in reverse (autonomy FIRST); --purge also GCs external volumes
./rig cleanup             # decommission: remove this deployment's images + volumes from the host
                          #   (after the final down, before deleting the tree; --dry-run to preview)
./rig up cam_front ins_main   # operate on a subset by name
```

`./rig` uses the system `python3`; on a host where PyYAML lives in a venv, run `.venv/bin/python rig …`
(and point each launcher's `*_PYTHON` env at that interpreter), or `apt install python3-yaml` on the robot.

## Deploy to a vehicle

Build images into a registry the vehicle can reach, bake a tagged artifact, ship it, run it — no driver
source or internet on the vehicle. `bake` **auto-vendors** each service's launch surface (no manual `rig
vendor` step) and digest-pins images against the registry from `vehicle.yaml` (`images.registry`) — pass
`--registry <ip:5000>` only to override it:

```bash
rig build                                   # build/push + mirror images into the registry
rig build --no-cache                        # full rebuild: RIG_BUILD_NO_CACHE=1 to every build command
                                            #   (re-converges apt-level drift across images)
rig image audit                             # inspect what the stacks will RUN: one ROS distro, the
                                            #   declared rmw installed, ros-* versions agree across images
rig bake --tag v1                           # -> var/artifacts/v1.tar.gz (auto-vendored + digest-pinned)
scp var/artifacts/v1.tar.gz vehicle:~/      # on the vehicle: `tar xzf v1.tar.gz && cd v1 && ./run.sh up`
```

The artifact bundles the resolved configs + vendored surfaces + rig + a **compose-only** form that runs on
just Docker (graceful fallback when Python/PyYAML are absent). `--bundle-images` additionally docker-saves
the image set into the artifact (multi-GB) for **zero-registry deploys** — `up.sh` self-loads on first run.
Re-baking *inside an extracted artifact* (on the vehicle, after field edits) records the parent artifact in
`metadata.yaml`, so save-points form a lineage. Full offline / local-registry flow: `docs/HOST_SETUP.md`.

**Fleet artifacts** need no flag: if the deployment references `{{var}}` values (e.g.
`rtsp://10.160.{{vehicle_id}}.80/…`), `rig bake` stages the tree *unresolved* and each vehicle
renders it locally from its provisioned identity — one artifact serves the whole fleet
(`sudo ./provision.sh --id 7 --name skiff-07` once per vehicle, then `./run.sh up` forever).
CHEATSHEET §1.6 has the full lifecycle.

## Registries & package kinds

Stacks install from **package registries** (a git repo or shared folder of manifests — the public seed
is [rig-registry-public](https://github.com/christomaszewski/rig-registry-public); `rig registry init`
scaffolds your own, with `tools/validate` + CI wrappers). Five kinds:

| kind | what it is | install shape |
|---|---|---|
| `service` | a code pointer (repo + full-SHA rev) and/or a digest-pinned image | `rig add internal/lidarish` — vendors the launch surface, routes it, instance from its example |
| `profile` | a complete **nameless** config for one service (`requires.service`), keyed `service:short` | `rig add sensor:zr30` / `rig add camera-service:siyi-zr30` — the instance's editable working config, base pinned + hash-anchored |
| `overlay` | a versioned **delta** on top of a profile-based instance (ordered bindings, last wins; local still beats overlays) | `rig overlay apply <instance> <ref>` — bindings only |
| `suite` | references only: members at exact pins (services/profiles/overlays + at most one vehicle plan) | `rig pkg add internal/gideon-boat` — atomic, all-or-rollback |
| `vehicle` | a suite's **instance plan** (v0.2.17): a template vehicle.yaml whose rows carry YOUR instance names, order, enabled, tiers, per-row overlay bindings, fleet defaults (`platform`, `images`, `data_dir`…); per-host identity stays markers | never standalone — captured with its suite (`promote --all --suite S --vehicle V`), installed through it into an empty `rig init` tree |

The capture/reproduce loop: tune a deployment → `rig pkg promote --all --suite <s> --vehicle <plan>
--to internal [--adopt]` (dirty instances become overlays; hand-authored ones become adopted profiles
with `--adopt`) → on a fresh tree `rig pkg add internal/<s>` gives the vehicle back, identity supplied
per host. Exact pins are **snapshots** (v0.2.19): a member's later release never breaks the suites or
profiles that pin it — `registry validate` warns, installs serve the pinned version from the
registry's git history, `rig pkg outdated` reports drift (exit 1, CI-able) and `rig pkg repin`
refreshes. CHEATSHEET §1.5 is the daily loop; `docs/DESIGN.md` has the decision log.

## Certify a launcher (the contract, executable)

`doctor` checks the *vehicle* (manifest composition); **`certify` checks a *service*** — it runs the
launcher's `config` verb under poison env values (`certify.invalid:5000`, `certify-tag-x`, instance
`certifyname0`) and asserts the contract held: project name honored (no `-p`), images pulled from
`RIG_IMAGE_REGISTRY`, built images pulled as `:RIG_IMAGE_TAG` (build/pull agreement; composed
`<tag>-<platform>` for services declaring a `build.platforms` matrix), the declared platform honored
(`RIG_TARGET_PLATFORM`/override env — every matrix entry must render on any host, never host-probe),
fleet ROS env reaching containers unmangled, deterministic output, identity fully derived from the config
`name`, clean stdout, parseable `status`. Run it in a deployment (`rig certify [name…]`, or
`rig doctor --deep`) or in a service repo's CI with no deployment at all:

```bash
rig certify --repo . --config core-driver/config/usb-real.yaml          # the service's own CI gate
rig certify cam_front --emit /tmp/mac.yaml                              # then the same on the vehicle...
rig certify --diff /tmp/mac.yaml /tmp/orin.yaml   # identical = `config` output is host-independent, so a
                                                  # dev-box bake is provably correct for the target
```

## Layout

```
vehicle.yaml            # which stacks THIS machine runs + fleet-wide ROS settings (+ vars:/env:
                        #   fleet defaults; {{var}} markers = supplied per vehicle)
vehicle.local.yaml      # OPTIONAL, gitignored, never baked: THIS machine's identity/values for
                        #   bench trees; vehicles use /etc/rig/vehicle.local.yaml (rig provision)
services.yaml           # catalog: service routing key -> where its repo lives
config/sensors/*.yaml   # one config per sensor (the single source of truth for that stack)
config/infra/*.yaml     # one config per shared infra service (zenoh router, bag logger, …)
config/autonomy/*.yaml  # one config per autonomy stack (planner, SLAM, perception, …)
services/               # VENDORED launch surfaces (`rig vendor`; bake auto-vendors; `rig add
                        #   public/<svc>` vendors at the registry pin) — for dev, point services.yaml
                        #   at sibling checkouts instead. Never source/submodules.
config/.pins/           # GENERATED pristine base copies (registry installs) — what `config diff`,
                        #   `pkg upgrade`, and `pkg promote` measure against. Commit them.
config/.overlays/       # GENERATED bound-overlay payload copies (deployment stays self-contained)
rig.lock                # GENERATED pins: registries@commit, packages+hashes, instance anchors,
                        #   image digests (bake). Commit it; `pkg install --locked` reproduces.
rig, rig_cli/           # the CLI (thin shim + package: manifest/catalog/dispatch/status/doctor/certify/
                        #   build/bake/init/rigify/runs/…)
docs/                   # CHEATSHEET (1-page flow) · RUNBOOK (worked example) · DESIGN/ROADMAP · STATE · HOST_SETUP
```

### `vehicle.yaml` (per machine)
Lists active stacks (`name`, `service`, `config`, `enabled`, `order`) and the fleet ROS settings, in three
tiers: `infra:` (substrate — routers, loggers, dashboards; up FIRST, down last), `sensors:` (producers),
and `autonomy:` (graph consumers — planners, SLAM, perception; up after ALL sensors, down FIRST, so the
decider dies before its eyes). The tier partition is hard; per-entry `order` sorts within a tier only —
and ordering is a courtesy, not correctness: consumers must still retry (discovery is dynamic). Disable
a stack with `enabled: false` rather than deleting its config. `name` must be unique across the vehicle —
it keys the compose project, external volumes, and ROS namespace.

Per-vehicle values flow through `{{var}}` markers (never `${VAR}` — that's compose's): configs and
manifest scalars may reference `{{vehicle_id}}`, `vars:` entries, etc., resolved at render from
shell (`RIG_VAR_*`) > `vehicle.local.yaml` > `/etc/rig/vehicle.local.yaml` > `vehicle.yaml`
defaults. A self-marker (`vehicle_id: "{{vehicle_id}}"`) makes the value MANDATORY per vehicle —
and since v0.2.20 `rig init` scaffolds `vehicle`/`vehicle_id` as markers by default (nothing comes
up as "vehicle 1" by accident; `--vehicle-id N` pins a single-vehicle literal). `platform:` (e.g.
`jp7`) declares the host's hardware/OS target → `RIG_TARGET_PLATFORM`; services with a build matrix
pull `<image>:<tag>-<platform>` and `images.tag` means VERSION only. Both identity and platform are
per-host facts the machine tier (`sudo rig provision --id 7 --name skiff-07 --platform jp7`) may
override.
An `env:` map exports extra (interpolated) variables to every launcher. One mapping form,
`{{map <list_var> <template_var>}}`, builds lists (e.g. zenoh peer endpoints from
`{{fleet_peer_ids}}` — the fleet minus THIS vehicle, derived). CHEATSHEET §1.6.

### `services.yaml` (catalog)
Maps each `service` routing key to its repo `path` (resolved relative to this repo). The key may differ
from the repo dir name (service `sbg` → repo `sbg_driver`).

### `config/sensors/<name>.yaml`
Thin ROS 2 drivers share a generic schema — `service`, `name`, `connection` (`tcp`/`udp`/`serial`/`file`),
`ros.namespace`, and an **opaque** `driver_params` block the launcher renders into the driver's ROS 2
params. The rich `camera-service` camera uses its own service-specific schema (rig hands it to `cam-up` as-is).

A config can instead be a **nameless profile** reused across instances via a per-sensor `overrides:` patch
in `vehicle.yaml` — rig deep-merges the patch, stamps in `name`, and renders the result to
`var/rendered/<name>.yaml` before handing it to the launcher (a complete named config with no overrides is
passed through untouched). See `config/sensors/camera.profile.yaml` and `docs/ROADMAP.md` §1.

## Shared infra services ([rig-infra](https://github.com/christomaszewski/rig-infra))

Ready-to-use shared (`infra:`) services live in their own repo — clone it beside your deployment,
point `services.yaml` at a service dir (`../rig-infra/zenoh-router`) and add an `infra:` entry, or let
`rig init --infra zenoh-router` (bare name — the workspace scan finds it) wire it for you; in an
EXISTING deployment, `rig add <name|path>` does the same wiring after the fact (infra arrives enabled,
sensor/autonomy services get a commented menu row to uncomment):

- **`zenoh-router/`** — the vehicle's shared `rmw_zenoh` router (order 0). Default: the `fleet-ros`
  base image running `rmw_zenohd`, so the router and the ROS sessions share one distro's zenoh packages
  by construction. Optional inline `router_config:` renders to a mounted `zenohd.json5`.
- **`ros2-bag-logger/`** / **`ros1-bag-logger/`** — record the ROS telemetry graph to
  `${RIG_DATA_DIR}/current/bags/<name>` (the open run — ROADMAP §3c; flat `bags/<name>` without a registry). Config: `record.mode: all|allow|exclude` (+ `exclude_images`, default
  true — image streams are huge over ROS and already recorded compressed at the camera source) and an
  `output` block (storage/compression/split). Place at order ~1 so it records from startup. The ros2
  logger defaults to `fleet-ros` (rosbag2 + mcap + rmw_zenoh, ~1 GB — no camera image on camera-less
  vehicles).
- **`base/`** — the `fleet-ros` image; `rig build` builds + pushes it via the riggings' `build:`
  declaration, and certify enforces the composes pull the same tag. The zenoh-router and
  ros2-bag-logger riggings mark it `provides: base`, making it the DEPLOYMENT's base image: `rig build` builds it first and exports it
  to every other build (and to launchers) as `RIG_BASE_IMAGE`, so one image pins the fleet's
  distro+rmw packages; `vehicle.yaml images.base` (or `rig build --base-image`) overrides it with an
  external ref.
- **`msgs/`** — the `fleet-ros-msgs` overlay: base + the union of the interface packages the fleet's
  services declare in their riggings' `msgs:` blocks. rosbag2 cannot record a topic whose message
  package isn't installed in the recorder's image (it logs "unknown type" and keeps going), so a
  fleet with custom types silently gets bags missing them. When services declare `msgs:` and a base
  provider declares `build.msgs_overlay`, `rig build` renders the union manifest
  (`RIG_MSGS_MANIFEST`), builds the overlay right after the base (FROM `RIG_BASE_IMAGE` — an
  external `images.base` gets an overlay too), and exports the ref as `RIG_MSGS_IMAGE`; the bag
  logger's compose prefers it over the bare base. No declarations → no overlay → the bare base,
  which is correct.

Each is just a launcher + compose around a stock tool — the same contract any service meets; rig-infra's
CI runs `rig certify` against every one.

## The contract: `rigging.yaml`

A repo is rig-compatible when its launcher exposes `up/down/status/logs/config` on one config, accepts a
config at any host path, honors fleet ROS env, observes **stdout/stderr discipline** (machine output on
stdout, human lines on stderr), and ships a `rigging.yaml` (the legacy name `deploy.yaml` is still accepted).
Optionally it also implements the **operational-state trio** — `standby`/`activate`/`state`, declared in
`verbs:` all-three-or-none (the declaration is the support claim; contract + reference adoption: the
service-state adoption prompt in `~/ws/infra`, ouster ≥ v0.2.0) — and honors `RIG_TARGET_STATE` at `up`.
**Start from `rig rigify <dir>`**: it generates the descriptor + a contract-correct launcher skeleton +
an example config in an existing software dir, pre-wired from a read-only analysis (found composes get
`-f`-wired; ports/volumes/images/Dockerfiles become commented `host_ports`/`external_volumes`/`mirror`/
`build:` hints). The onboarding arc is `rig rigify` → `rig certify --repo` (until green) → `rig add`:

```yaml
service: novatel
launcher: novatel-up                 # default: <service>-up
verbs: { status: ps }                # adapt logical verbs -> launcher args (defaults shown in descriptor.py)
ros_distro: lyrical
tier: sensor                         # optional: "infra" = shared, up-first (routers, loggers, dashboards);
                                     #   "autonomy" = graph consumer, up-last / down-first (planners, SLAM)
examples: [sensors/novatel.example.yaml]     # optional: example configs — init/add/fetch copy them;
                                             #   `rig certify --repo` uses the first as its default --config
launch_surface:                              # the minimal file set `rig vendor`/`bake` copy to LAUNCH
  - novatel-up                               #   this service (never its source)
  - docker/compose/compose.deploy.yaml
# build: { command: tools/build-images.sh, images: [novatel] }   # `rig build` runs `<cmd> <registry> [tag]`
#                                            #   (ROS_DISTRO exported from vehicle.yaml ros.distro)
# build: { command: ..., images: [...], platforms: [jp7, jp6] }   # a build MATRIX: distinct image sets per
#                                            #   hardware/OS target — rig composes pulls as <tag>-<platform>
# build: { command: ../base/build.sh, images: [fleet-ros], provides: base }  # this build produces the
#                                            #   deployment's BASE image: rig builds it FIRST and exports
#                                            #   <registry>/<images[0]>:<tag> as RIG_BASE_IMAGE to every
#                                            #   other build + launcher (vehicle.yaml images.base overrides).
#                                            #   Providers of one base must agree on build.platforms AND
#                                            #   the build script — rig refuses rather than pick by order.
# build: { command: ../base/build.sh, images: [fleet-ros], provides: base,
#          msgs_overlay: { command: ../msgs/build-msgs.sh, image: fleet-ros-msgs } }  # base providers may
#                                            #   also declare the msgs OVERLAY build: base + the union of
#                                            #   the riggings' `msgs:` blocks, exported as RIG_MSGS_IMAGE
#                                            #   (the bag logger prefers it over the bare base)
# msgs:                                      # interface packages THIS service's topics use (beyond
#   apt: [mavros_msgs]                       #   ros-base/common_interfaces) — rosbag2 can't record a topic
#   source:                                  #   whose message package isn't installed in the recorder's
#     - repo: https://github.com/PX4/px4_msgs.git   # image. ROS names (underscores) in apt:; source pins
#       ref: v1.16.0                         #   are MANDATORY and must equal the pin the service builds
#       packages: [px4_msgs]                 #   against. rig unions the blocks fleet-wide; one repo at two
#                                            #   refs is refused ("align the riggings"), never guessed.
# replay: { sim_time: true,                 # the launcher wires use_sim_time from RIG_SIM_TIME (rig's
#                                            #   one clock token under `rig replay`; explicit config wins
#                                            #   both ways). Undeclared services under test WARN at replay
#           service_introspection: true }    # + CONTENTS-level service introspection on its servers/
#                                            #   clients: calls RECORD into bags (record.services) and
#                                            #   REPLAY at this service (RIG_REPLAY_SERVICES / --calls)
# interface:                                 # the service's declared topic/service contract — checked
#   publishes:                               #   WARN-only against a run's OBSERVED graph epochs
#     - {topic: fix, type: sensor_msgs/msg/NavSatFix}   # relative = instance-namespace; absolute
#     - /tf                                  #   (/tf) = shared-bus; bare string = name only. Bootstrap
#   subscribes: [{topic: rtcm}]              #   from observation: `rig graph --contract <instance>`
#   provides: [{service: reset, type: std_srvs/srv/Trigger}]   # service servers
#   requires: []                             #   service clients (what this service NEEDS)
# platform: { auto_detect: /etc/nv_tegra_release, override_env: CAM_PLATFORM }  # the launcher's standalone
#                                            #   host probe + the env var it honors; rig mirrors the vehicle's
#                                            #   declared `platform:` into it (RIG_TARGET_PLATFORM sibling)
# mirror: [eclipse/zenoh:1.2.1]              # third-party images `rig build` copies into the registry
external_volumes: ["novatel_{name}_data"]    # optional: GC'd by `rig down --purge` (final teardown only)
host_ports: ["plugins[name=webrtc-bridge,enabled=true].params.port"]  # optional: rig validates these don't clash
```

`cam-up`, `novatel-up`, and `sbg-up` all satisfy this. See `docs/DESIGN.md` for the full rationale.
