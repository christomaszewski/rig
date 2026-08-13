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
from .interpolate import substitute
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


def overlay_payload_path(root: Path, ref: str) -> Path:
    """The deployment-local copy of a bound overlay's delta (`rig overlay apply` records it) —
    self-containment: rendering never needs the registry cache."""
    return root / "config" / ".overlays" / (ref.replace("/", "--").replace("@", "--") + ".yaml")


def _layered_dict(sensor: Sensor, root: Path, variables: dict | None = None) -> dict:
    """The four-layer merge, honoring LOCAL BEATS OVERLAYS: pinned base ⊕ overlays (bound order) ⊕
    the working file's local delta ⊕ row overrides — then ONE `{{var}}` interpolation pass
    (vehicle-local vars; unknown var = hard error). Without a pinned base (hand-authored
    instance) the working file itself is the base. Identity (name/service) is stamped last,
    never interpolated."""
    working = load_yaml(sensor.config)
    if sensor.overlays:
        pin = root / "config" / ".pins" / f"{sensor.name}.yaml"
        if pin.is_file():
            base = load_yaml(pin)
            local = structural_diff(
                {k: v for k, v in base.items() if k not in ("name", "service")},
                {k: v for k, v in working.items() if k not in ("name", "service")})
        else:
            base, local = working, {}
        cfg = dict(base)
        for ref in sensor.overlays:
            payload_file = overlay_payload_path(root, ref)
            if not payload_file.is_file():
                from . import RigError
                raise RigError(f"{sensor.name}: overlay '{ref}' payload copy missing "
                               f"({payload_file.relative_to(root)}) — re-run `rig overlay apply`")
            cfg = deep_merge(cfg, load_yaml(payload_file))
        cfg = deep_merge(cfg, local)
    else:
        cfg = dict(working)
    if sensor.overrides:
        cfg = deep_merge(cfg, sensor.overrides)
    if variables is not None:
        cfg = substitute(cfg, variables, where=f"{sensor.name}: config")
    cfg.setdefault("service", sensor.service)
    cfg["name"] = sensor.name
    return cfg


def resolved_dict(sensor: Sensor, root: Path | None = None) -> dict:
    """The fully-merged config dict for a sensor. Used where a caller needs the resolved values
    (e.g. doctor reading a host port). `root` is needed once overlays are bound; without it, a
    sensor with overlays raises rather than silently dropping layer 2."""
    if sensor.overlays and root is None:
        from . import RigError
        raise RigError(f"{sensor.name}: overlays bound but no deployment root given (internal)")
    if root is not None:
        return _layered_dict(sensor, root)
    base = load_yaml(sensor.config)
    cfg = deep_merge(base, sensor.overrides) if sensor.overrides else dict(base)
    cfg.setdefault("service", sensor.service)
    cfg["name"] = sensor.name
    return cfg


def materialize(sensor: Sensor, root: Path, variables: dict | None = None) -> Path:
    """Return the config path to hand the launcher. If the base is already a complete *named* config
    with no overrides, no overlays, and no `{{var}}` markers, return it unchanged; otherwise render
    the four-layer merge (+ interpolation) to ``var/rendered/<name>.yaml``. Deterministic: same
    inputs -> identical render (up and down agree)."""
    if (not sensor.overrides and not sensor.overlays
            and "{{" not in Path(sensor.config).read_text()
            and "name" in load_yaml(sensor.config)):
        return sensor.config
    cfg = _layered_dict(sensor, root, variables)
    out_dir = root / "var" / "rendered"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{sensor.name}.yaml"
    with open(out, "w") as handle:
        yaml.safe_dump(cfg, handle, sort_keys=False, default_flow_style=False)
    return out


def materialize_manifest(manifest: Manifest, root: Path) -> Manifest:
    """Rewrite each sensor's ``config`` to its resolved path (rendering profiles/overrides/overlays/
    vars as needed), so the rest of rig (dispatch, status, doctor) just uses ``sensor.config`` and
    never sees the templating."""
    sensors = [dataclasses.replace(s, config=materialize(s, root, manifest.vars))
               for s in manifest.sensors]
    return dataclasses.replace(manifest, sensors=sensors)
