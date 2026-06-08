# rig — vehicle/machine-level sensor-stack orchestrator

`rig` brings up and manages every sensor + autonomy stack on a single robot/vehicle computer (an NVIDIA
Jetson), driven by config. It is **"a loop + a manifest"**: it reads a vehicle manifest and *delegates*
the bring-up of each sensor to that service's own per-sensor launcher (`<service>-up`). It never
reimplements per-stack logic.

The dependency is strictly one-way: `rig` depends on the service repos; **a service never knows about
`rig`**. `rig` learns each service only through its `rigging.yaml` descriptor + the launcher CLI, so
services evolve independently and new ones drop in by adding two files (a launcher + a `rigging.yaml`).

```
                 vehicle.yaml (which sensors)          services.yaml (where each repo is)
                        │                                       │
                        └──────────────► rig ◄──────────────────┘
                                          │  per sensor: <launcher> <config> <verb>
              ┌───────────────────────────┼───────────────────────────┐
              ▼                            ▼                           ▼
          gige-up                     novatel-up                    sbg-up        (each repo's launcher)
   docker compose (camera)     docker compose (GNSS/INS)     docker compose (INS) ...one project per sensor
```

## What rig owns vs. what the launcher owns

- **Launcher (`<service>-up`)** owns everything per-stack: parse the config, derive instance identity
  (compose project `<service>_<name>`, ROS namespace `/<name>`), render driver params, select/parameterize
  its static compose, wire devices/network, run `docker compose`.
- **rig** owns the cross-cutting concerns: which sensors run (the manifest), **globally-unique instance
  names**, bring-up order (producers→consumers), fleet-wide ROS env (`ROS_DOMAIN_ID`/`RMW_IMPLEMENTATION`),
  status/health aggregation, and lifecycle/cleanup (external-volume GC on final teardown).

## Quick start

```bash
# host needs Python 3 + PyYAML and Docker (compose v2). For local dev:
python3 -m venv .venv && .venv/bin/pip install pyyaml

./rig doctor              # read-only preflight (unique names, one ROS distro, launchers present, ...)
./rig up --dry-run        # print the exact launcher invocations + fleet ROS env, run nothing
./rig up                  # bring all enabled sensors up (ascending order)
./rig status             # one rolled-up row per sensor (state + health from compose ps)
./rig status -v           # expand per-container detail
./rig logs cam_front -f   # follow one sensor's logs
./rig config gnss_primary # render a sensor's merged compose (delegates to the launcher's `config`)
./rig down                # tear down (reverse order); --purge also GCs declared external volumes
./rig up cam_front ins_main   # operate on a subset by name
```

`./rig` uses the system `python3`; on a host where PyYAML lives in a venv, run `.venv/bin/python rig …`
(and point each launcher's `*_PYTHON` env at that interpreter), or `apt install python3-yaml` on the robot.

## Deploy to a vehicle

Vendor each service's launch surface, bake a tagged artifact (digest-pinned to a registry the vehicle can
reach), ship it, and unbake — no driver source or internet on the vehicle:

```bash
rig vendor novatel --from ../novatel        # copy launch surfaces into services/ (text only, no source)
rig bake --registry devbox:5000 --tag v1    # -> var/artifacts/v1.tar.gz (resolved + digest-pinned)
scp var/artifacts/v1.tar.gz orin:/tmp/       # on the Orin: `rig unbake … && ./run.sh up`
```

The artifact bundles the resolved configs + vendored surfaces + rig + a **compose-only** form that runs on
just Docker (graceful fallback when Python/PyYAML are absent). Full offline / local-registry flow:
`docs/HOST_SETUP.md`.

## Layout

```
vehicle.yaml            # which sensors THIS machine runs + fleet-wide ROS settings
services.yaml           # catalog: service routing key -> where its repo lives
config/sensors/*.yaml   # one config per sensor (the single source of truth for that stack)
services/               # service repos as git submodules (deployment); or point services.yaml at sibling checkouts
rig, rig_cli/           # the CLI (thin shim + package: manifest/catalog/descriptor/dispatch/status/doctor)
docs/                   # DESIGN.md (decisions), HOST_SETUP.md (udev, provisioning, submodules)
```

### `vehicle.yaml` (per machine)
Lists active sensors (`name`, `service`, `config`, `enabled`, `order`) and the fleet ROS settings. Disable
a sensor with `enabled: false` rather than deleting its config. `name` must be unique across the vehicle —
it keys the compose project, external volumes, and ROS namespace.

### `services.yaml` (catalog)
Maps each `service` routing key to its repo `path` (resolved relative to this repo). The key may differ
from the repo dir name (service `sbg` → repo `sbg_driver`).

### `config/sensors/<name>.yaml`
Thin ROS 2 drivers share a generic schema — `service`, `name`, `connection` (`tcp`/`udp`/`serial`/`file`),
`ros.namespace`, and an **opaque** `driver_params` block the launcher renders into the driver's ROS 2
params. The rich `gige-vision` camera uses its own service-specific schema (rig hands it to `gige-up` as-is).

A config can instead be a **nameless profile** reused across instances via a per-sensor `overrides:` patch
in `vehicle.yaml` — rig deep-merges the patch, stamps in `name`, and renders the result to
`var/rendered/<name>.yaml` before handing it to the launcher (a complete named config with no overrides is
passed through untouched). See `config/sensors/camera.profile.yaml` and `docs/ROADMAP.md` §1.

## The contract: `rigging.yaml`

A repo is rig-compatible when its launcher exposes `up/down/status/logs/config` on one config, accepts a
config at any host path, honors fleet ROS env, observes **stdout/stderr discipline** (machine output on
stdout, human lines on stderr), and ships a `rigging.yaml` (the legacy name `deploy.yaml` is still accepted):

```yaml
service: novatel
launcher: novatel-up                 # default: <service>-up
verbs: { status: ps }                # adapt logical verbs -> launcher args (defaults shown in descriptor.py)
ros_distro: lyrical
external_volumes: ["gige_{name}_sock"]   # optional: GC'd by `rig down --purge` (final teardown only)
host_ports: ["plugins[name=webrtc-bridge].params.port"]  # optional: rig validates these don't clash
```

`gige-up`, `novatel-up`, and `sbg-up` all satisfy this. See `docs/DESIGN.md` for the full rationale.
