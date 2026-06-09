# Host setup

`rig` orchestrates; it deliberately does **not** mutate host state (`rig doctor` only *checks*). The
following are one-time, out-of-band host steps.

## Prerequisites

- **Docker + Compose v2** (`docker compose version` ≥ 2.20 for `include:`).
- **Python 3 + PyYAML** on the host — every launcher's `render_params.py` / `sensor_env.py` needs it, and
  so does `rig`.
  - Robot/Linux: `sudo apt install python3-yaml`.
  - Dev box (PEP 668 / externally-managed): `python3 -m venv .venv && .venv/bin/pip install pyyaml`, then
    run `.venv/bin/python rig …` and export each launcher's interpreter:
    `export NOVATEL_PYTHON=$PWD/.venv/bin/python SBG_PYTHON=… GIGE_PYTHON=…`.

## Wiring the service repos

For **deployment**, vendor each service as a git submodule so its launcher + compose are pinned on the
robot (runtime images are pulled from the registry, not built there):

```bash
git submodule add <url> services/gige-vision-service
git submodule add <url> services/novatel
git submodule add <url> services/sbg_driver
# then point services.yaml at services/<name>
```

For **local development**, point `services.yaml` at sibling checkouts (`../novatel`, …) — the default.

## Per-device host state (out of band)

- **Stable serial symlinks (udev).** Serial sensors must use a stable `/dev/serial/by-id/...` or a udev
  symlink (e.g. `/dev/sbg_imu`, `/dev/novatel_gnss`) — never `/dev/ttyUSB0`, which reorders across reboots.
  Each driver repo ships its udev rule:
  ```bash
  sudo cp services/sbg_driver/docker/udev/99-sbg.rules /etc/udev/rules.d/
  sudo udevadm control --reload-rules && sudo udevadm trigger
  ```
  Put the resulting symlink in the sensor config's `connection.serial.by_id`.
- **NovAtel Ethernet provisioning.** A receiver used over TCP/UDP needs one-time `ETHCONFIG`/`IPCONFIG`/
  `SAVECONFIG` over serial first — see `services/novatel/docs/HIL_BRINGUP.md`.
- **Jumbo frames (GigE cameras).** Set the camera NIC MTU to 9000 to match the camera's `packet_size`.

## ROS 2 graph

`rig` exports `ROS_DOMAIN_ID` + `RMW_IMPLEMENTATION` (from `vehicle.yaml`) to every launcher; all stacks
run `network_mode: host` + `ipc: host`, so they share one DDS graph on the host. Keep the distro aligned
(Lyrical) across services. At higher sensor counts, partition unrelated stacks onto distinct
`ROS_DOMAIN_ID`s and/or mount a Fast DDS XML profile (static peers) to tame discovery.

## Deploying a baked artifact (local registry / offline)

The vehicle pulls images from a registry it can reach (e.g. a local registry on your dev box) and runs a
baked, digest-pinned artifact — no driver source, no internet.

**Dev box:**
1. Get every image into the registry (build-from-source + mirror third-party). For services that declare
   `build:` / `mirror:` in their `rigging.yaml`:
   ```
   rig build --registry devbox:5000        # runs each service's build+push command + mirrors its 3rd-party images
   ```
   For a service that declares neither, push its (arm64) image yourself:
   `docker tag <local> devbox:5000/<repo>:<tag> && docker push devbox:5000/<repo>:<tag>`
   (or `docker buildx imagetools create` to preserve a multi-arch index).
2. Bake against that registry (digest-pins every image found in the local registry):
   ```
   rig bake --registry devbox:5000 --tag <name>      # -> var/artifacts/<name>.tar.gz
   ```
   (Or set `images.registry: devbox:5000` in vehicle.yaml and just `rig bake --tag <name>`.)
3. `scp var/artifacts/<name>.tar.gz  orin:/tmp/`

**Orin, one-time:**
- Docker; optionally `sudo apt install python3-yaml` (without it the artifact still runs via its compose-only `up.sh`).
- Trust the local registry — for a plain-HTTP LAN registry, `/etc/docker/daemon.json`:
  ```
  { "insecure-registries": ["devbox:5000"] }
  ```
  then `sudo systemctl restart docker`. (Or serve TLS + install the cert.)
- Plus the per-device host state above (udev serial symlinks, receiver provisioning, camera jumbo frames).

**Orin, deploy:**
```
rig unbake /tmp/<name>.tar.gz --into /opt/rig    # or just: tar xzf — it's a plain .tar.gz
cd /opt/rig/<name>
./run.sh up        # uses rig if Python+PyYAML present, else the static docker-compose scripts
./run.sh status
```
Images pull from `devbox:5000` by digest on first `up`, then cache locally → the vehicle runs offline
thereafter. To tweak on the vehicle, edit the unbaked tree and `./run.sh up`; `rig bake` again to capture a
new tagged artifact.

## Boot-time bring-up

Until a dedicated unit exists, a single oneshot systemd service can run `rig up` at boot (Compose's
`restart: unless-stopped` handles per-stack recovery thereafter):

```ini
# /etc/systemd/system/rig.service  (sketch)
[Service]
Type=oneshot
WorkingDirectory=/opt/rig
ExecStart=/opt/rig/rig up
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
```
