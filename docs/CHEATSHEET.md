# rig — deployment cheat sheet

> The whole workflow on one page (rig ≥ v0.1.18). Long-form: `RUNBOOK.md` (worked Orin example),
> `README.md` (concepts), `STATE.md` (current live state). Mental model: **services own their bring-up**
> (launcher + `rigging.yaml`); **rig owns the vehicle** (manifest, env, ordering, artifacts). The vehicle
> runs the baked compose-only form — no source, no internet.

```
author configs ──▶ validate ──▶ build images ──▶ bake ──▶ ship ──▶ run ──▶ iterate
                doctor/certify   rig build        rig bake   scp     run.sh
```

## 0 — one-time host setup

```bash
# DEV BOX: a persistent local registry (MUST keep its volume until the vehicle has pulled)
docker run -d --restart always -p 5000:5000 -v registry-data:/var/lib/registry --name registry registry:2
export REGISTRY="<dev-box-LAN-IP>:5000"        # the IP the VEHICLE can reach — always WITH the port
# Docker Desktop → Settings → Docker Engine → "insecure-registries": ["<dev-box-LAN-IP>:5000"] → Restart

# VEHICLE (Jetson): trust the registry — MERGE into /etc/docker/daemon.json (NEVER overwrite: it carries
# the `nvidia` runtime). See RUNBOOK §7 for the python3 merge one-liner. Then: sudo systemctl restart docker
# Optional but recommended: sudo apt install python3-yaml   (enables rig verbs + per-host overlays on-vehicle)
```

## 1 — workspace + deployment scaffold

```bash
mkdir -p ~/ws && cd ~/ws                      # service repos + rig as siblings
git clone <camera-service> <dashboard> <rig>...
alias rig="$PWD/rig/rig"

rig init my-vehicle && cd my-vehicle
# services.yaml — route each service name to its repo:
#   services: { zenoh-router: {path: ../rig/templates/zenoh-router}, camera-service: {path: ../camera-service}, ... }
# vehicle.yaml — identity + fleet env + the stacks:
#   vehicle: my-vehicle        vehicle_id: 7            # -> ROS domain 7, VEHICLE_ID=7
#   ros:    { rmw: rmw_zenoh_cpp, distro: lyrical }     # zenoh rmw ⇒ declare a zenoh-router in infra:
#   images: { registry: "<IP>:5000", tag: "jp7" }       # ONE tag per vehicle (the platform, e.g. JetPack)
#   data_dir: /home/<user>/logs                          # recordings/logs land here (RIG_DATA_DIR)
#   infra:   [ {name: zenoh-router, ...order: 0}, {name: dashboard, ...order: 5} ]
#   sensors: [ {name: cam_usb, ...order: 10}, {name: cam_rtsp, ...order: 20} ]
# config/sensors/<name>.yaml — one per instance (copy keys from the service's example configs)
```

Naming rules: instance `name` is unique vehicle-wide and keys *everything* (compose project, volumes,
ROS namespace). **Underscores, never hyphens** (`cam_usb`, not `cam-usb`). Two instances of one service:
unique names + unique host-facing ports (declare `host_ports` in the service's rigging.yaml → doctor checks).

## 2 — validate (before any docker work)

```bash
rig doctor                 # vehicle composition: names, one distro, port clashes, zenoh guardrail
rig certify                # launcher contract per service (poison env): project-name, registry/tag,
                           #   ros-env, determinism, identity, discipline   [= doctor --deep for both]
rig up --dry-run           # the exact launcher invocations + fleet env, runs nothing
```

`certify` in a service repo's CI (no deployment needed): `rig certify --repo . --config examples/usb.yaml`
Suspect a launcher probes the host? Prove it: `rig certify <name> --emit /tmp/dev.yaml` here, same on the
vehicle, then `rig certify --diff /tmp/dev.yaml /tmp/orin.yaml` — identical = dev-box bake is correct.

## 3 — build + push images

```bash
rig build -j 3                                # per unique service: build+push (build:) / mirror (mirror:)
curl -s http://$REGISTRY/v2/_catalog          # expect every repo the composes will pull
```

Tags: `rig build` tags with `images.tag` (jp7) and certify's tag check guarantees the composes pull the
same — build/pull agreement is enforced, not hoped.

## 4 — bake a deployable artifact

```bash
rig bake --tag v1                             # -> var/artifacts/v1.tar.gz  (sha256 printed)
```

The artifact = resolved configs + complete vehicle.yaml + vendored launch surfaces + rig + a **compose-only**
form (build-stripped; built images digest-pinned, mirrored images by tag). It runs on bare Docker.

## 5 — ship + run on the vehicle

```bash
scp var/artifacts/v1.tar.gz $VEHICLE:~/ws/
ssh $VEHICLE 'cd ~/ws && tar xzf v1.tar.gz'
ssh $VEHICLE 'cd ~/ws/v1 && ./run.sh up'      # pulls from $REGISTRY; infra first, then sensors
ssh $VEHICLE 'cd ~/ws/v1 && ./run.sh status'  # or: ./run.sh logs <name> · ./run.sh down
```

Quick verification: containers up (`docker ps`), dashboard at `http://<vehicle>:8080`, recordings growing
under `data_dir`, camera log shows `health: frames=N, no drops`. After the first pull the vehicle runs
**offline** — the registry is only needed for updates.

## 6 — iterate

| change                  | loop                                                              |
|-------------------------|-------------------------------------------------------------------|
| sensor config only      | edit → `rig bake --tag v2` → scp/extract → `./run.sh up`          |
| service code/images     | `rig build` → `rig bake --tag v2` → ship → `./run.sh up`          |
| field tweak on-vehicle  | edit the extracted tree's config → `./rig up` (re-renders live)   |
| new service             | add launcher+`rigging.yaml` in its repo → `rig certify --repo` until green → add to services.yaml/vehicle.yaml |

Teardown: `./run.sh down` (volumes survive); final removal `rig down --purge`. Dev registry off:
`docker rm -f registry` (keep the `registry-data` volume unless you're truly done).

## Gotchas (each learned the hard way)

- Registry trust is needed on **both** machines, **with the port** — a bare IP doesn't match `IP:5000`.
- **MERGE** the Jetson's `daemon.json` — overwriting drops the `nvidia` runtime the camera needs.
- One `images.tag` per vehicle: platform-agnostic services must still *pull* that tag (certify enforces).
- The registry must keep its volume between `rig build` and the vehicle's first pull (digests die with it).
- The baked tree runs the compose-only scripts, not the vendored launchers — those may carry `build:`.
- ROS 2 names: no hyphens, no leading digits — the instance name becomes a ROS namespace.
- Two cameras = two shm/NVENC budgets and two unique webrtc signalling ports (doctor flags clashes).
