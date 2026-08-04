# DEPRECATED — these services moved to rig-infra

The bundled infra templates now live in their own repo:
**https://github.com/christomaszewski/rig-infra** (zenoh-router, ros2-bag-logger, ros1-bag-logger,
plus the `fleet-ros` base image they now default to).

This copy remains for ONE version so existing `services.yaml` paths and `rig init --infra` keep
working, then it is deleted. Migrate now:

```bash
git clone https://github.com/christomaszewski/rig-infra   # beside your deployment / rig checkout
```

```yaml
# services.yaml — repoint each service:
services:
  zenoh-router:    { path: ../rig-infra/zenoh-router }     # was ../rig/templates/zenoh-router
  ros2-bag-logger: { path: ../rig-infra/ros2-bag-logger }
```

`rig init --infra zenoh-router` (bare name) already resolves from the workspace scan when rig-infra
is cloned as a sibling — the bundled fallback here is what prints the deprecation warning.

Why the move (ROADMAP §3e): the fleet-ros base image needs ordinary `build:` machinery, not a new
rig subsystem; rig-infra's CI certifies every service against the launcher contract, which was the
job co-location used to do.
