# Connect a deployment to Isaac Sim

For a complete machine-preparation and disconnected capture/replay walkthrough, start with
[offline Isaac attachment](ISAAC_OFFLINE.md) and its checked-in camera recipe. This feature
is experimental (since Rig v0.2.73); [preparation status](ISAAC_PREP_REPORT.md) separates local
fixture evidence from pending live Isaac acceptance.

`rig sim` starts a deployment's selected infrastructure and autonomy services against an
already running NVIDIA Isaac Sim ROS graph. The simulator publishes the expected sensor
topics and receives the autonomy stack's commands. Rig checks the connection, starts its
own services, and records the session through the deployment's normal run/logging setup.

This first provider supports **attachment**. Prepare the scene, robot, sensors, command
subscriptions and ROS bridge in Isaac before invoking Rig. Recipe topic bindings describe
the graph that must already exist; they do not create OmniGraphs, remap topics, rewrite
frame IDs, load a USD scene or change the simulator's seed.

A second provider, `external`, attaches in the same way to a simulator other than Isaac.
[An external simulator](#an-external-simulator) lists what differs; the rest of this page
applies to both.

## A shared session recipe

Save the following as `sessions/warehouse.yaml`, adapting the instance names and ROS
settings to your deployment and Isaac installation:

```yaml
version: 2
session:
  instances: [bag_logger, planner]
  clock: source
  overrides:
    bag_logger:
      record: {exclude_images: false}
      control: {use_sim_time: true}

simulation:
  provider: isaac
  mode: attach
  replace: [cam_front]
  ros: {distro: jazzy, rmw: rmw_fastrtps_cpp, domain_id: 42}
  scene: warehouse.usd
  asset_version: warehouse-v3
  seed: 7
  publishes:
    - {topic: /cam_front/image_raw, type: sensor_msgs/msg/Image, instance: cam_front}
    - {topic: /cam_front/camera_info, type: sensor_msgs/msg/CameraInfo, instance: cam_front}
  subscribes:
    - {topic: /planner/cmd_vel, type: geometry_msgs/msg/Twist}
  # Optional: choose a Python interpreter with sourced ROS, rclpy and rosgraph_msgs.
  probe: [python3]
  timeout_s: 10

replay:
  source: warehouse-sim
  playback:
    exclude: [/planner/cmd_vel]
```

The two provider sections are independent. A recipe may contain `simulation`, `replay`, or
both. The command requires its own provider section and never guesses missing bindings.
Existing version-1 replay recipes remain supported unchanged.

`session.instances` is the exact set of deployment processes to start. Include required
infrastructure explicitly: for example, a Zenoh deployment normally needs its router as
well as the logger and consumers. Omitted physical drivers stay off; simulation does not
start sensor-tier services or a bag player. `simulation.replace` identifies the physical
sensor instances represented by the simulator, and they cannot also be selected.
For Zenoh attachment, the simulator and probe must already have a reachable router before
Rig starts the selected services. Listing a router here cannot bootstrap the initial probe;
the attachment graph's routing must be available independently of the new session projects.

`session.overrides` applies service-owned configuration patches only to selected instances
for this session. It cannot change their `name` or `service`. The deployment's working
configs are preserved. Keep service-specific `use_sim_time` settings consistent with the
source clock and use launchers that declare `replay: {sim_time: true}` in `rigging.yaml`.
That existing declaration promises adoption of the shared `RIG_SIM_TIME` token.
Isaac attachment requires `session.clock: source` (the default); `clock: wall` remains a
replay option. Autonomy services without the simulation-time declaration fail preflight.

`publishes` and `subscribes` are from Isaac's perspective. Each entry uses an exact absolute
ROS topic and a `package/msg/Message` type. An optional `publishes[].instance` ties a
simulator output to a name in `replace`; it does not rename a node or topic. Do not list
`/clock` as a data binding: the session owns clock selection. Scene, asset/Isaac version
and seed fields record the stated simulator setup; attachment does not verify or apply them.

## Check, start and stop

Enable Isaac's ROS bridge, configure its topics, and press Play. Use the same ROS domain
and a compatible ROS distribution and middleware throughout the graph. The recipe's
`simulation.ros` declares the simulator's settings, which the probe checks in its own sourced
environment; it does not silently convert a deployment to a different distribution or
transport. The middleware and domain must match the deployment.

The simulator may run another ROS distribution than the deployment (v0.2.76), for example Isaac
on Jazzy against a Lyrical deployment. Message type names alone cannot show that the two sides
agree, so the session then requires every bound topic's REP-2016 type hash to match on all of
its endpoints. Endpoints appear on both sides once the deployment starts, so the check that
decides it is the one after startup: it waits up to `timeout_s` for the deployment's side of each
bound topic, and a mismatch rolls the session back. A topic with no deployment endpoint by then
is reported as a warning naming it (its hash was not compared), not passed silently. Humble and older report
no type hashes and cannot be mixed with another distribution. Rig does not check that the
middleware itself interoperates across the two releases; matching endpoints in the probe are
the evidence.

```sh
rig sim check --recipe warehouse
rig sim check --recipe warehouse --format json
rig sim up --recipe warehouse --dry-run
rig sim up --recipe warehouse --label warehouse-sim
rig sim down --end-run
```

`--attach` explicitly selects the supported attach mode on `check` or `up`. A bare recipe
name resolves to `sessions/NAME.yaml`; an explicit YAML path also works. To reuse the same
version-2 file for simulation and replay when it is stored under `replay/`, pass
`--recipe replay/NAME.yaml` explicitly to both commands. A second recipe file is unnecessary.
Check and dry-run inspect and report without starting deployment services or writing generated session state.
Execution may acquire missing selected images; `--pull always` refreshes them and
`--pull never` requires the local image cache. Selected launchers must support the same
`runtime-images` inventory and immutable image pinning used by replay. Rig validates the
local Docker engine's architecture and the deployment/artifact runtime before launching.
A configured `data_dir` is required. Select a bag logger to retain a replayable recording;
run metadata alone does not capture sensor data.
The default logger example excludes images because physical camera drivers record video
themselves. With those drivers replaced by Isaac, explicitly include the needed image topics
and set `record.exclude_images: false`, as in the example above.

The ROS probe runs in a separate Python process so ordinary Rig commands do not require
ROS Python packages. Its interpreter needs `rclpy` and `rosgraph_msgs` in a sourced ROS
environment, including the configured middleware's transport settings. `simulation.probe`
defaults to the Python interpreter running Rig; set it to `[python3]` or an explicit Python
path when ROS is installed in another environment. It is an argument list, not a shell
command. `timeout_s` bounds discovery and clock observation
and must be greater than zero and at most 120 seconds.

The probe checks its actual ROS distribution, middleware and domain, expected publisher and
subscriber topic types, and exactly one advancing `/clock` without a backwards jump during
observation. Endpoints of one topic that report different REP-2016 type hashes (message
definitions) block startup even on one distribution, because such endpoints never match. Missing topics, incompatible types or a paused clock prevent startup. Endpoint
discovery does not prove that every sensor is producing valid payloads, that all application
callbacks are healthy, or that the observed process is the stated Isaac scene. For discovered
publisher/subscriber pairs, ROS QoS compatibility errors block startup and uncertain
compatibility produces a warning. An absent endpoint cannot establish the compatibility of
a future subscriber. The probe records endpoint/QoS evidence; it does not repair the graph.

Use `rig sim down` to stop Rig's session with its saved configuration. The attached Isaac
process remains under its original operator's control. `--end-run` seals the recording after
successful teardown. Bare `rig down` also uses the saved simulation session while it is
active; partial selections and `--purge` are rejected in that state. Ordinary `rig up`
refuses an active simulation session. Stop it before returning to the working deployment.
`rig new-run` and `rig end-run`, including `--force`, also refuse an active shared session;
use `rig down --end-run` so teardown and sealing remain coordinated. Log capture uses the
saved Docker connection before containers are removed. An incomplete teardown retains its
session pointer for retry instead of reporting success from launcher exit codes alone.
If a shared replay session is active instead, `rig sim down` rejects it; use `rig down --end-run`.

Execution writes immutable provider configs under `var/sim/sessions/<id>/` and shared lifecycle
state under `var/sessions/<id>/state.json`, selected by `var/sessions/current.json`.
Version-2 replay uses the same lifecycle registry, with its provider configs under
`var/replay/sessions/`. The run's `.rig/simulation/` directory stores the recipe, resolved plan,
probe observations and execution evidence. Editing a working recipe does not change the
configuration used to stop an existing session. Bare `rig down`, `rig status` and `rig logs`
use the active shared session's saved selection and environment for either provider.
Teardown verifies the original Docker engine and session identity before changing services;
changing the shell's Docker connection cannot redirect it to another engine.

## Replay the recording or run the world again

After stopping and sealing the simulation run, stop Isaac or disconnect its ROS graph so
it cannot compete with recorded playback for sensor topics and `/clock`:

```sh
# Same selected consumers and session overrides, now with recorded inputs:
rig replay check --recipe warehouse
rig replay --recipe warehouse
rig down --end-run

# Or select the captured run directly with existing replay commands:
rig replay warehouse-sim planner
```

Replaying a simulation recording is normal recorded-data playback. Isaac is unnecessary,
and new controller outputs cannot alter the recorded sensor trajectory. The bag player
provides the replay clock; recorded `/clock` messages are not also replayed.

Running `rig sim up` again reconnects to the current live simulator. Resetting the scene,
restoring initial conditions and selecting a seed happen in Isaac before attachment. New
controller outputs can change the world and therefore its future sensor outputs. A saved
scene/seed describes the setup, not a guarantee of identical execution.

Keep command authority explicit: the live controller should own its regenerated command
topics. The example replay exclusion removes recorded commands from ordinary data playback.
Feeding recorded controls into a live simulator and mixing bag-driven sensors with Isaac
are outside this attach workflow.

## An external simulator

`provider: external` is for any simulator that already publishes `/clock` and its topics on
the deployment's ROS graph. It is experimental too, and Rig still only attaches: it does not
launch, reset, step or seed the simulator.

```yaml
simulation:
  provider: external
  simulator: {name: worldsim, version: "abc1234"}
  mode: attach
  ros: {distro: lyrical, rmw: rmw_zenoh_cpp, domain_id: 1}
  scene: scenarios/typical.yaml
  seed: 7
  publishes:
    - {topic: /world/view, type: std_msgs/msg/String}
    - {topic: /world/report, type: std_msgs/msg/String}
  subscribes:
    - {topic: /agent/plan, type: std_msgs/msg/String}
```

`simulator` is required and holds exactly `name` and `version`, both non-empty strings. Quote
a version that YAML would read as a number. `simulator` is valid only with `external` and
`isaac_version` only with `isaac`; any other provider is refused. The other fields, and the
`session` and `replay` sections, mean what they mean for Isaac.

Preflight and the probe check the same things as for Isaac: ROS settings, selected tiers,
one authority per output, runtime images, endpoint types, type hashes, QoS and the clock.
The simulator must be the only `/clock` publisher and must keep the clock advancing while it
waits for the stack, because the probe requires an advance within the last 2 seconds. Two
things differ. The Humble/Jazzy warning concerns Isaac's tested distributions and is not
raised. Messages name the simulator by `simulator.name`.

The plan, the run's `simulation` metadata, the saved session and the run's `.rig/simulation/`
directory say `provider: external`, carry the `simulator` mapping, and give `clock` as
`external`. The scene, the seed and the simulator's name and version are recorded as the
recipe states them. Rig does not ask the simulator what it is running, so a wrong value in
the recipe is a wrong value in the record. `rig sim down` and bare `rig down` stop the
session and leave the simulator running.

Two arrangements from the first live use, both for a single host:

- **Zenoh.** The simulator needs a router before the session starts, and `rig replay` needs
  the deployment's router among the session's services. Give the simulator its own router on
  another port, and connect the deployment's router to it with the router's `connect`
  endpoints. The same recipe, listing the deployment's router, then serves `rig sim` and
  `rig replay`. Point the probe's Zenoh session at the simulator's router.
- **A host without ROS.** `simulation.probe` is an argument list, so it can name a wrapper
  that runs Python in a ROS image: for example a script that ends in
  `exec docker run --rm -i --network host -e ROS_DOMAIN_ID -e RMW_IMPLEMENTATION IMAGE python3 "$@"`.
  Rig appends `-c` and the probe's source, and sends the request on standard input.

[examples/external-attach](../examples/external-attach/README.md) has a recipe to copy.
Automated tests cover this provider's recipes, plans, records and session lifecycle. It has
been run against one live simulator on one host; [State](STATE.md) records what was and was
not exercised.

## Frames, versions and current limits

Configure sensor frame IDs, TF authorities and QoS in Isaac and the selected services.
Rig checks topic endpoints, not individual transform ownership. Existing replay deliberately
retains `/tf` and `/tf_static`; where a live stack regenerates transforms, decide which
producer owns them and use explicit replay exclusions where appropriate. A whole-topic
exclusion may also remove transforms you still need; there is no per-transform filter here.
Simulation preflight currently rejects a topic declared as published by both Isaac and a
selected service, including `/tf` or `/tf_static`. Even disjoint transform sets do not exempt
that conflict: this version cannot prove or enforce authority per transform.

Validate the chosen Isaac release against the deployment's middleware and message packages.
Matching the domain ID alone cannot establish compatibility; matching type hashes show the
message definitions agree, not that every middleware feature behaves the same across releases. The simulator
host and the machine running Rig's containers may have different GPU/CPU requirements.

This feature does not launch Isaac, package its assets, control play/reset/step, validate
physics fidelity or coordinate simulator pauses with consumer startup. Automated checks cover
recipe/planning, probe evaluation and session lifecycle behavior; live Isaac/GPU integration
has not been verified as part of this implementation.
