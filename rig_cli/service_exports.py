"""Immutable service-only exports, portable inventories, and explicit replay selection.

Services own their conversion and profile definitions. Rig never interprets codec settings.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from . import RigError
from .common import eprint, load_yaml, safe_component

KIND = "rig-service-export"
ID = re.compile(r"[0-9a-f]{64}")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def file_sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or "\0" in value or
            Path(value).is_absolute() or any(p in (".", "..") for p in value.split("/")) or
            "//" in value):
        raise RigError(f"service export: invalid relative path {value!r}")
    return Path(value)


def _inside(root, relative):
    root = Path(root).resolve()
    path = root / _relative(relative)
    if not path.resolve().is_relative_to(root) or path.resolve() == root:
        raise RigError(f"service export: path escapes its root: {relative}")
    return path


def _inventory(root, paths):
    result = []
    for relative in sorted(paths):
        p = _inside(root, relative)
        if not p.is_file():
            raise RigError(f"service export: missing file {p}")
        before = p.stat()
        checksum = file_sha256(p)
        after = p.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size, after.st_mtime_ns, after.st_ino):
            raise RigError(f"service export: source changed while hashing: {p}")
        result.append(dict(path=relative, size=after.st_size, sha256=checksum))
    return result


def _verify_files(root, entries):
    for entry in entries:
        p = _inside(root, entry["path"])
        if not p.is_file() or p.stat().st_size != entry["size"]:
            raise RigError(f"service export: missing or changed file: {p}")
        if _inventory(root, [entry["path"]])[0] != entry:
            raise RigError(f"service export: missing or changed file: {p}")


def profile_for(descriptor, name=None):
    es = descriptor.export_source
    if es is None or "service" not in es.modes:
        raise RigError(f"{descriptor.service}: no service export support; update its service bundle")
    name = name or es.default_profile
    if name not in es.profiles:
        raise RigError(f"{descriptor.service}: unknown export profile {name!r}; available: "
                       + ", ".join(sorted(es.profiles)))
    definition = load_yaml(_inside(descriptor.repo, es.profiles[name]))
    _validate_profile(definition, name)
    return dict(id=name, sha256=digest(definition), revision=definition["revision"],
                definition=definition)


def _validate_profile(definition, name):
    if (type(definition.get("version")) is not int or definition["version"] != 1 or
            definition.get("id") != name or
            type(definition.get("revision")) is not int or definition["revision"] < 1 or
            not isinstance(definition.get("format"), str) or not definition["format"] or
            type(definition.get("lossy")) is not bool):
        raise RigError(f"service export: profile {name} needs version: 1, id, positive "
                       "revision, format and lossy boolean")


def _validate_artifact(doc, folder):
    if (type(doc.get("version")) is not int or doc["version"] != 1 or doc.get("kind") != KIND or
            doc.get("complete") is not True or not ID.fullmatch(str(doc.get("export_id", "")))):
        raise RigError(f"service export: incomplete or unsupported manifest: {folder}")
    if digest({k: v for k, v in doc.items() if k != "export_id"}) != doc["export_id"]:
        raise RigError(f"service export: manifest identity mismatch: {folder}")
    safe_component(doc.get("service", ""), what="service export service")
    profile = doc.get("profile")
    if (not isinstance(profile, dict) or not isinstance(profile.get("definition"), dict) or
            digest(profile["definition"]) != profile.get("sha256") or
            profile.get("id") != profile["definition"].get("id") or
            profile.get("revision") != profile["definition"].get("revision")):
        raise RigError(f"service export: invalid profile identity: {folder}")
    safe_component(profile.get("id", ""), what="service export profile")
    _validate_profile(profile["definition"], profile["id"])
    records = doc.get("recordings")
    if not isinstance(records, list) or not records:
        raise RigError(f"service export: no recordings: {folder}")
    names, outputs = set(), set()
    for record in records:
        if not isinstance(record, dict):
            raise RigError("service export: invalid recording entry")
        name = safe_component(record.get("name", ""), what="recorded instance")
        if name in names:
            raise RigError(f"service export: duplicate recording {name}")
        names.add(name)
        source_path = _relative(record.get("source_path"))
        output_path = _relative(record.get("path"))
        sessions = record.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            raise RigError(f"service export: no sessions for {name}")
        prefixes = set()
        for session in sessions:
            if not isinstance(session, dict):
                raise RigError("service export: invalid session entry")
            prefix = safe_component(session.get("id", ""), what="recording session")
            if prefix in prefixes:
                raise RigError(f"service export: duplicate session {prefix}")
            prefixes.add(prefix)
            for field, parent in (("source_files", source_path), ("files", output_path)):
                entries = session.get(field)
                if not isinstance(entries, list) or not entries:
                    raise RigError(f"service export: missing {field} for {prefix}")
                seen = set()
                for entry in entries:
                    if (not isinstance(entry, dict) or type(entry.get("size")) is not int or
                            entry["size"] <= 0 or not ID.fullmatch(str(entry.get("sha256", "")))):
                        raise RigError("service export: invalid file inventory")
                    relative = _relative(entry.get("path"))
                    if not relative.is_relative_to(parent) or relative == parent:
                        raise RigError(f"service export: file outside recording: {relative}")
                    if str(relative) in seen or (field == "files" and str(relative) in outputs):
                        raise RigError(f"service export: duplicate file {relative}")
                    seen.add(str(relative))
                    if field == "files":
                        _inside(folder, str(relative))
                        outputs.add(str(relative))
    if doc.get("validation", {}).get("ok") is not True:
        raise RigError(f"service export: recording validation not successful: {folder}")
    return outputs


def load_artifact(path, *, verify=True, source_root=None):
    """Read/validate an artifact, optionally hash media and its original source evidence.

The directory name is deliberately independent so staged transfers can be verified.
"""
    path = Path(path)
    folder = path.parent if path.name == "export.yaml" else path
    try:
        doc = load_yaml(folder / "export.yaml")
        expected = _validate_artifact(doc, folder)
        if verify:
            for record in doc["recordings"]:
                for session in record["sessions"]:
                    _verify_files(folder, session["files"])
                    if source_root is not None:
                        _verify_files(source_root, session["source_files"])
            actual = {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()}
            if actual != expected | {"export.yaml"}:
                raise RigError(f"service export: unexpected or missing artifact files: {folder}")
        return doc
    except (OSError, TypeError, ValueError, KeyError, AttributeError) as exc:
        raise RigError(f"service export: invalid artifact at {folder}: {exc}") from exc


def committed_artifacts(run_root, service=None, profile=None, *, strict=True):
    base = Path(run_root) / "exports" / "services"
    if not base.is_dir():
        return []
    pattern = f"{service or '*'}/{profile or '*'}/*/export.yaml"
    result = []
    for manifest in sorted(base.glob(pattern)):
        folder = manifest.parent
        if not ID.fullmatch(folder.name):
            continue
        try:
            _inside(run_root, str(manifest.relative_to(run_root)))
            doc = load_artifact(folder, verify=False)
            if (doc["export_id"] != folder.name or doc["service"] != folder.parent.parent.name or
                    doc["profile"]["id"] != folder.parent.name):
                raise RigError(f"service export: manifest does not match artifact location: {folder}")
        except RigError:
            if strict:
                raise
            eprint(f"rig: ignoring invalid unselected service export: {folder}")
            continue
        result.append((folder, doc))
    return result


def _discover(run_root, descriptor, instances):
    es = descriptor.export_source
    parent = _inside(run_root, es.data.rsplit("/{name}", 1)[0])
    wanted = set(instances or [])
    for name in wanted:
        safe_component(name, what="recorded instance")
    records = []
    if parent.is_dir():
        for p in sorted(parent.iterdir()):
            if p.is_dir() and any(p.iterdir()) and (not wanted or p.name in wanted):
                safe_component(p.name, what="recorded instance")
                relative = es.data_path(p.name)
                _inside(run_root, relative)
                records.append(dict(name=p.name, relative=relative, sessions=[]))
    missing = wanted - {r["name"] for r in records}
    if missing or not records:
        raise RigError(f"service export: no recordings for {', '.join(sorted(missing)) or descriptor.service}")
    return records


def _helper(descriptor, request, env, *, preflight=False):
    es = descriptor.export_source
    with tempfile.TemporaryDirectory(prefix="rig-service-export-") as temporary:
        root = Path(temporary)
        req, result = root / "request.json", root / "result.json"
        req.write_text(json.dumps(request))
        command = [str(_inside(descriptor.repo, es.command[0])), *es.command[1:],
                   "--request", str(req), "--result", str(result)]
        if preflight:
            command.append("--preflight")
        try:
            completed = subprocess.run(command, cwd=descriptor.repo, env=env, check=False)
        except OSError as exc:
            raise RigError(f"service export: cannot run {command[0]}: {exc}") from exc
        if completed.returncode:
            raise RigError(f"{descriptor.service}: export helper failed (exit {completed.returncode})")
        try:
            doc = json.loads(result.read_text())
        except (OSError, ValueError) as exc:
            raise RigError(f"service export: helper did not return a valid result: {exc}") from exc
        if (not isinstance(doc, dict) or doc.get("version") != 1 or
                doc.get("profile_sha256") != request["profile"]["sha256"] or
                not isinstance(doc.get("validation"), dict) or doc["validation"].get("ok") is not True):
            raise RigError("service export: incomplete helper result or profile mismatch")
        return doc


def _result_records(result, request, root):
    """Validate the helper's selection, keeping service metadata opaque."""
    records = result.get("recordings")
    wanted = {r["name"]: r["relative"] for r in request["recordings"]}
    if not isinstance(records, list) or len(records) != len(wanted):
        raise RigError("service export: helper returned a different recording selection")
    seen, all_files = set(), set()
    for record in records:
        if (not isinstance(record, dict) or record.get("name") not in wanted or
                record.get("relative") != wanted[record["name"]] or record["name"] in seen):
            raise RigError("service export: helper returned an unexpected recording")
        seen.add(record["name"])
        sessions = record.get("sessions")
        if not isinstance(sessions, list) or not sessions:
            raise RigError("service export: helper returned no sessions")
        ids = set()
        for session in sessions:
            if not isinstance(session, dict):
                raise RigError("service export: invalid helper session")
            prefix = safe_component(session.get("id", ""), what="recording session")
            if prefix in ids:
                raise RigError(f"service export: duplicate helper session {prefix}")
            ids.add(prefix)
            files = session.get("files")
            if not isinstance(files, list) or not files:
                raise RigError("service export: helper returned no recording files")
            for relative in files:
                p = _inside(root, relative)
                if not _relative(relative).is_relative_to(Path(record["relative"])):
                    raise RigError("service export: helper file outside recording")
                if relative in all_files or not p.is_file() or p.stat().st_size == 0:
                    raise RigError(f"service export: missing, empty or duplicate helper file: {relative}")
                all_files.add(relative)
    return records


def cmd(manifest, catalog, descriptors, **options):
    try:
        return _cmd(manifest, catalog, descriptors, **options)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RigError(f"service export: {exc}") from exc


def _cmd(manifest, catalog, descriptors, *, run_ref, service, profile=None, instances=None,
         force=False, dry_run=False):
    from . import dispatch
    from .descriptor import load_descriptor
    from .export import resolve_run
    import platform
    safe_component(service, what="export service")
    if service not in catalog:
        raise RigError(f"service export: {service} is not installed in services.yaml")
    descriptor = descriptors.get(service) or load_descriptor(service, catalog[service].path)
    chosen = profile_for(descriptor, profile)
    _, run_root = resolve_run(manifest, run_ref)
    run_root = run_root.resolve()
    records = _discover(run_root, descriptor, instances)
    namespace = _inside(run_root, "exports/services")
    if (namespace / "manifest.yaml").exists() or (namespace / ".rig/export.yaml").exists():
        raise RigError("service export: exports/services is an existing legacy whole-run profile")
    env = dispatch.fleet_env(manifest, descriptors)
    env["RIG_TARGET_PLATFORM"] = descriptor.replay_platform or "dev"
    if env.get("RIG_IMAGE_TAG") in descriptor.build_platforms:
        env.pop("RIG_IMAGE_TAG")  # legacy hardware-valued tags cannot select a portable image
    architecture = platform.machine().lower()
    env["RIG_TARGET_ARCH"] = {"aarch64": "arm64", "x86_64": "amd64"}.get(architecture, architecture)
    env = dispatch.service_env(env, descriptor)
    request = dict(version=1, source_root=str(run_root), destination=None,
                   profile=chosen, recordings=records)
    report = _helper(descriptor, request, env, preflight=True)
    inspected = _result_records(report, request, run_root)
    inputs = _inventory(run_root, [p for r in inspected for s in r["sessions"] for p in s["files"]])
    request_hash = digest(dict(service=service, profile=chosen, inputs=inputs,
                               producer=report.get("producer", {}),
                               helper=file_sha256(descriptor.repo / descriptor.export_source.command[0])))
    # A reusable artifact does not require the same architecture, local image cache, or
    # image tag. Converter code/profile/source identity defines the requested transformation;
    # exact runtime image identity remains recorded in the generation's provenance.
    reuse_hash = digest(dict(version=1, service=service, profile=chosen, inputs=inputs,
                             implementation=report.get("producer", {}).get("implementation_sha256"),
                             helper=file_sha256(descriptor.repo / descriptor.export_source.command[0])))
    eprint(f"rig run export: {service}/{chosen['id']} revision {chosen['revision']} "
           f"({'lossy' if chosen['definition']['lossy'] else 'lossless'}); "
           f"recordings: {', '.join(r['name'] for r in inspected)}")
    if not force:
        for folder, doc in committed_artifacts(run_root, service, chosen["id"], strict=False):
            if doc.get("reuse_sha256", doc.get("request_sha256")) in (reuse_hash, request_hash):
                load_artifact(folder, source_root=run_root)
                eprint(f"rig run export: reuse {folder}")
                return 0
    destination = _inside(run_root, f"exports/services/{service}/{chosen['id']}")
    dependencies = report.get("missing_dependencies", [])
    if dry_run:
        eprint(f"rig run export: would create {destination}/<export-id>/")
        for dependency in dependencies:
            eprint(f"rig run export: missing dependency: {dependency}")
        return 0
    if dependencies:
        raise RigError("service export: missing dependencies: " + "; ".join(map(str, dependencies)))
    destination.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (destination / f".lock-{request_hash}").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RigError("service export: this conversion is already running") from exc
        # Recheck after obtaining the lock: a prior producer may have just completed.
        if not force:
            for folder, doc in committed_artifacts(run_root, service, chosen["id"], strict=False):
                if doc.get("reuse_sha256", doc.get("request_sha256")) in (reuse_hash, request_hash):
                    load_artifact(folder, source_root=run_root)
                    eprint(f"rig run export: reuse {folder}")
                    return 0
        required = report.get("required_bytes", sum(e["size"] for e in inputs) * 2)
        if not isinstance(required, int) or required < 0:
            raise RigError("service export: invalid required_bytes estimate")
        if shutil.disk_usage(destination).free < required + 16 * 1024 * 1024:
            raise RigError("service export: insufficient free space for conversion")
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=destination) as staging:
            stage = Path(staging)
            request.update(destination=str(stage), recordings=inspected,
                           source_sha256={e["path"]: e["sha256"] for e in inputs})
            result = _helper(descriptor, request, env)
            produced = _result_records(result, request, stage)
            _verify_files(run_root, inputs)
            input_index = {e["path"]: e for e in inputs}
            original_records = {r["name"]: r for r in inspected}
            output_records = []
            for record in produced:
                original = original_records[record["name"]]
                sessions = {s["id"]: s for s in original["sessions"]}
                if set(sessions) != {s["id"] for s in record["sessions"]}:
                    raise RigError("service export: converter changed session selection")
                output_sessions = []
                for session in record["sessions"]:
                    before = sessions[session["id"]]
                    for key in ("frame_count", "start_ns", "end_ns", "width", "height", "pixel_format"):
                        if key in before and before[key] != session.get(key):
                            raise RigError(f"service export: converter changed {key} for {session['id']}")
                    output_sessions.append({**session,
                        "source_files": [input_index[p] for p in before["files"]],
                        "files": _inventory(stage, session["files"])})
                output_records.append(dict(name=record["name"], source_path=original["relative"],
                                           path=record["relative"], sessions=output_sessions))
            doc = dict(version=1, kind=KIND, complete=True, service=service, profile=chosen,
                       producer=result.get("producer", report.get("producer", {})),
                       request_sha256=request_hash, reuse_sha256=reuse_hash, recordings=output_records,
                       validation=result["validation"])
            doc["export_id"] = digest(doc)
            (stage / "export.yaml").write_text(json.dumps(doc, indent=2) + "\n")
            load_artifact(stage, source_root=run_root)
            final = destination / doc["export_id"]
            if final.exists():
                load_artifact(final, source_root=run_root)
            else:
                stage.rename(final)
                # TemporaryDirectory cleanup tolerates its now-absent original path.
            eprint(f"rig run export: created {final}")
    return 0


def resolve_proxy_binding(source_root, binding, *, service, profile=None, export_id=None,
                          descriptor=None):
    """Select exact source-compatible recordings; never silently fall back to originals."""
    if descriptor is None or getattr(descriptor.replay_source, "binding", None) != "direct":
        raise RigError(f"{service}: proxy replay requires direct source binding; update the service bundle")
    chosen = profile_for(descriptor, profile)
    if export_id is not None and not ID.fullmatch(str(export_id)):
        raise RigError("proxy export pin must be a full export ID")
    source_root = Path(source_root).resolve()
    selected_sessions = set(binding.get("sessions", []))
    candidates, rejected = [], []
    if export_id:
        folder = _inside(source_root, f"exports/services/{service}/{chosen['id']}/{export_id}")
        artifacts = [(folder, load_artifact(folder, verify=False))] if folder.is_dir() else []
        if artifacts and (artifacts[0][1]["export_id"] != export_id or
                          artifacts[0][1]["service"] != service or
                          artifacts[0][1]["profile"]["id"] != chosen["id"]):
            raise RigError("proxy pin does not match its service/profile location")
    else:
        artifacts = committed_artifacts(source_root, service, chosen["id"], strict=False)
    for folder, doc in artifacts:
        same_profile = doc["profile"]["sha256"] == chosen["sha256"]
        compatible = (doc["profile"]["definition"].get("format") == chosen["definition"]["format"])
        if not same_profile and not (export_id and compatible):
            continue
        for record in doc["recordings"]:
            if record["name"] != binding["recorded_as"]:
                continue
            sessions = [s for s in record["sessions"] if not selected_sessions or s["id"] in selected_sessions]
            if not sessions or (selected_sessions and {s["id"] for s in sessions} != selected_sessions):
                continue
            try:
                load_artifact(folder, verify=True)
                actual_inputs = set()
                for session in sessions:
                    for entry in session["source_files"]:
                        relative = str(Path(entry["path"]).relative_to(record["source_path"]))
                        _verify_files(binding["path"], [{**entry, "path": relative}])
                        actual_inputs.add(str(_inside(binding["path"], relative)))
                if binding.get("files") and actual_inputs != {str(Path(p).resolve()) for p in binding["files"]}:
                    raise RigError("source recording file selection changed")
            except RigError as exc:
                rejected.append(str(exc))
                continue
            candidates.append((folder, doc, record, sessions))
    if not candidates:
        detail = f" ({rejected[0]})" if rejected else ""
        raise RigError(f"{binding['recorded_as']}: no compatible proxy{detail}; generate with "
                       f"rig run export {source_root} --service {service} --profile {chosen['id']}"
                       + (f"; requested export {export_id}" if export_id else ""))
    if len(candidates) != 1:
        ids = ", ".join(doc["export_id"] for _, doc, _, _ in candidates)
        raise RigError(f"{binding['recorded_as']}: multiple compatible proxy exports; pin one in "
                       f"recipe proxies.instances: {ids}")
    folder, doc, record, sessions = candidates[0]
    path = _inside(folder, record["path"])
    from .replay_sources import inventory_fingerprint
    return {**copy.deepcopy(binding), "original": copy.deepcopy(binding), "path": str(path),
            "files": [str(_inside(folder, f["path"])) for s in sessions for f in s["files"]],
            "sessions": [s["id"] for s in sessions], "fingerprint": inventory_fingerprint(path),
            "adapted": False, "proxy": dict(export_id=doc["export_id"],
                profile=doc["profile"]["id"], profile_hash=doc["profile"]["sha256"],
                manifest=str(folder / "export.yaml"), manifest_sha256=file_sha256(folder / "export.yaml"),
                lossy=doc["profile"]["definition"].get("lossy", True),
                source_files=[f for s in sessions for f in s["source_files"]])}
