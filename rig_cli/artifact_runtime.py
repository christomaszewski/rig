"""Run a bundled deployment after local identity resolution, without changing its image selection."""
from __future__ import annotations

from pathlib import Path
import subprocess

from . import RigError
from .common import load_yaml


def check_target(root: Path, manifest=None, *, check_arch=True):
    from .image_catalog import docker_arch
    from .manifest import machine_file
    meta = load_yaml(root / "metadata.yaml") if (root / "metadata.yaml").is_file() else {}
    target = meta.get("target") or {}
    if not target:
        return
    if check_arch and target.get("arch"):
        actual = docker_arch()
        if actual != target["arch"]:
            raise RigError(f"artifact requires {target['arch']}; Docker engine is {actual}")
    machine = load_yaml(machine_file()) if machine_file().is_file() else {}
    for key, expected in (("platform", target.get("runtime")), ("arch", target.get("arch"))):
        if expected and machine.get(key) and machine[key] != expected:
            raise RigError(f"artifact requires {key}={expected}; machine is provisioned as {machine[key]}")
        actual = getattr(manifest, key, None) if manifest else None
        if expected and actual and actual != expected:
            raise RigError(f"local {key}={actual} conflicts with artifact target {expected}")


def run_bundle(root, manifest, descriptors, env, *, dry_run=False):
    """Return None for ordinary deployments; bundled artifacts render then verify exact membership.

    A templated artifact cannot freeze Compose at bake time. It captures Compose on the vehicle
    after identity resolution, and rewrites every image to its verified offline alias. The same
    path handles flagged ./run.sh up and direct ./rig up for resolved artifacts.
    """
    meta_file = root / "metadata.yaml"
    if not meta_file.exists():
        return None
    meta = load_yaml(meta_file)
    bundle = meta.get("bundle") or {}
    if not bundle.get("refs"):
        return None
    from .bake import _compose_only, _write_scripts
    from .artifact_images import rewrite_compose
    check_target(root, manifest, check_arch=not dry_run)
    staging = root / "var" / "bundle-runtime"
    staging.mkdir(parents=True, exist_ok=True)
    entries, skipped = _compose_only(manifest, descriptors, env, staging, {}, pin=False, source_root=root)
    if skipped:
        raise RigError(f"bundle: failed to render {', '.join(skipped)}; no services started")
    rewrite_compose(staging, entries, bundle["refs"])
    _write_scripts(staging, entries)
    if dry_run:
        from .common import eprint
        eprint(f"bundle: validated {len(entries)} stacks against the bundled image set")
        return 0
    loaded = subprocess.run([str(root / "ensure-images.sh")])
    if loaded.returncode:
        return loaded.returncode
    return subprocess.run([str(staging / "up.sh")]).returncode
