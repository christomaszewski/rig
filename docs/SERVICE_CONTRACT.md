# Rig service contract

What a service must provide for Rig to build, launch, observe, ship, record and replay it.
This is the normative reference. The [rigification guide](RIGIFICATION.md) is the walkthrough
that gets existing software to this point.

This document describes Rig **v0.2.84**. Where it and the code disagree, the code is right:
`rig_cli/descriptor.py` and `rig_cli/build_contract.py` parse the descriptor,
`rig_cli/dispatch.py` builds every launcher's environment, and `rig_cli/certify.py` is the
executable form of the core contract.

- [1. Model](#1-model)
- [2. Conformance](#2-conformance)
- [3. What a service ships](#3-what-a-service-ships)
- [4. The launcher](#4-the-launcher)
- [5. Instance configuration](#5-instance-configuration)
- [6. Identity and isolation](#6-identity-and-isolation)
- [7. Environment](#7-environment)
- [8. Images](#8-images)
- [9. Data, volumes and logs](#9-data-volumes-and-logs)
- [10. Health and shutdown](#10-health-and-shutdown)
- [11. Descriptor reference](#11-descriptor-reference)
- [12. Capability: builds](#12-capability-builds)
- [13. Capability: image pins and inventories](#13-capability-image-pins-and-inventories)
- [14. Capability: message packages](#14-capability-message-packages)
- [15. Capability: operational states](#15-capability-operational-states)
- [16. Capability: replay and simulation sessions](#16-capability-replay-and-simulation-sessions)
- [17. Capability: run export](#17-capability-run-export)
- [18. Capability: graph interface](#18-capability-graph-interface)
- [19. Distribution](#19-distribution)
- [20. Verification](#20-verification)

## 1. Model

| Term | Meaning |
| --- | --- |
| **Service** | A directory (a repo, or one directory of a collection repo) holding a descriptor and a launcher. |
| **Descriptor** | `rigging.yaml` at the service directory root: static metadata telling Rig how to drive the launcher. |
| **Launcher** | The service's own executable, `<service>-up` by default. It owns everything specific to the stack. |
| **Instance** | One configured copy of a service on a vehicle: one `vehicle.yaml` row, one config file, one Compose project. |
| **Instance config** | The YAML file an instance is configured by. Its schema belongs to the service. |
| **Deployment** | A tree with `vehicle.yaml` and `services.yaml` that lists instances and routes services. |

Three rules shape everything below.

- **The dependency is one-way.** Rig depends on the service; the service never imports,
  calls or requires Rig. Everything Rig provides arrives as launcher arguments and environment
  variables, and the launcher must still work with none of them set.
- **Rig is schema-opaque.** It reads `service` and `name` from an instance config and nothing
  else. The launcher interprets the rest.
- **Rig owns what is vehicle-wide.** Instance names, the Compose project name, start order,
  the shared ROS settings, the image registry and tag, the target platform, the data directory
  and run folders. A service takes these from the environment and does not choose its own.

The contract is Compose-shaped. Rig asks Docker directly about a Compose project named
`COMPOSE_PROJECT_NAME` and about containers and volumes labelled
`com.docker.compose.project=<project>`, and a baked artifact starts stacks with
`docker compose -p <project> -f <captured file> up -d`. A launcher that does not drive
Docker Compose would have to reproduce all of that, and none exists.

## 2. Conformance

**MUST**, **SHOULD** and **MAY** carry their usual meanings. Each requirement has an ID and
says how it is checked:

| Checked by | Meaning |
| --- | --- |
| `certify: <check>` | A `rig certify` check of that name. An ERROR unless marked WARN. |
| `doctor` | `rig doctor` reports it for a deployment. |
| `load` | Rig refuses to load the descriptor or manifest. |
| a command name | That command refuses or fails when the requirement is unmet. |
| — | Nothing checks it. It is still required, and review is the only gate. |

A service is **Rig-compliant** when it meets every MUST in sections 3 to 10. That is the
core contract, and `rig certify --repo .` with no errors is its gate. Sections 12 to 18 are
**capabilities**: optional, each claimed by a descriptor declaration. Declaring a capability
is a promise that the launcher, the compose files and the images implement it. Declare one
only after it works, because Rig trusts the declaration and several capabilities fail
silently when the promise is empty.

| To take part in | A service needs |
| --- | --- |
| `rig up`, `down`, `status`, `logs`, `config`, `pull`, `doctor`, `certify` | The core contract |
| `rig vendor`, `rig bake`, registry installs, run capture and `rig reconstruct` | `launch_surface` ([S4](#3-what-a-service-ships)) |
| `rig down --purge` and `rig cleanup` removing its volumes | `external_volumes` ([D5](#9-data-volumes-and-logs)) |
| Port-clash detection in `rig doctor` | `host_ports` ([I6](#6-identity-and-isolation)) |
| `rig build` | `build:` ([section 12](#12-capability-builds)) |
| `rig build --no-cache` | `build.capabilities.no_cache` |
| Portable multi-architecture releases, `rig build --scope` and `--arch` | `build.architectures` and `build.capabilities.platforms` |
| `rig build --offline` | `build.sources` for everything the build fetches from git |
| Digest pinning from the build catalog | `images.override_env` ([section 13](#13-capability-image-pins-and-inventories)) |
| `rig bake --bundle-images` of a fleet artifact | `verbs.images` |
| Being started by `rig replay`, `rig sim` or `rig bake --include-replay` | `verbs.runtime-images` and pin enforcement |
| Correct clocks under replay; any autonomy service under `rig sim` | `replay.sim_time` ([section 16](#16-capability-replay-and-simulation-sessions)) |
| Replaying its own recordings | `replay.source` |
| Custom message types recorded by the bag logger | `msgs:` ([section 14](#14-capability-message-packages)) |
| `rig standby`, `rig activate`, the `OP` status column | The state verbs ([section 15](#15-capability-operational-states)) |
| `rig run export` slimming its data | `export:` ([section 17](#17-capability-run-export)) |
| `rig graph --check` | `interface:` ([section 18](#18-capability-graph-interface)) |

## 3. What a service ships

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| S1 | MUST | A descriptor named `rigging.yaml` at the root of the service directory. `deploy.yaml` is still read as a legacy name; `rigging.yaml` wins when both exist. | `load` |
| S2 | MUST | When the descriptor declares `service:`, the value equals the name the service is routed and published under: the `services.yaml` key, the `service:` field of its `vehicle.yaml` rows, the `services/<name>/` vendor directory and the registry package name. Declare it; Rig otherwise falls back to the routing key or the directory name. | `load`, registry install |
| S3 | MUST | An executable launcher at `<service dir>/<launcher>`, named `<service>-up` unless `launcher:` says otherwise. It has the exec bit and a shebang, because Rig executes it directly and never through a shell. | `certify: launcher`, `doctor` |
| S4 | MUST for vendoring, baking, registry installs and run capture | `launch_surface:` lists every file that any launcher verb needs for any configuration the service supports: the launcher, every compose file and overlay, render helpers, and anything bind-mounted from the service directory. Entries are paths inside the service directory, with no absolute paths and no `..`; a directory entry is copied whole. The descriptor and the declared examples are added automatically. It lists launch files only, never source or build context, because the runtime is the image. | `rig vendor` rejects a missing list, a missing file or an outside path. Completeness is unchecked: see [V2](#19-distribution). |
| S5 | SHOULD | `examples:` lists at least one example instance config, repo-relative. `examples[0]` is the canonical one: `rig certify --repo` drives the launcher with it, `rig add`, `rig fetch` and registry installs seed a new instance from it, and `rig swap` reports key drift against it. | `rig certify --repo` needs it or `--config` |
| S6 | MUST | The service works without Rig. The launcher run by hand with no Rig variable set brings up a working instance. | — |

## 4. The launcher

### 4.1 Invocation

```
<service dir>/<launcher> <config> <verb args…>
```

- The working directory is the service directory. In a development deployment that is the
  checkout; in a baked or registry-installed deployment it is the vendored copy
  `services/<service>/`, which holds only the launch surface.
- `<config>` is the absolute path of the instance config.
- `<verb args…>` is the descriptor's `verbs.<verb>` string split on whitespace, followed by
  whatever Rig appends for that verb.
- The environment is described in [section 7](#7-environment).

### 4.2 Verbs

| Logical verb | Default args | Rig appends | Level | Behaviour |
| --- | --- | --- | --- | --- |
| `up` | `up -d` | — | MUST | Start the instance detached and return. |
| `down` | `down` | — | MUST | Stop and remove the instance's containers. |
| `status` | `ps` | `--format json` | MUST | Print container state on stdout ([4.5](#45-the-status-verb)). |
| `logs` | `logs` | `-f`, `--tail <N>` | MUST | Write the instance's logs to its own stdout and stderr. |
| `config` | `config` | — | MUST | Print the resolved Compose document on stdout ([4.4](#44-the-config-verb)). |
| `pull` | `pull` | — | SHOULD | Fetch the instance's images without touching containers. |

The defaults are Docker Compose subcommands, so a launcher that forwards its arguments to
`docker compose` gets all six. A launcher with a different command line maps them in
`verbs:`, for example `verbs: {status: ps}`. A verb with no mapping and no default is passed
as its own name.

Capability verbs are called only when declared: `standby`, `activate`, `state`
([section 15](#15-capability-operational-states)), `images`, `runtime-images`
([section 13](#13-capability-image-pins-and-inventories)) and `replay-capabilities`
([section 16](#16-capability-replay-and-simulation-sessions)). `rig run export` calls
`export` on a service that declares `export:` ([section 17](#17-capability-run-export)).

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| L1 | MUST | Accept the config at any absolute path and never modify it. Derive nothing from its file name or directory. Rig passes `<deployment>/config/<tier>/…`, `<deployment>/var/rendered/<name>.yaml`, a bake staging tree, a per-session directory and temporary files, depending on the command. | `certify` passes every config from a temporary directory |
| L2 | SHOULD | Find its own files relative to the launcher's location, not the working directory, so it also runs by hand from anywhere. | — |
| L3 | MUST | `up` returns once the stack is started. A foreground `up` blocks the whole vehicle: Rig waits for every launcher in a start group and applies no timeout to `up`, `down`, `standby` or `activate`. The launcher bounds its own waits and exits non-zero at its own deadline. | — |
| L4 | MUST | Exit 0 on success and non-zero on failure, for every verb. | `certify: discipline`, `status` |
| L5 | MUST | `down` removes every container of the instance's Compose project and nothing else. It never removes named volumes or recorded data. Session teardown re-checks that the project is empty. | `rig sim down`, recipe replay teardown |
| L6 | MUST | Every verb works with the environment Rig gives that verb. `down`, `status`, `logs` and `config` must not need a variable or an input that only `up` receives, and no verb other than `up` may fail on the value of `RIG_TARGET_STATE`. | `certify` poisons `RIG_TARGET_STATE` on every check |
| L7 | MUST | Be safe to run concurrently for two instances of the same service in the same working directory. Rendered and scratch files are keyed by instance name; there are no shared lock files. | — |
| L8 | MUST | Never write into the launch surface. Replay and simulation fingerprint the launcher, the descriptor and the launch-surface files when planning and refuse to start if they changed. Files the launcher renders go in a separate, git-ignored directory under the service directory; the reference services use `var/run/<name>…`. | `rig replay`, `rig sim` |
| L9 | SHOULD | Stop promptly on SIGTERM. On Ctrl-C Rig signals each launcher's process group and sends SIGKILL one second later. | — |
| L10 | SHOULD | Keep progress output line-oriented. Rig prefixes each line with `[<instance>]` and interleaves concurrent launchers. | — |

### 4.3 Output discipline

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| O1 | MUST | Standard output carries machine output only. Every human-readable line, on every verb, goes to standard error. Rig parses the stdout of `config`, `status`, `state`, `images`, `runtime-images` and `replay-capabilities`, and `rig bake` stores the `config` output verbatim. | `certify: discipline`, `status`, `state-verbs` |

### 4.4 The `config` verb

`config` is the most heavily used verb. `rig bake` captures it to build the artifact's
Compose files, `rig cleanup` and `rig image audit` read image references from it, and
`rig certify` proves most of the contract by inspecting it.

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| O2 | MUST | Print exactly what `docker compose config` prints: one YAML document whose top-level keys are among `name`, `version`, `services`, `networks`, `volumes`, `configs`, `secrets`, `include` and `x-*`, with a non-empty `services` map in which every service names an `image`. | `certify: discipline`; `rig bake` skips a stack that renders nothing |
| O3 | MUST | Be deterministic. The same config and environment give byte-identical output: no timestamps, no random values, no temporary paths. | `certify: determinism` |
| O4 | MUST | Be host-independent. `config` is a pure function of the config file and the environment. It never probes the host: no hardware or OS detection, no check that a device exists. A bake renders on a development machine for a declared target. | `certify: platform` for matrix services; `rig certify --emit` on two hosts, then `--diff` |
| O5 | MUST | Have no side effects: no containers, no volumes, no network access. Any setup the launcher performs is gated on the verb being `up`. | — |
| O6 | MUST | Be complete. Everything needed to start the instance is in the rendered document and the files it bind-mounts. The Compose-only scripts of a baked artifact run `docker compose` on the captured document and never execute the launcher, so an imperative step in the launcher's `up` does not happen there. The one exception is creating volumes marked `external: true`, which those scripts do themselves. | — |
| O7 | MUST | Render files that the stack bind-mounts under the service directory, and mount each as a single file where possible. `rig bake` copies bind sources found under its staging tree into the artifact and rewrites them as relative paths; a bind source anywhere else is kept as a literal host path that must exist on the vehicle. | `certify: binds` (WARN) flags directory binds into the service directory |
| O8 | SHOULD | Leave Compose `profiles:` already resolved and avoid relying on `build:` sections. Bake strips both. | — |

### 4.5 The `status` verb

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| O9 | MUST | `status --format json` prints what `docker compose ps --format json` prints: a JSON array, or one JSON object per line. Empty output means no containers. Rig reads `State` (a container counts as running when it is exactly `running`), `Health` (`healthy`, `unhealthy`, anything else, or empty for no healthcheck) and `Name` or `Service`. Any other text on stdout makes the instance read as down. | `certify: status` |

`rig status` rolls these up per instance. STATE is `running` when every container is
running, `down` when none is and `partial` otherwise. HEALTH is `n/a` when no container has
a healthcheck, `unhealthy` if any is, `healthy` when all checked containers are, and
`starting` otherwise.

## 5. Instance configuration

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| C1 | MUST | The config is a YAML mapping. Rig reads `service` and `name`, plus the paths declared in `host_ports`. Everything else is the service's own schema. | `load` |
| C2 | MUST | Take the instance's identity from the config's `name` key. Rig always provides `name` and `service` in the file it hands the launcher: it stamps them in when it renders a config, and a config it passes through untouched already has them. | `certify: identity` |
| C3 | SHOULD | Accept any name matching `[a-z][a-z0-9_]*`. That is what `rig add --as` and registry installs produce, and it is valid as a ROS name, a Compose project name and a volume name. `vehicle.yaml` itself accepts the wider `[a-z0-9][a-z0-9_-]*`; a launcher that cannot use such a name rejects it on stderr with a non-zero exit. | `doctor` warns on a hyphenated name for a ROS service |
| C4 | MUST | Tolerate the way Rig rewrites configs. Rig deep-merges overlays and `overrides:` on top of the config and writes the result with comments removed. In that merge mappings merge, lists and scalars are replaced whole, and a `null` value deletes its key. A config schema therefore must not give `null` a meaning, and settings an operator will override are better as mappings than as list items. | — |
| C5 | MUST | Keep literal `{{…}}` out of config values. `{{lower_snake}}` and `{{map a b}}` are Rig's interpolation syntax, resolved before the launcher sees the file, and an unknown variable is an error. Compose's `${VAR}` syntax is never touched by Rig. | config render |
| C6 | SHOULD | Avoid paths relative to the config file. The file the launcher receives is often a rendered copy in another directory. Use absolute host paths, or paths relative to the data directory. | — |
| C7 | SHOULD | Make `examples[0]` a complete, commented statement of the config surface. Give it a top-level `name:` on a line of its own, or no name at all: Rig comments that line out to make a nameless profile whose name the `vehicle.yaml` row supplies. Name the file `<something>.example.yaml`; the stem becomes the default config file name. | — |
| C8 | SHOULD | Refuse config-supplied container environment that sets `COMPOSE_PROJECT_NAME` or any `RIG_*` variable, when the config can inject environment at all. | — |

## 6. Identity and isolation

Several instances of one service run on the same host, and `name` is unique across the
vehicle. Rig sets the Compose project to `<name>-vehicle-<vehicle_id>`, or to `<name>` when
the deployment has no vehicle id.

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| I1 | MUST | Honour `COMPOSE_PROJECT_NAME`. Never pass `-p` or `--project-name` to Compose. The launcher may set its own default, conventionally `<service>_<name>`, only when the variable is unset. If the launcher uses another project, `rig status` inspects an empty stack, `rig down` removes nothing, and run sealing cannot see a live recorder. | `certify: project-name` |
| I2 | MUST | Derive every host-visible identifier from the instance name or the project: named and external volumes, socket and shared-memory paths, rendered file names, and any port taken from the config. | `certify: identity` in part |
| I3 | MUST | Never give a container, network or volume a name that is the same for every instance. A fixed `container_name:` stops a second instance from starting, because container names are global to the host; leave it out and Compose names containers after the project. A network or volume with a fixed `name:` is shared by every instance, and each project treats it as its own. A resource that all instances are meant to share is marked `external: true`. | `certify: fixed-names` (ERROR for a container, WARN for a network or volume) |
| I4 | MUST | After an instance is renamed, no trace of the old name remains in the `config` output. | `certify: identity` |
| I5 | SHOULD | Put the instance's ROS nodes under the namespace `/<name>`. A service that uses a different namespace declares it as `ros_namespace:`: an absolute, non-root namespace such as `/control/relay`, or a template such as `"/robot/{name}"`. A service that can have several instances uses the `{name}` template, because replay cannot attribute recorded topics when two instances share one fixed namespace. | `load`, `doctor`, `rig replay` |
| I6 | SHOULD | Read host-facing ports from the config and declare the config paths in `host_ports:` so `rig doctor` can report two enabled instances claiming one port. | `doctor` |

A `host_ports` entry is a dotted path into the instance config. A segment may carry a list
selector, `key[field=value,field2=value2]`, which picks the first list item whose fields all
match, compared case-insensitively as strings so that `enabled=true` matches the YAML
boolean. For example `plugins[name=webrtc-bridge,enabled=true].params.port` resolves only
while that plugin is enabled. There is no index syntax and no protocol dimension. A path
that does not resolve, or resolves to something other than an integer, is ignored.

## 7. Environment

Rig sets or removes every variable in this table on every launcher call. A variable Rig owns
never leaks in from the operator's shell, and a deployment's `env:` map cannot set one.

| Variable | Present | Meaning |
| --- | --- | --- |
| `COMPOSE_PROJECT_NAME` | On every call for a named instance; see E3 and P2 for the two calls without it | The instance's Compose project ([I1](#6-identity-and-isolation)). |
| `ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION` | Always | The vehicle's shared ROS graph settings. |
| `VEHICLE_ID` | When the deployment has an identity | The vehicle's numeric id. |
| `RIG_IMAGE_REGISTRY` | When configured | The registry to pull from, for example `devbox:5000`. |
| `RIG_IMAGE_TAG` | When configured | The release version. For a service with `build.platforms` Rig has already composed it to `<version>-<platform>`. |
| `RIG_TARGET_PLATFORM` | When the vehicle declares `platform:` | The runtime variant, for example `jp7`. Also copied into the service's `platform.override_env`. |
| `RIG_TARGET_ARCH` | When the vehicle declares `arch:`, and in sessions | `arm64` or `amd64`. |
| `RIG_BASE_IMAGE` | When the deployment has a base image | Full reference of the deployment's one base image. |
| `RIG_MSGS_IMAGE` | When a messages overlay is built | Full reference of the base plus the fleet's message packages. |
| `RIG_DATA_DIR` | When the vehicle declares `data_dir:` | Absolute host directory for recordings and other outputs. |
| `RIG_IMAGE_PINS` | When a build catalog exists | For Rig's own use. Rig translates it into the variables named by `images.override_env`. |
| `RIG_TARGET_STATE` | Only on `up`, with `--standby` or `--active` | Requested operational state ([section 15](#15-capability-operational-states)). |
| `RIG_SIM_TIME`, `RIG_REPLAY_*`, `RIG_RUNTIME_IMAGE_PINS` | Only in replay and simulation sessions | [Section 16](#16-capability-replay-and-simulation-sessions). |
| `RIG_EXPORT_*` | Only on the `export` verb | [Section 17](#17-capability-run-export). |

The deployment's `env:` map is applied last and is the supported way for an operator to set
a service's own variables fleet-wide.

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| E1 | MUST | Pass `ROS_DOMAIN_ID` and `RMW_IMPLEMENTATION` unchanged into every container that runs a ROS node. | `certify: ros-env`. A changed value is an ERROR for every service; a missing one is a WARN, and only for services that declare `ros_distro` |
| E2 | MUST | Never hard-code a value Rig owns: the ROS domain, the RMW, the registry, the tag, the project name. | `certify: ros-env`, `registry`, `tag`, `project-name` |
| E3 | MUST | Treat every Rig variable as optional and supply a default that works standalone ([S6](#3-what-a-service-ships)). `rig cleanup` renders `config` without `COMPOSE_PROJECT_NAME`, and `rig certify` runs without `RIG_BASE_IMAGE` and `RIG_MSGS_IMAGE`. | `certify` |
| E4 | MUST | Depend on no other ambient environment for `config`. `rig bake` runs launchers with only `PATH`, `HOME`, `TMPDIR`, `TMP`, `TEMP`, `LANG`, `LC_ALL`, `SYSTEMROOT`, the `DOCKER_*` connection variables and `SSH_AUTH_SOCK` from the shell, plus Rig's own variables and the deployment's `env:` map. A service setting comes from the instance config or from that map. | `rig bake` |
| E5 | MUST | Give the service's own variables names Rig does not own. Prefix them with the service, as in `CAM_PLATFORM` or `NOVATEL_IMAGE`. | `load` rejects a Rig-owned name in `platform.override_env` or `images.override_env` |
| E6 | SHOULD | Need nothing on the host beyond a POSIX shell, Docker with Compose v2, and Python 3 with PyYAML if the launcher uses them. Let the interpreter be overridden, conventionally with `<SERVICE>_PYTHON`. | — |

## 8. Images

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| M1 | MUST | Reference each image the service builds (`build.images`) as `<RIG_IMAGE_REGISTRY>/<image>:<RIG_IMAGE_TAG>` when those variables are set. Use the tag exactly as given; Rig has already added any platform suffix. Use the image name exactly as declared, with nothing between the registry and the name: `rig build` publishes `<registry>/<image>` and nothing else. | `certify: registry`, `tag`, `build-images` |
| M2 | MUST | Reference each mirrored third-party image (`mirror:`) as `<RIG_IMAGE_REGISTRY>/<upstream repository>:<upstream tag>`. `rig build` copies the image there under its original name. | `certify: registry` |
| M3 | SHOULD | Pull nothing from outside the fleet registry. An image pulled from upstream works only on a vehicle with internet access. | `certify: registry` (WARN) |
| M4 | SHOULD | Wrap each built image reference in a service-owned override variable and declare it under `images.override_env` ([section 13](#13-capability-image-pins-and-inventories)). | `certify: image-overrides` when declared |
| M5 | SHOULD | Build ROS images `FROM` the deployment's base image, and install extra packages with `apt-get install --no-upgrade`, so every image on the vehicle carries the same ROS and RMW package versions. For each ROS image, `rig image audit` requires the vehicle's distribution under `/opt/ros`, the declared RMW package installed, and one version of every `ros-*` package shared with another image. It inspects images with `/bin/sh` and `dpkg-query`. | `rig image audit` |

The reference form of an image line, which satisfies M1 and M4 and still works standalone:

```yaml
image: ${WIDGET_IMAGE:-${RIG_IMAGE_REGISTRY:+${RIG_IMAGE_REGISTRY}/}widget:${RIG_IMAGE_TAG:-latest}}
```

A service that runs the base or messages image directly puts those first in the chain:
`${ROUTER_IMAGE:-${RIG_BASE_IMAGE:-<the form above>}}`. The innermost fallback is what
`rig certify` renders.

`certify` matches images by repository name with the registry prefix removed, so
`build.images: [widget]` matches `<registry>/widget` and nothing else. A built image that the
compose never references under that name is not examined at all; see
[section 20](#20-verification).

## 9. Data, volumes and logs

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| D1 | MUST | Write persistent outputs under `RIG_DATA_DIR` when it is set. Within it, write to `current/<kind>/<name>/` when the `current` link exists and to `<kind>/<name>/` when it does not, where `<kind>` is a directory the service chooses (`bags`, `recordings`) and `<name>` is the instance name. | `certify` reports data-directory binds as INFO |
| D2 | MUST | Resolve `current` once, when the process starts, from inside the container. `current` is a relative symlink to the open run, so mount the whole data directory, not a subdirectory, and do not follow the link again mid-session. | — |
| D3 | MUST | Keep the run id out of the Compose document and the environment. It lives only in the filesystem; an id rendered into `config` output would be frozen into baked artifacts. | `certify: determinism` in part |
| D4 | MUST | Stay out of `<run>/.rig/` and `<run>/exports/`. They belong to Rig. | — |
| D5 | MUST | Declare every external named volume in `external_volumes:` as a pattern containing `{name}`, and give the Compose volume a `name:` that expands to exactly that pattern. Rig removes these on `rig down --purge` and `rig cleanup`; the service never removes a volume itself, because a consumer may still be attached. Volumes scoped to the Compose project need no declaration. | `certify: volumes` (WARN) |
| D6 | SHOULD | Not bind-mount a directory inside the service directory for outputs. A bake would freeze that path into the artifact. | `certify: binds` (WARN) |
| D7 | SHOULD | Log to container stdout and stderr. `rig down --end-run` saves `docker logs` of every container in the project into the run before teardown. | — |

When `RIG_DATA_DIR` is unset the service uses its own default location, or refuses only the
feature that needs it.

## 10. Health and shutdown

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| H1 | SHOULD | Give at least the main container a healthcheck, in the image or in Compose. Without one, `rig status` shows `n/a`. | — |
| H2 | SHOULD | Make the healthcheck probe readiness, not data flow: process up, configured, device or backend reachable. Probe the instance's own resources. On host networking every node shares topics such as `/diagnostics`, so a probe of a shared topic reports the wrong instance. | — |
| H3 | SHOULD | Use `restart: unless-stopped`, and retry connections to producers. Rig sequences start groups but does not wait for health between them. | — |
| H4 | SHOULD | Keep every container of the project long-running. An exited container makes the instance `partial`, and `rig sim up` requires every container to be running and none unhealthy. | `rig sim up` |
| H5 | SHOULD | Shut down cleanly on the configured stop signal within 30 seconds, exiting 0, 130 or 143. Set `stop_signal` when the main process ignores SIGTERM. Coordinated replay leaves a run unsealed otherwise. | coordinated `rig replay --auto-end` |

## 11. Descriptor reference

Unknown top-level keys are ignored, so a misspelled top-level key silently does nothing.
Inside `build.capabilities`, `build.variants.<p>`, `build.msgs_overlay`, `build.sources[]`,
`platform`, `msgs`, `replay`, `replay.source`, `export` and `interface` an unknown key is an
error.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `service` | string | routing key | The service name ([S2](#3-what-a-service-ships)). |
| `launcher` | path | `<service>-up` | Launcher file, relative to the service directory. |
| `verbs` | map of strings | [4.2](#42-verbs) | Launcher arguments per logical verb. Declaring `standby`, `activate`, `state`, `images`, `runtime-images` or `replay-capabilities` here claims that capability. |
| `tier` | `infra`, `sensor`, `autonomy` | `sensor` | Where `rig add`, `rig init` and installs place a new instance. The `vehicle.yaml` section a row sits in is what decides start order. |
| `ros_distro` | string | none | The ROS distribution. One per vehicle. Declaring it also makes `certify` warn when no container receives the fleet ROS settings. Omit for a non-ROS service. |
| `ros_namespace` | string | `/<name>` | The ROS node namespace the launcher actually uses ([I5](#6-identity-and-isolation)). Descriptive only: it does not configure the launcher. |
| `examples` | path or list | none | Example instance configs ([S5](#3-what-a-service-ships)). |
| `launch_surface` | list of paths | none | Files needed to launch ([S4](#3-what-a-service-ships)). |
| `external_volumes` | list of patterns | none | External volumes, with `{name}` ([D5](#9-data-volumes-and-logs)). |
| `host_ports` | list of config paths | none | Host-facing ports ([I6](#6-identity-and-isolation)). |
| `mirror` | list of image refs | none | Third-party images `rig build` copies into the registry ([M2](#8-images)). |
| `platform.auto_detect` | path | none | The file the launcher's standalone host probe reads. Informational. |
| `platform.override_env` | env name | none | The service's own platform variable. Rig sets it to `RIG_TARGET_PLATFORM`. |
| `images.override_env` | map image → env name | none | Per-image override variables ([section 13](#13-capability-image-pins-and-inventories)). |
| `build` | string or map | none | [Section 12](#12-capability-builds). A bare string is the command. |
| `build.command` | string | — | Build entry point. |
| `build.images` | list | none | Image repositories the build publishes. |
| `build.platforms` | list | none | Runtime variants; each is a valid image-tag fragment. |
| `build.architectures` | list of `arm64`, `amd64` | none | CPU architectures of each published tag. |
| `build.capabilities` | `{no_cache, platforms}` booleans | false | Behaviours the build script implements. |
| `build.variants.<platform>` | `{architectures, images}` | inherit | Per-variant overrides. |
| `build.provides` | `base` | none | This build produces the deployment's base image, `build.images[0]`. |
| `build.msgs_overlay` | `{command, image}` | none | The messages overlay build. Base providers only. |
| `build.sources` | list of `{name, repo, ref}` | none | Git repositories the build needs. `name` matches `[a-z0-9][a-z0-9_-]*`. |
| `msgs.apt` | list of ROS package names | none | Released interface packages, in the underscore form. |
| `msgs.source` | list of `{repo, ref, packages}` | none | Interface packages built from source. |
| `replay.sim_time` | boolean | false | The launcher follows `RIG_SIM_TIME`. |
| `replay.service_introspection` | boolean | false | Service calls are recorded with their contents. |
| `replay.start` | boolean | true | `false` keeps the service out of automatic replay selection. |
| `replay.platform` | one of `build.platforms` | none | The portable runtime variant for replay hosts. |
| `replay.source` | `{data, overrides, binding, image_labels}` | none | The service can replay its own recordings. |
| `export` | `{data, modes, command, default_profile, profiles}` | none | [Section 17](#17-capability-run-export). |
| `interface` | `{publishes, subscribes, provides, requires}` | none | [Section 18](#18-capability-graph-interface). |

The [README](../README.md#the-contract-riggingyaml) carries an annotated example of the
whole file.

## 12. Capability: builds

Declared by `build:`. `rig build` runs the command for each service the deployment uses,
base image first.

### 12.1 Invocation

```
<build.command> <registry> <tag>
```

- If `<service dir>/<command>` is a file, Rig executes it directly, so it needs the exec bit
  and a shebang. Otherwise the string runs under `bash -lc`.
- The working directory is the service directory. When that is a vendored copy without the
  build entry point, Rig builds from the source checkout pinned in `rig.lock`.
- `<tag>` is the release version, already composed to `<version>-<platform>` for a variant.
- A portable build (see B4) always passes both arguments. A build with no
  architecture declarations omits whichever of the two is unset, so with a tag and no
  registry the tag arrives as the first argument.
- Only the exit code is read. Output goes to a log file under `var/build/`.

### 12.2 Environment

The build inherits the operator's shell, plus:

| Variable | Present | Meaning |
| --- | --- | --- |
| `ROS_DISTRO` | When the vehicle declares `ros.distro` | The distribution to build for. |
| `RIG_ROS_RMW` | Always | The vehicle's RMW. A base image installs this one. It is deliberately not `RMW_IMPLEMENTATION`, which most ROS shells export. |
| `RIG_BASE_IMAGE` | When the deployment has a base; never for the base's own build | Build `FROM` this. In a portable build it is a digest reference. |
| `RIG_BUILD_NO_CACHE` | `1` with `--no-cache` | Disable Docker's instruction cache. |
| `RIG_TARGET_PLATFORM`, `<platform.override_env>` | For a variant build | The variant being built. |
| `RIG_BUILD_PLATFORMS` | Portable builds | Comma-separated Docker platforms, for example `linux/arm64,linux/amd64`. |
| `RIG_BUILD_RESULT` | Portable builds | Path of a file to append result lines to. |
| `PUSH` | `1` in portable builds | Publish; do not load locally. |
| `RIG_BUILD_SOURCES` | When the service declares `build.sources` | Directory of exported source trees. |
| `RIG_OFFLINE`, `RIG_BUILD_PULL` | `1` and `0` with `--offline` | Do not reach the network; do not pass `--pull`. |
| `RIG_MSGS_MANIFEST` | The messages overlay build only | Path of the fleet's message-package manifest. |

`RIG_IMAGE_REGISTRY`, `RIG_IMAGE_TAG` and the deployment's `env:` map are not given to
builds. The registry and tag arrive as arguments.

### 12.3 Requirements

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| B1 | MUST | Build and publish exactly `<registry>/<image>:<tag>` for each declared image, using the tag as given. For a variant, the images are that variant's `build.variants.<platform>.images` when declared. | `certify: build-no-cache`, `rig build` |
| B2 | MUST | Work from a clean checkout at the pinned revision, with no Rig variable set. `ROS_DISTRO`, `RIG_ROS_RMW`, `RIG_BASE_IMAGE` and `RIG_BUILD_SOURCES` all have defaults. | `certify: build-no-cache` runs the script in an isolated copy |
| B3 | MUST | Honour `RIG_BUILD_NO_CACHE`: every Docker build gets `--no-cache` when it is set and none does when it is not. Declare `build.capabilities.no_cache: true`. The certify build check tests this whether or not the capability is declared, and `rig build --no-cache` refuses a service that has not declared it. | `certify: build-no-cache`, `doctor` (WARN), `rig build --no-cache` |
| B4 | MUST for portable releases | Declare `build.architectures` and `build.capabilities.platforms: true`, and then: build each image once with `docker buildx build --platform "$RIG_BUILD_PLATFORMS" --push` and no `--load`; append one line `{"ref": "<registry>/<image>:<tag>", "digest": "sha256:…"}` per published image to `$RIG_BUILD_RESULT`, where the digest is the one the builder reported for the published index. One service declaring architectures or platforms puts the whole deployment's build in portable mode, and every other building service must then declare them too. | `certify: build-no-cache`, `rig build` |
| B5 | MUST for runtime variants | Declare `build.platforms`. Every declared platform renders with `config` on any host and pulls the tag `<version>-<platform>`, and no platform renders identically to the first one declared. The declared platform, from `RIG_TARGET_PLATFORM` or the override variable, wins over any host detection. | `certify: platform`, `doctor` |
| B6 | SHOULD | Declare every git repository the build fetches in `build.sources`, pass each as `--build-context <name>=$RIG_BUILD_SOURCES/<name>` when that variable is set, and keep a default stage so a standalone build still fetches. When `RIG_OFFLINE=1`, touch no network; when `RIG_BUILD_PULL=0`, omit `--pull`. Avoid `# syntax=` Dockerfile frontends, which are fetched from a registry. | `rig build --offline --check` checks the mirrors, not the script |
| B7 | MUST for local-source builds | Label an image built from local checkouts instead of its pinned sources with `io.rig.build.local`. `rig image audit` warns about it and `rig bake` refuses it without `--allow-local`. | `rig bake` |

[`packaging/rig-build.sh`](../packaging/rig-build.sh) implements B3 and B4. Copy it into the
build context, source it and call `rig_build <image ref> <docker build arguments…>` once per
image. [Builds and replay](BUILD_AND_REPLAY.md#service-build-contract) covers scopes,
builders and the build catalog.

### 12.4 Base image and messages overlay

A service whose build produces the deployment's base image declares `build.provides: base`.
Rig builds it first and hands `<registry>/<build.images[0]>:<tag>` to every other build and
launcher as `RIG_BASE_IMAGE`. Services that provide the same base must agree on the image
name, on `build.platforms` and on the build script; Rig refuses a disagreement and never
picks by order. A base build script's directory is its entire build context.

A base provider may also declare `build.msgs_overlay: {command, image}`. Rig runs that
command after the base, with `RIG_BASE_IMAGE` and `RIG_MSGS_MANIFEST` set, and exports the
result to launchers as `RIG_MSGS_IMAGE`.

## 13. Capability: image pins and inventories

### 13.1 Override variables

`images.override_env` maps a built image to a variable owned by the service:

```yaml
images:
  override_env: {widget: WIDGET_IMAGE}
```

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| P1 | MUST when declared | When the variable is set, every Compose service that runs that image uses the variable's value as the complete image reference. Rig sets it to a digest reference from the build catalog; the launcher never parses `RIG_IMAGE_PINS`. | `certify: image-overrides` |

### 13.2 `verbs.images`

Required of every enabled service when a fleet artifact, one whose configs still contain
unresolved per-vehicle `{{var}}` markers, is baked with `--bundle-images`.

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| P2 | MUST when declared | Print on stdout a non-empty YAML list of the image references `up` would run for this config, and nothing else. Answer within 15 seconds with no side effects. The config may still contain identity markers, and neither `VEHICLE_ID` nor `COMPOSE_PROJECT_NAME` is set, so image selection must not depend on vehicle identity. Fail if an unresolved value decides which image runs. | `rig bake --bundle-images` |

### 13.3 `verbs.runtime-images` and pin enforcement

Required of every instance that `rig replay`, `rig sim` or `rig bake --include-replay`
selects, including plain consumers and infrastructure. There is no fallback to
`build.images`.

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| P3 | MUST when declared | Print on stdout one JSON document, `{"version": 1, "images": [...], "roles": {...}}`, within 15 seconds and with no side effects. `images` is the non-empty list of unique image references the instance would run for this config and environment. `roles` is optional and maps a logical name, such as a build image name, to one of those references. The query must not need recorded data to exist. | `rig replay`, `rig sim` |
| P4 | MUST when declared | On `up` with `RIG_RUNTIME_IMAGE_PINS` set, start exactly the pinned images. The variable holds `{"version": 1, "images": {"<reference as reported>": "sha256:<local image id>"}}`. Recompute the active image set and require it to equal the pinned keys, replace each `image` with its pinned id, drop `build` sections, and start with pulling disabled. `status`, `logs` and `down` also work with the variable set. | — |

## 14. Capability: message packages

Declared by `msgs:`. rosbag2 cannot record a topic whose message package is missing from the
recorder's image; it logs one warning and the bag silently lacks the topic. Rig unions every
service's declaration into the messages overlay that the bag logger runs.

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| G1 | MUST for full recordings | Declare every interface package the service's topics, services and actions use, beyond those in `ros-base` and `common_interfaces`. Released packages go under `msgs.apt` by ROS name, with underscores. Packages built from source go under `msgs.source` with `repo`, `ref` and `packages`. Declare interface packages only, never the driver. | — |
| G2 | MUST | Make each `msgs.source` `ref` equal the revision the service's own image builds against. A different pin gives the overlay definitions that are wire-incompatible with what the service publishes, and the bag is wrong without any error. Across a deployment, one repository declared at two refs is refused. | `rig build`, `doctor`, `rig image audit` |
| G3 | SHOULD | For each `msgs.source` entry, record in the service's image, at `/opt/fleet-msgs/provenance.yaml`, the ref the build checked out and the commit it resolved to, so `rig image audit` can compare it with the overlay. | `rig image audit` |

## 15. Capability: operational states

Declared by listing all three of `standby`, `activate` and `state` in `verbs:`. The
declaration is the claim; these verbs have no defaults. A partial declaration is an error in
`certify` and a warning in `doctor`, and Rig skips the service. An undeclared service is
always active.

A running instance is `active` (doing its job) or `standby` (up, configured and ready, but
quiet: a lifecycle node inactive, a device in its low-power mode). Health is independent of
state: a healthy instance is one that can run, in either state.

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| T1 | MUST | `standby` and `activate` move the running instance to that state and exit 0. They converge: a repeat re-applies the complete state definition and succeeds. They never create or destroy containers, and fail clearly when the project is down. | — |
| T2 | MUST | `state` prints exactly one JSON object on stdout with a `state` of `active`, `standby`, `transitioning` or `down`, and exits 0 for any project state, including one that was never started. Implementation detail goes in a free-form `detail` key. It is read-only and renders nothing. Exit non-zero only when the probe itself cannot answer. | `certify: state-verbs` |
| T3 | MUST | Bound each transition with the launcher's own timeout and exit non-zero at the deadline. Rig applies none. | — |
| T4 | MUST | At `up`, choose the initial state by `RIG_TARGET_STATE`, then the config's `initial_state`, then `active`. Validate `RIG_TARGET_STATE` on `up` only. | `certify` poisons it on every other verb |
| T5 | MUST | Keep the healthcheck independent of state ([H2](#10-health-and-shutdown)). A healthy standby instance produces no data. | — |
| T6 | SHOULD | Release the launcher and the image together when part of the implementation lives in the image. | — |

`rig status` shows the `state` answer in the `OP` column and as `op_state` in its JSON, next
to HEALTH. Read them as a pair: `transitioning` with `healthy` means wait, `transitioning`
with `unhealthy` means stuck. A non-zero exit or any other output shows as `unknown`.
Software other than Rig may change a service's state directly; Rig records no commanded
state.

The Compose-only scripts of a baked artifact do not run the launcher, so `RIG_TARGET_STATE`
and `initial_state` handling that lives only in the launcher's `up` has no effect there
([O6](#44-the-config-verb)).

## 16. Capability: replay and simulation sessions

`rig replay` starts selected instances live while the bag player replays what they consumed
in a recorded run. `rig sim` attaches selected infrastructure and autonomy instances to a
running simulator. [Replay](REPLAY.md), [coordinated startup](replay-coordination.md) and
[simulation](SIMULATION.md) describe the operator side.

### 16.1 Every instance in a session

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| R1 | MUST | Declare `verbs.runtime-images` and enforce `RIG_RUNTIME_IMAGE_PINS` ([P3, P4](#133-verbsruntime-images-and-pin-enforcement)). | `rig replay`, `rig sim` |
| R2 | MUST | Start from a session-local config path, and let `status`, `logs` and `down` work both with the session's variables and without them. Recipe-driven sessions repeat the session environment on teardown; other replays tear down with the ordinary environment. | — |
| R3 | MUST | Start into an empty Compose project and leave it empty after `down` ([L5](#42-verbs)). | `rig sim`, recipe and coordinated replay |

### 16.2 Session clock: `replay.sim_time`

In a session with a simulated clock Rig sets `RIG_SIM_TIME=1` for every launcher. It is
unset otherwise, including under `rig replay --wall-clock`.

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| R4 | MUST when declared | When `RIG_SIM_TIME` is non-empty and the instance config does not set the service's own sim-time setting, run every ROS node with `use_sim_time: true`. An explicit config value wins in both directions; the variable only supplies the default. | — |

An undeclared consumer under replay draws a warning: it would compare recorded timestamps
with the wall clock and silently reject the data. Under `rig sim` an undeclared autonomy
service is an error and other tiers a warning.

### 16.3 Service calls: `replay.service_introspection`

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| R5 | MUST when declared | Enable ROS 2 service introspection at the contents level on the service's servers and clients, so their calls are recorded with request contents and can be replayed. Rig provides no variable for this; the service gates it itself. Leave the generated parameter services off. | — |

Recording happens at record time or never. Service types need the `msgs:` declaration like
any topic type. An undeclared service draws a warning when a replay includes service calls.

### 16.4 Selection: `replay.start` and `replay.platform`

`replay.start: false` keeps a service out of automatic replay selection while leaving its
recorded topics available. Use it for a link to live hardware with nothing to play back. A
deployment or the operator can still select the instance, and it must then meet 16.1.
`replay.platform` names the `build.platforms` entry that runs on a replay host without the
vehicle's hardware.

### 16.5 Replaying its own recordings: `replay.source`

```yaml
replay:
  source:
    data: recordings/{name}        # run-relative directory this instance records into
    binding: direct                # optional
    overrides:                     # config patch that flips the instance to replay
      camera: {type: replay}
      replay: {path: "{{replay_input_path}}", retime: "{{replay_retime}}"}
```

Rig finds the instance's recordings under `data` in the source run, deep-merges `overrides`
over the instance config for the session, and substitutes the variables below. It never
interprets the override keys. A variable with no value deletes its key, so the service's own
default applies.

| Variable | Value |
| --- | --- |
| `replay_source` | Absolute path of the source root: the run, or a prepared copy of it. |
| `replay_session` | The session prefix the operator selected, if any. |
| `replay_retime` | `original` under the simulated clock, `wall` otherwise. |
| `replay_epoch_unix_ns` | Start of the bag timeline, in nanoseconds; absent without a bag. |
| `replay_from_s`, `replay_to_s` | The replay window in seconds from the bag start; the end is absent when open. |
| `replay_start_at_unix_s` | The wall-clock release instant. Absent under coordinated startup. |
| `name` | The instance name. |
| `replay_input_path` | With `binding: direct`: absolute directory of the selected original or proxy recording. |
| `replay_input_sessions` | With `binding: direct`: list of selected recording session ids; empty means all. |
| `replay_input_proxy` | With `binding: direct`: identity of the selected export, or absent. |

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| R6 | MUST | With the overrides applied, replay the recordings instead of opening the live source, and never modify them. Rig fingerprints the inputs before launch. | `rig replay` |
| R7 | MUST | Hold output until the release instant and place data on the bag timeline using the epoch, window and retime variables. | — |
| R8 | MUST for `binding: direct` | Read the selected directory and sessions in place, including a recording made under another instance name and a subset of sessions. Direct binding is required to replay exported proxies. | `rig replay` |
| R9 | MUST when `image_labels` is declared | Set each listed label on the built image, and report that image as a role of the same name from `runtime-images`. Rig refuses an image without the label, which is how an old image that would ignore the binding is caught. | `rig replay`, `rig bake --include-replay` |

Rig reads camera-style sidecars in a source's data directory: `<prefix>.json` with
`pixel_format`, `width` and `height`, a `<prefix>.csv` of timestamps, and
`<prefix>-NNNNN.mkv` segments. A source service with a different layout should keep other
top-level `*.json` files out of that directory.

The run directory `recordings/<name>/` is treated as replayable data. If a run holds a
non-empty one for an instance whose service declares no `replay.source`, replay refuses that
instance with "service has no replay.source support". A service that cannot replay its
output should record under a different directory name.

### 16.6 Coordinated startup

With `rig replay --startup coordinated`, every producer starts held and all are released at
one instant. A recording source takes part by declaring `verbs.replay-capabilities`, a
read-only verb that prints a JSON object on stdout within 10 seconds:

```json
{"coordination": {"version": 1, "components": ["camera"],
                  "image_labels": {"<role>": {"io.rig.replay.coordination": "1"}}}}
```

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| R10 | MUST when advertised | Implement session protocol v1 as the component `camera`, the name Rig assigns to every recording source. Mount `RIG_REPLAY_CONTROL_DIR` into the container, follow `command.json`, and write `statuses/<RIG_REPLAY_PARTICIPANT>.camera.json` atomically, refreshing it at least every two seconds while not finished. | `rig replay --startup coordinated` |
| R11 | MUST when advertised | Carry the label `io.rig.replay.coordination=1` on the image that actually runs, reported as a role from `runtime-images`. | `rig replay --startup coordinated` |

The files, operations, states and timeouts are specified in
[Session protocol v1](replay-coordination.md#session-protocol-v1). Rig compares all
timestamps with the host clock, schedules the release two seconds ahead, and needs every
acknowledgement 0.3 seconds before it.

An ordinary consumer implements no protocol. Rig proves it ready from the recorded graph, so
its node names and namespace must be stable between the recorded run and the replay.

### 16.7 Simulation attach

`rig sim` selects infrastructure and autonomy instances only. Beyond 16.1 and 16.2:

- A service's `interface.publishes` must not include `/clock` or a topic the simulator
  publishes. That is an error; a missing `interface:` is a warning.
- After `up`, every container must be running and none unhealthy ([H4](#10-health-and-shutdown)).

### 16.8 Variables for the bag player

`RIG_REPLAY_SOURCE`, `RIG_REPLAY_PLAN`, `RIG_REPLAY_FROM_S`, `RIG_REPLAY_TO_S`,
`RIG_REPLAY_START_AT_UNIX_S`, `RIG_REPLAY_TOPICS`, `RIG_REPLAY_EXCLUDE`,
`RIG_REPLAY_SERVICES` and `RIG_REPLAY_CALLS` are the bag player's channel. Rig sets several
of them for every launcher in a session, and an ordinary service ignores them. A recording
source takes its inputs from the config variables in 16.5, not from these.

## 17. Capability: run export

Declared by `export:`. `rig run export` asks a service to write a smaller form of the data
it recorded into a run.

```yaml
export: {data: "bags/{name}"}                       # whole-run and in-place, via the launcher
```

`data` is the run-relative directory an instance writes, with `{name}` for the instance.
`modes` lists any of `whole-run`, `in-place` and `service`, and defaults to the first two.

### 17.1 Whole-run and in-place

Rig calls the launcher's `export` verb. `export` needs no `verbs:` entry.

| Variable | Value |
| --- | --- |
| `RIG_EXPORT_SOURCE` | The run directory. |
| `RIG_EXPORT_DEST` | The export directory; equal to the source for in-place. |
| `RIG_EXPORT_NAME` | The data directory's name, which may differ from the config's `name`. |
| `RIG_EXPORT_PROFILE` | The profile name; `recompress` for an in-place export without one. |
| `RIG_EXPORT_OPTIONS` | Path of a YAML file holding the profile's options for this instance, verbatim. Rig never reads its keys. |
| `RIG_EXPORT_FORCE` | `1` with `--force`. |
| `RIG_EXPORT_INPLACE` | `1` for in-place. |
| `RIG_EXPORT_LOSSY` | `1` when the operator allowed data loss in place. |

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| X1 | MUST | Whole-run: read `<RIG_EXPORT_SOURCE>/<data>` and write the result to the same relative path under `RIG_EXPORT_DEST`. Leave the source untouched. Exit 0 on success, including when there is nothing to do. | `rig run export` records the exit code |
| X2 | MUST | In-place: rewrite the data inside the run, verify the rewrite against the original before replacing it, and refuse options that drop data unless `RIG_EXPORT_LOSSY=1`. | — |

### 17.2 Service mode

For derivatives that sit beside the originals, such as transcoded video. The launcher is not
involved.

```yaml
export:
  data: recordings/{name}            # must end in /{name}
  modes: [service]
  command: [tools/export-recordings]
  default_profile: h264
  profiles: {h264: export-profiles/h264.yaml}
```

The command and every profile file exist inside the service directory and are listed in
`launch_surface`. Rig runs
`<command…> --request <request.json> --result <result.json>`, adding `--preflight` on a
first, non-converting call, with the service directory as the working directory.

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| X3 | MUST | A profile file declares `version: 1`, an `id` equal to its name, an integer `revision` of at least 1, a `format` and a boolean `lossy`. | `rig run export --service` |
| X4 | MUST | Read the request (`source_root`, `destination`, `profile`, `recordings`) and write a result with `version: 1`, the request's `profile_sha256`, `validation.ok: true`, and for each recording and session the files written, relative to the recording directory. On preflight, convert nothing; report `missing_images` or `missing_dependencies` if any, and `required_bytes`. | `rig run export --service` |
| X5 | MUST | Write only under the given `destination`, and never alter the originals. Rig hashes the outputs, rejects unexpected files, writes the export manifest and renames the directory into place. | `rig run export --service` |

## 18. Capability: graph interface

`interface:` declares the service's topics and services:

```yaml
interface:
  publishes: [{topic: fix, type: sensor_msgs/msg/NavSatFix}, /tf]
  subscribes: [{topic: rtcm}]
  provides: [{service: reset, type: std_srvs/srv/Trigger}]
  requires: []
```

A name without a leading slash is relative to the instance's namespace; a leading slash
means a shared topic. A bare string is a name with no type, and a type uses the full
`pkg/msg/Type` form. `rig graph --contract <instance>` prints the block from an observed run.

`rig graph --check` compares the declaration with a run's observed graph and only warns.
Replay does not read it. `rig sim` does, as described in
[16.7](#167-simulation-attach). Graph observation itself needs the bag logger's graph
snapshots.

## 19. Distribution

### 19.1 Vendoring and baking

`rig vendor`, registry installs, `rig bake` and run capture copy the launch surface into
`services/<service>/` and stamp it with `.vendored.yaml`. From then on the launcher runs
from that copy.

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| V1 | MUST | The vendored copy alone can run every verb for every supported configuration. | `rig bake`, in part |
| V2 | MUST | Keep `launch_surface` complete as the service grows. If the vendored launcher's `config` fails, `rig bake` skips that stack and then withholds the Compose-only scripts for the whole deployment; with `--bundle-images` the bake fails. If a missing file is only a bind-mount source, the bake succeeds and creates an empty directory in its place. | Certify the vendored copy: `rig certify --repo <deployment>/services/<service>` |
| V3 | MUST | Never edit the vendored copy. It is a derived mirror; change the source and vendor again. | — |

### 19.2 Registry package

A service is published as `services/<name>/manifest.yaml` in a registry:

```yaml
kind: service
name: widget                 # equals rigging.yaml `service:`
version: 1.2.0               # exactly X.Y.Z
source:
  repo: https://github.com/acme/widget.git
  rev: <full 40- or 64-hex commit SHA>
  path: subdir               # optional: the service directory inside a collection repo
```

| ID | Level | Requirement | Checked by |
| --- | --- | --- | --- |
| V4 | MUST | `name` matches `[a-z][a-z0-9-]*`, equals the directory name and equals the descriptor's `service:`. | `rig registry validate`, install |
| V5 | MUST | `source.rev` is a full commit SHA, and the service at that revision declares `launch_surface`. A package with only `image:` cannot be installed as a stack. | `rig registry validate`, install |
| V6 | SHOULD | Declare `examples:` at the published revision. Without it an install routes and vendors the service but cannot create an instance config. | install |

`rig pkg promote <service> --kind service --to <registry>` writes the manifest from the
routed development checkout, which must be clean with its HEAD on a remote branch. A
deployment holds one pin per service, shared by all of its instances.

## 20. Verification

### 20.1 `rig certify`

`rig certify --repo .` drives the launcher's `config` verb with values that cannot occur in
practice — registry `certify.invalid:5000`, tag `certify-tag-x`, instance names
`certifyname0` and `certifyname1`, vehicle id `42` — and looks for them in the output.

| Check | Level | Proves |
| --- | --- | --- |
| `launcher` | ERROR | The launcher exists and is executable (S3). |
| `discipline` | ERROR | `config` exits 0 and prints only a Compose document (O1, O2). |
| `project-name` | ERROR | The project is `COMPOSE_PROJECT_NAME` (I1). |
| `registry` | ERROR, WARN | Built and mirrored images come from the fleet registry (M1, M2). This includes a declared build image pulled from another registry or under an extra namespace, such as `ghcr.io/acme/widget` or `<registry>/acme/widget` for a declared `widget`. WARN for other images (M3). |
| `tag` | ERROR | Built images are pulled at `RIG_IMAGE_TAG`, composed with the platform for a matrix (M1). Reported as passed only when the output pulls at least one declared build image. |
| `build-images` | WARN, INFO | The declared `build.images` are the images the output pulls (M1). WARN when it pulls none of them, because `registry` and `tag` then examined nothing; the message lists what is pulled instead. INFO naming the declared images this config does not select, which were not examined. Counts every platform of a matrix. |
| `image-overrides` | ERROR | Declared override variables reach the rendered images (P1). |
| `ros-env` | ERROR, WARN | The fleet ROS settings reach containers unmodified (E1, E2). A container that carries either variable with another value is an ERROR, whether or not the service declares `ros_distro`. WARN when `ros_distro` is declared and no container carries them. |
| `volumes` | WARN | Declared `external_volumes` appear in the document (D5). |
| `binds` | WARN | No directory inside the service directory is bind-mounted (D6). |
| `determinism` | ERROR | Two identical runs give identical output (O3). |
| `identity` | ERROR | A renamed instance leaves no trace of the old name (I4). |
| `fixed-names` | ERROR, WARN | Renaming the instance changes every name the host sees (I3). ERROR for a `container_name` that stays the same. WARN for a network or volume whose explicit `name:` stays the same and that is not `external: true`. Names Compose derives from the project are not reported. |
| `status` | ERROR | `status --format json` exits 0 and parses (O9). Skipped without a Docker daemon. |
| `state-verbs` | ERROR | The state trio is complete and `state` answers correctly on a stopped project (T2). Skipped without a daemon. |
| `platform` | ERROR | Every declared platform renders, pulls its own tag and differs from the others (B5). |
| `build-no-cache` | ERROR, WARN | The build script, run in an isolated copy with a stub `docker`, builds exactly the declared images and honours the cache and platform variables (B1 to B4). WARN `NOT CHECKED` when isolation or the build context is unavailable. |
| `build-required` | ERROR | With `--require-build-check`: the build check completed. Use this flag in CI. |

`rig certify --emit <file>` writes the normalized `config` output. Run it on the development
machine and on the vehicle, then `rig certify --diff a b`; identical files prove O4.
`rig doctor --deep` runs certify for each service of a deployment.

### 20.2 `rig doctor`

`rig doctor` checks a deployment and reports, for the services in it: a missing or
non-executable launcher, a partial state-verb declaration, mixed ROS distributions, host-port
clashes, a vehicle platform outside a service's matrix, disagreeing base or message
providers, an undeclared no-cache capability, and instance names that are not valid ROS
names.

### 20.3 What nothing checks

A green `rig certify` does not establish these. Review them by hand.

- **A build image the checked config does not select (M1).** `registry` and `tag` examine
  the images the output pulls. `build-images` names the declared images it did not see;
  certify again with a config that selects them. When the Compose file uses an entirely
  different name from `build.images`, the result is a warning, not an error.
- **ROS settings outside the Compose environment (E1).** `ros-env` reads each container's
  `environment`. A value baked into the image, set by the entrypoint or passed on the
  command line is not visible to it.
- **Fixed names on external resources (I3, D5).** `fixed-names` treats `external: true` as a
  statement that sharing is intended, and `volumes` compares only what the descriptor
  lists. An external volume with a fixed name that is not declared passes both.
- **`up`, `down`, `logs` and `pull`.** Certify never starts a container.
- **Side effects and completeness of `config` (O5, O6)**, and concurrency safety (L7).
- **Launch-surface completeness (V2)**, unless the vendored copy is certified.
- **Capability promises**: `replay.sim_time` (R4), `replay.service_introspection` (R5),
  enforcement of `RIG_RUNTIME_IMAGE_PINS` (P4), state transitions (T1, T3), offline build
  behaviour (B6) and the export verbs.
