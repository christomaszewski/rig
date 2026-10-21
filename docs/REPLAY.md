# Replay on a hand-built deployment

Replay uses the destination deployment's services and configs. A historical deployment capture,
graph epochs, and publisher/subscriber declarations in service riggings are optional.

```sh
rig replay check RUN
rig replay prepare RUN --recipe field-test
rig replay check --recipe field-test --format json
rig replay --recipe field-test --auto-end
# Add a missing player from a checkout or an explicitly pinned registry reference:
rig replay prepare RUN --recipe field-test --player /path/to/rig-infra/ros2-bag-player
# Or install a pinned registry version:
rig replay prepare RUN --recipe field-test --player public/ros2-bag-player@1.17.0
```

Use `rig replay -h`, `rig replay check -h`, and `rig replay prepare -h` for startup policy,
selection flags, recipe locations, readiness exit codes, and examples.

`RUN` accepts a run ID/label, run directory, standalone ROS 2 bag directory, or a single
bag file. Split bags must be supplied as a directory. `rig run replay ...` is equivalent.
For a source literally named `check` or `prepare`, use `rig replay -- check`.
The player must advertise plan-v1 support (rig-infra 1.17.0 or later). Plain prepare reports a
missing player without choosing one. `--player` requests a `bag_player` row in `vehicle.yaml`
with `enabled: false, order: 999`, its `services.yaml` route, and
`config/autonomy/bag_player.yaml`; registry installation also vendors and pins the service.
Normal `rig up` leaves this row off; replay starts it when a bag is selected. Path wiring prints
a manual snippet when the YAML layout cannot be safely edited. Existing rows are preserved;
update old launchers explicitly with `rig swap`. Prepare does not pull/build images or start
deployment services.

## Startup policy

```yaml
infra:
  - name: radio
    service: doodle
    config: config/infra/radio.yaml
    replay: {start: false}
autonomy:
  - name: new_planner
    service: planner
    config: config/autonomy/new_planner.yaml
    enabled: false
    replay: {start: true}
```

True positively selects an instance even when normal `up` omits it. False omits its process and
keeps its bag data eligible. Absent means automatic selection: enabled infrastructure, detected
recording sources, and the player. Descriptor default-true means eligible, not force-start.

CLI names, `--live`, and `--include-service` override policy. Instance policy beats service policy,
which beats descriptor defaults. Row `replay.start` and top-level `replay.instances.NAME.start`
are aliases; contradictions in one deployment fail. Local instance policy overrides deployment
policy; local service policy does not erase an explicit instance choice.

`--skip-service` / `--skip-instance` omit processes and associated bag data. They cannot conflict
with explicit inclusions. The player cannot be disabled. A selected recording-capable service
without usable recordings requires explicit `--live` before hardware can start.

To replay without camera feeds, skip the whole camera service:

```sh
rig replay check RUN --skip-service camera-service
rig replay prepare RUN --recipe no-cameras --skip-service camera-service
rig replay --recipe no-cameras --skip-service camera-service
```

Repeat skip flags on every check, prepare, and replay invocation; they are not saved as recipe
defaults. Use `--skip-instance cam_front` to omit only one destination camera. Skipped cameras
are not started and their recordings/proxies are not selected or validated, even with
`--use-proxies`. Other replay inputs and consumers remain eligible.

Bag filtering uses recorded namespace bindings (recipe override, service declaration, then
recorded instance name) and graph evidence for exclusively owned topics. Shared topics outside
skipped namespaces remain available. Without graph epochs, remapped camera topics outside those
namespaces need explicit `playback.exclude` entries.
`playback.keep` cannot restore a topic removed by a skip flag.

## Recipes and generated state

Editable recipes live at `replay/NAME.yaml`; explicit YAML paths also work. Without `--recipe`,
prepare chooses a source-derived name and prints it.

```yaml
version: 1
source: 20260827T100000Z_field
bag: {logger: bag_logger, session: latest}
instances:
  new_planner: {recorded_as: planner}
  cam_front: {recorded_as: old_camera}
  relay: {recorded_namespace: /control/relay}
recordings:
  cam_front:
    from: recordings/old_camera
    sessions: [cam-20260827T100000Z]
playback:
  topics: all
  exclude: [/planner/trajectory]
  keep: [/tf, /tf_static]
  remap: {/old_gnss/fix: /gnss_primary/fix}
```

`recorded_as` selects the historical instance identity and default recording lookup; it defaults
to the destination instance name. `recorded_namespace` independently selects the recorded ROS
node namespace used to associate graph topics and service servers with that instance. It accepts
an absolute namespace, including nested namespaces such as `/control/relay`. A namespace-only
entry is valid and does not change recording lookup. It overrides the current service's
`rigging.yaml` declaration, making it useful when a historical run used a different namespace.

A service can declare its usual ROS **node** namespace once, at the top level of `rigging.yaml`:

```yaml
service: relay-service
ros_namespace: /control/relay
```

Use an absolute, non-root ROS namespace, or a quoted template such as
`ros_namespace: "/robot/{name}"`. Only `{name}` substitution is supported. Replay expands it
with `recorded_as` when present, otherwise the destination instance name; `rig graph` uses the
current instance name. No publisher/subscriber declarations are needed. The field describes
where nodes actually run; it does not configure a launcher, change the nodes' namespaces, or
remap topics and services.

Namespace precedence is recipe `instances.NAME.recorded_namespace`, then the service's
`ros_namespace`, then `/<recorded_as>` or `/<destination-name>` for undeclared services.
Distinct instances claiming the same fixed namespace are ambiguous; use a per-instance template
or explicit recipe overrides matching their actual recorded namespaces. Inspect the resolved
bindings and diagnostics with `rig replay check`.

Neither field renames topics or service calls. Use `playback.remap` for topic remapping; service
targets remain their recorded names. `recordings.from` is source-run-relative or an explicit absolute external directory.
Auxiliary `playback.calls` paths are recipe-relative. Topic selections use exact absolute names.

An explicit `RUN` overrides the recipe source. Prepare stores the concrete local path in
`var/replay/NAME/bindings.yaml`. Generated `resolved.yaml`, `report.yaml`, `provenance.yaml`,
`config/`, and optional derived `source/` live under `var/replay/NAME/<generation>/`.
Changed source inventories/bindings produce a new generation, preserving existing source mounts.
Repeated preparation preserves recipes and detects edited generated files.
To add positional instances while using a recipe, supply the source explicitly:
`rig replay RUN planner --recipe field-test` (the first positional argument is always `RUN`).

Original-run fingerprints use size/mtime inventories for media and hashes for small control files;
they are not full cryptographic verification of recording bytes. Selected service exports also
verify SHA-256 inventories of their source and proxy files. Prepared inputs are revalidated.

## Offline camera proxies

Camera-service owns the versioned `h264` export profile and its offline converter. Install a
camera-service bundle/image that supports service exports and direct replay input bindings.
No `export_profiles` entry or codec settings are needed in `vehicle.yaml`.

```sh
# Generate all camera recordings using camera-service's default h264 profile.
rig run export RUN --service camera-service
rig run export RUN --service camera-service --profile h264 --dry-run

# Limit conversion by original recorded instance name; repeat --instance as needed.
rig run export RUN --service camera-service --instance cam_front

# Convert again while retaining previous generations; identical output is deduplicated.
rig run export RUN --service camera-service --profile h264 --force

# Every replay/check/prepare invocation opts in separately.
rig replay check RUN --use-proxies
rig replay prepare RUN --recipe field-test --use-proxies
rig replay --recipe field-test --use-proxies
```

The service must be installed in `services.yaml`; its recorded instances need not be enabled or
present in this deployment. The helper uses a local native CPU image, without starting the
ordinary camera/ROS stack. Missing images require the usual explicit build/pull workflow.
`--dry-run` reports inputs, reuse, destination, and missing dependencies. A writable source run
and space for the conversion are required for generation. Source recordings and bags stay intact.

H.264 proxies are lossy. They preserve frame count/order/IDs, capture timestamps, session gaps,
geometry, and the logical camera format; pixel values change. The initial profile supports the
viable 8-bit mono, Bayer, and color paths. Unsupported high-bit-depth data and invalid geometry
fail preflight rather than being silently truncated or falling back to a different codec.
Proxies target lower decoding cost; the normal consumer/WebRTC conversion and encoding still run.
Measure representative footage before assuming a particular CPU or storage improvement.

Only transformed camera data and provenance are written:

```text
RUN/
  manifest.yaml
  bags/...
  recordings/cam_front/...
  exports/services/camera-service/h264/<export-id>/
    export.yaml
    recordings/cam_front/...
```

Each export ID identifies an immutable, validated generation. Existing matching exports are
verified and reused. Service-mode `--force` preserves old generations, unlike replacement of an
existing legacy whole-run export. Service mode rejects `--in-place` and `--lossy`; the profile
itself declares lossiness. Camera's default profile never joins legacy in-place exports.

Without `--use-proxies`, replay uses originals even after proxy preparation or when recipes have
proxy preferences. With it, every selected eligible camera needs a compatible export covering
its exact original sessions. Skipped/live cameras and services without proxy support do not need
one. Missing, stale, corrupt, or incompatible exports fail with a generation/upgrade diagnostic;
check, prepare, and replay never transcode implicitly or fall back silently.

Optional recipe preferences choose profiles, pin a generation, or explicitly retain originals:

```yaml
proxies:
  services:
    camera-service: {profile: h264}
  instances:
    cam_front: {export: "<full-export-id>"}
    thermal_camera: {use: original}
```

Instance names above belong to the destination deployment. Aliases still map them to original
recorded names. Preferences take effect only with the CLI flag. Multiple compatible generations
require a pin; preparation retains resolved pins, and deleting a pinned generation is an error.
The source remains the original run. Managed proxy directories cannot be supplied through
`recordings.from` to bypass the opt-in. Exact bindings, profile identity, and lossiness travel in
the new replay run's provenance. Adding an unused export does not invalidate original preparation.

Camera inputs bind directly to the selected original/proxy directories and session lists,
read-only. Selecting proxies or renaming a camera does not copy the bag into another run tree.
Independent legacy bag repairs still use their prepared copy.

Copy the run with its `exports/services` directory to share its generations. Fleet sync excludes
exports by default; opt in with `rig fleet sync --into fleet-runs --include-service-exports`.
This retains nested paths, transfers only committed generations, and verifies each before making
it available. It cannot be combined with legacy `--profile` sync. Import/archive preserve the
artifacts; catalog treats them as part of their run, and size reporting includes their bytes.

## Topics, timing, and legacy inputs

Broad playback is the default for new and existing consumers. It excludes ROS plumbing,
recorded clocks, and service events from ordinary data. Optional graph evidence removes outputs
exclusively regenerated by selected instances, including custom status topics. Ownership follows
the recorded ROS node namespace, resolved from the recipe override, service `ros_namespace`, or
recorded instance name. Set `instances.NAME.recorded_namespace` for historical namespace changes.
Topics with publishers outside the selected instances remain eligible.
Shared `/tf`, `/tf_static`,
and `/diagnostics` remain unless explicitly excluded; live/historical contributions can conflict.
Use recipe `exclude`/`keep` to resolve known outputs. Historical evidence cannot prove current
output behavior or compatibility with every new subscriber.

Without graph evidence, a namespace alone cannot distinguish a service's inputs from its outputs.
Use exact `playback.exclude` entries to suppress recorded status or other outputs that the
running instance will publish again. Legacy mapped producers may still use a labeled namespace
heuristic; an explicit ROS namespace is not evidence that every topic beneath it is an output.
Unknown new consumer namespaces stay eligible.

`--selection recorded` retains the previous subscription selector and graph-derived service
selection, applying the same recorded namespace mapping to the named instances' service servers.
Broad mode reissues no service requests unless the recipe lists `playback.services`,
or a `playback.calls`/`--calls` script is supplied. Recipe services and calls are mutually exclusive;
a CLI script overrides recipe service selection.

For a service declaring `ros_namespace: /control/relay`, its replay recipe can select recorded
requests without repeating that namespace binding:

```yaml
version: 1
playback:
  services: [/control/relay/set_mode]
  # Needed when graph evidence cannot identify this regenerated status topic:
  exclude: [/control/relay/status]
```

If the current declaration differs from the run, add
`instances: {relay: {recorded_namespace: /control/relay}}` to override it for this recipe.
Start that instance explicitly with `rig replay RUN relay --recipe relay-test`, or give it
`replay: {start: true}` in `vehicle.yaml`. Listing a service target in the recipe selects its
recorded requests; it does not start the receiving instance. Inspect `rig replay check` before
execution to see the namespace bindings, service call targets, skipped service events, and
regenerated output exclusions.

One exact logger/session controls topics, clock, windows, call-script zero, and camera alignment.
Recipe bag settings override player config. Multiple unexplained loggers require a choice.
`latest` resolves to one concrete session; split files form one session.
`--session PREFIX` retains its separate camera-recording meaning.

`--from`/`--to` count seconds from the chosen bag start. Remaps apply to bag playback and restored
pre-window latched messages. Empty data selections retain a clock-only timeline. `--wall-clock`
disables `/clock`; source-only replay without bags uses wall time. Release gates are computed at
execution, after preflight.

Canonical runs without captures/epochs work directly. Standalone bags are copied into a derived
layout. Invalid/missing indexes are rebuilt there with a bounded, network-disabled helper using
an existing local ROS image. Originals are never repaired or moved. MCAP and SQLite ROS 2 bags
are supported; ROS 1 and cross-schema conversion are deferred.

Camera-service with direct bindings uses aliases/external directories and exact session lists
in place; services without that capability retain the prepared layout adapter. Supported sidecars,
CSV timestamps, and video segments identify complete sessions. Automatic selection uses observed
time overlap; missing timing requires explicit sessions. No historical identity, epochs, or
capture times are fabricated. Player and camera inputs mount read-only; new outputs stay writable.

## Readiness and provenance

Check and previews write nothing and start no containers. Reports distinguish `ok`, `warning`,
`error`, and `not_checked`, with separate `ready_to_prepare` and `ready_to_run`. Unavailable
runtime checks and busy destinations prevent a green run-readiness result. Preparation can still
create useful state and returns 1 when runtime prerequisites remain missing.
Exit 0 means ready (or a valid preparation preview); invalid CLI arguments return 2.
`--json` aliases `--format json`.

Execution retains the clean-host guard and opens a linked run under `data_dir`. It snapshots
every launched instance, including disabled-for-normal-up consumers, and saves the recipe,
resolved plan, session, fingerprints, and call script in `.rig/replay/`. Configs under
`var/replay/sessions/` remain stable across later checks/status.

Suite packaging is deferred. Recipes have no registry dependency; future vehicle packages can
carry recipes and relative auxiliary files while bindings and generated state stay local.
Existing vehicle-package round trips preserve instance `replay.start`.
