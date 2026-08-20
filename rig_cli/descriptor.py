"""rigging.yaml — the per-repo descriptor that tells rig how to drive a service's launcher.

This is the porting adapter: a repo becomes rig-compatible by shipping a launcher that honors the
contract (up/down/status/logs/config on one config; arbitrary config path; fleet ROS env; stdout/stderr
discipline) plus this small descriptor. The `verbs` map adapts a launcher whose CLI doesn't match rig's
logical verbs (e.g. gige-up takes compose subcommands, so status -> "ps").
"""
from __future__ import annotations

import re
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
    build_platforms: list[str] = field(default_factory=list)  # the build MATRIX (e.g. [jp7, jp6]): distinct
    #                                              image sets per hardware/OS target. Presence makes the
    #                                              service platform-dependent: rig composes its pull tag as
    #                                              <tag>-<platform> and certify checks each entry renders.
    platform_auto_detect: str | None = None      # the launcher's standalone host probe (e.g.
    #                                              /etc/nv_tegra_release) — informational; declared wins
    platform_override_env: str | None = None     # env var the launcher honors as platform override (e.g.
    #                                              CAM_PLATFORM) — rig mirrors RIG_TARGET_PLATFORM into it
    mirror: list[str] = field(default_factory=list)  # third-party images to copy into the registry
    tier: str = "sensor"         # optional hint: "infra" = shared, up-first (dashboard, routers, loggers);
    #                              "autonomy" = graph consumer, up-last / down-first (planners, SLAM)
    examples: list[str] = field(default_factory=list)  # optional repo-relative example configs — copied by
    #                                              `rig init --discover`; default --config for `certify --repo`

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
        # The bundled templates/ moved to rig-infra and were deleted (v0.1.35); an old services.yaml
        # path under them must fail with the pointer, not a mystery.
        bundled = Path(__file__).resolve().parent.parent / "templates"
        try:
            in_bundled = repo.resolve().is_relative_to(bundled)
        except (OSError, ValueError):
            in_bundled = False
        if in_bundled:
            raise RigError(
                f"{service}: rig's bundled templates/ moved to rig-infra "
                f"(https://github.com/christomaszewski/rig-infra) — clone it beside your deployment "
                f"and point services.yaml at ../rig-infra/{repo.name}"
            )
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

    examples_raw = data.get("examples")  # a single path or a list of them
    examples = [examples_raw] if isinstance(examples_raw, str) else list(examples_raw or [])

    tier = str(data.get("tier") or "sensor")
    if tier not in ("sensor", "infra", "autonomy"):  # a typo must not silently demote a service to sensor
        raise RigError(f"{path}: tier must be 'infra', 'sensor', or 'autonomy', not '{tier}'")

    build_raw = data.get("build")  # `build: <cmd>` or `build: { command: <cmd>, images: [...], platforms: [...] }`
    build_images: list[str] = []
    build_platforms: list[str] = []
    if isinstance(build_raw, str):
        build_command = build_raw
    elif isinstance(build_raw, dict):
        build_command = build_raw.get("command")
        build_images = list(build_raw.get("images") or [])
        build_platforms = [str(p) for p in (build_raw.get("platforms") or [])]
    else:
        build_command = None
    for p in build_platforms:  # platform names suffix image tags — they must be tag-safe fragments
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", p):
            raise RigError(f"{path}: build.platforms entry '{p}' is not a valid image-tag fragment "
                           f"([A-Za-z0-9][A-Za-z0-9._-]*)")

    platform_raw = data.get("platform") or {}  # `platform: { auto_detect: <path>, override_env: <VAR> }`
    if not isinstance(platform_raw, dict):
        raise RigError(f"{path}: `platform` must be a mapping with auto_detect/override_env")
    unknown = set(platform_raw) - {"auto_detect", "override_env"}
    if unknown:  # a typo here silently breaks the routing standard — fail loudly
        raise RigError(f"{path}: platform: unknown key(s) {', '.join(sorted(unknown))} — it carries "
                       f"only auto_detect, override_env")
    override_env = platform_raw.get("override_env")
    if override_env is not None:
        override_env = str(override_env)
        if not re.match(r"^[A-Z][A-Z0-9_]*$", override_env):
            raise RigError(f"{path}: platform.override_env '{override_env}' — env names are "
                           f"UPPERCASE [A-Z][A-Z0-9_]*")
        from .manifest import RIG_OWNED_ENV
        if override_env in RIG_OWNED_ENV:
            raise RigError(f"{path}: platform.override_env '{override_env}' collides with a rig-owned "
                           f"variable — declare the service's OWN env name (e.g. CAM_PLATFORM)")

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
        build_platforms=build_platforms,
        platform_auto_detect=(str(platform_raw["auto_detect"]) if platform_raw.get("auto_detect")
                              else None),
        platform_override_env=override_env,
        mirror=list(data.get("mirror") or []),
        tier=tier,
        examples=examples,
    )
