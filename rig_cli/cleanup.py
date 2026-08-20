"""``rig cleanup`` — remove THIS deployment's docker footprint (images + volumes) from the host.

The decommission sweep: run it AFTER the final ``rig down`` and BEFORE deleting the deployment
tree. It refuses to run while any of the deployment's compose projects still has containers
(running OR stopped) — cleanup never kills anything; ``rig down`` owns containers. ``down
--purge`` remains the routine final-teardown volume GC; cleanup is the stronger form for "this
deployment is leaving the machine": the pulled images go too, plus compose-project volume residue.

What it removes, and what it never touches:

- **Images**: the union of every instance's rendered compose refs — the launcher's ``config``
  verb under the same per-service env ``up`` exports (composed ``<tag>-<platform>`` refs
  included), disabled instances too (decommission covers the whole tree) — plus rig.lock's
  ``images`` section (tag refs AND digest pins, so a bake-primed cache is fully untagged).
  Removal is by REF (``docker rmi``, never ``-f``, never by ID): an image still used by another
  project's containers is refused by docker and reported — the same in-use safety the volume
  purge relies on. One caveat no client can solve: two deployments pulling the SAME ref share
  one tag in the daemon, so cleaning one untags it for both (the other re-pulls; SIL trees on a
  shared dev box are where this bites — ``--dry-run`` first).
- **Volumes** (unless ``--keep-volumes``): the rigging.yaml-declared ``external_volumes`` (the
  exact set ``down --purge`` covers — idempotent with it) plus any volume labeled with an
  instance's compose project (launcher-side named volumes that a ``down`` without ``-v`` left
  behind). ``docker volume rm`` refusing an in-use volume is the safety, as ever.
- **Never**: RIG_DATA_DIR (runs/recordings are data, not residue), networks (project networks
  die with ``down``; the SIL fleet network is ``fleet down``'s), anything of another deployment's
  by name.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml

from . import RigError
from .bake import _service_images
from .common import eprint
from .descriptor import Descriptor
from .dispatch import service_env
from .manifest import Manifest, Sensor, project_name


def _docker(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def rendered_images(sensors: list[Sensor], descriptors: dict[str, Descriptor],
                    env: dict[str, str]) -> tuple[set[str], list[str]]:
    """Every image ref the selected instances' rendered composes pull — the launcher's `config`
    verb under the per-service fleet env, exactly what `up`/`pull` would fetch. Best-effort per
    stack (a launcher that can't render is a note, not an abort — the lock still contributes)."""
    refs: set[str] = set()
    notes: list[str] = []
    for sensor in sensors:
        desc = descriptors[sensor.service]
        cmd = [str(desc.launcher_path), str(sensor.config), *desc.verb_args("config")]
        try:
            proc = subprocess.run(cmd, env=service_env(env, desc), cwd=str(desc.repo),
                                  capture_output=True, text=True, timeout=60)
            compose = yaml.safe_load(proc.stdout) if proc.returncode == 0 else None
        except Exception as exc:  # noqa: BLE001
            proc, compose = None, None
            notes.append(f"{sensor.name}: config render failed ({exc}) — its refs come from "
                         f"rig.lock only")
        if isinstance(compose, dict) and compose.get("services"):
            refs |= set(_service_images(compose).values())
        elif proc is not None:
            notes.append(f"{sensor.name}: config render failed "
                         f"({(proc.stderr or '').strip()[:120] or 'no compose on stdout'}) — "
                         f"its refs come from rig.lock only")
    return refs, notes


def lock_images(root: Path) -> set[str]:
    """rig.lock's `images` section: tag refs (keys) AND digest pins (values) — a digest-primed
    cache (baked artifact) holds both names for the same image; both must be untagged."""
    from .lock import load_lock
    refs: set[str] = set()
    for ref, digest in (load_lock(root).get("images") or {}).items():
        refs.add(str(ref))
        if digest:
            refs.add(str(digest))
    return refs


def declared_volumes(sensors: list[Sensor], descriptors: dict[str, Descriptor]) -> set[str]:
    """The rigging.yaml-declared external volumes for the selected instances — the same set
    `down --purge` removes."""
    return {pattern.format(name=s.name)
            for s in sensors for pattern in descriptors[s.service].external_volumes}


def _projects_with_containers(projects: list[str]) -> dict[str, int]:
    """Compose projects that still have containers, running OR stopped (a stopped container
    still pins its image — and it is `rig down`'s to remove, never cleanup's)."""
    found: dict[str, int] = {}
    for project in projects:
        proc = _docker(["ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"])
        count = len(proc.stdout.split()) if proc.returncode == 0 else 0
        if count:
            found[project] = count
    return found


def _project_volumes(project: str) -> list[str]:
    """Volumes docker labeled with this compose project — launcher-side named volumes a `down`
    without -v left behind. Label-scoped, so another deployment's volumes are untouchable here."""
    proc = _docker(["volume", "ls", "-q", "--filter",
                    f"label=com.docker.compose.project={project}"])
    return proc.stdout.split() if proc.returncode == 0 else []


def cleanup(root: Path, manifest: Manifest, descriptors: dict[str, Descriptor],
            env: dict[str, str], *, names: list[str], dry_run: bool = False,
            keep_volumes: bool = False) -> int:
    sensors = manifest.select(names, enabled_only=False)  # decommission covers disabled rows too
    if not sensors:
        eprint("rig cleanup: no instances selected — nothing to remove")
        return 0
    projects = [project_name(s.name, manifest.vehicle_id) for s in sensors]

    have_docker = shutil.which("docker") is not None
    if not have_docker and not dry_run:
        raise RigError("cleanup: docker not found on PATH — nothing to clean without a daemon")

    # The gate: containers (even stopped ones) pin images and belong to `rig down`.
    if have_docker and not dry_run:
        in_use = _projects_with_containers(projects)
        if in_use:
            listed = ", ".join(f"{p} ({n})" for p, n in sorted(in_use.items()))
            raise RigError(f"cleanup: container(s) still exist for: {listed} — run "
                           f"`rig down{'' if keep_volumes else ' --purge'}` first "
                           f"(cleanup removes images/volumes, never containers)")

    refs, notes = rendered_images(sensors, descriptors, env)
    refs |= lock_images(root)
    for note in notes:
        eprint(f"  note: {note}")
    volumes = set() if keep_volumes else declared_volumes(sensors, descriptors)

    scope = f"{len(sensors)} instance(s)" + (f" [{', '.join(names)}]" if names else "")
    eprint(f"rig cleanup: {manifest.vehicle} — {scope}, {len(refs)} image ref(s)"
           + ("" if keep_volumes else f", {len(volumes)} declared volume(s) + project residue"))

    if dry_run:
        for ref in sorted(refs):
            eprint(f"  would rmi {ref}")
        for vol in sorted(volumes):
            eprint(f"  would rm volume {vol}")
        if not keep_volumes:
            for project in projects:
                eprint(f"  would rm volumes labeled com.docker.compose.project={project}")
        eprint("rig cleanup (dry-run): nothing removed — RIG_DATA_DIR is never touched")
        return 0

    removed = kept = 0
    for ref in sorted(refs):
        proc = _docker(["rmi", ref])
        if proc.returncode == 0:
            removed += 1
            eprint(f"  removed image {ref}")
        elif "No such image" in (proc.stderr or ""):
            pass  # never pulled here — silence, absence is the goal state
        else:
            kept += 1
            eprint(f"  kept image {ref} "
                   f"({(proc.stderr or '').strip().splitlines()[-1][:100] if proc.stderr else 'in use?'})")

    vol_removed = vol_kept = 0
    if not keep_volumes:
        for project in projects:  # labeled residue first: project volumes a down -v would have taken
            volumes.update(_project_volumes(project))
        for vol in sorted(volumes):
            proc = _docker(["volume", "rm", vol])
            if proc.returncode == 0:
                vol_removed += 1
                eprint(f"  removed volume {vol}")
            elif "no such volume" in (proc.stderr or "").lower():
                pass
            else:
                vol_kept += 1
                eprint(f"  kept volume {vol} (in use — a consumer may still be attached)")

    eprint(f"rig cleanup: {removed} image(s) removed"
           + (f", {kept} kept (in use)" if kept else "")
           + ("" if keep_volumes else f", {vol_removed} volume(s) removed"
              + (f", {vol_kept} kept" if vol_kept else ""))
           + " — RIG_DATA_DIR untouched; the tree is now safe to delete")
    return 0
