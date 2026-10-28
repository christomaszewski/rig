"""Portable session intent shared by recorded playback and simulator attachment.

Provider bindings stay explicit. A v2 document does not make recorded identities into
simulator bindings, and it never rewrites the deployment or a legacy replay recipe.
"""
from __future__ import annotations

import copy
import dataclasses
import re
from pathlib import Path

from . import RigError
from .common import load_yaml

_NAME = re.compile(r"[A-Za-z0-9_-]+\Z")
_TOPIC = re.compile(r"(?:/[A-Za-z_][A-Za-z0-9_]*)+\Z")
_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*/msg/[A-Za-z_][A-Za-z0-9_]*\Z")


def _mapping(value, allowed, where):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise RigError(f"{where}: expected a mapping with only {', '.join(sorted(allowed))}")
    return value


def _string(value, where, pattern=None):
    if not isinstance(value, str) or not value.strip() or (pattern and not pattern.fullmatch(value)):
        raise RigError(f"{where}: expected a nonempty " + ("valid name" if pattern else "string"))
    return value


def _names(value, where, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise RigError(f"{where}: expected {'a nonempty' if nonempty else 'a'} list of instance names")
    for name in value:
        _string(name, where, _NAME)
    if len(value) != len(set(value)):
        raise RigError(f"{where}: duplicate instance names")
    return value


def validate(doc):
    """Validate v2 without changing the supplied document or filling implicit defaults."""
    _mapping(doc, {"version", "session", "replay", "simulation"}, "session recipe")
    if type(doc.get("version")) is not int or doc["version"] != 2:
        raise RigError("session recipe: unsupported version (expected version: 2)")
    session = _mapping(doc.get("session"), {"instances", "overrides", "clock"}, "session")
    selected = _names(session.get("instances"), "session.instances", nonempty=True)
    if session.get("clock", "source") not in ("source", "wall"):
        raise RigError("session.clock: expected source or wall")
    overrides = session.get("overrides", {})
    if not isinstance(overrides, dict):
        raise RigError("session.overrides: expected an instance mapping")
    for name, patch in overrides.items():
        _string(name, "session.overrides", _NAME)
        if name not in selected:
            raise RigError(f"session.overrides.{name}: instance is not selected in session.instances")
        if not isinstance(patch, dict):
            raise RigError(f"session.overrides.{name}: expected a config patch mapping")
        if {"name", "service"} & set(patch):
            raise RigError(f"session.overrides.{name}: cannot change name or service identity")
    if "replay" not in doc and "simulation" not in doc:
        raise RigError("session recipe: declare replay, simulation, or both provider sections")
    if "replay" in doc:
        from .replay_recipe import validate as validate_replay
        if not isinstance(doc["replay"], dict) or "version" in doc["replay"]:
            raise RigError("replay: expected version-1 replay fields without a nested version")
        validate_replay({"version": 1, **doc["replay"]})
    if "simulation" in doc:
        sim = _mapping(doc["simulation"], {"provider", "mode", "replace", "ros", "publishes",
                       "subscribes", "scene", "asset_version", "isaac_version", "seed", "probe", "timeout_s"},
                       "simulation")
        if sim.get("provider") != "isaac" or sim.get("mode", "attach") != "attach":
            raise RigError("simulation: supported provider/mode is isaac/attach")
        replaced = _names(sim.get("replace", []), "simulation.replace")
        if set(replaced) & set(selected):
            raise RigError("simulation.replace: a replaced physical instance cannot also be selected")
        ros = _mapping(sim.get("ros"), {"distro", "rmw", "domain_id"}, "simulation.ros")
        _string(ros.get("distro"), "simulation.ros.distro", re.compile(r"[a-z][a-z0-9_]*"))
        _string(ros.get("rmw"), "simulation.ros.rmw", re.compile(r"rmw_[A-Za-z0-9_]+"))
        if type(ros.get("domain_id")) is not int or not 0 <= ros["domain_id"] <= 232:
            raise RigError("simulation.ros.domain_id: expected an integer between 0 and 232")
        _string(sim.get("scene"), "simulation.scene")
        for key in ("asset_version", "isaac_version"):
            if key in sim:
                _string(sim[key], f"simulation.{key}")
        if "seed" in sim and (type(sim["seed"]) is not int or sim["seed"] < 0):
            raise RigError("simulation.seed: expected a nonnegative integer")
        if "timeout_s" in sim and (type(sim["timeout_s"]) not in (int, float) or
                not 0 < sim["timeout_s"] <= 120):
            raise RigError("simulation.timeout_s: expected a finite number above 0 and at most 120")
        if "probe" in sim:
            if not isinstance(sim["probe"], list) or not sim["probe"]:
                raise RigError("simulation.probe: expected a nonempty command argument list")
            for arg in sim["probe"]:
                _string(arg, "simulation.probe")
                if "\x00" in arg:
                    raise RigError("simulation.probe: NUL is not allowed in command arguments")
        directions = {}
        for direction in ("publishes", "subscribes"):
            endpoints = sim.get(direction, [])
            if not isinstance(endpoints, list):
                raise RigError(f"simulation.{direction}: expected a list of topic bindings")
            seen = set()
            for endpoint in endpoints:
                where = f"simulation.{direction}"
                _mapping(endpoint, {"topic", "type", "instance"} if direction == "publishes" else
                         {"topic", "type"}, where)
                topic = _string(endpoint.get("topic"), where + ".topic", _TOPIC)
                _string(endpoint.get("type"), where + ".type", _TYPE)
                if topic in seen:
                    raise RigError(f"{where}: duplicate topic {topic}")
                seen.add(topic)
                if topic == "/clock":
                    raise RigError(f"{where}: /clock is managed by session.clock, not a data binding")
                if "instance" in endpoint:
                    _string(endpoint["instance"], where + ".instance", _NAME)
                    if endpoint["instance"] not in replaced:
                        raise RigError(f"{where}.instance: {endpoint['instance']} is not in simulation.replace")
            directions[direction] = seen
        if directions["publishes"] & directions["subscribes"]:
            raise RigError("simulation: a topic cannot be both a simulator output and a command input")
    return doc


def provider_recipe(doc, provider):
    """Return only the selected provider's bindings; missing sections never silently fall back."""
    if provider not in ("replay", "simulation"):
        raise RigError(f"Unknown session provider: {provider}")
    if isinstance(doc, dict) and doc.get("version") == 1 and provider == "replay":
        from .replay_recipe import validate as validate_replay
        return copy.deepcopy(validate_replay(doc))
    validate(doc)
    if provider not in doc:
        raise RigError(f"Session recipe has no {provider} section; add explicit {provider} bindings")
    payload = copy.deepcopy(doc[provider])
    return {"version": 1, **payload} if provider == "replay" else payload


def recipe_path(root, token, provider):
    root = Path(root)
    token = str(token)
    path = Path(token).expanduser()
    if "/" not in token and path.suffix not in (".yaml", ".yml"):
        shared = root / "sessions" / (token + ".yaml")
        legacy = root / "replay" / (token + ".yaml")
        path = legacy if provider == "replay" and not shared.exists() else shared
    elif not path.is_absolute():
        path = root / path
    return path.resolve()


def load(root, token, provider):
    if not token:
        raise RigError(f"{provider}: --recipe NAME or YAML path is required")
    path = recipe_path(root, token, provider)
    if not path.is_file():
        raise RigError(f"{provider} recipe not found: {path}")
    doc = load_yaml(path)
    provider_recipe(doc, provider)
    return path, doc


def apply_session(manifest, doc):
    """Apply exact selection and the session config layer without mutating deployment state."""
    if isinstance(doc, dict) and doc.get("version") == 1:
        return manifest
    validate(doc)
    session = doc["session"]
    names = set(session["instances"])
    known = {row.name for row in manifest.sensors}
    if names - known:
        raise RigError("session.instances: unknown instances: " + ", ".join(sorted(names - known)))
    from .manifest import PLAYER_SERVICE
    managed = {row.name for row in manifest.sensors if row.service == PLAYER_SERVICE}
    if names & managed:
        raise RigError("session.instances: the bag player is managed automatically")
    overrides = session.get("overrides", {})
    rows = [dataclasses.replace(row, enabled=row.name in names, replay_start=row.name in names,
                               session_overrides=copy.deepcopy(overrides.get(row.name, {})))
            for row in manifest.sensors]
    return dataclasses.replace(manifest, sensors=rows, replay_policy={})
