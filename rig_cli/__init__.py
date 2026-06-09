"""rig — a vehicle/machine-level sensor-stack orchestrator.

`rig` is "a loop + a manifest": it reads a vehicle manifest (which sensors this machine runs) and
*delegates* the bring-up/teardown of each to that service's own per-sensor launcher (``<service>-up``).
It never reimplements per-stack logic — it owns only the cross-cutting concerns (instance-name
uniqueness, fleet-wide ROS env, ordering, status/health aggregation, lifecycle/cleanup).

The dependency is strictly one-way: rig depends on the service repos; a service never knows about rig.
rig learns each service only through its ``rigging.yaml`` descriptor + the launcher CLI.
"""

__version__ = "0.1.16"


class RigError(Exception):
    """A user-facing error (bad manifest, missing service/launcher, etc.). Caught in the CLI and
    printed without a traceback."""
