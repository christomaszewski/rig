# External simulator attachment example

`world.yaml` is a shared Rig recipe for a simulator other than Isaac. It is an input
contract, not a simulator or a launcher: start the simulator yourself, then Rig attaches the
selected services to its ROS graph. Read
[An external simulator](../../docs/SIMULATION.md#an-external-simulator) first.

The recipe assumes a made-up simulator, `worldsim`, that:

- is the only `/clock` publisher (`rosgraph_msgs/msg/Clock`) and keeps the clock advancing
  from the moment it starts, also while it waits for the stack;
- publishes `/world/view` and `/world/report`, and subscribes to `/agent/plan`;
- uses the deployment's middleware and ROS domain.

It also assumes a Lyrical/Zenoh deployment with `data_dir` set, a `bag_logger`, and an
autonomy-tier `planner` whose `rigging.yaml` declares `replay: {sim_time: true}`. The Zenoh
router must already be reachable by the simulator and the probe; listing a router in
`session.instances` cannot bootstrap the first check.

Copy the file to `sessions/world.yaml` inside the deployment. Adapt the instance names, ROS
settings, topics, scene, seed and probe interpreter together. Set `simulator.name` and
`simulator.version` to what is really running: Rig records them, the scene and the seed as
stated and verifies none of them.

```sh
rig sim check --recipe world
rig sim up --recipe world --label world-a
rig sim down --end-run
rig replay check --recipe world    # the same consumers, now fed from the recording
```

The provider is experimental. [State](../../docs/STATE.md) says what has been exercised
against a live simulator; this checked-in example itself has not been run.
