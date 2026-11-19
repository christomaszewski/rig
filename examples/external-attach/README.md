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

It also assumes a Lyrical/Zenoh deployment with `data_dir` set, a router row named
`zenoh-router`, a `bag_logger`, and an autonomy-tier `planner` whose `rigging.yaml` declares
`replay: {sim_time: true}`.

Two routers are involved, as in the
[Zenoh arrangement from live use](../../docs/SIMULATION.md#an-external-simulator):

- The simulator needs a router before the session starts, reachable by the simulator and the
  probe. Listing a router in `session.instances` cannot bootstrap the first check. Run this
  one yourself, on a port other than the one the deployment's router listens on.
- The recipe lists the deployment's own router, `zenoh-router`. With a Zenoh RMW,
  `rig replay` refuses a session that selects no router, so without it the last command
  below fails. Give that router the simulator-side router's address in its `connect`
  endpoints, and point the probe's Zenoh session at the simulator-side router.

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
