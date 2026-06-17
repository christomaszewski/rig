# Runbook — deploy a 4-stack vehicle to an Orin from a local registry

A concrete, copy-pasteable walkthrough for the test deployment: **two camera-service instances (USB + RTSP)
+ the dashboard + a zenoh router**, built on a dev box, pulled onto a Jetson Orin from a local registry.
Set the placeholders, then work top to bottom. The only files you author by hand are the two camera configs.

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
export JETPACK="jp7"                     # the Orin's JetPack: jp7 or jp6
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
rig --version                              # -> rig 0.1.22
```

## 3 — Scaffold the deployment
```bash
rig init my-vehicle && cd my-vehicle      # scaffolds config/{infra,sensors}/ + the manifest files

cat > services.yaml <<'EOF'
services:
  zenoh-router:   { path: ../rig/templates/zenoh-router }
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
  tag: "$JETPACK"             # -> RIG_IMAGE_TAG; platform-specific composes pull <repo>:<tag>; rig build uses it
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
rig doctor          # vehicle 'orin-test' (id 7) · domain 7 · rmw_zenoh_cpp · 4 sensors · 0 errors, no zenoh warning
rig certify         # each launcher honors the contract: project name, registry/tag, ROS env, determinism, identity
rig up --dry-run    # zenoh-router -> dashboard -> cam_usb -> cam_rtsp; VEHICLE_ID=7, RIG_IMAGE_TAG=jp7 on each
```

## 5 — Build + push images
```bash
rig build -j 3      # builds cam-core:$JETPACK (tag from images.tag) + dashboard images; mirrors eclipse/zenoh
curl -s http://$REGISTRY/v2/_catalog       # expect: cam-core, dashboard-zenoh, dashboard-web, eclipse/zenoh
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
# re-deploy after a change: edit configs -> rig build (if images changed) -> rig bake --tag test2 -> scp -> tar xzf -> ./run.sh up
docker rm -f registry                                      # stop the dev-box registry when done
```

---

## Notes & prerequisites

**Platform image tag (`images.tag`).** It's a *vehicle-level* property (the Orin's JetPack), so it lives in
`vehicle.yaml`, not the per-sensor config. rig exports it as `RIG_IMAGE_TAG`; the camera compose pulls
`cam-core:${RIG_IMAGE_TAG:-latest}`, and `rig build` defaults its `--tag` to it (so build + pull agree on
`cam-core:jp7`). The **dashboard / zenoh-router are platform-agnostic** — they ignore `RIG_IMAGE_TAG` and use
their own tag (the dashboard defaults to `arm64`). For one coherent deployment tag, have the dashboard compose
also fall back through `RIG_IMAGE_TAG` (`dashboard-zenoh:${RIG_IMAGE_TAG:-arm64}`); otherwise confirm its
`build-images.sh` and compose agree on `arm64` so `rig build`'s `jp7` arg doesn't produce an unpullable tag.

**Run the cameras via rig, not compose-only.** The camera also has a *runtime overlay*
(`docker-compose.jp7.yml`, runc + CDI NVENC) that `cam-up` applies **per host**. Baked on the dev box, the
compose-only form would capture the *dev box's* host detection — wrong for the Orin. Installing `python3-yaml`
on the Orin makes `run.sh` use the bundled rig + `cam-up`, which detects JetPack **on the Orin** and applies
the right overlay. (zenoh-router / dashboard don't care.)

**Multi-instance safety.** Each camera entry has a unique `name` → its own compose project
(`camera-service_cam_usb`), ROS namespace (`/cam_usb`), and shm volume (`cam_cam_usb_sock`); same internal
`socket_path` is fine. With the `ros2-bridge`-only configs above there's no host-facing port to clash. **If
you enable `webrtc-bridge` on both cameras**, give each a distinct signalling port and have camera-service
declare `host_ports: ["plugins[name=webrtc-bridge,enabled=true].params.port"]` — then `rig doctor` validates
the ports across instances (and against the dashboard's 8080/10000).

**Optional infra** (beyond this 4-stack example): rig ships ready-to-use `templates/ros2-bag-logger/` and
`templates/ros1-bag-logger/` — add one to `services.yaml` + an `infra:` entry (order ~1, just after the
router) to record the ROS telemetry graph to `${RIG_DATA_DIR}/bags/`. See the templates' example configs.

**External prerequisites** (outside rig):
- **camera-service** supports `camera.type: usb | rtsp | gige` — copy the real source keys from its
  `core-driver/config/usb-real.yaml` / `rtsp-real.yaml` examples into your configs.
- **dashboard** ships `tools/build-images.sh <registry> [tag]` and an `infra:`-tier `rigging.yaml` (both
  done). It has no public git remote yet — get the repo from its maintainer.
- Images must be **arm64** (the Orin) and reachable in the registry; the Orin caches them after the first
  pull, so it runs offline thereafter — or use `rig bake --bundle-images` to skip the registry entirely.
