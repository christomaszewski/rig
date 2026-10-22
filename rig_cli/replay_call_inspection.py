"""Read-only service-event evidence, cached against the selected bag and helper.

Payload cache identity uses resolved path, device, inode, size, nanosecond mtime and
ctime; metadata is hashed in full. This detects ordinary writes, replacement and
same-size edits with restored mtime without rescanning multi-gigabyte bag contents
on every check. It is a local filesystem cache, not a cryptographic media-integrity
claim. Symlinks and paths outside the session are rejected. Identities are checked
again after reading cached evidence or running the bounded, read-only helper.
"""
from __future__ import annotations

from dataclasses import replace
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

import yaml

from . import RigError, dispatch
from .common import eprint, load_yaml
from .descriptor import find_descriptor
from .replay_recipe import digest

HELPER_TIMEOUT = 120
_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NAME = re.compile(r"(?:/[A-Za-z_][A-Za-z0-9_]*)+\Z")
_STATUSES = {"usable", "metadata_only", "response_only", "other_origin", "empty_window",
             "unsupported_type", "missing"}


def validate_request(request):
    expected = {"version", "session", "start_ns", "from_s", "to_s", "services", "source_mode"}
    if (not isinstance(request, dict) or set(request) != expected or
            type(request.get("version")) is not int or request["version"] != 1):
        raise RigError("Call inspection needs a version: 1 request with session, window and service targets")
    session = request["session"]
    if not isinstance(session, str) or not session or "\0" in session or not Path(session).is_absolute():
        raise RigError("Call inspection session must be an absolute bag directory")
    if type(request["start_ns"]) is not int or request["start_ns"] < 0:
        raise RigError("Call inspection start_ns must be a nonnegative integer")
    for name in ("from_s", "to_s"):
        value = request[name]
        if name == "to_s" and value is None:
            continue
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite or value < 0:
            raise RigError(f"Call inspection {name} must be finite and nonnegative")
    if request["to_s"] is not None and request["to_s"] <= request["from_s"]:
        raise RigError("Call inspection window must end after it starts")
    names = request["services"]
    if (not isinstance(names, list) or not names or
            any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in names) or
            len(set(names)) != len(names)):
        raise RigError("Call inspection services must be unique absolute ROS service names")
    if request["source_mode"] not in ("service", "client"):
        raise RigError("Call inspection source_mode must be service or client")
    return dict(request, services=sorted(names))


def validate_response(response, request):
    if (not isinstance(response, dict) or set(response) != {"version", "source_mode", "services"} or
            type(response.get("version")) is not int or response["version"] != 1 or
            response["source_mode"] != request["source_mode"] or
            not isinstance(response["services"], dict) or
            set(response["services"]) != set(request["services"])):
        raise RigError("Call inspection returned an invalid response or incomplete service targets")
    fields = {"status", "request_count", "other_origin_request_count", "duplicate_count", "type", "reason"}
    for name, entry in response["services"].items():
        if (not isinstance(entry, dict) or set(entry) != fields or
                not isinstance(entry["status"], str) or entry["status"] not in _STATUSES):
            raise RigError(f"Call inspection returned an invalid status for {name}")
        for field in ("request_count", "other_origin_request_count", "duplicate_count"):
            if type(entry[field]) is not int or entry[field] < 0:
                raise RigError(f"Call inspection returned an invalid {field} for {name}")
        if ((entry["status"] == "usable") != (entry["request_count"] > 0) or
                not isinstance(entry["reason"], str) or not entry["reason"].strip() or
                (entry["type"] is not None and
                 (not isinstance(entry["type"], str) or not entry["type"].strip())) or
                (entry["status"] == "usable" and not entry["type"])):
            raise RigError(f"Call inspection returned inconsistent request evidence for {name}")
    return response


def _file_identity(path):
    value = path.stat()
    if not stat.S_ISREG(value.st_mode):
        raise RigError(f"Call inspection requires regular files: {path}")
    return [str(path.resolve()), value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns]


def _session_identity(session):
    session = Path(session)
    if session.is_symlink() or not session.is_dir():
        raise RigError(f"Call inspection session must be a real bag directory: {session}")
    session = session.resolve()
    paths = []
    for folder, dirs, files in os.walk(session, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(folder) / name
            if path.is_symlink() or not path.resolve().is_relative_to(session):
                raise RigError(f"Call inspection rejects source symlinks or escaped paths: {path}")
        paths.extend(Path(folder) / name for name in files)
    metadata = session / "metadata.yaml"
    if metadata not in paths:
        raise RigError(f"Call inspection requires indexed bag metadata: {metadata}")
    before = _file_identity(metadata)
    content = metadata.read_bytes()
    if before != _file_identity(metadata):
        raise RigError("Call inspection source changed while reading bag metadata")
    try:
        doc = yaml.safe_load(content)
        info = doc.get("rosbag2_bagfile_information") if isinstance(doc, dict) else None
        files = info.get("relative_file_paths") if isinstance(info, dict) else None
        if not isinstance(files, list) or not files or len(set(files)) != len(files):
            raise ValueError("expected unique relative_file_paths")
        for relative in files:
            if (not isinstance(relative, str) or not relative or "\\" in relative or "\0" in relative or
                    Path(relative).is_absolute() or any(p in (".", "..", "") for p in relative.split("/"))):
                raise ValueError("invalid relative_file_paths entry")
            path = session / relative
            if not path.resolve().is_relative_to(session) or path not in paths or path.suffix not in (".mcap", ".db3"):
                raise ValueError(f"invalid bag payload {relative}")
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise RigError(f"Call inspection has invalid bag metadata: {exc}") from exc
    return dict(session=str(session), metadata=digest(content.hex()),
                files={str(path.relative_to(session)): _file_identity(path) for path in sorted(paths)})


def _helper_identity(desc):
    descriptor = find_descriptor(desc.repo)
    if descriptor is None:
        raise RigError("Call inspection needs the player's service descriptor")
    raw = load_yaml(descriptor)
    files = {desc.launcher_path, descriptor}
    for pattern in raw.get("launch_surface", []):
        files.update(path for path in desc.repo.glob(pattern) if path.is_file())
    result = {}
    for path in sorted(files):
        before = _file_identity(path)
        checksum = digest(path.read_bytes().hex())
        if before != _file_identity(path):
            raise RigError(f"Call inspection helper changed while reading: {path}")
        result[str(path)] = checksum
    return result


def _pins(pins):
    if (not isinstance(pins, dict) or set(pins) != {"version", "images"} or
            type(pins.get("version")) is not int or pins["version"] != 1 or
            not isinstance(pins.get("images"), dict) or not pins["images"] or
            any(not isinstance(ref, str) or not ref or not isinstance(value, str) or not _ID.fullmatch(value)
                for ref, value in pins["images"].items())):
        raise RigError("Call inspection requires locally verified player image pins")
    return pins


def _read_cache(path, identity, request):
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text())
        if (not isinstance(doc, dict) or set(doc) != {"version", "identity", "result", "sha256"} or
                type(doc.get("version")) is not int or doc["version"] != 1 or doc["identity"] != identity or
                doc["sha256"] != digest({key: value for key, value in doc.items() if key != "sha256"})):
            raise ValueError("cache identity or checksum mismatch")
        return validate_response(doc["result"], request)
    except (OSError, ValueError, RigError) as exc:
        raise RigError(f"Call inspection cache is corrupt: {path}; remove it and inspect again: {exc}") from exc


def _write_cache(path, identity, result):
    doc = dict(version=1, identity=identity, result=result)
    doc["sha256"] = digest(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", prefix=".inspection-", suffix=".json",
                                          dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(doc, stream, sort_keys=True)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def inspect_calls(root, row, desc, config, request, *, env, pins, expected_surfaces=None,
                  execute=False, verification=None):
    """Read existing evidence, optionally creating it with a pinned, read-only helper.

    `pins` is this player's versioned pin document. `env` is the replay fleet env,
    including its verified native architecture. `expected_surfaces`, when supplied,
    is this service's launch_surfaces mapping from the frozen plan. `verification`
    is accepted for the invocation API; this metadata/stat cache does not hash media.
    Check and dry-run must pass execute=False: cache misses never launch or write.
    """
    request = validate_request(request)
    pins = _pins(pins)
    if "replay-inspect-calls" not in desc.verbs:
        raise RigError(f"{row.name}: upgrade the player to declare verbs.replay-inspect-calls")
    if not isinstance(config, dict):
        raise RigError("Call inspection requires the player's resolved configuration")
    env = dict(env)
    env.pop("RIG_RUNTIME_IMAGE_PINS", None)
    env["RIG_RUNTIME_IMAGE_MAP"] = json.dumps({row.name: pins})
    effective_env = dispatch.instance_env(env, row, desc)
    def identity():
        helper = _helper_identity(desc)
        if expected_surfaces is not None and helper != expected_surfaces:
            raise RigError("Call inspection helper changed since planning; plan again")
        selectors = ("BAG_PLAYER_IMAGE", "RIG_MSGS_IMAGE", "RIG_BASE_IMAGE", "RIG_IMAGE_REGISTRY",
                     "RIG_IMAGE_TAG", "RIG_IMAGE_PINS", "DOCKER_DEFAULT_PLATFORM")
        return dict(version=1, request=request, source=_session_identity(request["session"]),
                    helper=helper, config=digest(config), pins=pins,
                    environment=digest({name: effective_env.get(name) for name in selectors}),
                    architecture=effective_env.get("RIG_TARGET_ARCH"),
                    platform=effective_env.get("RIG_TARGET_PLATFORM"))
    initial = identity()
    key = digest(initial)
    path = Path(root) / "var/replay/call-inspections" / (key + ".json")
    result = _read_cache(path, initial, request)
    if result is None and execute:
        eprint("rig replay: inspecting recorded request payloads for " + ", ".join(request["services"]))
        with tempfile.TemporaryDirectory(prefix="rig-call-inspection-") as temporary:
            folder = Path(temporary)
            config_path, request_path = folder / "config.yaml", folder / "request.json"
            config_path.write_text(yaml.safe_dump(config, sort_keys=False))
            request_path.write_text(json.dumps(request))
            prepared = replace(row, config=config_path)
            command = dispatch.launcher_cmd(prepared, desc, "replay-inspect-calls", [str(request_path)])
            try:
                proc = subprocess.run(command, cwd=desc.repo, env=effective_env,
                                      capture_output=True, text=True, timeout=HELPER_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RigError(f"Call inspection helper failed: {exc}") from exc
            if proc.returncode:
                raise RigError("Call inspection helper failed: " +
                               ((proc.stderr or "").strip()[:1000] or f"exit {proc.returncode}"))
            try:
                result = validate_response(json.loads(proc.stdout), request)
            except (TypeError, ValueError) as exc:
                raise RigError(f"Call inspection helper returned invalid JSON: {exc}") from exc
        if identity() != initial:
            raise RigError("Call inspection source or helper changed during inspection; retry with stable inputs")
        _write_cache(path, initial, result)
    elif identity() != initial:
        raise RigError("Call inspection source or helper changed while checking cached evidence")
    return dict(status="ready" if result is not None else "not_checked", cache_key=key,
                request=request, result=result, cache_path=str(path))
