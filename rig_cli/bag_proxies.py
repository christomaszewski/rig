"""Immutable, many-input bag proxies with explicit policy and exact source evidence.

Rig owns selection/publication; the bag-player provider owns ROS decoding and
conversion. Unlike a video export, this artifact describes an entire input set.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from . import RigError
from .common import eprint, load_yaml
from . import service_exports as artifacts

KIND = "rig-bag-proxy"
FORMAT = "rosbag2-proxy/v1"
POLICY_KEYS = {"time_source", "deduplication", "sensor_timestamp_field", "timestamp_fallback",
               "topic_timestamp_fields", "max_clock_skew_s", "dedup_window_ms"}
CONFIG_KEYS = POLICY_KEYS - {"deduplication"}


class MissingProxy(RigError):
    pass


def _policy(value):
    if not isinstance(value, dict) or set(value) != POLICY_KEYS:
        raise RigError("bag proxy: policy must explicitly specify timing, fallback, and deduplication settings")
    if value["time_source"] not in ("received", "sent", "sensor"):
        raise RigError("bag proxy: unsupported time_source")
    if value["deduplication"] not in ("off", "conservative", "aggressive"):
        raise RigError("bag proxy: unsupported deduplication policy")
    if value["timestamp_fallback"] not in ("error", "received", "sent"):
        raise RigError("bag proxy: unsupported timestamp fallback")
    fields = value["topic_timestamp_fields"]
    if not isinstance(fields, dict) or any(not isinstance(k, str) or not k.startswith("/") for k in fields):
        raise RigError("bag proxy: topic_timestamp_fields requires absolute topics")
    for field in [value["sensor_timestamp_field"], *fields.values()]:
        if not isinstance(field, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*", field):
            raise RigError("bag proxy: invalid sensor timestamp field")
    for key in ("max_clock_skew_s", "dedup_window_ms"):
        if type(value[key]) not in (int, float) or not math.isfinite(value[key]) or value[key] < 0:
            raise RigError(f"bag proxy: {key} must be finite and nonnegative")
    return value


def _config(row, root=None):
    if row is None:
        return {}
    from .resolve import resolved_dict
    return resolved_dict(row, Path(root) if root else None).get("play", {})


def profile_for(descriptor, name, row=None, root=None):
    if descriptor is None or "replay-export-bags" not in descriptor.verbs:
        raise RigError("bag proxies require a bag-player provider with replay-export-bags support")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise RigError("bag proxy: invalid profile name")
    path = artifacts._inside(descriptor.repo, f"export-profiles/{name}.yaml")
    if not path.is_file():
        raise RigError(f"bag proxy: unknown profile {name!r} in {descriptor.repo / 'export-profiles'}")
    definition = load_yaml(path)
    artifacts._validate_profile(definition, name)
    if definition["format"] != FORMAT:
        raise RigError("bag proxy: unsupported profile format")
    selected = dict(_policy(definition.get("policy")))
    config = _config(row, root)
    if "time_source" in config and config["time_source"] != selected["time_source"]:
        raise RigError(f"bag proxy profile {name} time_source conflicts with the bag-player configuration")
    selected.update({key: config[key] for key in CONFIG_KEYS if key in config})
    definition = {**definition, "policy": _policy(selected)}
    return dict(id=name, sha256=artifacts.digest(definition), revision=definition["revision"],
                definition=definition)


def _originals(source):
    return source.get("original_bags", source.get("bags", [source["bag"]] if source.get("bag") else []))


def _inputs(source, verification=None):
    root = Path(source["root"]).resolve()
    result = []
    for bag in _originals(source):
        if bag.get("repair"):
            raise RigError("bag proxy: valid metadata is required for every original session; "
                           "import/reindex an independent copy before exporting")
        path = Path(bag["path"]).resolve()
        if not path.is_relative_to(root):
            raise RigError("bag proxy: original bag must be inside the source run")
        files = [Path(p).resolve() for p in bag["files"]] + [path / "metadata.yaml"]
        if any(not p.is_relative_to(root) for p in files):
            raise RigError("bag proxy: input file escapes the source run")
        result.append(dict(logger=bag["logger"], session=bag["session"],
                           path=str(path.relative_to(root)), storage=bag["storage"],
                           files=artifacts._inventory(root, [str(p.relative_to(root)) for p in files],
                                                      verification=verification)))
    if not result:
        raise RigError("bag proxy: no bag sessions selected")
    return sorted(result, key=lambda b: (b["logger"], b["session"], b["path"]))


def load_artifact(folder, *, source_root=None, verify=True, verification=None):
    folder = Path(folder)
    try:
        before = artifacts._file_identity(folder / "export.yaml")
        result = _load_artifact(folder, source_root=source_root, verify=verify, verification=verification)
        if artifacts._file_identity(folder / "export.yaml") != before:
            raise RigError("bag proxy: manifest changed during verification")
        return result
    except (OSError, TypeError, ValueError, KeyError, AttributeError) as exc:
        raise RigError(f"bag proxy: invalid artifact at {folder}: {exc}") from exc


def _file_entries(entries):
    if not isinstance(entries, list) or not entries:
        raise RigError("bag proxy: missing file inventory")
    seen = set()
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}
                or type(entry["size"]) is not int or entry["size"] < 0
                or not artifacts.ID.fullmatch(str(entry["sha256"]))):
            raise RigError("bag proxy: invalid file inventory entry")
        path = str(artifacts._relative(entry["path"]))
        if path in seen:
            raise RigError("bag proxy: duplicate file inventory entry")
        seen.add(path)
    return seen


def _load_artifact(folder, *, source_root=None, verify=True, verification=None):
    folder = Path(folder)
    doc = load_yaml(folder / "export.yaml")
    if (doc.get("version") != 1 or doc.get("kind") != KIND or doc.get("complete") is not True
            or not artifacts.ID.fullmatch(str(doc.get("export_id", "")))
            or artifacts.digest({k: v for k, v in doc.items() if k != "export_id"}) != doc["export_id"]):
        raise RigError(f"bag proxy: incomplete or invalid manifest: {folder}")
    chosen = doc.get("profile", {})
    definition = chosen.get("definition", {})
    if (definition.get("format") != FORMAT or artifacts.digest(definition) != chosen.get("sha256")
            or chosen.get("id") != definition.get("id")
            or chosen.get("revision") != definition.get("revision")):
        raise RigError("bag proxy: invalid profile identity")
    artifacts._validate_profile(definition, chosen["id"])
    _policy(definition.get("policy"))
    if (not isinstance(doc.get("inputs"), list) or not doc["inputs"] or
            not isinstance(doc.get("files"), list) or not doc["files"] or
            doc.get("validation", {}).get("ok") is not True or doc.get("bag") != "bag"):
        raise RigError("bag proxy: incomplete input/output evidence")
    outputs = _file_entries(doc["files"])
    if not {"bag/metadata.yaml", "messages.jsonl", "report.json"} <= outputs:
        raise RigError("bag proxy: output inventory lacks bag metadata or provenance")
    input_paths = set()
    for item in doc["inputs"]:
        if (not isinstance(item, dict) or set(item) != {"logger", "session", "path", "storage", "files"}
                or not all(isinstance(item[key], str) and item[key] for key in ("logger", "session", "path"))
                or item["storage"] not in ("mcap", "sqlite3")):
            raise RigError("bag proxy: invalid original input")
        path = Path(item["path"])
        if path.is_absolute() or ".." in path.parts or item["path"] in input_paths:
            raise RigError("bag proxy: escaped or repeated original input")
        input_paths.add(item["path"])
        _file_entries(item["files"])
    if verify:
        artifacts._verify_files(folder, doc["files"], verification=verification)
        actual = {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()}
        if actual != outputs | {"export.yaml"}:
            raise RigError("bag proxy: unexpected or missing artifact files")
        if source_root is not None:
            for item in doc["inputs"]:
                artifacts._verify_files(source_root, item["files"], verification=verification)
    return doc


def committed_artifacts(root, profile=None):
    base = artifacts._inside(root, "exports/inputs/bags")
    matches = base.glob(f"{profile or '*'}/*/export.yaml")
    for manifest in sorted(matches):
        folder = manifest.parent
        if not artifacts.ID.fullmatch(folder.name):
            continue
        artifacts._inside(root, str(folder.relative_to(Path(root).resolve())))
        try:
            doc = load_artifact(folder, verify=False)
            if doc["export_id"] != folder.name or doc["profile"]["id"] != folder.parent.name:
                raise RigError("bag proxy: artifact directory does not match manifest")
        except (RigError, KeyError, TypeError) as exc:
            eprint(f"rig: ignoring invalid unselected bag proxy {folder}: {exc}")
            continue
        yield folder, doc


def _binding(folder, doc):
    return dict(export_id=doc["export_id"], profile=doc["profile"], path=str(folder), manifest=doc)


def verify_binding(binding, source_root, verification=None):
    root = Path(source_root).resolve()
    if root.is_file():
        root = root.parent
    folder = Path(binding["path"]).resolve()
    if not folder.is_relative_to(root / "exports" / "inputs" / "bags"):
        raise RigError("bag proxy: binding escapes the source export directory")
    doc = load_artifact(folder, source_root=root, verification=verification)
    if doc["export_id"] != binding["export_id"] or doc["profile"] != binding["profile"]:
        raise RigError("bag proxy: selected artifact identity changed")
    return doc


def resolve(source, selector, row, descriptor, *, pin=None, root=None, verification=None):
    source_root = Path(source["root"]).resolve()
    selected_inputs = _inputs(source, verification)
    profile = profile_for(descriptor, selector["profile"], row, root) if "profile" in selector else None
    if profile and pin and pin.get("profile", {}).get("sha256") != profile["sha256"]:
        eprint("rig replay: bag profile policy changed; resolving the requested policy again")
        pin = None
    requested_id = selector.get("proxy") or (pin or {}).get("export_id")
    if requested_id and not artifacts.ID.fullmatch(str(requested_id)):
        raise RigError("bag proxy pin must be a full 64-character artifact ID")
    candidates = list(committed_artifacts(source_root, profile["id"] if profile else None))
    if requested_id:
        candidates = [(folder, doc) for folder, doc in candidates if doc["export_id"] == requested_id]
    compatible = []
    implementation = Path(descriptor.repo) / "tools/bag_proxy.py" if descriptor else None
    implementation_sha = (artifacts.file_sha256(implementation)
                          if implementation and implementation.is_file() else None)
    for folder, doc in candidates:
        if doc["inputs"] != selected_inputs:
            continue
        if profile and doc["profile"]["sha256"] != profile["sha256"]:
            continue
        if (not requested_id and implementation_sha and
                doc.get("producer", {}).get("implementation_sha256") != implementation_sha):
            continue
        config = _config(row, root)
        settings = doc["profile"]["definition"]["policy"]
        for key in CONFIG_KEYS:
            if key in config and config[key] != settings[key]:
                raise RigError(f"bag proxy {doc['export_id']}: {key} conflicts with the player configuration")
        load_artifact(folder, source_root=source_root, verification=verification)
        compatible.append(_binding(folder, doc))
    if len(compatible) > 1:
        raise RigError("Multiple compatible bag proxies; choose inputs.bags.proxy: " +
                       ", ".join(b["export_id"] for b in compatible))
    if not compatible:
        if requested_id:
            raise RigError(f"Pinned bag proxy {requested_id} is missing, stale, or incompatible with the exact selected inputs")
        raise MissingProxy("No compatible bag proxy for the selected sessions and policy; "
                           "run rig run export RUN --recipe NAME")
    return compatible[0]


def apply_selection(source, selector, player_row, descriptor, options, verification=None):
    root = options.get("_deployment_root")
    if selector == {"use": "original"}:
        if _config(player_row, root).get("time_source", "received") != "received":
            raise RigError("Original bags replay in received-time order; select a sent/sensor bag proxy profile")
        return source
    try:
        binding = resolve(source, selector, player_row, descriptor,
                          pin=options.get("_prepared_bag_proxy"), root=root, verification=verification)
    except MissingProxy:
        if options.get("_export_inputs"):
            return source
        raise
    from .replay_sources import aggregate_bags, metadata
    path = Path(binding["path"]) / "bag"
    settings = binding["profile"]["definition"]["policy"]
    bag = dict(logger="bag-proxy", session=binding["export_id"], path=str(path),
               relative_path=str(path.relative_to(Path(source["root"]).resolve())),
               files=[str(path / p) for p in load_yaml(path / "metadata.yaml")[
                   "rosbag2_bagfile_information"]["relative_file_paths"]], repair=False, requires_copy=False,
               proxy={"export_id": binding["export_id"], "time_source": settings["time_source"],
                      "deduplication": settings["deduplication"]}, **metadata(path))
    return {**source, "original_bags": _originals(source), "bags": [bag], "bag": aggregate_bags([bag]),
            "bag_proxy": binding}


def build(source, selector, player_row, descriptor, manifest, descriptors, *, dry_run=False,
          force=False, verification=None, root=None):
    if selector == {"use": "original"}:
        return None
    try:
        previous = resolve(source, selector, player_row, descriptor, root=root, verification=verification)
    except MissingProxy:
        previous = None
    if previous is not None and (not force or "proxy" in selector):
        eprint(f"rig run export: reuse bag proxy {previous['export_id']}")
        return previous
    chosen = profile_for(descriptor, selector["profile"], player_row, root)
    source_root = Path(source["root"]).resolve()
    inputs = _inputs(source, verification)
    parent = artifacts._inside(source_root, f"exports/inputs/bags/{chosen['id']}")
    if dry_run:
        eprint(f"rig run export: would build {chosen['id']} from {len(inputs)} bag sessions at {parent}")
        return None
    parent.mkdir(parents=True, exist_ok=True)
    request_sha = artifacts.digest(dict(inputs=inputs, profile=chosen))
    import fcntl
    from . import dispatch
    env = dispatch.instance_env(dispatch.fleet_env(manifest, descriptors), player_row, descriptor)
    with (parent / (".lock-" + request_sha)).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RigError("bag proxy: this conversion is already running") from exc
        if not force:
            try:
                return resolve(source, selector, player_row, descriptor, root=root, verification=verification)
            except MissingProxy:
                pass
        size = sum(f["size"] for b in inputs for f in b["files"])
        if shutil.disk_usage(parent).free < size * 3 + 16 * 1024 * 1024:
            raise RigError("bag proxy: insufficient disk space for merge index, output, and provenance")
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=parent) as temporary:
            stage = Path(temporary)
            request = dict(version=1, source_root=str(source_root), destination=str(stage),
                           result=str(stage / ".result.json"), policy=chosen["definition"]["policy"],
                           bags=[{**b, "path": str(source_root / b["path"])} for b in inputs])
            request_path = stage / ".request.json"
            request_path.write_text(json.dumps(request))
            implementation = Path(descriptor.repo) / "tools/bag_proxy.py"
            implementation_sha = artifacts.file_sha256(implementation) if implementation.is_file() else None
            eprint(f"rig run export: building {chosen['id']} from {len(inputs)} bag sessions")
            command = [str(descriptor.repo / descriptor.launcher), str(player_row.config),
                       *descriptor.verb_args("replay-export-bags"), str(request_path)]
            completed = subprocess.run(command, cwd=descriptor.repo, env=env)
            if completed.returncode:
                raise RigError(f"bag proxy: converter failed (exit {completed.returncode}); originals unchanged")
            result = json.loads((stage / ".result.json").read_text())
            if (result.get("version") != 1 or result.get("validation", {}).get("ok") is not True
                    or result.get("policy") != request["policy"]):
                raise RigError("bag proxy: invalid converter validation or policy result")
            if implementation_sha and (result.get("producer", {}).get("implementation_sha256") != implementation_sha
                    or artifacts.file_sha256(implementation) != implementation_sha):
                raise RigError("bag proxy: converter changed during export; retry with a stable provider")
            from .replay_sources import metadata
            metadata(stage / "bag")
            if _inputs(source, verification) != inputs:
                raise RigError("bag proxy: original inputs changed during conversion")
            request_path.unlink()
            (stage / ".result.json").unlink()
            (stage / "report.json").write_text(json.dumps(result, indent=2) + "\n")
            files = [str(p.relative_to(stage)) for p in stage.rglob("*") if p.is_file()]
            doc = dict(version=1, kind=KIND, complete=True, bag="bag", profile=chosen, inputs=inputs,
                       producer=result.get("producer", {}), validation=result["validation"],
                       statistics=result.get("statistics", {}), files=artifacts._inventory(stage, files))
            doc["export_id"] = artifacts.digest(doc)
            (stage / "export.yaml").write_text(json.dumps(doc, indent=2) + "\n")
            load_artifact(stage, source_root=source_root, verification=verification)
            final = parent / doc["export_id"]
            if final.exists():
                load_artifact(final, source_root=source_root, verification=verification)
            else:
                stage.rename(final)
            stats = doc["statistics"]
            eprint(f"rig run export: created bag proxy {doc['export_id']}; "
                   f"messages {stats.get('input_messages', '?')} -> {stats.get('output_messages', '?')}, "
                   f"proven duplicates {stats.get('proven_duplicates', 0)}, "
                   f"likely duplicates {stats.get('likely_duplicates', 0)}, "
                   f"ambiguous preserved {stats.get('ambiguous_preserved', 0)}; "
                   f"report: {final / 'report.json'}")
            return _binding(final, doc)
