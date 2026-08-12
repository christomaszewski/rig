# rig — vehicle/machine-level sensor-stack orchestrator

`rig` brings up and manages every sensor + autonomy stack on a single robot/vehicle computer (an NVIDIA
Jetson), driven by config. It is **"a loop + a manifest"**: it reads a vehicle manifest and *delegates*
the bring-up of each sensor to that service's own per-sensor launcher (`<service>-up`). It never
reimplements per-stack logic.

The dependency is strictly one-way: `rig` depends on the service repos; **a service never knows about
`rig`**. `rig` learns each service only through its `rigging.yaml` descriptor + the launcher CLI, so
services evolve independently and new ones drop in by adding two files (a launcher + a `rigging.yaml` —
`rig rigify` generates both onto existing software).

```
                 vehicle.yaml (which sensors)          services.yaml (where each repo is)
                        │                                       │
                        └──────────────► rig ◄──────────────────┘
                                          │  per sensor: <launcher> <config> <verb>
              ┌───────────────────────────┼───────────────────────────┐
              ▼                            ▼                           ▼
          cam-up                      novatel-up                    sbg-up        (each repo's launcher)
   docker compose (camera)     docker compose (GNSS/INS)     docker compose (INS) ...one project per sensor
```

## What rig owns vs. what the launcher owns

- **Launcher (`<service>-up`)** owns everything per-stack: parse the config, derive the ROS namespace
  (`/<name>`), render driver params, select/parameterize its static compose, wire devices/network, run
  `docker compose`. It **honors** the rig-provided `COMPOSE_PROJECT_NAME` (never passes `-p`), falling
  back to its own `<service>_<name>` only when run standalone.
- **rig** owns the cross-cutting concerns: which sensors run (the manifest), **globally-unique instance
  names**, the compose project name (`<name>-vehicle-<vehicle_id>`), bring-up order (producers→consumers),
  fleet-wide ROS env (`ROS_DOMAIN_ID`/`RMW_IMPLEMENTATION`),
  status/health aggregation, and lifecycle/cleanup (external-volume GC on final teardown).

## Quick start

```bash
# host needs Python 3 + PyYAML and Docker (compose v2). For local dev:
python3 -m venv .venv && .venv/bin/pip install pyyaml

# authoring — build a deployment (see docs/CHEATSHEET.md for the full flow)
./rig init my-vehicle --infra zenoh-router --discover   # scaffold: wired infra + a discovered menu
./rig add ../novatel      # wire ONE more service into an existing deployment (path or bare name)
./rig fetch               # hand-wrote vehicle.yaml rows? materialize their configs from examples
./rig rigify ../my-sw     # make EXISTING software rig-compatible (descriptor + launcher + example)

# lifecycle — run the vehicle
./rig doctor              # read-only preflight (unique names, one ROS distro, launchers present, ...)
./rig doctor --deep       # + certify each service's launcher against the contract (runs `config`)
./rig certify             # launcher-contract conformance under a poisoned fleet env (see below)
./rig up --dry-run        # print the exact launcher invocations + fleet ROS env, run nothing
./rig up                  # bring everything up: infra → sensors → autonomy (order within a tier)
./rig pull                # pre-pull every stack's images, NO container changes (prime a cache, then run offline)
./rig status             # one rolled-up row per sensor (state + health from compose ps)
./rig status -v           # expand per-container detail
./rig logs cam_front -f   # follow one sensor's logs
./rig config gnss_primary # render a sensor's merged compose (delegates to the launcher's `config`)
./rig down                # tear down in reverse (autonomy FIRST); --purge also GCs external volumes
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
rig bake --tag v1                           # -> var/artifacts/v1.tar.gz (auto-vendored + digest-pinned)
scp var/artifacts/v1.tar.gz vehicle:~/      # on the vehicle: `tar xzf v1.tar.gz && cd v1 && ./run.sh up`
```

The artifact bundles the resolved configs + vendored surfaces + rig + a **compose-only** form that runs on
just Docker (graceful fallback when Python/PyYAML are absent). `--bundle-images` additionally docker-saves
the image set into the artifact (multi-GB) for **zero-registry deploys** — `up.sh` self-loads on first run.
Re-baking *inside an extracted artifact* (on the vehicle, after field edits) records the parent artifact in
`metadata.yaml`, so save-points form a lineage. Full offline / local-registry flow: `docs/HOST_SETUP.md`.

## Certify a launcher (the contract, executable)

`doctor` checks the *vehicle* (manifest composition); **`certify` checks a *service*** — it runs the
launcher's `config` verb under poison env values (`certify.invalid:5000`, `certify-tag-x`, instance
`certifyname0`) and asserts the contract held: project name honored (no `-p`), images pulled from
`RIG_IMAGE_REGISTRY`, built images pulled as `:RIG_IMAGE_TAG` (build/pull agreement), fleet ROS env
reaching containers unmangled, deterministic output, identity fully derived from the config `name`, clean
stdout, parseable `status`. Run it in a deployment (`rig certify [name…]`, or `rig doctor --deep`) or in a
service repo's CI with no deployment at all:

```bash
rig certify --repo . --config core-driver/config/usb-real.yaml          # the service's own CI gate
rig certify cam_front --emit /tmp/mac.yaml                              # then the same on the vehicle...
rig certify --diff /tmp/mac.yaml /tmp/orin.yaml   # identical = `config` output is host-independent, so a
                                                  # dev-box bake is provably correct for the target
```

## Layout

```
vehicle.yaml            # which stacks THIS machine runs + fleet-wide ROS settings
services.yaml           # catalog: service routing key -> where its repo lives
config/sensors/*.yaml   # one config per sensor (the single source of truth for that stack)
config/infra/*.yaml     # one config per shared infra service (zenoh router, bag logger, …)
config/autonomy/*.yaml  # one config per autonomy stack (planner, SLAM, perception, …)
services/               # VENDORED launch surfaces (`rig vendor`; bake auto-vendors) — deployment mode;
                        #   for dev, point services.yaml at sibling checkouts instead. Never source/submodules.
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
  declaration, and certify enforces the composes pull the same tag.

Each is just a launcher + compose around a stock tool — the same contract any service meets; rig-infra's
CI runs `rig certify` against every one.

## The contract: `rigging.yaml`

A repo is rig-compatible when its launcher exposes `up/down/status/logs/config` on one config, accepts a
config at any host path, honors fleet ROS env, observes **stdout/stderr discipline** (machine output on
stdout, human lines on stderr), and ships a `rigging.yaml` (the legacy name `deploy.yaml` is still accepted).
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
# mirror: [eclipse/zenoh:1.2.1]              # third-party images `rig build` copies into the registry
external_volumes: ["novatel_{name}_data"]    # optional: GC'd by `rig down --purge` (final teardown only)
host_ports: ["plugins[name=webrtc-bridge,enabled=true].params.port"]  # optional: rig validates these don't clash
```

`cam-up`, `novatel-up`, and `sbg-up` all satisfy this. See `docs/DESIGN.md` for the full rationale.
