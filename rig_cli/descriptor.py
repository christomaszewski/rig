"""rigging.yaml — the per-repo descriptor that tells rig how to drive a service's launcher.

This is the porting adapter: a repo becomes rig-compatible by shipping a launcher that honors the
contract (up/down/status/logs/config on one config; arbitrary config path; fleet ROS env; stdout/stderr
discipline) plus this small descriptor. The `verbs` map adapts a launcher whose CLI doesn't match rig's
logical verbs (e.g. gige-up takes compose subcommands, so status -> "ps").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import RigError
from .common import load_yaml

# The descriptor filename rig reads from each service repo. `rigging.yaml` is canonical; `deploy.yaml` is
# accepted as a legacy fallback during the rename.
DESCRIPTOR_NAMES = ("rigging.yaml", "deploy.yaml")


def find_descriptor(repo: Path) -> Path | None:
    """First existing descriptor in `repo` (rigging.yaml preferred, deploy.yaml legacy), or None."""
    for name in DESCRIPTOR_NAMES:
        candidate = repo / name
        if candidate.exists():
            return candidate
    return None


# Logical verb -> default launcher arg string. Overridable per repo via the descriptor's `verbs:`.
DEFAULT_VERBS = {
    "up": "up -d",
    "down": "down",
    "status": "ps",
    "logs": "logs",
    "config": "config",
    # Optional: pre-pull images with NO container changes (prime a vehicle's cache while the registry is
    # reachable, then run offline). Launchers that pass compose subcommands through get it for free.
    "pull": "pull",
}


@dataclass
class Descriptor:
    service: str
    repo: Path
    launcher: str
    verbs: dict[str, str]
    ros_distro: str | None
    external_volumes: list[str]  # name patterns (may contain `{name}`); GC'd only on final teardown
    host_ports: list[str]        # config paths to host-facing ports rig validates for clashes
    build_command: str | None = None             # `rig build` runs this: <command> <registry> [tag]
    build_images: list[str] = field(default_factory=list)  # image repos the build produces (certify checks
    #                                              the compose pulls them as :RIG_IMAGE_TAG — build/pull agreement)
    mirror: list[str] = field(default_factory=list)  # third-party images to copy into the registry

    @property
    def launcher_path(self) -> Path:
        return self.repo / self.launcher

    def verb_args(self, verb: str) -> list[str]:
        """The launcher args for a logical verb (e.g. 'status' -> ['ps'])."""
        spec = self.verbs.get(verb)
        if spec is None:
            spec = DEFAULT_VERBS.get(verb, verb)
        return spec.split()


def load_descriptor(service: str, repo: Path) -> Descriptor:
    path = find_descriptor(repo)
    if path is None:
        raise RigError(
            f"{service}: no rigging.yaml in {repo} — is the service repo checked out, and is it "
            f"rig-compatible? (see README)"
        )
    data = load_yaml(path)
    declared = data.get("service", service)
    if declared != service:
        raise RigError(
            f"{path}: declares service '{declared}' but the catalog routes it as '{service}'"
        )
    verbs = dict(DEFAULT_VERBS)
    verbs.update(data.get("verbs") or {})

    build_raw = data.get("build")  # `build: <cmd>` or `build: { command: <cmd>, images: [...] }`
    build_images: list[str] = []
    if isinstance(build_raw, str):
        build_command = build_raw
    elif isinstance(build_raw, dict):
        build_command = build_raw.get("command")
        build_images = list(build_raw.get("images") or [])
    else:
        build_command = None

    return Descriptor(
        service=service,
        repo=repo,
        launcher=data.get("launcher") or f"{service}-up",
        verbs=verbs,
        ros_distro=data.get("ros_distro"),
        external_volumes=list(data.get("external_volumes") or []),
        host_ports=list(data.get("host_ports") or []),
        build_command=build_command,
        build_images=build_images,
        mirror=list(data.get("mirror") or []),
    )
