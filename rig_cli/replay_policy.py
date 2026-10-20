"""Replay startup policy shared by runtime and build planning."""
from .manifest import PLAYER_SERVICE


def setting(manifest, row, descriptor):
    """(effective value, reason, explicit deployment choice).

    Descriptor True means eligible for automatic selection, not force-start.
    """
    start = getattr(descriptor, "replay_start", True)
    reason, explicit = "service default", False
    policy = manifest.replay_policy
    for kind, key in (("services", row.service), ("instances", row.name)):
        if kind == "instances" and getattr(row, "replay_start", None) is not None:
            start, reason, explicit = row.replay_start, "instance replay.start", True
        if key in policy.get(kind, {}):
            start, reason, explicit = policy[kind][key]["start"], f"replay.{kind}.{key}", True
    return start, reason, explicit


def forced(manifest, descriptors):
    names = []
    for row in manifest.sensors:
        start, _, explicit = setting(manifest, row, descriptors.get(row.service))
        if row.service != PLAYER_SERVICE and start and explicit:
            names.append(row.name)
    return names
