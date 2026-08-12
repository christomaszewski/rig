"""Resolve a sensor's config: deep-merge per-instance ``overrides`` onto its base config (or a nameless
*profile*), stamp in the instance ``name``/``service``, and render the result to ``var/rendered/<name>.yaml``.

This is a rig-only, schema-AGNOSTIC step — rig overlays keys without interpreting what they mean, then hands
the launcher a complete, named config exactly as if it were authored by hand. A complete named config with
no overrides is passed through untouched (no render), so the simple one-file-per-sensor case is unchanged.

It serves two needs with one mechanism: sharing a profile across instances that differ only by id, and
flipping a sensor's data source per run (e.g. ``overrides: {connection: {type: file, file: {path: …}}}``).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml  # PyYAML — already required by common

from .common import load_yaml
from .manifest import Manifest, Sensor


def deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge ``patch`` onto ``base``. Mappings merge; scalars and lists replace; a ``None``
    value deletes the key. Returns a new dict; inputs are untouched."""
    out = dict(base)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:  # scalar or list -> replace (keyed list-merge is a v2 enhancement)
            out[key] = value
    return out


def structural_diff(base: dict, current: dict) -> dict:
    """The inverse of deep_merge: the patch D such that ``deep_merge(base, D) == current``. Maps
    recurse; scalar/list differences become replacements; a key present in base but absent in
    current becomes a ``None`` delete marker (which deep_merge already honors). This is THE
    working-copy primitive: `config diff`, `pkg upgrade`, and `pkg promote` are all built on it.
    Caveat (documented): a legitimate bare ``null`` in `current` is indistinguishable from a
    deletion — rig configs don't use bare nulls."""
    patch: dict = {}
    for key, b in base.items():
        if key not in current:
            patch[key] = None
        elif isinstance(b, dict) and isinstance(current[key], dict):
            sub = structural_diff(b, current[key])
            if sub:
                patch[key] = sub
        elif current[key] != b:
            patch[key] = current[key]
    for key, c in current.items():
        if key not in base:
            patch[key] = c
    return patch


def resolved_dict(sensor: Sensor) -> dict:
    """The fully-merged config dict for a sensor (base + overrides + injected name/service). No file I/O —
    used where a caller needs the resolved values (e.g. doctor reading a host port)."""
    base = load_yaml(sensor.config)
    cfg = deep_merge(base, sensor.overrides) if sensor.overrides else dict(base)
    cfg.setdefault("service", sensor.service)
    cfg["name"] = sensor.name
    return cfg


def materialize(sensor: Sensor, root: Path) -> Path:
    """Return the config path to hand the launcher. If the base is already a complete *named* config with no
    overrides, return it unchanged; otherwise render the merged result to ``var/rendered/<name>.yaml`` and
    return that. Deterministic: same manifest + overrides -> identical render (so up and down agree)."""
    base = load_yaml(sensor.config)
    if not sensor.overrides and "name" in base:
        return sensor.config
    cfg = deep_merge(base, sensor.overrides) if sensor.overrides else dict(base)
    cfg.setdefault("service", sensor.service)
    cfg["name"] = sensor.name
    out_dir = root / "var" / "rendered"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{sensor.name}.yaml"
    with open(out, "w") as handle:
        yaml.safe_dump(cfg, handle, sort_keys=False, default_flow_style=False)
    return out


def materialize_manifest(manifest: Manifest, root: Path) -> Manifest:
    """Rewrite each sensor's ``config`` to its resolved path (rendering profiles/overrides as needed), so
    the rest of rig (dispatch, status, doctor) just uses ``sensor.config`` and never sees the templating."""
    sensors = [dataclasses.replace(s, config=materialize(s, root)) for s in manifest.sensors]
    return dataclasses.replace(manifest, sensors=sensors)
