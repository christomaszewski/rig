"""``rig provision`` — THIS machine's vehicle identity (`/etc/rig/vehicle.local.yaml`).

The one machine-scoped write rig has, so it's a separate verb from `rig setup` (user state,
never sudo). Write mode needs root (`sudo rig provision --id 7 --name skiff-07`); bare
`rig provision` is a no-sudo SHOW mode: the current identity plus — when run inside a
deployment or extracted artifact — which referenced vars are satisfied and which mandatory
ones are missing (the "why won't this artifact come up" diagnostic).

CHANGING an existing identity requires ``--force``: compose project names derive from
``<name>-vehicle-<id>``, so re-identifying a live vehicle orphans the running containers and
volumes under the old names. Creating is cheap; re-identifying should feel consequential.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .manifest import LOCAL_KEYS, MACHINE_LOCAL_DEFAULT


def _target() -> Path:
    return Path(os.environ.get("RIG_VEHICLE_LOCAL") or MACHINE_LOCAL_DEFAULT)


def _show(root: Path | None) -> int:
    target = _target()
    if target.is_file():
        data = load_yaml(target)
        eprint(f"machine identity ({target}):")
        for key in sorted(set(data) & LOCAL_KEYS):
            eprint(f"  {key}: {data[key]}")
    else:
        eprint(f"machine identity: NOT PROVISIONED ({target} does not exist)")
        eprint(f"  provision with: sudo rig provision --id <N> --name <name>")
    if root is None or not (root / "vehicle.yaml").is_file():
        return 0
    # Inside a deployment/artifact: cross-check what it references against what's provided.
    from .bake import fleet_refs
    refs = sorted(fleet_refs(root))
    if not refs:
        eprint(f"  deployment at {root}: no {{{{var}}}} references — identity-independent")
        return 0
    try:
        from .manifest import load_manifest
        manifest = load_manifest(root)
    except RigError as exc:
        eprint(f"  deployment at {root}: NOT satisfied — {exc}")
        return 1
    known = set(manifest.vars) | {"ros_domain_id", "fleet_peer_ids"}  # both DERIVED — the
    #                          latter only materializes once vehicle_id + fleet_ids are provided
    unmet = sorted((set(refs) - known) | set(manifest.missing_identity))
    if unmet:
        eprint(f"  deployment at {root}: NOT satisfied — missing: {', '.join(unmet)}")
        return 1
    eprint(f"  deployment at {root}: all vars satisfied "
           f"({', '.join(f'{r}={manifest.vars.get(r)}' for r in refs if r in manifest.vars)})")
    return 0


def provision(root: Path | None, *, vehicle_id: str | None, name: str | None,
              set_vars: list[str], platform: str | None = None,
              data_dir: str | None = None, force: bool) -> int:
    if vehicle_id is None and name is None and not set_vars and platform is None \
            and data_dir is None:
        return _show(root)

    target = _target()
    existing = load_yaml(target) if target.is_file() else {}
    data = dict(existing)
    if vehicle_id is not None:
        if not str(vehicle_id).isdigit():
            raise RigError(f"provision: --id must be numeric (it becomes the ROS domain and the "
                           f"compose-project suffix), got '{vehicle_id}'")
        data["vehicle_id"] = int(vehicle_id)
    if name is not None:
        if not re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", name):
            raise RigError(f"provision: --name must be [A-Za-z][A-Za-z0-9_-]* , got '{name}'")
        data["vehicle"] = name
    if set_vars:
        merged = dict(data.get("vars") or {})
        for pair in set_vars:
            key, sep, value = pair.partition("=")
            if not sep or not re.match(r"^[a-z][a-z0-9_]*$", key):
                raise RigError(f"provision: --var takes name=value with a lowercase name, got '{pair}'")
            merged[key] = int(value) if value.isdigit() else value
        data["vars"] = merged
    if platform is not None:  # a HOST fact, so the machine file is its natural home; tag-safe:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", platform):  # it suffixes image tags
            raise RigError(f"provision: --platform must be a valid image-tag fragment "
                           f"([A-Za-z0-9][A-Za-z0-9._-]*), got '{platform}'")
        data["platform"] = platform
    if data_dir is not None:  # the run registry's home — a HOST fact like platform (runs.py's
        #                       absolute-path rule: a relative path would fork the registry per
        #                       working directory). Registry creation itself stays LAZY: the
        #                       first `up`/`new-run` mints <data_dir>/runs/ — nothing to init.
        expanded = Path(data_dir).expanduser()
        if not expanded.is_absolute():
            raise RigError(f"provision: --data-dir must be an ABSOLUTE path, got '{data_dir}'")
        data["data_dir"] = str(expanded)

    for key in ("vehicle", "vehicle_id"):  # re-identification gate — orphaned-containers warning
        if key in existing and existing.get(key) != data.get(key) and not force:
            raise RigError(
                f"provision: this machine is already '{existing.get('vehicle', '?')}' "
                f"(id {existing.get('vehicle_id', '?')}) — changing identity renames every compose "
                f"project (<name>-vehicle-<id>), ORPHANING running containers/volumes under the old "
                f"names. Bring the vehicle down first, then re-run with --force")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# Machine vehicle identity — written by `rig provision`; read by every rig/artifact\n"
            "# on this machine (precedence: shell > deployment-local > THIS file > vehicle.yaml).\n"
            + yaml.safe_dump(data, sort_keys=False))
    except PermissionError:
        raise RigError(f"provision: cannot write {target} — run with sudo")
    eprint(f"rig provision: {target} written — vehicle "
           f"'{data.get('vehicle', '?')}' id {data.get('vehicle_id', '?')}"
           + (f", platform {data['platform']}" if data.get("platform") else "")
           + (f", data_dir {data['data_dir']}" if data.get("data_dir") else "")
           + (f", vars: {', '.join(sorted(data.get('vars') or {}))}" if data.get("vars") else ""))
    return 0
