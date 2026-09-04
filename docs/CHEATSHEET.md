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
#   images: { registry: "<IP>:5000", tag: "v1.3.0" }    # tag = VERSION -> RIG_IMAGE_TAG (legacy: a bare
#                                                        #   platform tag still works, deprecated)
#   platform: jp7                                        # THIS host's hw/OS target -> RIG_TARGET_PLATFORM;
#                                                        #   matrix services pull <image>:<tag>-<platform>
#                                                        #   (per-host: /etc/rig may carry it — `rig
#                                                        #   provision --platform jp7`)
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
rig setup --shell                  # + TAB completion in your rc (deb/brew installs ship it already)
rig registry sync                  # clone/ff-pull the caches; everything below is OFFLINE after this
rig init my-vehicle --vehicle-id 7 && cd my-vehicle    # born a git repo (--no-git opts out);
                                   #   without --vehicle-id the tree carries per-host identity
                                   #   MARKERS (v0.2.20) — `rig provision`/RIG_VEHICLE_ID supply them
rig add public/zenoh-router        # infra from the registry: repo cloned AT THE PIN, launch surface
                                   #   VENDORED into services/, config from its example, rig.lock written
rig add sensor:zr30                # match a profile (exact → glob → fallback), install its service
                                   #   transitively, materialize the payload as the EDITABLE config
rig pkg search ouster:             # profiles for a service, across registries ("what drives
                                   #   ouster?"); `ouster:gen*` globs the short half
rig add ouster:generic             # profile identity IS the tuple <service>:<short> (schema 2) —
                                   #   this is a plain ref: unqualified = priority order, or pin
                                   #   with <registry>/ouster:generic@1.0.0. On disk the profile
                                   #   lives at profiles/ouster/generic/ in its registry.

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
rig pkg promote --all --project gideon --suite gideon-boat --vehicle gideon --to internal --adopt
                                   # whole-deployment capture: dirty instances -> overlays, plus a
                                   #   suite, plus (--vehicle, v0.2.17) the instance PLAN — a template
                                   #   vehicle.yaml whose rows carry YOUR names/order/enabled/tiers/
                                   #   bindings (N instances per profile). --adopt (v0.2.18) consents
                                   #   to capturing HAND-AUTHORED instances (no registry anchor — e.g.
                                   #   a service with no examples) as adopted PROFILES; without it
                                   #   they are skipped loudly. Re-running the same command re-captures
                                   #   the plan by name. Identity stays per-host (markers); fleet
                                   #   defaults (platform, images, data_dir, ros, vars) travel literal
rig init veh2 && rig pkg add internal/gideon-boat --root veh2
                                   # a fresh (EMPTY) tree reproduces the vehicle: rows, bindings,
                                   #   platform… — identity arrives per host (`rig provision` /
                                   #   RIG_VEHICLE_ID; `rig init --vehicle-id 7` pins a literal)
rig pkg save siyi_zr30             # CANONICAL update-in-place (v0.2.8): publish the edits into the
                                   #   package the instance is pinned to + re-anchor clean, render
                                   #   identical — bound overlay first (top of stack), else profile.
rig pkg promote siyi_zr30 --kind profile --to internal
                                   # the explicit spelling of the same profile update (name from
                                   #   provenance, --bump implied, provides/match CARRIED FORWARD);
                                   #   hand-authored instance (no pin)? bare promote infers profile
                                   #   — promote is the verb for NEW packages, forks, and suites.
rig pkg promote siyi_zr30 --kind profile --name org-zr30 --to internal --adopt
                                   # FORK the public base into an org profile (records based_on
                                   #   lineage) and ADOPT it: instance re-pins to the fork, render
                                   #   identical — the three-tier shape: public base -> internal org
                                   #   profile -> project overlays bound per deployment
rig pkg promote my_driver --kind service --to internal --version 0.1.0
                                   # publish the routed DEV CHECKOUT's code pointer (origin URL +
                                   #   pushed HEAD; clean tree enforced) — registry-release sans CI
rig registry sync && rig pkg rebase camera-service:org-zr30 --to internal
                                   # public base moved? three-way the fork onto it (D vs old parent
                                   #   replayed on the new one; conflicts keep YOURS, loudly; old
                                   #   parent payload served from the registry's git history).
                                   #   Deployments then follow with plain `rig pkg upgrade`.
rig pkg upgrade                    # registry moved? three-way merge: new base ⊕ your edits, conflicts
                                   #   loud; bound overlays rebind IN PLACE (order kept). All-or-nothing:
                                   #   a mid-sweep failure rolls the whole tree + lock back
rig pkg add internal/zr30-gideon@1.0.0
                                   # a PAST version: git-backed registries serve history read-only
                                   #   (git log/show on the full clone — no checkout, no tags).
                                   #   --locked re-resolves at the LOCKED registry commit, so
                                   #   reproduction reproduces even after the registry moves
```

**The daily loop (rig ≥ v0.2.9): discover, save, stay current, publish, undo.**

```bash
rig pkg search                     # no query = the WHOLE catalog (--kind overlay, --registry public)
rig pkg list                       # the FULL inventory: registry packages + upgrade state, PLUS
                                   #   path-added/vendored services as local/unpublished rows —
                                   #   the promotion worklist
rig pkg add ../my-driver           # local paths work here too — ONE grammar with `rig add`
                                   #   (dir AND registry ref both live = hard error; ./ or @ver escapes)
rig pkg info internal/ouster:generic --versions
                                   # every published version from git history (@old is installable)
rig pkg save siyi_zr30             # publish local edits into the package they CAME FROM + re-anchor
                                   #   clean, render identical: bound overlay first (top of the
                                   #   stack), else the pinned profile. save = update in place;
                                   #   promote = something NEW (fork/kind/suite). Never pushes.
rig pkg save camera-service        # a routed dev checkout's CODE pointer (next version @ HEAD);
                                   #   prints which instance configs still carry unsaved edits
rig pkg outdated                   # registry-authoring currency: profile requires/based_on, overlay
                                   #   authored_against, suite members vs current — FIX column names
                                   #   the repair; exit 1 on drift (--quiet for cron)
rig pkg repin ouster:generic --to internal
                                   # advance declared PINS to current + next patch version (payloads
                                   #   untouched — that's rebase); suites refresh every member incl.
                                   #   the vehicle plan. Repin inside-out: profiles first, then the
                                   #   suites that reference them (outdated's FIX column = the order).
                                   #   NOT an emergency (v0.2.19): stale exact pins are snapshots —
                                   #   validate warns, installs serve the pinned version from the
                                   #   registry's git history (keep internal registries git-backed)
rig pkg upgrade --dry-run          # the REAL sweep (three-ways, conflicts) then rolled back — a
                                   #   full-fidelity preview
rig registry sync                  # now prints the package-level delta digest (what moved upstream)
rig registry pending               # unpublished promote/* branches across the caches, with commands
rig registry push internal --all --pr
                                   # push via YOUR git (promote/* only, never the default branch);
                                   #   --pr creates the PR via your gh/glab when installed
rig registry discard internal promote/save-zr30-gideon
                                   # pre-push undo: branch deleted AND this deployment re-anchored —
                                   #   your changes come back as LOCAL edits, render identical
rig pkg yank ouster:generic --from internal
                                   # retract the CURRENT version (previous restored from history; a
                                   #   first publish is removed) — same render-preserving un-save
```

Registries: `rig registry init <dir>` scaffolds a new one (usable immediately via
`rig registry add internal --path <dir>`; push it to GitHub/GitLab later — CI wrappers included).
`--front` makes a dev checkout shadow `public` for unqualified names. `rig.lock` records every pin +
hash; `rig pkg add <ref> --locked` reproduces byte-identical configs on a second machine.

Canonical grouped commands (old flat spellings stay as permanent aliases): `config show|render|diff` ·
`run new|end|list|rm|import|retrofit` · `registry init|add|remove|list|sync|pending|push|discard|validate|index` · `pkg
search|info|list|outdated|add|remove|upgrade|lock|save|promote|repin|rebase|yank` · `overlay
apply|remove|reorder|list` · `service rigify|vendor|certify` · `artifact bake|unbake|list` ·
`image build|pull`.

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
vars:
  rtsp_port: 8554                 # per-DEPLOYMENT defaults; vars may chain: ip: 10.160.{{vehicle_id}}.25
  gcs_ip: 10.160.1.10             # a fallback default — the FLEET value comes from fleet.yaml (below)
env:
  SIYI_IP: "10.160.{{vehicle_id}}.25"   # exported to every launcher via the fleet env
  GCS_IP: "{{gcs_ip}}"
# config/sensors/zr30.yaml:   url: rtsp://10.160.{{vehicle_id}}.80:{{rtsp_port}}/main
# config/infra/zenoh.yaml:    connect: "{{map fleet_peer_ids peer_endpoint}}"
#   {{map <list_var> <template_var>}} (whole-scalar only) renders a LIST — one template expansion
#   per element, {} as the placeholder.
```

**Fleet-level vars live in fleet.yaml, not vehicle.yaml** (v0.1.68): the deployment-root
fleet.yaml (pushed by `rig fleet up`, persisting across reboots) is a vars source —
`{{fleet_ids}}` is DERIVED from the roster (`vehicles[].id`; never hand-maintained),
`{{gcs_ip}}` and `{{fleet_mode}}` come from its top-level keys, and its `vars:` section carries
fleet policy like `peer_endpoint` (field IPs vs SIL ports = editing ONE file, or a
`RIG_VAR_peer_endpoint='tcp/127.0.0.1:744{}'` override). rig derives `{{fleet_peer_ids}}` =
fleet_ids minus THIS vehicle's id.

Sources, most-specific wins: shell (`RIG_VEHICLE_ID`, `RIG_VAR_<name>`) > `vehicle.local.yaml`
beside vehicle.yaml (bench trees) > **`/etc/rig/vehicle.local.yaml`** (THE machine's identity) >
**`fleet.yaml`** (the fleet tempo tier) > vehicle.yaml. Unknown var = hard error listing what's
available. Everything lands in every run's config snapshot — per-run fleet composition is
recorded per vehicle automatically, and a mid-test REBOOT re-renders from the pushed fleet.yaml,
not stale defaults.

**Fleet roster + `rig fleet` (v0.1.66):** `fleet.yaml` on the GCS box is OPERATIONAL state
(gitignored by init; commit only by choice) and drives the fan-out verbs — the ssh loop,
automated, never a control plane (system ssh/scp, BatchMode, fail-soft per vehicle):

```yaml
fleet: gideon
mode: sil                    # sil | field (EXPLICIT) -> {{fleet_mode}} in every render/snapshot
gcs_ip: 10.160.1.10          # -> {{gcs_ip}} on every vehicle (the ONE home for it)
sil:                         # SIL block (mode: sil required)
  data_root: /sil/data       # local rows: data_dir=<data_root>/<name> via a SIMULATED machine
  network: {name: rig-sil, subnet: 10.160.1.0/24}   #   identity file (<data_root>/.identity/)
vars:                        # FLEET-level {{var}} values, pushed to every vehicle at `fleet up`
  peer_endpoint: tcp/10.160.1.{}:7447   # SIL? tcp/127.0.0.1:744{} — one file, whole fleet
vehicles:                    # THE roster -> {{fleet_ids}} derived ([3, 7]); never hand-listed
  - {id: 3, name: veh3, host: localhost, path: ~/sil/veh3, ip: 10.160.1.3}   # local = no ssh
  - {id: 7, name: skiff-07, host: orin, path: ~/ws/v3, data_dir: /home/uxv/logs}
```

```bash
rig fleet list                        # roster + reachability
rig fleet status [-v]                 # aggregates `status --format json` per vehicle
rig fleet up --run tuesday-swarm --var gcs_ip=192.168.44.10
                                      # CORRELATED run label on every vehicle; --var rides the
                                      #   RIG_VAR_* tier into each run's snapshot. SIL: ensures
                                      #   the docker network + the shared run-dir VIEW
                                      #   (<data_root>/runs/<label>-<date>/<vehicle> symlinks)
rig fleet down --end-run              # tear down + seal everywhere (full success rms the network)
rig fleet sync --into fleet-runs      # harvest SEALED runs (ended: = safe) into
                                      #   fleet-runs/<label>/<vehicle>/<run-id> — the SAME tree
                                      #   the SIL view shows live; idempotent re-sync
```

The SIL network is create/teardown + env only: `RIG_NETWORK`/`RIG_VEHICLE_IP` are exported to
every stack; services JOIN it in their own composes (external network + ipv4_address) — rig
never writes compose config. Vehicles never read fleet.yaml; `fleet up` pushes it beside each
deployment so the run snapshot records the roster the run was launched under.

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
rig build --no-cache                          # FULL rebuild: RIG_BUILD_NO_CACHE=1 to every build command
                                              #   (scripts opt in: docker build ${RIG_BUILD_NO_CACHE:+--no-cache});
                                              #   the way to re-converge apt-level drift across images
rig image audit                               # inspect what the stacks will RUN (docker + images local/pullable):
                                              #   every ROS image carries ros.distro, the declared rmw package
                                              #   is installed, and shared ros-* package VERSIONS agree across
                                              #   images (a skewed rmw_zenoh_cpp = sessions that can't talk)
curl -s http://<dev-box-ip>:5000/v2/_catalog  # expect every repo the composes will pull
```

Tags: `rig build` tags with `images.tag` — COMPOSED to `<tag>-<platform>` for services declaring a
`build.platforms` matrix (vehicle.yaml `platform:` or `--platform`; the build also gets
RIG_TARGET_PLATFORM + the service's override env) — and certify's tag/platform checks guarantee the
composes pull the same per matrix entry — build/pull agreement is enforced, not hoped.

Base image: a service whose rigging declares `build: {…, provides: base}` (rig-infra's fleet-ros)
builds FIRST, and its composed ref (`<registry>/<images[0]>:<tag>`) is exported to every other build
and launcher as **RIG_BASE_IMAGE** — build `FROM ${RIG_BASE_IMAGE}` and the fleet's distro+rmw
packages come from ONE image by construction. `vehicle.yaml images.base` (or `--base-image REF`)
overrides with an external ref. Providers of one base must AGREE on everything that decides the ref —
a different image name, a different `build.platforms`, or a genuinely different build script is an
ERROR, never a manifest-order guess (the last one would have two builds racing for one tag). "The same
script" is judged by CONTENT, not path (v0.2.25): two pinned checkouts of one collection repo — the
registry-install case, where each service used to fetch its own clone — dedupe to ONE base build when
the script's directory has the same git tree, even at different revs. The contract that makes this
sound: **a base build script's directory is its whole build context** (rig-infra's `base/` —
Dockerfile + build.sh, self-contained); anything unprovable (dirty checkout, not git) falls back to
path identity, i.e. toward refusing. `ros.rmw` rides along as
**RIG_ROS_RMW** so a base build installs the rmw the vehicle declares — the prevention counterpart to
audit's rmw check.

Consumer rule: rebasing alone is NOT enough. A plain `apt-get install` of a package the base already
carries silently UPGRADES it (the base was built earlier; the ROS apt repo moved in between) —
re-creating the skew under a new package name with no pin and no operator error anywhere. Install your
own extras with `apt-get install --no-upgrade`: base-pinned packages stay pinned, new packages install
normally, and a rig-less build on a stock base still completes. Base packages then update only through
a base rebuild — that single point of update is the point. Audit reports any residual non-ROS drift
(transitive pulls) as one summarized WARN.

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

**Operational states** (rig ≥ v0.2.35; services declaring `standby`/`activate`/`state` in rigging
`verbs:` — e.g. ouster ≥ v0.2.0. Others are always-active and skipped):
```bash
rig standby [name…]        # park: ready but quiet (lifecycle idle, device low-power) — HEALTH unaffected
rig activate [name…]       # wake (producers first; launchers own the budgets — device spin-up ≈ a minute)
rig up --standby           # come up parked: RIG_TARGET_STATE=standby beats each config's initial_state
rig status                 # OP column, next to HEALTH — read as a PAIR: transitioning+healthy = wait
                           #   (post-activate self-reset), transitioning+unhealthy = stuck; '-' = undeclared
```
Trigger-style stacks (SLAM, planning) idle in standby until a mission layer activates them — direct
lifecycle transitions are expected, not drift. After a vehicle POWER event, re-run `rig standby` if
parked: device modes are applied, not persisted (a power-cycled sensor boots back NORMAL while
`state` still reads standby); the repeat converges the full standby definition.

**Run directories** (needs `data_dir`; ROADMAP §3c): one session = one folder under
`data_dir/runs/<stamp>_<label>/`, with a provenance manifest (`ended:` present = sealed = safe to sync).
`up` auto-opens an `_auto` run if none is open — it NEVER rotates; rotation/sealing are explicit and
refuse while stacks run. Each `up` also snapshots the effective config into the run
(`.rig/config/<digest>/`, deduplicated) — the manifest's `config:`/`ups:` say exactly which config each
stretch of data was recorded under, and sealing warns if config changed after the last `up`.
`down --end-run` additionally captures every container's `docker logs` into the run
(`.rig/logs/<sensor>/<container>.log`) before tearing down — compose down removes the containers, so
that is the last moment their logs exist. With the bag logger's `graph:` sidecar enabled
(rig-infra ≥ v1.7.0) the run also records the live graph TOPOLOGY as epoch files
(`graph/<name>/epoch_*.yaml` — who published/subscribed/served what, when); `rig graph [run]`
derives the union view at read time, `--check` WARNs on drift vs the riggings' `interface:` blocks,
`--contract <instance>` scaffolds that block from observation:
```bash
ssh $VEHICLE 'cd ~/ws/v1 && ./run.sh up --run dock-test'   # open a labeled session + up (idempotent)
ssh $VEHICLE 'cd ~/ws/v1 && ./run.sh runs'                 # registry: OPEN / sealed / interrupted
ssh $VEHICLE 'cd ~/ws/v1 && ./run.sh down --end-run'       # stop everything, then seal the session
scp -r $VEHICLE:<data_dir>/runs/<stamp>_dock-test .        # the whole session, data + manifest
rig graph <stamp>_dock-test --check                        # topology + declared-vs-observed WARNs
rig reconstruct <path-to-run> --into tree/                 # the run dir back into a runnable tree
                                                           #   (its .rig/artifact.tar.gz capture;
                                                           #   old runs: `rig run retrofit` first)
                                                           #   --registry HOST (bench mirror) ·
                                                           #   --enable-replay <path|ref> (old trees)
rig replay <stamp>_dock-test planner                       # SIL: play planner's recorded inputs at
                                                           #   the CURRENT planner build/config —
                                                           #   new run links back via replay-of
rig replay <stamp>_dock-test planner --from 120 --to 300   # a SECTION of the run (seconds from
                                                           #   bag start; --auto-end for sweeps)
rig swap planner ../planner                                # in a reconstructed tree: same rows +
rig swap planner public/planner@2.1.0                      #   configs, DIFFERENT code (drift named)
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

Teardown: `./run.sh down` (volumes survive); final removal `rig down --purge`. Leaving the machine
entirely: `rig cleanup` (or `./run.sh cleanup` in an artifact) — removes the deployment's images +
volumes (never containers, never RIG_DATA_DIR); `--dry-run` shows the sweep, `--keep-volumes` limits
it to images. Then the tree is safe to delete. Dev registry off:
`docker rm -f registry` (keep the `registry-data` volume unless you're truly done).

## Gotchas (each learned the hard way)

- The registry rig uses is `vehicle.yaml: images.registry` (or `rig build/bake --registry`) — **there is no
  `$REGISTRY` env var.** Exporting one does nothing; if `rig build` pushes to the "wrong" IP, it's the one
  in vehicle.yaml. A baked artifact also pins that host, so fix vehicle.yaml *before* you bake.
- Registry trust is needed on **both** machines, **with the port** — a bare IP doesn't match `IP:5000`.
- **MERGE** the Jetson's `daemon.json` — overwriting drops the `nvidia` runtime the camera needs.
- One `images.tag` (version) + one `platform:` per host: platform-agnostic services pull `:<tag>`,
  matrix services pull `:<tag>-<platform>` (certify enforces both). A platform-valued tag with no
  `platform:` is the deprecated legacy spelling.
- The registry must keep its volume between `rig build` and the vehicle's first pull (digests die with it).
- The baked tree runs the compose-only scripts, not the vendored launchers — those may carry `build:`.
- ROS 2 names: no hyphens, no leading digits — the instance name becomes a ROS namespace.
- Two cameras = two shm/NVENC budgets and two unique webrtc signalling ports (doctor flags clashes).
