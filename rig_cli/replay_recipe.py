"""Portable replay recipes. Host bindings and generated plans live separately."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import RigError
from .common import load_yaml


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     separators=(",", ":")).encode()).hexdigest()


def mapping(value, allowed, where):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise RigError(f"{where}: expected a mapping with only {', '.join(sorted(allowed))}")
    return value


def names(value, where):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise RigError(f"{where}: expected a list of nonempty names")
    return value


def recorded_namespaces(doc):
    """Recorded Rig identity -> explicitly bound ROS node namespace."""
    result, owners = {}, {}
    for name, entry in doc.get("instances", {}).items():
        namespace = entry.get("recorded_namespace")
        if namespace is None:
            continue
        identity = entry.get("recorded_as", name)
        if identity in result and result[identity] != namespace:
            raise RigError(f"recipe.instances: conflicting recorded_namespace for {identity}")
        if namespace in owners and owners[namespace] != identity:
            raise RigError(f"recipe.instances: recorded namespace {namespace} belongs to both "
                           f"{owners[namespace]} and {identity}; use one recorded_as identity")
        result[identity], owners[namespace] = namespace, identity
    return result


def validate(doc):
    mapping(doc, {"version", "source", "bag", "instances", "recordings", "playback", "proxies"}, "recipe")
    if type(doc.get("version")) is not int or doc["version"] != 1:
        raise RigError("recipe: unsupported version (expected version: 1)")
    if "source" in doc and (not isinstance(doc["source"], str) or not doc["source"]):
        raise RigError("recipe.source: expected a run id or path")
    bag = mapping(doc.get("bag", {}), {"logger", "session"}, "recipe.bag")
    for key, value in bag.items():
        if not isinstance(value, str) or not value or "/" in value or value in (".", ".."):
            raise RigError(f"recipe.bag.{key}: expected a single name")
    for kind in ("instances", "recordings"):
        values = doc.get(kind, {})
        if not isinstance(values, dict):
            raise RigError(f"recipe.{kind}: expected an instance mapping")
        for name, value in values.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise RigError(f"recipe.{kind}: invalid instance name {name!r}")
            allowed = {"recorded_as", "recorded_namespace"} if kind == "instances" else {"from", "sessions"}
            mapping(value, allowed, f"recipe.{kind}.{name}")
            key = "recorded_as" if kind == "instances" else "from"
            if not (kind == "instances" and key not in value and "recorded_namespace" in value) and (
                    not isinstance(value.get(key), str) or not value[key]):
                raise RigError(f"recipe.{kind}.{name}.{key}: required nonempty string")
            if kind == "instances" and key in value and not re.fullmatch(r"[A-Za-z0-9_-]+", value[key]):
                raise RigError(f"recipe.instances.{name}: invalid recorded_as")
            if "recorded_namespace" in value and (not isinstance(value["recorded_namespace"], str) or
                    not re.fullmatch(r"(?:/[A-Za-z_][A-Za-z0-9_]*)+", value["recorded_namespace"])):
                raise RigError(f"recipe.instances.{name}.recorded_namespace: expected an absolute, "
                               "non-root ROS namespace, e.g. /robot/controller")
            if "sessions" in value:
                for session in names(value["sessions"], f"recipe.recordings.{name}.sessions"):
                    if "/" in session or session in (".", ".."):
                        raise RigError(f"recipe.recordings.{name}: invalid session {session!r}")
    recorded_namespaces(doc)
    proxies = mapping(doc.get("proxies", {}), {"services", "instances"}, "recipe.proxies")
    for kind, allowed in (("services", {"profile"}), ("instances", {"export", "use"})):
        values = proxies.get(kind, {})
        if not isinstance(values, dict):
            raise RigError(f"recipe.proxies.{kind}: expected a name mapping")
        for name, value in values.items():
            where = f"recipe.proxies.{kind}.{name}"
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise RigError(f"recipe.proxies.{kind}: invalid name {name!r}")
            mapping(value, allowed, where)
            if kind == "services":
                if not isinstance(value.get("profile"), str) or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.-]*", value["profile"]):
                    raise RigError(f"{where}.profile: required profile name")
            elif set(value) == {"use"} and value["use"] == "original":
                continue
            elif set(value) != {"export"} or not isinstance(value["export"], str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]*", value["export"]):
                raise RigError(f"{where}: choose either export: ID or use: original")
    play = mapping(doc.get("playback", {}), {"topics", "exclude", "keep", "remap",
                                          "services", "calls"}, "recipe.playback")
    if play.get("topics", "all") != "all":
        names(play["topics"], "recipe.playback.topics")
    for key in ("exclude", "keep", "services"):
        names(play.get(key, []), f"recipe.playback.{key}")
    if set(play.get("exclude", [])) & set(play.get("keep", [])):
        raise RigError("recipe.playback: a topic cannot be both kept and excluded")
    remap = play.get("remap", {})
    if not isinstance(remap, dict):
        raise RigError("recipe.playback.remap: expected a topic mapping")
    topic_names = [*play.get("exclude", []), *play.get("keep", []),
                   *play.get("services", []), *remap.keys(), *remap.values()]
    if isinstance(play.get("topics"), list):
        topic_names += play["topics"]
    if any(not isinstance(n, str) or not re.fullmatch(r"/[A-Za-z0-9_/]+", n)
           or "//" in n or n.endswith("/") for n in topic_names):
        raise RigError("recipe.playback: use exact absolute ROS names")
    if len(set(remap.values())) != len(remap):
        raise RigError("recipe.playback.remap: multiple inputs map to the same destination")
    if any(n in ("/clock", "/rosout", "/parameter_events") or n.endswith("/_service_event")
           for n in [*remap.keys(), *remap.values()]):
        raise RigError("recipe.playback.remap: cannot remap clock or plumbing topics")
    if "calls" in play and (not isinstance(play["calls"], str) or not play["calls"]):
        raise RigError("recipe.playback.calls: expected a script path")
    if play.get("calls") and play.get("services"):
        raise RigError("recipe.playback: calls and services are mutually exclusive")
    return doc


def recipe_path(root, token):
    from .session_recipe import recipe_path as session_path
    return session_path(root, token, "replay")


def load(root, token, *, create=False):
    path = recipe_path(root, token) if token else None
    if path and path.exists():
        from .session_recipe import provider_recipe
        doc = load_yaml(path)
        provider_recipe(doc, "replay")
        return path, doc
    if path and not create:
        raise RigError(f"replay recipe not found: {path}")
    return path, {"version": 1}


def workspace(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    name = re.sub(r"[^A-Za-z0-9_-]", "-", path.stem)
    # Same-named recipes outside replay/ cannot overwrite one another.
    if path.parent != (root / "replay").resolve():
        name += "-" + digest(str(path))[:8]
    return root / "var" / "replay" / name


def bound_source(root, path, doc, override=None):
    if override:
        return override
    if doc.get("version") == 2:
        doc = doc["replay"]
    if path:
        binding = workspace(root, path) / "bindings.yaml"
        if binding.exists():
            saved = load_yaml(binding)
            if saved.get("recipe_source") == doc.get("source"):
                return saved.get("source") or doc.get("source")
    return doc.get("source")
