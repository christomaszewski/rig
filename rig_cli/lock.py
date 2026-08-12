"""rig.lock — ONE lockfile for the deployment (settled: extend bake's existing file, never a second).

Lives at the deployment root once any registry package is installed; ``rig bake`` merges its
image-digest map into the copy staged inside the artifact, so the artifact carries the complete
picture (packages + images) under one filename and schema.

Sections (all optional — an empty lock is `{}`):

- ``registries``: name -> {type, url|path, commit} — enough to reproduce the registry set on a
  fresh machine (`--locked` installs resolve against these exact commits).
- ``packages``: fully-qualified exact ref (``public/siyi-zr30@1.0.0``) -> {kind, source|image,
  payload_sha256, requires} — what was resolved, pinned, and verified at install time.
- ``instances``: instance name -> {profile, base_sha256, overlays: [refs]} — the working-copy
  anchor: which pinned payload materialized the instance's editable config (the hash is what
  `config diff` and `pkg promote` diff against) and the ordered overlay bindings.
- ``images``: image ref -> digest — bake's original section, unchanged shape.

Everything is written deterministically (sorted keys) so lockfile diffs are honest.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from . import RigError
from .common import load_yaml

LOCK_SCHEMA = 1
_HEADER = ("# rig.lock — GENERATED provenance: registries@commit, package pins + hashes, instance\n"
           "# base-config anchors, image digests (bake). Commit it; regenerate via rig, don't edit.\n")


def lock_path(root: Path) -> Path:
    return root / "rig.lock"


def load_lock(root: Path) -> dict:
    path = lock_path(root)
    if not path.is_file():
        return {"schema": LOCK_SCHEMA}
    data = load_yaml(path)
    schema = data.get("schema", LOCK_SCHEMA)
    if isinstance(schema, int) and schema > LOCK_SCHEMA:
        raise RigError(f"{path}: lock schema {schema} is newer than this rig understands "
                       f"(max {LOCK_SCHEMA}) — upgrade rig")
    data.setdefault("schema", LOCK_SCHEMA)
    return data


def save_lock(root: Path, lock: dict) -> Path:
    path = lock_path(root)
    body = {key: lock[key] for key in sorted(lock) if lock[key] not in (None, {}, [])}
    body["schema"] = lock.get("schema", LOCK_SCHEMA)
    path.write_text(_HEADER + yaml.safe_dump(body, sort_keys=True, default_flow_style=False))
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_registry(lock: dict, name: str, *, rtype: str, location: str, commit: str | None) -> None:
    entry: dict = {"type": rtype, ("url" if rtype == "git" else "path"): location}
    if commit:
        entry["commit"] = commit
    lock.setdefault("registries", {})[name] = entry


def record_package(lock: dict, ref: str, info: dict) -> None:
    """`ref` is the fully-qualified exact pin (ns/name@X.Y.Z); `info` carries kind + provenance
    (source/image/payload_sha256/requires as applicable), stored minus empty values."""
    lock.setdefault("packages", {})[ref] = {k: v for k, v in info.items() if v not in (None, {}, [])}


def record_instance(lock: dict, name: str, *, profile: str | None, base_sha256: str,
                    overlays: list[str] | None = None) -> None:
    row: dict = {"base_sha256": base_sha256}
    if profile:
        row["profile"] = profile
    if overlays:
        row["overlays"] = list(overlays)  # ORDER IS SEMANTIC — binding/merge order
    lock.setdefault("instances", {})[name] = row


def verify_payload(path: Path, want_sha256: str, *, what: str) -> None:
    got = sha256_file(path)
    if got != want_sha256:
        raise RigError(f"{what}: payload hash mismatch — expected {want_sha256[:12]}…, got "
                       f"{got[:12]}… ({path}); the registry content changed under the pin")
