# Isaac attachment example

Follow [the offline walkthrough](../../docs/ISAAC_OFFLINE.md) before using
`camera-record.yaml`. This is an input contract and shared Rig recipe, not a USD scene or
simulator launcher. It assumes a prepared Jazzy/Zenoh deployment with a router row
named `zenoh-router`, `bag_logger`, `cam_front` and a compatible, normally disabled bag
player. A second, external Zenoh router for Isaac and the probe is already running on another
port, and the deployment's router connects to it
([walkthrough, section 3](../../docs/ISAAC_OFFLINE.md#3-establish-transport-before-starting-rig)).
The repository's default Lyrical deployment does not satisfy it unchanged.

The first pass selects the deployment's router and the existing logger, records Isaac's
camera messages, then replays them through the same selection. The router is listed because
`rig replay` refuses a Zenoh session that selects none. The explicit image setting matters:
the normal logger example excludes images because physical cameras record their own video.
An omitted physical camera does not create that video during simulation.

Copy the file to `sessions/camera-record.yaml` inside the deployment. Adapt ROS settings,
instance names, topics, scene provenance and the Python interpreter together. Add actual
autonomy instances and command bindings only after the camera recording round trip works.
Keep one shared file: its `simulation` and `replay` sections supply the two input sources,
while `session.instances` selects the same consumers. An explicit `--recipe PATH.yaml` also
works for both commands if the file lives elsewhere.
The walkthrough includes that extension and an acceptance checklist. No GPU or live Isaac
result is implied by the checked-in example.
