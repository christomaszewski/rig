"""``rig bake`` / ``rig unbake`` — freeze the live deployment into a tagged, content-addressed artifact, and
restore it.

bake snapshots the *resolved* deployment tree (rig itself + resolved per-sensor configs + vendored launch
surfaces + metadata) AND compiles a **compose-only** resolved form (each sensor's ``docker compose config``
output + flat ``up.sh``/``down.sh``/``status.sh``) so the artifact runs with just Docker when Python/PyYAML
are absent. A ``run.sh`` bootstrap uses rig when present, else the static scripts. unbake extracts an
artifact back to an editable tree.

Best-effort, host-dependent steps degrade gracefully: if a launcher's ``config`` verb can't run (no Docker)
the artifact still ships the rig-runnable tree; image digests are pinned where resolvable, else left as tags.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import yaml

from . import RigError, __version__
from .common import eprint, load_yaml
from .vendor import vendor


# --- compose transforms (operate on the parsed `docker compose config` output) ------------------

def _services(compose: dict):
    return (compose.get("services") or {}).items()


def _strip_build(compose: dict) -> None:
    """A vehicle pulls images; it never builds. Drop build contexts (gige's core-driver carries one)."""
    for _, svc in _services(compose):
        svc.pop("build", None)


def _service_images(compose: dict) -> dict[str, str]:
    return {name: svc["image"] for name, svc in _services(compose) if svc.get("image")}


def _external_volume_names(compose: dict) -> list[str]:
    names = []
    for key, vol in (compose.get("volumes") or {}).items():
        if isinstance(vol, dict) and vol.get("external"):
            names.append(vol.get("name") or key)
    return names


def _strip_profiles(compose: dict) -> None:
    """`docker compose config` already filtered to the active profile set; drop the `profiles:` markers so a
    plain `docker compose up` (no COMPOSE_PROFILES) starts exactly those services."""
    for _, svc in _services(compose):
        svc.pop("profiles", None)


def _localize_binds(compose: dict, dest: Path, staging_root: Path) -> None:
    """Make the project self-contained: any bind whose source is a path bake CREATED (under the staging root
    — rendered params, the vendored repo's relative dirs like core-driver/config and recordings) is copied
    (files) or placeheld (dirs) into the project dir and rewritten relative. Genuine host paths (/dev/*,
    /tmp/gige, a /data partition) are NOT under the staging root, so they're left literal to resolve on the
    vehicle."""
    for sname, svc in _services(compose):
        for vol in svc.get("volumes") or []:
            if not (isinstance(vol, dict) and vol.get("type") == "bind"):
                continue
            src = Path(str(vol.get("source", "")))
            try:
                under_staging = src.is_relative_to(staging_root)
            except (ValueError, OSError):
                under_staging = False
            if not under_staging:
                continue  # a real host path on the vehicle — leave literal
            relname = f"{sname}__{src.name}"
            target = dest / relname
            if src.is_file():
                shutil.copy2(src, target)
            elif src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True)  # vendored dir (e.g. a static web bundle)
            else:
                target.mkdir(parents=True, exist_ok=True)  # missing -> empty placeholder (config, recordings)
            vol["source"] = f"./{relname}"


def _pin_images(compose: dict, digests: dict[str, str | None]) -> None:
    for _, svc in _services(compose):
        ref = svc.get("image")
        if ref and digests.get(ref):
            svc["image"] = digests[ref]


def _repo_of(ref: str) -> str:
    """The repo part of an image ref, dropping any :tag or @digest (handles host:port/ refs)."""
    ref = ref.split("@", 1)[0]
    colon, slash = ref.rfind(":"), ref.rfind("/")
    return ref[:colon] if colon > slash else ref


def _resolve_digest(ref: str) -> str | None:
    """A pinned ``repo@sha256:…`` for an image ref. Tries the local image's RepoDigests first, then the
    registry via ``docker buildx imagetools`` (so a tag pushed to your local/offline registry pins to its
    content digest). Returns None if neither resolves (then the ref stays a tag)."""
    repo = _repo_of(ref)
    try:  # 1. local image's repo digest
        proc = subprocess.run(["docker", "inspect", "--format", "{{json .RepoDigests}}", ref],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            for rd in json.loads(proc.stdout or "[]"):
                if rd.startswith(repo + "@sha256:"):
                    return rd
    except Exception:  # noqa: BLE001
        pass
    try:  # 2. registry manifest digest (multi-arch index digest -> the vehicle pulls the right arch)
        proc = subprocess.run(["docker", "buildx", "imagetools", "inspect", ref,
                               "--format", "{{.Manifest.Digest}}"], capture_output=True, text=True)
        dig = proc.stdout.strip()
        if proc.returncode == 0 and dig.startswith("sha256:"):
            return f"{repo}@{dig}"
    except Exception:  # noqa: BLE001
        pass
    return None


# --- bake -------------------------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_scripts(staging: Path, entries: list[dict]) -> None:
    up = ["#!/usr/bin/env sh", "set -e", "cd \"$(dirname \"$0\")\""]
    for e in entries:
        for vol in e["external_volumes"]:
            up.append(f'docker volume create "{vol}" >/dev/null')
        up.append(f'docker compose -p "{e["project"]}" -f "{e["compose"]}" up -d')
    down = ["#!/usr/bin/env sh", "cd \"$(dirname \"$0\")\""]
    for e in reversed(entries):
        down.append(f'docker compose -p "{e["project"]}" -f "{e["compose"]}" down')
    status = ["#!/usr/bin/env sh", "cd \"$(dirname \"$0\")\""]
    for e in entries:
        status.append(f'echo "== {e["sensor"]} ({e["project"]}) =="')
        status.append(f'docker compose -p "{e["project"]}" -f "{e["compose"]}" ps')
    for fname, lines in (("up.sh", up), ("down.sh", down), ("status.sh", status)):
        path = staging / fname
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o755)


def _write_bootstrap(staging: Path) -> None:
    boot = staging / "run.sh"
    boot.write_text(
        "#!/usr/bin/env sh\n"
        "# Bring the vehicle up: use rig if Python+PyYAML are present, else the static compose scripts.\n"
        'cd "$(dirname "$0")"\n'
        "verb=\"${1:-up}\"\n"
        "if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' >/dev/null 2>&1; then\n"
        '  exec python3 ./rig "$verb"\n'
        "fi\n"
        'case "$verb" in\n'
        "  up) exec ./up.sh ;; down) exec ./down.sh ;; status) exec ./status.sh ;;\n"
        '  *) echo "rig (python) unavailable; use ./up.sh | ./down.sh | ./status.sh" >&2; exit 1 ;;\n'
        "esac\n"
    )
    boot.chmod(0o755)


def _compose_only(manifest, descriptors, env, staging: Path, images: dict) -> list[dict]:
    """Per sensor: run the VENDORED launcher's `config` verb, capture the resolved compose, transform it for
    portable/offline use, and write compose/<name>/. Best-effort — a sensor whose launcher can't run is
    skipped (the rig-runnable tree still ships)."""
    entries: list[dict] = []
    for sensor in manifest.sensors:
        desc = descriptors[sensor.service]
        repo = staging / "services" / sensor.service
        launcher = repo / desc.launcher
        config = staging / "config" / "sensors" / f"{sensor.name}.yaml"
        cmd = [str(launcher), str(config), *desc.verb_args("config")]
        try:
            proc = subprocess.run(cmd, env=env, cwd=str(repo), capture_output=True, text=True)
            if proc.returncode != 0 or not proc.stdout.strip():
                eprint(f"  compose-only: skip {sensor.name} (launcher config failed: "
                       f"{(proc.stderr or '').strip()[:140]})")
                continue
            compose = yaml.safe_load(proc.stdout)
        except Exception as exc:  # noqa: BLE001
            eprint(f"  compose-only: skip {sensor.name} ({exc})")
            continue

        _strip_build(compose)
        _strip_profiles(compose)
        for ref in _service_images(compose).values():
            images.setdefault(ref, _resolve_digest(ref))
        _pin_images(compose, images)
        ext = _external_volume_names(compose)
        outdir = staging / "compose" / sensor.name
        outdir.mkdir(parents=True, exist_ok=True)
        _localize_binds(compose, outdir, staging)
        (outdir / "docker-compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False))
        entries.append({
            "sensor": sensor.name,
            "project": f"{sensor.service}_{sensor.name}",
            "compose": f"compose/{sensor.name}/docker-compose.yaml",
            "external_volumes": ext,
        })
    return entries


def bake(root: Path, manifest, catalog, descriptors, env, tag: str, *, registry: str | None = None) -> Path:
    if registry:
        env = {**env, "RIG_IMAGE_REGISTRY": registry}  # override vehicle.yaml images.registry for this bake
    staging = root / "var" / "bake" / tag
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    # 1. rig itself (so the artifact is self-contained for the Python path)
    shutil.copy2(root / "rig", staging / "rig")
    (staging / "rig").chmod(0o755)
    shutil.copytree(root / "rig_cli", staging / "rig_cli",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 2. resolved per-sensor configs + a resolved vehicle.yaml (overrides/profiles already baked in)
    cfg_dir = staging / "config" / "sensors"
    cfg_dir.mkdir(parents=True)
    rows = []
    for s in manifest.sensors:
        shutil.copy2(s.config, cfg_dir / f"{s.name}.yaml")
        rows.append({"name": s.name, "service": s.service,
                     "config": f"config/sensors/{s.name}.yaml", "enabled": s.enabled, "order": s.order})
    (staging / "vehicle.yaml").write_text(yaml.safe_dump(
        {"vehicle": manifest.vehicle,
         "ros": {"domain_id": manifest.ros.domain_id, "rmw": manifest.ros.rmw, "distro": manifest.ros.distro},
         "sensors": rows}, sort_keys=False))

    # 3. vendor each service's launch surface in + a catalog that points at them
    catalog_out = {}
    for service in sorted({s.service for s in manifest.sensors}):
        vendor(service, catalog[service].path, staging)
        catalog_out[service] = {"path": f"services/{service}"}
    (staging / "services.yaml").write_text(yaml.safe_dump({"services": catalog_out}, sort_keys=False))

    # 4. compose-only resolved form (best-effort) + scripts + bootstrap
    images: dict[str, str | None] = {}
    entries = _compose_only(manifest, descriptors, env, staging, images)
    if entries:
        _write_scripts(staging, entries)
    _write_bootstrap(staging)

    # 5. metadata + lock
    sources = {}
    for svc_dir in sorted((staging / "services").glob("*")):
        stamp = svc_dir / ".vendored.yaml"
        if stamp.exists():
            d = load_yaml(stamp)
            sources[svc_dir.name] = {"source": d.get("source"), "ref": d.get("ref")}
    meta = {
        "tag": tag,
        "vehicle": manifest.vehicle,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "rig_version": __version__,
        "registry": registry or manifest.image_registry,
        "image_tag": manifest.image_tag,
        "sensors": [s.name for s in manifest.sensors],
        "compose_only": [e["sensor"] for e in entries],
        "sources": sources,
        "images": {ref: dig for ref, dig in images.items()},
    }
    (staging / "metadata.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))
    pinned = sum(1 for d in images.values() if d)
    (staging / "rig.lock").write_text(yaml.safe_dump(
        {"images": {ref: dig for ref, dig in images.items() if dig}}, sort_keys=False))

    # 6. bundle
    artifacts = root / "var" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    tarpath = artifacts / f"{tag}.tar.gz"
    with tarfile.open(tarpath, "w:gz") as tf:
        tf.add(staging, arcname=tag)
    digest = _sha256(tarpath)
    eprint(f"baked '{tag}' -> {tarpath}")
    eprint(f"  sha256:{digest}")
    eprint(f"  {len(manifest.sensors)} sensors · {len(entries)} compose-only · "
           f"{pinned}/{len(images)} images digest-pinned")
    return tarpath


def unbake(artifact: Path, into: Path) -> Path:
    if not artifact.exists():
        raise RigError(f"unbake: artifact not found: {artifact}")
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(artifact, "r:gz") as tf:
        try:
            tf.extractall(into, filter="data")  # py3.12+: refuse path traversal / unsafe members
        except TypeError:  # older Python
            tf.extractall(into)
    tops = sorted(p for p in into.iterdir() if p.is_dir())
    target = tops[-1] if tops else into
    eprint(f"unbaked {artifact.name} -> {target}")
    return target
