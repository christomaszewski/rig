"""rig — a vehicle-level stack orchestrator (infra · sensors · autonomy).

`rig` reads a vehicle manifest (which services this machine runs) and *delegates* the
bring-up/teardown of each to that service's own launcher (``<service>-up``). It never
reimplements a service's own logic — it owns only the cross-cutting concerns (instance-name
uniqueness, fleet-wide ROS env, ordering, status/health aggregation, lifecycle/cleanup).

The dependency is strictly one-way: rig depends on the service repos; a service never knows about rig.
rig learns each service only through its ``rigging.yaml`` descriptor + the launcher CLI.
"""

__version__ = "0.2.86"


class RigError(Exception):
    """A user-facing error (bad manifest, missing service/launcher, etc.). Caught in the CLI and
    printed without a traceback."""
