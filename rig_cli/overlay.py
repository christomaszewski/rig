"""``rig overlay`` — BINDINGS only (installed ≠ active): apply/remove/reorder/list an instance's
ordered overlay list. Package authoring lives in ``rig pkg promote``.

Applying copies the overlay's delta into ``config/.overlays/`` (deployment-local, so rendering
never needs the registry cache) and appends the fully-qualified ref to the instance row's
``overlays:`` list — ORDER IS MERGE ORDER, last-in-order wins on key conflicts, and local (file
edits + row overrides) always beats every overlay. ``--clear-local`` resets the working file to
its pristine pin and drops the row's ``overrides:`` — the promote round-trip's second half:
identical render, but the tuning now lives in a versioned layer.

Row edits go through a parse→modify→re-serialize of the one generated single-line row (verified
to re-parse; on a hand-authored row shape rig prints the exact edit instead of guessing).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .install import qualified, registry_commit, resolve_ref
from .lock import load_lock, record_instance, record_package, record_registry, save_lock, sha256_file
from .manifest import Sensor, load_manifest
from .resolve import overlay_payload_path

_ROW_KEY_ORDER = ("name", "service", "config", "profile", "overlays", "overrides", "enabled", "order")


def _row_to_line(row: dict) -> str:
    parts = []
    for key in _ROW_KEY_ORDER:
        if key not in row:
            continue
        value = row[key]
        rendered = yaml.safe_dump(value, default_flow_style=True, width=10 ** 6).strip()
        if rendered.endswith("..."):  # plain scalars dump with a document-end marker
            rendered = rendered[:-3].strip()
        parts.append(f"{key}: {rendered}")
    extras = {k: v for k, v in row.items() if k not in _ROW_KEY_ORDER}
    for key, value in extras.items():
        rendered = yaml.safe_dump(value, default_flow_style=True, width=10 ** 6).strip()
        if rendered.endswith("..."):
            rendered = rendered[:-3].strip()
        parts.append(f"{key}: {rendered}")
    return "- { " + ", ".join(parts) + " }"


def edit_row(root: Path, instance: str, mutate) -> None:
    """Load vehicle.yaml, find `instance`'s single-line row, apply ``mutate(rowdict)``, write back —
    verified to re-parse, refusing shapes rig can't safely rewrite."""
    veh = root / "vehicle.yaml"
    text = veh.read_text()
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines)
            if re.match(r"^\s*- \{.*\bname: " + re.escape(instance) + r"[,}]", line)]
    if len(hits) != 1:
        raise RigError(f"overlay: vehicle.yaml row for '{instance}' "
                       f"{'not found' if not hits else 'is ambiguous'} or not in the generated "
                       f"single-line form — edit its `overlays:` list yourself")
    i = hits[0]
    indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    row = yaml.safe_load(lines[i].lstrip()[2:])  # strip "- "
    if not isinstance(row, dict):
        raise RigError(f"overlay: cannot parse the row for '{instance}'")
    mutate(row)
    lines[i] = indent + _row_to_line(row)
    new = "\n".join(lines) + "\n"
    try:
        yaml.safe_load(new)
    except yaml.YAMLError as exc:
        raise RigError(f"overlay: refusing to write vehicle.yaml — result would not parse ({exc})")
    veh.write_text(new)


def _find_instance(root: Path, name: str) -> Sensor:
    manifest = load_manifest(root)
    for sensor in manifest.sensors:
        if sensor.name == name:
            return sensor
    raise RigError(f"overlay: unknown instance '{name}' (rig status shows them)")


def _targets_cover(pkg_manifest: dict, sensor: Sensor) -> bool:
    for target in pkg_manifest.get("targets") or []:
        if not isinstance(target, dict):
            continue
        if target.get("service") and str(target["service"]).rpartition("/")[-1] == sensor.service:
            return True
        if target.get("instance") == sensor.name:
            return True
    return False


def apply(root: Path, instance: str, ref: str, *, clear_local: bool = False) -> int:
    sensor = _find_instance(root, instance)
    entry, _, pkg = resolve_ref(ref)
    if pkg.kind != "overlay":
        raise RigError(f"overlay apply: '{ref}' is a {pkg.kind}, not an overlay")
    if not _targets_cover(pkg.manifest, sensor):
        raise RigError(f"overlay apply: '{pkg.name}' does not target service '{sensor.service}' "
                       f"or instance '{instance}' (its targets: {pkg.manifest.get('targets')})")
    fq = qualified(entry, pkg)
    if fq in sensor.overlays:
        raise RigError(f"overlay apply: '{fq}' is already bound to '{instance}' "
                       f"(rig overlay reorder changes precedence)")
    payload_rel = (pkg.manifest.get("config") or {}).get("payload")
    src = pkg.pkg_dir / str(payload_rel)
    if not src.is_file():
        raise RigError(f"overlay apply: payload missing in the synced registry: {payload_rel}")

    dest = overlay_payload_path(root, fq)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())
    lock = load_lock(root)
    record_package(lock, fq, {"kind": "overlay", "payload_sha256": sha256_file(dest),
                              "project": pkg.manifest.get("project")})
    record_registry(lock, entry.name, rtype=entry.type, location=entry.location,
                    commit=registry_commit(entry))

    def mutate(row: dict) -> None:
        row.setdefault("overlays", [])
        row["overlays"] = list(row["overlays"]) + [fq]
        if clear_local:
            row.pop("overrides", None)

    edit_row(root, instance, mutate)
    if clear_local:
        pin = root / "config" / ".pins" / f"{instance}.yaml"
        if pin.is_file():
            Path(sensor.config).write_bytes(pin.read_bytes())
            eprint(f"  {instance}: working config reset to the pristine base (pin); row overrides "
                   f"dropped — the tuning now lives in '{fq}'")
        else:
            eprint(f"  {instance}: no pinned base — working file left as-is (only overrides dropped)")
    sensor = _find_instance(root, instance)  # re-read: the row just changed
    record_instance(lock, instance, profile=sensor.profile,
                    base_sha256=((lock.get("instances") or {}).get(instance) or {}).get(
                        "base_sha256", ""), overlays=list(sensor.overlays))
    save_lock(root, lock)
    eprint(f"rig overlay apply: '{fq}' bound to '{instance}' at position {len(sensor.overlays)} "
           f"(last binding wins on conflicts; local still beats every overlay)")
    return 0


def _bound_match(bound: str, ref: str) -> bool:
    """A bound fq ref matches itself, its unversioned form, or its bare package name."""
    unversioned = bound.split("@", 1)[0]
    return ref in (bound, unversioned, unversioned.rpartition("/")[-1])


def remove(root: Path, instance: str, ref: str) -> int:
    sensor = _find_instance(root, instance)
    matches = [o for o in sensor.overlays if _bound_match(o, ref)]
    if not matches:
        raise RigError(f"overlay remove: '{ref}' is not bound to '{instance}' "
                       f"(bound: {', '.join(sensor.overlays) or 'none'})")
    fq = matches[0]

    def mutate(row: dict) -> None:
        row["overlays"] = [o for o in row.get("overlays") or [] if o != fq]
        if not row["overlays"]:
            row.pop("overlays")

    edit_row(root, instance, mutate)
    lock = load_lock(root)
    remaining = [o for o in sensor.overlays if o != fq]
    row = (lock.get("instances") or {}).get(instance) or {}
    record_instance(lock, instance, profile=sensor.profile,
                    base_sha256=row.get("base_sha256", ""), overlays=remaining)
    still_bound = any(fq in s.overlays for s in load_manifest(root).sensors)
    if not still_bound:
        overlay_payload_path(root, fq).unlink(missing_ok=True)  # last binding gone -> drop the copy
        (lock.get("packages") or {}).pop(fq, None)
    save_lock(root, lock)
    eprint(f"rig overlay remove: '{fq}' unbound from '{instance}'")
    return 0


def reorder(root: Path, instance: str, refs: list[str]) -> int:
    sensor = _find_instance(root, instance)
    current = list(sensor.overlays)
    full = []
    for ref in refs:  # accept bare names as shorthand for the bound fq refs
        hit = [o for o in current if _bound_match(o, ref)]
        if not hit:
            raise RigError(f"overlay reorder: '{ref}' is not bound to '{instance}'")
        full.append(hit[0])
    if sorted(full) != sorted(current):
        raise RigError(f"overlay reorder: give the COMPLETE new order — bound: "
                       f"{', '.join(current)}")

    edit_row(root, instance, lambda row: row.__setitem__("overlays", full))
    lock = load_lock(root)
    row = (lock.get("instances") or {}).get(instance) or {}
    record_instance(lock, instance, profile=sensor.profile,
                    base_sha256=row.get("base_sha256", ""), overlays=full)
    save_lock(root, lock)
    eprint(f"rig overlay reorder: '{instance}' -> {', '.join(full)} (last wins)")
    return 0


def list_bindings(root: Path, instance: str | None) -> int:
    manifest = load_manifest(root)
    shown = 0
    for sensor in manifest.sensors:
        if instance and sensor.name != instance:
            continue
        if not sensor.overlays and not instance:
            continue
        shown += 1
        if not sensor.overlays:
            print(f"{sensor.name}: no overlays bound")
            continue
        print(f"{sensor.name}:")
        for i, ref in enumerate(sensor.overlays, 1):
            print(f"  {i}. {ref}")
    if not shown:
        print("no overlay bindings in this deployment")
    return 0
