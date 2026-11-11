# Replay recorded runs on the vehicle

Build and bake the deployment on your workstation, then collect, export and replay runs on the
vehicle. The extracted artifact contains its own Rig and service launchers; it does not need a
source checkout or reconstructed deployment. The vehicle needs Python 3 + PyYAML and Docker
Compose. The bare-Docker `up.sh` path alone does not provide the replay CLI.

For live Isaac input, use [offline Isaac attachment](ISAAC_OFFLINE.md). Its recordings become
normal replay sources; Isaac does not need to be running to replay them.

## Build and bake on the host

Set the vehicle runtime and CPU in `vehicle.yaml`, independently of the build machine:

```yaml
platform: jp6m
arch: arm64
images:
  registry: registry.lan:5000
  tag: field-v1
```

For an ARM64 Mac building a Jetson deployment and native development helpers:

```sh
rig build --scope deployment,host --tag field-v1 --dry-run
rig build --scope deployment,host --tag field-v1
rig bake --tag field-v1 --include-replay
```

`deployment` builds enabled vehicle services for the declared target. `host` adds enabled
services for the host's development runtime and Docker CPU; on an ARM64 Mac that is normally
Linux ARM64 with the `dev` camera runtime. Both scopes include shared build providers. The image
registry must be reachable from the vehicle; see [registry setup](BUILD_AND_REPLAY.md#targets-and-scopes)
when the host uses `localhost` and the vehicle uses a LAN address.

Add `replay` to the scope when you need disabled consumers selected by replay policy or portable
replay variants for all supported CPUs:

```sh
rig build --scope deployment,host,replay --tag field-v1
```

It is not mandatory for every onboard replay. Inspect the dry-run for the instances and image
variants you intend to use. The replay scope follows deployment replay policy, not a particular
recipe, so ensure optional consumers are included in that policy or build them explicitly.
Baking never builds missing images. The bake `--tag` names the artifact; `images.tag` selects the
image release. Keep the latter aligned with the tag you built.

`--include-replay` adds a missing `ros2-bag-player` only inside the baked artifact. You do not need
to add a player to the host's `vehicle.yaml` first. The generated instance is disabled for normal
startup, has `play.loop: false`, and uses an unused name such as `bag_player` or `bag_player_2`.
With exactly one enabled bag logger, it also defaults to that logger's recorded instance name.
Multiple logger inputs are selected from the run or recipe later.

Player discovery checks service routes, exact lockfile pins, nearby checkouts, then configured
package registries. If necessary, choose the source for the automatically added player:

```sh
rig bake --tag field-v1 --include-replay --replay-player /path/to/rig-infra
# Or select a pinned package from a configured registry:
rig bake --tag field-v1 --include-replay --replay-player public/ros2-bag-player@1.20.0
```

Existing player rows and configs are preserved; `--replay-player` does not replace them. Update
an existing player through its service route/package before baking. Bag proxies and multi-bag
replay require the player's plan-v2, multi-input and export capabilities plus matching images.
The current bake check accepts supported plan/recorded-call inspection versions 1 or 2 and
checks the extra image labels when the provider advertises multi-input support.

Bake queries the resulting launchers and records the default onboard replay image plan in
`metadata.yaml.replay`, also shown by `rig artifact show field-v1`. It uses the vehicle's runtime
and CPU, requires a known target CPU, and does not inspect a historical run. A recipe that selects
additional consumers can require images beyond this default plan.

## Collect a run and select its inputs

After transferring and extracting the artifact, work from its directory:

```sh
./rig pull                  # enabled instances for normal deployment startup
./rig up
# Collect the run, then stop the deployment and close the run.
./rig down --end-run
./rig runs
```

A plain `./rig pull` does not select disabled rows such as the generated bag player and uses
normal deployment configuration. Real replay and prepare acquire the recipe's selected runtime
images with `--pull missing` by default, including replay-specific camera variants and the player.
**Preparation pulls require Rig v0.2.83 or later**; older Rig versions only report them.
To preload just the player explicitly, use `./rig pull bag_player`, substituting its actual name.
`replay check` and dry-run commands never pull missing replay images.
Export helpers have their own native image requirements; a real export can acquire missing
camera converter images after reporting them, while an export dry-run only reports the plan.

A direct replay uses default startup and original inputs:

```sh
./rig replay check RUN
./rig replay RUN --auto-end
```

Use a version-2 recipe when you want an exact process selection and per-input proxy choices.
Create **one file**, for example `replay/analysis.yaml`. Replace the instance names and logger
names below with those in your deployment and recorded run:

```yaml
version: 2
session:
  instances: [zenoh_router, cam_front, cam_rear, feature_detector]
  clock: source
replay:
  bags:
    - {logger: bag_front, session: all}
    - {logger: bag_rear, session: all}
  inputs:
    bags: {profile: sensor-conservative}
    instance:cam_front: {profile: h264}
    instance:cam_rear: {use: original}
  playback:
    exclude: [/feature_detector/output]
```

`session.instances` contains destination **instance names**, including required infrastructure,
camera sources and consumers. The player is managed automatically and must not be listed.
Include an output bag logger only if you want to record replay outputs. The `logger` fields refer
to recording directories under `RUN/bags/LOGGER/`, usually the original logger instance names;
those source loggers do not have to run in the replay deployment. Use `bags: all` for every source
bag, or list only the sessions you want. The selected set becomes one bag proxy.

Omitted input selectors use originals. `h264` is camera-service's current proxy profile;
`inputs.instance:NAME` selects it for that camera independently. To preview proxy video while
feeding full-quality recorded ROS images to algorithms, add this only when the selected bags
contain the camera's matching image topics:

```yaml
# Under replay.playback:
image_sources:
  cam_front: bag
```

That disables the camera's ROS image bridge while keeping its video preview. `camera` instead
selects camera-produced ROS images and suppresses their matching bag topics. See
[replay inputs and image authority](REPLAY.md#one-recipe-for-original-and-exported-inputs).

`sensor-conservative` schedules ordinary messages from their sensor headers and retains ambiguous
cross-bag repeats. It does not collapse ordinary byte-identical IMU, SBG or point-cloud messages.
Choose `sensor-aggressive` for equal-payload cross-bag matching within the receive-time window,
or `arrival-conservative` to preserve recorded receive-time scheduling. Original multi-bag
playback performs no live deduplication. See [bag timing profiles](REPLAY.md#bag-merge-and-timing-profiles),
including TF handling and timestamp caveats.

## Export, prepare and replay with the same recipe

```sh
RUN=your-recorded-run-id
./rig run export "$RUN" --recipe replay/analysis.yaml --dry-run
./rig run export "$RUN" --recipe replay/analysis.yaml
./rig replay prepare "$RUN" --recipe replay/analysis.yaml
./rig replay check --recipe replay/analysis.yaml --format json
./rig replay --recipe replay/analysis.yaml --auto-end
```

The recipe is your editable configuration. Export creates immutable proxy generations inside
the original run's `exports/` tree. Prepare saves the resolved source path, selected generations
and derived state under `var/replay/`; edit the recipe, not those generated files. No second recipe
or `sessions/` directory is required. Using the explicit path also avoids ambiguity if a
same-named file exists under `sessions/`.

Real prepare pulls missing selected runtime images, validates them, and may run bounded repair
or recorded-call inspection helpers. It does not start deployment services. To refresh cached
references, add `--pull always`; to prohibit image downloads, use `--pull never`. A preparation
preview stays read-only with either policy:

```sh
./rig replay prepare "$RUN" --recipe replay/analysis.yaml --pull never
./rig replay prepare "$RUN" --recipe replay/analysis.yaml --dry-run --pull always
```

Each recipe keeps its own prepared source binding. Preparing another recipe for another run does
not change this one; preparing this same recipe again replaces its current binding. An explicit
`RUN` on a check or replay command overrides the source for that invocation without changing the
saved preparation. After changing topic exclusions, process selection or input selectors, prepare
that recipe again. Topic exclusions do not require re-exporting a bag; a changed proxy profile or
conversion policy does. Changing an exact proxy ID requires selecting the new generation explicitly.

For repeated unchanged replays, Rig v0.2.81 adds `--fast`:

```sh
./rig replay --recipe replay/analysis.yaml --fast --auto-end
```

This reuses previously computed hashes when the files' identities and timestamps still match.
Normal prepare/replay warms the local cache; missing or changed entries are hashed normally.
Runtime images, recipe/config changes and startup checks still run. Omit the flag for fresh byte
verification. See [faster repeated replays](REPLAY.md#faster-repeated-replays) for the scope and
limits; the separate legacy startup delay is still controlled by `--start-delay`.

The first positional argument to `rig replay` is always the run. With a version-2 recipe, select
processes by editing `session.instances`; `--include-service` and `--skip-service` are rejected.
Version-1/default workflows also support repeated `--include-service SERVICE`, where `SERVICE`
is the `service:` package name and selects all matching instances, not one `name:` value.
See [process selection](REPLAY.md).

`RUN` may be a catalog ID or an absolute directory, including a run collected with another
artifact. Replay uses the **current deployment's** service configs, launchers and image catalog;
the source run supplies recordings and provenance. Compatible message types, topic names and
recording formats are still required. Code/config changes can change algorithm results even when
the data is compatible. Reconstruct the source deployment when you instead need its captured
configuration and source/image provenance; see [reconstruction](BUILD_AND_REPLAY.md#reconstruction-and-selection).

The camera converter likewise uses the current deployment's image selection. Its native `cam-dev`
image is a separate requirement even on Jetson. Inspect `vehicle.yaml` `images.registry` /
`images.tag`, effective `CAM_DEV_IMAGE` overrides, and the `rig.lock.build_catalog` entry when an
export proposes an unexpected image. `metadata.yaml.image_tag` identifies the deployment's image
release; the artifact's own `tag` is not an image tag.

## Startup, windows and teardown

Replay refuses to start over a running deployment. It mounts source recordings read-only and,
when `data_dir` is configured, opens a new provenance-linked run for outputs. `--auto-end` waits
for the player to finish, then tears down replay and closes that new run. It does not restart
live collection; use `./rig up` when ready to resume it. `./run.sh replay` also forwards to Rig.

Startup remains `legacy` by default. With coordinated-capable player/camera packages and images,
opt in with the same recipe:

```sh
./rig replay check --recipe replay/analysis.yaml --startup coordinated
./rig replay --recipe replay/analysis.yaml --startup coordinated --auto-end 0
```

Only participating replay producers need the coordination protocol; ordinary consumers use their
recorded ROS endpoint evidence. The explicit check verifies selected packages, images and source
run evidence. Bake's basic replay capability check alone does not promise coordinated support.
See [coordinated replay](replay-coordination.md).

Use `--from 10 --to 40` to select a window. A cold image cache may make check report missing images
that a normal replay would fetch. For TF-dependent analysis, allow preceding dynamic transform
data to warm the buffer when starting inside a recording.

## Optional offline replay

On the build host:

```sh
rig bake --tag field-offline --include-replay --bundle-images
```

This bundles the union of normal startup and default onboard replay images for the deployment
CPU, including the disabled player and replay-mode cameras. Required replay image labels are
checked before publication. A plain `--bundle-images` artifact covers only normal startup.
The bundle does not include arbitrary later recipe/config changes, other runtimes/CPUs, or every
proxy conversion helper. Prepare proxies beforehand or provision those helper images separately
before going offline. Templated fleet artifacts support the same options when their launchers
can plan image requirements without resolved vehicle identity.

Normal startup loads bundled images. For replay before the first `up`, or after clearing the cache:

```sh
./run.sh load
./rig replay --recipe replay/analysis.yaml --pull never --auto-end
```

Bundled replay requires the recorded image identities and never fetches replacements. Existing
artifacts retain their bundled Rig and launcher versions; bake and ship a new artifact to adopt
updated replay behavior.
