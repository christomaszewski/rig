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
import re
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path

import yaml

from . import RigError, __version__
from .common import eprint, load_yaml, safe_component
from .manifest import TIER_SUB, project_name, stack_summary
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


# The registry round-trip is bounded: an unreachable or non-resolving registry host (a bench off
# the vehicle network, a fake `reg.test` ref, a resolver that hangs instead of NXDOMAIN-ing) must
# leave the ref as a tag with one warning, never hang a bake. RIG_DIGEST_TIMEOUT_S overrides.
DIGEST_TIMEOUT_S = 20.0


def _digest_timeout() -> float:
    import os
    try:
        return float(os.environ.get("RIG_DIGEST_TIMEOUT_S") or DIGEST_TIMEOUT_S)
    except ValueError:
        return DIGEST_TIMEOUT_S


def _resolve_digest(ref: str, *, local_only: bool = False) -> str | None:
    """A pinned ``repo@sha256:…`` for an image ref. Tries the local image's RepoDigests first, then the
    registry via ``docker buildx imagetools`` (so a tag pushed to your local/offline registry pins to its
    content digest). Returns None if neither resolves (then the ref stays a tag). ``local_only`` skips the
    registry round-trip — bundle mode records digests as audit metadata only, and must not stall on an
    unreachable registry (the whole point of bundling)."""
    if '@sha256:' in ref:
        return ref  # a catalog already chose the immutable index; never substitute a local child
    repo = _repo_of(ref)
    try:  # 1. local image's repo digest
        proc = subprocess.run(["docker", "inspect", "--format", "{{json .RepoDigests}}", ref],
                              capture_output=True, text=True, timeout=_digest_timeout())
        if proc.returncode == 0:
            for rd in json.loads(proc.stdout or "[]"):
                if rd.startswith(repo + "@sha256:"):
                    return rd
    except Exception:  # noqa: BLE001
        pass
    if local_only:
        return None
    try:  # 2. registry manifest digest (multi-arch index digest -> the vehicle pulls the right arch)
        from .docker_registry import imagetools
        proc = imagetools(["inspect", ref, "--format", "{{.Manifest.Digest}}"], [ref],
                          capture_output=True, text=True, timeout=_digest_timeout())
        dig = proc.stdout.strip()
        if proc.returncode == 0 and dig.startswith("sha256:"):
            return f"{repo}@{dig}"
    except subprocess.TimeoutExpired:
        eprint(f"rig: warning: digest lookup for {ref} timed out after {_digest_timeout():g}s "
               f"(registry unreachable?) — the ref stays a tag (RIG_DIGEST_TIMEOUT_S to adjust)")
    except Exception:  # noqa: BLE001
        pass
    return None


def _local_build_keys(ref: str) -> str | None:
    """The LOCAL_BUILD_LABEL value of a pinned image ref, or None when absent or unverifiable. The
    local store answers first; otherwise every platform member's config in the registry, bounded
    like the digest lookup. Only digest-resolved refs reach here, so an unreachable registry was
    already reported (and the ref left a tag) by `_resolve_digest`."""
    from .artifact_images import LOCAL_BUILD_LABEL

    def label(labels):
        return (labels.get(LOCAL_BUILD_LABEL) or None) if isinstance(labels, dict) else None

    try:
        proc = subprocess.run(["docker", "image", "inspect", "--format", "{{json .Config.Labels}}",
                               ref], capture_output=True, text=True, timeout=_digest_timeout())
        if proc.returncode == 0:
            return label(json.loads(proc.stdout or "null"))
    except Exception:  # noqa: BLE001
        pass
    try:
        from .docker_registry import imagetools
        proc = imagetools(["inspect", ref, "--format", "{{json .Image}}"], [ref],
                          capture_output=True, text=True, timeout=_digest_timeout())
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    # One config for a single-platform manifest; platform -> config for an index.
    for config in ([data] if "config" in data else data.values()):
        found = label((config.get("config") or {}).get("Labels")) if isinstance(config, dict) else None
        if found:
            return found
    return None


def _bundle_images(staging: Path, refs: list[str], *, arch: str | None = None,
                   inspect_platform: bool = False) -> dict:
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

    def image_ids() -> dict[str, str]:
        ids = {}
        for ref in refs:
            platform = ["--platform", "linux/" + arch] if arch and inspect_platform else []
            p = subprocess.run(["docker", "image", "inspect", *platform, "--format", "{{.Id}}", ref],
                               capture_output=True, text=True)
            image_id = p.stdout.strip()
            if p.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise RigError(f"bake --bundle-images: cannot determine image ID for {ref}")
            ids[ref] = image_id
        return ids

    ids = image_ids()
    tar = staging / "images.tar"
    eprint(f"  bundle: docker save {len(refs)} image(s) -> images.tar (can take minutes)")
    selection = ["--platform", "linux/" + arch] if arch else []
    proc = subprocess.run(["docker", "save", *selection, "-o", str(tar), *refs], capture_output=True, text=True)
    if proc.returncode != 0 or not tar.exists():
        raise RigError(f"bake --bundle-images: docker save failed: {(proc.stderr or '').strip()[:200]}")
    # Refuse a tag moving during the save: startup must check the identities we captured,
    # never record a different image merely because it now occupies the same tag.
    if image_ids() != ids:
        raise RigError("bake --bundle-images: image IDs changed during docker save; retry with stable tags")
    from .image_archive import archive_identities
    identities = archive_identities(tar, ids, arch)
    return {"file": "images.tar", "size_bytes": tar.stat().st_size, "image_ids": ids, **identities}


# --- bake -------------------------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_guard(image_ids: dict[str, str], *, accepted_ids: dict[str, list[str]] | None = None,
                force: bool = False) -> list[str]:
    """Load missing OR different images, then verify every identity before starting anything.

    Docker stores report either config or OCI digests. Accept only identities verified
    from this archive; mutable tags and registry digest restoration are insufficient.
    """
    lines = ["bundle_images_match() {"]
    for ref, image_id in sorted(image_ids.items()):
        identities = (accepted_ids or {}).get(ref, [image_id])
        pattern = "|".join(shlex.quote(value) for value in identities)
        lines += [
            f"  actual=$(docker image inspect --format '{{{{.Id}}}}' {shlex.quote(ref)} 2>/dev/null) || actual=",
            f'  case "$actual" in {pattern}) ;; *)',
            f"    echo {shlex.quote(f'bundle: {ref}: expected {image_id} (verified archive identity), got ')}\"${{actual:-missing}}\" >&2",
            "    return 1",
            "  esac",
        ]
    lines += ["  return 0", "}"]
    if force:
        lines.append("docker load -i ./images.tar")
    else:
        lines += [
            "if ! bundle_images_match >/dev/null 2>&1; then",
            '  [ -f ./images.tar ] || { echo "bundle: images differ or are missing, but images.tar is absent" >&2; exit 1; }',
            '  echo "loading bundled images (missing or different image IDs) ..." >&2',
            "  docker load -i ./images.tar",
            "fi",
        ]
    return lines + ["bundle_images_match"]


def _target_guard(target: dict | None) -> list[str]:
    if not target or not target.get("arch"):
        return []
    expected = shlex.quote("linux/" + target["arch"])
    return [
        "actual=$(docker info --format '{{.OSType}}/{{.Architecture}}')",
        'case "$actual" in linux/aarch64) actual=linux/arm64 ;; linux/x86_64) actual=linux/amd64 ;; esac',
        f'[ "$actual" = {expected} ] || {{ echo "artifact requires {expected}; Docker engine is $actual" >&2; exit 1; }}',
        "if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' >/dev/null 2>&1; then",
        "  python3 -c 'from pathlib import Path; from rig_cli.artifact_runtime import check_target; check_target(Path.cwd(), check_arch=False)'",
        "fi",
    ]


def _write_bundle_guard(staging: Path, bundle: dict) -> None:
    for filename, force in (("ensure-images.sh", False), ("load.sh", True)):
        script = staging / filename
        lines = ["#!/usr/bin/env sh", "set -e", 'cd "$(dirname "$0")"',
                 *_target_guard(bundle.get("target")),
                 *_load_guard(bundle["image_ids"], accepted_ids=bundle.get("accepted_ids"), force=force)]
        script.write_text("\n".join(lines) + "\n")
        script.chmod(0o755)


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


def _write_scripts(staging: Path, entries: list[dict], *, bundle_ids: dict[str, str] | None = None,
                   bundle_accepted_ids: dict[str, list[str]] | None = None,
                   run_ctx: dict | None = None, aliases: dict[str, str] | None = None,
                   cleanup_refs: list[str] | None = None, target: dict | None = None) -> None:
    up = ["#!/usr/bin/env sh", "set -e", "cd \"$(dirname \"$0\")\""]
    up += _target_guard(target)
    if run_ctx:
        up += _run_ensure_guard(run_ctx)
    if bundle_ids:
        up += _load_guard(bundle_ids, accepted_ids=bundle_accepted_ids)
    for e in entries:
        for vol in e["external_volumes"]:
            up.append(f'docker volume create "{vol}" >/dev/null')
        pull_policy = " --pull never" if bundle_ids else ""
        up.append(f'docker compose -p "{e["project"]}" -f "{e["compose"]}" up -d{pull_policy}')
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
    if bundle_ids:
        pull += _target_guard(target) + _load_guard(bundle_ids, accepted_ids=bundle_accepted_ids)
    else:
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
    if bundle_ids:  # explicit load also verifies that the archive restored the expected content
        scripts.append(("load.sh", ["#!/usr/bin/env sh", "set -e", "cd \"$(dirname \"$0\")\"",
                                    *_load_guard(bundle_ids, accepted_ids=bundle_accepted_ids, force=True)]))
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
        "  up)     [ $# -eq 1 ] && [ -f ./up.sh ] && exec ./up.sh ;;\n"
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


def _compose_only(manifest, descriptors, env, staging: Path, images: dict, *,
                  pin: bool = True, source_root: Path | None = None) -> tuple[list[dict], list[str]]:
    """Per ENABLED instance, in launch order: run the VENDORED launcher's `config` verb, capture the
    resolved compose, transform it for portable/offline use, and write compose/<name>/. Returns
    (entries, skipped): a stack whose launcher can't run is skipped and NAMED — the caller decides
    what a partial capture means (bake withholds the scripts: the rig-runnable tree still ships).
    ``pin=False`` keeps logical refs for the bundle exporter to map to verified content aliases;
    local digests are collected only as audit metadata."""
    entries: list[dict] = []
    skipped: list[str] = []
    # Images declared `mirror:` in a rigging.yaml are kept as their registry TAG, not digest-pinned: a
    # mirrored multi-arch tag's digest is fragile (index vs per-arch manifest, re-push churn). Built images
    # (single-arch, stable digest) are still pinned.
    mirrored = {_repo_of(m) for d in descriptors.values() for m in d.mirror}
    # The SAME selection `rig up` dispatches: enabled rows only, tiered, `order`-sorted within a
    # tier — up.sh/down.sh are built from this list in this order. Iterating manifest.sensors
    # (every row, YAML order) started disabled stacks and ignored `order`.
    for sensor in manifest.select([], enabled_only=True):
        desc = descriptors[sensor.service]
        repo = (source_root or staging) / "services" / sensor.service
        launcher = repo / desc.launcher
        # _stage_tree lays configs out by TIER (config/infra|sensors|autonomy) — read the row's
        # own tier, or every infra and autonomy stack fails its render and drops out of up.sh.
        config = sensor.config if source_root else staging / "config" / TIER_SUB[sensor.tier] / f"{sensor.name}.yaml"
        cmd = [str(launcher), str(config), *desc.verb_args("config")]
        try:
            # The per-INSTANCE env `up` runs with — platform routing AND the compose project. A
            # launcher may interpolate COMPOSE_PROJECT_NAME into values (a volume name, a container
            # env var); those freeze into the YAML here, where `-p` at run time can't reach them.
            from .dispatch import instance_env
            proc = subprocess.run(cmd, env=instance_env(env, sensor, desc), cwd=str(repo),
                                  capture_output=True, text=True)
            if proc.returncode != 0 or not proc.stdout.strip():
                eprint(f"  compose-only: skip {sensor.name} (launcher config failed: "
                       f"{(proc.stderr or '').strip()[:140]})")
                skipped.append(sensor.name)
                continue
            compose = yaml.safe_load(proc.stdout)
        except Exception as exc:  # noqa: BLE001
            eprint(f"  compose-only: skip {sensor.name} ({exc})")
            skipped.append(sensor.name)
            continue

        _strip_build(compose)
        _strip_profiles(compose)
        if manifest.arch:
            for _, svc in _services(compose):
                requested = svc.get("platform")
                expected = "linux/" + manifest.arch
                accepted = (expected, expected + "/v8") if manifest.arch == "arm64" else (expected,)
                if requested and requested not in accepted:
                    raise RigError(f"{sensor.name}: compose platform {requested} conflicts with {expected}")
                svc["platform"] = expected
        for ref in _service_images(compose).values():
            r = _repo_of(ref)
            if any(r == m or r.endswith("/" + m) for m in mirrored):
                images.setdefault(ref, None)  # mirrored third-party -> keep the registry tag (pullable, stable)
            else:
                images.setdefault(ref, _resolve_digest(ref, local_only=not pin))
        if pin:
            _pin_images(compose, images)
        else:
            # Bundled images must not be replaced by an upstream `always` policy or the
            # special handling of :latest. This also protects direct compose invocations.
            for _, svc in _services(compose):
                if svc.get("image"):
                    svc["pull_policy"] = "never"
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
    return entries, skipped


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


def _routes_text(catalog_out: dict) -> str:
    """services.yaml in rig's GENERATED single-line form. A staged tree IS a deployment tree, and
    the line-oriented editors (`add`, `swap`, `pkg remove`) write and read that shape; safe_dump's
    block form parses identically but made every reconstructed tree a second convention. Falls
    back to safe_dump for any path that isn't a bare token, where flow style would need quoting."""
    simple = re.compile(r"^[A-Za-z0-9._/-]+$")
    if all(simple.match(str((spec or {}).get("path", ""))) and set(spec or {}) == {"path"}
           for spec in catalog_out.values()):
        return "\n".join(["services:"] + [f"  {svc}: {{ path: {spec['path']} }}"
                                          for svc, spec in catalog_out.items()]) + "\n"
    return yaml.safe_dump({"services": catalog_out}, sort_keys=False)


def _stage_tree(staging: Path, manifest, catalog, *, registry: str | None = None) -> None:
    """Steps 1–3 shared by the deploy bake and the run-open capture: bundled rig + resolved
    per-sensor configs + a COMPLETE resolved vehicle.yaml (overrides/profiles already baked in;
    images / vehicle_id / all three tiers preserved so a `rig up` on the unbaked tree exports the
    same fleet env and keeps the tier ordering) + every service's vendored launch surface with a
    catalog that points at them. ALL rows stage, disabled included — a reconstruction (or a
    later `rig replay`) needs their surfaces and configs too."""
    _stage_rig(staging)

    # Configs land in their TIER's dir (config/infra|sensors|autonomy), the layout `rig init`,
    # `rig add` and `pkg add` all write — a staged tree is a deployment tree, and an operator who
    # reconstructs one then adds a service must not end up with two conventions in one config/.
    # (Through v0.2.47 every row was flattened into config/sensors/; those artifacts stay valid —
    # their rows point where their files are, and reconstruct reads the row, never the convention.)
    (staging / "config").mkdir(parents=True, exist_ok=True)
    tier_rows: dict[str, list] = {"infra": [], "sensor": [], "autonomy": []}
    for s in manifest.sensors:
        sub = TIER_SUB[s.tier]
        (staging / "config" / sub).mkdir(parents=True, exist_ok=True)
        shutil.copy2(s.config, staging / "config" / sub / f"{s.name}.yaml")
        tier_rows[s.tier].append({"name": s.name, "service": s.service,
                                  "config": f"config/{sub}/{s.name}.yaml",
                                  "enabled": s.enabled, "order": s.order})
        if s.replay_start is not None:
            # Serialize effective policy, not a stale row value shadowed by a local file.
            tier_rows[s.tier][-1]["replay"] = {
                "start": manifest.replay_policy.get("instances", {}).get(s.name, {}).get(
                    "start", s.replay_start)}
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
    if manifest.arch:
        veh["arch"] = manifest.arch
    if manifest.data_dir:
        veh["data_dir"] = manifest.data_dir
    # The rest of what a `rig up` on the unbaked tree needs to behave like the source did: the
    # operator's `env:` map (already interpolated, like every other value staged here) and the
    # capture opt-out — both were dropped, so a re-run in the field exported nothing and captured
    # into every run on a disk-tight vehicle that had said not to.
    if manifest.extra_env:
        veh["env"] = dict(manifest.extra_env)
    if manifest.replay_policy:
        veh["replay"] = manifest.replay_policy
    if not manifest.run_capture:
        veh["run_capture"] = False
    if tier_rows["infra"]:
        veh["infra"] = tier_rows["infra"]
    veh["sensors"] = tier_rows["sensor"]
    if tier_rows["autonomy"]:
        veh["autonomy"] = tier_rows["autonomy"]
    (staging / "vehicle.yaml").write_text(yaml.safe_dump(veh, sort_keys=False))

    catalog_out = {}
    for service in sorted({s.service for s in manifest.sensors}):
        vendor(service, catalog[service].path, staging)
        catalog_out[service] = {"path": f"services/{service}"}
    (staging / "services.yaml").write_text(_routes_text(catalog_out))


def bake(root: Path, manifest, catalog, descriptors, env, tag: str, *, registry: str | None = None,
         bundle_images: bool = False, platform: str | None = None, arch: str | None = None,
         overwrite: bool = False, include_replay: bool = False, output_root: Path | None = None,
         allow_local: bool = False) -> Path:
    output_root = output_root or root
    safe_component(tag, what="bake --tag")  # names var/bake/<tag>, which is rmtree'd below
    from .artifacts import confirm_overwrite, publish
    overwrite = confirm_overwrite(output_root, tag, overwrite=overwrite)
    from dataclasses import replace
    from .targets import deployment_target
    active = {s.service for s in manifest.sensors if s.enabled}
    if include_replay:
        from .bake_replay import selected_rows
        active.update(s.service for s in selected_rows(manifest, descriptors))
    target = deployment_target(manifest, {n: d for n, d in descriptors.items() if n in active},
                               platform=platform, arch=arch,
                               required=bundle_images or include_replay or bool(manifest.build_catalog))
    manifest = replace(manifest, platform=target.runtime, arch=target.arch)
    if registry:
        manifest = replace(manifest, image_registry=registry)
    from .image_catalog import artifact_catalog
    build_catalog, _ = artifact_catalog(manifest.build_catalog, manifest.image_registry)
    manifest = replace(manifest, build_catalog=build_catalog)
    if manifest.context == "deployment":
        from .dispatch import fleet_env
        env = fleet_env(manifest, descriptors)
    if target.runtime:
        env = {**env, "RIG_TARGET_PLATFORM": target.runtime}
    if target.arch:
        env = {**env, "RIG_TARGET_ARCH": target.arch}
    if registry:
        env = {**env, "RIG_IMAGE_REGISTRY": registry}  # override vehicle.yaml images.registry for this bake
        if manifest.build_catalog:
            from dataclasses import replace
            from .dispatch import fleet_env
            localized = fleet_env(replace(manifest, image_registry=registry), descriptors)
            for key in ('RIG_IMAGE_PINS', 'RIG_BASE_IMAGE', 'RIG_MSGS_IMAGE'):
                env.pop(key, None)
                if key in localized:
                    env[key] = localized[key]
    staging = output_root / "var" / "bake" / tag
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    # 1.–3. rig + configs/vehicle.yaml + vendored surfaces (shared with the run-open capture).
    _stage_tree(staging, manifest, catalog, registry=registry)
    replay_images = None
    if include_replay:
        from .bake_replay import image_plan
        replay_images = image_plan(root, staging, manifest, descriptors)

    # 4. compose-only resolved form (best-effort) + scripts + bootstrap. Bundle mode also `docker save`s
    #    the image set into the artifact: zero registry at deploy time, integrity = the artifact's sha256.
    images: dict[str, str | None] = {}
    entries, skipped = _compose_only(manifest, descriptors, env, staging, images, pin=not bundle_images)
    for ref in (replay_images or {}).get("images", {}):
        if ref not in images:
            images[ref] = _resolve_digest(ref, local_only=bundle_images)
    if not bundle_images:  # bundle mode checks the same label while exporting (export_images)
        from .artifact_images import LOCAL_BUILD_HINT, local_build_note
        local = {ref: keys for ref, dig in images.items() if dig
                 for keys in [_local_build_keys(dig)] if keys}
        if local and not allow_local:
            raise RigError(f"bake: {local_build_note(local)} — {LOCAL_BUILD_HINT}")
        if local:
            eprint(f"  warning (--allow-local): {local_build_note(local)}")
    if skipped:
        # The compose-only form is ALL or nothing. run.sh prefers up.sh whenever it exists and
        # never falls back to rig for the stacks it lacks — a partial up.sh silently started a
        # subset of the vehicle. Withhold the scripts instead: run.sh then drives the bundled rig,
        # which starts everything (python3 + pyyaml on the vehicle, the documented fallback).
        eprint(f"  compose-only: {len(skipped)} of {len(entries) + len(skipped)} enabled stacks did "
               f"not render ({', '.join(skipped)}) — no up.sh/down.sh emitted; run.sh will drive "
               f"the bundled rig instead")
        entries = []
    bundle = None
    if bundle_images:
        if skipped:
            raise RigError(f"bake --bundle-images: the compose-only rendering must succeed for every "
                           f"enabled stack, and {', '.join(skipped)} did not (see the skip messages "
                           f"above)")
        if not images:
            raise RigError("bake --bundle-images: no images captured — the compose-only rendering must "
                           "succeed for every stack you want bundled (see the skip messages above)")
        from .artifact_images import export_images, rewrite_compose
        from .bake_replay import required_labels
        bundle = export_images(staging, sorted(images), manifest, target,
                               required_labels=required_labels(replay_images),
                               allow_local=allow_local)
        rewrite_compose(staging, entries, bundle["refs"])
    run_ctx = None
    if manifest.data_dir:  # runs feature: inert without data_dir
        run_ctx = {"data_dir": manifest.data_dir, "vehicle": manifest.vehicle,
                   "vehicle_id": manifest.vehicle_id, "tag": tag,
                   "projects": [project_name(s.name, manifest.vehicle_id)
                                for s in manifest.sensors if s.enabled]}
        _write_run_scripts(staging, run_ctx)
    if entries:
        # Registry mode also exposes the logical tags; bundles use their own content aliases.
        aliases = {} if bundle else {ref: dig for ref, dig in images.items() if dig}
        _write_scripts(staging, entries, bundle_ids=bundle["image_ids"] if bundle else None,
                       bundle_accepted_ids=bundle.get("accepted_ids") if bundle else None,
                       run_ctx=run_ctx, aliases=aliases,
                       # cleanup.sh untags BOTH names of a pinned image (tag ref + digest ref)
                       cleanup_refs=sorted(bundle["image_ids"] if bundle else set(images) | {d for d in images.values() if d}),
                       target=target.as_dict())
    if bundle:
        _write_bundle_guard(staging, bundle)
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
        "target": target.as_dict(),
        "data_dir": manifest.data_dir,
        "pinning": "target+bundle" if bundle else "digest",
        "sensors": [s.name for s in manifest.sensors],
        "compose_only": [e["sensor"] for e in entries],
        "compose_only_skipped": skipped,
        "sources": sources,
        "images": {ref: dig for ref, dig in images.items()},
    }
    if bundle:
        meta["bundle"] = bundle
    if replay_images:
        meta["replay"] = replay_images
    if parent:
        meta["parent"] = parent
    (staging / "metadata.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))
    pinned = sum(1 for d in images.values() if d)
    # ONE lockfile: merge bake's image digests into the deployment's package lock (if any), so the
    # artifact carries registries+packages+instances+images under a single rig.lock.
    from .lock import load_lock, save_lock
    from .image_catalog import artifact_lock
    locked = artifact_lock(load_lock(root), manifest.image_registry)
    locked["images"] = {ref: dig for ref, dig in images.items() if dig}
    save_lock(staging, locked)

    # 6. bundle
    tarpath = publish(staging, output_root, tag, overwrite=overwrite)
    digest = _sha256(tarpath)
    eprint(f"baked '{tag}' -> {tarpath}")
    eprint(f"  sha256:{digest}")
    pin_note = (f"{len(images)} images BUNDLED ({bundle['size_bytes'] / 1e9:.1f} GB tar; "
                f"target + exact image IDs verified)" if bundle
                else f"{pinned}/{len(images)} images digest-pinned")
    lineage = f" · parent: {parent['tag']}" if parent else ""
    eprint(f"  {stack_summary(manifest.sensors)} · {len(entries)} compose-only · {pin_note}{lineage}")
    return tarpath


def capture_run(root: Path, manifest, run_dir: Path) -> dict:
    """The LEAN run-open capture (rig-reconstruct plan): the deployment tree as it stands, into
    ``<run>/.rig/artifact.tar.gz`` — vendored surfaces for ALL rows (disabled included), all
    configs, the resolved vehicle.yaml, bundled rig. Deliberately SKIPS the deploy bake's
    compose-only render (slow — runs every launcher), registry digest-pinning (offline-hostile),
    and image bundling: fast and network-free by construction, so `_open_run` can call it on a
    field vehicle. Image identity still lands: LOCAL docker-daemon digests (what is actually
    present to run — stronger than a registry tag) into ``<run>/.rig/images.yaml``, best-effort
    per ref. Returns the manifest ``capture:`` stamp ({sha256, rig_version}).

    Loads its own catalog/descriptors from ``root`` (self-contained — runs.py's signatures stay
    stable); the caller wraps the whole call fail-soft: capture must never block a session."""
    import tempfile

    from .catalog import load_catalog
    from .descriptor import load_descriptor

    catalog = load_catalog(root)
    descriptors = {}
    for s in manifest.sensors:
        if s.service not in descriptors and s.service in catalog:
            descriptors[s.service] = load_descriptor(s.service, catalog[s.service].path)

    work = Path(tempfile.mkdtemp(prefix="rig-capture-"))
    staging = work / "capture"
    staging.mkdir()
    try:
        _stage_tree(staging, manifest, catalog)
        from .lock import load_lock, save_lock
        save_lock(staging, load_lock(root))
        meta = {
            "kind": "run-capture",  # reconstruct treats deploy artifacts (retrofits) and captures alike
            "tag": f"run-capture-{run_dir.name}",
            "vehicle": manifest.vehicle,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "rig_version": __version__,
            "platform": manifest.platform,
            "arch": manifest.arch,
            "data_dir": manifest.data_dir,
        }
        if (root / "metadata.yaml").exists():  # capture INSIDE an extracted artifact: keep lineage
            try:
                pmeta = load_yaml(root / "metadata.yaml")
                meta["parent"] = {k: pmeta[k] for k in ("tag", "vehicle", "created", "rig_version")
                                  if pmeta.get(k)}
            except RigError:
                pass
        (staging / "metadata.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))

        rig_dir = run_dir / ".rig"
        rig_dir.mkdir(parents=True, exist_ok=True)
        tarpath = rig_dir / "artifact.tar.gz"
        tmp = rig_dir / ".artifact.tar.gz.tmp"
        with tarfile.open(tmp, "w:gz") as tf:
            tf.add(staging, arcname="capture")
        tmp.replace(tarpath)

        # Image IDENTITY, not bytes: local daemon digests for the refs the manifest implies —
        # base, msgs overlay, each service's own build images (platform-composed like the pull
        # side) and mirrored third-party refs. Unresolvable refs record null (absent image, no
        # daemon): the capture still stands, the pin is just weaker there.
        images: dict[str, str | None] = {}
        try:
            from .build import _service_tag, resolve_base_image, resolve_msgs_image
            refs: set[str] = set()
            for resolver in (resolve_base_image, resolve_msgs_image):
                try:
                    ref = resolver(manifest, descriptors)[0]
                    if ref:
                        refs.add(ref)
                except RigError:
                    pass  # provider conflicts are doctor's ERROR, not capture's problem
            prefix = f"{manifest.image_registry}/" if manifest.image_registry else ""
            for desc in descriptors.values():
                composed = _service_tag(desc, manifest.image_tag, manifest.platform) or "latest"
                refs.update(f"{prefix}{img}:{composed}" for img in desc.build_images)
                refs.update(desc.mirror)
            images = {ref: _resolve_digest(ref, local_only=True) for ref in sorted(refs)}
            (rig_dir / "images.yaml").write_text(yaml.safe_dump(
                {"schema": 1, "images": images}, sort_keys=True))
        except Exception:  # noqa: BLE001 — identity capture is best-effort, never blocks
            pass

        stamp = {"sha256": _sha256(tarpath), "rig_version": __version__}
        pinned = sum(1 for d in images.values() if d)
        eprint(f"rig: run capture -> .rig/artifact.tar.gz "
               f"({tarpath.stat().st_size // 1024} KB, {pinned}/{len(images)} image digests)")
        return stamp
    finally:
        shutil.rmtree(work, ignore_errors=True)


def is_fleet(root: Path) -> bool:
    """Fleet-ness is a PROPERTY OF THE DEPLOYMENT, not a bake flag (settled): any `{{var}}`
    reference in vehicle.yaml or the config tree makes the artifact a fleet artifact — bake
    resolves from the committed tree only (never vehicle.local.yaml/shell), so markers cannot
    resolve at bake time by construction."""
    return bool(fleet_refs(root))


def fleet_refs(root: Path) -> set[str]:
    """Every {{var}} the deployment would resolve at `rig up`: the manifest's VALUES, each row's
    config file, and the layers rig composes under a working config (config/.pins, .overlays).
    Parsed values, never raw text — `rig init`'s own scaffold documents the fleet vocabulary in
    COMMENTS, and a text scan read a literal, freshly initialized deployment as a fleet (which
    then refused --bundle-images and --registry). An unreferenced config left in the tree does
    not vote either. A file rig can't parse falls back to the text scan for that file alone: a
    marker that starts an unquoted value is a load error at `up` too, and must still read as
    fleet here rather than vanish."""
    from .interpolate import MAP, MARKER, referenced_vars

    def _scan_text(text: str) -> set[str]:
        return set(MARKER.findall(text)) | {g for m in MAP.finditer(text) for g in m.groups()}

    def _refs_of(path: Path) -> set[str]:
        try:
            return referenced_vars(load_yaml(path))
        except RigError:
            return _scan_text(path.read_text())

    veh = root / "vehicle.yaml"
    refs: set[str] = _refs_of(veh)
    try:
        doc = load_yaml(veh)
    except RigError:
        doc = {}
    for tier in ("infra", "sensors", "autonomy"):
        for row in doc.get(tier) or []:
            if isinstance(row, dict) and row.get("config"):
                cfg = root / str(row["config"])
                if cfg.is_file():
                    refs |= _refs_of(cfg)
    for layer in (".pins", ".overlays"):
        d = root / "config" / layer
        if d.is_dir():
            for path in sorted(d.rglob("*.yaml")):
                refs |= _refs_of(path)
    return refs


def bake_fleet(root: Path, tag: str, *, registry: str | None = None,
               bundle_images: bool = False, platform: str | None = None, arch: str | None = None,
               overwrite: bool = False, include_replay: bool = False, output_root: Path | None = None,
               allow_local: bool = False) -> Path:
    """The fleet artifact: ONE tar for N vehicles. Stages the UNRESOLVED tree (vehicle.yaml +
    the whole config/ tree incl. .pins/.overlays, verbatim) + vendored launch surfaces + the
    bundled rig; rendering happens on-vehicle at `rig up` from /etc/rig/vehicle.local.yaml.
    No compose-only form (it would embed one vehicle's values) — run.sh's existing fallback
    drives the bundled rig, which needs python3 + pyyaml on the vehicle (a documented install).
    Ships provision.sh + vehicle.local.example.yaml so a fresh vehicle needs zero rig knowledge."""
    from .catalog import load_catalog
    from .interpolate import MARKER
    output_root = output_root or root

    if registry:
        raise RigError("bake: --registry cannot override a fleet artifact (values resolve "
                       "per-vehicle) — set images.registry in vehicle.yaml, or per vehicle in "
                       "vehicle.local.yaml")
    safe_component(tag, what="bake --tag")  # names var/bake/<tag>, which is rmtree'd below
    from .artifacts import confirm_overwrite, publish
    overwrite = confirm_overwrite(output_root, tag, overwrite=overwrite)
    refs = sorted(fleet_refs(root))
    raw = load_yaml(root / "vehicle.yaml")
    manifest, descriptors, target = None, {}, None
    if bundle_images or platform or arch or include_replay:
        from .manifest import load_manifest
        from .descriptor import load_descriptor
        from .targets import deployment_target
        manifest = load_manifest(root, context="deployment",
                                 target_overrides={"platform": platform, "arch": arch})
        routes = load_catalog(root)
        descriptors = {s.service: load_descriptor(s.service, routes[s.service].path) for s in manifest.sensors}
        active = {s.service for s in manifest.sensors if s.enabled}
        if include_replay:
            from .bake_replay import selected_rows
            replay_services = {s.service for s in selected_rows(manifest, descriptors)}
        else:
            replay_services = set()
        if bundle_images:
            missing = sorted(n for n in active if "images" not in descriptors[n].verbs)
            if missing:
                raise RigError(f"bundle: {', '.join(missing)} needs a declared verbs.images image planner "
                               "for a templated artifact (no resolved compose-only form)")
        target = deployment_target(manifest, {n: d for n, d in descriptors.items() if n in active | replay_services},
                                   platform=platform, arch=arch, required=True)
        manifest = load_manifest(root, context="deployment",
                                 target_overrides={"platform": target.runtime, "arch": target.arch})

    staging = output_root / "var" / "bake" / tag
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    # rig itself + the raw tree, verbatim (templates preserved — that's the whole point)
    _stage_rig(staging)
    shutil.copy2(root / "vehicle.yaml", staging / "vehicle.yaml")
    if (root / "config").is_dir():
        shutil.copytree(root / "config", staging / "config")
    if (root / "rig.lock").is_file():
        from .lock import load_lock, save_lock
        from .image_catalog import artifact_lock
        # Registry selection is deferred with the template; carry portable logical identities.
        locked = artifact_lock(load_lock(root), None)
        save_lock(staging, locked)

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

    bundle, replay_images = None, None
    if target:
        from .artifact_images import export_images, fleet_image_refs
        raw = dict(raw, arch=target.arch)
        if target.runtime:
            raw["platform"] = target.runtime
        (staging / "vehicle.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
        if include_replay:
            from .bake_replay import image_plan
            replay_images = image_plan(root, staging, manifest, descriptors, fleet=True)
        if bundle_images:
            if any(u.startswith("images.") for u in manifest.missing_identity):
                raise RigError("bundle: image registry/tag/base must resolve from deployment inputs")
            refs_to_bundle = fleet_image_refs(root, staging, manifest, descriptors)
            refs_to_bundle = sorted(set(refs_to_bundle) | set((replay_images or {}).get("images", {})))
            if not refs_to_bundle:
                raise RigError("bundle: no enabled service images selected")
            from .bake_replay import required_labels
            bundle = export_images(staging, refs_to_bundle, manifest, target,
                                   required_labels=required_labels(replay_images),
                                   allow_local=allow_local)
            _write_bundle_guard(staging, bundle)

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
    if target:
        meta["target"] = target.as_dict()
    if bundle:
        meta["bundle"] = bundle
        meta["pinning"] = "target+bundle"
    if replay_images:
        meta["replay"] = replay_images
    (staging / "metadata.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))

    tarpath = publish(staging, output_root, tag, overwrite=overwrite)
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
        from .common import safe_extract
        safe_extract(tf, into)  # refuse path traversal / unsafe members on every Python
    tops = sorted(p for p in into.iterdir() if p.is_dir())
    target = tops[-1] if tops else into
    eprint(f"unbaked {artifact.name} -> {target}")
    return target
