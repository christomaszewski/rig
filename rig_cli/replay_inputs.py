"""Portable input selectors shared by replay and recipe-driven exports."""
from __future__ import annotations

import copy
import re

from . import RigError

_TARGET = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?::[A-Za-z0-9_.-]+)*(?:/[A-Za-z0-9_.-]+)*\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def validate(inputs):
    if not isinstance(inputs, dict):
        raise RigError("recipe.inputs: expected a target mapping")
    for target, selector in inputs.items():
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            raise RigError(f"recipe.inputs: invalid target {target!r}")
        where = f"recipe.inputs.{target}"
        if not isinstance(selector, dict) or len(selector) != 1:
            raise RigError(f"{where}: choose one of use: original, profile: NAME, or proxy: ID")
        key, value = next(iter(selector.items()))
        if key == "use" and value == "original":
            continue
        if key not in ("profile", "proxy") or not isinstance(value, str) or not _NAME.fullmatch(value):
            raise RigError(f"{where}: choose one of use: original, profile: NAME, or proxy: ID")
    return inputs


def parse_overrides(values):
    selected = {}
    for value in values or ():
        if not isinstance(value, str) or "=" not in value:
            raise RigError("--input expects TARGET=original|profile:NAME|proxy:ID")
        target, choice = value.split("=", 1)
        if choice == "original":
            selector = {"use": "original"}
        elif choice.startswith("profile:") or choice.startswith("proxy:"):
            key, name = choice.split(":", 1)
            selector = {key: name}
        else:
            raise RigError("--input expects TARGET=original|profile:NAME|proxy:ID")
        validate({target: selector})
        if target in selected and selected[target] != selector:
            raise RigError(f"--input: conflicting selections for {target}; specify it once")
        selected[target] = selector
    return selected


def with_overrides(recipe, values):
    """CLI choices override recipe intent; return a copy suitable for prepare persistence."""
    overrides = parse_overrides(values)
    result = copy.deepcopy(recipe)
    if overrides:
        provider = result["replay"] if result.get("version") == 2 else result
        provider["inputs"] = {**provider.get("inputs", {}), **overrides}
    return result


def selection(recipe, target, *, service=None, use_proxies=False):
    """Resolve one selector, retaining the legacy flag's opt-in behavior."""
    explicit = recipe.get("inputs", {}).get(target)
    legacy = None
    if target.startswith("instance:"):
        name = target.split(":", 1)[1]
        preferences = recipe.get("proxies", {})
        instance = preferences.get("instances", {}).get(name, {})
        requested = preferences.get("services", {}).get(service, {})
        if instance.get("use") == "original":
            legacy = {"use": "original"}
        elif instance.get("export"):
            legacy = {"proxy": instance["export"]}
        elif requested.get("profile"):
            legacy = {"profile": requested["profile"]}
        if explicit is not None and legacy is not None and explicit != legacy:
            raise RigError(f"{target}: inputs conflicts with legacy proxies selection; remove the "
                           "legacy proxies entry and keep the choice in inputs")
    if explicit is not None:
        return dict(explicit)
    if use_proxies and target.startswith("instance:"):
        return dict(legacy or {"profile": None})
    return {"use": "original"}


def uses_proxies(recipe, options=None):
    provider = recipe.get("replay", {}) if recipe.get("version") == 2 else recipe
    return bool((options or {}).get("use_proxies") or any(
        value.get("use") != "original" for value in provider.get("inputs", {}).values()))


def validate_targets(recipe, manifest):
    """Unknown or future sub-input selectors must never be silently ignored."""
    rows = {row.name: row for row in manifest.sensors}
    for target in recipe.get("inputs", {}):
        if target == "bags":
            continue
        if not target.startswith("instance:") or target.count(":") != 1 or "/" in target:
            raise RigError(f"Unsupported replay input target {target!r}; this version supports "
                           "bags and instance:NAME. Select the whole input or upgrade rig "
                           "for sub-input support")
        name = target.split(":", 1)[1]
        if name not in rows:
            raise RigError(f"{target}: unknown destination instance; use a name from vehicle.yaml")
        selection(recipe, target, service=rows[name].service)
