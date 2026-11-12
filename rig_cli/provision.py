"""``rig provision`` — THIS machine's vehicle identity (`/etc/rig/vehicle.local.yaml`).

The one machine-scoped write rig has, so it's a separate verb from `rig setup` (user state,
never sudo). Write mode needs root (`sudo rig provision --id 7 --name skiff-07`); bare
`rig provision` is a no-sudo SHOW mode: the current identity plus — when run inside a
deployment or extracted artifact — which referenced vars are satisfied and which mandatory
ones are missing (the "why won't this artifact come up" diagnostic).

CHANGING an existing identity requires ``--force``: compose project names derive from
``<name>-vehicle-<id>``, so re-identifying a live vehicle orphans the running containers and
volumes under the old names. Creating is cheap; re-identifying should feel consequential.

Handover: ``rig provision --export`` (no sudo) writes this machine's file as a provisioning
RECORD, split in two: ``values`` (its ``vars`` and ``env`` — what a replacement vehicle may
adopt) and ``machine`` (every other key — this computer and airframe). ``sudo rig provision
--apply SOURCE`` merges a record's values into this machine's file, as ``--var`` does;
``--only``/``--except`` pick them by name, and only ``--with-identity`` applies the machine
block (a replaced computer, never a spare that flies beside the original). SOURCE is a record
file or a run: every `up` copies the machine file into the run snapshot (runs.py), so a
vehicle that cannot be reached is still read from its last run. rig does not know which values
are a vehicle's role and which belong to its airframe; the operator says so with --only and
--except, and every apply prints each key's old and new value.
"""
from __future__ import annotations

import datetime
import os
import re
import sys
from pathlib import Path

import yaml

from . import RigError, __version__
from .common import eprint, load_yaml
from .manifest import LOCAL_KEYS, MACHINE_LOCAL_DEFAULT, RIG_OWNED_ENV

RECORD_VERSION = 1  # the `rig_provision:` a record carries; apply refuses any other
_RECORD_KEYS = {"rig_provision", "exported", "rig", "source", "values", "machine"}
_VALUE_KEYS = ("vars", "env")  # what a record's `values` holds; every other local key is `machine`
_VAR_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_UNSET = object()


def _target() -> Path:
    return Path(os.environ.get("RIG_VEHICLE_LOCAL") or MACHINE_LOCAL_DEFAULT)


def registry_host(value: str) -> str:
    """Validate an `images.registry` value: a docker registry HOST[:port][/namespace] — never a
    URL (the scheme is docker's business; a pasted `http://` would compose into an unpullable
    ref), no whitespace, trailing slash dropped. Shared by `provision --registry` (machine-wide)
    and `reconstruct --registry` (tree-local)."""
    host = value.strip().rstrip("/")
    if "://" in host:
        raise RigError(f"--registry takes a docker registry HOST (localhost:5000, "
                       f"registry.lan:5000/fleet), not a URL: '{value}'")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*(:\d+)?(/[A-Za-z0-9._-]+)*$", host):
        raise RigError(f"--registry: '{value}' is not a registry host[:port][/namespace]")
    return host


def _checked_id(value, what: str) -> int:
    if isinstance(value, bool) or not str(value).isdigit():
        raise RigError(f"provision: {what} must be numeric (it becomes the ROS domain and the "
                       f"compose-project suffix), got '{value}'")
    return int(value)


def _checked_name(value, what: str) -> str:
    if not isinstance(value, str) or not re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", value):
        raise RigError(f"provision: {what} must be [A-Za-z][A-Za-z0-9_-]* , got '{value}'")
    return value


def _checked_platform(value, what: str) -> str:
    # a HOST fact, so the machine file is its natural home; tag-safe: it suffixes image tags
    if not isinstance(value, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", value):
        raise RigError(f"provision: {what} must be a valid image-tag fragment "
                       f"([A-Za-z0-9][A-Za-z0-9._-]*), got '{value}'")
    return value


def _checked_arch(value, what: str) -> str:
    if value not in ("arm64", "amd64"):
        raise RigError(f"provision: {what} must be arm64 or amd64")
    return value


def _checked_data_dir(value, what: str) -> str:
    # the run registry's home — a HOST fact like platform (runs.py's absolute-path rule: a
    # relative path would fork the registry per working directory). Registry creation itself
    # stays LAZY: the first `up`/`new-run` mints <data_dir>/runs/ — nothing to init.
    expanded = Path(str(value)).expanduser()
    if not expanded.is_absolute():
        raise RigError(f"provision: {what} must be an ABSOLUTE path, got '{value}'")
    return str(expanded)


def _identity_gate(existing: dict, data: dict, force: bool) -> None:
    for key in ("vehicle", "vehicle_id"):  # re-identification gate — orphaned-containers warning
        if key in existing and existing.get(key) != data.get(key) and not force:
            raise RigError(
                f"provision: this machine is already '{existing.get('vehicle', '?')}' "
                f"(id {existing.get('vehicle_id', '?')}) — changing identity renames every compose "
                f"project (<name>-vehicle-<id>), ORPHANING running containers/volumes under the old "
                f"names. Bring the vehicle down first, then re-run with --force")


def _write(target: Path, data: dict) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# Machine vehicle identity — written by `rig provision`; read by every rig/artifact\n"
            "# on this machine (precedence: shell > deployment-local > THIS file > vehicle.yaml).\n"
            + yaml.safe_dump(data, sort_keys=False))
    except PermissionError:
        raise RigError(f"provision: cannot write {target} — run with sudo")
    except OSError as exc:
        raise RigError(f"provision: cannot write {target}: {exc}")


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
    else:
        eprint(f"  deployment at {root}: all vars satisfied "
               f"({', '.join(f'{r}={manifest.vars.get(r)}' for r in refs if r in manifest.vars)})")
    # …and which tier each referenced value came from: a value that is satisfied but from the
    # wrong place (a stale machine entry over the roster's) looks exactly like a right one above.
    from .manifest import values_by_tier
    for tier, values in values_by_tier(manifest):
        shown = [f"{name}={value}" for name, value in values.items() if name in refs]
        if shown:
            eprint(f"    from {tier}: {', '.join(shown)}")
    for clash in manifest.row_overrides:
        eprint(f"    note: {clash.name}={clash.value!r} from {clash.tier} outranks "
               f"{clash.row}'s {clash.row_value!r}")
    return 1 if unmet else 0


def provision(root: Path | None, *, vehicle_id: str | None, name: str | None,
              set_vars: list[str], platform: str | None = None,
              arch: str | None = None,
              data_dir: str | None = None, registry: str | None = None, force: bool,
              migrate: bool = False, keep_old: bool = False,
              export: str | None = None, apply: str | None = None, only: str | None = None,
              except_names: str | None = None, with_identity: bool = False,
              dry_run: bool = False) -> int:
    writes = not (vehicle_id is None and name is None and not set_vars and platform is None
                  and arch is None and data_dir is None and registry is None and not migrate)
    if export is not None:
        if writes or apply is not None:
            raise RigError("provision --export only reads this machine's file: give it alone "
                           "(with an optional FILE)")
        return export_record(export)
    if apply is not None:
        if writes:
            raise RigError("provision --apply takes its values from the record: drop the other "
                           "write flags (--only/--except narrow it, --with-identity widens it)")
        return apply_record(root, apply, only=only, except_names=except_names,
                            with_identity=with_identity, force=force, dry_run=dry_run)
    if only is not None or except_names is not None or with_identity or dry_run:
        raise RigError("provision: --only, --except, --with-identity and --dry-run belong to "
                       "--apply SOURCE")
    if not writes:
        return _show(root)

    target = _target()
    existing = load_yaml(target) if target.is_file() else {}
    data = dict(existing)
    if migrate:  # move the machine registry along with the setting (datadir.migrate_registry)
        if data_dir is None:
            raise RigError("provision --migrate needs --data-dir NEW (the registry moves there)")
        old = str(existing.get("data_dir") or "").strip()
        if not old:
            raise RigError("provision --migrate: this machine has no data_dir to move — plain "
                           "--data-dir sets one")
        new = Path(data_dir).expanduser()
        if not new.is_absolute():
            raise RigError(f"provision: --data-dir must be an ABSOLUTE path, got '{data_dir}'")
        if Path(old).resolve() != new.resolve():
            from .datadir import migrate_registry
            migrate_registry(Path(old), new, keep_old=keep_old)
    if vehicle_id is not None:
        data["vehicle_id"] = _checked_id(vehicle_id, "--id")
    if name is not None:
        data["vehicle"] = _checked_name(name, "--name")
    if set_vars:
        merged = dict(data.get("vars") or {})
        for pair in set_vars:
            key, sep, value = pair.partition("=")
            if not sep or not re.match(r"^[a-z][a-z0-9_]*$", key):
                raise RigError(f"provision: --var takes name=value with a lowercase name, got '{pair}'")
            merged[key] = int(value) if value.isdigit() else value
        data["vars"] = merged
    if platform is not None:
        data["platform"] = _checked_platform(platform, "--platform")
    if arch is not None:
        data["arch"] = _checked_arch(arch, "--arch")
    if data_dir is not None:
        data["data_dir"] = _checked_data_dir(data_dir, "--data-dir")
    if registry is not None:  # the image registry HOST is a per-machine fact too (a bench pulls
        #                       from its own mirror). `images` merges per SUBKEY in the local
        #                       file: tag/base keep coming from vehicle.yaml, only the host swaps.
        data["images"] = {**(data.get("images") or {}), "registry": registry_host(registry)}

    _identity_gate(existing, data, force)
    _write(target, data)
    eprint(f"rig provision: {target} written — vehicle "
           f"'{data.get('vehicle', '?')}' id {data.get('vehicle_id', '?')}"
           + (f", platform {data['platform']}" if data.get("platform") else "")
           + (f", arch {data['arch']}" if data.get("arch") else "")
           + (f", data_dir {data['data_dir']}" if data.get("data_dir") else "")
           + (f", registry {data['images']['registry']}"
              if (data.get("images") or {}).get("registry") else "")
           + (f", vars: {', '.join(sorted(data.get('vars') or {}))}" if data.get("vars") else ""))
    return 0


# --- provisioning records: export from one vehicle, apply to another ----------------------------

def _check_local_keys(data: dict, where) -> None:
    unknown = set(data) - LOCAL_KEYS
    if unknown:
        raise RigError(f"provision: {where}: unknown key(s): {', '.join(sorted(unknown))} — a "
                       f"machine file carries only: {', '.join(sorted(LOCAL_KEYS))}")


def _record(data: dict, source_file) -> dict:
    """A machine file's data as a record. Nothing is dropped: `values` and `machine` together
    are the file, so --with-identity on an empty machine rebuilds it."""
    return {
        "rig_provision": RECORD_VERSION,
        "exported": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rig": __version__,
        "source": {"vehicle": data.get("vehicle"), "vehicle_id": data.get("vehicle_id"),
                   "file": str(source_file)},
        "values": {key: dict(data.get(key) or {}) for key in _VALUE_KEYS},
        "machine": {key: value for key, value in data.items() if key not in _VALUE_KEYS},
    }


def export_record(dest: str) -> int:
    """`rig provision --export [FILE]`: this machine's file as a record, on standard output
    (`-`, the default) or into FILE. Reads only — no sudo."""
    target = _target()
    if not target.is_file():
        raise RigError(f"provision --export: this machine is NOT PROVISIONED ({target} does not "
                       f"exist) — there is nothing to export")
    data = load_yaml(target)
    _check_local_keys(data, target)
    record = _record(data, target)
    text = ("# rig provisioning record (`rig provision --export`). On another vehicle:\n"
            "#   sudo rig provision --apply THIS_FILE [--only k,k | --except k,k] [--dry-run]\n"
            "# applies `values`; `machine` is this computer and airframe (--with-identity).\n"
            + yaml.safe_dump(record, sort_keys=False))
    if dest == "-":
        sys.stdout.write(text)
    else:
        try:
            Path(dest).expanduser().write_text(text)
        except OSError as exc:
            raise RigError(f"provision --export: cannot write {dest}: {exc}")
    values = record["values"]
    eprint(f"rig provision: exported '{data.get('vehicle', '?')}' (id {data.get('vehicle_id', '?')})"
           f" — {len(values['vars'])} var(s), {len(values['env'])} env value(s)"
           + ("" if dest == "-" else f" -> {dest}"))
    return 0


def _record_from_run(run_dir: Path) -> dict:
    """The record a run stands for: the machine file its LAST `up` captured (runs.py copies it
    into the snapshot). Never vars.yaml — that is every tier mixed, and applying it would
    provision vehicle.yaml defaults and roster values onto the machine tier."""
    from . import runs
    mpath = run_dir / "manifest.yaml"
    if not mpath.is_file():
        raise RigError(f"provision --apply: {run_dir} is not a run directory (no manifest.yaml)")
    doc = load_yaml(mpath)
    ups = doc.get("ups")
    last = ups[-1] if isinstance(ups, list) and ups and isinstance(ups[-1], dict) else None
    if last is None or not last.get("config"):
        raise RigError(f"provision --apply: run {run_dir.name} recorded no `up`, so it holds no "
                       f"config snapshot to read")
    snap = run_dir / ".rig" / "config" / str(last["config"])
    copy = snap / runs.MACHINE_COPY
    if not copy.is_file():
        why = (f"it was brought up by rig {doc.get('rig_version', '?')}, which did not capture "
               f"one" if last.get("capture_set", 1) < 2 else
               "its vehicle had none at that `up` (its identity came from the shell or the tree)")
        raise RigError(f"provision --apply: run {run_dir.name} holds no machine file — {why}. "
                       f"Its vars.yaml mixes every tier and is no substitute; export a record on "
                       f"the vehicle instead (`rig provision --export`)")
    from .reconstruct import _snapshot_digest_ok
    if not _snapshot_digest_ok(snap):
        raise RigError(f"provision --apply: snapshot {last['config']} of run {run_dir.name} fails "
                       f"its own content-address — altered or truncated; re-fetch the run")
    record = _record(load_yaml(copy), copy)
    record["exported"] = str(last.get("at") or record["exported"])
    record["rig"] = str(doc.get("rig_version") or record["rig"])
    return record


def _read_source(root: Path | None, source: str) -> tuple[dict, str]:
    """(record, what to call it). SOURCE is a record file, a run directory, or — inside a
    deployment — a run id or label as the run verbs take it."""
    path = Path(source).expanduser()
    if path.is_file():
        record = load_yaml(path)
        if "rig_provision" not in record:
            raise RigError(f"provision --apply: {path} is not a provisioning record (it has no "
                           f"rig_provision key) — make one with `rig provision --export` on the "
                           f"source vehicle")
        return record, str(path)
    if path.is_dir():
        return _record_from_run(path), f"run {path.name}"
    if "/" in source:
        raise RigError(f"provision --apply: {path} is neither a record file nor a run directory")
    if root is None:
        raise RigError(f"provision --apply: '{source}' is not a file or a run directory. A run id "
                       f"or label is looked up in a deployment's registry: run this inside one, "
                       f"or pass --root")
    from . import runs
    from .manifest import load_manifest
    _, run_dir = runs.resolve_ref(load_manifest(root), source, verb="provision --apply")
    return _record_from_run(run_dir), f"run {run_dir.name}"


def _check_record(record: dict, origin: str) -> tuple[dict, dict]:
    """(values, machine) of a record, validated as the write flags and the loader would: the
    record format, the keys a machine file may carry, var and env names, an absolute data_dir."""
    where = f"--apply: {origin}"
    if record.get("rig_provision") != RECORD_VERSION:
        raise RigError(f"provision {where}: rig_provision is {record.get('rig_provision')!r}, and "
                       f"this rig reads format {RECORD_VERSION} — export it again with this "
                       f"version, or apply it with the rig that wrote it")
    stray = set(record) - _RECORD_KEYS
    if stray:
        raise RigError(f"provision {where}: unknown record key(s): {', '.join(sorted(stray))}")
    values, machine = record.get("values") or {}, record.get("machine") or {}
    if not isinstance(values, dict) or set(values) - set(_VALUE_KEYS) \
            or not all(isinstance(values.get(k) or {}, dict) for k in _VALUE_KEYS):
        raise RigError(f"provision {where}: `values` holds two mappings, vars and env")
    if not isinstance(machine, dict):
        raise RigError(f"provision {where}: `machine` must be a mapping")
    stray = set(machine) - (LOCAL_KEYS - set(_VALUE_KEYS))
    if stray:
        raise RigError(f"provision {where}: `machine` holds {', '.join(sorted(stray))} — a "
                       f"machine file carries only: {', '.join(sorted(LOCAL_KEYS))}")
    values = {key: dict(values.get(key) or {}) for key in _VALUE_KEYS}
    for name in values["vars"]:
        if not isinstance(name, str) or not _VAR_NAME.match(name):
            raise RigError(f"provision {where}: var name {name!r} — names are lowercase "
                           f"[a-z][a-z0-9_]*")
    for name in values["env"]:
        if not isinstance(name, str) or not _ENV_NAME.match(name) or name in RIG_OWNED_ENV:
            raise RigError(f"provision {where}: env name {name!r} — exported names are UPPERCASE "
                           f"[A-Z][A-Z0-9_]* and never one rig owns")
    checked = dict(machine)
    for key, check in (("vehicle", _checked_name), ("vehicle_id", _checked_id),
                       ("platform", _checked_platform), ("arch", _checked_arch),
                       ("data_dir", _checked_data_dir)):
        if key in checked:
            checked[key] = check(checked[key], f"{where}: machine.{key}")
    ros = checked.get("ros")
    if "ros" in checked and (not isinstance(ros, dict) or set(ros) != {"domain_id"}):
        raise RigError(f"provision {where}: machine.ros carries only domain_id")
    for key in ("images", "replay"):
        if key in checked and not isinstance(checked[key], dict):
            raise RigError(f"provision {where}: machine.{key} must be a mapping")
    return values, checked


def _chosen(values: dict, only: str | None, except_names: str | None) -> set[str]:
    """The names of `values` (vars and env — one namespace: vars are lowercase, env UPPERCASE)
    that --only / --except leave selected. A name the record does not hold is an error: a typo
    must not quietly apply everything, or nothing."""
    names = set(values["vars"]) | set(values["env"])
    if only is not None and except_names is not None:
        raise RigError("provision --apply: --only and --except say the same thing two ways — "
                       "give one")
    chosen = names
    for flag, raw in (("--only", only), ("--except", except_names)):
        if raw is None:
            continue
        picked = {part.strip() for part in raw.split(",") if part.strip()}
        if not picked:
            raise RigError(f"provision --apply: {flag} needs at least one name")
        unknown = sorted(picked - names)
        if unknown:
            raise RigError(f"provision --apply: {flag} names {', '.join(unknown)}, which the "
                           f"record does not hold (it holds: {', '.join(sorted(names)) or 'no values'})")
        chosen = picked if flag == "--only" else names - picked
    return chosen


def _identical(a, b) -> bool:
    """The same YAML, type and all. Python calls True and 1 equal, and 1.0 and 1; a config that
    takes the value whole (`flag: "{{use_sensor}}"`) and a typed consumer do not."""
    return yaml.safe_dump(a, sort_keys=True) == yaml.safe_dump(b, sort_keys=True)


def apply_record(root: Path | None, source: str, *, only: str | None, except_names: str | None,
                 with_identity: bool, force: bool, dry_run: bool) -> int:
    """`sudo rig provision --apply SOURCE`: merge a record's values into this machine's file.
    Prints every key's old and new value; --dry-run stops there."""
    record, origin = _read_source(root, source)
    values, machine = _check_record(record, origin)
    chosen = _chosen(values, only, except_names)
    target = _target()
    existing = load_yaml(target) if target.is_file() else {}
    data = dict(existing)
    lines: list[str] = []
    for bucket in _VALUE_KEYS:
        have = dict(existing.get(bucket) or {})
        merged = dict(have)
        for name, new in values[bucket].items():
            old = have.get(name, _UNSET)
            if name not in chosen:
                kept = "" if old is _UNSET else f"; this machine keeps {old!r}"
                lines.append(f"  {bucket}.{name}: not applied (the record has {new!r}{kept})")
            elif old is _UNSET:
                lines.append(f"  {bucket}.{name}: (unset) -> {new!r}")
            elif _identical(old, new):
                lines.append(f"  {bucket}.{name}: {new!r} (unchanged)")
            else:
                lines.append(f"  {bucket}.{name}: {old!r} -> {new!r}")
            if name in chosen:
                merged[name] = new
        if not _identical(merged, have):
            data[bucket] = merged
    if with_identity:
        for key, new in machine.items():
            old = existing.get(key, _UNSET)
            lines.append(f"  {key}: " + (f"(unset) -> {new!r}" if old is _UNSET else
                                         f"{new!r} (unchanged)" if _identical(old, new) else
                                         f"{old!r} -> {new!r}"))
            data[key] = new
    elif machine:
        lines.append(f"  machine block ({', '.join(machine)}): not applied — --with-identity "
                     f"applies it (a replaced computer, not a spare beside the original)")
    src = record.get("source") or {}
    eprint(f"rig provision: {origin} — from '{src.get('vehicle', '?')}' (id "
           f"{src.get('vehicle_id', '?')}), exported {record.get('exported', '?')} -> {target}")
    for line in lines or ["  (the record holds nothing)"]:
        eprint(line)
    _identity_gate(existing, data, force)
    if dry_run:
        eprint("rig provision: (dry-run) nothing written")
        return 0
    if _identical(data, existing):
        eprint(f"rig provision: nothing to change — {target} left as it is")
        return 0
    _write(target, data)
    eprint(f"rig provision: {target} written — vehicle '{data.get('vehicle', '?')}' id "
           f"{data.get('vehicle_id', '?')}"
           + (f", vars: {', '.join(sorted(data.get('vars') or {}))}" if data.get("vars") else ""))
    return 0
