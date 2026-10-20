"""One read-only planner for check, prepare and execution.

Recipes express intent; this module resolves it against the current deployment and
one source session. It never starts services or writes rendered configs.
"""
from __future__ import annotations

import dataclasses
import json
import math
import re
import subprocess
from pathlib import Path

from . import RigError, dispatch, graph, replay, resolve
from .manifest import PLAYER_SERVICE
from .replay_policy import forced, setting
from .replay_recipe import digest
from .replay_sources import inspect_source, recording_binding, metadata


def capabilities(row, desc):
    """An explicitly advertised, read-only launcher verb; never probe arbitrary verbs."""
    if "replay-capabilities" not in desc.verbs:
        return {}
    try:
        result = subprocess.run([str(desc.repo / desc.launcher), str(row.config),
                                 *desc.verb_args("replay-capabilities")],
                                cwd=desc.repo, capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise ValueError(result.stderr.strip())
        value = json.loads(result.stdout)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise RigError(f"player capability probe failed: {exc}") from exc


def build(manifest, catalog, descriptors, root, ref, recipe, options, *, host=True):
    issues = []

    def issue(status, code, message, phase="prepare"):
        issues.append(dict(status=status, code=code, message=message, phase=phase))

    plan = dict(version=1, recipe=recipe, issues=issues, selected=[], roles={}, recordings={},
                configs={}, omitted={}, player=None, playback={}, source=None,
                window={}, sim_time=False)
    known = {r.name: r for r in manifest.sensors}
    service_names = {r.service for r in manifest.sensors}
    named = set(options.get("names") or [])
    live = set(options.get("live") or [])
    include = set(options.get("include_services") or [])
    skip_services = set(options.get("skip_services") or [])
    skip = set(options.get("skip_instances") or [])
    for values, allowed, label in ((named | live | skip, known, "instances"),
                                   (include | skip_services, service_names, "services"),
                                   (set(recipe.get("instances", {})) |
                                    set(recipe.get("recordings", {})), known, "recipe instances")):
        if values - set(allowed):
            issue("error", "selection", f"Unknown {label}: {', '.join(sorted(values - set(allowed)))}")
    skip |= {r.name for r in manifest.sensors if r.service in skip_services}
    requested = named | live | {r.name for r in manifest.sensors if r.service in include}
    if skip & requested:
        issue("error", "selection", f"Both selected and skipped: {', '.join(sorted(skip & requested))}")
    try:
        lo, hi = replay.validate_window(options.get("window_from"), options.get("window_to"))
        delay = options.get("start_delay", 20.0)
        if not math.isfinite(delay) or delay < 0:
            raise RigError("--start-delay must be finite and nonnegative")
        plan["window"] = dict(start=lo or 0, end=hi)
        grace = options.get("auto_end")
        if grace is not None and grace < 0:
            raise RigError("--auto-end grace must be nonnegative")
        if options.get("export_calls") and (any(options.get(k) for k in (
                "names", "live", "calls", "skip_services", "skip_instances", "include_services", "session"))
                or any(options.get(k) is not None for k in ("window_from", "window_to", "auto_end"))):
            raise RigError("--export-calls cannot be combined with session/playback options")
    except RigError as exc:
        issue("error", "window", str(exc))
    try:
        bag_config = dict(recipe.get("bag", {}))
        player_cfg = next((r.config for r in manifest.sensors if r.service == PLAYER_SERVICE), None)
        if player_cfg and player_cfg.is_file():
            from .common import load_yaml
            defaults = load_yaml(player_cfg).get("source") or {}
            bag_config = {**{k: defaults[k] for k in ("logger", "session") if defaults.get(k)}, **bag_config}
        source = inspect_source(manifest, ref, bag_config)
        plan["source"] = source
        runtime = options.get("_prepared_source")
        if runtime:
            source["runtime_root"] = runtime
            if source["bag"] and source["bag"]["repair"]:
                selected = source["bag"]
                selected.update(metadata(Path(runtime) / "bags" / selected["logger"] / selected["session"]))
                selected["repair"] = False
        if not source["manifest"].get("ended"):
            issue("warning", "unsealed", "Source is unsealed; stability is checked again before use")
        if not (Path(source["root"]) / ".rig").exists():
            issue("warning", "historical_provenance", "No historical deployment capture; using the destination deployment")
        bag = source["bag"]
        if bag and bag["repair"]:
            issue("warning", "reindex", "Bag metadata needs repair in a derived copy")
            issue("not_checked", "bag_metadata", "Run prepare to index this bag", "run")
        if bag and bag.get("duration_ns") is not None:
            duration = bag["duration_ns"] / 1e9
            if plan["window"].get("start", 0) >= duration:
                issue("error", "window", "Replay window starts at or after the selected bag ends")
            if plan["window"].get("end") is not None and plan["window"]["end"] > duration:
                plan["window"]["end"] = duration
                issue("warning", "window_clamped", "Window end clamped to the selected bag duration")
    except (RigError, OSError, ValueError) as exc:
        issue("error", "source", str(exc))
        source, bag = None, None

    candidates = requested | set(forced(manifest, descriptors))
    for row in manifest.sensors:
        if row.service == PLAYER_SERVICE:
            if row.name in requested | skip:
                issue("error", "player_selection", "The bag player is managed automatically")
            continue
        desc = descriptors.get(row.service)
        start, reason, _ = setting(manifest, row, desc)
        if row.name in skip or (not start and row.name not in requested):
            plan["omitted"][row.name] = "CLI skip" if row.name in skip else reason
            continue
        if row.tier == "infra" and row.enabled:
            candidates.add(row.name)
        if source and row.name not in live:
            try:
                binding = recording_binding(row, desc, source, recipe,
                                            camera_session=options.get("session"))
                if binding:
                    plan["recordings"][row.name] = binding
                    candidates.add(row.name)
                elif getattr(desc, "replay_source", None) and row.name in candidates:
                    issue("error", "missing_recording",
                          f"{row.name}: no recording found; bind recordings or explicitly use --live")
            except (RigError, OSError, ValueError) as exc:
                issue("error", "recording", str(exc))
    candidates -= set(plan["omitted"])
    player_rows = [r for r in manifest.sensors if r.service == PLAYER_SERVICE]
    if len(player_rows) > 1:
        issue("error", "player", "Only one ros2-bag-player instance is supported")
    if bag:
        if not player_rows:
            issue("error", "missing_player", "No ros2-bag-player; prepare with --player PATH_OR_PINNED_REF",
                  "run")
        else:
            player = player_rows[0]
            plan["player"] = player.name
            candidates.add(player.name)
    elif source and not plan["recordings"]:
        issue("error", "empty_source", "No bag or usable instance recordings")
    plan["sim_time"] = bool(bag) and not options.get("wall_clock", False)
    if not bag and (options.get("calls") or options.get("export_calls") or recipe.get("playback", {}).get("calls")
                    or options.get("auto_end") is not None):
        issue("error", "player_required", "Calls and --auto-end require a bag timeline")
    rows = manifest.select(sorted(candidates & set(known)), enabled_only=False) if candidates else []
    # Player last regardless of the hand-authored row's tier/order.
    rows.sort(key=lambda r: r.service == PLAYER_SERVICE)
    plan["selected"] = [r.name for r in rows]
    for row in rows:
        role = ("player" if row.service == PLAYER_SERVICE else "live-device" if row.name in live else
                "recording-source" if row.name in plan["recordings"] else "infra" if row.tier == "infra" else "consumer")
        _, reason, explicit = setting(manifest, row, descriptors.get(row.service))
        plan["roles"][row.name] = dict(role=role, reason="CLI" if row.name in requested else
                                      reason if explicit else "automatic " + role)

    playback = recipe.get("playback", {})
    available = set((bag or {}).get("topics", {}))
    data_topics = {t for t in available if t not in ("/clock", "/rosout", "/parameter_events")
                   and not t.endswith("/_service_event")}
    topics = data_topics if playback.get("topics", "all") == "all" else set(playback["topics"])
    missing = (topics | set(playback.get("keep", [])) | set(playback.get("exclude", [])) |
               set(playback.get("remap", {}))) - available
    if missing and bag and not bag["repair"]:
        issue("error", "missing_topics", f"Topics absent from selected bag: {', '.join(sorted(missing))}")
    regenerated = set()
    if source and bag and not bag["repair"]:
        aliases = {n: recipe.get("instances", {}).get(n, {}).get("recorded_as", n) for n in known}
        epochs = graph.load_epochs(Path(source["root"]))
        if options.get("selection", "broad") == "recorded":
            mode, value, notes = replay.select_topics(
                Path(source["root"]), [aliases[n] for n in named if n in aliases],
                live_names=[aliases[n] for n in candidates if n in aliases])
            topics &= set(value.split()) if mode == "topics" else {
                t for t in topics if not re.search(value, t)}
            for note in notes:
                issue("warning", "recorded_selection", note)
        elif epochs:
            union = graph.union(epochs)
            groups = graph.group_nodes(union.nodes, sorted(set(aliases.values()) |
                                                          set(source["manifest"].get("stacks") or [])))
            owners = {}
            for name, nodes in groups.items():
                for node in nodes:
                    for edge in union.nodes[node]:
                        if edge.kind == "pubs" and not graph.is_plumbing(edge):
                            owners.setdefault(edge.name, set()).add(name)
            # Unassigned publishers also count, preventing an accidental exclusive-owner claim.
            assigned = {n for ns in groups.values() for n in ns}
            for node, edges in union.nodes.items():
                if node not in assigned:
                    for edge in edges:
                        if edge.kind == "pubs":
                            owners.setdefault(edge.name, set()).add("unassigned:" + node)
            producers = {aliases[n] for n in candidates if n in aliases}
            regenerated = {t for t, ns in owners.items() if ns and ns <= producers}
            regenerated -= {"/tf", "/tf_static", "/diagnostics"}
            observed = set(groups)
            unknown = sorted(n for n in candidates if n in aliases and aliases[n] not in observed)
            if unknown:
                issue("warning", "unknown_outputs", "Output ownership unknown for " +
                      ", ".join(unknown) + "; use playback.exclude for known live outputs")
        else:
            # A source being re-delivered has a known historical namespace. A new consumer does not.
            mapped_producers = set(plan["recordings"]) | (set(recipe.get("instances", {})) & candidates)
            for name in mapped_producers:
                old = aliases[name]
                regenerated.update(t for t in topics if t.startswith("/" + old + "/") or t == "/" + old)
            issue("warning", "unknown_outputs",
                  "No recorded graph; mapped producers use a namespace heuristic. "
                  "Other data topics remain eligible; exclude live outputs explicitly")
        if skip:
            pattern, notes = replay.skipped_topic_pattern(
                Path(source["root"]), [aliases[n] for n in skip if n in aliases], set(aliases.values()))
            topics = {t for t in topics if not re.search(pattern, t)}
            for note in notes:
                issue("warning", "skip_topics", note)
    topics = ((topics - regenerated) | set(playback.get("keep", []))) - set(playback.get("exclude", []))
    topics &= data_topics
    remap = playback.get("remap", {})
    destinations = [remap.get(t, t) for t in topics]
    if len(set(destinations)) != len(destinations):
        issue("error", "topic_collision", "Remapping collides with another selected topic")
    if set(destinations) & regenerated - set(playback.get("keep", [])):
        issue("error", "live_destination", "A selected topic maps into a known regenerated output")
    services = playback.get("services", [])
    if options.get("selection") == "recorded" and "services" not in playback and named and source:
        value, notes = replay.select_services(Path(source["root"]),
                                             [recipe.get("instances", {}).get(n, {}).get("recorded_as", n)
                                              for n in sorted(named)])
        services = value.split() if value else []
        for note in notes:
            issue("warning", "recorded_services", note)
    if bag and not bag["repair"]:
        for name in services:
            if name + "/_service_event" not in available:
                issue("error", "service_events", f"No recorded service events for {name}")
    calls = options.get("calls") or playback.get("calls")
    if calls:
        calls = str(Path(calls).expanduser().resolve())
        try:
            plan["calls_sha"] = replay.validate_calls(Path(calls))
        except RigError as exc:
            issue("error", "calls", str(exc))
        services = []
    plan["playback"] = dict(topics=sorted(topics), remap=remap, services=services, calls=calls,
                            clock_only=bool(bag and not topics and not services))
    plan["needs_preparation"] = bool(source and (source["kind"] != "run" or
        (bag and (bag["repair"] or bag.get("requires_copy"))) or
        any(b["adapted"] for b in plan["recordings"].values())))
    if plan["needs_preparation"] and not options.get("_prepared_source"):
        issue("not_checked", "normalization", "Prepare the derived source layout before playback", "run")
    plan["excluded_regenerated"] = sorted(regenerated)
    plan["excluded"] = dict(plumbing=sorted(available - data_topics), regenerated=sorted(regenerated),
                           explicit=playback.get("exclude", []), skipped_instances=sorted(skip))
    issue("warning", "compatibility_scope",
          "Bag metadata cannot prove new subscriber compatibility or current output ownership; "
          "no service interface declarations are required")

    variables = dict(manifest.vars or {})
    variables.update(replay_source=(source or {}).get("runtime_root", (source or {}).get("root")),
                     replay_session=options.get("session"),
                     replay_retime="original" if plan["sim_time"] else "wall",
                     replay_start_at_unix_s=None, replay_epoch_unix_ns=(bag or {}).get("start_ns"),
                     replay_from_s=plan["window"].get("start"), replay_to_s=plan["window"].get("end"))
    for row in rows:
        desc = descriptors.get(row.service)
        if desc is None:
            issue("error", "descriptor", f"{row.name}: service descriptor unavailable")
            continue
        if not (desc.repo / desc.launcher).is_file():
            issue("error", "launcher", f"{row.name}: launcher missing: {desc.launcher}")
        try:
            from .common import load_yaml
            raw = load_yaml(row.config)
            if any(raw.get(key) is not None and raw[key] != value
                   for key, value in (("name", row.name), ("service", row.service))):
                raise RigError("config name/service disagrees with the instance row")
            plan["configs"][row.name] = resolve._layered_dict(
                row, root, variables={**variables, "name": row.name},
                extra_overrides=desc.replay_source.overrides if row.name in plan["recordings"] else None)
        except (RigError, OSError) as exc:
            issue("error", "config", f"{row.name}: {exc}")
    if getattr(manifest, "missing_identity", ()):
        issue("error", "identity", "Resolve deployment identity before playback", "run")
    if manifest.data_dir and source:
        import os
        output = Path(manifest.data_dir).expanduser().resolve()
        if output.is_relative_to(Path(source["root"])):
            issue("error", "output_storage", "Output data_dir must not be inside the source recording")
        ancestor = next((p for p in (output, *output.parents) if p.exists()), output)
        if not os.access(ancestor, os.W_OK):
            issue("error", "output_storage", f"Output storage is not writable: {ancestor}", "run")
    elif not manifest.data_dir:
        issue("warning", "output_storage", "No data_dir: playback will not capture a new run", "run")
    active = dataclasses.replace(manifest, sensors=[dataclasses.replace(r, enabled=True) for r in rows])
    if (rows and manifest.ros.rmw in ("rmw_zenoh", "rmw_zenoh_cpp") and
            not any(r.tier == "infra" and "zenoh" in r.service for r in rows)):
        issue("error", "router", "Replay needs a selected zenoh router for this deployment's RMW", "run")
    if len(plan["configs"]) == len(rows) and all(r.service in descriptors for r in rows):
        from . import doctor
        active_descriptors = {r.service: descriptors[r.service] for r in rows}
        for finding in doctor.collect(active, catalog, active_descriptors, configs=plan["configs"]):
            if finding.level == doctor.ERROR:
                issue("warning" if options.get("force") else "error", "deployment",
                      finding.message + (" (--force)" if options.get("force") else ""), "run")
        consumers = [r.name for r in rows if plan["roles"][r.name]["role"] == "consumer"]
        for finding in doctor.replay_issues(active, active_descriptors, consumers,
                                             sim_time=plan["sim_time"], services=bool(services or calls),
                                             sources=list(plan["recordings"])):
            if finding.level == doctor.WARN:
                issue("warning", "clock_support", finding.message, "run")
    if plan["player"] and plan["player"] in plan["configs"]:
        player = known[plan["player"]]
        player_cfg = plan["configs"][player.name].get("play") or {}
        if options.get("auto_end") is not None and player_cfg.get("loop"):
            issue("error", "auto_end", "--auto-end requires play.loop: false")
        try:
            rate = float(player_cfg.get("rate", 1))
            if not math.isfinite(rate) or rate <= 0:
                raise ValueError()
        except (TypeError, ValueError):
            issue("error", "player_rate", "Player play.rate must be finite and positive")
        try:
            caps = capabilities(player, descriptors[player.service])
            plan["capabilities"] = caps
            if caps.get("plan") != 1:
                issue("error", "player_capability", "Update the player: replay plan v1 is required", "run")
            if remap and not caps.get("remap"):
                issue("error", "player_capability", "Player does not support topic remaps", "run")
            if plan["playback"]["clock_only"] and not caps.get("clock_only"):
                issue("error", "player_capability", "Player does not support an empty-data timeline", "run")
        except RigError as exc:
            issue("error", "player_capability", str(exc), "run")
    if host and rows and not getattr(manifest, "missing_identity", ()):
        try:
            from .image_catalog import validate_replay, docker_arch
            architecture = docker_arch()
            plan["execution"] = validate_replay(
                manifest, {r.service: descriptors[r.service] for r in rows if r.service in descriptors},
                fleet_descriptors=descriptors)
            if not manifest.build_catalog:
                # Without companion digests check the local tags actually used by launchers.
                from .build import _service_tag, resolve_base_image, resolve_msgs_image
                from .targets import variant_spec
                refs = set()
                env = dispatch.fleet_env(manifest, descriptors)
                base_ref, _, _ = resolve_base_image(manifest, descriptors)
                msgs_ref, _, _ = resolve_msgs_image(manifest, descriptors)
                replaced = ({d.build_images[0] for d in descriptors.values()
                             if d.build_provides == "base"} if manifest.image_base else set())
                for row in rows:
                    desc = descriptors.get(row.service)
                    if not desc:
                        continue
                    _, images = variant_spec(desc, manifest.platform)
                    if row.service == PLAYER_SERVICE:
                        refs.add(env.get("BAG_PLAYER_IMAGE") or msgs_ref or base_ref or
                                 (f"{manifest.image_registry}/" if manifest.image_registry else "") +
                                 f"fleet-ros:{manifest.image_tag or 'latest'}")
                        continue
                    for image in images:
                        if image in replaced:
                            continue
                        tag = _service_tag(desc, manifest.image_tag, manifest.platform) or "latest"
                        prefix = f"{manifest.image_registry}/" if manifest.image_registry else ""
                        key = desc.image_env.get(image)
                        refs.add(env.get(key) if key and env.get(key) else f"{prefix}{image}:{tag}")
                for resolver in (resolve_base_image, resolve_msgs_image):
                    ref, _, error = resolver(manifest, descriptors)
                    if error:
                        raise RigError(error)
                    if ref:
                        refs.add(ref)
                for ref in sorted(refs):
                    proc = subprocess.run(["docker", "image", "inspect", ref, "--format",
                                           "{{.Os}}/{{.Architecture}}"], capture_output=True,
                                          text=True, timeout=10)
                    if proc.returncode or proc.stdout.strip() != "linux/" + architecture:
                        raise RigError(f"Replay image unavailable for linux/{architecture}: {ref}; "
                                       "build or pull it explicitly")
                plan["images"] = sorted(refs)
                issue("warning", "unpinned_images", "Local image tags checked; no companion digest catalog")
            issue("ok", "runtime", "Docker architecture and replay image policy checked", "run")
        except (RigError, OSError, subprocess.TimeoutExpired) as exc:
            issue("not_checked", "runtime", str(exc), "run")
        if not options.get("export_calls"):
            try:
                replay._guard_clean_host(manifest, False)
            except RigError as exc:
                issue("warning" if options.get("force") else "not_checked", "busy_destination", str(exc), "run")
    else:
        issue("not_checked", "runtime", "Docker and image availability were not checked", "run")
    if bag and bag["repair"] and not plan.get("capabilities", {}).get("reindex"):
        issue("error", "reindex_helper", "Metadata repair needs a player with replay-reindex support",
              "run")
    from .descriptor import find_descriptor
    from .common import load_yaml
    surfaces = {}
    for service in {r.service for r in rows} & set(descriptors):
        desc = descriptors[service]
        descriptor_path = find_descriptor(desc.repo)
        raw = load_yaml(descriptor_path) if descriptor_path else {}
        files = {desc.launcher_path}
        if descriptor_path:
            files.add(descriptor_path)
        for pattern in raw.get("launch_surface", []):
            files.update(p for p in desc.repo.glob(pattern) if p.is_file())
        surfaces[service] = {str(p): digest(p.read_bytes().hex()) for p in files if p.is_file()}
    plan["launch_surfaces"] = surfaces
    controls = {root / n for n in ("vehicle.yaml", "vehicle.local.yaml", "services.yaml",
                                   "fleet.yaml", "rig.lock")}
    controls.update(r.config for r in rows)
    if options.get("_recipe_path"):
        controls.add(Path(options["_recipe_path"]))
    controls.update(p for p in (root / "config/.overlays").glob("*.yaml"))
    plan["control_files"] = {str(p): digest(p.read_bytes().hex()) if p.is_file() else None
                             for p in sorted(controls)}
    plan["fingerprint"] = digest(dict(recipe=recipe, source=(source or {}).get("fingerprint"),
                                     configs=plan["configs"], selected=plan["selected"],
                                     recordings=plan["recordings"], playback=plan["playback"],
                                     window=plan["window"], surfaces=surfaces, controls=plan["control_files"],
                                     environment=manifest.extra_env, images=manifest.build_catalog,
                                     execution=plan.get("execution"), calls=plan.get("calls_sha")))
    return finish(plan)


def finish(plan):
    issues = plan["issues"]
    plan["ready_to_prepare"] = not any(i["status"] == "error" and i["phase"] == "prepare" for i in issues)
    plan["ready_to_run"] = not any(i["status"] in ("error", "not_checked") for i in issues)
    return plan
