# Rigifying a service

How to take software that already runs in Docker and make it a Rig-compliant service: one
that `rig up` can start, `rig status` can observe, `rig bake` can ship and `rig certify`
passes. The [service contract](SERVICE_CONTRACT.md) is the reference for every rule; IDs
such as **I1** or **O4** below point into it.

Rigifying does not change what your software does. It adds three small files beside it and
adjusts the Compose file:

| File | Role |
| --- | --- |
| `rigging.yaml` | The descriptor: static metadata Rig reads. |
| `<service>-up` | The launcher: reads one instance config and runs Docker Compose. |
| an example instance config | The settings of one instance. Its schema is yours. |

The software never learns about Rig. The launcher keeps working when you run it by hand,
and that stays true at every step.

The guide follows one example throughout: `sounder`, an echo-sounder driver that today is
started with `docker compose up -d` from this file.

```yaml
# docker-compose.yml, before
services:
  driver:
    image: ghcr.io/acme/sounder:1.4
    build: .
    container_name: sounder
    network_mode: host
    environment:
      ROS_DOMAIN_ID: "7"
      RMW_IMPLEMENTATION: rmw_cyclonedds_cpp
      SOUNDER_HOST: 192.168.1.50
      SOUNDER_UI_PORT: "8080"
    volumes:
      - sounder_cal:/var/lib/sounder
      - ./logs:/logs
volumes:
  sounder_cal:
    external: true
```

Every listing for the finished service below was run through `rig certify` as shown.

## Before you start

You need Rig, Docker with Compose v2, and Python 3 with PyYAML on the machine where you
work. The generated launcher calls `python3` from `PATH`. If that interpreter has no PyYAML,
point the launcher at one that does with `<SERVICE>_PYTHON`, for example
`SOUNDER_PYTHON=/path/to/venv/bin/python`; `rig certify` otherwise fails its first check with
a `ModuleNotFoundError`.

For a brand-new thin ROS 2 driver, the `boilerplate` Copier template generates a repository
with a launcher and descriptor already in place. Certify what it generates like any other
service, and add whatever the checks ask for.

| Step | What you do | Done when |
| --- | --- | --- |
| [1](#1-take-stock) | List what is specific to one instance | You have the list |
| [2](#2-run-rig-rigify) | `rig rigify <dir>` | Three files exist |
| [3](#3-design-the-instance-config) | Move per-instance settings into a config file | The example config describes one instance |
| [4](#4-finish-the-launcher) | Render from the config, then run Compose | `<service>-up <config> config` prints a Compose document |
| [5](#5-fix-the-compose-file) | Take identity, images and ROS settings from the environment | Two instances render without sharing a name |
| [6](#6-finish-the-descriptor) | Declare what Rig needs to know | `rigging.yaml` lists the launch surface, volumes and ports |
| [7](#7-certify) | `rig certify --repo .` | No errors, and every warning understood |
| [8](#8-check-what-certify-cannot) | Review and run by hand | The checklist passes |
| [9](#9-make-it-buildable) | Add a build script and declare it | `--require-build-check` passes |
| [10](#10-run-it-in-a-deployment) | `rig add`, `rig doctor`, `rig up` | The instance runs and reports healthy |
| [11](#11-prove-it-survives-vendoring) | Certify the vendored copy | The launch surface is complete |
| [12](#12-gate-it-in-ci) | Add the certify job | CI fails on a contract break |
| [13](#13-publish-it) | Publish a registry package | `rig add <registry>/<service>` works |
| [14](#14-add-capabilities) | Adopt optional capabilities | Each declared capability works |

## 1. Take stock

Read how the software starts today and sort every setting into one of three groups.

- **Owned by the vehicle.** The ROS domain and RMW, the image registry and release tag, the
  target platform, where recorded data goes. Rig supplies these in the environment. Anything
  hard-coded here has to go.
- **Specific to one instance.** Device address, serial port, frame ids, ports, calibration.
  These move into the instance config.
- **The same for every instance.** The image contents, the entrypoint, the launch file. These
  stay where they are.

Then list what the stack touches on the host: published ports, named volumes, bind mounts,
devices, and directories it writes to. Each of these needs a per-instance name or a declared
place.

For `sounder`: the ROS settings and the image reference are vehicle-owned; the transducer
address and the UI port are per-instance; `container_name`, the external volume and the
`./logs` bind are host resources that two instances would fight over.

## 2. Run `rig rigify`

```bash
rig rigify ../sounder --tier sensor
```

```
rigify: …/sounder -> service 'sounder' · tier: sensor
  found: docker-compose.yml — pre-wired into the launcher's compose file list
  found: external volume(s) ['sounder_cal'] — declare `external_volumes:` (with {name}) …
  found: build inputs (Dockerfile, compose service 'driver') — declare `build:` in rigging.yaml …
  found: entry script(s) ['start.sh'] — your current launch path? …
  wrote: rigging.yaml, sounder-up, config/sounder.example.yaml
```

`rigify` reads the directory and writes `rigging.yaml`, an executable `<service>-up` and
`config/<service>.example.yaml`. If it finds no Compose file it also writes a starting
`docker/compose.deploy.yaml`. It only adds files and never overwrites one. `--service NAME`
sets the service name when the directory name is not what you want, and `--tier` records
whether the service is `infra`, `sensor` or `autonomy`.

The generated launcher already satisfies the mechanical parts of the contract: it takes the
config path as its first argument, reads `name` from it, honours `COMPOSE_PROJECT_NAME`,
forwards verbs to `docker compose` and keeps its own messages on stderr. Everything it could
not decide is a commented hint in `rigging.yaml` or a `TODO` in the launcher.

`rig certify` already runs at this point. For `sounder` it reports what the scaffold could
not fix:

```
rig certify: sounder
  [✓] 6 ok: discipline, project-name, registry, determinism, identity, status
  [!] registry: upstream image(s) not under RIG_IMAGE_REGISTRY …: ['ghcr.io/acme/sounder:1.4']
  [✗] ros-env: fleet ROS env reaches containers with the wrong value …: ["driver: ROS_DOMAIN_ID='7'", "driver: RMW_IMPLEMENTATION='rmw_cyclonedds_cpp'"]
  [!] binds: directory bind(s) into the service repo …: ['driver: …/sounder/logs']
  [✗] fixed-names: container_name does not change with the instance name …: ['driver: container_name=sounder']
rig certify: 1 target(s) — 2 error(s), 2 warning(s)
```

Certify says nothing about the `sounder_cal` volume, which every instance would share: it is
`external: true`, and certify takes that as deliberate. The remaining steps fix all of these.

## 3. Design the instance config

Rig reads two keys from an instance config, `service` and `name`. The rest is yours.
Everything from the "specific to one instance" list goes here.

```yaml
# config/sounder.example.yaml
service: sounder
name: sounder                 # unique on the vehicle; letters, digits and underscores
connection:
  host: 192.168.1.50          # the transducer's address
  port: 9000
ui:
  port: 8080                  # host port of the status page
driver_params:                # passed to the driver node as ROS parameters
  ping_rate_hz: 10
  frame_id: sounder
```

Guidelines that follow from how Rig handles this file:

- **Make the example complete.** It is the documentation of your config surface, the default
  that `rig certify` drives the launcher with, and the file `rig add` copies to seed a new
  instance (**S5**, **C7**).
- **Keep `name:` on a line of its own at the top level.** `rig add` comments that line out so
  the `vehicle.yaml` row supplies the name, which lets several instances share one file.
- **Prefer mappings to lists for anything an operator will override.** A `vehicle.yaml`
  `overrides:` block is deep-merged over the config: mappings merge, lists are replaced
  whole, and `null` deletes a key (**C4**).
- **Use absolute paths, not paths relative to the config file.** The launcher often receives
  a rendered copy from another directory (**C6**).
- **Do not use `{{…}}` in values.** That is Rig's variable syntax (**C5**).

## 4. Finish the launcher

A launcher does the same things in the same order every time:

1. Take the config path, check it exists, make it absolute.
2. Read the instance name and any settings Compose will interpolate.
3. Render per-instance files, such as a ROS parameter file.
4. Set the Compose project name only if nothing set it.
5. Do setup that changes the host, on `up` only.
6. Hand the verb to `docker compose`.

Put the parsing in a small helper so the shell never interprets config values:

```python
#!/usr/bin/env python3
"""tools/render_params.py — turn an instance config into what the launcher needs.

  render_params.py --env CONFIG      print KEY=VALUE lines for the launcher
  render_params.py CONFIG OUT        write the driver's ROS parameter file to OUT
"""
import re
import sys

import yaml

SAFE = re.compile(r"^[a-z][a-z0-9_]*$")


def load(path):
    with open(path) as handle:
        cfg = yaml.safe_load(handle) or {}
    name = str(cfg.get("name") or "")
    if not SAFE.match(name):
        sys.exit(f"sounder: config `name` must match [a-z][a-z0-9_]*, got {name!r}")
    return cfg, name


def main(argv):
    if len(argv) == 3 and argv[1] == "--env":
        cfg, name = load(argv[2])
        port = int((cfg.get("ui") or {}).get("port", 8080))
        print(f"SOUNDER_NAME={name}")
        print(f"SOUNDER_UI_PORT={port}")
        return
    if len(argv) != 3:
        sys.exit(__doc__)
    cfg, name = load(argv[1])
    conn = cfg.get("connection") or {}
    params = dict(cfg.get("driver_params") or {})
    params.update(host=str(conn.get("host", "")), port=int(conn.get("port", 9000)))
    doc = {f"/{name}/sounder_driver": {"ros__parameters": params}}
    with open(argv[2], "w") as handle:
        yaml.safe_dump(doc, handle, sort_keys=True, default_flow_style=False)


main(sys.argv)
```

```bash
#!/usr/bin/env bash
# sounder-up <config.yaml> [verb...] — launch one sounder instance with Docker Compose.
# Human output goes to stderr; stdout belongs to compose (`config`, `ps --format json`).
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${SOUNDER_PYTHON:-python3}"

CONFIG="${1:?usage: sounder-up <config.yaml> [up [-d]|down|status|logs|config|pull]}"; shift || true
[ -f "$CONFIG" ] || { echo "sounder-up: config not found: $CONFIG" >&2; exit 1; }
CONFIG_ABS="$(cd "$(dirname "$CONFIG")" && pwd)/$(basename "$CONFIG")"

# Identity and settings come from the config, never from its file name.
TOKENS="$("$PYTHON" "$REPO/tools/render_params.py" --env "$CONFIG_ABS")"
while IFS='=' read -r key value; do
  case "$key" in SOUNDER_NAME|SOUNDER_UI_PORT) export "$key=$value" ;; esac
done <<<"$TOKENS"

# Render per-instance files under the service directory, keyed by instance name.
mkdir -p "$REPO/var/run"
export SOUNDER_PARAMS="$REPO/var/run/${SOUNDER_NAME}.params.yaml"
"$PYTHON" "$REPO/tools/render_params.py" "$CONFIG_ABS" "$SOUNDER_PARAMS"

# The orchestrator owns the project name. Never pass -p.
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-sounder_${SOUNDER_NAME}}"

ARGS=("$@"); [ ${#ARGS[@]} -eq 0 ] && ARGS=(up -d)
[ "${ARGS[0]}" = "status" ] && ARGS=(ps "${ARGS[@]:1}")

# Side effects only on `up`: `config` must change nothing on the host.
if [ "${ARGS[0]}" = "up" ]; then
  docker volume create "sounder_${SOUNDER_NAME}_cal" >/dev/null
fi

echo "sounder-up: project=${COMPOSE_PROJECT_NAME} name=${SOUNDER_NAME}" >&2
exec docker compose -f "$REPO/docker-compose.yml" "${ARGS[@]}"
```

Add `var/` to `.gitignore`.

Why each part is the way it is:

- **`REPO` from the script's own location.** Rig runs the launcher with the service
  directory as the working directory, but a person may run it from anywhere (**L2**).
- **The name comes from the config's `name` key.** Rig passes configs from several
  directories under several file names (**L1**, **C2**).
- **Assigning the helper's output to a variable first.** With `set -e`, a helper that fails
  stops the launcher there with the helper's own message. Reading only expected keys in a
  `case` means a config value is never executed as shell.
- **Rendering into `var/run/<name>…` under the service directory.** `rig bake` copies bind
  sources it finds under the service directory into the artifact; a file rendered to `/tmp`
  would be a path that does not exist on the vehicle (**O7**). Keying by instance name keeps
  two instances from overwriting each other when Rig starts them at the same moment (**L7**).
  The launcher must not write into files listed in the launch surface (**L8**).
- **Sorted, timestamp-free rendering.** `rig certify` runs `config` twice and compares the
  output byte for byte (**O3**).
- **The project-name fallback.** Rig sets `COMPOSE_PROJECT_NAME` for every call. The
  fallback applies only when you run the launcher yourself (**I1**).
- **Setup gated on `up`.** `config`, `status` and `logs` are read-only (**O5**). Nothing in
  the launcher may probe the host on `config`: no "is the device plugged in", no OS
  detection. A bake renders on your development machine for a vehicle that is somewhere else
  (**O4**).
- **`exec docker compose … "${ARGS[@]}"`.** Forwarding the arguments gives you `up -d`,
  `down`, `ps --format json`, `logs -f --tail N`, `config` and `pull` without writing any of
  them. `up` must return, so the default is `up -d` (**L3**).
- **One line to stderr.** Anything on stdout corrupts the output Rig parses (**O1**).

Select static Compose files from the config when an instance needs different wiring, such as
a serial-device overlay: append `-f "$REPO/docker/compose.serial.yaml"` conditionally. Do not
generate Compose files.

Docker Compose does not recreate a container when only the contents of a bind-mounted file
change. If operators will edit a config and run `rig up` again expecting the change to take
effect, have the launcher export a hash of the rendered file and put it in a container label
or environment variable, so the service definition changes when the file does.

## 5. Fix the compose file

```yaml
# docker-compose.yml, after
services:
  driver:
    image: ${SOUNDER_IMAGE:-${RIG_IMAGE_REGISTRY:+${RIG_IMAGE_REGISTRY}/}sounder:${RIG_IMAGE_TAG:-latest}}
    network_mode: host
    restart: unless-stopped
    environment:
      ROS_DOMAIN_ID: ${ROS_DOMAIN_ID:-0}
      RMW_IMPLEMENTATION: ${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}
      VEHICLE_ID: ${VEHICLE_ID:-}
      SOUNDER_NAME: ${SOUNDER_NAME:?set by sounder-up}
      SOUNDER_UI_PORT: ${SOUNDER_UI_PORT:?set by sounder-up}
    volumes:
      - ${SOUNDER_PARAMS:?set by sounder-up}:/etc/sounder/params.yaml:ro
      - cal:/var/lib/sounder
      - ${RIG_DATA_DIR:-/var/lib/sounder-data}:/data
    healthcheck:
      test: ["CMD", "/opt/sounder/healthcheck.sh"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 20s
volumes:
  cal:
    external: true
    name: sounder_${SOUNDER_NAME}_cal
```

| Before | After | Why |
| --- | --- | --- |
| `image: ghcr.io/acme/sounder:1.4` | The registry, tag and override chain | The vehicle pulls from the fleet registry at the release tag (**M1**), and one variable can pin a digest (**M4**). With nothing set it resolves to `sounder:latest`, so a local build still runs. |
| `build: .` | Removed | A vehicle pulls images and never builds. Building moves to a script ([step 9](#9-make-it-buildable)). |
| `container_name: sounder` | Removed | Two instances would collide. Compose names containers from the project (**I3**). |
| `ROS_DOMAIN_ID: "7"`, a fixed RMW | Passed through from the environment | Every stack on the vehicle must share one ROS graph (**E1**, **E2**). |
| `SOUNDER_HOST`, `SOUNDER_UI_PORT` literals | From the instance config, through the rendered parameter file and launcher exports | They differ per instance. |
| `sounder_cal`, shared by all | `sounder_<name>_cal` | Each instance needs its own volume, and Rig needs a pattern to clean up (**D5**). The volume key stays fixed and `name:` carries the instance, because Compose does not interpolate keys. |
| `./logs:/logs` | The whole data directory at `/data` | Outputs belong under `RIG_DATA_DIR`, not inside the service directory (**D1**, **D6**). |
| No restart policy or healthcheck | `restart: unless-stopped` and a readiness probe | `rig status` reports health from it (**H1** to **H3**). |

`${VAR:?set by sounder-up}` makes a bare `docker compose up` fail with a clear message
instead of starting something half-configured.

Three things happen inside the image, not in Compose.

**Write to the open run.** When `RIG_DATA_DIR` is set, Rig keeps a `current` link in it that
points at the open run. Resolve it once at start, from inside the container (**D2**):

```sh
# entrypoint
root=/data
[ -e "$root/current" ] && root="$(readlink -f "$root/current")"
out="$root/sounder/$SOUNDER_NAME"
mkdir -p "$out"
```

Mount the whole data directory, as above. `current` is a relative link, so it resolves only
when its target is inside the same mount.

**Namespace the ROS nodes.** Start nodes under `/<name>`, as the parameter file above
assumes. If the software must use another namespace, declare it as `ros_namespace:` in
`rigging.yaml` (**I5**).

**Probe readiness, not data.** A healthcheck answers "can this run": process up, configured,
device reachable. Probe something that belongs to this instance. On host networking every
node shares topics such as `/diagnostics`, so a probe of a shared topic can be answered by a
different instance (**H2**).

## 6. Finish the descriptor

```yaml
# rigging.yaml
service: sounder
launcher: sounder-up
tier: sensor
ros_distro: lyrical
examples: [config/sounder.example.yaml]

launch_surface:
  - sounder-up
  - tools/render_params.py
  - docker-compose.yml

external_volumes: ["sounder_{name}_cal"]
host_ports: ["ui.port"]
```

- **`ros_distro`** names the ROS distribution. With it declared, certify also warns when no
  container receives the fleet ROS settings. Leave it out for software that runs no ROS node. A vehicle runs one distribution;
  `rig doctor` reports a mismatch with `vehicle.yaml`.
- **`launch_surface`** lists every file the launcher needs at run time: itself, its helpers,
  every Compose file and overlay. Rig copies exactly these files to the vehicle, and nothing
  else from the repository is there (**S4**). [Step 11](#11-prove-it-survives-vendoring)
  tests the list.
- **`external_volumes`** takes the volume name with `{name}` where the instance name goes.
- **`host_ports`** takes paths into the instance config. `rig doctor` then reports two
  enabled instances that claim the same port.
- **`verbs`** is needed only if the launcher's command line differs from the defaults; see
  the [verb table](SERVICE_CONTRACT.md#42-verbs).

`mirror:` is for third-party images the stack runs unchanged. List the upstream reference,
for example `mirror: [foxglove/bridge:1.2]`, and reference it in Compose as
`${RIG_IMAGE_REGISTRY:+${RIG_IMAGE_REGISTRY}/}foxglove/bridge:1.2`. `rig build` copies it into
the fleet registry so the vehicle never needs the internet (**M2**).

## 7. Certify

```bash
rig certify --repo .
```

```
rig certify: using the declared example config/sounder.example.yaml
rig certify: sounder
  [✓] 9 ok: discipline, project-name, registry, ros-env, volumes, determinism, identity, fixed-names, status
  [·] data-dir: binds under RIG_DATA_DIR: ['/certify/data']
rig certify: 1 target(s) — 0 error(s), 0 warning(s)
```

Certify renders `config` with values that cannot be real — registry `certify.invalid:5000`,
tag `certify-tag-x`, instance `certifyname0` — and looks for them in the output. It starts
no containers. Without a Docker daemon it skips the `status` check and says so. Use
`--config <file>` to certify a different config; run it once for each config that selects a
different Compose overlay.

| Message | Cause | Fix |
| --- | --- | --- |
| `discipline: config exited N` | The launcher failed. The message carries its stderr. | Run `./<service>-up <config> config` by hand. |
| `discipline: non-compose top-level keys on stdout` | The launcher printed a human line to stdout. | Send it to stderr with `>&2`. |
| `discipline: stdout is not a compose document` | `config` printed something other than `docker compose config` output. | Forward the verb to `docker compose`. |
| `project-name: compose project is '…', not COMPOSE_PROJECT_NAME` | The launcher passes `-p`, or overwrites the variable. | Remove `-p`; set the variable only when it is unset. |
| `registry: images the service builds/mirrors are not pulled from RIG_IMAGE_REGISTRY` | A built or mirrored image is referenced without the registry. | Use the image form from step 5. |
| `registry: declared build image(s) pulled from somewhere other than <RIG_IMAGE_REGISTRY>/<image>` | A built image has a fixed registry, or a namespace between the registry and its name. | Use the image form from step 5; `rig build` publishes `<registry>/<image>` and nothing else. |
| `registry: upstream image(s) not under RIG_IMAGE_REGISTRY` (warning) | An image is pulled from outside the fleet registry. | Add it to `mirror:` and prefix it, or accept that the vehicle needs internet. |
| `tag: built image(s) pulled with a tag other than RIG_IMAGE_TAG` | A built image has a fixed tag, or the launcher adds a suffix. | Use `${RIG_IMAGE_TAG:-latest}` unchanged. |
| `build-images: no declared build.images entry is pulled …` (warning) | The Compose file and `build.images` name the image differently, so the registry and tag checks examined nothing. The message lists what is pulled. | Make the two names agree. |
| `build-images: declared but not pulled with this config …` (information) | This config does not select every image the service builds. | Nothing to fix. Certify again with a config that selects the others. |
| `image-overrides: declared image overrides ignored by: …` | A Compose service does not honour its `images.override_env` variable. | Wrap the image reference in `${VAR:-…}`. |
| `ros-env: … wrong value (hardcoded …)` | The ROS domain or RMW is a literal. Reported with or without `ros_distro`. | Pass both through from the environment. |
| `ros-env: no container env carries the fleet …` (warning) | No container receives the ROS settings. | Pass them through, or drop `ros_distro` if this is not a ROS service. |
| `volumes: declared external_volumes not in the compose` (warning) | The pattern and the Compose volume name differ. | Make the volume's `name:` match the pattern. |
| `binds: directory bind(s) into the service repo` (warning) | A directory inside the service directory is mounted. | Write outputs under `RIG_DATA_DIR`; mount rendered inputs as single files. |
| `determinism: two identical config runs differ` | A timestamp, random value or temporary path reaches the output. | Render to fixed paths; sort keys. |
| `identity: renamed to '…' but '…' persists` | Something still carries the old instance name. | Derive it from the config's `name`. |
| `fixed-names: container_name does not change with the instance name` | A `container_name:` is the same for every instance, so a second one cannot start. | Remove it. Compose names containers after the project. |
| `fixed-names: name(s) that do not change with the instance name` (warning) | A network or volume has a fixed `name:`, so every instance shares it. | Put the instance name in it, or mark it `external: true` if all instances are meant to share it. |
| `status: status --format json stdout is not JSON` | The status verb prints text, or does not forward `--format json`. | Forward the arguments to `docker compose ps`. |
| `build-no-cache: NOT CHECKED: …` (warning) | The build check could not run. | See [step 9](#9-make-it-buildable). |

## 8. Check what certify cannot

A clean certify run leaves these unproven. Go through them once.

- [ ] **Every shared resource is meant to be shared.** Certify accepts a fixed name on an
  `external: true` volume or network as deliberate. For each one, confirm that all instances
  really should use the same one. If not, put the instance name in it, and declare a volume
  in `external_volumes`.
- [ ] **Every image was checked.** Certify examines the images the config it ran with
  selects. If it printed `build-images: declared but not pulled with this config`, certify
  again with a config that selects those images.
- [ ] **No ROS setting is fixed outside the Compose file.** Certify reads each container's
  `environment`. Search the Dockerfile, the entrypoint and the launch files for
  `ROS_DOMAIN_ID` and `RMW_IMPLEMENTATION`.
- [ ] **It runs without Rig.** With no Rig variable set, `./<service>-up <config> config`
  renders and `up`, `status`, `logs -f`, `down` work.
- [ ] **`up` returns** and a second `up` changes nothing.
- [ ] **`down` leaves volumes and data alone** and removes every container.
- [ ] **`config` has no side effects.** `docker volume ls` and `docker ps -a` are the same
  before and after.
- [ ] **Two instances coexist.** Render two configs with different names and compare; start
  both if you can. Nothing collides: containers, volumes, ports, files under `var/run/`.
- [ ] **Nothing in `config` depends on this machine.** Run `rig certify --repo . --emit a.yaml`
  here and the same on the vehicle, then `rig certify --diff a.yaml b.yaml`.
- [ ] **Nothing depends on your shell.** A setting the launcher reads from an environment
  variable you happen to export will be missing when `rig bake` runs it with a clean
  environment. Settings come from the config, or from the deployment's `env:` map.

## 9. Make it buildable

`rig build` runs one command per service as `<command> <registry> <tag>` and expects it to
publish `<registry>/<image>:<tag>` for each image you declare.

Copy [`packaging/rig-build.sh`](../packaging/rig-build.sh) into the service, for example as
`tools/rig-build.sh`, and call it once per image:

```sh
#!/usr/bin/env sh
# tools/build.sh <registry> <tag> — build and publish the sounder image.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/rig-build.sh"
REGISTRY="${1:?usage: build.sh <registry> <tag>}"
TAG="${2:-latest}"
rig_build "$REGISTRY/sounder:$TAG" \
  --build-arg "BASE_IMAGE=${RIG_BASE_IMAGE:-ros:${ROS_DISTRO:-lyrical}-ros-base}" \
  -f "$HERE/../Dockerfile" "$HERE/.."
```

Make it executable. The helper turns Rig's build variables into the right Docker commands:
`--no-cache` when a clean rebuild is requested, and one multi-architecture `buildx` build
that publishes and reports its digest when Rig asks for specific platforms. Run by hand with
none of those variables set, it is a plain `docker build` followed by `docker push`.

Then declare the build:

```yaml
build:
  command: tools/build.sh
  images: [sounder]
  architectures: [arm64, amd64]
  capabilities: {no_cache: true, platforms: true}
images:
  override_env: {sounder: SOUNDER_IMAGE}
```

In the Dockerfile, build on the deployment's base image and do not upgrade what it provides:

```dockerfile
ARG BASE_IMAGE=ros:lyrical-ros-base
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends --no-upgrade <your packages>
```

The whole vehicle then runs one set of ROS and RMW package versions, which `rig image audit`
checks (**M5**).

Now certify the build as well:

```bash
rig certify --repo . --require-build-check
```

```
  [✓] 13 ok: discipline, project-name, registry, tag, image-overrides, ros-env, volumes, determinism, identity, fixed-names, status, build-images, build-no-cache
```

The build check copies the repository to a temporary directory and runs the build script
there with a stand-in `docker` that records commands, no network and no daemon. It builds
nothing. It needs a sandbox: `sandbox-exec` on macOS, `bubblewrap` and `libseccomp2` on
Linux. Without one the check reports `NOT CHECKED`, which `--require-build-check` turns into
an error.

Requirements a build script must meet, and the full list of build variables, are in
[section 12 of the contract](SERVICE_CONTRACT.md#12-capability-builds). Two that are easy to
miss: the script must work from a clean clone with no Rig variable set, and it must use the
tag exactly as given.

If the build clones other repositories, declare them under `build.sources` so
`rig build --offline` can supply them from a local mirror (**B6**).

## 10. Run it in a deployment

From a deployment directory (`rig init my-vehicle --vehicle-id 1` makes one):

```bash
rig add ../sounder --as bow_sounder
```

This routes the service in `services.yaml`, copies the example to
`config/sensors/bow_sounder.yaml` with its `name:` line commented out, and adds a commented
row to `vehicle.yaml`. Uncomment the row to enable the instance:

```yaml
sensors:
  - { name: bow_sounder, service: sounder, config: config/sensors/bow_sounder.yaml, enabled: true, order: 10 }
```

A second instance is another row, not another `rig add`. It can share the config file and
differ through `overrides:`:

```yaml
  - { name: stern_sounder, service: sounder, config: config/sensors/bow_sounder.yaml, enabled: true, order: 10,
      overrides: { ui: { port: 8081 }, connection: { host: 192.168.1.51 } } }
```

Then:

```bash
rig doctor
```

`rig doctor` checks the deployment as a whole. With the override left out it reports
`host port 8080 claimed by multiple stacks: ['bow_sounder', 'stern_sounder']`, which is the
`host_ports` declaration at work. It also reports a `ros_distro` that differs from the
vehicle's `ros.distro`.

```bash
rig up --dry-run
```

```
rig up: concurrent sensor order 10: bow_sounder, stern_sounder
  bow_sounder [sounder]  (cwd=…/sounder)
    COMPOSE_PROJECT_NAME=bow_sounder-vehicle-1 ROS_DOMAIN_ID=1 RMW_IMPLEMENTATION=rmw_zenoh_cpp VEHICLE_ID=1 \
    …/sounder/sounder-up …/my-vehicle/var/rendered/bow_sounder.yaml up -d
```

The dry run shows exactly what Rig will execute: the environment, the working directory and
the config path, which here is a rendered copy under `var/rendered/`. Two instances with the
same `order` start at the same time.

```bash
rig up
```

```bash
rig status
```

```bash
rig logs bow_sounder --tail 50
```

```bash
rig down
```

`rig status` should show both instances `running` and, once the healthcheck passes,
`healthy`. `rig doctor --deep` runs certify for every service in the deployment.

## 11. Prove it survives vendoring

On a vehicle, and in any baked artifact, the launcher runs from a copy that contains only
the files in `launch_surface`. A file missing from that list works on your machine and fails
there. Test the copy directly:

```bash
rig vendor sounder
```

```bash
rig certify --repo services/sounder
```

Certify should pass as before, apart from `build-no-cache: NOT CHECKED: build context
absent`, which is expected: the copy has no build files. If a helper is missing from the
list, the first check fails with the launcher's error, for example
`discipline: config exited 2: … can't open file '…/services/sounder/tools/render_params.py'`.

Remove `services/sounder/` afterwards if this deployment routes the service at your checkout;
the copy is not used there.

Repeat the check with every config that selects a different Compose overlay, using
`--config`. If a vendored launcher cannot render during `rig bake`, Rig prints
`compose-only: skip <name> (launcher config failed: …)` and leaves out the artifact's
Compose-only start scripts for the whole deployment (**V2**).

Baked artifacts start stacks by running `docker compose` on the captured `config` output,
without the launcher. Anything the launcher does on `up` beyond creating external volumes
does not happen in that path, so keep such steps out of the launcher or express them in
Compose (**O6**).

## 12. Gate it in CI

Certify needs only Python with PyYAML, Docker Compose and a checkout of Rig. For GitHub
Actions:

```yaml
jobs:
  certify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/checkout@v4
        with:
          repository: christomaszewski/rig
          path: .rig
      - run: python3 -m pip install --user pyyaml
      - uses: ./.rig/.github/actions/setup-build-probe
      - run: ./.rig/rig certify --require-build-check --repo .
```

`setup-build-probe` installs the sandbox the build check needs. Add one certify line per
config that exercises a different Compose overlay. This job checks out Rig's default branch;
pin `ref:` if you need a fixed version.

Worth adding beside it: `shellcheck` on the launcher and build script, and tests of your
render helper.

## 13. Publish it

A registry lets a deployment install the service without a checkout:
`rig add <registry>/sounder`. The package is one file,
`services/sounder/manifest.yaml`, pointing at a commit:

```yaml
kind: service
name: sounder                # must equal rigging.yaml `service:`
version: 1.0.0
source:
  repo: https://github.com/acme/sounder.git
  rev: <full commit SHA>
```

Write it from a deployment that routes the service at your clean, pushed checkout:

```bash
rig pkg promote sounder --kind service --to <registry>
```

Or have a release workflow in the service repository rewrite the manifest on each tagged
release, then run the registry's `tools/gen-index` and `tools/validate`. The rules are in
[section 19.2 of the contract](SERVICE_CONTRACT.md#192-registry-package). The published
revision must declare `launch_surface`, because an install vendors it, and should declare
`examples:`, because an install creates the first instance config from it.

## 14. Add capabilities

Everything above is the core contract. The rest is optional, and each item is claimed by a
declaration in `rigging.yaml`. Implement first, declare second: Rig trusts a declaration,
and most of these fail without an error when the promise is empty.

| You want | Adopt | Contract |
| --- | --- | --- |
| The service to start in `rig replay` or `rig sim` sessions | `verbs.runtime-images`, and start exactly the images named in `RIG_RUNTIME_IMAGE_PINS` | [13.3](SERVICE_CONTRACT.md#133-verbsruntime-images-and-pin-enforcement) |
| Correct timestamps when the service consumes replayed data | `replay: {sim_time: true}`: `use_sim_time` follows `RIG_SIM_TIME` | [16.2](SERVICE_CONTRACT.md#162-session-clock-replaysim_time) |
| The bag logger to record your custom message types | `msgs:` | [14](SERVICE_CONTRACT.md#14-capability-message-packages) |
| Recorded service calls that can be replayed | `replay: {service_introspection: true}` | [16.3](SERVICE_CONTRACT.md#163-service-calls-replayservice_introspection) |
| A hardware link kept out of replay sessions | `replay: {start: false}` | [16.4](SERVICE_CONTRACT.md#164-selection-replaystart-and-replayplatform) |
| The service to replay its own recordings | `replay.source` | [16.5](SERVICE_CONTRACT.md#165-replaying-its-own-recordings-replaysource) |
| `rig standby` and `rig activate` to park and wake it | The `standby`, `activate` and `state` verbs | [15](SERVICE_CONTRACT.md#15-capability-operational-states) |
| Different images per hardware or OS target | `build.platforms`, `platform.override_env` | [12](SERVICE_CONTRACT.md#12-capability-builds) |
| Image bundles for a fleet artifact | `verbs.images` | [13.2](SERVICE_CONTRACT.md#132-verbsimages) |
| Smaller copies of recorded data | `export:` | [17](SERVICE_CONTRACT.md#17-capability-run-export) |
| The observed topic graph checked against a declaration | `interface:` | [18](SERVICE_CONTRACT.md#18-capability-graph-interface) |

For a service that will be used in replay, adopt the first three together.

## Common mistakes

Each of these was hit by a real service on the way to compliance.

- **A Compose overlay or helper missing from `launch_surface`.** The service works from a
  checkout and is skipped by `rig bake`. Certify the vendored copy
  ([step 11](#11-prove-it-survives-vendoring)).
- **Mounting a directory from the service directory.** Mount one rendered file read-only
  instead; write outputs under `RIG_DATA_DIR`.
- **A healthcheck on a shared topic.** It reports another instance's health, or never
  matches. Probe the instance's own node.
- **`config` that inspects the host.** Detecting the platform, checking a device exists or
  creating a volume during `config` makes a bake wrong for the target. Take the platform
  from the environment and leave device checks to run time.
- **A verb that fails when an optional variable is absent.** `down`, `status`, `logs` and
  `config` must render without anything that only a replay or a particular `up` provides.
- **Executing config values in the shell.** `eval` of a helper's output lets a config value
  run as code. Read expected keys only.
- **The main process ignores SIGTERM.** `ros2 launch` as PID 1 is the usual case. Set
  `stop_signal: SIGINT` and a `stop_grace_period`, so recorders finish their files.
- **The registry package name differs from `service:`.** A repository called `sbg_driver`
  whose service is `sbg` publishes as `sbg`.
- **A `ros_distro` that differs from the vehicle's.** `rig doctor` reports the mismatch as
  an error for the whole deployment, and `rig up` refuses to start.
- **Reinstalling packages the base image provides.** A plain `apt-get install` upgrades them
  past the base's versions and splits the vehicle across two builds of the same library.
- **A `# syntax=` line in the Dockerfile.** It fetches a frontend from a registry and breaks
  offline builds.

## Done

- [ ] `rig certify --repo . --require-build-check` reports no errors, and every warning is
  understood.
- [ ] The [step 8](#8-check-what-certify-cannot) checklist passes.
- [ ] `rig certify --repo services/<service>` passes on the vendored copy, for every config
  variant.
- [ ] Two instances run side by side under `rig up`, and `rig status` shows both healthy.
- [ ] The certify job runs in CI.
- [ ] Every capability declared in `rigging.yaml` has been exercised for real.
