# Service exports and opt-in replay proxies implementation plan

Historical checkpoint: retain the dated performance measurements and their limits below.
Use [REPLAY.md](REPLAY.md) for current behavior and the
[v0.2.72 readiness report](releases/coordinated-replay-readiness-2026-10-27.md) for subsequent
native replay acceptance; those results do not establish a general proxy performance gain.

Status: implemented, 2026-10-21. Scope: Rig and camera-service. This document
preserves the design and acceptance criteria. [REPLAY.md](REPLAY.md) describes the
implemented commands; suite packaging remains deferred.

Validation: all 65 Rig test scripts passed, with focused export/replay/sharing tests
rerun after review fixes. Native ARM64 GStreamer conversion and a real Rig-to-Docker
export verified frame counts and timestamps. In a synthetic 300-frame 1280x720
sample, proxy video was 87% smaller and one-stream replay used about 31% less process
CPU; three concurrent streams used about 4% more CPU, with all 900 frames delivered
and no drops in either mode. These results do not establish a general multi-camera
CPU improvement. Representative footage, full WebRTC deployment performance, and
native AMD64 execution remain unmeasured. Revision 1 is an opt-in lossy profile,
not a promise of lower CPU for every workload.

Camera-service will define a default `h264` recording proxy profile. Rig will
generate reusable, service-only exports beneath the original run and use them
for replay only when the caller supplies `--use-proxies`. Bags and other unchanged
run contents stay in their original locations.

## 1 Agreed behavior

- Camera-service owns its profile definitions, conversion implementation, recording
  format, and compatibility checks. Rig owns discovery, orchestration, storage,
  selection, and provenance. No camera encoding settings are required in `vehicle.yaml`.
- `rig run export` performs offline conversion. `rig replay prepare` validates and
  binds existing exports; neither preparation nor replay generates proxies implicitly.
- Original recordings remain the default on every invocation. Creating exports,
  preparing with proxies, or saving proxy preferences in a recipe does not change that.
- Exports contain only transformed service data and their manifests. They are not
  new runs and contain no copied or hardlinked bags, run manifests, or unchanged services.
- A committed export is immutable and portable with its source run. Another user
  can consume it without the producer's deployment, absolute paths, or local cache.
- Existing deployment-defined whole-run export profiles and explicit in-place
  exports remain compatible. Camera's new default must never opt it into destructive
  in-place conversion or an unrelated existing export profile.
- No mandatory publisher/subscriber declarations, live subscription discovery, or
  rig-infra/player changes are required.

Suite packaging stays deferred. Keep profile definitions in the service bundle,
recipe preferences portable, and generated host bindings outside the recipe so
recipes can be packaged with suites later.

## 2 Command surface

```sh
# Use camera-service's declared default profile; currently h264.
rig run export RUN --service camera-service

# Explicit selection; no vehicle-level h264 profile is needed.
rig run export RUN --service camera-service --profile h264
rig run export RUN --service camera-service --profile h264 --dry-run

# Limit conversion by ORIGINAL recorded instance name; repeatable.
rig run export RUN --service camera-service --instance cam_front

# Generate again while retaining previously committed generations.
rig run export RUN --service camera-service --profile h264 --force

# Original recordings remain the default, even when exports are present.
rig replay check RUN
rig replay RUN

# Explicit proxy selection on check, preparation, and execution.
rig replay check RUN --use-proxies
rig replay prepare RUN --recipe laptop --use-proxies
rig replay --recipe laptop --use-proxies
```

`laptop` in this example is a local replay recipe name, not a built-in profile.
The camera profile is `h264`.

Add `--service` as a distinct service-export mode. Initially select one service
per invocation and allow repeated `--instance` filters. Without `--service`, keep
the existing deployment-profile command semantics. Reject service mode combined
with `--in-place` or `--lossy`; H.264's lossy nature is part of its declared profile
and is shown before conversion. `--force` bypasses reuse in service mode without
deleting an existing generation. Document this difference from legacy whole-run
export replacement.

Default discovery covers all valid recordings of that service in the source run,
including recorded names absent from, renamed in, or disabled in the current
deployment. Use an installed service descriptor/converter, not a requirement to
start a corresponding deployment instance. `--dry-run` reports profile revision,
inputs, supported/unsupported sessions, reusable generations, output location,
and missing runtime dependencies without writing or launching a stack.

Expose `--use-proxies` consistently on the grouped replay commands and their
existing aliases, including `rig run replay`. Check reports missing proxies with
the export command needed to produce them. Prepare can create its usual local
bindings and legacy bag repairs, but cannot transcode missing camera data.

## 3 Service-owned profile and export contract

Extend Rig's strict descriptor schema while retaining existing `export: {data: ...}`
behavior. Proposed camera-service declaration:

```yaml
export:
  data: recordings/{name}
  modes: [service]
  command: [tools/export-recordings]
  default_profile: h264
  profiles:
    h264: export-profiles/h264.yaml
```

For old descriptors, absent `modes` means the existing whole-run and in-place
launcher contract. A descriptor declaring only `service` is excluded from both
legacy exporter discovery paths. Validate modes, profile names, relative bundled
paths, and the default's existence. Do not interpolate arbitrary shell commands.

The profile file belongs to camera-service and includes a schema version, profile
ID/revision, output recording-format version, lossy declaration, supported input
formats, and concrete converter settings. Rig treats encoding settings as opaque.
Hash the canonical profile definition and record it in every artifact; changing
its meaning requires a revision. Bundle the definition and helper in the service
launch surface and converter image so registry installs behave like source checkouts.

Use a versioned request/result contract for the dedicated helper: exact source
recording/session selections, read-only source root, writable staging destination,
profile ID/hash, and selected execution image. The result enumerates produced
recordings, files, integrity hashes, transformations, and validation results.
The converter checks that its bundled definition matches the requested hash.
Rig rejects a failed, incomplete, or mismatched result.

Use camera-service's portable native-architecture CPU image for offline conversion
on ARM64 and AMD64. Resolve and record its image identity; use existing explicit
build/pull workflows if it is missing. The helper must not start camera capture,
ROS, Zenoh, device discovery, socket volumes, or the ordinary compose stack.
Mount inputs read-only and only the staging output writable.

### Initial h264 profile

The first profile targets lower software decode cost while preserving the replay
timeline and geometry. It is deliberately lossy and is not a replacement for the
original data when pixel fidelity matters.

- H.264 in the existing segmented MKV plus CSV/JSON recording format.
- Preserve resolution, every frame, frame order/IDs, capture timestamps, session
  boundaries, and gaps. No resizing, frame-rate reduction, or timestamp synthesis.
- Start benchmark tuning with CRF 23, `veryfast`, no B-frames, one reference frame,
  constrained-baseline/no CABAC, and approximately one-second keyframe spacing.
  Freeze the exact settings as profile revision 1 after the performance gate below;
  do not change live recording defaults.
- Initially support the existing viable 8-bit mono, Bayer, and color paths.
  Undo any lossless CFA storage tiling before conversion. Preserve the logical
  consumer format and Bayer pattern, explicitly recording chroma/pixel loss and
  any conversion. Proxy headers must describe the actual new storage encoding.
- Reject unsupported high-bit-depth/thermal formats, invalid dimensions, and
  conversions that would silently discard required channels. Do not use the live
  recorder's fallback to FFV1 and label that output `h264`.
- Preflight all selected sessions before conversion; report unsupported inputs
  together. Users can limit generation with `--instance` and explicitly keep
  unsupported replay instances on originals through recipe preferences.

Reuse camera-service's recording readers, format conversions, and stamp handling;
extract shared helpers where needed instead of implementing a second interpretation
of its sidecars. Preserve capture metadata, regenerate container-dependent metadata,
and verify decoded output frame counts and stamp alignment before publishing.

This reduces the decode portion of replay CPU if benchmarks confirm the hypothesis.
It still decodes to frames for existing consumers, and WebRTC still performs its
current conversion/encoding. Encoded WebRTC passthrough and hardware encoding or
decoding are separate future work.

## 4 Storage, integrity, and reuse

```text
RUN/
  manifest.yaml
  bags/...
  recordings/
    cam_front/...                        # untouched originals
    cam_rear/...
  exports/
    services/
      camera-service/
        h264/
          <export-id>/
            export.yaml
            recordings/
              cam_front/...              # new MKV and required sidecars only
              cam_rear/...
```

The `services` namespace separates sparse artifacts from existing
`exports/<deployment-profile>/` whole-run exports. Reserve it for new writes;
detect a pre-existing legacy profile named `services` and report the conflict
without changing it. Continue allowing explicit reads of existing legacy exports.

`export.yaml` is a versioned artifact manifest, not `manifest.yaml`. It records:

| Information | Purpose |
| --- | --- |
| Service, profile ID/revision/hash, output format version | Identify meaning and reader compatibility. |
| Export ID and producer service revision/image/tool versions | Identify a generation and how it was produced. |
| Original recorded names, session IDs, relative input paths and SHA-256 hashes | Bind to the exact source recordings regardless of deployment naming. |
| Relative output paths, sizes and SHA-256 hashes | Verify a copied/shared artifact and detect partial or altered output. |
| Frame counts, capture time ranges, geometry and logical/storage formats | Validate the replay contract for each recording. |
| Lossy transformations and links to original recording identity | Preserve lineage in replay outputs and diagnostics. |
| Completion and validation results | Distinguish consumable artifacts from interrupted staging. |

Absolute paths and the producer's run location are not authoritative identifiers.
Input hashes cover relevant media and sidecars, not unrelated run files or other
exports. Export identity derives from the immutable manifest's content, excluding
the ID field itself; selection never depends on filesystem modification times.
Validate all relative paths and resolved symlink targets against their declared
roots before reading an artifact or accepting helper output.

Use a request fingerprint covering selected source content, profile definition,
export contract, and converter implementation for producer-side reuse. Validate an
existing matching artifact before returning it. `--force` performs a new conversion;
retain both generations if the result differs and deduplicate an identical artifact.
Reader compatibility is based on the recording contract/profile, not exact equality
with the producer's service commit or machine architecture.

Write to a temporary sibling directory with a per-request concurrency lock. Check
source stability, available storage, helper success, complete output inventory, and
recording validation before atomically committing the directory. On failure leave
originals and committed exports intact; clean or explicitly identify abandoned
staging. A read-only run produces an actionable error, not an implicit export into
an unrelated local cache. An open run is not eligible for export.

Hash at generation and verify selected artifacts before replay. A local verification
cache may avoid repeat full media hashing when file identity/stat evidence is unchanged;
copied artifacts on another host require verification. Check remains read-only and
reports validation that needs preparation. Never hash or verify every unselected
export just to replay originals.

## 5 Replay selection and direct bindings

Resolve startup policy, instance aliases, recording/session selection, and the bag
window first. Then apply proxy selection to the resulting recording bindings.
Skipped and live instances do not require proxies. Services without proxy support
retain their normal input behavior.

Without `--use-proxies`, ignore proxy preferences and previous proxy preparations,
and bind the original recordings. With the flag, use the service's declared default
profile unless the recipe supplies a preference. For each eligible selected instance,
require an artifact covering all selected original sessions with matching source
identities and a compatible reader contract. An explicitly requested proxy that is
missing, incomplete, stale, or incompatible is an error, with no silent fallback.

Extend recipe schema validation with optional preferences, for example:

```yaml
version: 1
source: field-run
instances:
  front_camera: {recorded_as: cam_front}
proxies:
  services:
    camera-service: {profile: h264}
  instances:
    front_camera: {export: "<export-id>"}
    thermal_camera: {use: original}
```

Instance entries use either an export pin or an explicit original choice; reject
contradictory fields and unknown names. These preferences apply only when the CLI
flag is present. They cannot turn proxies on by themselves. A pin is resolved
within the source run and must match the requested service, profile, and recordings.
If several compatible generations cover the same selection, require a recipe pin
instead of silently choosing the newest. Preparation records resolved IDs in local
bindings; later use verifies those exact IDs. A deleted pin is an error.

Keep the recipe's source pointing to the original run. Prevent a recipe from using
a recognized managed proxy directory as an ordinary `recordings.from` input to
bypass the opt-in. Do not mistake an original lossy camera recording for a derived
proxy; identify derivatives through their explicit artifact/sidecar provenance.

### Per-instance paths and session lists

The current camera override builds its path from the shared replay root and the
destination instance name. Replace that assumption for capable services with a
resolved per-instance input path and exact session list. Add a declared source-binding
capability and shared variables such as `replay_input_path` and
`replay_input_sessions`; the existing interpolator preserves whole-variable list types.
Use one binding-to-config function in planning and execution.

Camera core images advertise the direct reader through the
`io.rig.camera.replay-bindings=1` Docker label. The descriptor's
`replay.source.image_labels` maps core image names to this requirement. Preflight
inspects the selected local image, including overrides and catalog pins, so an old
image cannot silently ignore the new session selector when paired with a new bundle.

Camera-service consumes these variables through its replay overrides and gains an
exact `replay.sessions` allow-list. Keep standalone `replay.run` compatibility,
reject conflicting selectors, and validate all requested sessions. Aliases and
session subsets then work by direct binding without copying files into a renamed
directory. Bind original recordings the same way when proxies are disabled.

For proxy replay, bind each selected camera to its artifact recording directory
read-only. The bag player remains bound to the original bag or an independently
prepared legacy repair. Other services retain their original sources. Require
the new binding capability before selecting proxies with an older camera-service
installation; provide an upgrade diagnostic instead of generating a hybrid run tree.

### Preparation and provenance

Split the current source inventory fingerprint into original-run evidence and
selected derivative evidence. Exclude the managed root export area from original
run inventory, without broadly ignoring every nested directory named `exports`.
Adding or removing an unselected export must not invalidate original-run preparation.

Include proxy mode, profile hashes, selected export IDs, exact sessions, and reader
compatibility in preparation identity. Keep separate prepared states for original
and proxy execution. Remove proxy-only bindings from the conditions that trigger
copying bags and recordings into a derived source tree; retain copies genuinely
needed for legacy bag repair or services lacking direct binding support.

Preparation and execution both revalidate selected evidence. Write exact resolved
bindings and lossiness into replay plans, session configuration snapshots, and
new-run provenance. Extend camera replay provenance to retain both original capture
identity and proxy identity. Normal status output clearly says which cameras use
originals and which use a named proxy generation.

## 6 Sharing and existing run workflows

Copying a run, including its `exports/services` tree, must preserve usable exports.
Validate portability by relocating the run and replaying it against a deployment
with renamed instances. No original exporter installation is required for consumption;
the reader's compatible service bundle is sufficient.

Fleet sync currently excludes exports by default, and its existing `--profile`
option treats an export as a complete run. Add an explicit
`--include-service-exports` option to the normal run sync path. Preserve nested
artifact paths, transfer only committed generations, and detect incomplete transfers
before use. Keep existing sync defaults and `--profile` behavior; never flatten a
sparse artifact onto the destination run root.

Audit import, archive, run discovery, size reporting, and integrity checks so sparse
artifacts travel with a run when requested, are counted accurately, and are never
catalogued as separate runs. Proxy-only transfer without the original source data,
automatic pruning, and remote artifact registries are deferred.

## 7 Implementation sequence

| Phase | Changes | Completion gate |
| --- | --- | --- |
| 1. Contracts and storage | Rig descriptor/profile parsing; service-export request/result schemas; immutable artifact manifest, discovery, hashing, staging, and reuse. Primary files: `rig_cli/descriptor.py`, a new `rig_cli/service_exports.py`, and `rig_cli/export.py`. | Fixture exporter demonstrates sparse generation, reuse, failure recovery, and legacy mode isolation. |
| 2. Camera converter | `rigging.yaml`, new `export-profiles/h264.yaml` and `tools/export-recordings`; reusable recording conversion/validation helpers; image and launch-surface packaging. | Real GStreamer fixture conversion passes frame/stamp/format checks on native ARM64 and AMD64. |
| 3. Export CLI | `rig_cli/cli.py`, service discovery and filtering, dry-run/reuse/force reporting, exporter dependency diagnostics. | Commands work without matching enabled deployment instances and never duplicate unchanged data. |
| 4. Direct replay bindings | Camera `ReplayConfig`, replay source/session selection, rigging overrides, and mounting tests; Rig `replay_sources.py`, `replay_plan.py`, `replay_prepare.py`, and `replay_workflow.py`. | Original and proxy bindings work with aliases and session subsets without copying the bag for camera substitution. |
| 5. Opt-in selection and sharing | CLI/recipe validation, fingerprints, provenance, fleet sync inclusion, import/archive audits. Primary files include `replay_recipe.py`, `fleet.py`, and `runs.py`. | Defaults stay original; shared exports resolve on a second deployment; incompatible/missing selections fail clearly. |
| 6. Performance and documentation | ARM64 Mac multi-camera benchmark; help/completion coverage; Rig replay/export docs and camera profile/playback docs. | Measured benefit and limitations documented; compatibility regression suite passes. |

Camera-service ships the producer and reader capability before or with the Rig
release that exposes the workflow. Gate by declared contract versions so mixed
installations fail clearly. Release and suite publication remain separate operations.

## 8 Validation and acceptance

Rig tests must cover service-defined defaults without a vehicle export profile;
recorded names absent from the deployment; sparse output and unchanged originals;
reuse after relocation; source/profile changes; concurrent/interrupted conversion;
corrupt or incomplete manifests/media; force preserving old generations; and legacy
whole-run/in-place behavior with camera-service installed.

Replay tests must prove that no flag means originals even after proxy preparation
or with recipe preferences present. Cover explicit pins, ambiguity, missing/stale
proxies, per-instance original exceptions, live/skipped cameras, aliases, multiple
sessions, bag-window alignment, immutable session provenance, and independent
legacy bag repair. Adding an unused export must not invalidate preparation; changing
a selected export must. Assert that proxy-only preparation does not copy any bag.

Camera integration tests must decode the generated output and compare frame counts,
IDs, timestamps, geometry, logical pixel formats, and session gaps against the
originals. Cover supported mono/Bayer/color paths, tiled FFV1 input, truncated input,
unsupported high-bit-depth data, segmented sessions, and copied artifacts. Pixel
equality is not an H.264 acceptance criterion; assess visible quality separately.

For the performance gate, replay the same multi-camera source and proxy windows
on the ARM64 Mac at identical geometry, rate, and consumer settings. Use
`core-driver/tools/replay_buffer_benchmark.py` for isolated decode CPU, elapsed time,
throughput, and dropped frames, then repeat with the normal deployment to capture
WebRTC/conversion costs. Record storage size and offline generation time as well.
Require repeatable lower camera-core CPU with correct frame delivery before calling
the default profile a performance improvement. Tune and freeze revision 1 from this
evidence; do not promise that every footage type gets smaller or faster.

Update `rig run export -h`, all replay/check/prepare aliases, fleet sync help,
completion, `README.md`, `docs/REPLAY.md`, `docs/BUILD_AND_REPLAY.md`, the cheatsheet
and runbook. Camera docs describe the profile, supported inputs, artifact format,
lossiness, and direct input/session binding. Preserve the existing help-audit changes
already present in the working tree.
