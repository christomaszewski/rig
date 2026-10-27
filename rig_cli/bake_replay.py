"""Plan onboard replay images at bake time, without a recording or running containers."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import re
import shutil
import tempfile

import yaml

from . import RigError, dispatch, replay, resolve, runtime_images
from .artifact_images import known_values
from .common import eprint, load_yaml
from .manifest import PLAYER_SERVICE
from .replay_policy import setting


def _local_players(root, catalog):
    """Look beside routed services first, then use the usual one-level workspace search."""
    from .descriptor import find_descriptor

    def matches(paths):
        result = set()
        for path in paths:
            descriptor = find_descriptor(path)
            if descriptor and load_yaml(descriptor).get("service") == PLAYER_SERVICE:
                result.add(path.resolve())
        return sorted(result)

    nearby = matches([root / "services" / PLAYER_SERVICE,
                      *(entry.path.parent / PLAYER_SERVICE for entry in catalog.values())])
    if nearby:
        return nearby
    return matches(path if path.name == PLAYER_SERVICE else path / PLAYER_SERVICE
                   for path in root.parent.iterdir() if path.is_dir() and not path.name.startswith("."))


def _player_source(root, catalog, lock, explicit=None):
    from .descriptor import find_descriptor
    from . import install
    from .lock import record_package, record_registry
    from .refs import unqualified

    def fetch(source):
        if (not source.get("repo") or not re.fullmatch(
                r"[0-9a-f]{40}(?:[0-9a-f]{24})?", str(source.get("rev", "")))):
            raise RigError("bake --include-replay: bag player package needs a source pinned to a full commit SHA")
        return install._fetch_source(PLAYER_SERVICE, source)

    def discard_player_pins():
        # An explicit replacement must not leave a conflicting pin in the artifact's lock.
        packages = lock.get("packages", {})
        for ref in list(packages):
            if unqualified(ref) == PLAYER_SERVICE and packages[ref].get("kind") == "service":
                del packages[ref]

    if explicit and Path(explicit).expanduser().is_dir():
        folder = Path(explicit).expanduser().resolve()
        for candidate in (folder, folder / PLAYER_SERVICE):
            descriptor = find_descriptor(candidate)
            if descriptor and load_yaml(descriptor).get("service") == PLAYER_SERVICE:
                discard_player_pins()
                return candidate
        raise RigError("bake --replay-player: expected a ros2-bag-player service or rig-infra directory")
    if not explicit:
        if PLAYER_SERVICE in catalog:
            return catalog[PLAYER_SERVICE].path
        pinned = [(ref, pkg) for ref, pkg in lock.get("packages", {}).items()
                  if unqualified(ref) == PLAYER_SERVICE and pkg.get("kind") == "service"]
        if len(pinned) > 1:
            raise RigError("bake --include-replay: multiple locked bag player versions; "
                           "select one with --replay-player PATH_OR_PINNED_REF")
        if pinned:
            return fetch(pinned[0][1].get("source") or {})
        candidates = _local_players(root, catalog)
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            raise RigError("bake --include-replay: ambiguous bag player sources: " +
                           ", ".join(map(str, candidates)) + "; select one with --replay-player PATH")
    elif unqualified(explicit) != PLAYER_SERVICE or not re.search(r"@\d+\.\d+\.\d+$", explicit):
        raise RigError("bake --replay-player: use a service directory or a pinned "
                       "ros2-bag-player registry reference (NAME/ros2-bag-player@VERSION)")
    try:
        entry, _, pkg = install.resolve_ref(explicit or PLAYER_SERVICE, history=bool(explicit))
    except RigError as exc:
        raise RigError(f"bake --include-replay: cannot find a bag player ({exc}); "
                       "sync a configured package registry or pass --replay-player PATH_OR_PINNED_REF") from exc
    source = pkg.manifest.get("source") or {}
    if pkg.kind != "service":
        raise RigError("bake --include-replay: the bag player package must be a service")
    path = fetch(source)
    if explicit:
        discard_player_pins()
    record_registry(lock, entry.name, rtype=entry.type, location=entry.location,
                    commit=install.registry_commit(entry))
    record_package(lock, install.qualified(entry, pkg), {"kind": "service", "source": source})
    return path


@contextmanager
def deployment_inputs(root, *, player=None, target_overrides=None):
    """Add missing replay wiring in an isolated bake input tree; never edit the deployment.

    Service routes remain anchored to the original sources. Only config inputs are copied;
    recordings, generated state and local identity are never copied into this workspace.
    """
    from .catalog import load_catalog
    from .descriptor import load_descriptor
    from .lock import load_lock, save_lock, record_instance, sha256_file
    from .manifest import load_manifest
    manifest = load_manifest(root, context="deployment", target_overrides=target_overrides)
    if any(row.service == PLAYER_SERVICE for row in manifest.sensors):
        yield root
        return
    catalog, lock = load_catalog(root), load_lock(root)
    source = _player_source(root, catalog, lock, player)
    load_descriptor(PLAYER_SERVICE, source)
    raw = load_yaml(root / "vehicle.yaml")
    used = {row.name for row in manifest.sensors}
    name, suffix = "bag_player", 2
    while name in used or (root / "config" / "autonomy" / f"{name}.yaml").exists():
        name, suffix = f"bag_player_{suffix}", suffix + 1
    config_path = f"config/autonomy/{name}.yaml"
    config = dict(service=PLAYER_SERVICE, name=name, source={"session": "latest"}, play={"loop": False})
    loggers = [r.name for r in manifest.sensors if r.service == "ros2-bag-logger" and r.enabled]
    if len(loggers) == 1:
        config["source"]["logger"] = loggers[0]
    raw.setdefault("autonomy", [])
    raw["autonomy"] = [*(raw["autonomy"] or []), dict(name=name, service=PLAYER_SERVICE,
                          config=config_path, enabled=False, order=999)]
    with tempfile.TemporaryDirectory(prefix="rig-bake-inputs-") as temporary:
        tree = Path(temporary)
        if (root / "config").is_dir():
            shutil.copytree(root / "config", tree / "config")
        # Hand-built deployments can keep configs outside config/. Preserve those inputs too.
        for tier in ("infra", "sensors", "autonomy"):
            for row in raw.get(tier) or []:
                path = Path(row["config"])
                if path.is_absolute():
                    continue
                if ".." in path.parts:
                    row["config"] = str((root / path).resolve())
                elif (root / path).is_file() and not (tree / path).exists():
                    (tree / path).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(root / path, tree / path)
        if (root / "metadata.yaml").is_file():
            shutil.copy2(root / "metadata.yaml", tree / "metadata.yaml")
        (tree / "vehicle.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
        routes = {name: {"path": str(entry.path)} for name, entry in catalog.items()}
        routes[PLAYER_SERVICE] = {"path": str(source)}
        (tree / "services.yaml").write_text(yaml.safe_dump({"services": routes}, sort_keys=False))
        cfg = tree / config_path
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(yaml.safe_dump(config, sort_keys=False))
        record_instance(lock, name, profile=None, base_sha256=sha256_file(cfg))
        save_lock(tree, lock)
        eprint(f"bake: adding {name} from {source} to the artifact (enabled: false, order: 999)")
        yield tree


def selected_rows(manifest, descriptors):
    """The possible automatic replay set; source availability is decided on the vehicle."""
    players = [r for r in manifest.sensors if r.service == PLAYER_SERVICE]
    if len(players) != 1:
        raise RigError("bake --include-replay needs exactly one ros2-bag-player instance; "
                       "configure it with enabled: false before baking")
    rows = []
    for row in manifest.select([], enabled_only=False):
        desc = descriptors[row.service]
        start, _, explicit = setting(manifest, row, desc)
        if row.service == PLAYER_SERVICE or (start and (
                explicit or desc.replay_source or (row.tier == "infra" and row.enabled))):
            rows.append(row)
    return rows


def image_plan(root, staging, manifest, descriptors, *, fleet=False):
    """Use the shipped launchers with replay config patches and the vehicle's runtime/CPU.

    Placeholder paths are only image-query inputs, never shipped bindings or fake recordings.
    Fleet identity may remain unresolved; runtime-images must reject it if it selects images.
    The normal deployment manifest/configs and startup scripts remain unchanged.
    """
    from .replay_plan import capabilities
    rows = selected_rows(manifest, descriptors)
    shipped = {name: replace(desc, repo=staging / "services" / name)
               for name, desc in descriptors.items() if any(r.service == name for r in rows)}
    query_manifest = replace(manifest, context="deployment", missing_identity=(),
                             vehicle_id=None if fleet else manifest.vehicle_id)
    env = dispatch.fleet_env(query_manifest, descriptors)
    configs, required = {}, {}
    with tempfile.TemporaryDirectory(prefix="rig-bake-replay-") as temporary:
        source = Path(temporary)
        env.update(RIG_REPLAY_SOURCE=str(source), RIG_SIM_TIME="1",
                   RIG_REPLAY_FROM_S="0", RIG_REPLAY_TO_S="0")
        variables = {**manifest.vars, **replay.timeline_variables(
            source, has_bags=False, sim_time=True, start_delay_s=0,
            w_from=0, w_to=None, session=None)}
        for row in rows:
            desc = shipped[row.service]
            values = {**variables, "name": row.name}
            patch = None
            if desc.replay_source:
                values.update(replay_input_path=str(source / desc.replay_source.data_path(row.name)),
                              replay_input_sessions=[], replay_input_proxy=None)
                patch = known_values(desc.replay_source.overrides, values)
                if desc.replay_source.binding == "direct":
                    required[row.name] = desc.replay_source.image_labels
            configs[row.name] = known_values(resolve._layered_dict(
                row, root, extra_overrides=patch), values)
            if row.service == PLAYER_SERVICE:
                caps = capabilities(row, desc)
                if caps.get("plan") != 1 or caps.get("inspect_calls") != 1 or not caps.get("image_labels"):
                    raise RigError("bake --include-replay: update ros2-bag-player; replay plan v1, "
                                   "recorded-call inspection and image capability labels are required")
                labels = caps["image_labels"]
                if not isinstance(labels, dict) or any(
                        not isinstance(role, str) or not role or not isinstance(values, dict) or not values
                        or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items())
                        for role, values in labels.items()):
                    raise RigError("bake --include-replay: invalid player image capability labels")
                required[row.name] = labels
        inventory = runtime_images.inventory(query_manifest, shipped, rows, configs,
            architecture=manifest.arch, env=env, required_labels=required)
    # Only the resolved image requirements leave the planning workspace.
    return dict(version=1, instances=inventory["instances"], images=inventory["images"])


def required_labels(plan):
    return {ref: item["required_labels"] for ref, item in (plan or {}).get("images", {}).items()}
