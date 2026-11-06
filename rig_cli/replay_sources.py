"""Read-only source inventory and session selection, including legacy layouts."""
from __future__ import annotations

from pathlib import Path
from . import RigError, runs
from .common import load_yaml
from .replay_recipe import digest


def inventory_fingerprint(path, *, exclude_exports=False):
    """Small metadata hashes + size/mtime inventory, not a media-content integrity claim."""
    import os
    path = Path(path)
    if path.is_file():
        files = [path]
        if path.suffix in (".mcap", ".db3") and (path.parent / "metadata.yaml").is_file():
            files.append(path.parent / "metadata.yaml")
    else:
        files, visited = [], set()
        for folder, directories, filenames in os.walk(path, followlinks=True):
            if exclude_exports and Path(folder) == path:
                directories[:] = [name for name in directories if name != "exports"]
            directories.sort()
            resolved = Path(folder).resolve()
            if resolved in visited:
                directories[:] = []
                continue
            visited.add(resolved)
            files.extend(Path(folder) / n for n in filenames if (Path(folder) / n).is_file())
            # Include the link itself as well as the target's files; avoid directory cycles.
            files.extend(Path(folder) / n for n in directories if (Path(folder) / n).is_symlink())
        files.sort()
    out = []
    for p in files:
        st = p.stat()
        rel = p.name if path.is_file() else str(p.relative_to(path))
        small = p.is_file() and p.suffix in (".yaml", ".yml", ".json", ".csv") and st.st_size < 4_000_000
        out.append([rel, st.st_size, st.st_mtime_ns, str(p.resolve()) if p.is_symlink() else None,
                    digest(p.read_text(errors="replace")) if small else None])
    return digest(out)


def source_fingerprint(path):
    """Derived exports are independent evidence, never part of the original run inventory."""
    return inventory_fingerprint(path, exclude_exports=True)


def source_path(manifest, ref):
    if not ref:
        raise RigError("replay: supply RUN or a recipe source")
    path = Path(ref).expanduser()
    if path.exists():
        path = path.resolve()
    else:
        _, path = runs.resolve_ref(manifest, ref, verb="replay")
        path = path.resolve()
    if any(runs.is_open(manifest, p) for p in (path, *path.parents)):
        raise RigError("replay: source is the OPEN run — end it before preparing or replaying")
    return path


def metadata(path):
    doc = load_yaml(path / "metadata.yaml")
    info = doc.get("rosbag2_bagfile_information")
    if not isinstance(info, dict):
        raise RigError(f"{path}: missing rosbag2 metadata")
    start = info.get("starting_time")
    duration = info.get("duration")
    start = start.get("nanoseconds_since_epoch") if isinstance(start, dict) else start
    duration = duration.get("nanoseconds") if isinstance(duration, dict) else duration
    if start is None or duration is None or int(start) < 0 or int(duration) < 0:
        raise RigError(f"{path}: metadata needs a nonnegative bag duration and known starting time")
    if info.get("storage_identifier") not in ("mcap", "sqlite3"):
        raise RigError(f"{path}: unsupported or missing ROS 2 storage identifier")
    paths = info.get("relative_file_paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise RigError(f"{path}: metadata needs a list of bag files")
    for filename in paths:
        p = Path(filename)
        if p.is_absolute() or ".." in p.parts or not (path / p).is_file():
            raise RigError(f"{path}: invalid or missing bag file {filename!r}")
    topics = {}
    topic_rows = info.get("topics_with_message_count") or []
    if not isinstance(topic_rows, list):
        raise RigError(f"{path}: invalid metadata topic list")
    for row in topic_rows:
        if not isinstance(row, dict) or not isinstance(row.get("topic_metadata"), dict):
            raise RigError(f"{path}: invalid topic metadata")
        tm = row.get("topic_metadata") or {}
        if tm.get("name") is not None and not isinstance(tm["name"], str):
            raise RigError(f"{path}: invalid topic name")
        if tm.get("name"):
            count = row.get("message_count", 0)
            if type(count) is not int or count < 0:
                raise RigError(f"{path}: invalid topic message count")
            type_hash = tm.get("type_description_hash")
            if type_hash == "RIHS01_" + "0" * 64:
                type_hash = None  # rosbag2's unknown type hash is not schema evidence.
            topics[tm["name"]] = dict(type=tm.get("type"), count=count,
                                     qos=tm.get("offered_qos_profiles"),
                                     serialization=tm.get("serialization_format"),
                                     type_hash=type_hash)
    if not sum(topic["count"] for topic in topics.values()):
        raise RigError(f"{path}: metadata has no recorded messages")
    return dict(start_ns=int(start) if start is not None else None,
                duration_ns=max(1, int(duration)) if duration is not None else None,
                topics=topics, storage=info.get("storage_identifier"))


def selected_bags(source):
    """The actual inputs; source.bag is only an aggregate when there is more than one."""
    if "bags" in source:
        return source["bags"]
    return [source["bag"]] if source.get("bag") else []


def bag_relative_path(bag):
    relative = Path(bag.get("relative_path") or Path("bags") / bag["logger"] / bag["session"])
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise RigError("replay: bag path must remain inside the runtime source")
    return relative


def bag_path(source, bag, runtime=None):
    root = runtime or source.get("runtime_root")
    return Path(root) / bag_relative_path(bag) if root else Path(bag["path"])


def aggregate_bags(bags):
    """Union metadata without modifying, merging, or deduplicating recorded messages."""
    if not bags:
        return None
    if len(bags) == 1:
        return bags[0]
    topics = {}
    for bag in bags:
        for name, entry in bag.get("topics", {}).items():
            if name not in topics:
                topics[name] = {**entry, "count": 0, "sources": []}
            combined = topics[name]
            for field in ("type", "serialization", "type_hash"):
                before, after = combined.get(field), entry.get(field)
                if before and after and before != after:
                    raise RigError(f"replay: incompatible {field} for topic {name} across selected bags")
                if not before and after:
                    combined[field] = after
            combined["count"] += entry.get("count", 0)
            combined["sources"].append({"logger": bag["logger"], "session": bag["session"]})
            # Retain each source's QoS evidence; runtime helpers read the original metadata.
            combined.setdefault("source_qos", []).append(entry.get("qos"))
    indexed = all(b.get("start_ns") is not None and b.get("duration_ns") is not None for b in bags)
    start = min(b["start_ns"] for b in bags) if indexed else None
    end = max(b["start_ns"] + b["duration_ns"] for b in bags) if indexed else None
    stores = {b.get("storage") for b in bags}
    return dict(logger="multiple", session="selected", topics=topics, start_ns=start,
                duration_ns=end - start if indexed else None,
                storage=next(iter(stores)) if len(stores) == 1 else "mixed",
                files=[p for b in bags for p in b["files"]],
                repair=any(b.get("repair") for b in bags),
                requires_copy=any(b.get("requires_copy") for b in bags),
                prepared_index=any(b.get("prepared_index") for b in bags))


def inspect_source(manifest, ref, bag_config=None, *, bags_config=None):
    path = source_path(manifest, ref)
    if path.is_file() and path.suffix not in (".mcap", ".db3"):
        raise RigError(f"{path}: expected a ROS 2 .mcap/.db3 bag or a directory")
    base = path if path.is_dir() else path.parent
    config = bag_config or {}
    native = (base / "bags").is_dir() or (base / "manifest.yaml").is_file()
    candidates = []
    if native:
        for logger in sorted((base / "bags").glob("*")):
            if logger.is_dir():
                for folder in sorted(logger.iterdir()):
                    if folder.is_dir() and any(folder.glob("*.mcap")) or (
                            folder.is_dir() and any(folder.glob("*.db3"))):
                        candidates.append((logger.name, folder.name, folder))
    elif any(base.glob("*.mcap")) or any(base.glob("*.db3")):
        candidates = [(config.get("logger", "bag_logger"), base.name, base)]
    elif not (base / "recordings").is_dir():
        raise RigError(f"{path}: no ROS 2 bags or recording layout found")
    choices = []
    if candidates and bag_config is not None:
        loggers = {c[0] for c in candidates}
        logger = config.get("logger")
        if logger is None:
            if len(loggers) != 1:
                raise RigError(f"replay: choose bag.logger from {', '.join(sorted(loggers))}")
            logger = next(iter(loggers))
        available = [c for c in candidates if c[0] == logger]
        if not available:
            raise RigError(f"replay: bag logger {logger!r} not found")
        session = config.get("session", "latest")
        choices = available if session == "latest" else [
            c for c in available if c[1] in (session, f"{logger}_{session}")]
        if not choices:
            raise RigError(f"replay: bag session {session!r} not found")
        choices = [max(choices, key=lambda c: c[1])]
    elif candidates:
        if bags_config is None or bags_config == "all":
            choices = candidates[:]
        else:
            for selection in bags_config:
                logger = selection["logger"]
                available = [c for c in candidates if c[0] == logger]
                if not available:
                    raise RigError(f"replay: bag logger {logger!r} not found")
                session = selection.get("session", "all")
                matches = available if session in ("all", "latest") else [
                    c for c in available if c[1] in (session, f"{logger}_{session}")]
                if not matches:
                    raise RigError(f"replay: bag session {session!r} not found for {logger}")
                choices.extend([max(matches, key=lambda c: c[1])] if session == "latest" else matches)
    elif bag_config is not None or isinstance(bags_config, list):
        raise RigError("replay: selected bag recordings were not found")
    bags, seen = [], set()
    for logger, session, folder in choices:
        canonical = folder.resolve()
        if canonical in seen:
            raise RigError(f"replay: bag session selected more than once: {logger}/{session}")
        seen.add(canonical)
        files = sorted([*folder.glob("*.mcap"), *folder.glob("*.db3")])
        if path.is_file() and len(files) > 1:
            raise RigError("replay: a bag file belongs to a multi-file directory; supply the directory")
        selected = dict(logger=logger, session=session, path=str(folder),
                        relative_path=str(Path("bags") / logger / session),
                        files=[str(p) for p in files], repair=False)
        formats = {"mcap" if p.suffix == ".mcap" else "sqlite3" for p in files}
        selected["storage"] = next(iter(formats)) if len(formats) == 1 else None
        selected["requires_copy"] = any(not p.resolve().is_relative_to(base.resolve())
                                         for p in [*files, folder / "metadata.yaml"])
        try:
            selected.update(metadata(folder))
        except (RigError, OSError, TypeError, ValueError) as exc:
            selected.update(repair=True, topics={}, start_ns=None, duration_ns=None,
                            repair_reason=str(exc))
        bags.append(selected)
    try:
        doc = load_yaml(base / "manifest.yaml") if (base / "manifest.yaml").is_file() else {}
    except RigError:
        doc = {}  # Legacy runs may have an incomplete manifest; never rewrite it.
    return dict(path=str(path), root=str(base), id=str(doc.get("run") or base.name),
                kind="run" if native else "rosbag2", manifest=doc, bags=bags, bag=aggregate_bags(bags),
                sessions=[{"logger": a, "session": b} for a, b, _ in candidates],
                fingerprint=source_fingerprint(path))


def recording_binding(row, desc, source, recipe, *, camera_session=None, window=None):
    rs = getattr(desc, "replay_source", None)
    spec = recipe.get("recordings", {}).get(row.name, {})
    old = recipe.get("instances", {}).get(row.name, {}).get("recorded_as", row.name)
    if rs is None:
        discovered = Path(source["root"]) / "recordings" / old
        if spec or (discovered.is_dir() and any(discovered.iterdir())):
            raise RigError(f"{row.name}: service has no replay.source support; update its launcher")
        return None
    rel = rs.data_path(old)
    original = Path(spec.get("from", rel)).expanduser()
    if not original.is_absolute():
        original = (Path(source["root"]) / original).resolve()
        if not original.is_relative_to(Path(source["root"])):
            raise RigError(f"{row.name}: recording path escapes the source run")
    original = original.resolve()
    managed = Path(source["root"]).resolve() / "exports" / "services"
    managed_artifact = original.is_relative_to(managed)
    for parent in (original, *original.parents):
        artifact = parent / "export.yaml"
        if artifact.is_file() and load_yaml(artifact).get("kind") == "rig-service-export":
            managed_artifact = True
            break
    if managed_artifact:
        raise RigError(f"{row.name}: bind the original recording and select its export with "
                       "--use-proxies; recordings.from cannot point into a managed export")
    if not original.is_dir() or not any(original.iterdir()):
        if spec:
            raise RigError(f"{row.name}: no recordings at {original}")
        return None
    sessions = spec.get("sessions", [])
    if camera_session:
        sessions = [camera_session]
    selected_files = None
    # Known legacy camera format; other source services retain opaque directory semantics.
    from .reconstruct import _sidecar_sessions
    observed = _sidecar_sessions(original)
    if observed:
        chosen = []
        bag = source["bag"]
        for prefix, header, files in observed:
            if sessions and prefix not in sessions:
                continue
            if isinstance(header.get("proxy"), dict) and header["proxy"].get("format") == "camera-recording-proxy/v1":
                raise RigError(f"{row.name}: recording {prefix} is a derived proxy; bind the original "
                               "recordings.from and select the export with --use-proxies")
            parts = [p for p in files if p.suffix == ".mkv"]
            indices = [int(p.stem.rsplit("-", 1)[1]) for p in parts]
            if indices != list(range(len(parts))) or any(p.stat().st_size == 0 for p in files):
                raise RigError(f"{row.name}: incomplete recording segments/sidecars for {prefix}")
            attested = header.get("session") or {}
            if header.get("sidecar_csv_failed") or (isinstance(attested, dict) and
                                                   (attested.get("truncated") or attested.get("error"))):
                raise RigError(f"{row.name}: sidecar attests an incomplete recording for {prefix}")
            if isinstance(attested, dict) and attested.get("segments", 0) > len(parts):
                raise RigError(f"{row.name}: missing attested segments for {prefix}")
            if not sessions and bag and bag.get("start_ns") is not None:
                first, last = camera_bounds(original / f"{prefix}.csv", header)
                if first is None or last is None:
                    raise RigError(f"{row.name}: session {prefix} has no usable time bounds; "
                                   "select recordings.sessions explicitly")
                start = bag["start_ns"] + int((window or {}).get("start", 0) * 1e9)
                duration = (window or {}).get("end")
                end = bag["start_ns"] + (int(duration * 1e9) if duration is not None else
                                         (bag.get("duration_ns") or 0))
                if first is not None and first > end:
                    continue
                if last < start:
                    continue
            chosen.append((prefix, files))
        missing = set(sessions) - {p for p, _ in chosen}
        if missing or not chosen:
            raise RigError(f"{row.name}: recording session missing or outside bag interval: "
                           f"{', '.join(sorted(missing)) or original}")
        sessions = [p for p, _ in chosen]
        selected_files = [str(f) for _, files in chosen for f in files]
    elif sessions or list(original.glob("*.json")):
        raise RigError(f"{row.name}: incomplete recording sidecars/video under {original}")
    dest_rel = rs.data_path(row.name)
    return dict(path=str(original), relative=dest_rel, recorded_as=old, sessions=sessions,
                files=selected_files, fingerprint=inventory_fingerprint(original),
                direct=getattr(rs, "binding", None) == "direct",
                adapted=original != Path(source["root"]) / dest_rel or bool(spec.get("sessions"))
                or bool(camera_session) or (selected_files is not None and
                                            len(chosen) != len(observed)))


def select_proxy(row, desc, source, binding, recipe, options, *, verification=None):
    """Apply explicit proxy intent only after original recording/session selection."""
    from .replay_inputs import selection
    target = "instance:" + row.name
    selector = selection(recipe, target, service=row.service,
                         use_proxies=bool(options.get("use_proxies")))
    preferences = recipe.get("proxies", {})
    instance = preferences.get("instances", {}).get(row.name, {})
    if selector.get("use") == "original":
        return binding
    exporter = getattr(desc, "export_source", None)
    requested = preferences.get("services", {}).get(row.service, {})
    eligible = exporter and "service" in getattr(exporter, "modes", ()) and getattr(exporter, "profiles", {})
    if not eligible:
        if requested or instance or target in recipe.get("inputs", {}):
            raise RigError(f"{row.name}: service {row.service} does not declare proxy profiles; upgrade it")
        return binding
    if not binding.get("direct"):
        raise RigError(f"{row.name}: proxy playback requires replay.source.binding: direct; "
                       f"upgrade {row.service}")
    # Export planning discovers the same originals, aliases and session windows, then
    # creates the requested artifacts before a normal replan resolves their bytes.
    if options.get("_export_inputs"):
        return {**binding, "requested_input": selector}
    pin = selector.get("proxy")
    profile = selector.get("profile")
    if not profile and instance.get("export") and target not in recipe.get("inputs", {}):
        profile = requested.get("profile")
    saved = options.get("_prepared_proxies", {}).get(row.name, {})
    if saved and not pin:
        from .service_exports import profile_for
        current_profile = profile_for(desc, profile)
        if saved.get("proxy", {}).get("profile_hash") != current_profile["sha256"]:
            from .common import eprint
            eprint(f"rig replay: {row.name} proxy profile changed; resolving the requested profile again")
            saved = {}
    if (not pin and saved and all(saved.get("original", {}).get(key) == binding.get(key)
                                 for key in ("path", "recorded_as", "fingerprint")) and
            (profile is None or saved.get("proxy", {}).get("profile") == profile) and
            set(binding.get("sessions", [])) <= set(saved.get("original", {}).get("sessions", []))):
        pin = saved.get("proxy", {}).get("export_id")
    from .service_exports import resolve_proxy_binding
    resolved = resolve_proxy_binding(source["root"], binding, service=row.service,
                                     profile=profile, export_id=pin, descriptor=desc,
                                     verification=verification)
    resolved.setdefault("original", dict(binding))
    resolved["direct"] = True
    manifest = Path(resolved["proxy"]["manifest"])
    resolved["proxy"]["manifest_fingerprint"] = digest(manifest.read_bytes().hex())
    return resolved


def binding_variables(variables, row, binding):
    """The same resolved input contract feeds both preview and execution config rendering."""
    values = {**variables, "name": row.name}
    if binding and binding.get("direct"):
        values.update(replay_input_path=binding["path"],
                      replay_input_sessions=binding["sessions"],
                      replay_input_proxy=binding.get("proxy"))
    return values


def verify_binding(binding):
    """Revalidate precisely the evidence selected for this instance, including its lineage."""
    for item in (binding, binding.get("original")):
        if item and inventory_fingerprint(item["path"]) != item["fingerprint"]:
            raise RigError("Recordings changed since planning; check/prepare again")
    proxy = binding.get("proxy")
    if proxy:
        path = Path(proxy["manifest"])
        if not path.is_file() or digest(path.read_bytes().hex()) != proxy["manifest_fingerprint"]:
            raise RigError("Selected proxy manifest changed since planning; check/prepare again")


def camera_bounds(csv_path, header):
    """Use observed frame times, never a guessed duration or filenames."""
    import csv
    lo = hi = None
    with csv_path.open() as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        # System arrival time remains comparable to the bag when camera time isn't PTP.
        key = "system_ns" if "system_ns" in fields and not header.get("ptp_synced") else "timestamp_ns"
        for row in reader:
            value = row.get(key) or row.get("timestamp_ns")
            if not value:
                continue
            ns = int(value)
            lo = ns if lo is None else min(lo, ns)
            hi = ns if hi is None else max(hi, ns)
    return lo, hi
