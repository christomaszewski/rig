# Coordinated replay startup

`rig replay RUN --startup coordinated` opens every replay producer held, waits for
consumer endpoints, then releases the producers at one shared instant. Rig does
not import ROS libraries; the player image supplies the ROS readiness observer.

Coordinated startup currently requires the updated Rig, bag-player and any selected
camera-service package **and rebuilt runtime images**. Both launchers and active
image roles must advertise coordination v1. Existing images with only the earlier
replay-reader label are insufficient. No automatic fallback mixes coordinated and
timer-based producers.

The default remains `--startup legacy`. Coordinated replay is explicitly opt-in,
including in artifacts baked with `--include-replay`. The legacy mode retains image validation and
immutable image pinning; its timer cannot guarantee consumer readiness.

## Operator controls

```sh
rig replay check RUN --startup coordinated
rig replay RUN --startup coordinated --auto-end 0
rig replay RUN --startup coordinated --start-delay 2 --ready-timeout 120 --startup-timeout 300
```

In coordinated mode, `--start-delay` defaults to zero and means an optional settling
interval **after** readiness. A zero settling interval still schedules one common
release, with a two-second delivery margin. Legacy mode keeps its 20-second default.
The per-participant timeout bounds launch-to-held preparation and then arm-to-ready
readiness separately; the overall timeout includes launches, readiness, settling
and release. Both timeout options accept up to 3600 seconds. Planning, image pulls,
source verification and run capture happen before this startup deadline.

Required consumer evidence is an exact ROS node, subscription topic/type or service
name/type from the recorded graph, mapped to the destination namespace. Scripted
calls contribute only services used inside the selected half-open replay window.
A service client must also report its target available. An explicit Zenoh TCP
listener is checked separately. Duplicate node names, missing types, unknown
ownership and consumers with no meaningful endpoint evidence block coordinated
startup. The diagnostics name the missing evidence; legacy mode remains available.

These checks establish endpoint discovery and transport reachability. They do not
claim application-level callback health or prove every QoS combination. Readiness
never requires an advancing replay message, avoiding startup deadlocks. MAVLink
router readiness and source-only runs without a bag-player observer are not
supported by this first coordinated mode.

## Session protocol v1

Each execution creates a fresh UUID directory under `var/replay/sessions/`. Its
`control/` directory is mounted into participating containers. Rig writes atomic
`session.json`, `requirements.json` and `command.json` files. Participants atomically
write `statuses/INSTANCE.COMPONENT.json` and heartbeat while active.

All documents carry `version: 1`, `session_id` (32 hex characters) and `plan_id`
(the SHA-256 identity of the resolved selection, source, launcher and image evidence).
Commands include a monotonically increasing `seq` and one operation:

| Operation | Required behavior |
| --- | --- |
| `hold` | Open inputs, validate types, seek and preroll internally; withhold playback output. |
| `arm` | Permit the initial paused clock and latched-state restoration; retain the timeline hold. |
| `prepare_release` | Acknowledge the immutable future `release_at_unix_s` only while ready. |
| `commit` | Accept the matching proposal before its timestamp; release at that timestamp. |
| `abort` | End the startup hold without publishing replay data. |

Participant states are `preparing`, `held`, `ready`, `release_ack`, `running`, then
`completed`, `failed` or `aborted`. Statuses record update/held/readiness times, the
scheduled release and actual release. Rig rejects stale identities and heartbeats;
participants reject regressing commands, changed release timestamps, missing or
expired commits and unregistered identities. Retries create new sessions.

Selected message and recorded-service types must load their runtime typesupport
before the player can become held. A missing selected type fails startup; an unknown
type on an excluded topic does not block replay. The injector inherits the player's
resolved Zenoh settings and applies the selected playback rate under wall clock.

The paused clock and explicit latch restoration are the only arming exceptions.
Camera previews and recorded/scripted service calls, including a call at the
window start, remain held. Cameras preroll internally and retain their first
in-window frame. All producers use the committed timestamp as the timeline origin.
Coordinated cameras also suppress held-preview refreshes: those refreshes share the
ROS transport and would otherwise inject duplicate historical frames during gaps or
after completion. Legacy preview refresh behavior is unchanged.

Rig rechecks readiness and container health before commit. After commitment, a
crash is a failed replay; already published data cannot be recalled. Independent
participant status files preserve the outcome even when the CLI returns detached.
Detached replay does not run a background Rig completion observer. Coordinated
`--auto-end` monitors the bag, call injector and other coordinated producers, checks
owned-container health, preserves completion evidence and seals successful runs.

## Ownership and diagnostics

Coordinated startup obtains per-Compose-project locks shared across deployment
directories and refuses existing containers, including with `--force`. It records
the exact IDs created by each launch. Failure cleanup and coordinated auto-end
remove those IDs, rather than issuing a project-wide `compose down`. Container
networks and persistent volumes are retained. Unrelated projects are untouched.
Successful auto-end first stops those containers with their configured stop signals
and a 30-second drain. A forced kill or unsuccessful shutdown leaves the run unsealed
and the containers available for inspection; startup failure still uses bounded forced cleanup.

Execution records in the session and captured run contain planning, image acquisition,
request inspection, verification, capture, launch, readiness, settling and release
timings. Per-participant statuses retain release lateness and terminal details.
Launcher process groups have bounded deadlines; Ctrl+C during startup publishes
abort and cleans up owned containers.

## Acceptance

The [2026-10-27 release-readiness report](releases/coordinated-replay-readiness-2026-10-27.md)
records native architecture, baked/offline, real vehicle recording, compatibility and
failure-injection results, plus completed companion publication and hosted pre-release CI.

`tests/test_replay_coordinator.py` injects slow preparation, stale identities,
heartbeat loss, lost readiness, missing/mismatched acknowledgements, early output,
timeouts, partial launch failure, conflicting ownership and zero settling delay.
The companion repositories test actual participant transitions and camera output.

The Docker integration harness uses a unique deployment, ROS domain, router port
and synthetic MCAP. It checks automatic recorded calls, regenerated-topic suppression,
the first input message, a delayed consumer, common release evidence and unchanged
source data. It runs without image pulls against an already built compatible image:

```sh
python tests/replay_coordinated_smoke.py --infra /path/to/rig-infra --image IMAGE
python tests/replay_coordinated_smoke.py --infra /path/to/rig-infra --image IMAGE --scripted-calls
python tests/replay_coordinated_smoke.py --infra /path/to/rig-infra --image IMAGE --scripted-calls --wall-clock --rate 2 --window-from .5
```

The scripted variant schedules a call at time zero and asserts that no input or
service call reaches the consumer before the committed release.

For combined camera/bag acceptance, add `--camera-service /path/to/camera-service`,
`--camera-image IMAGE` and `--bridge-image IMAGE`. The fixture uses the production camera
launcher, decoder and ROS bridge, and compares every received frame's bytes and timestamp.
`--bake registry --auto-player` exercises an extracted artifact's `./rig` with no player in
the source deployment; `--bake offline --auto-player` also saves and loads all replay images.
The generated recordings are controlled test fixtures, not vehicle recordings.

`--fault timeout`, `--fault kill-held`, `--fault cancel` and `--fault concurrent` exercise
actual containers. They require failure before release, exact owned-container cleanup, an
unchanged source, and survival of an unrelated sentinel container. `--legacy-default` checks
the unchanged default against an older reader image without a coordination label.

The release workflow requires the native AMD64/ARM64 `replay-coordinated.yml` gate in addition
to image acquisition. Before tagging, set repository variables `RIG_REPLAY_INFRA_REF` and
`RIG_REPLAY_CAMERA_REF` to the full, published commit SHAs of the tested companions. The gate
refuses missing or mutable refs, builds their production images, installs Rig's wheel, and
runs baked/offline, timing and real failure cases. It can also run manually with those SHAs.
Companion packages and images must be published before operators can opt in from a registry;
there is no requirement to migrate every existing service or deployment.

### Earlier checkpoint — 2026-10-22

The following measurements preserve the earlier checkpoint. Publication and broader
acceptance subsequently completed as recorded in the 2026-10-27 report above.

Local native arm64 acceptance on 2026-10-22 passed automatic recorded calls, a
fractional window, scripted time-zero calls and wall-clock calls at 2x rate. Each
used a two-second consumer startup delay and zero settling delay. The three final
window/script/rate commands took 13.27s, 12.68s and 9.70s end to end, including run
capture, playback and sealing; subsequent read-only warm checks took 0.52–0.55s.
These are synthetic fixture measurements, not vehicle performance targets. The
full Rig suite passed all 78 test scripts; focused coordinator tests cover deadline
expiry even when every participant reports ready and immediate acknowledgements
during atomic command writes.

Companion validation passed 174 infra host checks and 13 native ROS cases. Camera
passed all 597 tests on both GStreamer 1.20 and 1.28.8, plus 14 tests against a
baked runtime image. A slow MJPEG PCAP regression also confirmed that a 20-second
scheduled drain retains every raw and encoded frame while genuine decoder stalls
and cancellation remain bounded. Companion releases have not yet been published.
