"""Select and export deployment images, plus explicitly requested onboard replay images."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import subprocess

import yaml

from . import RigError
from .common import eprint
from .image_catalog import canonical_ref, pins_for_row


def _run(args, **kwargs):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RigError(f"bundle: docker {args[0]} failed: {exc}") from exc


def source_for(manifest, ref, target):
    """Return an immutable architecture child when the release catalog covers this reference."""
    from .bake import _repo_of
    for original, row in manifest.build_catalog.items():
        tags = list(pins_for_row(manifest, original, row))
        if not tags:
            continue
        repo = _repo_of(tags[0])
        index = repo + "@" + row["digest"]
        children = row.get("children", {})
        matches = [*tags, index, *(repo + "@" + d for d in children.values())]
        if canonical_ref(ref) not in matches:
            continue
        if row.get("variant") and row["variant"] != target.runtime:
            raise RigError(f"bundle: {ref} selects runtime {row['variant']}, expected {target.runtime}")
        if target.arch not in row.get("architectures", []):
            raise RigError(f"bundle: {ref} has no {target.arch} release member")
        child = children.get(target.arch)
        if not child or not re.fullmatch(r"sha256:[0-9a-f]{64}", child):
            raise RigError(f"bundle: {ref} lacks a recorded {target.arch} child digest; rebuild this release")
        if "@" in ref and ref != index and ref != repo + "@" + child:
            raise RigError(f"bundle: {ref} selects a different architecture from {target.arch}")
        return repo + "@" + child, tags + [index]
    return ref, []


def export_images(staging: Path, refs, manifest, target, *, required_labels=None):
    """Pull/inspect the selected CPU, save content-specific local aliases, and record the mapping.

    Content aliases survive save/load and don't depend on Docker restoring registry digests.
    --platform on save prevents a containerd store from exporting unrelated architecture members.
    """
    if not target.arch:
        raise RigError("bundle: a deployment architecture is required")
    # Docker save --platform requires API 1.48. Refuse older daemons before pulling or tagging.
    version = _run(["version", "--format", "{{.Server.APIVersion}}"], timeout=15)
    try:
        api = tuple(int(n) for n in version.stdout.strip().split("."))
    except ValueError:
        api = ()
    if version.returncode or api < (1, 48):
        raise RigError("bundle: architecture-specific docker save requires Docker API 1.48 or newer")
    runtime_refs, ids, sources = {}, {}, {}
    temporary = set()
    try:
        for ref in sorted(set(refs)):
            source, synonyms = source_for(manifest, ref, target)

            def inspect():
                # Containerd's default inspection can return an index (including
                # attestations), which save --platform deliberately drops. Record
                # the selected child so inspected and exported identities agree.
                platform = ["--platform", f"linux/{target.arch}"] if api >= (1, 49) else []
                result = _run(["image", "inspect", *platform, "--format", "{{json .}}", source], timeout=20)
                try:
                    info = json.loads(result.stdout) if not result.returncode else {}
                    if not isinstance(info, dict):
                        info = {}
                except ValueError:
                    info = {}
                return info

            info = inspect()
            if info.get("Os") != "linux" or info.get("Architecture") != target.arch:
                eprint(f"  bundle: pull linux/{target.arch} {source}")
                result = _run(["pull", "--platform", f"linux/{target.arch}", source])
                if result.returncode:
                    raise RigError(f"bundle: cannot pull {source}: {result.stderr.strip()[:300]}")
                info = inspect()
            image_id = info.get("Id", "")
            if (info.get("Os") != "linux" or info.get("Architecture") != target.arch
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)):
                raise RigError(f"bundle: {source} is not a verified linux/{target.arch} image")
            labels = (info.get("Config") or {}).get("Labels") or {}
            if api < (1, 49) and (info.get("Descriptor") or {}).get("mediaType") in (
                    "application/vnd.oci.image.index.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json"):
                raise RigError("bundle: selecting an image index member requires Docker API 1.49 or newer")
            for key, value in (required_labels or {}).get(ref, {}).items():
                if labels.get(key) != value:
                    raise RigError(f"bundle: {ref} requires image label {key}={value}; "
                                   "update the replay image before baking")
            # A tag per source repository/content identity, never a release's mutable tag.
            alias = f"rig-bundle/{hashlib.sha256(ref.encode()).hexdigest()[:16]}:{image_id[7:]}"
            platform = ["--platform", f"linux/{target.arch}"] if api >= (1, 49) else []
            existed = _run(["image", "inspect", "--format", "{{.Id}}", alias], timeout=20)
            if existed.returncode == 0:
                selected = (_run(["image", "inspect", *platform, "--format", "{{.Id}}", alias], timeout=20)
                            if platform else existed)
                if selected.returncode or selected.stdout.strip() != image_id:
                    raise RigError(f"bundle: content alias {alias} already refers to a different image")
            # An index's child is not necessarily addressable by bare digest in
            # the local store. Tag the source, then verify the alias's selected
            # member before and after save; a source moving still fails closed.
            result = _run(["tag", source, alias])
            if result.returncode:
                raise RigError(f"bundle: cannot tag {source}: {result.stderr.strip()[:300]}")
            if existed.returncode:
                temporary.add(alias)
            for key in [ref, canonical_ref(ref), *synonyms]:
                if key in runtime_refs and runtime_refs[key] != alias:
                    # The same selected content can be referenced by both its tag and index.
                    if ids[runtime_refs[key]] != image_id:
                        raise RigError(f"bundle: conflicting image selection for {key}")
                runtime_refs[key] = alias
            ids[alias] = image_id
            sources[ref] = {"source": source, "image_id": image_id, "alias": alias}
        from .bake import _bundle_images
        bundle = _bundle_images(staging, sorted(ids), arch=target.arch,
                                inspect_platform=api >= (1, 49))
        if bundle["image_ids"] != ids:
            raise RigError("bundle: selected image identities changed during export")
        return dict(bundle, refs=runtime_refs, sources=sources, target=target.as_dict())
    finally:
        for alias in sorted(temporary):
            result = _run(["image", "rm", alias])
            if result.returncode:
                eprint(f"bundle: temporary alias cleanup failed for {alias}: {result.stderr.strip()[:150]}")


def rewrite_compose(staging, entries, refs):
    for entry in entries:
        path = staging / entry["compose"]
        compose = yaml.safe_load(path.read_text())
        for service in (compose.get("services") or {}).values():
            ref = service.get("image")
            if not ref:
                raise RigError("bundle: every Compose service must declare an image")
            alias = refs.get(ref) or refs.get(canonical_ref(ref))
            if not alias:
                raise RigError(f"bundle: runtime requires unbundled image {ref}; bake a new artifact")
            service["image"] = alias
            service["pull_policy"] = "never"
        path.write_text(yaml.safe_dump(compose, sort_keys=False))


def run_image_query(sensor, desc, env, *, repo=None, verb="images", timeout=15):
    """Bounded, declared launcher query shared by bundle and replay image planning."""
    if verb not in desc.verbs:
        raise RigError(f"{sensor.name}: service must declare verbs.{verb} before querying images")
    repo = Path(repo) if repo is not None else desc.repo
    try:
        result = subprocess.run([str(repo / desc.launcher), str(sensor.config), *desc.verb_args(verb)],
                                cwd=repo, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RigError(f"{sensor.name}: {verb} query failed: {exc}") from exc
    if result.returncode:
        raise RigError(f"{sensor.name}: {verb} query failed: "
                       f"{(result.stderr or '').strip()[:500] or 'exit ' + str(result.returncode)}")
    return result.stdout


def parse_image_refs(output, *, instance):
    """The original verbs.images YAML-list contract used by templated artifacts."""
    from .interpolate import referenced_vars
    try:
        images = yaml.safe_load(output)
    except yaml.YAMLError as exc:
        raise RigError(f"{instance}: invalid image-plan output: {exc}") from exc
    if (not isinstance(images, list) or not images
            or any(not isinstance(ref, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]*", ref)
                   for ref in images) or referenced_vars(images)):
        raise RigError(f"{instance}: image planner must return a nonempty YAML list of resolved image refs; "
                       "unresolved image selectors need deployment defaults")
    return images


def known_values(value, variables):
    """Resolve deployment inputs, retaining fleet identity markers for image-only queries."""
    from .interpolate import substitute_scalar
    if isinstance(value, dict):
        return {k: known_values(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [known_values(v, variables) for v in value]
    if isinstance(value, str):
        try:
            return substitute_scalar(value, variables, where="image plan")
        except RigError:
            return value
    return value


def fleet_image_refs(root, staging, manifest, descriptors):
    """Optional `verbs.images`: YAML list of refs, from raw layered config + target, no identity.

    Services must fail if unresolved variables affect image selection; build outputs are never
    used as a guess at runtime requirements. Existing resolved artifacts use compose capture.
    """
    from .dispatch import fleet_env, service_env
    from .resolve import _layered_dict
    refs = set()
    env = fleet_env(replace(manifest, missing_identity=(), vehicle_id=None), descriptors)
    for sensor in manifest.select([], enabled_only=True):
        desc = descriptors[sensor.service]
        if "images" not in desc.verbs:
            raise RigError(f"bundle: {sensor.service} needs a declared verbs.images image planner "
                           "for a templated artifact (no resolved compose-only form)")
        config = staging / "var" / "image-plan" / f"{sensor.name}.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(yaml.safe_dump(known_values(_layered_dict(sensor, root), manifest.vars), sort_keys=False))
        repo = staging / "services" / sensor.service
        output = run_image_query(replace(sensor, config=config), desc, service_env(env, desc), repo=repo)
        refs.update(parse_image_refs(output, instance=sensor.name))
    return sorted(refs)
