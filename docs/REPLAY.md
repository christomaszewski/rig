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
rig replay prepare RUN --recipe field-test --player public/ros2-bag-player@1.18.0
```

Use `rig replay -h`, `rig replay check -h`, and `rig replay prepare -h` for startup policy,
selection flags, recipe locations, readiness exit codes, and examples.

`RUN` accepts a run ID/label, run directory, standalone ROS 2 bag directory, or a single
bag file. Split bags must be supplied as a directory. `rig run replay ...` is equivalent.
For a source literally named `check` or `prepare`, use `rig replay -- check`.
Use rig-infra 1.18.0 or later: plan-v1 alone (introduced in 1.17.0) is insufficient;
this replay workflow also requires runtime image pinning and recorded-call inspection. Plain prepare reports a
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

`--skip-service` / `--skip-instance` omit processes, source recordings, and bag topics proved
to belong exclusively to the skipped instances. They cannot conflict
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
recorded instance name) to identify graph nodes and their observed publishers. A topic's name
alone does not prove ownership, even when it is beneath a skipped instance's namespace.
Shared or unknown outputs remain eligible, including when graph epochs are unavailable; use
exact `playback.exclude` entries to remove known topics. Automatic output suppression and skips
also preserve eligibility for `/tf`, `/tf_static`, and `/diagnostics`.
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
ordinary camera/ROS stack. When conversion is needed, Rig pulls missing native images reported
by an updated service helper, preserving the exact image reference or digest and selecting the
host architecture. It rechecks dependencies before converting; it does not build or replace
incompatible cached images. Registry authentication uses the usual Docker configuration.
`--dry-run` reports inputs, reuse, destination, missing dependencies, and planned pulls without
downloading images. Reusing an existing export also needs no pull. A writable source run
and space for the conversion are required for generation. Source recordings and bags stay intact.

H.264 proxies are lossy. They preserve frame count/order/IDs, capture timestamps, session gaps,
and geometry; pixel values change. Camera-service's profile revision 2 keeps the existing
8-bit mono, Bayer, and color conversions and adds `GRAY16_LE`, `GRAY16_BE`, and `Mono16`.
These 16-bit mono inputs use WebRTC's auto normalization: a 1st–99th percentile contrast
stretch with exponential smoothing (previous range weight 0.9), reset per recording session.
Their proxy headers describe the actual `GRAY8` output, stored as I420 H.264, and provenance
retains the original pixel format and bit depth. These are visual intensity previews, not
thermal/radiometric counts; use originals for measurements or consumers requiring 16-bit data.
Packed/high-bit-depth Bayer, other unsupported high-bit-depth formats, and invalid geometry
still fail preflight. The supported 8-bit paths retain their logical camera format.
Proxies target lower decoding cost; the normal consumer/WebRTC conversion and encoding still run.
Measure representative footage before assuming a particular CPU or storage improvement.

Update the installed camera-service checkout/bundle and its native images together before
generating revision 2 proxies. The usual export command uses the new profile hash to create a
new export identity; `--force` is not needed. Revision 1 generations remain on disk and can
still be selected with an explicit recipe generation pin.

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

## Topics and recorded calls

Broad playback is the default for new and existing consumers. It excludes ROS plumbing,
recorded clocks, and service events from ordinary data. Optional graph evidence removes outputs
exclusively regenerated by selected instances, including custom status topics. Ownership follows
the recorded ROS node namespace, resolved from the recipe override, service `ros_namespace`, or
recorded instance name. Set `instances.NAME.recorded_namespace` for historical namespace changes.
Topics with publishers outside the selected instances, unassigned publishers, or ambiguous
ownership remain eligible. The ownership model includes omitted instances as well as selected
ones, so a broad namespace cannot claim another known instance's output.
Shared `/tf`, `/tf_static`, and `/diagnostics` stay eligible under automatic suppression and skips;
live/historical contributions can conflict. Explicit topic lists, recorded subscriber selection,
or `playback.exclude` can still omit them.
Use recipe `exclude`/`keep` to resolve known outputs. Historical evidence cannot prove current
output behavior or compatibility with every new subscriber.

Without graph evidence, a namespace alone cannot distinguish a service's inputs from its outputs.
Use exact `playback.exclude` entries to suppress recorded status or other outputs that the
running instance will publish again. Broad mode never suppresses topics by namespace alone,
including for skipped sources. Unknown new consumer namespaces stay eligible.

`--selection recorded` limits topics to the named instances' observed subscriptions when
available. Its legacy fallback may use a namespace heuristic, reported as such. Topic selection
does not change the recorded-call policy and that fallback never establishes service ownership.

Recorded requests are selected **automatically** when `playback.services` is absent, in both
topic modes. A candidate must have one unambiguous recorded server owner mapped to one instance
in the entire effective started set, including infrastructure and instances selected by startup
policy. Servers that are omitted, unassigned, or ambiguous are excluded. If a selected live
client is known to issue the request, automatic injection is excluded to avoid replaying its
calls as well. Unassigned clients leave duplicate-call risk unknown and produce a warning.
Missing graph evidence never establishes automatic service ownership.

This is a default change for existing recipes. Use an explicit empty list to disable recorded
requests, or a nonempty list to choose exact targets manually:

```yaml
version: 1
playback:
  services: []  # Recorded calls off; omitting this field means automatic selection.
```

For a service declaring `ros_namespace: /control/relay`, a manual override can select recorded
requests without repeating the namespace binding:

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
recorded requests; it does not start the receiving instance. Manual lists may override uncertain
ownership or the live-client exclusion, with warnings, but still require usable request payloads.
Service names are not remapped. Recipe `services` and `calls` are mutually exclusive.
`--calls FILE` overrides recipe call selection, and either a CLI or recipe script replaces
recorded calls before their event contents are checked. An explicit `[]` is preserved during
preparation; generated recipes do not insert it to represent automatic selection.

An event topic in bag metadata is insufficient evidence that its requests can be replayed.
During preparation or execution, Rig inspects the chosen bag session and `--from`/`--to` window
using a bounded helper in the verified native player image, pinned to its image ID. It mounts
the source read-only and does not send requests. Results distinguish usable request contents,
metadata-only events, response-only events, requests from the other origin, unsupported types,
missing events, and empty windows. Automatic selection excludes confirmed unusable targets with
reasons; explicit targets with unusable data are errors. A failed inspection blocks readiness
rather than silently disabling calls.

The player's effective configuration supplies `play.services_source`: `service` is the default
and selects server-side `REQUEST_RECEIVED` events; `client` selects client-side `REQUEST_SENT`
events. Rig freezes that value with the selection and uses the same origin for inspection and
playback. If only the other origin has request contents, the report identifies the mismatch;
change this player config setting deliberately before preparing again. Recording both origins
does not cause both to play. Repeated events within the selected origin remain repeated calls,
matching native rosbag2 playback; inspection reports duplicates but does not deduplicate them.
Recorded response events are not issued as calls.

Check and dry-run read valid cached inspection evidence only: they write nothing and start no
helper containers. A cache miss reports `not_checked`; prepare or execute with the required
local image to inspect the calls. Prepare never pulls images. The cache binds the session,
window, origin, player configuration, image pins, helper files, and source-file identities.
Its media identity checks detect ordinary changes; they are not a cryptographic verification
of every bag byte.

The player launcher must advertise `inspect_calls: 1` in `replay-capabilities` and provide
`verbs.replay-inspect-calls`. Update its declared launch surfaces together with Rig. The helper
uses the same verified image as playback, seeks by received timestamp, and stops at the window
end; it does not scan the remainder of a long recording for a short replay slice.

Inspect `rig replay check --format json` for `ownership`, `topic_selection`,
`service_selection`, and `call_inspection` evidence. Topic records include original/remapped
names, publisher owners, selected producers, the decision reason, and explicit `keep` overrides.
Call records include server/client ownership and exclusions; inspection adds request counts and
content status. The launch summary lists the final call targets and topic counts, and the new
run retains the resolved evidence. Remapping into a known regenerated output is an error unless
the source topic is explicitly kept. Structured replay warns when it overrides standalone
player `play.topics`, `play.exclude`, `play.services`, or top-level `calls`; use the corresponding
recipe `playback` fields (or `--calls`) instead.

## Timing and legacy inputs

One exact logger/session controls topics, clock, windows, call-script zero, and camera alignment.
Recipe bag settings override player config. Multiple unexplained loggers require a choice.
`latest` resolves to one concrete session; split files form one session.
`--session PREFIX` retains its separate camera-recording meaning.

`--from`/`--to` count seconds from the chosen bag start, including fractional offsets. The start
is inclusive and the end exclusive for structured replay, matching recorded-request inspection.
They override standalone player offsets; omitting `--to` keeps the replay window open to the bag end.
Remaps apply to bag playback and restored
pre-window latched messages. Empty data selections retain a clock-only timeline. `--wall-clock`
disables `/clock`; source-only replay without bags uses wall time. The release deadline is
computed after source verification and run capture, immediately before final configuration
snapshotting and startup. `--start-delay 0` still provides a release timestamp so paused
sources resume as they start. A positive deadline that expires before any launcher starts
blocks startup; slow launchers that outlast it produce a warning. This timer does not prove
that every consumer is ready; coordinated readiness remains planned.

Canonical runs without captures/epochs work directly. Standalone bags are copied into a derived
layout. Invalid/missing indexes are rebuilt there with a bounded, network-disabled helper using
an existing local ROS image. Originals are never repaired or moved. MCAP and SQLite ROS 2 bags
are supported; ROS 1 and cross-schema conversion are deferred.

Camera-service with direct bindings uses aliases/external directories and exact session lists
in place; services without that capability retain the prepared layout adapter. Supported sidecars,
CSV timestamps, and video segments identify complete sessions. Automatic selection uses observed
time overlap; missing timing requires explicit sessions. No historical identity, epochs, or
capture times are fabricated. Player and camera inputs mount read-only; new outputs stay writable.

## Runtime images

Execution defaults to `--pull missing`. Rig asks each selected launcher for its active images
using the resolved replay configuration, including the replay-only player and enabled
sidecars. It fetches missing Linux images for the Docker engine's native CPU before checking
image capabilities or creating a replay run. A cached image for another CPU counts as missing.
A warm cache needs no pull. Downloads print Docker's progress.

```sh
rig replay RUN                         # pull missing native images, then validate and start
rig replay RUN --pull always           # explicitly refresh the selected registry references
rig replay RUN --pull never            # require locally cached images
rig replay check RUN --json            # report exact images and proposed pulls; never download
```

`check`, `prepare`, and `--dry-run` never pull. Missing images prevent `ready_to_run` but do
not imply a source recording is incompatible. Metadata repair during preparation requires
an already verified local player image. A cached native image lacking a required capability
fails under `missing`; use an updated release or explicitly refresh with `always`. The
player image must carry `io.rig.rosbag2.replay=1`, applied only after the fleet-ros build
verifies the replay CLI, ROS Python APIs, service-event types, and MCAP/SQLite readers and
reindexing. Rebuild the base/messages images along with updated player launch surfaces;
an old image does not gain this capability label when Rig or the launcher is updated.

When a companion catalog exists, every selected image must have a matching native child
digest. Registry relocation retains that digest. Overrides such as `BAG_PLAYER_IMAGE` are
part of the inventory and must also have matching catalog entries. Without a catalog, Rig
validates the selected local image IDs and capability labels. Bundled replay checks the
recorded local aliases and identities, never contacts a registry, and rejects `--pull always`.
Missing bundled companions require loading or rebuilding the bundle.

After validation, Rig passes each launcher its exact reference-to-image-ID mapping. The
launcher rechecks the active image set, removes build instructions, and launches the frozen
Compose model with pulling disabled. A tag changing after validation cannot change the image
that starts. Reports and `.rig/replay/resolved.yaml` retain the inventory and validated IDs.

This requires updated launch surfaces: `verbs.runtime-images` must advertise the version-1
inventory and enforce `RIG_RUNTIME_IMAGE_PINS` on launch. Camera-service, dashboard, and the infra
player/logger/router implement this contract. Upgrade other replay-selected services before
using this Rig version; there is no fallback to guessed `build.images`. Updating Rig alone
does not update a deployment's vendored service bundles.

## Upgrading an existing deployment

Update selected service code as well as Rig. Reconstructing an old run preserves its existing
player; `replay prepare --player ...` only supplies a missing player. It does not upgrade one.
For vendored registry services, swap to the compatible release while preserving instance configs:

```sh
rig swap ros2-bag-player public/ros2-bag-player@1.18.0
rig swap ros2-bag-logger public/ros2-bag-logger@1.18.0
rig swap zenoh-router public/zenoh-router@1.18.0
rig swap camera-service public/camera-service@1.11.0
# If installed: rig swap ros1-bag-logger public/ros1-bag-logger@1.18.0
# For locally routed dashboard code, point at its updated checkout:
rig swap dashboard /path/to/dashboard
```

Swap only installed services; names above are service keys, not instance names. Check local
routes in `services.yaml` and registry pins in `rig.lock` before rebuilding. The dashboard
launcher must include `runtime-images` support even if its Docker images were already built.
MAVLink router is outside this release's supported replay set; deployments using it must omit
it from replay with `replay: {start: false}` or `--skip-service mavlink-router`.

Build/publish the deployment's base and messages overlay from updated sources with its normal
`rig build --registry REGISTRY --tag TAG` workflow. The overlay must retain the fleet's custom
service interfaces. Confirm the player image carries `io.rig.rosbag2.replay=1`; rebuilding just
Rig or the player launcher does not add the label. Refresh the deployment's image catalog
through the build workflow and rebake offline bundles. `--pull always` refreshes mutable refs
but cannot replace a pinned catalog digest with different contents.

After changing service routes or image catalogs, rerun `rig replay prepare` to regenerate the
plan and request evidence, then inspect `rig replay check --format json`. Ordinary execution
acquires missing images; prepare/check need them cached already. Review automatic call targets
before the first replay: an absent recipe `playback.services` now enables automatic selection,
while `playback.services: []` keeps recorded requests disabled. Full vehicle acceptance and
coordinated startup readiness are separate from the packaged replay tests.

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

Startup stops at the first failed launcher. Rig stops newly launched containers before log
capture, then removes attempted projects that had no containers before their launcher ran.
It retains preexisting projects for inspection.
The run remains open with a failed/cancelled startup outcome. Startup timings, attempted
instances, the release deadline, and cleanup results are saved in `.rig/replay/execution.yaml`
and the session directory. Proxy verification prints each file and its byte count before
hashing. Shared exports and originals are hashed once per invocation, with fresh file-identity
checks before reuse; a later invocation verifies them again.

Ordinary replay returns after startup: exit 0 means the launchers succeeded, not that playback
finished. With `--auto-end`, Rig inspects the exact bag-player container and any required call
injector, checks their identities and exit codes, and waits for both to succeed. Paused,
restarting, and created containers remain active; missing, replaced, stale, or uninspectable
containers never authorize teardown. Failed participants and repeated inspection failures
return nonzero and leave the session up for inspection. Completion evidence is saved in
`.rig/replay/completion.json` before successful log capture, teardown, and sealing. Ctrl+C
during completion monitoring leaves the session running; use `rig down --end-run` when ready.

Suite packaging is deferred. Recipes have no registry dependency; future vehicle packages can
carry recipes and relative auxiliary files while bindings and generated state stay local.
Existing vehicle-package round trips preserve instance `replay.start`.
