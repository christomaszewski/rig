# Coordinated replay release readiness — 2026-10-27 UTC

Implementation and native release acceptance are complete. All final checks below
pass. Before tagging, publish the matching companion changes and pass the pinned
GitHub Actions gate described below.

Coordinated startup remains opt-in:
`./rig replay RUN --startup coordinated`. Omitting the flag retains legacy startup,
including in artifacts created with `--include-replay`.

The working trees are ready for the release publication sequence below. No release
has been tagged or published. GitHub Actions has not run against these unpublished
working-tree changes; the native acceptance runs described here were executed locally,
on the AMD64 test host, and on the ARM64 vehicle.

## Fixes completed during release checks

- Coordinated camera playback no longer refreshes a held preview onto its ROS transport.
  That refresh injected an extra copy of the final frame and could also duplicate frames
  during recorded gaps. Legacy preview refresh behavior is preserved.
- Camera replay now reads exact Matroska fragments directly, preserving the CSV cursor
  and pacing across fragments. An intermittent `splitmuxsrc` preparation race duplicated
  an encoded frame on AMD64 and shifted the timestamp mapping. The fix passed 300
  repeated mixed-codec replays; regression coverage also verifies restarting from a
  later fragment. No frame deduplication conceals damaged recordings.
- Successful coordinated auto-end now stops exact owned container IDs gracefully before
  removing them. It honors their stop signals, allows a bounded 30-second drain, and
  refuses to seal the run after forced or unsuccessful shutdown. Startup failures still
  abort and remove only owned containers. Regression tests verify both paths.
- The integration harness now exercises real camera decoding and the production ROS
  bridge, baked `./rig`, automatic player provisioning, optional image bundles, graceful
  consumer finalization, and real failure injection. It runs on native Linux as well as
  Docker Desktop.
- Release automation now requires native coordinated acceptance in addition to the
  existing image-acquisition gate. Companion sources must be full commit SHAs.

## Results

| Check | Result |
| --- | --- |
| Rig host tests | All 79 test scripts pass; full native AMD64 suite also passed, with sandbox enforcement. Changed finalization tests were rerun after the fix. |
| Infra host tests | All 21 selected host suites pass. |
| Native ROS replay | All 13 cases pass without skips in base and source-message-overlay images on both ARM64 and AMD64. Includes latches, missing selected types, time-zero calls and windowing. |
| Runtime image regression | Record, convert, read and replay pass with FastDDS and Zenoh; apt-only and source-built message overlays pass on both architectures. |
| Camera regression | All 599 tests pass without skips on GStreamer 1.28.8, native ARM64 and AMD64; all 599 also pass on the legacy 1.20.3 runtime. An additional 300 repeated mixed-codec replays pass. |
| Combined camera/bag replay | Production launcher, decoder and bridge deliver all 125 expected frames with exact bytes and timestamps, no pre-release delivery, and no duplicates. Delayed consumer, first bag message and recorded calls pass. |
| Baked artifacts | Extracted `./rig replay` passes with a player automatically added to the artifact. Registry-style artifacts pass on ARM64/AMD64; rebuilt camera-plus-bag offline bundles pass on the vehicle and AMD64. The final AMD64 wheel also passes cold-alias loading through `./run.sh load` and graceful teardown. The vehicle combined test preserves existing shared image aliases; the separate real-recording test clears all of its unique test references before loading. |
| Timing variants | Installed-wheel tests pass for scripted time-zero calls, a fractional start window, and wall-clock calls at 2x rate. |
| Real startup failures | Readiness timeout, killed held player, Ctrl+C and a conflicting deployment using `--force` fail before release, clean owned containers, and preserve an unrelated sentinel. |
| Compatibility | Default replay succeeds with the older reader image lacking coordination support. Ordinary baked `./rig up` starts four normal containers and leaves the added player disabled; normal teardown succeeds. |
| Image acquisition | Cold registry pull, warm cache and offline identity checks pass on native ARM64 and AMD64. |
| Packaging | Wheel and sdist build; isolated wheel imports and execution pass. Linux `arch=all` Debian package builds, extracts and runs its replay entrypoint. |
| Hygiene | All three repositories pass `git diff --check`. Temporary replay containers were removed; existing local registry/build containers were preserved. |

## Actual vehicle recording

The original run `/home/uxv/logs/runs/20261009T074031Z_fast-poll` was replayed on
`orin` with freshly baked artifacts and the rebuilt custom-message overlay.
Both commands used the shipped `./rig`, coordinated startup, and automatic end:

```sh
./rig replay /home/uxv/logs/runs/20261009T074031Z_fast-poll \
  --startup coordinated --start-delay 5 --auto-end 2

./run.sh load
./rig replay /home/uxv/logs/runs/20261009T074031Z_fast-poll \
  --startup coordinated --start-delay 5 --auto-end 2 --pull never
```

The registry case started without the test image references and pulled from an isolated
loopback registry. For the offline case, that registry was stopped and test references
and content aliases were removed before loading the bundle. Both final runs took about
67.3 seconds including acquisition/startup, playback, grace and teardown.

An independent raw-message observer compared ordered serialized payload hashes against
the original MCAP: 1,249 IMU messages, 125 point clouds, 125 range images, four temperature
messages and one metadata message. All **1,504 messages matched in both cases**. All 21
source files retained their original hashes. Inputs were mounted read-only, output runs
were sealed, and owned containers were removed.

This was an isolated router/player deployment with an external verification observer
confirmed ready before release. It does not claim validation of every vehicle application.
The original run has no camera recording; camera-plus-bag acceptance used generated,
timestamp-aligned MCAP and FFV1 recordings with known payloads. Consumer readiness and
camera coordination were exercised together in that separate fixture.

## Before tagging

1. Land and publish the tested `rig-infra` player package/runtime images and
   `camera-service` package/images. Coordination requires both launcher and active image
   capability declarations; a new launcher with an old image is deliberately rejected.
2. Set Rig repository variables `RIG_REPLAY_INFRA_REF` and `RIG_REPLAY_CAMERA_REF` to those
   full published commit SHAs. Run `replay-coordinated.yml` manually with the same SHAs;
   the release workflow requires that gate again. Missing pins fail the release gate.
3. Choose/bump the Rig release version, then tag using the normal release process.
   Candidate packaging was tested at the current working version, `0.2.71`.

There is **no fleet-wide service migration**. Only the selected bag player and recorded
camera producers need coordination support. Ordinary consumers use their recorded ROS
endpoint evidence. Existing deployments retain legacy behavior until explicitly opted in.

## Evidence

Local evidence is under `var/acceptance/coordinated-release-20261027/` (gitignored):

- `rig-host-tests-final.json`, `infra/summary.json`, `faults.json`, `runtime-arm.json`.
- `release-summary.json` and `source-fingerprints.json` identify the final evidence and sources.
- `camera-modern-complete/`, `camera-legacy-complete/`, `amd/camera-tests-complete/`.
- `amd/replay-reader-stress-final.log`; earlier failing reports are retained for diagnosis.
- `arm-combined-reader-fix.log`, `ordinary-up.json`, `legacy-old-image.log`, `wheel-smokes.json`.
- `orin/actual/evidence/summary.json`, original/after hashes and per-topic payload evidence.
- `orin/combined-offline-reader-fix/summary.json`,
  `amd/combined-offline-reader-fix/summary.json`, `amd/runtime-matrix/`.
- `dist/`, final package build logs, and the source snapshot archive.

Vehicle artifacts and evidence remain in `/tmp/rig-coordination-actual/a5dcb12973` and
`/tmp/rig-coordination-rc-8d054951`. AMD64 evidence remains in
`/home/ckt/coordination-rc-8d054951`. These are isolated test directories, not installed
vehicle deployments.
