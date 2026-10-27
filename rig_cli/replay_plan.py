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

from . import RigError, dispatch, graph, replay, resolve, replay_selection
from .manifest import PLAYER_SERVICE
from .common import eprint
from .descriptor import resolve_ros_namespace
from .replay_policy import forced, setting
from .replay_recipe import digest, recorded_namespaces
from .replay_sources import inspect_source, recording_binding, metadata, select_proxy, binding_variables


SELECTION_VERSION = 2


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


def required_image_labels(descriptors, rows, recordings, plan=None):
    """Capabilities belong to the actual role selected by the launcher, including overrides."""
    required = {row.name: descriptors[row.service].replay_source.image_labels
            for row in rows if recordings.get(row.name, {}).get("direct")
            and descriptors[row.service].replay_source.image_labels}
    if plan and plan.get("player"):
        required[plan["player"]] = plan.get("capabilities", {}).get("image_labels", {})
    if plan:
        required = {name: {role: dict(labels) for role, labels in roles.items()}
                    for name, roles in required.items()}
        for name, roles in plan.get("startup", {}).get("image_labels", {}).items():
            for role, labels in roles.items():
                required.setdefault(name, {}).setdefault(role, {}).update(labels)
    return required


def image_inventory(plan, manifest, descriptors, rows, root, architecture):
    import tempfile
    from . import runtime_images
    from .common import load_yaml
    from .replay_prepare import player_document
    env = dispatch.fleet_env(manifest, descriptors)
    env["RIG_TARGET_ARCH"] = architecture
    source = plan["source"] or {}
    runtime = source.get("runtime_root", source.get("root"))
    if runtime:
        env["RIG_REPLAY_SOURCE"] = str(runtime)
    if plan["sim_time"]:
        env["RIG_SIM_TIME"] = "1"
    env["RIG_REPLAY_FROM_S"] = str(plan["window"]["start"])
    env["RIG_REPLAY_TO_S"] = str(plan["window"]["end"] if plan["window"]["end"] is not None else 0)
    meta = load_yaml(root / "metadata.yaml") if (root / "metadata.yaml").is_file() else {}
    with tempfile.TemporaryDirectory(prefix="rig-replay-image-plan-") as temporary:
        if plan["player"] and source.get("bag"):
            path = Path(temporary) / "player.json"
            path.write_text(json.dumps(player_document(plan, runtime)))
            env["RIG_REPLAY_PLAN"] = str(path)
        return runtime_images.inventory(
            manifest, descriptors, rows, plan["configs"], architecture=architecture, env=env,
            required_labels=required_image_labels(descriptors, rows, plan["recordings"], plan),
            bundle=meta.get("bundle"))


def apply_image_report(plan, inspection):
    """Replace only image readiness evidence after acquisition; retain source/selection checks."""
    plan["image_inspection"] = inspection
    plan["issues"][:] = [i for i in plan["issues"] if i["code"] != "runtime_images"]
    def issue(status, message):
        plan["issues"].append(dict(status=status, code="runtime_images", message=message, phase="run"))
    for message in inspection["errors"]:
        issue("error", message)
    for ref in inspection["proposed_pulls"]:
        issue("not_checked", f"Runtime image acquisition required: {ref} (--pull {plan['pull_policy']})")
    if inspection["ready"]:
        issue("ok", "Selected runtime images and capabilities verified; launch uses immutable image IDs")
    elif not inspection["errors"] and not inspection["proposed_pulls"]:
        issue("not_checked", "Runtime images could not be verified")
    plan["image_pins"] = inspection["pins"]
    plan["execution"]["images"] = {
        ref: {"source": item["source"], "image_id": item["image_id"]}
        for ref, item in inspection["images"].items()}
    return finish(plan)


def build(manifest, catalog, descriptors, root, ref, recipe, options, *, host=True, verification=None):
    issues = []

    def issue(status, code, message, phase="prepare"):
        issues.append(dict(status=status, code=code, message=message, phase=phase))

    plan = dict(version=1, selection_version=SELECTION_VERSION, recipe=recipe, issues=issues, selected=[], roles={}, recordings={},
                configs={}, omitted={}, player=None, playback={}, source=None,
                window={}, sim_time=False, use_proxies=bool(options.get("use_proxies")))
    known = {r.name: r for r in manifest.sensors}
    aliases = {n: recipe.get("instances", {}).get(n, {}).get("recorded_as", n) for n in known}
    namespaces = recorded_namespaces(recipe)
    explicit_namespaces = set(namespaces)
    namespace_sources = {identity: "recipe" for identity in namespaces}
    for row in manifest.sensors:
        identity = aliases[row.name]
        desc = descriptors.get(row.service)
        if identity in explicit_namespaces or getattr(desc, "ros_namespace", None) is None:
            continue
        try:
            namespace = resolve_ros_namespace(desc, identity)
            if identity in namespaces and namespaces[identity] != namespace:
                raise RigError(f"Recorded instance {identity} has conflicting service ros_namespace declarations; "
                               "set instances.NAME.recorded_namespace in the recipe")
            namespaces[identity] = namespace
            namespace_sources[identity] = "rigging.yaml"
        except RigError as exc:
            issue("error", "namespace_mapping", str(exc))
    plan["recorded_namespaces"] = {n: namespaces[alias] for n, alias in aliases.items() if alias in namespaces}
    plan["recorded_namespace_sources"] = {n: namespace_sources[alias] for n, alias in aliases.items()
                                           if alias in namespace_sources}
    service_names = {r.service for r in manifest.sensors}
    named = set(options.get("names") or [])
    live = set(options.get("live") or [])
    include = set(options.get("include_services") or [])
    skip_services = set(options.get("skip_services") or [])
    skip = set(options.get("skip_instances") or [])
    for values, allowed, label in ((named | live | skip, known, "instances"),
                                   (include | skip_services, service_names, "services"),
                                   (set(recipe.get("instances", {})) |
                                    set(recipe.get("recordings", {})) |
                                    set(recipe.get("proxies", {}).get("instances", {})), known, "recipe instances"),
                                   (set(recipe.get("proxies", {}).get("services", {})),
                                    service_names, "recipe proxy services")):
        if values - set(allowed):
            issue("error", "selection", f"Unknown {label}: {', '.join(sorted(values - set(allowed)))}")
    skip |= {r.name for r in manifest.sensors if r.service in skip_services}
    requested = named | live | {r.name for r in manifest.sensors if r.service in include}
    if skip & requested:
        issue("error", "selection", f"Both selected and skipped: {', '.join(sorted(skip & requested))}")
    try:
        lo, hi = replay.validate_window(options.get("window_from"), options.get("window_to"))
        delay = options.get("start_delay", 20.0)
        if delay is None:
            delay = 0.0 if options.get("startup") == "coordinated" else 20.0
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
                selected["prepared_index"] = True
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
            end = plan["window"].get("end")
            if end is not None and plan["window"].get("start", 0) < end:
                end_ns = int(end * 1e9)
                if end_ns <= int(plan["window"].get("start", 0) * 1e9):
                    issue("error", "window", "Replay window is empty at nanosecond precision")
                elif bag["start_ns"] + end_ns - 1 <= 0:
                    issue("error", "window", "ROS cannot represent this exclusive replay stop timestamp")
    except (RigError, OSError, ValueError) as exc:
        issue("error", "source", str(exc))
        source, bag = None, None

    candidates = set() if options.get("export_calls") else requested | set(forced(manifest, descriptors))
    for row in manifest.sensors:
        if row.service == PLAYER_SERVICE:
            if row.name in requested | skip:
                issue("error", "player_selection", "The bag player is managed automatically")
            continue
        if options.get("export_calls"):
            plan["omitted"][row.name] = "call export uses only the player helper"
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
                                            camera_session=options.get("session"), window=plan["window"])
                if binding:
                    binding = select_proxy(row, desc, source, binding, recipe, options,
                                           verification=verification)
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
    ownership_names = set(aliases.values()) | set(((source or {}).get("manifest") or {}).get("stacks") or [])
    namespace_owners = {}
    for identity in sorted(ownership_names):
        namespace = namespaces.get(identity, "/" + identity)
        namespace_owners.setdefault(namespace, []).append(identity)
    for namespace, owners in namespace_owners.items():
        if len(owners) > 1 and any(owner in namespaces for owner in owners):
            issue("error", "namespace_mapping", f"ROS namespace {namespace} has multiple recorded owners: "
                  f"{', '.join(owners)}. Use a distinct ros_namespace template per instance, "
                  "or recipe recorded_as/recorded_namespace bindings to identify the historical owner")
    available = set((bag or {}).get("topics", {}))
    data_topics = {t for t in available if t not in ("/clock", "/rosout", "/parameter_events")
                   and not t.endswith("/_service_event")}
    topics = data_topics if playback.get("topics", "all") == "all" else set(playback["topics"])
    missing = (topics | set(playback.get("keep", [])) | set(playback.get("exclude", [])) |
               set(playback.get("remap", {}))) - available
    if missing and bag and not bag["repair"]:
        issue("error", "missing_topics", f"Topics absent from selected bag: {', '.join(sorted(missing))}")
    epochs = graph.load_epochs(Path(source["root"])) if source else []
    model = replay_selection.ownership(epochs, ownership_names, namespaces=namespaces,
                    aliases=aliases, selected=[n for n in plan["selected"] if n != plan["player"]])
    plan["ownership"] = model
    evidence = replay_selection.topic_selection(model, data_topics | set(model["topics"]))
    regenerated = set(evidence["suppressed"])
    initially_selected = set(topics)
    if source and bag and not bag["repair"]:
        if options.get("selection", "broad") == "recorded":
            selected_aliases = {aliases[n] for n in named if n in aliases}
            if selected_aliases and model["graph_available"] and all(
                    model["owners"][n]["nodes"] for n in selected_aliases):
                # Use the legacy subscriber scope with the same conservative output policy.
                # An empty observed input set stays empty; it is not a reason to guess.
                topics &= {t for t, entry in model["topics"].items()
                           if any(node["owner"] in selected_aliases for node in entry["subscribers"])}
            else:
                mode, value, notes = replay.select_topics(
                    Path(source["root"]), sorted(selected_aliases),
                    live_names=[aliases[n] for n in model["selected"]],
                    namespaces=namespaces, known_names=ownership_names)
                topics &= set(value.split()) if mode == "topics" else {
                    t for t in topics if not re.search(value, t)}
                for note in notes:
                    issue("warning", "recorded_selection", note)
        if model["selected_without_nodes"]:
            issue("warning", "unknown_outputs", "Output ownership unknown for " +
                  ", ".join(model["selected_without_nodes"]) +
                  "; bind recorded_namespace for observed nodes or use playback.exclude for known outputs")
        if not model["graph_available"]:
            issue("warning", "unknown_outputs", "No valid recorded graph; broad replay retains data topics. "
                  "Namespace bindings alone do not prove outputs; use playback.exclude explicitly")
    subscriber_selected = set(topics)
    skipped_model = replay_selection.ownership(epochs, ownership_names, namespaces=namespaces,
                                               aliases=aliases, selected=skip)
    skipped_topics = set(replay_selection.topic_selection(skipped_model, data_topics)["suppressed"])
    if skip and not model["graph_available"]:
        issue("warning", "skip_topics", "Skipped instances have no recorded publisher evidence; "
              "their bag topics remain eligible. Use playback.exclude to remove known topics")
    keeps, excludes = set(playback.get("keep", [])), set(playback.get("exclude", []))
    topics = (((topics - regenerated) | keeps) - excludes - skipped_topics) & data_topics
    remap = playback.get("remap", {})
    destinations = [remap.get(t, t) for t in topics]
    if len(set(destinations)) != len(destinations):
        issue("error", "topic_collision", "Remapping collides with another selected topic")
    collisions = [t for t in topics if remap.get(t, t) in regenerated and t not in keeps]
    if collisions:
        issue("error", "live_destination", "Selected topics map into known regenerated outputs: " +
              ", ".join(f"{t} -> {remap.get(t, t)}" for t in sorted(collisions)) +
              "; explicitly keep the source topic to override")
    plan["topic_selection"] = []
    for record in evidence["records"]:
        name = record["topic"]
        if name not in data_topics:
            continue
        reason = ("skipped_instance" if name in skipped_topics else "explicit_exclude" if name in excludes
                  else "explicit_keep" if name in keeps else "not_requested" if name not in initially_selected
                  else "recorded_subscriber_selection" if name not in subscriber_selected else record["reason"])
        plan["topic_selection"].append(dict(record, original=name, destination=remap.get(name, name),
            decision="keep" if name in topics else "exclude", reason=reason,
            ownership_reason=record["reason"], keep_override=name in keeps))
    # A script replaces recorded requests, before graph or event-content checks.
    services = []
    calls = options.get("calls") or playback.get("calls")
    if calls:
        calls = str(Path(calls).expanduser().resolve())
        plan["service_selection"] = dict(mode="script", candidates=[], records=[])
        try:
            plan["calls_sha"] = replay.validate_calls(Path(calls))
        except RigError as exc:
            issue("error", "calls", str(exc))
    elif options.get("export_calls"):
        plan["service_selection"] = dict(mode="export", candidates=[], records=[])
    else:
        manual = playback.get("services") if "services" in playback else None
        selection = replay_selection.service_selection(model,
            {t.removesuffix("/_service_event") for t in available if t.endswith("/_service_event")}
            | set(model["services"]), mode="manual" if manual is not None else "auto", manual=manual)
        if manual == []:
            selection["mode"] = "off"
        plan["service_selection"] = selection
        for record in selection["records"]:
            name = record["service"]
            if record["decision"] != "candidate":
                continue
            for warning in record["warnings"]:
                issue("warning", "service_selection", f"{name}: {warning}")
            if not bag or (not bag["repair"] and name + "/_service_event" not in available):
                record.update(decision="exclude", reason="missing_events")
                issue("error" if manual is not None else "warning", "service_events",
                      f"No recorded service events for {name}")
            else:
                services.append(name)
        selection["candidates"] = services[:]
        if selection["mode"] == "auto" and not services and any(
                t.endswith("/_service_event") for t in available):
            issue("warning", "unselected_service_events",
                  "No recorded calls selected automatically: no unambiguous started server without "
                  "a selected client has usable event topics. See service_selection for ownership reasons; "
                  "bind recorded_namespace or choose playback.services explicitly when needed")
    plan["playback"] = dict(topics=sorted(topics), remap=remap, services=services, calls=calls,
                            services_source="service", clock_only=bool(bag and not topics and not services))
    plan["needs_preparation"] = bool(source and (source["kind"] != "run" or
        (bag and (bag["repair"] or bag.get("requires_copy") or bag.get("prepared_index"))) or
        any(b["adapted"] and not b.get("direct") for b in plan["recordings"].values())))
    if plan["needs_preparation"] and not options.get("_prepared_source"):
        issue("not_checked", "normalization", "Prepare the derived source layout before playback", "run")
    plan["excluded_regenerated"] = sorted(regenerated)
    plan["excluded"] = dict(plumbing=sorted(available - data_topics), regenerated=sorted(regenerated),
                           explicit=playback.get("exclude", []), skipped_instances=sorted(skip),
                           skipped_topics=sorted(skipped_topics))
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
                row, root, variables=binding_variables(variables, row, plan["recordings"].get(row.name)),
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
    if (rows and not options.get("export_calls") and manifest.ros.rmw in ("rmw_zenoh", "rmw_zenoh_cpp") and
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
        origin = player_cfg.get("services_source", "service")
        if origin not in ("service", "client"):
            issue("error", "service_origin", "Player play.services_source must be service or client")
        else:
            plan["playback"]["services_source"] = origin
        for field, equivalent in (("topics", "playback.topics"), ("exclude", "playback.exclude"),
                                  ("services", "playback.services"), ("calls", "playback.calls or --calls")):
            if field in player_cfg:
                issue("warning", "shadowed_player_selection",
                      f"Player play.{field} is overridden by the replay plan; use {equivalent}" +
                      (": [] to disable recorded calls" if field == "services" and player_cfg[field] == [] else ""))
        if "calls" in plan["configs"][player.name]:
            issue("warning", "shadowed_player_selection",
                  "Player calls is overridden by the replay plan; use playback.calls or --calls")
        for field, flag in (("start_offset_s", "--from"), ("end_offset_s", "--to")):
            if player_cfg.get(field) not in (None, "", 0, "0"):
                issue("warning", "shadowed_player_selection",
                      f"Player play.{field} is overridden by the replay window; use {flag}")
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
            labels = caps.get("image_labels")
            if (not isinstance(labels, dict) or not labels or any(
                    not isinstance(role, str) or not role or not isinstance(values, dict) or not values
                    or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items())
                    for role, values in labels.items())):
                issue("error", "player_capability", "Update the player: required runtime image capability "
                      "labels must be advertised by replay-capabilities", "run")
                caps["image_labels"] = {}
            if remap and not caps.get("remap"):
                issue("error", "player_capability", "Player does not support topic remaps", "run")
            if plan["playback"]["clock_only"] and not caps.get("clock_only"):
                issue("error", "player_capability", "Player does not support an empty-data timeline", "run")
        except RigError as exc:
            issue("error", "player_capability", str(exc), "run")
    from . import replay_coordinator
    replay_coordinator.configure_plan(plan, manifest, descriptors, rows, options, capabilities)
    if host and rows and not getattr(manifest, "missing_identity", ()):
        try:
            from .image_catalog import docker_arch
            from . import runtime_images
            architecture = docker_arch()
            if manifest.arch and manifest.arch != architecture:
                raise RigError(f"Replay deployment requires {manifest.arch}; Docker engine is {architecture}")
            plan["execution"] = dict(manifest.replay_execution, platform=manifest.platform,
                                     architecture=architecture)
            plan["pull_policy"] = options.get("pull", "missing")
            if (len(plan["configs"]) == len(rows) and all(r.service in descriptors for r in rows)
                    and not any(i["status"] == "error" and i["phase"] == "prepare" for i in issues)):
                eprint("rig replay: resolving selected runtime images")
                plan["image_inventory"] = image_inventory(plan, manifest, descriptors, rows, root, architecture)
                inspection = runtime_images.inspect_images(plan["image_inventory"], policy=plan["pull_policy"])
                apply_image_report(plan, inspection)
            else:
                issue("not_checked", "runtime_images", "Resolve planning errors before resolving images", "run")
        except (RigError, OSError, subprocess.TimeoutExpired) as exc:
            issue("not_checked", "runtime_images", str(exc), "run")
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
                                   "fleet.yaml", "rig.lock", "metadata.yaml")}
    controls.update(r.config for r in rows)
    for row in rows:
        if row.overlays:
            # The pinned base determines the local delta applied above overlays. Its
            # creation/removal changes the rendered config just as editing the working file does.
            controls.add(root / "config" / ".pins" / f"{row.name}.yaml")
            controls.update(resolve.overlay_payload_path(root, ref) for ref in row.overlays)
    if options.get("_recipe_path"):
        controls.add(Path(options["_recipe_path"]))
    controls.update(p for p in (root / "config/.overlays").glob("*.yaml"))
    plan["control_files"] = {str(p): digest(p.read_bytes().hex()) if p.is_file() else None
                             for p in sorted(controls)}
    inspect_recorded_calls(plan, manifest, descriptors, root, verification=verification)
    replay_coordinator.refresh_requirements(plan, manifest, descriptors, rows)
    plan["fingerprint"] = digest(dict(selection_version=SELECTION_VERSION, ownership=model,
                                     call_inspection=plan.get("call_inspection"), recipe=recipe, source=(source or {}).get("fingerprint"),
                                     use_proxies=plan["use_proxies"],
                                     namespaces=namespaces,
                                     configs=plan["configs"], selected=plan["selected"],
                                     recordings=plan["recordings"], playback=plan["playback"],
                                     window=plan["window"], surfaces=surfaces, controls=plan["control_files"],
                                     environment=manifest.extra_env, images=manifest.build_catalog,
                                     execution=plan.get("execution"), inventory=plan.get("image_inventory"),
                                     calls=plan.get("calls_sha"), startup=plan.get("startup")))
    return finish(plan)


def inspect_recorded_calls(plan, manifest, descriptors, root, *, execute=False, verification=None):
    """Resolve candidate requests using exact-window evidence; read-only unless execute=True."""
    from .replay_call_inspection import inspect_calls
    plan["issues"][:] = [i for i in plan["issues"] if i["code"] != "recorded_calls"]
    selection = plan.get("service_selection", {})
    candidates = selection.get("candidates", [])
    if not candidates or any(i["status"] == "error" and i["phase"] == "prepare" for i in plan["issues"]):
        return finish(plan)

    def issue(status, message):
        plan["issues"].append(dict(status=status, code="recorded_calls", message=message, phase="run"))

    # Candidates remain visible while inspection is pending. They never authorize launch.
    plan["playback"]["services"] = candidates[:]
    plan.pop("call_inspection", None)
    for record in selection["records"]:
        if record["service"] in candidates:
            record.update(decision="candidate", reason=record["automatic_reason"]
                          if selection["mode"] == "auto" else "manual")
            record.pop("inspection", None)
    bag = (plan.get("source") or {}).get("bag")
    player = next((r for r in manifest.sensors if r.name == plan.get("player")), None)
    if not bag or bag["repair"]:
        issue("not_checked", "Recorded calls need indexed bag metadata; prepare this source first")
    elif player is None or player.name not in plan["configs"]:
        issue("not_checked", "Recorded calls need a configured player")
    elif (plan.get("capabilities", {}).get("inspect_calls") != 1 or
          "replay-inspect-calls" not in descriptors[player.service].verbs):
        issue("error", "Update the player: recorded request inspection (inspect_calls: 1) is required")
    elif not plan.get("image_pins", {}).get(player.name):
        issue("not_checked", "Recorded requests need a locally verified player image before inspection")
    elif plan.get("needs_preparation") and not plan["source"].get("runtime_root"):
        issue("not_checked", "Prepare the derived source before inspecting recorded requests")
    else:
        source = plan["source"]
        session = (Path(source["runtime_root"]) / "bags" / bag["logger"] / bag["session"]
                   if source.get("runtime_root") else Path(bag["path"]))
        request = dict(version=1, session=str(session), start_ns=bag["start_ns"],
                       from_s=plan["window"]["start"], to_s=plan["window"]["end"],
                       services=candidates[:], source_mode=plan["playback"]["services_source"])
        env = dispatch.fleet_env(manifest, descriptors)
        if plan.get("execution", {}).get("architecture"):
            env["RIG_TARGET_ARCH"] = plan["execution"]["architecture"]
        try:
            evidence = inspect_calls(root, player, descriptors[player.service], plan["configs"][player.name],
                request, env=env, pins=plan["image_pins"][player.name],
                expected_surfaces=plan["launch_surfaces"][player.service],
                execute=execute, verification=verification)
            plan["call_inspection"] = evidence
            if evidence["status"] != "ready":
                issue("not_checked", "Recorded request contents have not been inspected for this selection; "
                      "run replay prepare or replay to inspect with the verified player image")
            else:
                included = []
                for record in selection["records"]:
                    name = record["service"]
                    if name not in candidates:
                        continue
                    result = evidence["result"]["services"][name]
                    record["inspection"] = result
                    usable = result["status"] == "usable"
                    record.update(decision="include" if usable else "exclude",
                                  reason="usable_requests" if usable else result["status"])
                    if usable:
                        included.append(name)
                        if result["duplicate_count"]:
                            issue("warning", f"{name}: {result['duplicate_count']} repeated request identities "
                                  "in the selected origin; the player reissues these duplicate events")
                    else:
                        message = f"{name}: {result['reason']}"
                        if result["other_origin_request_count"]:
                            other = "client" if request["source_mode"] == "service" else "service"
                            message += f"; requests exist in the other origin: set player play.services_source: {other}"
                        issue("warning" if selection["mode"] == "auto" else "error", message)
                plan["playback"]["services"] = included
                issue("ok", f"Recorded request inspection complete: {len(included)} target(s), "
                      f"{request['source_mode']} origin")
        except (RigError, OSError) as exc:
            issue("error", str(exc))
    plan["playback"]["clock_only"] = bool(bag and not plan["playback"]["topics"]
                                          and not plan["playback"]["services"])
    if plan["playback"]["clock_only"] and not plan.get("capabilities", {}).get("clock_only"):
        issue("error", "Player does not support the empty-data timeline left after call selection")
    return finish(plan)


def verify_recorded_call_evidence(plan, manifest, descriptors, root):
    """Fail if evidence changed after inspection, including files with restored mtime."""
    import copy
    if not plan.get("service_selection", {}).get("candidates"):
        return
    expected = plan.get("call_inspection")
    current = copy.deepcopy(plan)
    inspect_recorded_calls(current, manifest, descriptors, root)
    if (not expected or expected.get("status") != "ready" or current.get("call_inspection") != expected
            or current["playback"] != plan["playback"]
            or any(i["status"] in ("error", "not_checked") and i["code"] == "recorded_calls"
                   for i in current["issues"])):
        raise RigError("Recorded request evidence changed since inspection; plan/prepare again before launch")


def finish(plan):
    issues = plan["issues"]
    plan["ready_to_prepare"] = not any(i["status"] == "error" and i["phase"] == "prepare" for i in issues)
    plan["ready_to_run"] = not any(i["status"] in ("error", "not_checked") for i in issues)
    return plan
