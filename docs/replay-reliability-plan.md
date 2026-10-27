# Replay reliability implementation plan

Status: implementation in progress. Scope: Rig, the bag player in rig-infra, camera-service,
and the launch surfaces used by replay-selected services.

Implemented locally: phase 1 correctness fixes and phase 4 invocation-scoped proxy hash
reuse, file verification progress, and execution timing records. Phase 2 now resolves
selected launcher inventories, acquires missing native images, verifies runtime capability
labels, and pins Compose launch to immutable image IDs. Camera, dashboard, and all four infra launchers
implement the contract; fleet-ros builds verify the player runtime before applying its
capability label. Phase 3 automatic recorded-call selection, conservative topic selection,
request inspection/cache, frozen origin, and policy-version invalidation are implemented.
Native ROS tests confirmed mixed-origin playback and exposed two additional window bugs:
rounded offsets and an inclusive ROS stop bound. Structured replay now preserves fractional
offsets and enforces an exclusive integer timestamp end. Inspection seeks to the selected
window and stops at its end. Those changes shipped in Rig v0.2.71 with infra v1.18.0
and camera-service v1.11.0. Phase 5 is now implemented locally as the explicit
`--startup coordinated` mode; see [the startup contract](replay-coordination.md).
Companion publication, the default switch and full vehicle acceptance remain outstanding.

This batch preserves detached startup behavior: it does not add a background observer for
later terminal outcomes. Completion evidence is collected by `--auto-end`. Concurrent
ownership is enforced for coordinated startup with project locks, fresh session IDs,
empty-project preflight and cleanup by exact container ID. Legacy startup retains
projects that already had containers before their launcher ran.

Validation after the image batch: all 72 Rig test scripts pass, including 22 image resolver
and 15 workflow cases; the opt-in Docker tests are skipped in the ordinary host run. Ten
infra and nine camera tests exercise real Compose rendering with daemon mutations intercepted.
Three host tests guard the player image capability declaration, and the runtime checker
passes inside the cached native Linux arm64 ROS image. Two opt-in live Docker tests pass:
a disposable loopback registry verifies cold acquisition, warm reuse and offline behavior;
a no-network Compose container proves that moving a temporary tag after validation still
launches the validated image ID. Temporary resources are removed. No service image was built or
published by this batch. The two real ROS playback/reindex tests also pass again against
the updated player tools. Original phase-1 validation covered 123 bag-player host tests
and Docker completion states. Full vehicle-recording and native
amd64 acceptance remain outstanding.

Phase 3 validation: 75 Rig test scripts passed, followed by 160 replay regression tests
after the final planner fixes, including eleven topic workflow cases and 26 call inspection/cache
and workflow cases. Five native Linux arm64 ROS tests pass for recorded SetBool requests,
both introspection origins, opt-out, windows, rates, simulation/wall clocks, duplicate events,
and MCAP/SQLite payload diagnostics. A short window in an 800-event bag reads at most two
events. Fractional boundary tests cover both storage backends. The updated runtime capability
validator also passes in the cached native ROS image. No production images were rebuilt.
The complete player host suite passed 141 tests; 43 affected cases passed again after the
final structured-window override correction. Three base contract tests passed.

Make replay fetch missing runtime images before image-dependent validation, explain and
reduce startup work, suppress known regenerated outputs, and automatically replay recorded
ROS requests to selected service servers. Correct failure detection and startup timing so
an incomplete or failed replay cannot be reported as successful.

This extends the [replay preparation plan](replay-preparation-plan.md) and brings the
previously deferred producer-readiness protocol into scope. The current operator contract
is in [REPLAY.md](REPLAY.md); update it with each shipped behavior change.

## Checkpoint release validation

The release gate now runs Docker cold-pull/warm-cache/offline acquisition on native amd64 and
arm64 runners before Rig assets can publish. Infra CI includes every host helper suite and a
separate native matrix that builds the capability-labeled base/messages images and requires all
seven real ROS replay cases to pass without skips. It also verifies an immutable Compose launch
after the original tag moves. Camera and dashboard CI run their launcher inventory/pin tests.

Local validation for this checkpoint: all 76 Rig test scripts passed; the isolated registry
acquisition test passed; the wheel and sdist built and the installed wheel imports the new
replay modules outside the source checkout. A replay-only base build and its apt message overlay
each passed all seven native arm64 ROS tests, the capability checker, and the moved-tag launch
check. The dashboard contract passed 16 cases. Native amd64/arm64 acquisition and infra replay
CI have also passed. The installed Rig 0.2.71 wheel, released infra 1.18.0 archive, and published
full messages overlay passed an isolated end-to-end replay: exactly two automatic service calls,
recorded-output suppression, exclusive window cutoff, run capture/sealing, cleanup, unchanged
source hashes, and a warm readiness check. Final companion publication remains a release gate.
MAVLink router is outside this checkpoint's
supported replay set; omit it explicitly in deployments that normally select it.

## 1 Agreed behavior

- Normal execution pulls missing images needed by the effective replay selection. It does
  not pull every image in the deployment, refresh cached mutable tags by default, or build
  images. Cheap source/configuration checks still run before downloads.
- Image resolution, validation, capture, and launch use the same effective configuration
  and image identities, including player overrides and native architecture.
- Check and dry-run remain non-mutating and never pull. Prepare retains its existing
  explicit preparation writes and bounded helper operations, but never pulls implicitly.
  Offline and bundled execution never silently contact a registry.
- Recorded ROS calls are automatically selected for unambiguously mapped servers in the
  complete set of instances started by replay, including policy-selected instances.
  Unrelated, omitted, and ambiguous servers are excluded with reasons.
- An explicit `playback.services: []` disables recorded calls. An explicit list retains
  manual target selection. Scripted calls override recorded calls before event validation.
- Broad topic playback remains the default. Suppress outputs whose recorded publishers
  are all accounted for by selected producers. Explain uncertain ownership and explicit
  overrides rather than claiming collision-free playback.
- Source recordings remain unchanged. Unknown runtime state never authorizes automatic
  teardown or a successful completion result.

## 2 Findings to close

| Finding | Implementation destination | Required result |
| --- | --- | --- |
| Local image checks fail before any pull | Rig image planning and replay workflow | Cold-cache replay resolves and pulls its required native images before local capability checks. |
| Catalog checks can inspect a different image from `BAG_PLAYER_IMAGE` | Rig runtime image resolver and service launchers | The validated image is the image actually launched. |
| Shared camera exports are fully hashed once per camera | Rig service export verification | Each unchanged artifact and selected original is verified once per invocation. |
| Release deadline is set before source verification and run capture | Rig replay workflow | Preparation cannot consume the startup delay; full coordination follows in phase 5. |
| Launch continues after a prerequisite fails | Rig replay dispatch | A failed prerequisite prevents subsequent dependent startup and producer release. |
| Auto-end treats unexpected or non-running Compose status as success | Rig completion monitoring | Completion requires positive terminal evidence and successful exit status. |
| A call script is rejected because overridden recorded events are missing | Rig effective playback selection | Validate only the selected call mode after resolving precedence. |
| Wall-clock script timing adds initialization time to the deadline | rig-infra call injector | Calls use the common absolute origin without accumulating initialization delay. |
| A structurally malformed graph epoch crashes loading | Rig graph parser | Skip malformed epochs with diagnostics and retain valid evidence. |
| Topic exclusions and ROS service defaults do not match operator expectations | Rig selection reports, recipes, and player integration | Automatic selected-server calls and an auditable reason for every topic/call decision. |

The review reproduced these code paths, but did not profile the user's incident recording.
The startup work below must measure actual phase durations before attributing the entire
pause to hashing, registry lookup, capture, or the intentional default hold.

## 3 Phase 1 Correct failure handling and timing

Start with regressions against the active `replay_workflow.command` path, not only the
older replay command. Implement these independent fixes as the first delivery.

1. Replace Compose status substring matching with inspection of the exact player and
   required helper containers belonging to this replay session. Distinguish active,
   succeeded, failed, and unknown. Created, paused, and restarting containers are not
   complete; malformed inventory, Docker errors, or a disappeared container are unknown.
   A nonzero player or injector exit produces a failed replay result. Preserve the
   terminal evidence before cleanup; distinguish sealing a failed run from declaring it
   successful. Bound retries on unknown state, then report the unresolved state without
   automatically tearing down the session. Require completion of required calls as well
   as the bag timeline before reporting completion success. Preserve ordinary detached
   replay: its exit status describes startup, while `--auto-end` monitors completion and
   returns the final outcome. Label these results distinctly and persist observed
   terminal outcomes for detached sessions; do not imply a later crash changes an already
   returned CLI exit status.
2. Stop replay startup on the first failed prerequisite. Track resources created by this
   invocation and abort/clean up only that session's resources, retaining useful logs.
   Keep this behavior scoped to replay unless ordinary `up` changes are separately tested.
3. Finish expensive planning, image acquisition, source verification, and run capture before
   choosing the legacy release deadline wherever possible. Use the same final deadline in
   every rendered participant configuration and replay provenance. Report intended versus
   actual launch timing. This mitigates the existing bug; sequential startup still needs
   phase 5 to guarantee readiness.
4. Resolve CLI `--calls`, recipe calls, and recorded-call selection before validating
   recorded service events. Keep malformed recipes as errors, but do not reject a valid
   script because its overridden recipe target has no recorded events.
5. In the call injector, establish the monotonic clock origin from the absolute release
   instant after initialization. Preserve elapsed lateness if that instant has passed;
   do not restart the schedule at initialization completion. Test rate, window, and
   simulated-clock behavior as well as wall-clock mode.
6. Validate graph document, node, and edge structure. Skip a malformed epoch with its
   filename and reason, without losing valid epochs. Missing evidence must remain unknown
   to both topic suppression and automatic call selection.

Do not use `--start-delay 0` as the timing fix. The current camera configuration starts
paused, and removing its release timestamp can leave it paused indefinitely. Define and
test zero-delay behavior explicitly; coordinated mode must always issue a release.

Acceptance: malformed/empty status, paused/restarting/created player, failed player or
injector, delayed Docker inspection, missing container, failed router launch, cancellation,
capture taking longer than the configured delay, delayed injector initialization, script
override of unavailable events, and mixed valid/malformed graph epochs. No uncertain state
may produce a successful run or stop unrelated containers.

Primary files: [replay_workflow.py](../rig_cli/replay_workflow.py),
[replay.py](../rig_cli/replay.py), [dispatch.py](../rig_cli/dispatch.py),
[replay_plan.py](../rig_cli/replay_plan.py), and [graph.py](../rig_cli/graph.py).
The injector change belongs in rig-infra's `ros2-bag-player/tools/call_injector.py`.

## 4 Phase 2 Resolve and acquire the actual runtime images

Introduce one effective runtime-image inventory for the exact selected instances and
their resolved replay configuration. Include disabled-but-selected instances, optional
sidecars only when enabled, base/messages fallback, runtime variants, registry relocation,
and catalog pins. Each entry carries its owners, effective reference, native architecture,
required capabilities, and immutable identity when available.

Reuse the existing service `verbs.images` contract and extract the reusable invocation and
parser from [artifact_images.py](../rig_cli/artifact_images.py). The implemented replay extension
is `verbs.runtime-images`, a JSON version-1 inventory with image roles, paired with the
`RIG_RUNTIME_IMAGE_PINS` launch contract; templated artifacts retain the YAML-list `images` verb. Its current fleet helper
cannot be reused unchanged: it targets ordinary enabled rows, writes staging configuration,
and loses per-instance identity. Add bounded, side-effect-free image queries to the
participating launchers, including camera and the infra player/logger/router. Queries must
use the same Compose/profile/configuration decisions as launch. A missing inventory
contract produces an actionable upgrade diagnostic, not a guessed runtime list from
`build.images`. Certify the query against captured launch commands. Pair the inventory
query with a launch-pinning contract, such as frozen rendered Compose input plus an
effective-reference equality check; a list of references alone cannot enforce launch.

Execution order becomes:

1. Resolve source, selected instances, recipe precedence, and effective configurations.
2. Resolve and deduplicate runtime image references; inspect Docker architecture once.
3. With the default `--pull missing` policy, pull missing native-platform references using
   exact catalog digests when available. Stream progress and support cancellation.
4. Verify architecture, digest/image identity, required labels, and helper capabilities.
5. Complete source/content validation and preparation checks before run creation or startup.

Treat a reference cached only for another architecture as missing its native variant:
pull the selected native reference once under `missing`, then revalidate it. A cached
native image with incompatible capabilities or identity fails clearly instead of silently
refreshing a mutable tag. Provide `--pull always` for an explicit refresh of selected
registry references and `--pull never` for cached/offline use. Refreshing cannot replace a
catalog pin with different content. Preserve bundled content aliases and validate their
recorded image IDs without registry access; reject a conflicting registry-refresh request.
A bundle missing a required companion image needs an explicit migration.

Check, prepare, and dry-run report the exact proposed pulls and incomplete image-dependent
checks. Missing images must not be misreported as permanent source incompatibility or as a
green readiness result. After materialization, freeze the image choices used by launch so
Compose cannot silently re-resolve a different tag or pull an unvalidated dependency.

Acceptance: cold-cache `localhost:5000` replay succeeds without manual pulling; warm-cache
replay pulls nothing; catalog and non-catalog paths honor the same `BAG_PLAYER_IMAGE`;
missing refs, wrong architecture, missing labels, inaccessible registry, and bad pins fail
before starting services if acquisition and verification cannot satisfy the requirement.
Cover successful acquisition when only a foreign architecture is cached, explicit refresh,
and failure without pulling under `never`. Include replay-only instances, optional sidecars, mirrored
digests, bundled aliases, and zero-pull/zero-registry offline cases. Run native acceptance
on both amd64 and arm64.

## 5 Phase 3 Make topic and service selection explicit

Use one effective selection result for check, prepare, dry-run, execution, and provenance.
Resolve the started instance set first. Keep topic-selection mode separate from recorded
service-call policy so broad playback can replay calls automatically.

For recorded calls, absence of `playback.services` means automatic selection. This is a
documented default change for existing recipes; `[]` preserves the previous no-call
behavior. A nonempty list selects only those exact names and remains the manual override
when historical ownership is insufficient. It still requires usable recorded requests and
reports possible overlap with live clients. No new recipe spelling is needed; the resolved
player handoff continues to contain an explicit service list.

Automatic selection uses historical server ownership plus `recorded_as` and
`recorded_namespace` bindings. Include only unambiguously mapped servers in the entire
effective started set. Where graph evidence identifies a selected live client that is
expected to regenerate the request, exclude automatic injection and explain the duplicate
call risk. Do not infer service renames or start an omitted server merely because a bag
contains its events. Explicit manual lists may override this inference; scripts remain
exclusive with recorded playback.

Inspect the chosen session and replay window for actual request contents. An event topic
alone is insufficient: distinguish usable requests, metadata-only events, response-only
events, unsupported types, and empty windows. Resolve event origin from the same effective
`play.services_source` setting used by playback, then freeze it with the call selection
in the versioned handoff or resolved configuration. Inspect only requests usable by that
origin policy. If only the other origin contains usable requests, report the required
source-setting change instead of claiming readiness. Select only that origin when both
ends were recorded. Repeated events within the selected origin remain repeated requests,
matching the observed native rosbag2 behavior; report their count without promising
deduplication or exactly-once delivery.

Use a bounded helper in the resolved player
image during execution or preparation, with source files mounted read-only and a cache
keyed by verified source/window/helper identity. Check uses valid existing evidence or
reports `not_checked`; it does not launch helper containers. Automatic mode excludes
confirmed unusable targets with reasons; explicit targets with unusable data are errors.
Inspection failures must not be silently treated as an empty call list.

For topics, enforce conservative ownership and make its result inspectable:

- Show each topic's original and remapped name, recorded owners, selected producers,
  inclusion/exclusion reason, and whether an explicit `keep` overrides suppression.
- Remove broad mode's no-graph namespace-only suppression. A producer's namespace can
  contain subscribed inputs as well as outputs, so recording-source status or `recorded_as`
  alone does not prove ownership. Leave those topics eligible, report unknown ownership,
  and use explicit `playback.exclude` when needed. If explicitly requested
  `--selection recorded` retains a legacy namespace fallback, label it as a heuristic
  and never reuse it to establish automatic service-call ownership.
- Test namespace mismatches, unknown publishers, multiple owners, missing/corrupt epochs,
  renamed instances, skipped instances, and remap destinations that collide with outputs.
- Keep `/tf`, `/tf_static`, and `/diagnostics` eligible by default because they can contain
  shared data; show this exception and the exact `playback.exclude` remedy. Per-message
  filtering of shared topics remains out of scope.
- Warn when structured replay supersedes standalone player `play.topics`, `play.exclude`,
  `play.services`, or call settings. Identify the ignored fields and show their recipe
  equivalents rather than silently merging two selection authorities.

Print a concise topic/call summary before launch and retain the complete reason table in
the machine-readable report. Prominently list automatic call targets. Invalidate prepared
selection state when policy or planner version changes; never replay an old cached empty
call list under the new default. Preparation must preserve an explicit user-written `[]`
and must not manufacture `[]` when it intends automatic selection.

Acceptance: a selected server receives each selected request event's recorded payload; an omitted,
unmapped, or ambiguous server receives none; a selected client does not cause duplicate
automatic injection; explicit off/list/script precedence works. Cover server- and
client-origin event recordings and both together, matching origin selection, response-only
events and repeated requests (which remain repeated), custom types, windows, rate,
and both clock modes. Confirm the bag does not republish known regenerated outputs while
selected producers publish them, and unrelated bag inputs still reach a new consumer.

## 6 Phase 4 Reduce and explain startup work

Add phase progress and elapsed timings from the beginning of planning: source inspection,
image resolution/pulls, artifact verification, call inspection, run capture, instance
startup, readiness, and intentional hold. Long hashing phases show files and bytes;
external operations identify their target and timeout. Store timings with the replay
report so a slow start can be diagnosed from one run.

Pass an invocation-scoped verification context through planner passes and camera bindings.
Verify a shared export once, and each selected original once, reusing the result only while
artifact identity and file identity/stat evidence remain unchanged. Detect mutation during
verification and invalidate reused results if inputs change. Keep full cross-invocation
integrity verification initially; persistent hash caching is a separate policy decision.
Deduplicate local image inspections and capability probes as well.

Acceptance: two cameras sharing one export hash every proxy once per invocation, including
prepared revalidation; corruption and concurrent mutation are still rejected. Compare
before/after phase timings on a representative large recording, with cold and warm image
caches and prepared/unprepared inputs. Verify progress remains visible throughout long
operations. Set measured startup targets after this baseline rather than promising an
unsupported percentage improvement.

Primary files: [service_exports.py](../rig_cli/service_exports.py),
[replay_prepare.py](../rig_cli/replay_prepare.py), and the shared planner/workflow.
Instrumentation can land with phases 1 and 2; deduplication can proceed independently of
the selection work.

## 7 Phase 5 Release producers only after readiness

Add a versioned replay coordination capability shared by Rig, the bag player, call injector,
and camera-service. Rig remains the coordinator without depending on ROS libraries. Use
launcher operations for status, release, and abort backed by a session-scoped control
channel. Bind every participant and control message to a unique session ID and resolved
plan identity. Require both launcher and running-image capability; a new descriptor around
an old image is not sufficient.

The state sequence is prepare, start held, arm, ready, release, running, and terminal.
Every producer must remain held while input opening, type loading, seeking, decoder
initialization, and consumer startup occur. Consumers need service-specific readiness
criteria for subscriptions and service availability; `compose up` returning successfully
or a container merely running is not that proof.

Allow a named arming phase for initial paused `/clock` and required latched-state restoration
after transport and consumer endpoints are available. This must not advance the replay
timeline or dispatch recorded/scripted requests. Do not require receipt of advancing replay
data to declare a consumer ready, which would deadlock the barrier.

Camera decoder preroll stays internal: its current paused behavior can publish a first
frame, so coordinated mode must withhold that frame and publish the first in-window frame
after release. The bag player must confirm its paused/seek state. Clock-only playback and
the call injector participate; a script event at the window start cannot fire merely
because a paused clock was published. Pre-start state-setting calls would require a
separate explicit policy and are not part of automatic recorded-call replay.

After all required participants acknowledge readiness, Rig commits one immutable release
with a common near-future timestamp. Define delivery acknowledgement, expiry, and abort
handling so stale, late, or duplicate commands cannot start another session. The transport
must provide a bounded release margin and detect lost readiness before commit. A crash
after release commitment becomes a failed replay; the protocol must not claim it can undo
already published data.

In coordinated mode, `--start-delay` is the optional settling interval after readiness;
zero still sends a valid common release. Use bounded per-participant and overall startup
timeouts. On failure or cancellation, name the participant/phase and abort the resources
owned by that replay. Record readiness times, intended/actual releases, lateness, and
terminal outcomes.

Offer an explicit legacy startup mode for old packages with its timer limitation visible.
Never silently mix coordinated producers with autonomous timer-based producers, or claim
coordinated readiness for an unsupported consumer. Make coordinated startup the default
only after the participating packages and images support the complete contract.

Acceptance: slow consumer startup, long camera opening, failed type loading, crashed helper,
stale control state, lost readiness, expired release, duplicate release, cancellation, zero
settling delay, mixed old/new packages, and a source window beginning on the first frame.
Observe no timeline data or calls before release except the declared clock/latch arming
phase; verify preserved first frames and common time origin across bag, camera, and calls.

## 8 Validation and delivery

The review baseline passed 230 existing host tests, two existing real ROS runtime tests,
and an isolated recorded `SetBool` experiment that delivered one request when selected and
none when disabled. Those results establish useful fixtures, not coverage of complete
Compose orchestration or the user's recording.

Promote the temporary recorded-service experiment into the rig-infra integration suite.
Add workflow-level tests in Rig for selection, pulls, failure paths, and preparation reuse;
add producer state-machine tests in camera-service and the player. Reuse the existing
MCAP/SQLite reindex, source-preservation, remapping, latched-message, and clock-only tests.

The final acceptance run uses one representative vehicle recording and a clean replay
workspace on native Linux amd64 and Linux arm64, including Linux containers on the arm64
Mac. Exercise a cold local registry cache, warm cache, offline bundle where supported,
camera decoding, custom service requests, topic suppression, simulated time, wall clock,
window/rate settings, normal completion, and injected failures. Verify source checksums,
image provenance, first/last messages, call payload/count, timings, and session-scoped
cleanup. A host mock suite alone does not complete this acceptance.

Deliver in this order:

1. Rig completion/startup/parser/precedence regressions and fixes, plus the independent
   rig-infra script timing fix and initial timing diagnostics.
2. Runtime image inventory support in service packages, then Rig's shared image resolver
   and default missing-image pulls. Verify cold-cache replay against the combined versions.
3. Automatic call selection, request-content inspection support, topic decision reports,
   and recipe migration documentation. Ship verification deduplication alongside these
   changes when its integrity tests pass.
4. Backward-compatible coordination support in infra and camera packages/images, followed
   by Rig coordination and its default switch after cross-repository acceptance.

Update `REPLAY.md`, `BUILD_AND_REPLAY.md`, CLI help, and release notes with pull policy,
automatic calls and explicit opt-out, ignored legacy selectors, readiness semantics,
timeouts, and legacy-mode migration. Record the tested package/image compatibility set.
Reconstructed deployments must update their vendored launch surfaces and pinned images;
updating the system Rig binary alone does not update older bundled copies. Publish runtime
support before making a new Rig release require it, and retain a documented route to
explicit legacy startup for packages lacking the coordination contract. Legacy startup
only relaxes timing coordination: it does not bypass runtime image validation. Launchers
without an effective-image inventory/pinning contract need an upgrade even in legacy
startup mode; document that dependency before releasing the new image requirement.

Completion means all findings in section 2 have regression coverage and the combined
native replay acceptance passes. Live graph discovery, mandatory general-purpose ROS
interface declarations, service-name remapping, per-message TF filtering, and persistent
cross-invocation integrity caching remain outside this work.
