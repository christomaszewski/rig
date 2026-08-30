# Runbook — deploy a 4-stack vehicle to an Orin from a local registry

A concrete, copy-pasteable walkthrough for the test deployment: **two camera-service instances (USB + RTSP)
+ the dashboard + a zenoh router**, built on a dev box, pulled onto a Jetson Orin from a local registry.
Set the placeholders, then work top to bottom. The only files you author by hand are the two camera configs.

> **Note (rig ≥ v0.1.44):** this walkthrough predates the package-registry layer and uses the
> clone-and-wire workspace flow, which still works exactly as written. The registry flow removes
> the clone steps — `rig setup && rig registry sync`, then `rig add public/zenoh-router` /
> `rig add sensor:zr30` install pinned, vendored services with no checkouts (CHEATSHEET §1.5) —
> and `{{var}}` templating turns the bake into a fleet artifact deployable to any provisioned
> vehicle (CHEATSHEET §1.6). The dashboard still has no public remote, so this walkthrough's §2
> clone remains the way to get it.

```
infra:   zenoh-router (order 0)   +  dashboard (order 5; a zenoh-client sidecar)
sensors: cam_usb (camera.type usb) +  cam_rtsp (camera.type rtsp)
```

## Placeholders — set once (this shell)
```bash
export REGISTRY="192.168.1.50:5000"     # dev box LAN IP:5000 (reachable from the Orin). Substituted into
                                        #   vehicle.yaml below — rig reads the registry from THERE (or a
                                        #   `--registry` flag), never from this shell var.
export ORIN="orin"                       # ssh target (e.g. user@192.168.1.60)
export JETPACK="jp7"                     # the Orin's JetPack: jp7 or jp6 -> vehicle.yaml `platform:`
CAMERA_URL="https://github.com/christomaszewski/camera-service.git"   # public
DASHBOARD_URL="git@github.com:christomaszewski/dashboard.git"         # NOT yet published — see §2
```

## 1 — Dev box: local registry
```bash
docker run -d --restart always -p 5000:5000 -v registry-data:/var/lib/registry --name registry registry:2
curl -s http://$REGISTRY/v2/_catalog       # -> {"repositories":[]}
```
> **Trust the registry on the dev box too** — it's plain HTTP. Docker Desktop → Settings → Docker Engine →
> add `"insecure-registries": ["192.168.x.x:5000"]` → Apply & Restart. Without it, `rig build`/`docker push`
> fail with `server gave HTTP response to HTTPS client`. If a service's `build-images.sh` pushes via
> `docker buildx build --push` and still errors, BuildKit needs its own insecure config:
> `printf '[registry."'$REGISTRY'"]\n  http = true\n' > /tmp/bk.toml && docker buildx create --name rig --driver docker-container --config /tmp/bk.toml --use --bootstrap`

## 2 — Workspace + clones
```bash
mkdir -p ~/rig-walkthrough && cd ~/rig-walkthrough
git clone https://github.com/christomaszewski/rig.git rig    # rig is public
git clone "$CAMERA_URL"    camera-service
git clone "$DASHBOARD_URL" dashboard       # dashboard has no public remote yet — get it from its maintainer
alias rig="$HOME/rig-walkthrough/rig/rig"
rig --version                              # -> rig 0.1.24 (or newer)
```

## 3 — Scaffold the deployment
```bash
rig init my-vehicle && cd my-vehicle      # scaffolds config/{infra,sensors}/ + the manifest files
# Shortcut: `rig init my-vehicle --vehicle-id 7 --infra zenoh-router --discover` pre-wires the router
# (enabled, order 0) and catalogs every sibling service repo with a commented vehicle.yaml menu — the
# heredocs below then shrink to uncommenting entries + authoring the camera configs.

cat > services.yaml <<'EOF'
services:
  zenoh-router:   { path: ../rig-infra/zenoh-router }
  dashboard:      { path: ../dashboard }
  camera-service: { path: ../camera-service }
EOF

cat > vehicle.yaml <<EOF
vehicle: orin-test
vehicle_id: 7                 # decides ROS domain (=7) + exported as VEHICLE_ID
ros:
  rmw: rmw_zenoh_cpp
  distro: lyrical
images:
  registry: "$REGISTRY"       # -> RIG_IMAGE_REGISTRY (composes prefix their repo)
  tag: ""                     # a VERSION (e.g. v1.3.0) -> RIG_IMAGE_TAG; empty = the platform's moving
                              #   head for matrix services. NEVER the platform itself anymore.
platform: "$JETPACK"          # THIS host's target -> RIG_TARGET_PLATFORM (+ CAM_PLATFORM for the camera);
                              #   matrix services pull <image>:<tag>-<platform> (bare <platform> w/o tag)
infra:
  - { name: zenoh-router, service: zenoh-router, config: config/infra/zenoh-router.yaml, enabled: true, order: 0 }
  - { name: dashboard,    service: dashboard,    config: config/infra/dashboard.yaml,    enabled: true, order: 5 }
sensors:
  - { name: cam_usb,  service: camera-service, config: config/sensors/cam_usb.yaml,  enabled: true, order: 10 }
  - { name: cam_rtsp, service: camera-service, config: config/sensors/cam_rtsp.yaml, enabled: true, order: 20 }
EOF

cat > config/infra/zenoh-router.yaml <<'EOF'
service: zenoh-router
name: zenoh-router
EOF

cat > config/infra/dashboard.yaml <<'EOF'
service: dashboard
name: dashboard
web_port: 8080
ws_port: 10000
EOF
```

**Camera configs** — the symmetric schema (`camera.type` + a per-source block). These use `ros2-bridge`
(no host-facing port). Check the real keys first: `cat ../camera-service/core-driver/config/usb-real.yaml
../camera-service/core-driver/config/rtsp-real.yaml`.
```bash
cat > config/sensors/cam_usb.yaml <<'EOF'
service: camera-service
name: cam_usb
camera:
  type: usb
  frame_rate: 30.0
usb:
  device: /dev/video0          # EDIT: prefer /dev/v4l/by-id/... for stable hotplug
  fake: false
  pixel_format: MJPEG
  width: 1280
  height: 720
  sof_timestamps: true
recording: { enabled: false }
transport:
  plugin_endpoint: { enabled: true, socket_path: /tmp/cam/frames }
plugins:
  - { name: ros2-bridge, enabled: true, isolation: container, params: { topic: image_raw, frame_id: cam_usb } }
EOF

cat > config/sensors/cam_rtsp.yaml <<'EOF'
service: camera-service
name: cam_rtsp
camera:
  type: rtsp
rtsp:
  url: rtsp://10.160.1.80:8554/main.264   # EDIT: your RTSP camera URL
  protocols: tcp
  latency_ms: 200
recording: { enabled: false }
transport:
  plugin_endpoint: { enabled: true, socket_path: /tmp/cam/frames }
plugins:
  - { name: ros2-bridge, enabled: true, isolation: container, params: { topic: image_raw, frame_id: cam_rtsp } }
EOF
```

## 4 — Validate (dev box)
```bash
rig doctor          # -> rig doctor: orin-test — 2 sensors + 2 infra, 0 error(s); no zenoh warning (router present)
rig certify         # each launcher honors the contract: project name, registry/tag, ROS env, determinism, identity
rig up --dry-run    # zenoh-router -> dashboard -> cam_usb -> cam_rtsp; VEHICLE_ID=7 on each;
                    #   RIG_TARGET_PLATFORM=jp7 fleet-wide, CAM_PLATFORM=jp7 + composed RIG_IMAGE_TAG=jp7
                    #   on the camera stacks (tag-less: the platform IS the moving head)
```

## 5 — Build + push images
```bash
rig build -j 3      # builds the camera images (cam-core, ros2-bridge, webrtc-bridge):$JETPACK + dashboard images; mirrors eclipse/zenoh
curl -s http://$REGISTRY/v2/_catalog       # expect: cam-core, ros2-bridge, webrtc-bridge, dashboard-zenoh, dashboard-web, eclipse/zenoh
```
> Work is per unique *service*, so the two camera instances build `camera-service` **once**. `-j N` runs up
> to N services concurrently (output grouped per service); omit it for sequential, live-streamed output.

## 6 — Bake
```bash
rig bake --tag test1                       # auto-vendors surfaces + compose-only + digest-pins (cam-core:$JETPACK@sha256)
# rig bake --tag test1 --bundle-images     # OR: docker-save the images INTO the artifact (multi-GB) for an
#                                          #   air-gapped / zero-registry deploy — up.sh self-loads on first run
ls -lh var/artifacts/test1.tar.gz
```

## 7 — Ship + deploy on the Orin
```bash
# one-time host setup: trust the registry (plain HTTP). MERGE — do NOT overwrite: the Jetson's daemon.json
# carries the `nvidia` runtime the camera's jp7 containers need. (python3 merge keeps existing keys.)
ssh $ORIN "sudo python3 - <<'PY'
import json, pathlib
p = pathlib.Path('/etc/docker/daemon.json')
d = json.loads(p.read_text()) if (p.exists() and p.read_text().strip()) else {}
regs = d.setdefault('insecure-registries', [])
if '$REGISTRY' not in regs: regs.append('$REGISTRY')
p.write_text(json.dumps(d, indent=2) + '\n')
PY
sudo systemctl restart docker"
#   Also: plug the USB camera into the Orin; confirm the RTSP stream is reachable from it.

scp var/artifacts/test1.tar.gz $ORIN:/tmp/
ssh $ORIN 'sudo mkdir -p /opt/rig && sudo chown $USER /opt/rig && cd /opt/rig && tar xzf /tmp/test1.tar.gz'
ssh $ORIN 'cd /opt/rig/test1 && ./run.sh pull'    # optional: pre-warm the image cache (touches no containers)
ssh $ORIN 'cd /opt/rig/test1 && ./run.sh up'      # pulls digest-pinned images, infra -> sensors
ssh $ORIN 'cd /opt/rig/test1 && ./run.sh status'
```
Open `http://<ORIN-IP>:8080` from a laptop on the mesh.

## 8 — Iterate / teardown
```bash
ssh $ORIN 'cd /opt/rig/test1 && ./run.sh logs cam_usb'    # or: down
# parking between sorties (services declaring the state trio, e.g. ouster ≥ v0.2.0; rig ≥ v0.2.35):
ssh $ORIN 'cd /opt/rig/test1 && ./rig standby'            # lidar motor stops, laser off; HEALTH stays green
ssh $ORIN 'cd /opt/rig/test1 && ./rig status'             # OP column: standby · read OP+HEALTH as a pair
ssh $ORIN 'cd /opt/rig/test1 && ./rig activate'           # wake — device spin-up takes tens of seconds
# after a vehicle POWER event while parked: re-run `./rig standby` (device modes are applied, not
# persisted — a power-cycled sensor boots back NORMAL while state still reads standby)
rig graph --check          # after a run: observed topology vs declared interface: blocks (WARN-only)
# re-deploy after a change: edit configs -> rig build (if images changed) -> rig bake --tag test2 -> scp -> tar xzf -> ./run.sh up
# retiring a deployment from the Orin entirely: down, then decommission, then delete the tree —
ssh $ORIN 'cd /opt/rig/test1 && ./run.sh down && ./run.sh cleanup && cd .. && rm -rf test1'
docker rm -f registry                                      # stop the dev-box registry when done
```

---

## Field day — collecting a dataset worth replaying

What can't be retrofitted is decided at RECORD time: bags need the logger row up, replay's exact
topic selection needs the graph sidecar's epochs, and replay-from-any-offset needs latched topics
re-written into every split (`record.repeat_transient_local: true`). All three are wired in this
tree's `config/infra/bag_logger.yaml`. The night before:

```bash
# on the vehicle: data_dir + platform are MACHINE-local (/etc/rig/vehicle.local.yaml)
rig build && rig image audit         # images current (logger/sidecar/player ride fleet-ros ≥ infra v1.8.0)
rig bake --tag <t>                   # ship; on the vehicle: doctor, then a DRESS REHEARSAL —
./run.sh up --run rehearsal          #   a 3-minute run, then verify the run dir has bags/ +
./run.sh down --end-run              #   graph/<name>/epoch_*.yaml + camera video, and
rig graph <rehearsal-run> --check    #   the topology looks right. Cheap today, priceless tomorrow.
```

On the day: `up --run <label>` (label every session), `down --end-run` after landing (captures
docker logs, seals), copy the SEALED run dir off (`ended:` present = safe to sync), and leave
disk headroom ≥ 2× the expected bag+video volume.

## SIL replay — test a service change against a recorded run (rig ≥ v0.2.33)

Needs the `ros2-bag-player` row in vehicle.yaml (`autonomy:`, `enabled: false`, high `order` —
rig-infra ≥ v1.8.0) and a sealed source run with bags. The named instances come up LIVE at their
CURRENT build/config; the player plays exactly the topics they consumed in the source run
(selected from its graph epochs — observed subscribes minus observed publishes, so a service
never hears its own past outputs; pre-epoch runs fall back to a namespace heuristic, loudly).
By default the player publishes `/clock` (`RIG_SIM_TIME=1`) — services whose launchers adopted
sim-time pace to bag time; `--wall-clock` disables both sides at once. Replay preflight WARNs per
service under test whose rigging lacks `replay: {sim_time: true}` (the adoption promise — one
launcher change per service, see `~/ws/infra/service-sim-time-adoption-prompt.md`; the bag-logger
adopted in rig-infra v1.9.0, so replay-run bags record on bag time and the A/B pair aligns).

```bash
rig down                                   # replay starts from a quiet host (recorders pin their
                                           #   run dir at start — survivors would write elsewhere)
rig replay <stamp>_fieldtest planner       # new run opens, labeled replay-<source> (--label to name)
rig down --end-run                         # seal the replay session like any run
rig runs                                   # the REPLAY-OF column links the pair
```

The A/B artifact: the SOURCE run's bag holds the original outputs, the replay run's bag holds the
new ones, and `replay:` in the new run's manifest links them. `rig graph <replay-run>` shows the
SIL topology. Known limits (by design): `/tf` is shared-bus — the bag replays the ORIGINAL
stack's transforms, and frames the service under test re-publishes will conflict (the player
config's `play.exclude` is the hatch); ROS services/actions are not in bags, so
request/response-driven behavior does not SIL; replay is current-code-against-old-data, never a
bit-exact rerun (tag pins, not digests).

---

## Notes & prerequisites

**Platform (`platform:`) vs version (`images.tag`).** Two orthogonal *host-level* properties, both in
`vehicle.yaml` (never the per-sensor config). `platform: jp7` is the hardware/OS target — rig exports it
as `RIG_TARGET_PLATFORM` and mirrors it into each service's declared override env (`CAM_PLATFORM` for the
camera, which selects the matching runtime overlay — `docker-compose.jp7.yml`, runc + CDI NVENC).
`images.tag` is the release version — rig exports it as `RIG_IMAGE_TAG`, COMPOSED to `<tag>-<platform>`
for services declaring a build matrix (`cam-core:v1.3.0-jp7`; a bare `jp7` when the tag is empty), passed
through untouched for the rest (the dashboard falls back `RIG_IMAGE_TAG` → `DASH_IMAGE_TAG` → `arm64`).
`rig build` passes the same composed tag to each build command, so build + pull agree everywhere;
`rig certify`'s **tag and platform checks enforce** the agreement per launcher and per matrix entry. Only
the zenoh-router ignores the tag (it pulls the mirrored `eclipse/zenoh:latest`). *Legacy:* `images.tag:
jp7` with `platform:` unset still behaves exactly as before (deprecation warning; declare `platform:`).

**The baked compose-only form is correct for the target, by design.** The vehicle.yaml `platform:`
declaration is authoritative — cam-up honors `CAM_PLATFORM`/`RIG_TARGET_PLATFORM` over its own host
probe (`/etc/nv_tegra_release` remains the standalone/no-rig fallback), and its `config` verb never
probes the host under rig, so a bake on the dev box captures exactly what the Orin needs.
(`rig certify --emit` on both machines + `--diff` proves it: identical output; the certify `platform`
check proves every matrix entry renders declared-wins.) `python3-yaml` on the Orin is still
recommended — it enables the bundled rig's verbs (`./rig doctor`, field re-bakes) — but it is no
longer needed for platform correctness.

**Multi-instance safety.** Each camera entry has a unique `name` → its own compose project
(`cam_usb-vehicle-7` — rig owns the project name), ROS namespace (`/cam_usb`), and shm volume
(`cam_cam_usb_sock`); same internal `socket_path` is fine. With the `ros2-bridge`-only configs above
there's no host-facing port to clash. **If you enable `webrtc-bridge` on both cameras**, give each a
distinct signalling port — camera-service's rigging.yaml already declares the enabled-aware `host_ports`
selector, so `rig doctor` validates the ports across instances (and against the dashboard's 8080/10000)
automatically.

**Optional infra** (beyond this 4-stack example): [rig-infra](https://github.com/christomaszewski/rig-infra)
ships ready-to-use `ros2-bag-logger/` and `ros1-bag-logger/` — add one to `services.yaml`
(`../rig-infra/ros2-bag-logger`) + an `infra:` entry (order ~1, just after the router) to record the ROS
telemetry graph to `${RIG_DATA_DIR}/current/bags/` (the open run; flat `bags/` without a run registry).
See the services' example configs.

**External prerequisites** (outside rig):
- **camera-service** supports `camera.type: usb | rtsp | gige` — copy the real source keys from its
  `core-driver/config/usb-real.yaml` / `rtsp-real.yaml` examples into your configs.
- **dashboard** ships `tools/build-images.sh <registry> [tag]` and an `infra:`-tier `rigging.yaml` (both
  done). It has no public git remote yet — get the repo from its maintainer.
- Images must be **arm64** (the Orin) and reachable in the registry; the Orin caches them after the first
  pull, so it runs offline thereafter — or use `rig bake --bundle-images` to skip the registry entirely.

## Registry maintainer loop (rig ≥ v0.2.9)

Keeping a program registry current — run from any machine with the registries configured:

```bash
rig registry sync                  # ff-pull; prints the package-level delta digest (what moved)
rig pkg outdated                   # dependency drift across all registries: FIX column names the
                                   #   repair verb; exit 1 on drift, so a cron/CI sweep is one line
rig pkg repin <pkg> --to internal  # advance declared pins to current (next patch version) —
                                   #   inside-out: profiles (requires.service) first, then suites
                                   #   (every member incl. the vehicle plan refreshes)
rig pkg rebase <fork> --to internal# ...or three-way a fork's PAYLOAD onto its moved parent
rig registry pending               # what's awaiting publish (promote/* branches in the caches)
rig registry push internal --all --pr   # publish via YOUR git + gh/glab (PR creation only)
```

A service's registry-release CI never blocks on this (v0.2.19): suites/profiles pinning an older
version keep validating (a stale WARNING) and keep installing at their pinned versions from the
registry's git history — so the loop above is maintenance, not firefighting. Whole-vehicle captures
(`promote --all --suite S --vehicle V [--adopt]`) re-capture the vehicle plan by name on every
run; a fresh `rig init` tree reproduces the vehicle with `rig pkg add internal/S`.

Mistakes are cheap to retract: `rig registry discard internal <branch>` (unpushed — the cwd
deployment is re-anchored render-identically, your edits back as local), `rig pkg yank <pkg>
--from internal` (already current/merged — previous version restored from git history; a first
publish is removed). Both print what they re-anchored. Deployments elsewhere follow with
`rig registry sync && rig pkg upgrade` (preview first with `--dry-run`).
