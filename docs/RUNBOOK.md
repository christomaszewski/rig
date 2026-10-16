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

`rig bake` defaults the artifact name to the literal `images.tag` in `vehicle.yaml`.
Use `--tag` to name a particular snapshot independently of the image version, as below.
Missing or templated `images.tag` values require `--tag`; machine/tree-local image overrides
do not choose the artifact name. Neither form changes the runtime image tags.

```bash
rig bake --tag test1                       # auto-vendors surfaces + compose-only + digest-pins (cam-core:$JETPACK@sha256)
# rig bake                               # OR: use vehicle.yaml's images.tag as the artifact name
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
rig bake                            # uses images.tag; pass --tag <t> for another artifact name
# ship; on the vehicle: doctor, then a DRESS REHEARSAL —
./run.sh up --run rehearsal          #   a 3-minute run, then verify the run dir has bags/ +
./run.sh down --end-run              #   graph/<name>/epoch_*.yaml + camera video, and
rig graph <rehearsal-run> --check    #   the topology looks right. Cheap today, priceless tomorrow.
```

On the day: `up --run <label>` (label every session), `down --end-run` after landing (captures
docker logs, seals), copy the SEALED run dir off (`ended:` present = safe to sync — a slim
export first over a thin link, the full run later: next section), and leave disk headroom ≥ 2×
the expected bag+video volume.

## Getting runs off the vehicle — full copies and smaller bags

The end-to-end loop is: seal → transfer the whole run → reconstruct its deployment → restore
images → optionally swap target code → select a bag session → replay into target instances.
The examples below use `RUN` for a sealed run ID and `fieldtest` for its label; replace the
host, paths, vehicle and instance names with yours.

On the vehicle, from the deployment directory:

```bash
rig down --end-run
rig runs
```

Copy the entire `<data_dir>/runs/RUN/` directory, including `.rig/`: `manifest.yaml` identifies
the run; `bags/` holds telemetry; `recordings/<instance>/` holds camera video and sidecars;
`graph/` holds the observed topology; `.rig/artifact.tar.gz`, `.rig/config/` and
`.rig/images.yaml` carry the deployment, config states and best-effort image identities.
Copying just the bags loses the context needed to reconstruct and select inputs precisely.

**Full transfer, without recompressing first.** From the bench, a fleet roster with the
vehicle's SSH `host`, deployment `path` and `data_dir` lets rig harvest sealed runs:

```bash
rig fleet sync skiff-07 --fleet fleet.yaml --label fieldtest --into fleet-runs
# Result: fleet-runs/<label>/<vehicle>/<run-id>/
```

Without a fleet roster, copy one run directly (these are alternatives):

```bash
mkdir -p runs
rsync -a --partial --exclude='/exports/' orin:/data/rig/runs/RUN/ ./runs/RUN/
# Or, for a first copy:
scp -r orin:/data/rig/runs/RUN ./runs/
```

Copy the directory itself, not `RUN/*`, which misses `.rig/`. Fleet sync uses rsync when
available at both ends: transfers resume and existing copies are updated. Its scp fallback
can pull a run once but cannot update it in place. Full fleet pulls exclude the run's redundant
`exports/` copies. `ended:` must be present for fleet sync to harvest a run.

**Recompress before transfer, keeping the original.** Video and MCAP chunks are normally
already compressed, so whole-directory gzip usually saves little. The bag logger can rewrite
its data using a denser preset. Declare a compression-only recipe in the vehicle's
`vehicle.yaml` to retain all topics and camera files (rig ≥ v0.2.54, rig-infra ≥ v1.14.0):

```yaml
export_profiles:
  replay:
    ros2-bag-logger: {preset: zstd_small}
```

```bash
# On the vehicle: produces <run>/exports/replay/; --dry-run previews it.
rig run export RUN --profile replay
# On the bench: --export makes missing exports on the vehicle before pulling them.
rig fleet sync skiff-07 --fleet fleet.yaml --label fieldtest --into fleet-runs \
  --profile replay --export
```

Kept files are hardlinked where possible; rewritten bags consume additional disk space.
A smaller, lossy `review` profile may omit video or drop topics (CHEATSHEET §1.6). Use it only
when those inputs are unnecessary for the intended replay:

```bash
# on the vehicle (or from the bench through fleet): a slim copy under <run>/exports/review/,
# hardlinked where kept (no disk cost), video omitted, bags re-written at zstd_small minus the
# point clouds by the bag logger's own `export` verb (rig-infra ≥ v1.14.0)
rig run export <run|label> --profile review        # --dry-run shows kept/omitted + the exporters
rig fleet sync --into fleet-runs --profile review --export   # make it there if missing, pull it
rig fleet sync --into fleet-runs                   # later, on a fat link: complete the SAME
                                                   #   copies to full runs (rsync adds what the
                                                   #   slim pull lacked; a full copy is never
                                                   #   downgraded by a later --profile pull)
```

**Reclaiming disk in place** (rig ≥ v0.2.59, rig-infra ≥ v1.15.0). An export is a COPY — the
run gets bigger by it. To shrink the run itself, the same exporters rewrite their data inside it:

```bash
rig run export <run|label> --in-place --dry-run   # what would be rewritten, and its size now
rig run export <run|label> --in-place             # lossless: every session re-written at
                                                  #   zstd_small, per-topic message counts checked
                                                  #   against the original's metadata, THEN swapped
rig run export <run> --in-place --profile review --lossy   # a profile's exclude/topics/window
                                                  #   applied for good — refused without --lossy
```

One session converts at a time beside the original (free space for one is checked first); a
session that fails to convert or verify stays exactly as it was, and a crash between the two
renames of the swap is repaired on the next run. `omit` globs never apply in place — nothing is
deleted. Runs recorded BEFORE this existed work as they are (a rosbag2 session with its
`metadata.yaml` is all it needs), and so do runs from another deployment or an instance since
renamed: `bags/<other-name>` is rewritten by this tree's logger row under the run's own name.
A session the recorder never closed (no `metadata.yaml`) is skipped with the fix named. The run
manifest records every rewrite (`rewrites:` — options, bytes before/after, lossy or not), which
is also why a second `--in-place` with the same options is a no-op (`--force` redoes it). A
multi-file session becomes one file unless the profile sets `split_duration_s` / `max_size_mb`.

An export is a plain run dir (manifest, config snapshots, graph epochs, sidecars) plus
`.rig/export.yaml` naming the profile, the source run, what was omitted and each exporter's
outcome — `rig run import` adopts it like any run, `rig replay` plays what the slim bags hold.
Transport is rsync (resumable; on a laptop without it, scp pulls once and cannot update). The
camera services do not export yet: video is either omitted (`omit:`) or travels as recorded.
After an in-place rewrite, use the ordinary full-transfer command above. The exporter lives in
the logger's launcher/image: installing a newer rig alone does not upgrade an old vendored logger.
An export that drops target inputs cannot reproduce their behavior. In particular, a profile
that omits `.mkv` files but keeps camera sidecars still needs `--skip-service camera-service`
when replaying without video; source discovery sees a nonempty recordings directory.

## Reconstruct the deployment and restore its images

With `run_capture` on (the default), opening a run attempts to capture the tree that ran in
`.rig/artifact.tar.gz` (launch surfaces + configs + rig; sha-stamped). Capture is fail-soft, so
check that the archive exists. Reconstruction needs the complete run directory and no existing
deployment; image availability is a separate step below.

```bash
rig reconstruct /absolute/path/to/RUN --into flight1-tree/ --copy-run
#   … --registry localhost:5000                      # + images.registry for THIS tree only
#   … --enable-replay ~/ws/rig-infra                 # a tree that flew without the player row
cd flight1-tree
./rig doctor
```

The destination must be absent or empty. Rig verifies the archive hash when stamped, restores
the deployment and writes a tree-local `vehicle.local.yaml` with `data_dir` pointing at
`flight1-tree/var/data`. Native captures default to the state when the run opened;
`--config <digest12>` overlays a verified snapshot named in the run manifest's `ups:` log.

`--copy-run` copies the source into `var/data/runs/RUN/`, so `./rig replay RUN …` resolves it
without a separate `run import`. Omitting the flag creates a symlink to the archive instead,
saving space for bag-only experiments. **Use `--copy-run` for camera recordings:** camera
containers mount the workspace data root and cannot follow a link to recordings outside it.
Replay by the copied run ID after changing directories; passing the original archive path
would bypass the copy. `--no-import` opts out of either form of registration. Copying duplicates
the recordings, so allow disk space for the source plus new replay output.

A tree that FLEW before the player row existed reconstructs without it — reconstruct says so and
never injects it silently (the output is the tree that ran). `--enable-replay <ros2-bag-player
dir | rig-infra checkout | public/ros2-bag-player@1.10.0>` wires it on request: the route,
`config/autonomy/bag_player.yaml`, and the `enabled: false, order: 999` row (the registry form
also pins it in rig.lock). Harness only — the player is never in a with-set and absent from the
config-drift report. A capture that already carries the player row needs no flag; the flag is
then a no-op. The bundled `./rig` is the captured version; use a newer installed `rig` from
inside the reconstructed directory when you need a CLI feature absent from that version.

**Restore runtime images before replay.** Native run captures contain no Docker image bytes.
The source run's `.rig/images.yaml` maps image refs to locally observed `repo@sha256:…`
identities, best-effort: entries can be null and the file can be absent. `reconstruct` neither
pulls these images nor rewrites launcher image refs to those digests. `--registry HOST` changes
the registry host for the tree; it does not populate the mirror or restore image content.

For a replay using the deployment's current image tags, pull the required instances. Include
disabled targets and the disabled player explicitly; bare `pull` covers enabled instances only:

```bash
./rig pull                               # enabled instances
./rig pull bag_player planner            # example disabled player/target instance names
./rig config bag_player                  # inspect the rendered image refs
./rig config planner
```

For the recorded image bytes, inspect `var/data/runs/RUN/.rig/images.yaml`, pull each required
non-null digest, and make the corresponding launcher use it. Prefer a service's full-image-ref
override when available. For a launcher that uses tags, an alternative is to tag the pulled
image with the exact ref shown by `./rig config <instance>`:

```bash
# Replace these placeholders with one recorded digest and its rendered runtime ref.
docker pull REGISTRY/IMAGE@sha256:DIGEST
docker tag REGISTRY/IMAGE@sha256:DIGEST RENDERED_IMAGE_REF
```

In the tag-based case, a later `pull` or launcher pull policy can replace the restored image;
check the rendered compose and the launched container's image identity. Null/missing digests
cannot prove the original bytes, and an unavailable digest requires an image archive or a
rebuild. An arm64 run needs a compatible host, emulation where supported, or a platform rebuild;
rebuilding at the same source pins is a new image, not restoration of the recorded bytes.

Runs recorded before capture can be retrofitted: `rig run retrofit <run…>` in the original
deployment, with `var/artifacts/` populated, stamps them with the deploy artifact their manifest
names. Reconstruction defaults to overlaying the LAST `ups:` config snapshot for these runs,
because that artifact is as-shipped. If neither a capture nor a matching deploy artifact exists,
the saved config layer alone cannot reconstruct the service launch surfaces.

**Swapping the code under a reconstructed tree** (rig ≥ v0.2.48) is the SIL loop itself: the tree
is what ran, and the experiment is the same config and the same data against a newer service.

```bash
./rig swap planner /absolute/path/to/planner   # local checkout: edit, rebuild, re-replay
./rig swap planner public/planner@2.1.0        # alternative: registry version, vendored + pinned
```

`swap` takes the **service key** from `services.yaml` (the row's `service:`), whereas `replay`
takes **instance names** (the row's `name:`). They happen to match in the planner example.
Swapping changes the source route/launch surface/pin; it does not build or pull runtime images.
Build/pull the new service image and ensure its rendered runtime ref points at that build
before replaying. Captured launch surfaces need not contain a complete image build context.

Rows, working configs, config pins and overlay bindings are untouched — keeping the run's config
is the point. Every instance of that service moves, and they are named in the output. rig reports
config-key drift against the new version's example config without gating on it; `--reset-config`
re-materializes the configs from the new example instead, discarding your edits. The undo is
`rig reconstruct` again. Configs in a reconstructed tree sit in their tier's directory
(`config/infra|sensors|autonomy`), the same layout `rig add` writes, so the two never collide.

Camera recordings and old runs (rig ≥ v0.2.53): a run whose recordings already sit under
`<run>/recordings/<instance>/` — every run since camera-service recorded into the registry —
needs no recording retrofit: preserve the video AND JSON/CSV sidecars, use the copied workspace
run, and use a camera-service version whose descriptor declares `replay.source` and whose image
supports playback. Rig discovers those recordings automatically; older sidecar headers (no
session stamp) order by file time. Recordings made BEFORE that, in a flat `/data/recordings` or
an instance directory outside any run, are adopted with
`rig run retrofit <run> --recordings <instance>=<dir>`: the sessions
that began inside the run's `started..ended` window are MOVED under the run (`--copy` to copy,
`--all-sessions` to ignore the window), the manifest records `retrofit.recordings` and lists the
instance in `stacks`. Two independent retrofits, one verb. A RECONSTRUCTED tree from an old run
carries camera-service as it ran — a vendored rigging without `replay.source` and an image that
cannot replay. Replace that service, then build/pull its playback-capable images:

```bash
./rig swap camera-service /absolute/path/to/current-camera-service
# After preparing its images, name a camera INSTANCE to test it against recorded video:
./rig replay RUN cam_front --dry-run
```

The swap affects every camera-service instance, including `cam_front`; `swap cam_front` is not
valid unless that is itself a service key. Some older replay diagnostics suggest an instance
in the swap command; use the service key instead. For a bag-only experiment, pass
`--skip-service camera-service` to omit all camera instances and their bag topics. Do not skip
cameras when the target needs their inputs.

## SIL replay — test a service change against a recorded run (rig ≥ v0.2.33)

Run these commands from the reconstructed deployment. For bag replay, it needs a
`ros2-bag-player` row in vehicle.yaml (`autonomy:`, `enabled: false`, high `order` —
rig-infra ≥ v1.8.0) and a sealed source run with bags. Named targets are **instance names** in
that tree, including disabled instances; do not name the player, which rig adds automatically.
Targets execute the tree's current code/config, with recording-capable sources fed from their
own files as described below. Rig starts enabled infra (including the logger), detected sources,
targets, and the player last; it does not start every enabled hardware driver.

The player selects the targets' recorded subscriptions minus publications regenerated by all
running instances (targets, infra and recording sources). This prevents replaying their old
outputs alongside new ones. Missing graph epochs or unobserved target instances trigger a
warned namespace heuristic. Preview the selection and config-drift report before starting:

```bash
./rig down --end-run                       # finish any prior session in this deployment
./rig replay RUN planner controller --dry-run
./rig replay RUN planner controller --auto-end
./rig runs                                # new run under var/data/runs/; REPLAY-OF links the source
```

The examples assume `planner` and `controller` exist in `vehicle.yaml`. Replay refuses while
this deployment's stacks are running; do not work around that with `--force` for normal SIL.
`--auto-end` waits for the bag player to finish, allows 10 seconds for consumers to drain
(or specify `--auto-end 30`), then tears down and seals the new run. It needs a bag player and
`play.loop: false`; it does not wait for every camera session to finish. For manual teardown,
or a source-only replay with no bag player, use `./rig down --end-run`.

By default the player publishes `/clock` (`RIG_SIM_TIME=1`) — services whose launchers adopted
sim-time pace to bag time; `--wall-clock` disables both sides at once. Replay preflight WARNs per
service under test whose rigging lacks `replay: {sim_time: true}` (the launcher's adoption
promise; the bag-logger adopted in rig-infra v1.9.0, so replay-run bags record on bag time and
the A/B pair aligns).

**Per-sensor sources (rig ≥ v0.2.52).** An instance whose service declares `replay.source` in
its rigging (camera-service: `recordings/{name}`, its mkv + csv sessions) is a SOURCE whenever the
source run holds its recordings: it comes up fed from them, its config rendered for the session
with the descriptor's patch on top (`var/rendered/replay/<name>.yaml`; the next plain `up`
renders the live config again — nothing to undo). Sources are found, never named. With no names
`rig replay <run>` REPRODUCES the run: infra (the dashboard shows what the operator saw), every
source, and the player for what the bag holds — nothing under test; with names the names are
under test as before and the sources ride along (a named source is still a source: the
new-camera-service-against-this-run's-video case). Every source shares one timeline — the bag's
zero (no bag: each starts on its own first recorded frame), one release instant (`--start-delay`,
default 20 s: sources come up paused on their first frame and resume together), `--from/--to`,
and the clock decision (recorded stamps under sim
time, retimed onto now under wall clock; no bags ⇒ no player ⇒ wall clock). `--live NAME` forces
an instance live although its recordings exist (HIL); `--session PREFIX` pins one recorded
session for every source, not the bag player. The manifest records `replay.sources`, `clock`,
`epoch_unix_ns`, `start_at_unix_s`. A run with instance recordings and no bags replays without
a player row.

**Select the bag session explicitly when a run contains more than one.** A run can span logger
restarts and contain multiple sessions under `bags/<logger>/`. The player config, normally
`config/autonomy/bag_player.yaml` (check its row's `config:`), defaults to:

```yaml
source:
  run: ""                 # rig supplies the selected run at replay time
  logger: bag_logger      # source run's bags/<logger>/ directory
  session: latest         # lexically greatest stamped session, NOT all sessions
```

Inspect `var/data/runs/RUN/bags/`, set `source.logger` to the recorded logger name, and set
`source.session` to the desired UTC stamp to play an earlier session. Each session's split
files play together; separate sessions require separate replay invocations. The player's
warning lists skipped sessions. `rig replay --session PREFIX` selects per-source recordings
(such as camera sessions) and does not override this bag selection. `--export-calls` also
uses the player's selected bag session, so keep the selection consistent with the replay.

For multiple bag sessions or loggers, verify camera/bag timestamp alignment: rig currently
derives the source timeline epoch from the earliest bag metadata anywhere in the run, whereas
the player chooses one session. Selecting a later bag session does not automatically rebase
the camera epoch. The shared release gate also requires a player launcher that honors
`RIG_REPLAY_START_AT_UNIX_S`; older captured players can start with container-start skew.

To inspect recorded behavior without recomputing target services, use `./rig replay RUN`
(no target names): infra and sources run, and the player supplies the remaining recorded
topics. For a run whose video was omitted:

```bash
./rig replay RUN --skip-service camera-service --auto-end
```

This excludes camera instances and their bag topics; it cannot supply missing video to a
target that needs it.

**Windows** (rig ≥ v0.2.46 + rig-infra ≥ v1.12.0): `--from S` / `--to S` replay a SECTION of the
recording — seconds from the selected BAG SESSION's start, the same zero as call-script `t`,
so a script means the same thing under any window. The player seeks there AND restores the
latched topics (`/tf_static`…) a
bare seek would skip; it exits at `--to`, so `--auto-end` composes and section sweeps are a loop:

```bash
for w in 0:120 120:240 240:360; do
  rig replay <stamp>_fieldtest planner --from ${w%:*} --to ${w#*:} --auto-end
done
```

The window is recorded as `replay.window` in the new run's manifest. Scripted calls outside it
are skipped by the injector (rig says how many up front; `t == from` fires at window start).
A window cuts service STATE — a mode set at 119.5 s is gone at `--from 120`; pick quiescent
starts, or script the state-setting calls at `t: 120`.

**Service calls** (rig ≥ v0.2.37 + rig-infra ≥ v1.10.0, sources recorded with `record.services`
and CONTENTS-level introspection adopted): by default replay also re-issues the recorded calls at
the with-set's servers — observed `provides` minus `requires`, the topic rule's twin. rig says
up front whether the source run recorded ANY service events (from the bags' metadata) and why
verbatim replay is or isn't armed — a run recorded before `record.services` was on and the
servers ran introspection holds no calls to replay, record-time-or-never. To RETIME
or INJECT calls instead, pass a call script (script XOR verbatim — never both):

```bash
rig replay <run> --export-calls > calls.yaml
```

```bash
rig replay <run> planner --calls calls.yaml
```

Edit `calls.yaml` between the two — move a `t:`, add a new call — the injector executes it on
the sim clock (so the player config's `play.rate` scales it) and appends each result to the NEW
run's `calls/<name>/results.yaml`; the script itself is copied + hashed into the replay run
(`.rig/replay-calls.yaml`). Responses go
to the player/injector, not to original callers — this tests SERVERS.

The A/B artifact: the SOURCE run's bag holds the original outputs, the replay run's bag holds the
new ones, and `replay:` in the new run's manifest links them. `rig graph <replay-run>` shows the
SIL topology. Known limits (by design): `/tf` is shared-bus — the bag replays the ORIGINAL
stack's transforms when selected, and frames the service under test re-publishes can conflict
(the player config's `play.exclude` is the hatch). Recorded service requests can exercise
servers as above; replies are not routed back to original callers, and this workflow does not
reconstruct a complete service/action interaction. Missing service events cannot be recovered
after recording, though a call script can inject new requests. Reconstructed launch surfaces
and configs do not enforce recorded image digests or guarantee a deterministic rerun; restore
and verify images separately, and treat code/config changes as the experiment.

## Optional run registry and archive management

Bench/laptop registry setup is one command (the registry itself is minted lazily on first use;
it belongs to `data_dir`, not to the machine or the rig install):

```bash
rig setup --data-dir ~/rig-data             # THIS USER's registry, no sudo (a first `rig setup` asks);
sudo rig provision --data-dir /data/rig     #   or the machine's; or a tree-local vehicle.local.yaml
rig setup --show                            # where everything is, one screen
sudo rig provision --registry localhost:5000  # the bench's image mirror (images.registry, machine-wide;
                                            #   each deployment's tag/base pins stay — only the host swaps)
```

`rig run import <path…>` adopts scp'd/downloaded runs into the registry (copy by default) so
id-based verbs and TAB completion cover them; `rig run rm <id…>` reclaims disk (sealed runs
freely, interrupted with --force, the OPEN run never).

**Where imports go, and who sees them** (rig ≥ v0.2.55). The registry is the box's — this
user's (`rig setup --data-dir`) or the machine's (`sudo rig provision --data-dir`) — so a run
imported from any deployment on the laptop is found from every deployment on it. A tree with its OWN `data_dir` in `vehicle.local.yaml` —
a reconstruct workspace, a bench experiment — keeps its runs (and its replay sessions) to itself
and still sees the machine registry read-through: `rig runs` prints its own table and then the
host's, `rig replay <id|label>` and TAB resolve both, `run rm` and `import` only ever touch its
own. The reverse never holds: a deployment on the machine registry does not see any workspace's
runs. To FIND a run wherever it sits — machine registry, workspaces, `fleet sync` trees, an
archive disk — `rig catalog`:

```bash
rig run tag <run|label> site:mojave event:demo-day   # tags live in the run's manifest: they travel
rig catalog --tag site:mojave --since 2026-08         # …with every copy; `site:` = any site tag
rig catalog survey --vehicle skiff-07 --paths         # free text; --paths feeds any run verb
rig catalog roots / add <dir> / remove <dir>          # rig remembers what it touches; add a disk
```

**When the collection outgrows the disk** (rig ≥ v0.2.56). Recording must stay on a LOCAL disk:
the registry relies on symlinks (`current`), hardlinks (exports) and atomic renames, none of
which SMB/NFS honor, and bags write at disk speed. Two moves, for two situations:

```bash
rig setup --data-dir /Volumes/ssd/rig-data --migrate   # a bigger LOCAL disk: the whole registry
                                                       #   moves (hardlinks kept, verified), a
                                                       #   symlink stays at the old path so
                                                       #   workspace links keep resolving
rig run archive <id…> --to /Volumes/nas/runs           # a drive or NAS for OLD runs: the bytes
                                                       #   move to <to>/<vehicle>/<id>, a LINKED
                                                       #   entry stays — runs/replay/TAB still
                                                       #   resolve it, `run rm` unlinks only, an
                                                       #   unmounted drive lists as `dangling`;
                                                       #   the root joins `rig catalog`
```

macOS note: stacks bind-mount the registry, and Docker Desktop shares only `/Users` by default —
add an external volume under Settings → Resources → File sharing before recording or replaying
there.

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
