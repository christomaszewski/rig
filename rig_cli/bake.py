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
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path

import yaml

from . import RigError, __version__
from .common import eprint, load_yaml
from .manifest import project_name, stack_summary
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


def _resolve_digest(ref: str, *, local_only: bool = False) -> str | None:
    """A pinned ``repo@sha256:…`` for an image ref. Tries the local image's RepoDigests first, then the
    registry via ``docker buildx imagetools`` (so a tag pushed to your local/offline registry pins to its
    content digest). Returns None if neither resolves (then the ref stays a tag). ``local_only`` skips the
    registry round-trip — bundle mode records digests as audit metadata only, and must not stall on an
    unreachable registry (the whole point of bundling)."""
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
    if local_only:
        return None
    try:  # 2. registry manifest digest (multi-arch index digest -> the vehicle pulls the right arch)
        proc = subprocess.run(["docker", "buildx", "imagetools", "inspect", ref,
                               "--format", "{{.Manifest.Digest}}"], capture_output=True, text=True)
        dig = proc.stdout.strip()
        if proc.returncode == 0 and dig.startswith("sha256:"):
            return f"{repo}@{dig}"
    except Exception:  # noqa: BLE001
        pass
    return None


def _bundle_images(staging: Path, refs: list[str]) -> dict:
    """``docker save`` the resolved image set into staging/images.tar (one multi-ref save, so shared base
    layers are stored once). Missing images are pulled first; still-missing is a HARD error — a silently
    incomplete bundle would betray the air-gap promise. Returns the metadata block."""
    def _have(ref: str) -> bool:
        return subprocess.run(["docker", "image", "inspect", ref], capture_output=True).returncode == 0

    missing = [r for r in refs if not _have(r)]
    for ref in missing:
        eprint(f"  bundle: image not in the local store, pulling {ref}")
        subprocess.run(["docker", "pull", ref], capture_output=True, text=True)
    still = [r for r in missing if not _have(r)]
    if still:
        raise RigError(f"bake --bundle-images: not in the local store and not pullable: {still} "
                       f"(run `rig build` / `rig pull` first, or check the registry)")
    tar = staging / "images.tar"
    eprint(f"  bundle: docker save {len(refs)} image(s) -> images.tar (can take minutes)")
    proc = subprocess.run(["docker", "save", "-o", str(tar), *refs], capture_output=True, text=True)
    if proc.returncode != 0 or not tar.exists():
        raise RigError(f"bake --bundle-images: docker save failed: {(proc.stderr or '').strip()[:200]}")
    ids = {}
    for ref in refs:
        p = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref],
                           capture_output=True, text=True)
        if p.returncode == 0:
            ids[ref] = p.stdout.strip()
    return {"file": "images.tar", "size_bytes": tar.stat().st_size, "image_ids": ids}


# --- bake -------------------------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_guard(refs: list[str]) -> list[str]:
    """sh block: load the bundled images.tar iff any bundled ref is absent from the local store —
    so the first `up` on an air-gapped vehicle self-loads, and every later run skips the (slow) load."""
    quoted = " ".join(shlex.quote(r) for r in refs)
    return [
        "if [ -f ./images.tar ]; then",
        "  need=0",
        f"  for img in {quoted}; do",
        '    docker image inspect "$img" >/dev/null 2>&1 || { need=1; break; }',
        "  done",
        '  [ "$need" = 1 ] && { echo "loading bundled images (one-time) ..." >&2; docker load -i ./images.tar; }',
        "fi",
    ]


def _write_scripts(staging: Path, entries: list[dict], *, bundle_refs: list[str] | None = None) -> None:
    up = ["#!/usr/bin/env sh", "set -e", "cd \"$(dirname \"$0\")\""]
    if bundle_refs:
        up += _load_guard(bundle_refs)
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
    # pull: prime the vehicle's image cache while the registry is reachable — touches NO containers,
    # so it's safe against a running deployment (unlike `up`, which recreates changed services).
    pull = ["#!/usr/bin/env sh", "set -e", "cd \"$(dirname \"$0\")\""]
    for e in entries:
        pull.append(f'docker compose -p "{e["project"]}" -f "{e["compose"]}" pull')
    scripts = [("up.sh", up), ("down.sh", down), ("status.sh", status), ("pull.sh", pull)]
    if bundle_refs:  # explicit/forced load (up.sh already self-loads when refs are missing)
        scripts.append(("load.sh", ["#!/usr/bin/env sh", "set -e", "cd \"$(dirname \"$0\")\"",
                                    "exec docker load -i ./images.tar"]))
    for fname, lines in scripts:
        path = staging / fname
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o755)


def _write_bootstrap(staging: Path) -> None:
    boot = staging / "run.sh"
    boot.write_text(
        "#!/usr/bin/env sh\n"
        "# Run the baked deployment. The compose-only scripts are registry-pinned + build-stripped (they\n"
        "# PULL images, never build) -> prefer them for up/down/status. rig (the vendored launchers, which\n"
        "# may build from source) is the fallback for other verbs / a missing compose-only form, and the\n"
        "# mutable path: after editing a config, run `rig up` directly to re-render.\n"
        'cd "$(dirname "$0")"\n'
        'verb="${1:-up}"\n'
        'case "$verb" in\n'
        "  up)     [ -f ./up.sh ]     && exec ./up.sh ;;\n"
        "  down)   [ -f ./down.sh ]   && exec ./down.sh ;;\n"
        "  status) [ -f ./status.sh ] && exec ./status.sh ;;\n"
        "  pull)   [ -f ./pull.sh ]   && exec ./pull.sh ;;\n"
        "  load)   [ -f ./load.sh ]   && exec ./load.sh ;;\n"
        "esac\n"
        "if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' >/dev/null 2>&1; then\n"
        '  exec python3 ./rig "$verb"\n'
        "fi\n"
        'echo "run.sh: $verb needs the compose-only scripts or rig (python3 + pyyaml)" >&2\n'
        "exit 1\n"
    )
    boot.chmod(0o755)


def _compose_only(manifest, descriptors, env, staging: Path, images: dict, *, pin: bool = True) -> list[dict]:
    """Per sensor: run the VENDORED launcher's `config` verb, capture the resolved compose, transform it for
    portable/offline use, and write compose/<name>/. Best-effort — a sensor whose launcher can't run is
    skipped (the rig-runnable tree still ships). ``pin=False`` (bundle mode) keeps tag refs: ``docker load``
    can't restore registry digests, so a bundled artifact's integrity is its own sha256, not @sha256 pins —
    digests are still collected (local-only) as audit metadata."""
    entries: list[dict] = []
    # Images declared `mirror:` in a rigging.yaml are kept as their registry TAG, not digest-pinned: a
    # mirrored multi-arch tag's digest is fragile (index vs per-arch manifest, re-push churn). Built images
    # (single-arch, stable digest) are still pinned.
    mirrored = {_repo_of(m) for d in descriptors.values() for m in d.mirror}
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
            r = _repo_of(ref)
            if any(r == m or r.endswith("/" + m) for m in mirrored):
                images.setdefault(ref, None)  # mirrored third-party -> keep the registry tag (pullable, stable)
            else:
                images.setdefault(ref, _resolve_digest(ref, local_only=not pin))
        if pin:
            _pin_images(compose, images)
        ext = _external_volume_names(compose)
        outdir = staging / "compose" / sensor.name
        outdir.mkdir(parents=True, exist_ok=True)
        _localize_binds(compose, outdir, staging)
        (outdir / "docker-compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False))
        entries.append({
            "sensor": sensor.name,
            "project": project_name(sensor.name, manifest.vehicle_id),
            "compose": f"compose/{sensor.name}/docker-compose.yaml",
            "external_volumes": ext,
        })
    return entries


def bake(root: Path, manifest, catalog, descriptors, env, tag: str, *, registry: str | None = None,
         bundle_images: bool = False) -> Path:
    if registry:
        env = {**env, "RIG_IMAGE_REGISTRY": registry}  # override vehicle.yaml images.registry for this bake
    staging = root / "var" / "bake" / tag
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    # 1. rig itself (so the artifact is self-contained for the Python path). The tool may live in a
    #    different dir than the deployment root (the `rig init` layout), so source it from THIS package,
    #    not from `root`. tool_root holds `rig` + `rig_cli/` (== root in the classic single-repo layout).
    tool_root = Path(__file__).resolve().parent.parent
    shutil.copy2(tool_root / "rig", staging / "rig")
    (staging / "rig").chmod(0o755)
    shutil.copytree(tool_root / "rig_cli", staging / "rig_cli",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 2. resolved per-sensor configs + a COMPLETE resolved vehicle.yaml (overrides/profiles already baked
    #    in; images / vehicle_id / infra+sensor tiers preserved so a `rig up` on the unbaked tree exports
    #    the same fleet env — RIG_IMAGE_REGISTRY/RIG_IMAGE_TAG/VEHICLE_ID/ROS_DOMAIN_ID).
    cfg_dir = staging / "config" / "sensors"
    cfg_dir.mkdir(parents=True)
    infra_rows, sensor_rows = [], []
    for s in manifest.sensors:
        shutil.copy2(s.config, cfg_dir / f"{s.name}.yaml")
        row = {"name": s.name, "service": s.service,
               "config": f"config/sensors/{s.name}.yaml", "enabled": s.enabled, "order": s.order}
        (infra_rows if s.tier == "infra" else sensor_rows).append(row)
    veh: dict = {"vehicle": manifest.vehicle}
    if manifest.vehicle_id is not None:
        veh["vehicle_id"] = manifest.vehicle_id
    veh["ros"] = {"domain_id": manifest.ros.domain_id, "rmw": manifest.ros.rmw, "distro": manifest.ros.distro}
    eff_registry = registry or manifest.image_registry
    if eff_registry or manifest.image_tag:
        veh["images"] = {k: v for k, v in (("registry", eff_registry), ("tag", manifest.image_tag)) if v}
    if manifest.data_dir:
        veh["data_dir"] = manifest.data_dir
    if infra_rows:
        veh["infra"] = infra_rows
    veh["sensors"] = sensor_rows
    (staging / "vehicle.yaml").write_text(yaml.safe_dump(veh, sort_keys=False))

    # 3. vendor each service's launch surface in + a catalog that points at them
    catalog_out = {}
    for service in sorted({s.service for s in manifest.sensors}):
        vendor(service, catalog[service].path, staging)
        catalog_out[service] = {"path": f"services/{service}"}
    (staging / "services.yaml").write_text(yaml.safe_dump({"services": catalog_out}, sort_keys=False))

    # 4. compose-only resolved form (best-effort) + scripts + bootstrap. Bundle mode also `docker save`s
    #    the image set into the artifact: zero registry at deploy time, integrity = the artifact's sha256.
    images: dict[str, str | None] = {}
    entries = _compose_only(manifest, descriptors, env, staging, images, pin=not bundle_images)
    bundle = None
    if bundle_images:
        if not images:
            raise RigError("bake --bundle-images: no images captured — the compose-only rendering must "
                           "succeed for every stack you want bundled (see the skip messages above)")
        bundle = _bundle_images(staging, sorted(images))
    if entries:
        _write_scripts(staging, entries, bundle_refs=sorted(images) if bundle else None)
    _write_bootstrap(staging)

    # 5. metadata + lock. A re-bake INSIDE an extracted artifact (field edits on the vehicle) records its
    #    parent, so save-points chain: test2 -> test2+edits -> day3-final.
    parent = None
    if (root / "metadata.yaml").exists():
        pmeta = load_yaml(root / "metadata.yaml")
        parent = {k: pmeta[k] for k in ("tag", "vehicle", "created", "rig_version") if pmeta.get(k)}
        if pmeta.get("parent"):  # keep one hop only; the full chain lives across the artifacts themselves
            parent["parent_tag"] = (pmeta["parent"] or {}).get("tag")
        if pmeta.get("sources"):
            parent["sources"] = pmeta["sources"]
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
        "data_dir": manifest.data_dir,
        "pinning": "tag+bundle" if bundle else "digest",
        "sensors": [s.name for s in manifest.sensors],
        "compose_only": [e["sensor"] for e in entries],
        "sources": sources,
        "images": {ref: dig for ref, dig in images.items()},
    }
    if bundle:
        meta["bundle"] = bundle
    if parent:
        meta["parent"] = parent
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
    pin_note = (f"{len(images)} images BUNDLED ({bundle['size_bytes'] / 1e9:.1f} GB tar; tag-pinned, "
                f"integrity = the artifact sha256)" if bundle
                else f"{pinned}/{len(images)} images digest-pinned")
    lineage = f" · parent: {parent['tag']}" if parent else ""
    eprint(f"  {stack_summary(manifest.sensors)} · {len(entries)} compose-only · {pin_note}{lineage}")
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
