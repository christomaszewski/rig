# Offline Isaac Sim attachment and replay

This walkthrough connects an already prepared Rig deployment to an already prepared Isaac
scene, records its ROS camera data, and replays that recording with the same version-2
recipe. It starts with the repository's existing `bag_logger` and `cam_front` instance names;
add your controller after this smaller round trip works. It does not require a fictional
planner service to be installed.

**Reference checked 2026-10-28 UTC:** NVIDIA's **Isaac Sim 6.0.0** documentation, ROS 2
**Jazzy**, Linux **Ubuntu 24.04**, and `rmw_zenoh_cpp`. This is a documentation baseline,
not a claim that this combination has been exercised with Rig on a GPU. The Rig feature is
experimental (since v0.2.73). See [the preparation report](ISAAC_PREP_REPORT.md) for local
evidence and the remaining machine acceptance work. The shorter [simulation reference](SIMULATION.md)
defines the CLI and recipe semantics.

## 1. Choose one compatible deployment

Use a Linux machine that meets the selected Isaac release's GPU, driver, memory and disk
requirements. Run NVIDIA's compatibility checker and retain its result; additional camera
resolution and scene complexity can require more memory than the minimum. The simulator
machine and the machine running Rig containers may be separate.
[Isaac 6.0.0 requirements](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/installation/requirements.html)

The repository example currently declares **Lyrical + Zenoh**. NVIDIA 6.0.0 documents
Humble/Jazzy as tested, other native distributions as experimental, and Linux Jazzy + Zenoh
with an externally installed RMW and router. For this walkthrough prepare a **Jazzy + Zenoh**
deployment, including compatible service images and message packages. Changing a YAML distro
label does not convert an image or bridge. Keeping Lyrical requires a separately validated
custom integration; a warning or successful endpoint discovery is not that validation. Since
v0.2.76 Rig accepts Isaac on Jazzy against a Lyrical deployment when every bound topic's REP-2016
type hash matches after startup (see [simulation](SIMULATION.md#check-start-and-stop)); that
proves the message definitions agree, not that Jazzy and Lyrical `rmw_zenoh` interoperate in
every respect.
[NVIDIA ROS installation](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/installation/install_ros.html)

The effective deployment must contain these settings, adapted to a dedicated test identity:

```yaml
vehicle_id: 42
ros:
  distro: jazzy
  rmw: rmw_zenoh_cpp
  domain_id: 42
data_dir: /srv/rig-data
```

Use an absolute writable `data_dir` on the machine running the containers. Keep a native
Linux Docker target: an ARM64/JetPack artifact is not an AMD64 workstation artifact. Prepare
the correct platform/architecture and rebuild the service images as described in
[portable builds](BUILD_AND_REPLAY.md). Host-local provisioning may override your working
manifest; inspect the resolved deployment before assuming the values above took effect.

Required deployment contents:

- A Rig executable that exposes `rig sim check`, `up`, and `down`. An older baked artifact
  carries its own older Rig, even when a newer executable is installed elsewhere.
- Selected infrastructure/autonomy launchers supporting `runtime-images` and immutable image
  pinning. Autonomy launchers must declare `replay.sim_time: true` and honor `RIG_SIM_TIME`.
- `bag_logger` (`ros2-bag-logger`) with graph capture and writable output storage, plus
  `cam_front` declared as a sensor to be replaced. Its physical driver remains omitted.
- A compatible `ros2-bag-player` row, normally disabled, for subsequent recorded playback.
  Do not put the player in `session.instances`.
- Host Python with PyYAML for Rig; the probe interpreter additionally needs `rclpy`,
  `rosgraph_msgs`, and the chosen RMW in a sourced ROS environment.

Run from the deployment root. If using an extracted artifact, use `./rig` in place of `rig`
throughout this guide so the selected executable is unambiguous:

```sh
rig --version
rig sim --help
docker version
docker compose version
rig doctor
rig image audit
```

`rig doctor` evaluates the ordinary deployment; its hardware checks can refer to physical
sensors deliberately omitted from simulation. `rig sim check` evaluates the actual session.

## 2. Finish online preparation before disconnecting

Download/install the exact Isaac release, ROS packages, GPU driver, Docker prerequisites and
all custom message definitions while connected. Install Jazzy's `rmw_zenoh_cpp` and source
that ROS installation before starting Isaac. Save package/image versions with the deployment.
The NVIDIA installation page linked above describes the external Zenoh installation.

Prepare a **local USD scene and every referenced asset**, including robot meshes, textures,
materials, sensor extensions, application extensions and custom Python packages. Download the
matching Isaac asset packs if the scene uses them. Configure a local asset root and check for
an ambient `ISAACSIM_ASSET_ROOT` that would override it. Opening a cached scene once does not
prove all of its dependencies are available offline.
[Isaac 6.0.0 local assets and root precedence](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/installation/install_faq.html)

Run the intended scene online once to finish extension/shader initialization, save it, quit
Isaac, then relaunch it with external network access unavailable. Retain the startup log and
verify that the complete scene renders without remote-asset errors. Keep the local test LAN
available if the simulator and Rig are on separate machines. This disconnected rehearsal is
the acceptance test for asset completeness.

For Rig images, choose one delivery method:

- A prepared deployment using an already populated Docker image cache. Inspect
  `rig sim check --recipe camera-record --json` after installing the recipe below; its
  `image_inventory.images` maps launch references to acquisition `source` references.
  While online, acquire those exact native references with `docker pull REF` or your normal
  image release process. Preserve the images through transfer; check again on the destination.
- An artifact built with `--include-replay --bundle-images`, following
  [onboard offline replay](ONBOARD_REPLAY.md#optional-offline-replay). Run its documented image
  loader before the first offline session. The bake selection must include your required
  services and replay readers. A custom session override can select additional images;
  bundle creation is not a guarantee that every future session recipe is covered.

Acquire the bag-player and any replay-only reader images too. Simulation's inventory only
covers simulation's selected processes. After the first short recording, run the replay
preflight while online to identify remaining replay requirements. Retain immutable IDs/digests,
the artifact metadata, `rig.lock`, recipes, transport files, service launch surfaces and
configuration alongside the image archive. Copy `sessions/` explicitly; recipe distribution
with packages/artifacts is not an automatic promise of this feature.

`sim check` and `sim up --dry-run` do not fetch images. They may report a cold cache as an
error while still printing the inventory. There is no `rig sim prepare` or pull-only sim
command. After staging, require `--pull never`; do not rely on a registry becoming reachable
during an offline run.

## 3. Establish transport before starting Rig

The baseline uses a separately managed Zenoh router already reachable by Isaac, the probe and
the containers. Start it before preflight. Do not start this deployment's normal hardware
with `rig up` just to get its router. Simulation requires the deployment to be quiet, and
listing `zenoh-router` in the session cannot satisfy the earlier probe. An external router
must also avoid the ports/projects that selected services would claim.

On the single Linux host, in a dedicated sourced terminal:

```sh
source /opt/ros/jazzy/setup.bash
export RMW_IMPLEMENTATION=rmw_zenoh_cpp
export ROS_DOMAIN_ID=42
ros2 run rmw_zenoh_cpp rmw_zenohd
```

In each Isaac/probe terminal, source Jazzy and set the same RMW/domain before launch. Check
that every Isaac ROS Context uses the environment domain or explicitly uses 42. Restart
Isaac after changing launch environment; exporting variables in another terminal cannot
change an existing process.

For multiple hosts, use an explicit router topology. The Jazzy RMW documentation describes
router-to-router links and direct remote clients; a direct client needs client mode as well
as the endpoint. For example, the following client override targets a router at a reserved
example address; substitute the reachable LAN address:

```sh
export ZENOH_CONFIG_OVERRIDE='mode="client";connect/endpoints=["tcp/192.0.2.10:7447"]'
```

Apply equivalent transport settings to the actual containers through their supported
launcher configuration/environment, and verify them there. A host export alone does not
guarantee a compose service forwards it. If using configuration files, they must be mounted
at paths meaningful **inside** each container. Preserve the files, not just their path names.
[ROS Jazzy rmw_zenoh networking](https://github.com/ros2/rmw_zenoh/tree/jazzy)

If choosing **Fast DDS** instead, rebuild/select the entire deployment for
`rmw_fastrtps_cpp`, change the recipe to match, and use NVIDIA's multiple-machine/**Docker**
UDP profile guidance. A shared-memory-only host setup is not a portable container-network
configuration. Set `FASTRTPS_DEFAULT_PROFILES_FILE` in each relevant environment and mount
the profile in containers. Do not assume Zenoh and DDS discover one another merely because
they share a domain. See the [NVIDIA transport setup](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/installation/install_ros.html).

## 4. Prepare the scene's exact ROS contract

Create and save `/srv/isaac-scenes/camera-record.usd`, or change the recipe to the scene you
actually prepared. `simulation.scene` is a provenance string; Rig does not open that path.
Enable Isaac's ROS bridge, create a camera and render product, and configure these outputs:

| Isaac endpoint | ROS type | Setup for this example |
| --- | --- | --- |
| Publishes `/clock` | `rosgraph_msgs/msg/Clock` | One simulation-time publisher, advancing while Play is active |
| Publishes `/cam_front/image_raw` | `sensor_msgs/msg/Image` | RGB image, simulation timestamps, `cam_front_optical_frame` |
| Publishes `/cam_front/camera_info` | `sensor_msgs/msg/CameraInfo` | Calibration matching that camera, same frame and time basis |
| Subscribes to commands | None initially | Add the controller extension below after capture/replay works |

For the clock graph, connect a playback tick, ROS Context, simulation-time reader and ROS
clock publisher as shown in NVIDIA's tutorial. Use simulation time consistently for sensor
stamps. Keep the clock monotonic within a session; the time reader's `resetOnStop` option can
reset it, so stop the Rig session before deliberately resetting the world. Rig requires a
real advancing clock during its checks; paused Isaac cannot pass preflight.
[Clock graph](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/ros2_tutorials/tutorial_ros2_clock.html)

Use the camera graph shortcut or Camera Helper/render-product graph, selecting RGB and
camera-info output and setting the final namespace/topic/frame explicitly. Check actual
expanded topic names: automatic prim namespaces can add unwanted prefixes. Frame IDs are
message values, not recipe remaps. The baseline can record images without a TF tree; any
consumer requiring TF needs a coherent tree configured separately.
[Camera graph](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/ros2_tutorials/tutorial_ros2_camera.html)

Choose QoS for each publisher and its actual subscribers. A best-effort publisher cannot
satisfy a reliable subscriber. Transient-local data such as static transforms needs a
compatible late-join policy. Verify reliability, durability and other policies with
`ros2 topic info --verbose`; topic-name agreement alone is insufficient.
[ROS Jazzy QoS compatibility](https://github.com/ros2/ros2_documentation/blob/jazzy/source/Concepts/Intermediate/About-Quality-of-Service-Settings.rst)
Isaac accepts QoS profiles on its ROS nodes. In the 6.0.0 QoS Profile node, select **Custom**
before editing individual fields so the saved USD retains the custom profile. Reopen the
scene and inspect the published endpoint again.
[Isaac QoS setup](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/ros2_tutorials/tutorial_ros2_qos.html)

Press Play, then inspect from the same sourced environment the Rig probe will use:

```sh
/usr/bin/python3 -c 'import rclpy, rosgraph_msgs; print("ROS probe imports OK")'
ros2 topic list -t
ros2 topic info /clock --verbose
ros2 topic echo /clock --once
ros2 topic info /cam_front/image_raw --verbose
ros2 topic hz /cam_front/image_raw
```

Stop the rate command after observing samples. Check camera-info dimensions, intrinsics,
frame ID and image timestamps separately; Rig's graph probe does not validate image content.
If the ROS CLI daemon came from a different environment, stop it with `ros2 daemon stop`
before retrying discovery in the correct environment.

## 5. Install and check the shared recipe

Copy [camera-record.yaml](../examples/isaac-attach/camera-record.yaml) into
`sessions/camera-record.yaml` in the deployment. The entire example is:

```yaml
version: 2
session:
  instances: [bag_logger]
  clock: source
  overrides:
    bag_logger:
      record:
        mode: allow
        topics: [/cam_front/image_raw, /cam_front/camera_info]
        exclude_images: false
      control: {use_sim_time: true}
simulation:
  provider: isaac
  mode: attach
  replace: [cam_front]
  ros: {distro: jazzy, rmw: rmw_zenoh_cpp, domain_id: 42}
  scene: /srv/isaac-scenes/camera-record.usd
  isaac_version: "6.0.0"
  asset_version: camera-record-v1
  publishes:
    - {topic: /cam_front/image_raw, type: sensor_msgs/msg/Image, instance: cam_front}
    - {topic: /cam_front/camera_info, type: sensor_msgs/msg/CameraInfo, instance: cam_front}
  subscribes: []
  probe: [/usr/bin/python3]
  timeout_s: 15
replay:
  source: isaac-camera
  playback:
    topics: [/cam_front/image_raw, /cam_front/camera_info]
```

The logger normally excludes images because real camera drivers record video themselves.
Here the driver is omitted, so `exclude_images: false` and the explicit camera topic list
are essential. Size disk capacity and the logger's storage/cache settings for uncompressed
ROS image traffic. The session overrides stay separate from normal deployment configuration.

`session.instances` is exact: this recipe starts only the logger. It neither starts omitted
physical sensors nor silently adds a router/controller. The shared replay path adds its
managed bag player. `publishes`/`subscribes` describe Isaac's direction. `/clock` is owned by
the session and must not be listed as a data binding.

```sh
rig sim check --recipe camera-record --pull never
rig sim check --recipe camera-record --pull never --json > isaac-check.json
rig sim up --recipe camera-record --pull never --dry-run
```

Resolve every error and review warnings. A successful preflight means the current image,
ROS endpoint and clock checks passed. It is not a simulation fidelity test. Check performs
no launch/pull and does not write generated session state; the redirection above is your
explicit evidence file. Exit 0 indicates a ready plan; invalid/preflight errors are nonzero.

## 6. Record, observe and stop

Keep Isaac playing and the external router running:

```sh
rig sim up --recipe camera-record --pull never --label isaac-camera
rig status
rig status --format json
rig logs bag_logger --tail 100
rig logs bag_logger -f
```

Stop following logs with Ctrl+C when finished observing; that does not stop the session.
Record a short changing scene first, then:

```sh
rig sim down --end-run
rig runs
```

`status`, `logs` and teardown use the saved session's selection/configuration/environment.
Teardown checks the original Docker engine and session ownership. It leaves Isaac and the
external router running. Stop those separately after capture if desired. Do not edit saved
state to bypass an ownership or engine error. Restore the expected environment/launchers and
retry teardown; failed startup retains evidence and can need this same cleanup command.
Bare `rig down` also follows the shared session. `rig sim down` refuses an active replay
session; after replay, use `rig down --end-run` as shown below.

Data lives under `/srv/rig-data/runs/<run-id>/`: the run manifest, config snapshot, captured
logs, and `bags/bag_logger/<session>/` hold the recording and provenance. Simulation evidence
is under `.rig/simulation/` (`recipe.yaml`, `resolved.yaml`, pre/post probe JSON and execution
state). Shared lifecycle state is under `var/sessions/<id>/state.json`, with an active pointer
at `var/sessions/current.json`. Immutable simulation configs remain under
`var/sim/sessions/<id>/`; shared version-2 replay uses the same lifecycle registry and keeps
its configs under `var/replay/sessions/`. Keep the complete run when transferring it.

Inspect the actual bag with `ros2 bag info /absolute/path/to/bag-session`; both camera topics
must have nonzero message counts. Metadata alone is not a camera recording. Check graph
epochs and logger errors too. Do not assume a green startup guarantees sufficient disk
space or uninterrupted data for the remainder of the run.

## 7. Replay through the same recipe, without Isaac

Stop Isaac or disconnect its ROS graph before replay so it cannot compete with the player
for camera topics and `/clock`. Keep the external router available for this exact common
selection. `isaac-camera` selects the newest run with that label; for reproducible acceptance,
replace `replay.source` with the exact sealed run ID or its absolute path.

```sh
rig replay check --recipe camera-record --pull never
rig replay --recipe camera-record --pull never
rig status
rig logs bag_player --tail 100
# After playback has completed and the logger has drained:
rig down --end-run
```

The actual player name can differ if it was auto-provisioned around a name collision; inspect
the plan/status. For compatible coordinated producers, opt in using
`--startup coordinated` on both check and execution; see [coordinated startup](replay-coordination.md).
Automatic completion/sealing is available with replay's `--auto-end`, subject to that
workflow's ownership and successful-completion checks.

The common selection and logger override are reused. Isaac supplied the first clock; the
bag player supplies the replay clock. Recorded `/clock` is not replayed as a second clock.
Replaying the sensors follows the recorded trajectory. A new controller command cannot
change those past images. To run the world again, prepare/reset Isaac and attach a new
simulation session instead. Scene version and seed are provenance, not deterministic replay.

## 8. Extend to a controller

Install your real autonomy service and configure it to consume the two camera topics (and
any additional sensor/TF inputs it needs). Add its instance to `session.instances`, declare
simulation-time support in its launcher, and add its command topic to Isaac's subscriptions:

```yaml
# Example fragments; "planner" must already be a real deployment instance.
session:
  instances: [bag_logger, planner]
simulation:
  subscribes:
    - {topic: /planner/cmd_vel, type: geometry_msgs/msg/Twist}
replay:
  playback:
    topics: [/cam_front/image_raw, /cam_front/camera_info]
    exclude: [/planner/cmd_vel]
```

Merge these fragments into the complete recipe; do not replace the other required fields.
Configure an Isaac Twist subscriber at that exact topic and connect its linear/angular
outputs to the robot's controller/articulation, with the robot's real wheel parameters and
stage units. Merely discovering a subscriber does not prove commands move the robot.
[NVIDIA's Twist/controller example](https://docs.isaacsim.omniverse.nvidia.com/6.0.0/ros2_tutorials/tutorial_ros2_drive_turtlebot.html)

Assign one command authority during capture. To compare controller output during replay,
record its command topic too, but exclude it from playback so the live controller owns its
regenerated output. Keep source and result runs separate. Feeding recorded controls into a
live simulator or mixing bag sensors with Isaac is not supported by this attach workflow.

If you need TF, declare each simulated TF topic and decide ownership before attaching.
The current planner rejects a topic declared published by both Isaac and a selected service,
including `/tf` and `/tf_static`, even when their individual frame sets would be disjoint.
It has no per-transform ownership proof or filter. During replay, a whole-topic TF exclusion
also removes transforms another consumer may need; choose the replay graph explicitly.

## Troubleshooting and acceptance evidence

| Symptom | Check/action |
| --- | --- |
| `sim` is unknown | Use the new Rig checkout/package; older artifact-local Rig stays older. |
| ROS settings mismatch | Compare effective manifest, recipe, probe environment and built image contents; rebuild for a distro/RMW change. |
| Probe cannot import ROS | Use the sourced Jazzy interpreter in `simulation.probe`; verify both imports before attaching. |
| Graph exists in Isaac but probe sees nothing | Check Context domain, RMW, external router, client/router topology, container mounts and firewall; inspect from the probe environment. |
| Clock missing, paused, duplicated or backwards | Enable exactly one simulation clock, press Play, remove other clock publishers, and begin a new session after a reset. |
| Topic is present but no data arrives | Check endpoint type/QoS and actual payloads; the prelaunch probe cannot validate a subscriber that does not yet exist. Postlaunch QoS errors roll startup back. |
| QoS result is unknown | Inspect native endpoint policies and test delivery; warnings do not certify compatibility. |
| Images absent from the bag | Confirm `exclude_images: false`, allow-list names, logger subscriptions, disk capacity and nonzero bag counts. |
| Declared output authority conflict | Remove the duplicate producer in scene/service config; split ownership deliberately. Shared TF topics have no per-frame exception. |
| Native image/target error | Stage the correct runtime/CPU images and immutable identities. `--pull never` will not repair a cold cache. |
| Saved session or existing projects block startup | Inspect status, use the matching session teardown, then retry. No force takeover is part of sim attachment. |
| Down reports changed engine/launcher/config | Restore the saved session's expected connection and launch surface; preserve evidence before investigating. |
| Offline scene has missing materials/assets | Restore all local references/extensions and repeat the disconnected restart test. |

Retain the following with the acceptance run. Tick a row only after observing it on the
actual simulation machine:

- [ ] Compatibility checker, Isaac version, ROS package/RMW versions, image IDs and platform recorded.
- [ ] Fully disconnected Isaac restart opens the local scene with no missing dependencies.
- [ ] Preflight JSON passes with `--pull never`; warnings reviewed and graph/QoS output saved.
- [ ] Physical drivers/player remain stopped during sim; exactly one advancing clock observed.
- [ ] Image payloads, calibration, frame IDs and timestamps inspected; controller/TF behavior tested if used.
- [ ] Run has actual nonempty bags, frozen config and pre/post probe evidence; normal teardown seals it.
- [ ] Isaac is stopped, same recipe replays offline, and the result recording contains the expected samples.
- [ ] Paused/missing clock and incompatible endpoint tests prevent startup without touching unrelated projects.
- [ ] Failed startup can be inspected and cleaned with the saved session; normal deployment is usable afterward.

Repository fixture tests can establish schema, planning, mocked ROS evaluation and lifecycle
behavior. They cannot tick these hardware/transport/physics rows. This preparation leaves
those rows pending until a Linux GPU operator performs and records the acceptance run.
