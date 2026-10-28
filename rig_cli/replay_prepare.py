"""Transactional, source-preserving local preparation artifacts."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

from . import RigError, dispatch
from .common import load_yaml
from .replay_recipe import digest, workspace
from .replay_sources import inventory_fingerprint, metadata, source_fingerprint, verify_binding


def write_yaml(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(yaml.safe_dump(value, sort_keys=False))
    os.replace(temporary, path)


def generation(plan):
    from .replay_plan import SELECTION_VERSION
    return digest(dict(source=plan["source"]["path"], fingerprint=plan["source"]["fingerprint"],
                       selection_version=SELECTION_VERSION,
                       recipe=plan.get("session_recipe", plan["recipe"]), recordings=plan["recordings"],
                       use_proxies=bool(plan.get("use_proxies"))))[:20]


def location(root, recipe_path, source, *, key=None, use_proxies=False):
    folder = workspace(root, recipe_path)
    binding = folder / "bindings.yaml"
    if key is None and binding.exists():
        doc = load_yaml(binding)
        if doc.get("source") == str(Path(source).resolve()):
            key = doc.get("generations", {}).get("proxies" if use_proxies else "original")
            if key is None and not use_proxies and "generations" not in doc:
                key = doc.get("generation")
    if key is not None and not re.fullmatch(r"[0-9a-f]{16,64}", str(key)):
        raise RigError(f"Invalid prepared generation in {binding}")
    return folder / (key or digest(str(Path(source).resolve()))[:16])


def prepared(root, recipe_path, recipe, source, *, use_proxies=False):
    """Return a verified generated source, or None; never consume stale host bindings."""
    source = Path(source).resolve()
    dest = location(root, recipe_path, source, use_proxies=use_proxies)
    record = dest / "provenance.yaml"
    if not record.exists():
        return None
    doc = load_yaml(record)
    from .replay_plan import SELECTION_VERSION
    if (doc.get("selection_version") != SELECTION_VERSION or
            bool(doc.get("use_proxies")) != use_proxies or doc.get("recipe_sha") != digest(recipe) or
            doc.get("source_fingerprint") != source_fingerprint(source)):
        return None
    for path, fingerprint in doc.get("recordings", {}).items():
        if inventory_fingerprint(path) != fingerprint:
            return None
    for binding in doc.get("bindings", {}).values():
        try:
            verify_binding(binding)
        except RigError:
            return None
    for name, fingerprint in doc.get("generated", {}).items():
        path = dest / name
        if not path.is_file() or digest(path.read_bytes().hex()) != fingerprint:
            raise RigError(f"Generated replay file changed: {path}; preserve your edit elsewhere "
                           "and remove this preparation before preparing again")
    runtime = Path(doc["runtime_source"])
    if not runtime.is_dir():
        return None
    # Metadata and payload mutations in derived copies also invalidate the preparation.
    if runtime != Path(source) and doc.get("derived_fingerprint") != inventory_fingerprint(runtime):
        raise RigError(f"Prepared source changed: {runtime}; prepare again in a fresh recipe workspace")
    return runtime


def proxy_pins(root, recipe_path, recipe, source):
    """Remember selected identities even if an artifact was removed or became corrupt.

    Replanning then reports a broken pin instead of silently selecting another generation.
    Changed original evidence or recipe intent starts a fresh selection.
    """
    dest = location(root, recipe_path, source, use_proxies=True)
    record = dest / "provenance.yaml"
    if not record.is_file():
        return {}
    doc = load_yaml(record)
    if (not doc.get("use_proxies") or doc.get("recipe_sha") != digest(recipe) or
            doc.get("source_fingerprint") != source_fingerprint(source)):
        return {}
    return {name: binding for name, binding in doc.get("bindings", {}).items() if binding.get("proxy")}


def save_binding(root, recipe_path, recipe, source, dest, use_proxies):
    path = workspace(root, recipe_path) / "bindings.yaml"
    saved = load_yaml(path) if path.is_file() else {}
    generations = dict(saved.get("generations", {})) if saved.get("source") == source else {}
    generations["proxies" if use_proxies else "original"] = dest.name
    bindings = recipe["replay"] if recipe.get("version") == 2 else recipe
    write_yaml(path, dict(version=1, recipe_source=bindings.get("source"), source=source,
                          generation=dest.name, generations=generations))


def copy_file(source, dest):
    """Independent copies only: reindexing can never mutate an inode shared with the source."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def create(root, recipe_path, recipe, plan, manifest, descriptors):
    source = plan["source"]
    use_proxies = bool(plan.get("use_proxies"))
    dest = location(root, recipe_path, source["path"], key=generation(plan))
    existing = prepared(root, recipe_path, recipe, source["path"], use_proxies=use_proxies)
    if existing and location(root, recipe_path, source["path"], use_proxies=use_proxies) == dest:
        return existing
    # Refuse to replace modified generated files even when a recipe/source change invalidated it.
    if dest.exists():
        old = load_yaml(dest / "provenance.yaml")
        for name, fingerprint in old.get("generated", {}).items():
            p = dest / name
            if not p.is_file() or digest(p.read_bytes().hex()) != fingerprint:
                raise RigError(f"Preserving edited generated file: {p}; remove/move it explicitly")
        runtime = Path(old["runtime_source"])
        if old.get("derived_fingerprint") and inventory_fingerprint(runtime) != old["derived_fingerprint"]:
            raise RigError(f"Prepared source changed: {runtime}; preserve it and use a fresh recipe workspace")
        # Selecting a previous generation must not replace a source mounted by an ongoing replay.
        save_binding(root, recipe_path, recipe, source["path"], dest, use_proxies)
        return runtime
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".preparing-", dir=dest.parent))
    bag = source["bag"]
    adapted = (source["kind"] != "run" or (bag and (bag["repair"] or bag.get("requires_copy") or
                                                  bag.get("prepared_index"))) or
               any(b["adapted"] and not b.get("direct") for b in plan["recordings"].values()))
    # Direct readers keep independent read-only mounts, even when a legacy bag needs repair.
    copies = [b for b in plan["recordings"].values() if not b.get("direct")]
    runtime = dest / "source" if adapted else Path(source["root"])
    try:
        if adapted:
            derived = stage / "source"
            derived.mkdir()
            bytes_needed = sum(Path(p).stat().st_size for p in (bag or {}).get("files", []))
            for binding in copies:
                files = binding["files"] or [p for p in Path(binding["path"]).rglob("*") if p.is_file()]
                bytes_needed += sum(Path(p).stat().st_size for p in files)
            if shutil.disk_usage(stage).free < bytes_needed + 64 * 1024 * 1024:
                raise RigError(f"Preparation needs at least {bytes_needed} bytes plus working space")
            if bag:
                target = derived / "bags" / bag["logger"] / bag["session"]
                target.mkdir(parents=True)
                for path in [*[Path(p) for p in bag["files"]], Path(bag["path"]) / "metadata.yaml"]:
                    if path.is_file():
                        copy_file(path, target / path.name)
                if source.get("runtime_root") and not bag["repair"]:
                    # Reuse a verified recovered index when a new recording binding needs
                    # another generation of the same formerly unindexed bag.
                    recovered = Path(source["runtime_root"]) / "bags" / bag["logger"] / bag["session"] / "metadata.yaml"
                    if recovered.is_file():
                        copy_file(recovered, target / "metadata.yaml")
                if bag["repair"]:
                    player = next((r for r in manifest.sensors if r.name == plan["player"]), None)
                    if player is None or not plan.get("capabilities", {}).get("reindex"):
                        raise RigError("Prepare needs a current player with replay-reindex support")
                    desc = descriptors[player.service]
                    pins = plan.get("image_pins", {}).get(player.name)
                    if not pins:
                        raise RigError("Metadata repair requires a locally verified player image; "
                                       "prepare never pulls images. Acquire the reported image first")
                    fleet_env = dispatch.fleet_env(manifest, descriptors)
                    if plan.get("execution", {}).get("architecture"):
                        fleet_env["RIG_TARGET_ARCH"] = plan["execution"]["architecture"]
                    fleet_env["RIG_RUNTIME_IMAGE_MAP"] = json.dumps({player.name: pins})
                    env = dispatch.instance_env(fleet_env, player, desc)
                    proc = subprocess.run([str(desc.repo / desc.launcher), str(player.config),
                                           *desc.verb_args("replay-reindex"), str(target)], cwd=desc.repo, env=env,
                                          capture_output=True, text=True, timeout=600)
                    if proc.returncode:
                        raise RigError("Bag reindex failed: " + proc.stderr.strip())
                    metadata(target)  # usable output is required before publishing the directory
            for binding in copies:
                target = derived / binding["relative"]
                original = Path(binding["path"])
                files = [Path(p) for p in binding["files"]] if binding["files"] is not None else [
                    p for p in original.rglob("*") if p.is_file()]
                for path in files:
                    copy_file(path, target / path.relative_to(original))
            # Copy only the historical evidence that exists; never fabricate epochs or captures.
            original = Path(source["root"])
            if source["kind"] == "run":
                for name in ("manifest.yaml", "graph"):
                    src = original / name
                    if src.is_file():
                        copy_file(src, derived / name)
                    elif src.is_dir():
                        shutil.copytree(src, derived / name)
            else:
                write_yaml(derived / "manifest.yaml", {
                    "schema": 1, "run": "import-" + generation(plan),
                    "imported_from": source["path"], "source_kind": source["kind"],
                    "historical_identity": "unknown"})
        if source_fingerprint(source["path"]) != source["fingerprint"]:
            raise RigError("Source changed during preparation; stop its writer and retry")
        for binding in plan["recordings"].values():
            verify_binding(binding)
        write_yaml(stage / "resolved.yaml", plan)
        write_yaml(stage / "report.yaml", {k: plan[k] for k in
                                          ("issues", "ready_to_prepare", "ready_to_run")})
        for name, config in plan["configs"].items():
            write_yaml(stage / "config" / (name + ".yaml"), config)
        generated = {str(p.relative_to(stage)): digest(p.read_bytes().hex())
                     for p in stage.rglob("*") if p.is_file() and "source" not in p.relative_to(stage).parts}
        write_yaml(stage / "provenance.yaml", dict(
            version=1, recipe_sha=digest(recipe), source=source["path"],
            selection_version=plan["selection_version"],
            use_proxies=use_proxies, bindings=plan["recordings"],
            source_fingerprint=source["fingerprint"], runtime_source=str(runtime),
            recordings={b["path"]: b["fingerprint"] for b in plan["recordings"].values()},
            generated=generated,
            operations=["copy-source", "reindex" if bag and bag["repair"] else "preserve-index"]
                       if adapted else ["bind-inputs" if use_proxies else "use-original"],
            player_capabilities=plan.get("capabilities", {}),
            derived_fingerprint=inventory_fingerprint(stage / "source") if adapted else None))
        # New immutable source generation: publish only after copying and verification finish.
        os.rename(stage, dest)
        save_binding(root, recipe_path, recipe, source["path"], dest, use_proxies)
        return runtime
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def player_document(plan, runtime):
    bag = plan["source"]["bag"]
    return dict(version=1, source=str(runtime),
                bag={k: bag[k] for k in ("logger", "session", "start_ns", "duration_ns")},
                **plan["playback"])


def refresh(root, recipe_path, plan):
    """Publish the post-repair report and configs, then their integrity record."""
    dest = location(root, recipe_path, plan["source"]["path"], use_proxies=bool(plan.get("use_proxies")))
    doc = load_yaml(dest / "provenance.yaml")
    write_yaml(dest / "resolved.yaml", plan)
    write_yaml(dest / "report.yaml", {k: plan[k] for k in
                                     ("issues", "ready_to_prepare", "ready_to_run")})
    for name, config in plan["configs"].items():
        write_yaml(dest / "config" / (name + ".yaml"), config)
    doc["generated"] = {str(p.relative_to(dest)): digest(p.read_bytes().hex())
                        for p in dest.rglob("*") if p.is_file()
                        and "source" not in p.relative_to(dest).parts and p.name != "provenance.yaml"}
    write_yaml(dest / "provenance.yaml", doc)
