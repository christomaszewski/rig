"""``rig vendor`` — copy a service's declared *launch surface* into the rig repo (``services/<service>/``),
with a provenance stamp, so the rig repo is self-contained: a vehicle gets the few small launch files
(launcher + compose + render helper + rigging.yaml) and pulls the runtime image — never the driver source,
no submodules.

The source repo declares its own surface in ``rigging.yaml``::

    launch_surface:
      - novatel-up
      - tools/render_params.py
      - docker/compose/compose.deploy.yaml
      - docker/compose/compose.deploy.serial.yaml

Re-run to update (the vendored dir is a derived mirror — edit the source, not the copy)."""
from __future__ import annotations

import datetime
import shutil
import subprocess
from pathlib import Path

import yaml

from . import RigError
from .common import eprint, load_yaml
from .descriptor import find_descriptor


def _git_ref(repo: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
        )
        return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None
    except Exception:  # noqa: BLE001 — provenance is best-effort
        return None


def vendor(service: str, source: Path, root: Path) -> Path:
    """Copy ``service``'s launch surface from ``source`` into ``<root>/services/<service>/``."""
    source = source.resolve()
    descriptor = find_descriptor(source)
    if descriptor is None:
        raise RigError(f"vendor {service}: no rigging.yaml in {source}")
    data = load_yaml(descriptor)
    declared = data.get("service")
    if declared is not None and declared != service:
        raise RigError(f"vendor {service}: {descriptor} declares service '{declared}'")

    surface = list(data.get("launch_surface") or [])
    if not surface:
        raise RigError(
            f"vendor {service}: {descriptor} has no `launch_surface` — the repo isn't vendor-ready "
            f"(add a launch_surface: list of the files rig needs to launch it)"
        )
    files = list(dict.fromkeys(surface + [descriptor.name]))  # always include the descriptor, dedup, keep order

    target = root / "services" / service
    if target.exists() and not (target / ".vendored.yaml").exists():
        raise RigError(f"vendor {service}: {target} exists and isn't a vendored dir; remove it first")
    if target.exists():
        shutil.rmtree(target)  # refresh a prior vendor in full
    target.mkdir(parents=True)

    for rel in files:
        src = source / rel
        if not src.exists():
            raise RigError(f"vendor {service}: launch_surface entry missing in source: {src}")
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)  # a surface may list a dir (e.g. a static bundle)
        else:
            shutil.copy2(src, dst)

    stamp = {
        "service": service,
        "source": str(source),
        "ref": _git_ref(source),
        "files": files,
        "when": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    with open(target / ".vendored.yaml", "w") as handle:
        yaml.safe_dump(stamp, handle, sort_keys=False)

    eprint(f"vendored {service}: {len(files)} files -> services/{service}  (ref {stamp['ref'] or 'n/a'})")
    return target
