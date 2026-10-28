# Isaac compatibility preparation report

Preparation checkpoint: **2026-10-28 UTC**, originally tested against Rig **v0.2.72 plus the
shared-session/Isaac changes**, now selected for **v0.2.73**. This is an engineering preparation
record, not a certified simulator support matrix or a live simulation test
report. Operator instructions are in [SIMULATION.md](SIMULATION.md) and
[ISAAC_OFFLINE.md](ISAAC_OFFLINE.md); the [documentation index](README.md) separates current
guides from historical plans.

## Prepared scope

- Shared version-2 recipes describe the exact selected processes, service-owned overrides
  and clock policy, with separate replay and simulation provider sections. Version-1 replay
  remains supported. Recorded simulation runs use the ordinary run/replay model.
- The Isaac provider attaches to an existing graph. It checks declared ROS configuration,
  expected topic/type endpoints, QoS and one advancing clock, validates native runtime images,
  then starts selected infrastructure/autonomy with frozen configuration. Shared lifecycle
  state in `var/sessions/` governs inspection and teardown for simulation and version-2 replay;
  provider configs remain in `var/sim/sessions/` or `var/replay/sessions/`.
  Scene creation, assets, robot control and transport provisioning
  remain external responsibilities.
- The checked-in [camera recipe](../examples/isaac-attach/camera-record.yaml) uses existing
  example names, an external Zenoh router, exact camera topics and an explicit logger override
  to retain ROS images. The normal camera driver is omitted and therefore does not write its
  customary source video. An optional controller extension is documented separately.
- The offline guide covers versions, image/asset staging, disconnected startup, transport,
  clock/sensors/commands, QoS, data paths, lifecycle, same-recipe replay and failure diagnosis.
  Its final checklist requires evidence from the actual simulation machine.

## Compatibility evidence and boundaries

Official documentation was checked on **2026-10-28 UTC** against the fixed **Isaac Sim 6.0.0**
documentation URLs. The walkthrough's distro/RMW compatibility choices follow the vendor's
tested/experimental distinction; it does not treat the repository's default as certified.
[NVIDIA ROS installation](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/installation/install_ros.html)

Additional primary references used by the guide:

- [NVIDIA requirements](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/installation/requirements.html)
  and [local asset setup](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/installation/install_faq.html).
- [Clock](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/ros2_tutorials/tutorial_ros2_clock.html),
  [camera](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/ros2_tutorials/tutorial_ros2_camera.html),
  [Twist control](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/ros2_tutorials/tutorial_ros2_drive_turtlebot.html)
  and [QoS](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/ros2_tutorials/tutorial_ros2_qos.html) tutorials.
- Official ROS sources: [Jazzy rmw_zenoh](https://github.com/ros2/rmw_zenoh/tree/jazzy) and
  [Jazzy QoS documentation source](https://github.com/ros2/ros2_documentation/blob/jazzy/source/Concepts/Intermediate/About-Quality-of-Service-Settings.rst).
  The rendered ROS documentation site denied automated access during this audit; its
  official source repository was used instead.

These references establish vendor-described behavior, not Rig-specific acceptance. No
Isaac installation, GPU scene, custom Lyrical bridge, physical vehicle or disconnected asset
run was exercised by the documentation work. The existing
[v0.2.72 release report](releases/coordinated-replay-readiness-2026-10-27.md) records separate
native replay evidence and remains unchanged.

## Documentation audit

The audit compared current navigation/operator claims with the CLI, shared recipe validator,
simulation workflow/probe, runtime-image planner and replay implementation. Historical release
records were read for their evidence and not rewritten as current results.

| Document(s) | Result/action |
| --- | --- |
| Project README | Added simulation/offline navigation and unreleased/live-acceptance qualification. Existing deployment/build/launcher sections retained. |
| `docs/README.md` | Added an operator index and separate historical evidence list. |
| `SIMULATION.md`, `ISAAC_OFFLINE.md`, example README/YAML | Added attach contract, usable camera record/replay example, image-logging override, offline preparation and explicit acceptance limits. |
| `REPLAY.md` | Added shared version-2 exact selection, override/clock rules, provider compatibility and version-1 preservation. Existing replay preparation/proxy/call behavior retained. |
| `ONBOARD_REPLAY.md` | Linked shared recipes and simulator recording; retained artifact/image-bundle requirements and opt-in coordination. |
| `BUILD_AND_REPLAY.md` | Replaced obsolete “shared readiness is a follow-up” and pending v0.2.72 acceptance claims with the shipped opt-in mode and release evidence. |
| `HOST_SETUP.md` | Corrected blanket no-host-mutation and enforced-host-networking claims; distinguished example Lyrical from a compatible simulator deployment. |
| `CHEATSHEET.md`, `RUNBOOK.md` | Added navigation, coordinated replay examples and the version-2 service-selection distinction; retained the worked hardware deployment's scope. |
| `DESIGN.md` | Added implemented shared/provider boundaries, frozen lifecycle, clocks and current limits. |
| `ROADMAP.md` | Replaced stale replay readiness/clock adoption status in §2, added current checkpoint; preserved original broader source/footprint design. |
| `STATE.md` | Replaced the stale v0.2.60/760-test current header; clearly marked dated release/machine notes and old open items as historical. |
| `replay-coordination.md` | Corrected publication status and labeled the older 2026-10-22 measurements. |
| Portability, preparation, proxy and reliability plan documents | Added current references and marked checkpoint counts/pending items historical, preserving original plans and performance limits. |
| `services/README.md` | Included offline image delivery and session runtime-image launcher requirements. |
| `docs/releases/*` | Reviewed as immutable release evidence; no historical result was promoted to an Isaac acceptance claim. |

Documentation validation completed: the example validates for both providers; its full YAML
equals the copy embedded in the walkthrough; **122 relative file links resolve with zero
missing targets**; sim/status/logs command options were compared with current CLI help; and
`git diff --check` passes. These checks used the local Python environment with PyYAML and did
not launch services. Code-test results are tracked separately below rather than inferred
from documentation edits.

## Remaining acceptance and improvements

| ID | Priority | Finding and next concrete step | Status |
| --- | --- | --- | --- |
| ISAAC-01 | Live Isaac qualification | Run the entire offline checklist on a compatible Linux GPU machine, retaining version/image, preflight, payload, bag and replay evidence. | Pending machine; not a v0.2.73 release blocker |
| ISAAC-02 | Compatibility | Validate Jazzy/Zenoh images and actual Isaac bridge together; retaining Lyrical requires independent custom-bridge/build evidence. | Pending machine/build choice |
| ISAAC-03 | Data correctness | Verify image payloads, calibration/frame IDs, TF and controller response. Endpoint/QoS discovery does not prove these. | Pending scene |
| ISAAC-04 | Offline completeness | Restart with external network unavailable and local assets/extensions; verify replay-only images are staged as well as sim-selected images. | Pending machine |
| ISAAC-05 | Recorder usability | Default logger excludes images. The camera example now overrides this; a future semantic warning would need a reliable launcher-owned recording contract rather than guessing opaque configs. | Documentation fix complete; optional enhancement |
| ISAAC-06 | Transport lifecycle | Initial preflight requires an external reachable Zenoh router. A provider-aware router bootstrap/ownership design could simplify setup, but must preserve quiet-project and teardown guarantees. | Documented limitation |
| ISAAC-07 | Offline preparation | No sim prepare/pull-only or portable scene/recipe bundle command. An inventory export/staging helper could reduce manual acquisition and make missing dependencies clearer. | Candidate QoL |
| ISAAC-08 | Transform authority | Current overlap checks reject shared `/tf` or `/tf_static` even for disjoint frame sets. Per-frame validation/filtering needs an explicit contract; do not weaken checks blindly. | Documented limitation |
| ISAAC-09 | Simulator identity | Scene/asset version and seed are declared provenance, not verified loaded state. A future Isaac-side handshake could attest the scene and bridge configuration. | Candidate feature |
| ISAAC-10 | Runtime health | Pre/post probes are bounded samples, not continuous clock/topic/payload supervision. Test long-running pauses, resets and disk pressure operationally. | Documented limitation |
| ISAAC-11 | Experiment control | No launch/reset/step, pause coordination, recorded-controls-in-sim or mixed bag/sim scheduler. Each requires separate lifecycle/time authority semantics. | Outside attach scope |
| ISAAC-12 | Repeatable source | A replay label selects the newest matching run. Acceptance should pin a sealed run ID/path and retain immutable image/asset identities. | Documented procedure |
| CAPTURE-01 | Post-release collection check | The checked-in development camera has `recording.enabled: false`, while the logger has `record.exclude_images: true`; `data_dir` also needs deployment provisioning. Validate an explicit collection profile so required camera frames and other inputs are actually retained. A successful launch is not recording-completeness evidence. | Deferred by user until examining a collected run; not a release blocker |
| CAPTURE-02 | Post-release real-run check | Bake the release, record a representative run, seal it, transfer the complete run, and replay/decode it on a clean target. Verify required payloads, timestamps/time basis, camera sidecars, calibration/TF, custom message support and relevant commands/state; check the actual export profile does not discard required inputs. Retain the pinned software/images needed to interpret it. | Deferred by user until examining a collected run; not a release blocker |

## Release decision for future use of collected data

The user explicitly authorized cutting **v0.2.73** now, with collection-profile validation and
fresh real-run acceptance deferred until an actual collected run can be examined. CAPTURE-01
and CAPTURE-02 therefore **do not block this release**. Their status remains unverified rather
than implicitly passed. Isaac attachment is released as experimental.

Live Isaac acceptance need not block a release whose promise is durable, replayable field
recordings. This change preserves the existing bag and camera recording formats and normal
run layout; version 2 describes session recipes, not a migration of sensor data. Simulation
provenance is additive. Future simulator adapters can build on these recordings and explicit
ROS interfaces without requiring the collection machine to run Isaac now.

That is not yet a guarantee that every deployment records everything needed. Rig delegates
recording to services, and artifact/config capture remains best-effort. The development
example's disabled camera recording combined with excluded ROS images is a concrete example
of valid configuration that does not retain camera frames. CAPTURE-01 and CAPTURE-02 are
post-release checks before making a collection-completeness claim; normal packaging/release
gates remain separate. Do not infer their completion from the previous release's native
acceptance or the new simulated round-trip fixture.

Preserving sensor inputs supports future playback and comparison with simulated inputs.
It does not reconstruct an Isaac scene, robot dynamics or an interactive world from a bag;
scene assets, calibration, robot models and any necessary initial state require their own
retention contract. Missing payloads or metadata cannot be restored by a later recipe.

## Implementation and test evidence

The implementation pass added an unchanged-YAML simulation → sealed recording → preparation
→ replay → sealed result test. It exercises actual Rig selection, configuration resolution,
source fingerprints, normal run provenance and replay planning. Docker/ROS boundaries and
bag bytes in that test are fixtures; it does not establish image payload or GPU correctness.

The extended audit found and fixed these issues before machine acceptance:

| Finding | Fix and regression coverage |
| --- | --- |
| Manual version-2 replay teardown lost session intent | Both providers retain frozen selection, configuration, clock, image pins, environment, Docker identity and launch/file hashes. Status/logs/down work even if the working manifest becomes invalid. |
| Concurrent lifecycle operations could race capture/pointer publication | A deployment transition lock serializes shared replay/sim, ordinary up/down and run rotation/sealing. Active shared sessions reject independent forced run changes. |
| Legacy replay `--force` could claim existing projects under v2 | V2 requires empty selected projects before preparation and immediately before each launcher; a late foreign project is never registered as attempted or cleaned. V1 behavior is preserved. |
| Source/engine could change during expensive capture | Replay rechecks source and recording bindings; both providers recheck Docker identity before launch. |
| Cleanup could stop early or falsely succeed | Every attempted row receives teardown despite an earlier exception; all selected projects must be empty before pointer retirement. Failed cleanup retains recovery state and evidence. |
| Saved teardown omitted pre-removal logs | Capture logs using the saved Docker routing before down/seal; reject a different current run. |
| Probe protocol could claim readiness while reporting errors | Validate response types and issue records, reject contradictory/nonzero readiness, and retain actionable timeout/import errors. |
| Native ROS probe used an executor on the wrong context | A real ROS smoke exposed a default-context guard-condition failure. The probe now creates, spins and shuts down an explicit executor bound to its own initialized context; regression fixtures reject use of the default executor. |
| Implicit generated replay recipe could resolve a different shared recipe | Generated replay paths are explicit; existing v2 intent is applied before planning. |
| Invalid timeout/state data could raise unhandled exceptions | Bound large/nonfinite timeouts; reject damaged ownership/file/ROS state; normalize missing/timed-out Docker identity checks. |

Additional tests exercise exact selection and null-delete overrides, source/wall clock
policy, duplicate outputs, expected endpoint direction/types, native QoS evaluation, advancing
and reset/stale clocks, image pinning, source changes, frozen player/call plans, JSON CLI
output, no-data-directory replay, failed startup recovery and same-recipe round trips.

The first combined discovery run passed **428 tests with 3 environment-dependent skips**.
The repository also has plain-script tests that discovery does not execute; the final
CI-style all-script run passed **all 89 scripts**, reporting **1,250 cases** (436 unittest
cases and 814 plain-script cases), including **3 skips**, in approximately **191 seconds**
on the final rerun after the native-probe fix.
The skips were one platform sandbox test and two opt-in native Docker image tests.
Parallel fixture tests
initially collided on real host-global project locks. Fixtures now isolate only their lock
directory while preserving actual `flock` behavior; production locking was not weakened.

Validation uses Python 3.12.14, PyYAML 6.0.3 and coverage.py 7.17.0 in a temporary test
environment. Coverage measures Python execution, not ROS middleware or simulator coverage.
The all-script measurement includes branches: **87% across `rig_cli`**, **86% across the
seven focused modules below**. No subprocess auto-instrumentation was enabled, so CLI
subprocesses and external launchers are not necessarily included in the Python coverage data.

| Module | Branch-aware coverage |
| --- | ---: |
| `session_recipe.py` | 85% |
| `session_runtime.py` | 85% |
| `sim_workflow.py` | 86% |
| `sim_probe.py` | 93% |
| `replay_recipe.py` | 80% |
| `replay_prepare.py` | 90% |
| `replay_workflow.py` | 85% |

Reproduction from the repo root (in an environment containing those dependencies):

```sh
python -m unittest discover -s tests -p 'test_*.py'
mkdir -p var/isaac-prep
export COVERAGE_FILE=var/isaac-prep/.coverage-all
python -m coverage erase
rc=0
for test_file in tests/test_*.py; do
  python -m coverage run --append --branch --source=rig_cli "$test_file" || rc=1
done
python -m coverage report
exit "$rc"
```

Local detailed logs and the machine-readable per-script results are retained under
`var/isaac-prep/`; these generated artifacts are ignored by Git. At this preparation checkpoint,
no commit, release or deployment to the simulation machine had been performed. The subsequent
release decision is recorded above. ISAAC-01 through ISAAC-04 remain pending.

### Native ROS smoke

The standalone [native probe smoke](../tests/sim_probe_smoke.py) was exercised inside a
disposable **Linux ARM64** Docker container using the existing cached
`local/fleet-ros:coordination-rc-8d054951` image, **ROS Lyrical, Fast DDS
(`rmw_fastrtps_cpp`), domain 227**, with Docker networking disabled. It uses real `rclpy`
publisher/subscriber endpoints, clock messages, discovery and native QoS checks.

All **seven scenarios** passed after the context fix: healthy graph accepted; missing,
duplicate, paused and resetting clocks rejected; wrong sensor message type rejected; and
best-effort image publication paired with a reliable subscriber rejected for incompatible
QoS. Fixture processes are reaped and the disposable container is removed.
This is a native ROS test, **not** an Isaac test or validation of the walkthrough's
Jazzy/Zenoh combination. No GPU, simulator scene, real bag payload or external network was
involved. The initial native failure and fix are material evidence of why fixture coverage
alone does not establish middleware compatibility.

To repeat in another prepared ROS environment, first isolate its ROS network/domain from
operational graphs, source that distribution, set `RMW_IMPLEMENTATION` and `ROS_DOMAIN_ID`,
then run `python3 tests/sim_probe_smoke.py`. The script prints JSON observations for each
scenario. It intentionally publishes test data and `/clock`; it is separate from Rig's
read-only attachment probe and must not run alongside an operational simulation session.
