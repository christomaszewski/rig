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
    if start is None or duration is None or int(start) < 0 or int(duration) <= 0:
        raise RigError(f"{path}: metadata needs a positive bag duration and known starting time")
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
            topics[tm["name"]] = dict(type=tm.get("type"), count=row.get("message_count", 0),
                                     qos=tm.get("offered_qos_profiles"))
    return dict(start_ns=int(start) if start is not None else None,
                duration_ns=int(duration) if duration is not None else None,
                topics=topics, storage=info.get("storage_identifier"))


def inspect_source(manifest, ref, bag_config=None):
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
    selected = None
    if candidates:
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
        _, session, folder = max(choices, key=lambda c: c[1])
        files = sorted([*folder.glob("*.mcap"), *folder.glob("*.db3")])
        if path.is_file() and len(files) > 1:
            raise RigError("replay: a bag file belongs to a multi-file directory; supply the directory")
        selected = dict(logger=logger, session=session, path=str(folder),
                        files=[str(p) for p in files], repair=False)
        selected["requires_copy"] = any(not p.resolve().is_relative_to(base.resolve())
                                         for p in [*files, folder / "metadata.yaml"])
        try:
            selected.update(metadata(folder))
        except (RigError, OSError, TypeError, ValueError) as exc:
            selected.update(repair=True, topics={}, start_ns=None, duration_ns=None,
                            repair_reason=str(exc))
    try:
        doc = load_yaml(base / "manifest.yaml") if (base / "manifest.yaml").is_file() else {}
    except RigError:
        doc = {}  # Legacy runs may have an incomplete manifest; never rewrite it.
    return dict(path=str(path), root=str(base), id=str(doc.get("run") or base.name),
                kind="run" if native else "rosbag2", manifest=doc, bag=selected,
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
    if not options.get("use_proxies"):
        return binding
    preferences = recipe.get("proxies", {})
    instance = preferences.get("instances", {}).get(row.name, {})
    if instance.get("use") == "original":
        return binding
    exporter = getattr(desc, "export_source", None)
    requested = preferences.get("services", {}).get(row.service, {})
    eligible = exporter and "service" in getattr(exporter, "modes", ()) and getattr(exporter, "profiles", {})
    if not eligible:
        if requested or instance:
            raise RigError(f"{row.name}: service {row.service} does not declare proxy profiles; upgrade it")
        return binding
    if not binding.get("direct"):
        raise RigError(f"{row.name}: proxy playback requires replay.source.binding: direct; "
                       f"upgrade {row.service}")
    pin = instance.get("export")
    saved = options.get("_prepared_proxies", {}).get(row.name, {})
    if (not pin and saved and all(saved.get("original", {}).get(key) == binding.get(key)
                                 for key in ("path", "recorded_as", "fingerprint")) and
            set(binding.get("sessions", [])) <= set(saved.get("original", {}).get("sessions", []))):
        pin = saved.get("proxy", {}).get("export_id")
    from .service_exports import resolve_proxy_binding
    resolved = resolve_proxy_binding(source["root"], binding, service=row.service,
                                     profile=requested.get("profile"), export_id=pin, descriptor=desc,
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
