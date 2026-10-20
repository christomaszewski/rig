"""Transactional, source-preserving local preparation artifacts."""
from __future__ import annotations

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
from .replay_sources import inventory_fingerprint, metadata


def write_yaml(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(yaml.safe_dump(value, sort_keys=False))
    os.replace(temporary, path)


def generation(plan):
    return digest(dict(source=plan["source"]["path"], fingerprint=plan["source"]["fingerprint"],
                       recipe=plan["recipe"], recordings=plan["recordings"]))[:20]


def location(root, recipe_path, source, *, key=None):
    folder = workspace(root, recipe_path)
    binding = folder / "bindings.yaml"
    if key is None and binding.exists():
        doc = load_yaml(binding)
        if doc.get("source") == str(Path(source).resolve()):
            key = doc.get("generation")
    if key is not None and not re.fullmatch(r"[0-9a-f]{16,64}", str(key)):
        raise RigError(f"Invalid prepared generation in {binding}")
    return folder / (key or digest(str(Path(source).resolve()))[:16])


def prepared(root, recipe_path, recipe, source):
    """Return a verified generated source, or None; never consume stale host bindings."""
    source = Path(source).resolve()
    dest = location(root, recipe_path, source)
    record = dest / "provenance.yaml"
    if not record.exists():
        return None
    doc = load_yaml(record)
    if (doc.get("recipe_sha") != digest(recipe) or
            doc.get("source_fingerprint") != inventory_fingerprint(source)):
        return None
    for path, fingerprint in doc.get("recordings", {}).items():
        if inventory_fingerprint(path) != fingerprint:
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


def copy_file(source, dest):
    """Independent copies only: reindexing can never mutate an inode shared with the source."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def create(root, recipe_path, recipe, plan, manifest, descriptors):
    source = plan["source"]
    dest = location(root, recipe_path, source["path"], key=generation(plan))
    existing = prepared(root, recipe_path, recipe, source["path"])
    if existing and location(root, recipe_path, source["path"]) == dest:
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
        write_yaml(workspace(root, recipe_path) / "bindings.yaml",
                   dict(version=1, recipe_source=recipe.get("source"), source=source["path"],
                        generation=dest.name))
        return runtime
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".preparing-", dir=dest.parent))
    bag = source["bag"]
    adapted = (source["kind"] != "run" or (bag and (bag["repair"] or bag.get("requires_copy"))) or
               any(b["adapted"] for b in plan["recordings"].values()))
    runtime = dest / "source" if adapted else Path(source["root"])
    try:
        if adapted:
            derived = stage / "source"
            derived.mkdir()
            bytes_needed = sum(Path(p).stat().st_size for p in (bag or {}).get("files", []))
            for binding in plan["recordings"].values():
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
                    env = dispatch.instance_env(dispatch.fleet_env(manifest, descriptors), player, desc)
                    proc = subprocess.run([str(desc.repo / desc.launcher), str(player.config),
                                           *desc.verb_args("replay-reindex"), str(target)], cwd=desc.repo, env=env,
                                          capture_output=True, text=True, timeout=600)
                    if proc.returncode:
                        raise RigError("Bag reindex failed: " + proc.stderr.strip())
                    metadata(target)  # usable output is required before publishing the directory
            for binding in plan["recordings"].values():
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
        if inventory_fingerprint(source["path"]) != source["fingerprint"]:
            raise RigError("Source changed during preparation; stop its writer and retry")
        for binding in plan["recordings"].values():
            if inventory_fingerprint(binding["path"]) != binding["fingerprint"]:
                raise RigError("Recordings changed during preparation; stop their writer and retry")
        write_yaml(stage / "resolved.yaml", plan)
        write_yaml(stage / "report.yaml", {k: plan[k] for k in
                                          ("issues", "ready_to_prepare", "ready_to_run")})
        for name, config in plan["configs"].items():
            write_yaml(stage / "config" / (name + ".yaml"), config)
        generated = {str(p.relative_to(stage)): digest(p.read_bytes().hex())
                     for p in stage.rglob("*") if p.is_file() and "source" not in p.relative_to(stage).parts}
        write_yaml(stage / "provenance.yaml", dict(
            version=1, recipe_sha=digest(recipe), source=source["path"],
            source_fingerprint=source["fingerprint"], runtime_source=str(runtime),
            recordings={b["path"]: b["fingerprint"] for b in plan["recordings"].values()},
            generated=generated,
            operations=["copy-source", "reindex" if bag and bag["repair"] else "preserve-index"]
                       if adapted else ["use-original"],
            player_capabilities=plan.get("capabilities", {}),
            derived_fingerprint=inventory_fingerprint(stage / "source") if adapted else None))
        # New immutable source generation: publish only after copying and verification finish.
        os.rename(stage, dest)
        write_yaml(workspace(root, recipe_path) / "bindings.yaml",
                   dict(version=1, recipe_source=recipe.get("source"), source=source["path"],
                        generation=dest.name))
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
    dest = location(root, recipe_path, plan["source"]["path"])
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
