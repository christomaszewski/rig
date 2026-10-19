"""Explicit deployment, host and replay target selection; never merge their configuration sources."""
from __future__ import annotations

from dataclasses import dataclass

from . import RigError
from .build_contract import variant_spec
from .common import load_yaml
from .manifest import PLAYER_SERVICE


@dataclass(frozen=True)
class Target:
    runtime: str | None
    arch: str | None
    source: str

    def as_dict(self):
        return dict(os="linux", runtime=self.runtime, arch=self.arch, source=self.source)


def _runtime(descriptors, requested, *, required=True):
    matrices = [set(d.build_platforms) for d in descriptors.values() if d.build_platforms]
    if requested:
        for name, desc in descriptors.items():
            if desc.build_platforms and requested not in desc.build_platforms:
                raise RigError(f"{name}: runtime {requested!r} is not in {desc.build_platforms}")
        return requested
    if not matrices:
        return None
    common = set.intersection(*matrices)
    if len(common) == 1:
        return next(iter(common))
    if required:
        raise RigError(f"deployment runtime is ambiguous or incompatible ({sorted(common)}); "
                       "set platform in vehicle.yaml or pass --platform")
    return None


def deployment_target(manifest, descriptors, *, platform=None, arch=None, required=True):
    runtime = _runtime(descriptors, platform or manifest.platform, required=required)
    architecture = arch or manifest.arch
    if architecture and architecture not in ("arm64", "amd64"):
        raise RigError("arch must be arm64 or amd64")
    supports = [(name, set(variant_spec(d, runtime)[0])) for name, d in descriptors.items()
                if d.build_architectures and (runtime or not d.build_platforms)]
    if architecture:
        for name, supported in supports:
            if architecture not in supported:
                raise RigError(f"{name}/{runtime or 'generic'}: unsupported architecture {architecture}")
    elif supports:
        common = set.intersection(*(s for _, s in supports))
        if len(common) == 1:
            architecture = next(iter(common))
        elif required:
            raise RigError(f"deployment architecture is ambiguous or incompatible ({sorted(common)}); "
                           "set arch in vehicle.yaml or pass --arch (never inferred from the build host)")
    if not architecture and required:
        raise RigError("deployment architecture is unknown; set arch in vehicle.yaml or pass --arch")
    source = "command arguments" if platform or arch else "deployment declarations/inference"
    return Target(runtime, architecture, source)


def host_target(descriptors):
    from .image_catalog import docker_arch
    from .manifest import machine_file
    path = machine_file()
    machine = load_yaml(path) if path.is_file() else {}
    architecture = docker_arch()
    if machine.get("arch") and machine["arch"] != architecture:
        raise RigError(f"provisioned arch {machine['arch']} does not match Docker engine {architecture}")
    runtime = machine.get("platform")
    source = "machine provisioning + Docker engine"
    if not runtime:
        defaults = {d.replay_platform for d in descriptors.values() if d.replay_platform}
        if len(defaults) > 1:
            raise RigError("host runtime is ambiguous; provision this machine with rig provision --platform")
        runtime = next(iter(defaults), None)
        source = "service replay default + Docker engine"
    runtime = _runtime(descriptors, runtime)
    for name, desc in descriptors.items():
        supported, _ = variant_spec(desc, runtime)
        if supported and architecture not in supported:
            raise RigError(f"{name}/{runtime or 'generic'}: host architecture {architecture} is unsupported")
    return Target(runtime, architecture, source)


def parse_scopes(scope=None):
    values = [scope] if isinstance(scope, str) else scope or ["all"]
    scopes = list(dict.fromkeys(s.strip() for value in values for s in value.split(",")))
    if set(scopes) - {"all", "deployment", "host", "replay"}:
        raise RigError("build --scope accepts all, deployment, host, replay (comma-separated or repeated)")
    if "all" in scopes and len(scopes) > 1:
        raise RigError("build scope all cannot be combined with narrower scopes")
    return scopes


def build_selections(manifest, descriptors, scopes, *, platform=None, arch=None):
    """Union of per-service runtime/CPU selections. Generic dependencies share CPU members."""
    if "deployment" not in scopes and (platform or arch):
        raise RigError("--platform/--arch with scopes require deployment; host and replay select their own targets")
    selected = {}
    notes = []
    enabled = {s.service for s in manifest.sensors if s.enabled}
    providers = {name for name, d in descriptors.items() if d.build_provides == "base" or d.msgs_overlay_command}

    def add(name, runtime, architectures):
        desc = descriptors[name]
        variant = runtime if desc.build_platforms else None
        selected.setdefault(name, {}).setdefault(variant, set()).update(architectures)

    for scope in scopes:
        names = enabled | providers
        active = {n: d for n, d in descriptors.items() if n in names}
        if scope in ("deployment", "host"):
            target = (deployment_target(manifest, active, platform=platform, arch=arch)
                      if scope == "deployment" else host_target(active))
            notes.append(f"{scope}: {target.runtime or 'generic'}/linux/{target.arch} ({target.source})")
            for name in active:
                add(name, target.runtime, [target.arch])
        else:
            replay_names = set()
            for sensor in manifest.sensors:
                desc = descriptors[sensor.service]
                start = desc.replay_start
                for kind, key in (("services", sensor.service), ("instances", sensor.name)):
                    start = manifest.replay_policy.get(kind, {}).get(key, {}).get("start", start)
                if (sensor.enabled and start) or sensor.service == PLAYER_SERVICE:
                    replay_names.add(sensor.service)
            requirements = {}
            for name, desc in descriptors.items():
                if name not in replay_names or name in providers:
                    continue
                runtime = _runtime({name: desc}, desc.replay_platform or manifest.platform)
                supported, _ = variant_spec(desc, runtime)
                if desc.build_command and not supported:
                    raise RigError(f"{name}: replay scope requires declared build.architectures")
                add(name, runtime, supported)
                variant = runtime if desc.build_platforms else None
                requirements.setdefault(variant, set()).update(supported)
                notes.append(f"replay: {name}/{variant or 'generic'} {','.join(supported)} (service declarations)")
            # Providers follow their consumers' replay targets, not the vehicle runtime. Generic
            # providers need the union of CPU members; matrix providers keep one tag per runtime.
            for name, desc in descriptors.items():
                if name not in providers:
                    continue
                variants = ([v for v in requirements if v] or [desc.replay_platform or manifest.platform]
                            if desc.build_platforms else [None])
                for variant in variants:
                    runtime = _runtime({name: desc}, variant)
                    supported, _ = variant_spec(desc, runtime)
                    needed = (requirements.get(runtime, set()) | requirements.get(None, set())
                              if desc.build_platforms else set().union(*requirements.values()))
                    needed = needed or set(supported)
                    if needed - set(supported):
                        raise RigError(f"{name}: shared replay provider lacks {sorted(needed - set(supported))}")
                    add(name, runtime, needed)
                    notes.append(f"replay: {name}/{runtime or 'generic'} {','.join(sorted(needed))} (shared provider)")
    return selected, notes
