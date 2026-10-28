# Isaac attachment example

Follow [the offline walkthrough](../../docs/ISAAC_OFFLINE.md) before using
`camera-record.yaml`. This is an input contract and shared Rig recipe, not a USD scene or
simulator launcher. It assumes a prepared Jazzy/Zenoh deployment with `bag_logger`,
`cam_front` and a compatible, normally disabled bag player. The external Zenoh router is
already running. The repository's default Lyrical deployment does not satisfy it unchanged.

The first pass selects only the existing logger, records Isaac's camera messages, then
replays them through the same logger selection. The explicit image setting matters:
the normal logger example excludes images because physical cameras record their own video.
An omitted physical camera does not create that video during simulation.

Copy the file to `sessions/camera-record.yaml` inside the deployment. Adapt ROS settings,
instance names, topics, scene provenance and the Python interpreter together. Add actual
autonomy instances and command bindings only after the camera recording round trip works.
The walkthrough includes that extension and an acceptance checklist. No GPU or live Isaac
result is implied by the checked-in example.
