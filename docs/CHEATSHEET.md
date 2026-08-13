# rig — deployment cheat sheet

> The whole workflow on one page (rig ≥ v0.1.48). Long-form: `RUNBOOK.md` (worked Orin example),
> `README.md` (concepts + install), `STATE.md` (current live state). Mental model: **services own their
> bring-up** (launcher + `rigging.yaml`); **rig owns the vehicle** (manifest, env, ordering, artifacts).
> The vehicle runs the baked artifact — no source, no internet (fleet artifacts render on-vehicle, §1.6).
> Install rig itself via deb/brew/pipx (README "Install"); `rig setup` once per user.

```
author configs ──▶ validate ──▶ build images ──▶ bake ──▶ ship ──▶ run ──▶ iterate
                doctor/certify   rig build        rig bake   scp     run.sh
```

## 0 — one-time host setup

```bash
# DEV BOX: a persistent local registry (MUST keep its volume until the vehicle has pulled)
docker run -d --restart always -p 5000:5000 -v registry-data:/var/lib/registry --name registry registry:2
# Docker Desktop → Settings → Docker Engine → "insecure-registries": ["<dev-box-LAN-IP>:5000"] → Restart
# Use the dev-box LAN IP the VEHICLE can reach, always WITH the :5000 port.
#
# rig learns this registry from vehicle.yaml `images.registry` (step 1), or `rig build/bake --registry
# <ip:5000>` to override — there is NO $REGISTRY env var. (`VEHICLE=<user@orin-ip>` below is just an ssh
# alias for the scp/ssh lines, your convenience — also not read by rig.)

# VEHICLE (Jetson): trust the registry — MERGE into /etc/docker/daemon.json (NEVER overwrite: it carries
# the `nvidia` runtime). See RUNBOOK §7 for the python3 merge one-liner. Then: sudo systemctl restart docker
# (skip this entirely if you deploy with `rig bake --bundle-images` — no registry needed on the vehicle.)
# Optional but recommended: sudo apt install python3-yaml   (enables rig verbs + per-host overlays on-vehicle)
```

## 1 — workspace + deployment scaffold

```bash
# with rig INSTALLED (deb/brew/pipx — README "Install") the registry flow in §1.5 needs no
# workspace at all. The clone-and-wire flow below remains for local service development:
mkdir -p ~/ws && cd ~/ws                      # service repos + rig + rig-infra as siblings
git clone <camera-service> <dashboard> <rig> https://github.com/christomaszewski/rig-infra ...
alias rig="$PWD/rig/rig"                      # only when running rig from this checkout

# --infra: shared services from the workspace (rig-infra), wired + ENABLED (router pinned to order 0)
# --discover: scan sibling repos (rigging.yaml) -> services.yaml populated + examples copied + commented MENU
rig init my-vehicle --vehicle-id 7 --infra zenoh-router --infra ros2-bag-logger --discover
cd my-vehicle
# --discover never enables anything: a repo in the workspace ≠ hardware on this vehicle. Uncomment the
# vehicle.yaml menu entries this vehicle actually runs (copied examples are nameless profiles — the entry
# supplies the instance name), then `rig doctor`. Or author everything by hand:
# services.yaml — route each service name to its repo:
#   services: { zenoh-router: {path: ../rig-infra/zenoh-router}, camera-service: {path: ../camera-service}, ... }
# vehicle.yaml — identity + fleet env + the stacks:
#   vehicle: my-vehicle        vehicle_id: 7            # -> ROS domain 7, VEHICLE_ID=7
#   ros:    { rmw: rmw_zenoh_cpp, distro: lyrical }     # zenoh rmw ⇒ declare a zenoh-router in infra:
#   images: { registry: "<IP>:5000", tag: "jp7" }       # ONE tag per vehicle (the platform, e.g. JetPack)
#   data_dir: /home/<user>/logs                          # recordings/logs land here (RIG_DATA_DIR)
#   infra:   [ {name: zenoh-router, ...order: 0}, {name: dashboard, ...order: 5} ]     # up FIRST
#   sensors: [ {name: cam_usb, ...order: 10}, {name: cam_rtsp, ...order: 20} ]
#   autonomy: [ {name: planner, ...order: 10} ]      # graph consumers: up after ALL sensors, down FIRST
# config/{infra,sensors,autonomy}/<name>.yaml — one per instance. Hand-authored the rows? `rig fetch`
#   materializes each missing row config from the routed service's example (nameless profile — the row
#   stamps the name). Never edits manifests, never overwrites. (`pull` = images; `fetch` = configs.)
```

Grow the deployment later with `rig add <name|path>` (routes + config + manifest row: infra ENABLED,
sensor/autonomy a commented menu row; `--tier` overrides the service's declared tier for THIS vehicle —
placement + enabled-vs-menu follow); make NON-rig software compatible first with `rig rigify <dir>`
(descriptor + launcher skeleton + example, analysis-seeded; `--tier` declares it in the generated
rigging.yaml) → `rig certify --repo` until green.

## 1.5 — the package registry (rig ≥ v0.1.44): install, tune, promote

No workspace checkouts needed — services/profiles install from registries, pinned + vendored:

```bash
rig setup                          # once per machine: ~/.rig + the default `public` registry
rig registry sync                  # clone/ff-pull the caches; everything below is OFFLINE after this
rig init my-vehicle --vehicle-id 7 && cd my-vehicle    # born a git repo (--no-git opts out)
rig add public/zenoh-router        # infra from the registry: repo cloned AT THE PIN, launch surface
                                   #   VENDORED into services/, config from its example, rig.lock written
rig add sensor:zr30                # match a profile (exact → glob → fallback), install its service
                                   #   transitively, materialize the payload as the EDITABLE config
```

The working config (`config/sensors/<name>.yaml`) is yours to edit — the pristine base is pinned at
`config/.pins/` (committed, hash-locked). The git-style loop:

```bash
vim config/sensors/siyi_zr30.yaml  # tune for your project — edit the file directly
rig config diff                    # like `git status`: which instances are dirty, per key, attributed
rig pkg promote siyi_zr30 --name zr30-gideon --project gideon --to internal
                                   # delta -> a versioned overlay in your internal registry checkout
                                   #   (write+validate; publish = plain git push/PR, printed for you)
rig registry sync && rig overlay apply siyi_zr30 internal/zr30-gideon --clear-local
                                   # same render, tuning now VERSIONED (local always beats overlays)
rig pkg promote --all --project gideon --suite gideon-boat --to internal
                                   # whole-deployment capture: overlays + a suite; a fresh vehicle
                                   #   reproduces it with `rig pkg install internal/gideon-boat`
rig pkg upgrade                    # registry moved? three-way merge: new base ⊕ your edits, conflicts loud
```

Registries: `rig registry init <dir>` scaffolds a new one (usable immediately via
`rig registry add internal --path <dir>`; push it to GitHub/GitLab later — CI wrappers included).
`--front` makes a dev checkout shadow `public` for unqualified names. `rig.lock` records every pin +
hash; `rig pkg install <ref> --locked` reproduces byte-identical configs on a second machine.

Canonical grouped commands (old flat spellings stay as permanent aliases): `config show|render|diff` ·
`run new|end|list` · `registry init|add|remove|list|sync|validate|index` · `pkg
search|info|install|upgrade|lock|promote` · `overlay apply|remove|reorder|list` · `service
rigify|vendor|certify` · `artifact bake|unbake|list` · `image build|pull`.

Airgap: `sync` → `install` → `rig pull` → `rig bake --bundle-images` — the deployment is
self-contained after install (vendored surfaces + materialized configs); the registry cache is only
needed to install/upgrade.

## 1.6 — fleet vehicles: one artifact, N vehicles (rig ≥ v0.1.48)

Reference per-vehicle values with `{{var}}` anywhere in configs (never `${VAR}` — that's compose's):

```yaml
# vehicle.yaml (fleet-shared)
vehicle: "{{vehicle}}"            # self-marker = supplied PER VEHICLE, mandatory (never vehicle 0)
                                  # QUOTE markers that START a value (bare {{ is YAML mapping syntax);
                                  #   mid-string markers (rtsp://10.{{vehicle_id}}.80) are fine unquoted
vehicle_id: "{{vehicle_id}}"
vars: {rtsp_port: 8554}           # fleet defaults; vars may chain: ip: 10.160.{{vehicle_id}}.25
env:  {SIYI_IP: "10.160.{{vehicle_id}}.25"}   # exported to every launcher via the fleet env
# config/sensors/zr30.yaml:  url: rtsp://10.160.{{vehicle_id}}.80:{{rtsp_port}}/main
```

Sources, most-specific wins: shell (`RIG_VEHICLE_ID`, `RIG_VAR_<name>`) > `vehicle.local.yaml`
beside vehicle.yaml (bench trees) > **`/etc/rig/vehicle.local.yaml`** (THE machine's identity) >
vehicle.yaml. Unknown var = hard error listing what's available.

`rig bake` detects markers automatically (no flag): a templated deployment bakes a **fleet
artifact** — unresolved configs, no compose-only form, rendered on-vehicle by the bundled rig
(python3 + pyyaml required). Bake never reads vehicle.local.yaml/shell vars, so bench identity
can't leak into artifacts. Vehicle lifecycle:

```bash
# once per vehicle (imaging time):
sudo ./provision.sh --id 7 --name skiff-07     # or: sudo rig provision …  (writes /etc/rig/…)
# forever after, ANY artifact:
tar xzf v3.tar.gz && cd v3 && ./run.sh up
rig provision                                   # no sudo: show identity + check the deployment's vars
```

Re-identifying a machine needs `--force` (compose projects rename → running containers orphan —
bring the vehicle down first). `certify --emit/--diff` output legitimately differs across vehicles
for templated configs — that's the feature, not a bug.

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
                                              #   registry comes from vehicle.yaml; --registry <ip:5000> overrides
                                              #   exports ROS_DISTRO from ros.distro (fleet-ros bakes YOUR distro)
curl -s http://<dev-box-ip>:5000/v2/_catalog  # expect every repo the composes will pull
```

Tags: `rig build` tags with `images.tag` (jp7) and certify's tag check guarantees the composes pull the
same — build/pull agreement is enforced, not hoped.

## 4 — bake a deployable artifact

```bash
rig bake --tag v1                             # -> var/artifacts/v1.tar.gz  (sha256 printed)
rig bake --tag v1 --bundle-images             # + docker-saves the image set INTO the artifact (multi-GB):
                                              #   zero registry at deploy time; up.sh self-loads on first run
```

The artifact = resolved configs + complete vehicle.yaml + vendored launch surfaces + rig + a **compose-only**
form (build-stripped; built images digest-pinned, mirrored images by tag). It runs on bare Docker.
Bundled artifacts keep **tag** refs (loaded images can't carry registry digests) — integrity is the
artifact's own sha256; digests are still recorded in `metadata.yaml`/`rig.lock` for audit.

## 5 — ship + run on the vehicle

```bash
scp var/artifacts/v1.tar.gz $VEHICLE:~/ws/
ssh $VEHICLE 'cd ~/ws && tar xzf v1.tar.gz'
ssh $VEHICLE 'cd ~/ws/v1 && ./run.sh pull'    # optional: pre-warm the image cache — touches NO containers
ssh $VEHICLE 'cd ~/ws/v1 && ./run.sh up'      # pulls from the registry in vehicle.yaml; infra → sensors → autonomy
ssh $VEHICLE 'cd ~/ws/v1 && ./run.sh status'  # or: ./run.sh logs <name> · ./run.sh down
```

Quick verification: containers up (`docker ps`), dashboard at `http://<vehicle>:8080`, recordings growing
under `data_dir`, camera log shows `health: frames=N, no drops`. After the first pull the vehicle runs
**offline** — the registry is only needed for updates.

**Run directories** (needs `data_dir`; ROADMAP §3c): one session = one folder under
`data_dir/runs/<stamp>_<label>/`, with a provenance manifest (`ended:` present = sealed = safe to sync).
`up` auto-opens an `_auto` run if none is open — it NEVER rotates; rotation/sealing are explicit and
refuse while stacks run:
```bash
ssh $VEHICLE 'cd ~/ws/v1 && ./run.sh up --run dock-test'   # open a labeled session + up (idempotent)
ssh $VEHICLE 'cd ~/ws/v1 && ./run.sh runs'                 # registry: OPEN / sealed / interrupted
ssh $VEHICLE 'cd ~/ws/v1 && ./run.sh down --end-run'       # stop everything, then seal the session
scp -r $VEHICLE:<data_dir>/runs/<stamp>_dock-test .        # the whole session, data + manifest
```
(bare-Docker hosts: `./new-run.sh dock-test && ./up.sh` — the flagged forms need the bundled rig.)

## 6 — iterate

| change                  | loop                                                              |
|-------------------------|-------------------------------------------------------------------|
| sensor config only      | edit → `rig bake --tag v2` → scp/extract → `./run.sh up`          |
| service code/images     | `rig build` → `rig bake --tag v2` → ship → `./run.sh up`          |
| field tweak on-vehicle  | edit the extracted tree's config → `./rig up` (re-renders live)   |
| save a field state      | on the vehicle: `./rig bake --tag day3-final [--bundle-images]` — re-bakes the extracted tree (local edits included) and stamps its parent artifact (lineage) |
| new service             | `rig rigify <dir>` (descriptor + launcher skeleton + example config, analysis-seeded; never overwrites) → finish TODOs → `rig certify --repo` until green → `rig add <name\|path>` (routes services.yaml + copies the example config + adds the vehicle.yaml row: infra ENABLED, sensor/autonomy a commented menu row) |

Teardown: `./run.sh down` (volumes survive); final removal `rig down --purge`. Dev registry off:
`docker rm -f registry` (keep the `registry-data` volume unless you're truly done).

## Gotchas (each learned the hard way)

- The registry rig uses is `vehicle.yaml: images.registry` (or `rig build/bake --registry`) — **there is no
  `$REGISTRY` env var.** Exporting one does nothing; if `rig build` pushes to the "wrong" IP, it's the one
  in vehicle.yaml. A baked artifact also pins that host, so fix vehicle.yaml *before* you bake.
- Registry trust is needed on **both** machines, **with the port** — a bare IP doesn't match `IP:5000`.
- **MERGE** the Jetson's `daemon.json` — overwriting drops the `nvidia` runtime the camera needs.
- One `images.tag` per vehicle: platform-agnostic services must still *pull* that tag (certify enforces).
- The registry must keep its volume between `rig build` and the vehicle's first pull (digests die with it).
- The baked tree runs the compose-only scripts, not the vendored launchers — those may carry `build:`.
- ROS 2 names: no hyphens, no leading digits — the instance name becomes a ROS namespace.
- Two cameras = two shm/NVENC budgets and two unique webrtc signalling ports (doctor flags clashes).
