"""``rig bake`` / ``rig unbake`` — freeze the live deployment into a tagged, content-addressed artifact, and
restore it.

bake snapshots the *resolved* deployment tree (rig itself + resolved per-sensor configs + vendored launch
surfaces + metadata) AND compiles a **compose-only** resolved form (each sensor's ``docker compose config``
output + flat ``up.sh``/``down.sh``/``status.sh``/``pull.sh``, plus ``load.sh`` and an up.sh self-load
guard when ``--bundle-images`` is used) so the artifact runs with just Docker when Python/PyYAML are
absent. The ``run.sh`` bootstrap prefers the compose-only scripts (registry-pinned, never build) and falls
back to rig for other verbs or a missing compose-only form. unbake extracts an artifact back to an
editable tree.

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
    used: dict[str, Path] = {}  # localized name -> source; two SOURCES must never share a name
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
            if used.get(relname, src) != src:  # basename collision (a/params.yaml + b/params.yaml):
                #                                the second copy would silently OVERWRITE the first —
                #                                fall back to the full staging-relative path, flattened
                relname = f"{sname}__{'_'.join(src.relative_to(staging_root).parts)}"
            used[relname] = src
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


def _manifest_echo_lines(ctx: dict, indent: str = "  ") -> list[str]:
    """The provenance fields as sh echo lines (values shell-quoted — a vehicle name is data, not code).
    Mirrors runs.py's manifest so auto and labeled runs carry the same machine contract."""
    lines = [f'{indent}echo {shlex.quote("vehicle: " + str(ctx["vehicle"]))}']
    if ctx.get("vehicle_id") is not None:
        lines.append(f'{indent}echo {shlex.quote("vehicle_id: " + str(ctx["vehicle_id"]))}')
    lines.append(f'{indent}echo {shlex.quote("artifact: " + str(ctx["tag"]))}')
    lines.append(f'{indent}echo {shlex.quote("rig_version: " + __version__)}')
    lines.append(f'{indent}echo "started: $(date -u +%Y-%m-%dT%H:%M:%S+00:00)"')
    return lines


def _run_ensure_guard(ctx: dict) -> list[str]:
    """sh block for up.sh: make sure an open run exists (create `_auto` if absent). NEVER rotates —
    the safety net for reboots/systemd; deliberate sessions use new-run.sh / `rig up --run`.
    A DANGLING current (run dir deleted by hand) is healed here, matching runs.py's ensure."""
    d = shlex.quote(ctx["data_dir"])
    return [
        f'D={d}',
        'if [ -L "$D/current" ] && [ ! -e "$D/current" ]; then rm -f "$D/current"; fi',
        'if [ ! -e "$D/current" ]; then',
        '  id="$(date -u +%Y%m%dT%H%M%SZ)_auto"',
        '  n=2; while [ -e "$D/runs/$id" ]; do id="$(date -u +%Y%m%dT%H%M%SZ)_auto-$n"; n=$((n+1)); done',
        '  mkdir -p "$D/runs/$id"',
        '  {',
        '    echo "run: $id"',
        *_manifest_echo_lines(ctx, "    "),
        '  } > "$D/runs/$id/manifest.yaml"',
        '  ln -sfn "runs/$id" "$D/current"',
        '  echo "opened run $id (auto — label one with ./new-run.sh <label>)" >&2',
        'fi',
    ]


def _write_run_scripts(staging: Path, ctx: dict) -> None:
    """new-run.sh / end-run.sh / runs.sh — the run registry in pure sh (bare-Docker parity). data_dir and
    the guard's project list are inlined at bake time; manifest fields stay grep-able flat keys."""
    d = shlex.quote(ctx["data_dir"])
    projects = " ".join(shlex.quote(p) for p in ctx["projects"])
    guard = [
        'if [ "$FORCE" = 0 ]; then',
        f'  for p in {projects}; do',
        '    if docker compose -p "$p" ps -q 2>/dev/null | grep -q .; then',
        '      echo "$0: $p is running — ./down.sh first, or --force (late writes land in the sealed run)" >&2',
        '      exit 1',
        '    fi',
        '  done',
        'fi',
    ] if projects else []
    seal = [  # append ended: (+ size) to the open run's manifest — newline-safe (>> onto a file whose
        'if [ -L "$D/current" ] && [ -e "$D/current" ]; then',  # last line lacks \n would glue the stamp)
        '  old="$D/$(readlink "$D/current")"',
        '  if [ -f "$old/manifest.yaml" ]; then',
        '    if [ -n "$(tail -c1 "$old/manifest.yaml")" ]; then echo >> "$old/manifest.yaml"; fi',
        '    echo "ended: $(date -u +%Y-%m-%dT%H:%M:%S+00:00)" >> "$old/manifest.yaml"',
        '    kb=$(du -sk "$old" 2>/dev/null | cut -f1)',
        '    if [ -n "$kb" ]; then echo "disk_kb: $kb" >> "$old/manifest.yaml"; fi',
        '    echo "sealed run $(basename "$old")" >&2',
        '  fi',
        'fi',
    ]
    label_check = [  # mirror runs.py's _LABEL_RE — the label becomes a directory name
        'case "$LABEL" in',
        '  ""|*[!A-Za-z0-9_-]*|-*|_*) echo "invalid label \'$LABEL\' (use [A-Za-z0-9][A-Za-z0-9_-]*)" >&2; exit 2 ;;',
        'esac',
    ]
    new_run = (["#!/usr/bin/env sh", "set -e", f'D={d}',
                'FORCE=0; if [ "$1" = "--force" ]; then FORCE=1; shift; fi',
                'LABEL="${1:-auto}"']
               + label_check + guard + seal +
               ['id="$(date -u +%Y%m%dT%H%M%SZ)_$LABEL"',
                'n=2; while [ -e "$D/runs/$id" ]; do id="$(date -u +%Y%m%dT%H%M%SZ)_$LABEL-$n"; n=$((n+1)); done',
                'mkdir -p "$D/runs/$id"',
                '{',
                '  echo "run: $id"',
                '  if [ "$LABEL" != auto ]; then echo "label: \\"$LABEL\\""; fi',  # quoted: 123 stays a string
                *_manifest_echo_lines(ctx),
                '} > "$D/runs/$id/manifest.yaml"',
                'ln -sfn "runs/$id" "$D/current"',
                'echo "opened run $id" >&2'])
    end_run = (["#!/usr/bin/env sh", "set -e", f'D={d}',
                'FORCE=0; if [ "$1" = "--force" ]; then FORCE=1; shift; fi',
                'if [ ! -L "$D/current" ] || [ ! -e "$D/current" ]; then echo "no active run" >&2; exit 0; fi']
               + guard + seal + ['rm -f "$D/current"'])
    runs_sh = ["#!/usr/bin/env sh", f'D={d}',
               'cur=""; if [ -L "$D/current" ]; then cur="$(basename "$(readlink "$D/current")")"; fi',
               'for dir in "$D"/runs/*/; do',
               '  [ -d "$dir" ] || continue',
               '  id="$(basename "$dir")"',
               '  ended="$(sed -n \'s/^ended: //p\' "$dir/manifest.yaml" 2>/dev/null | head -1)"',
               '  state=sealed; [ -z "$ended" ] && state=interrupted; [ "$id" = "$cur" ] && state=OPEN',
               '  printf \'%s\\t%s\\t%s\\n\' "$id" "$state" "${ended:--}"',
               'done']
    for fname, lines in (("new-run.sh", new_run), ("end-run.sh", end_run), ("runs.sh", runs_sh)):
        path = staging / fname
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o755)


def _alias_lines(aliases: dict[str, str]) -> list[str]:
    """sh block: alias each digest-pinned image back to its ORIGINAL TAG in the local store. A digest
    pull doesn't create the tag name, so without this the launcher path (tag refs: `rig up --run`, field
    `./rig up`) misses a digest-primed cache — re-pulling online, FAILING offline. `docker tag` from a
    digest ref to a tag is legal (it's digest TARGETS docker refuses); best-effort by design."""
    lines = ["# unify the image namespace: digest pulls don't create tag names — alias them so the",
             "# launcher path (tag refs) hits the same local images, including fully offline."]
    for tag_ref, digest_ref in sorted(aliases.items()):
        lines.append(f"docker tag {shlex.quote(digest_ref)} {shlex.quote(tag_ref)} 2>/dev/null || true")
    return lines


def _cleanup_script(entries: list[dict], refs: list[str]) -> list[str]:
    """cleanup.sh — the decommission sweep in pure sh (bare-Docker parity with `rig cleanup`):
    refuse while any project still has containers, then untag this artifact's image refs and
    remove its external + project-labeled volumes. Data dirs are never touched."""
    projects = " ".join(shlex.quote(e["project"]) for e in entries)
    volumes = sorted({v for e in entries for v in e["external_volumes"]})
    lines = ["#!/usr/bin/env sh",
             "# Decommission: remove this artifact's images + volumes from the host. Run AFTER",
             "# ./down.sh, BEFORE deleting this directory. Never containers, never data dirs.",
             'cd "$(dirname "$0")"',
             f'for p in {projects}; do',
             '  if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$p")" ]; then',
             '    echo "cleanup: containers still exist for $p — ./down.sh first" >&2; exit 1',
             '  fi',
             'done']
    for ref in sorted(refs):
        q = shlex.quote(ref)
        lines += [f'if docker image inspect {q} >/dev/null 2>&1; then',
                  f'  docker rmi {q} >/dev/null 2>&1 && echo "removed image {ref}" '
                  f'|| echo "kept image {ref} (in use)"',
                  'fi']
    for vol in volumes:
        q = shlex.quote(vol)
        lines.append(f'docker volume rm {q} >/dev/null 2>&1 && echo "removed volume {vol}" || true')
    lines += [f'for p in {projects}; do',
              '  for v in $(docker volume ls -q --filter "label=com.docker.compose.project=$p"); do',
              '    docker volume rm "$v" >/dev/null 2>&1 && echo "removed volume $v" '
              '|| echo "kept volume $v (in use)"',
              '  done',
              'done',
              'echo "cleanup done — data dirs untouched; this directory is now safe to delete"']
    return lines


def _write_scripts(staging: Path, entries: list[dict], *, bundle_refs: list[str] | None = None,
                   run_ctx: dict | None = None, aliases: dict[str, str] | None = None,
                   cleanup_refs: list[str] | None = None) -> None:
    up = ["#!/usr/bin/env sh", "set -e", "cd \"$(dirname \"$0\")\""]
    if run_ctx:
        up += _run_ensure_guard(run_ctx)
    if bundle_refs:
        up += _load_guard(bundle_refs)
    for e in entries:
        for vol in e["external_volumes"]:
            up.append(f'docker volume create "{vol}" >/dev/null')
        up.append(f'docker compose -p "{e["project"]}" -f "{e["compose"]}" up -d')
    if aliases:  # after up: everything referenced is now local — leave the namespace unified
        up += _alias_lines(aliases)
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
    if aliases:
        pull += _alias_lines(aliases)
    if run_ctx:  # status header: the open run, in pure sh (after shebang+cd, before the stack tables)
        d = shlex.quote(run_ctx["data_dir"])
        status[2:2] = [f'D={d}',
                       'if [ -L "$D/current" ] && [ -e "$D/current" ]; then',  # dangling != open
                       '  echo "run: $(basename "$(readlink "$D/current")") (open)"',
                       'else echo "run: none active"; fi']
    scripts = [("up.sh", up), ("down.sh", down), ("status.sh", status), ("pull.sh", pull)]
    if cleanup_refs is not None:
        scripts.append(("cleanup.sh", _cleanup_script(entries, cleanup_refs)))
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
        '[ $# -eq 0 ] && set -- up\n'
        'verb="$1"\n'
        '# bare verbs run the static scripts; FLAGGED forms (up --run X, down --end-run) fall through to\n'
        '# the bundled rig, which owns those semantics.\n'
        'case "$verb" in\n'
        "  up)     [ $# -eq 1 ] && [ -f ./up.sh ]     && exec ./up.sh ;;\n"
        "  down)   [ $# -eq 1 ] && [ -f ./down.sh ]   && exec ./down.sh ;;\n"
        "  status) [ $# -eq 1 ] && [ -f ./status.sh ] && exec ./status.sh ;;\n"
        "  pull)   [ $# -eq 1 ] && [ -f ./pull.sh ]   && exec ./pull.sh ;;\n"
        "  cleanup) [ $# -eq 1 ] && [ -f ./cleanup.sh ] && exec ./cleanup.sh ;;\n"
        "  load)   [ -f ./load.sh ]   && exec ./load.sh ;;\n"
        '  new-run) if [ -f ./new-run.sh ]; then shift; exec ./new-run.sh "$@"; fi ;;\n'
        '  end-run) if [ -f ./end-run.sh ]; then shift; exec ./end-run.sh "$@"; fi ;;\n'
        "  runs)   [ -f ./runs.sh ]   && exec ./runs.sh ;;\n"
        "esac\n"
        "if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' >/dev/null 2>&1; then\n"
        '  exec python3 ./rig "$@"\n'
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
            from .dispatch import service_env  # platform routing: composed tag + override_env, same as up
            proc = subprocess.run(cmd, env=service_env(env, desc), cwd=str(repo),
                                  capture_output=True, text=True)
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


_RIG_SHIM = '''#!/usr/bin/env python3
"""rig — bundled entry point (staged by bake). Runs the rig_cli packaged alongside, so the
artifact needs NO rig installed on the vehicle (python3 + pyyaml only)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from rig_cli.cli import main  # noqa: E402

raise SystemExit(main())
'''


def _stage_rig(staging: Path) -> None:
    """Bundle rig into the artifact: the rig_cli package from WHEREVER it is imported
    (site-packages for brew/deb/pipx installs, the repo for checkouts) plus a GENERATED shim —
    an installed rig has no ./rig file beside the package (the console script is the
    installer's), which is exactly the layout the old copy-the-shim approach crashed on."""
    package_dir = Path(__file__).resolve().parent  # == the rig_cli package, any install layout
    shutil.copytree(package_dir, staging / "rig_cli",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (staging / "rig").write_text(_RIG_SHIM)
    (staging / "rig").chmod(0o755)


def bake(root: Path, manifest, catalog, descriptors, env, tag: str, *, registry: str | None = None,
         bundle_images: bool = False) -> Path:
    if registry:
        env = {**env, "RIG_IMAGE_REGISTRY": registry}  # override vehicle.yaml images.registry for this bake
    staging = root / "var" / "bake" / tag
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    # 1. rig itself (so the artifact is self-contained for the Python path).
    _stage_rig(staging)

    # 2. resolved per-sensor configs + a COMPLETE resolved vehicle.yaml (overrides/profiles already baked
    #    in; images / vehicle_id / all three tiers preserved so a `rig up` on the unbaked tree exports
    #    the same fleet env — RIG_IMAGE_REGISTRY/RIG_IMAGE_TAG/VEHICLE_ID/ROS_DOMAIN_ID — and keeps the
    #    tier ordering (infra first, autonomy last / down-first)).
    cfg_dir = staging / "config" / "sensors"
    cfg_dir.mkdir(parents=True)
    tier_rows: dict[str, list] = {"infra": [], "sensor": [], "autonomy": []}
    for s in manifest.sensors:
        shutil.copy2(s.config, cfg_dir / f"{s.name}.yaml")
        tier_rows[s.tier].append({"name": s.name, "service": s.service,
                                  "config": f"config/sensors/{s.name}.yaml",
                                  "enabled": s.enabled, "order": s.order})
    veh: dict = {"vehicle": manifest.vehicle}
    if manifest.vehicle_id is not None:
        veh["vehicle_id"] = manifest.vehicle_id
    veh["ros"] = {"domain_id": manifest.ros.domain_id, "rmw": manifest.ros.rmw, "distro": manifest.ros.distro}
    eff_registry = registry or manifest.image_registry
    if eff_registry or manifest.image_tag or manifest.image_base:
        veh["images"] = {k: v for k, v in (("registry", eff_registry), ("tag", manifest.image_tag),
                                           ("base", manifest.image_base)) if v}
    if manifest.platform:  # the host's declared target — a rig re-run on the unbaked tree must
        veh["platform"] = manifest.platform  # export the same RIG_TARGET_PLATFORM / composed tags
    if manifest.data_dir:
        veh["data_dir"] = manifest.data_dir
    if tier_rows["infra"]:
        veh["infra"] = tier_rows["infra"]
    veh["sensors"] = tier_rows["sensor"]
    if tier_rows["autonomy"]:
        veh["autonomy"] = tier_rows["autonomy"]
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
    run_ctx = None
    if manifest.data_dir:  # runs feature: inert without data_dir
        run_ctx = {"data_dir": manifest.data_dir, "vehicle": manifest.vehicle,
                   "vehicle_id": manifest.vehicle_id, "tag": tag,
                   "projects": [project_name(s.name, manifest.vehicle_id)
                                for s in manifest.sensors if s.enabled]}
        _write_run_scripts(staging, run_ctx)
    if entries:
        # Registry mode: alias every digest-pinned image back to its tag post-pull/up (bundle mode keeps
        # tag refs throughout, so there is no split namespace to unify).
        aliases = {} if bundle else {ref: dig for ref, dig in images.items() if dig}
        _write_scripts(staging, entries, bundle_refs=sorted(images) if bundle else None,
                       run_ctx=run_ctx, aliases=aliases,
                       # cleanup.sh untags BOTH names of a pinned image (tag ref + digest ref)
                       cleanup_refs=sorted(set(images) | {d for d in images.values() if d}))
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
        "platform": manifest.platform,
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
    # ONE lockfile: merge bake's image digests into the deployment's package lock (if any), so the
    # artifact carries registries+packages+instances+images under a single rig.lock.
    from .lock import load_lock, save_lock
    locked = load_lock(root)
    locked["images"] = {ref: dig for ref, dig in images.items() if dig}
    save_lock(staging, locked)

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


def is_fleet(root: Path) -> bool:
    """Fleet-ness is a PROPERTY OF THE DEPLOYMENT, not a bake flag (settled): any `{{var}}`
    reference in vehicle.yaml or the config tree makes the artifact a fleet artifact — bake
    resolves from the committed tree only (never vehicle.local.yaml/shell), so markers cannot
    resolve at bake time by construction."""
    return bool(fleet_refs(root))


def fleet_refs(root: Path) -> set[str]:
    from .interpolate import MAP, MARKER

    def _scan(text: str) -> set[str]:
        # MARKER does not match `{{map a b}}` — a map-ONLY deployment must still read as fleet,
        # so both of the form's var names are harvested too.
        return set(MARKER.findall(text)) | {g for m in MAP.finditer(text) for g in m.groups()}

    refs: set[str] = _scan((root / "vehicle.yaml").read_text())
    cfg = root / "config"
    if cfg.is_dir():
        for path in sorted(cfg.rglob("*.yaml")):
            refs |= _scan(path.read_text())
    return refs


def bake_fleet(root: Path, tag: str, *, registry: str | None = None,
               bundle_images: bool = False) -> Path:
    """The fleet artifact: ONE tar for N vehicles. Stages the UNRESOLVED tree (vehicle.yaml +
    the whole config/ tree incl. .pins/.overlays, verbatim) + vendored launch surfaces + the
    bundled rig; rendering happens on-vehicle at `rig up` from /etc/rig/vehicle.local.yaml.
    No compose-only form (it would embed one vehicle's values) — run.sh's existing fallback
    drives the bundled rig, which needs python3 + pyyaml on the vehicle (a documented install).
    Ships provision.sh + vehicle.local.example.yaml so a fresh vehicle needs zero rig knowledge."""
    from .catalog import load_catalog
    from .interpolate import MARKER

    if bundle_images:
        raise RigError("bake: --bundle-images needs the resolved compose-only form, which a fleet "
                       "(templated) artifact deliberately omits — pre-pull per vehicle instead "
                       "(`./run.sh pull`)")
    if registry:
        raise RigError("bake: --registry cannot override a fleet artifact (values resolve "
                       "per-vehicle) — set images.registry in vehicle.yaml, or per vehicle in "
                       "vehicle.local.yaml")
    refs = sorted(fleet_refs(root))
    raw = load_yaml(root / "vehicle.yaml")

    staging = root / "var" / "bake" / tag
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    # rig itself + the raw tree, verbatim (templates preserved — that's the whole point)
    _stage_rig(staging)
    shutil.copy2(root / "vehicle.yaml", staging / "vehicle.yaml")
    if (root / "config").is_dir():
        shutil.copytree(root / "config", staging / "config")
    if (root / "rig.lock").is_file():
        shutil.copy2(root / "rig.lock", staging / "rig.lock")

    # vendor every service the rows reference; route the staged catalog at the vendored copies
    rows = [row for tier in ("infra", "sensors", "autonomy") for row in (raw.get(tier) or [])
            if isinstance(row, dict) and row.get("service")]
    catalog = load_catalog(root)
    catalog_out = {}
    for service in sorted({str(row["service"]) for row in rows}):
        if service not in catalog:
            raise RigError(f"bake: service '{service}' in vehicle.yaml is not routed in services.yaml")
        vendor(service, catalog[service].path, staging)
        catalog_out[service] = {"path": f"services/{service}"}
    (staging / "services.yaml").write_text(yaml.safe_dump({"services": catalog_out}, sort_keys=False))

    # vehicle.local.example.yaml: every referenced var, split mandatory vs fleet-defaulted
    defaults = raw.get("vars") or {}

    def _provided_literally(name: str) -> bool:  # vehicle.yaml itself supplies a non-marker value
        value = raw.get(name)
        return value is not None and not (isinstance(value, str) and name in MARKER.findall(value))

    mandatory = sorted(r for r in refs
                       if r not in ("ros_domain_id", "fleet_peer_ids")  # always derived —
                       and r not in defaults and not _provided_literally(r))  # never supplied
    lines = ["# vehicle.local.yaml — THIS vehicle's identity + values. Write it ONCE per vehicle:",
             "#   sudo ./provision.sh --id <N> --name <name> [--var k=v ...]",
             "# (or by hand at /etc/rig/vehicle.local.yaml). This example lists every var the",
             "# deployment references. Fleet-level vars (fleet_ids from the roster, gcs_ip,",
             "# fleet vars:) may instead arrive via the fleet.yaml `rig fleet up` pushes.", ""]
    for name in mandatory:
        if name == "vehicle_id":
            lines.append("vehicle_id: 1            # REQUIRED")
        elif name == "vehicle":
            lines.append("vehicle: my-vehicle      # REQUIRED")
        else:
            lines.append(f"# vars: {{{name}: <value>}}   # REQUIRED")
    for name in sorted(set(refs) & set(defaults)):
        lines.append(f"# vars: {{{name}: {defaults[name]}}}   # optional — fleet default shown")
    (staging / "vehicle.local.example.yaml").write_text("\n".join(lines) + "\n")

    shim = staging / "provision.sh"
    shim.write_text("#!/bin/sh\n# Provision THIS machine's identity (writes /etc/rig/vehicle.local.yaml).\n"
                    'cd "$(dirname "$0")"\nexec sudo ./rig provision "$@"\n')
    shim.chmod(0o755)
    _write_bootstrap(staging)  # no compose-only scripts exist -> every verb falls through to rig

    meta = {
        "tag": tag, "vehicle": "(fleet)", "fleet": True,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "rig_version": __version__,
        "pinning": "fleet-deferred (rendered on-vehicle)",
        "vars": refs,
        "sensors": [str(row.get("name")) for row in rows],
    }
    (staging / "metadata.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))

    artifacts = root / "var" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    tarpath = artifacts / f"{tag}.tar.gz"
    with tarfile.open(tarpath, "w:gz") as tf:
        tf.add(staging, arcname=tag)
    eprint(f"baked FLEET artifact '{tag}' -> {tarpath}")
    eprint(f"  sha256:{_sha256(tarpath)}")
    eprint(f"  vars: {', '.join(refs)} — resolved per vehicle (vehicle.local.yaml); "
           f"provision.sh + vehicle.local.example.yaml included")
    eprint(f"  no compose-only form: vehicles run the bundled rig (python3 + pyyaml required)")
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
