"""Build exactly the recorded inputs selected by a replay recipe."""
from __future__ import annotations

from pathlib import Path

from . import RigError, replay_inputs, replay_recipe, service_exports, session_recipe
from .common import eprint, load_yaml
from .replay_sources import source_fingerprint, source_path


def load_pins(root, recipe_path, recipe, origin):
    """Keep an export's exact resolutions only while its recipe and originals still match."""
    path = replay_recipe.workspace(root, recipe_path) / "export-pins.yaml"
    if not path.is_file():
        return {}
    doc = load_yaml(path)
    if (doc.get("version") != 1 or doc.get("recipe_sha") != replay_recipe.digest(recipe) or
            doc.get("source") != str(Path(origin).resolve()) or
            doc.get("source_fingerprint") != source_fingerprint(origin)):
        return {}
    # Do not silently discard a corrupt or removed artifact here. Resolution verifies
    # these exact identities and reports the broken pin instead of selecting a substitute.
    return {"bag": doc.get("bag"), "recordings": doc.get("recordings", {})}


def _check(plan):
    errors = [item["message"] for item in plan["issues"]
              if item["status"] == "error" and item.get("phase", "prepare") == "prepare"]
    if errors:
        raise RigError("recipe export: " + "; ".join(errors))


def command(args, root):
    from .replay_workflow import _build_plan, load_context
    from .replay_prepare import write_yaml

    if any((args.service, args.profile, args.instance, args.in_place, args.lossy)):
        raise RigError("run export --recipe uses recipe.inputs and --input; omit --service, "
                       "--profile, --instance, --in-place and --lossy")
    root = Path(root)
    recipe_path, recipe = replay_recipe.load(root, args.recipe)
    recipe = replay_inputs.with_overrides(recipe, getattr(args, "inputs", []))
    provider = session_recipe.provider_recipe(recipe, "replay")
    manifest, catalog, descriptors = load_context(root)
    manifest = session_recipe.apply_session(manifest, recipe)
    replay_inputs.validate_targets(provider, manifest)
    origin = source_path(manifest, replay_recipe.bound_source(root, recipe_path, recipe, args.run))
    verification = service_exports.VerificationContext()
    options = dict(_export_inputs=True, _recipe_path=str(recipe_path), force=False)
    if provider.get("playback", {}).get("calls"):
        calls = Path(provider["playback"]["calls"]).expanduser()
        options["calls"] = str(calls if calls.is_absolute() else recipe_path.parent / calls)

    def plan():
        result = _build_plan(manifest, catalog, descriptors, root, str(origin), recipe,
                             options, host=False, verification=verification)
        _check(result)
        return result

    planned = plan()
    rows = {row.name: row for row in manifest.sensors}
    bag_choice = replay_inputs.selection(provider, "bags")
    bag_binding = None
    if bag_choice.get("use") != "original":
        from . import bag_proxies
        if not planned.get("player"):
            raise RigError("recipe export: bag proxies require a configured ros2-bag-player")
        player = rows[planned["player"]]
        bag_binding = bag_proxies.build(planned["source"], bag_choice, player,
            descriptors[player.service], manifest, descriptors, dry_run=args.dry_run,
            force=args.force, verification=verification, root=root)
        if bag_binding:
            options["_prepared_bag_proxy"] = bag_binding
            # Recompute camera sessions and image-source policy against the actual bag
            # artifact, using the same planner that check/prepare/run use.
            planned = plan()
    resolutions = {}
    for name, binding in planned["recordings"].items():
        row = rows[name]
        choice = replay_inputs.selection(provider, "instance:" + name, service=row.service)
        if choice.get("use") == "original":
            continue
        original = binding.get("original", binding)
        if "profile" in choice:
            resolved = service_exports.build_binding(manifest, catalog, descriptors,
                planned["source"], original, service=row.service, profile=choice["profile"],
                dry_run=args.dry_run, force=args.force, verification=verification)
        else:
            resolved = service_exports.resolve_proxy_binding(planned["source"]["root"], original,
                service=row.service, export_id=choice["proxy"], descriptor=descriptors[row.service],
                verification=verification)
            eprint(f"rig run export: verified {name} proxy {choice['proxy']}")
        if resolved:
            resolutions[name] = resolved
    if args.dry_run:
        eprint("rig run export: recipe input plan complete (dry run)")
        return 0
    options["_export_inputs"] = False
    options["_prepared_proxies"] = resolutions
    final = plan()
    pins = dict(version=1, recipe_sha=replay_recipe.digest(recipe),
                source=str(origin.resolve()), source_fingerprint=source_fingerprint(origin),
                bag=final["source"].get("bag_proxy", bag_binding),
                recordings={name: binding for name, binding in final["recordings"].items()
                            if binding.get("proxy")})
    write_yaml(replay_recipe.workspace(root, recipe_path) / "export-pins.yaml", pins)
    eprint(f"rig run export: recipe inputs ready for {recipe_path}")
    return 0
