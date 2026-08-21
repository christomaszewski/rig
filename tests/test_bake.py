"""bake — compose transforms, baked script/bootstrap emission (incl. pull.sh + bundle load guard),
end-to-end bake (tool bundling, --bundle-images tag refs), and re-bake parent provenance.
Run: `.venv/bin/python tests/test_bake.py`."""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.bake import (
    _external_volume_names,
    _localize_binds,
    _pin_images,
    _service_images,
    _strip_build,
    _strip_profiles,
)


def test_strip_build_keeps_image():
    c = {"services": {"driver": {"image": "x:tag", "build": {"context": "."}}}}
    _strip_build(c)
    assert "build" not in c["services"]["driver"]
    assert _service_images(c) == {"driver": "x:tag"}


def test_external_volume_names():
    c = {"volumes": {"sock": {"external": True, "name": "cam_cam_sock"}, "data": {}}}
    assert _external_volume_names(c) == ["cam_cam_sock"]


def test_pin_images_resolved_and_unresolved():
    c = {"services": {"d": {"image": "x:tag"}, "e": {"image": "y:tag"}}}
    _pin_images(c, {"x:tag": "x@sha256:abc", "y:tag": None})
    assert c["services"]["d"]["image"] == "x@sha256:abc"
    assert c["services"]["e"]["image"] == "y:tag"  # unresolved -> left as a tag


def test_strip_profiles():
    c = {"services": {"a": {"profiles": ["x"], "image": "i"}, "b": {"image": "j"}}}
    _strip_profiles(c)
    assert "profiles" not in c["services"]["a"]


def test_localize_binds_relativizes_staging_paths_only():
    staging = pathlib.Path(tempfile.mkdtemp())
    pfile = staging / "sub" / "params.yaml"
    pfile.parent.mkdir(parents=True)
    pfile.write_text("a: 1\n")
    missing_dir = staging / "sub" / "recordings"  # under staging but doesn't exist yet
    dest = pathlib.Path(tempfile.mkdtemp())
    c = {"services": {"driver": {"volumes": [
        {"type": "bind", "source": str(pfile), "target": "/etc/p.yaml"},        # file under staging -> copied
        {"type": "bind", "source": str(missing_dir), "target": "/data/rec"},     # missing dir -> placeholder
        {"type": "bind", "source": "/dev/sbg_imu", "target": "/dev/sbg_imu"},     # host path -> literal
        {"type": "bind", "source": "/data/host", "target": "/data/host"},         # host path -> literal
    ]}}}
    _localize_binds(c, dest, staging)
    vols = c["services"]["driver"]["volumes"]
    assert vols[0]["source"] == "./driver__params.yaml" and (dest / "driver__params.yaml").is_file()
    assert vols[1]["source"] == "./driver__recordings" and (dest / "driver__recordings").is_dir()
    assert vols[2]["source"] == "/dev/sbg_imu"
    assert vols[3]["source"] == "/data/host"


def test_localize_binds_disambiguates_same_basename_sources():
    # Two staged binds whose sources share a basename (a/params.yaml + b/params.yaml) must NOT
    # collapse onto one localized file — the second copy silently overwrote the first, cross-wiring
    # both mounts to b's content.
    staging = pathlib.Path(tempfile.mkdtemp())
    for sub in ("a", "b"):
        (staging / sub).mkdir()
        (staging / sub / "params.yaml").write_text(f"from: {sub}\n")
    dest = pathlib.Path(tempfile.mkdtemp())
    c = {"services": {"drv": {"volumes": [
        {"type": "bind", "source": str(staging / "a" / "params.yaml"), "target": "/etc/a.yaml"},
        {"type": "bind", "source": str(staging / "b" / "params.yaml"), "target": "/etc/b.yaml"},
    ]}}}
    _localize_binds(c, dest, staging)
    vols = c["services"]["drv"]["volumes"]
    assert vols[0]["source"] != vols[1]["source"]                       # distinct localized names
    for vol, want in zip(vols, ("from: a\n", "from: b\n")):             # each mount keeps ITS content
        assert (dest / vol["source"][2:]).read_text() == want


def test_write_scripts_emits_pull_beside_up_down_status():
    from rig_cli.bake import _write_bootstrap, _write_scripts

    staging = pathlib.Path(tempfile.mkdtemp())
    entries = [
        {"sensor": "a", "project": "a-vehicle-1", "compose": "compose/a/docker-compose.yaml",
         "external_volumes": []},
        {"sensor": "b", "project": "b-vehicle-1", "compose": "compose/b/docker-compose.yaml",
         "external_volumes": []},
    ]
    _write_scripts(staging, entries)
    _write_bootstrap(staging)
    pull = (staging / "pull.sh").read_text()
    assert 'docker compose -p "a-vehicle-1" -f "compose/a/docker-compose.yaml" pull' in pull
    assert 'docker compose -p "b-vehicle-1" -f "compose/b/docker-compose.yaml" pull' in pull
    assert "up -d" not in pull and "down" not in pull  # pull touches NO containers
    assert (staging / "pull.sh").stat().st_mode & 0o111
    assert "pull)" in (staging / "run.sh").read_text()  # run.sh routes the verb


def test_pull_is_a_default_verb():
    import tempfile as _tf

    from rig_cli.descriptor import load_descriptor

    svc = pathlib.Path(_tf.mkdtemp())
    (svc / "rigging.yaml").write_text("service: demo\nlauncher: demo-up\n")
    desc = load_descriptor("demo", svc)
    assert desc.verb_args("pull") == ["pull"]  # compose-passthrough launchers get it for free
    (svc / "rigging.yaml").write_text("service: demo\nlauncher: demo-up\nverbs: {pull: 'fetch --all'}\n")
    assert load_descriptor("demo", svc).verb_args("pull") == ["fetch", "--all"]  # and it's overridable


def test_bake_emits_run_scripts_when_data_dir_set():
    import subprocess

    from rig_cli.bake import _write_bootstrap, _write_run_scripts, _write_scripts

    staging = pathlib.Path(tempfile.mkdtemp())
    ctx = {"data_dir": "/home/uxv/logs", "vehicle": "t", "vehicle_id": 1, "tag": "v9",
           "projects": ["cam-vehicle-1", "bag_logger-vehicle-1"]}
    entries = [{"sensor": "cam", "project": "cam-vehicle-1",
                "compose": "compose/cam/docker-compose.yaml", "external_volumes": []}]
    _write_run_scripts(staging, ctx)
    _write_scripts(staging, entries, run_ctx=ctx)
    _write_bootstrap(staging)
    for f in ("new-run.sh", "end-run.sh", "runs.sh", "up.sh", "status.sh", "run.sh"):
        assert (staging / f).exists()
        proc = subprocess.run(["sh", "-n", str(staging / f)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{f} has a sh syntax error: {proc.stderr}"   # every script parses
    up = (staging / "up.sh").read_text()
    assert "_auto" in up and 'ln -sfn "runs/$id"' in up             # ensure-guard, RELATIVE symlink
    assert up.index("_auto") < up.index("compose")                   # run exists BEFORE stacks start
    assert 'rm -f "$D/current"' in up                                # dangling `current` self-heals
    assert "rig_version:" in up and "artifact: v9" in up             # auto-manifest carries the contract
    nr = (staging / "new-run.sh").read_text()
    assert "artifact: v9" in nr and "cam-vehicle-1" in nr            # provenance + inlined guard list
    assert "A-Za-z0-9_-" in nr                                       # label validated (dir/traversal safety)
    assert "while [ -e" in nr                                        # same-second collision suffix
    er = (staging / "end-run.sh").read_text()
    assert "ended:" in er and "tail -c1" in er                       # newline-safe seal append
    st = (staging / "status.sh").read_text()
    assert "run:" in st and '[ -e "$D/current" ]' in st              # dangling != open
    boot = (staging / "run.sh").read_text()
    assert "new-run)" in boot and '[ $# -eq 1 ]' in boot             # flagged forms fall through to rig


def test_scripts_alias_digest_pins_back_to_tags():
    # A digest pull doesn't create the tag name locally, so the launcher path (tag refs) would miss a
    # digest-primed cache — re-pulling online, failing offline. pull.sh/up.sh must alias pins back.
    import subprocess

    from rig_cli.bake import _write_scripts

    staging = pathlib.Path(tempfile.mkdtemp())
    entries = [{"sensor": "ouster", "project": "ouster-vehicle-1",
                "compose": "compose/ouster/docker-compose.yaml", "external_volumes": []}]
    aliases = {"reg:5000/ouster_driver:jp6": "reg:5000/ouster_driver@sha256:abc"}
    _write_scripts(staging, entries, aliases=aliases)
    for script in ("pull.sh", "up.sh"):
        body = (staging / script).read_text()
        line = "docker tag reg:5000/ouster_driver@sha256:abc reg:5000/ouster_driver:jp6 2>/dev/null || true"
        assert line in body, f"{script} missing the alias line"
        assert body.index("docker compose") < body.index("docker tag")   # alias AFTER the pull/up
        assert subprocess.run(["sh", "-n", str(staging / script)]).returncode == 0
    # bundle mode keeps tag refs everywhere — no split namespace, no aliases:
    staging2 = pathlib.Path(tempfile.mkdtemp())
    _write_scripts(staging2, entries, bundle_refs=["reg:5000/ouster_driver:jp6"], aliases={})
    assert "docker tag" not in (staging2 / "pull.sh").read_text()


def test_bake_bundles_tool_from_separated_deployment():
    # rig init layout: the deployment root has NO rig tool in it (the tool lives in this package's dir).
    from rig_cli.bake import bake, unbake
    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor
    from rig_cli.manifest import load_manifest

    svc = pathlib.Path(tempfile.mkdtemp())
    (svc / "rigging.yaml").write_text("service: demo\nlauncher: demo-up\nlaunch_surface: [demo-up]\n")
    (svc / "demo-up").write_text("#!/bin/sh\nexit 1\n")  # config verb fails -> compose-only skips (no Docker)
    (svc / "demo-up").chmod(0o755)

    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config" / "sensors").mkdir(parents=True)
    (root / "vehicle.yaml").write_text("vehicle: t\nsensors: [{name: a, service: demo, config: config/sensors/a.yaml}]\n")
    (root / "services.yaml").write_text(f"services: {{demo: {{path: {svc}}}}}\n")
    (root / "config" / "sensors" / "a.yaml").write_text("service: demo\nname: a\n")
    assert not (root / "rig").exists()  # the tool is NOT in the deployment

    m, cat, descs = load_manifest(root), load_catalog(root), {"demo": load_descriptor("demo", svc)}
    artifact = bake(root, m, cat, descs, {}, "t")
    assert artifact.exists()
    into = pathlib.Path(tempfile.mkdtemp())
    unbake(artifact, into)
    assert (into / "t" / "rig").is_file()              # tool sourced from the package, not from root
    assert (into / "t" / "rig_cli" / "cli.py").is_file()


def _deployment_fixture(launcher_body: str):
    """A one-service deployment whose launcher is the given sh script (no real docker needed for
    `config` — the script prints the compose itself)."""
    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor
    from rig_cli.manifest import load_manifest

    svc = pathlib.Path(tempfile.mkdtemp())
    (svc / "rigging.yaml").write_text("service: demo\nlauncher: demo-up\nlaunch_surface: [demo-up]\n")
    (svc / "demo-up").write_text(launcher_body)
    (svc / "demo-up").chmod(0o755)
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config" / "sensors").mkdir(parents=True)
    (root / "vehicle.yaml").write_text(
        "vehicle: t\nsensors: [{name: a, service: demo, config: config/sensors/a.yaml}]\n")
    (root / "services.yaml").write_text(f"services: {{demo: {{path: {svc}}}}}\n")
    (root / "config" / "sensors" / "a.yaml").write_text("service: demo\nname: a\n")
    return root, load_manifest(root), load_catalog(root), {"demo": load_descriptor("demo", svc)}


_COMPOSE_LAUNCHER = """\
#!/bin/sh
[ "$2" = config ] || exit 0
printf 'services:\\n  core:\\n    image: reg.test/foo:t1\\n'
"""

# Fake docker: reports a RepoDigest (so REGISTRY mode would pin), succeeds image-inspect, and `save`
# writes the -o file — everything the bundle path shells out for, with no daemon.
_DOCKER_SHIM = """\
#!/bin/sh
case "$1" in
  image) [ "$3" = "--format" ] && echo "sha256:fakeimageid"; exit 0 ;;
  inspect) echo '["reg.test/foo@sha256:deadbeef"]'; exit 0 ;;
  save) printf 'fake-image-layers' > "$3"; exit 0 ;;
  pull) exit 0 ;;
esac
exit 0
"""


def test_bundle_images_saves_tar_and_keeps_tag_refs():
    import os

    from rig_cli.bake import bake, unbake

    root, m, cat, descs = _deployment_fixture(_COMPOSE_LAUNCHER)
    # A deployment lock exists (a registry package was installed): bake must MERGE its image
    # digests into that lock, not replace it — the artifact carries one rig.lock with both.
    (root / "rig.lock").write_text(
        "schema: 1\npackages:\n  public/demo@1.0.0: {kind: service, payload_sha256: abc}\n")
    shim = pathlib.Path(tempfile.mkdtemp())
    (shim / "docker").write_text(_DOCKER_SHIM)
    (shim / "docker").chmod(0o755)
    env = {"PATH": f"{shim}:/usr/bin:/bin"}          # for the launcher subprocesses
    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{shim}:{old_path}"        # for bundle/digest subprocesses (inherit os.environ)
    try:
        artifact = bake(root, m, cat, descs, env, "t", bundle_images=True)
    finally:
        os.environ["PATH"] = old_path
    into = pathlib.Path(tempfile.mkdtemp())
    unbake(artifact, into)
    tree = into / "t"
    assert (tree / "images.tar").read_text() == "fake-image-layers"
    compose = (tree / "compose" / "a" / "docker-compose.yaml").read_text()
    assert "reg.test/foo:t1" in compose and "@sha256" not in compose  # tags kept despite a known digest
    up = (tree / "up.sh").read_text()
    assert "images.tar" in up and "reg.test/foo:t1" in up             # guarded self-load on first up
    assert "docker tag " not in up and "docker tag " not in (tree / "pull.sh").read_text()  # bundle
    #                                  mode keeps tag refs throughout — bake() must emit NO aliases
    assert "docker load" in (tree / "load.sh").read_text()
    assert "load)" in (tree / "run.sh").read_text()
    meta = (tree / "metadata.yaml").read_text()
    assert "pinning: tag+bundle" in meta and "fakeimageid" in meta    # audit ids recorded
    assert "reg.test/foo@sha256:deadbeef" in meta                     # digest kept as metadata, not as ref
    lock = (tree / "rig.lock").read_text()
    assert "public/demo@1.0.0" in lock                                # deployment lock carried through
    assert "reg.test/foo:t1" in lock                                  # + bake's images section merged in


def test_bake_preserves_all_three_tiers_and_script_order():
    # §3d: the baked vehicle.yaml must keep autonomy entries under `autonomy:` (not demote them to
    # sensors), and the compose-only up.sh must run infra -> sensor -> autonomy (down.sh reversed).
    import os

    from rig_cli.bake import bake, unbake
    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor
    from rig_cli.manifest import load_manifest

    svc = pathlib.Path(tempfile.mkdtemp())
    (svc / "rigging.yaml").write_text("service: demo\nlauncher: demo-up\nlaunch_surface: [demo-up]\n")
    (svc / "demo-up").write_text(_COMPOSE_LAUNCHER)
    (svc / "demo-up").chmod(0o755)
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config" / "sensors").mkdir(parents=True)
    (root / "vehicle.yaml").write_text(
        "vehicle: t\n"
        "autonomy: [{name: planner, service: demo, config: config/sensors/planner.yaml, order: 1}]\n"
        "infra: [{name: router, service: demo, config: config/sensors/router.yaml, order: 99}]\n"
        "sensors: [{name: gnss, service: demo, config: config/sensors/gnss.yaml, order: 50}]\n")
    (root / "services.yaml").write_text(f"services: {{demo: {{path: {svc}}}}}\n")
    for n in ("planner", "router", "gnss"):
        (root / "config" / "sensors" / f"{n}.yaml").write_text(f"service: demo\nname: {n}\n")
    m, cat = load_manifest(root), load_catalog(root)
    descs = {"demo": load_descriptor("demo", svc)}

    shim = pathlib.Path(tempfile.mkdtemp())     # digest resolution shells out to docker — fake it
    (shim / "docker").write_text(_DOCKER_SHIM)
    (shim / "docker").chmod(0o755)
    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{shim}:{old_path}"
    try:
        artifact = bake(root, m, cat, descs, {"PATH": f"{shim}:/usr/bin:/bin"}, "t")
    finally:
        os.environ["PATH"] = old_path

    tree = unbake(artifact, pathlib.Path(tempfile.mkdtemp()))
    m2 = load_manifest(tree)
    assert {s.name: s.tier for s in m2.sensors} == {"router": "infra", "gnss": "sensor",
                                                    "planner": "autonomy"}  # round-trip keeps the tiers
    up = (tree / "up.sh").read_text()
    down = (tree / "down.sh").read_text()
    assert up.index('"router"') < up.index('"gnss"') < up.index('"planner"')       # autonomy up LAST
    assert down.index('"planner"') < down.index('"gnss"') < down.index('"router"')  # autonomy down FIRST
    cleanup = (tree / "cleanup.sh").read_text()                        # bake() must EMIT cleanup.sh,
    assert "reg.test/foo:t1" in cleanup                                # untagging the tag ref
    assert "reg.test/foo@sha256:deadbeef" in cleanup                   # AND the pinned digest ref


def test_rebake_inside_extracted_artifact_stamps_parent():
    from rig_cli.bake import bake, unbake
    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor
    from rig_cli.manifest import load_manifest

    root, m, cat, descs = _deployment_fixture("#!/bin/sh\nexit 1\n")  # compose-only skips; tree still bakes
    artifact = bake(root, m, cat, descs, {}, "day0")
    tree = unbake(artifact, pathlib.Path(tempfile.mkdtemp()))
    m2, cat2 = load_manifest(tree), load_catalog(tree)
    descs2 = {"demo": load_descriptor("demo", cat2["demo"].path)}
    bake(tree, m2, cat2, descs2, {}, "day0-final")                     # re-bake the extracted field state
    meta = (tree / "var" / "bake" / "day0-final" / "metadata.yaml").read_text()
    assert "parent:" in meta and "tag: day0" in meta and "rig_version:" in meta


# --- appended coverage: bake_fleet, _bundle_images/_resolve_digest error paths, mirror tags ------

def _docker_on_path(body: str):
    """Context manager: prepend a mkdtemp holding a fake `docker` sh script to PATH (restored on
    exit) — bake's digest/bundle helpers shell out via os.environ, not a passed env."""
    import contextlib
    import os

    @contextlib.contextmanager
    def _cm():
        shim = pathlib.Path(tempfile.mkdtemp())
        (shim / "docker").write_text(body)
        (shim / "docker").chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{shim}:{old}"
        try:
            yield shim
        finally:
            os.environ["PATH"] = old
    return _cm()


def _fleet_fixture() -> pathlib.Path:
    """A fleet-SHAPED deployment: fleet-ness is a property of the tree ({{var}} markers in
    vehicle.yaml / config), never a bake flag. Identity markers + one mandatory config var
    ({{gcs_ip}}) + one fleet-defaulted var (vars: rtsp_port) to exercise the example split."""
    svc = pathlib.Path(tempfile.mkdtemp()) / "camsvc"
    svc.mkdir(parents=True)
    (svc / "rigging.yaml").write_text("service: camsvc\nlauncher: camsvc-up\n"
                                      "launch_surface: [camsvc-up]\n")
    (svc / "camsvc-up").write_text("#!/bin/sh\n")
    (svc / "camsvc-up").chmod(0o755)
    root = pathlib.Path(tempfile.mkdtemp()) / "veh"
    (root / "config" / "sensors").mkdir(parents=True)
    (root / "vehicle.yaml").write_text(
        'vehicle: "{{vehicle}}"\n'
        'vehicle_id: "{{vehicle_id}}"\n'
        "vars: {rtsp_port: 8554}\n"
        "sensors:\n"
        "  - {name: cam, service: camsvc, config: config/sensors/cam.yaml}\n")
    (root / "config" / "sensors" / "cam.yaml").write_text(
        "service: camsvc\nname: cam\n"
        "rtsp: {url: 'rtsp://10.160.{{vehicle_id}}.80:{{rtsp_port}}/main'}\n"
        "gcs: {ip: '{{gcs_ip}}'}\n")
    (root / "services.yaml").write_text(f"services:\n  camsvc: {{ path: {svc} }}\n")
    return root


def test_bake_fleet_stages_unresolved_tree_and_splits_vars():
    from rig_cli.bake import bake_fleet, is_fleet, unbake

    root = _fleet_fixture()
    assert is_fleet(root)                       # the tree's markers alone make it a fleet artifact
    artifact = bake_fleet(root, "f1")
    tree = unbake(artifact, pathlib.Path(tempfile.mkdtemp()))
    # The staged tree is the UNRESOLVED template, verbatim — rendering happens on-vehicle; a
    # resolved value baked in here would silently weld ONE vehicle's identity into all N.
    veh = (tree / "vehicle.yaml").read_text()
    assert "{{vehicle}}" in veh and "{{vehicle_id}}" in veh
    cam = (tree / "config" / "sensors" / "cam.yaml").read_text()
    assert "{{rtsp_port}}" in cam and "{{gcs_ip}}" in cam
    # vehicle.local.example.yaml: mandatory (nothing supplies them) split from fleet-defaulted
    example = (tree / "vehicle.local.example.yaml").read_text()
    assert "vehicle: my-vehicle      # REQUIRED" in example
    assert "vehicle_id: 1            # REQUIRED" in example
    assert "# vars: {gcs_ip: <value>}   # REQUIRED" in example
    assert "# vars: {rtsp_port: 8554}   # optional — fleet default shown" in example
    # ...and the split is exclusive: a fleet-DEFAULTED var must not also be listed as mandatory
    # (an operator told rtsp_port is REQUIRED would hand-write a value the fleet already sets).
    assert "rtsp_port: <value>" not in example
    assert example.count("# REQUIRED") == 3          # exactly gcs_ip + vehicle + vehicle_id
    # provisioning + vendored launch surfaces travel with the artifact; no compose-only form
    # (it would embed one vehicle's values) — run.sh falls through to the bundled rig.
    assert (tree / "provision.sh").is_file() and (tree / "provision.sh").stat().st_mode & 0o111
    assert (tree / "services" / "camsvc" / "rigging.yaml").is_file()
    assert (tree / "services" / "camsvc" / "camsvc-up").is_file()
    assert (tree / "rig").is_file() and (tree / "run.sh").is_file()
    assert not (tree / "up.sh").exists()
    assert "fleet: true" in (tree / "metadata.yaml").read_text()


def test_bake_fleet_refuses_bundle_images_and_registry_override():
    from rig_cli import RigError
    from rig_cli.bake import bake_fleet

    root = _fleet_fixture()
    # --bundle-images needs the resolved compose-only form a fleet artifact deliberately omits;
    # --registry would bake one override into values that must resolve per vehicle.
    for kwargs, needle in (({"bundle_images": True}, "compose-only"),
                           ({"registry": "reg:5000"}, "per-vehicle")):
        try:
            bake_fleet(root, "f2", **kwargs)
            raise AssertionError(f"expected RigError for {kwargs}")
        except RigError as exc:
            assert needle in str(exc), f"{kwargs} -> {exc}"
    assert not (root / "var" / "bake" / "f2").exists()  # refused before any staging


def test_bundle_images_hard_errors():
    from rig_cli import RigError
    from rig_cli.bake import _bundle_images

    staging = pathlib.Path(tempfile.mkdtemp())
    # A ref neither local nor pullable is a HARD error (a silently incomplete bundle would
    # betray the air-gap promise): `image inspect` fails before AND after the pull attempt.
    with _docker_on_path("#!/bin/sh\nexit 1\n"):
        try:
            _bundle_images(staging, ["reg.test/foo:t1"])
            raise AssertionError("expected RigError for unpullable ref")
        except RigError as exc:
            assert "not in the local store and not pullable" in str(exc)
            assert "reg.test/foo:t1" in str(exc)
    assert not (staging / "images.tar").exists()
    # Images all present but `docker save` fails (e.g. disk full): hard error, stderr surfaced.
    save_fail = ("#!/bin/sh\n"
                 'case "$1" in\n'
                 "  image) exit 0 ;;\n"
                 '  save) echo "write images.tar: no space left on device" >&2; exit 1 ;;\n'
                 "esac\n"
                 "exit 0\n")
    with _docker_on_path(save_fail):
        try:
            _bundle_images(staging, ["reg.test/foo:t1"])
            raise AssertionError("expected RigError for failed save")
        except RigError as exc:
            assert "docker save failed" in str(exc) and "no space left" in str(exc)


def test_resolve_digest_registry_fallback_chain():
    from rig_cli.bake import _resolve_digest

    # Step 1 (local RepoDigests) fails -> step 2 (buildx imagetools, the registry) still pins.
    buildx_only = ("#!/bin/sh\n"
                   'case "$1" in\n'
                   "  inspect) exit 1 ;;\n"
                   '  buildx) echo "sha256:beefcafe"; exit 0 ;;\n'
                   "esac\n"
                   "exit 1\n")
    with _docker_on_path(buildx_only):
        assert _resolve_digest("reg.test/foo:t1") == "reg.test/foo@sha256:beefcafe"
        # local_only (bundle mode) must NOT take the registry round-trip even when it would
        # answer — the whole point is not stalling on an unreachable registry.
        assert _resolve_digest("reg.test/foo:t1", local_only=True) is None
    # Both sources fail -> None, and the caller leaves the ref as a tag (graceful degrade).
    with _docker_on_path("#!/bin/sh\nexit 1\n"):
        assert _resolve_digest("reg.test/foo:t1") is None


_MIRROR_LAUNCHER = """\
#!/bin/sh
[ "$2" = config ] || exit 0
printf 'services:\\n  core:\\n    image: reg.test/foo:t1\\n  router:\\n    image: thirdparty/zenoh:1.0\\n'
"""

# Digest shim answering PER REF (repo derived from the inspected ref, $4) — so BOTH refs are
# pinnable, and only the mirror exemption keeps the third-party ref a tag. A shim that only
# knows foo's digest would leave zenoh a tag by accident, guarding nothing.
_PER_REF_DIGEST_SHIM = """\
#!/bin/sh
case "$1" in
  inspect) repo="${4%%:*}"; echo "[\\"$repo@sha256:deadbeef\\"]"; exit 0 ;;
esac
exit 0
"""


def test_mirror_declared_refs_stay_tags_while_built_refs_pin():
    import os

    import yaml as _yaml

    from rig_cli.bake import bake, unbake
    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor
    from rig_cli.manifest import load_manifest

    svc = pathlib.Path(tempfile.mkdtemp())
    (svc / "rigging.yaml").write_text("service: demo\nlauncher: demo-up\nlaunch_surface: [demo-up]\n"
                                      "mirror: [thirdparty/zenoh:1.0]\n")
    (svc / "demo-up").write_text(_MIRROR_LAUNCHER)
    (svc / "demo-up").chmod(0o755)
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "config" / "sensors").mkdir(parents=True)
    (root / "vehicle.yaml").write_text(
        "vehicle: t\nsensors: [{name: a, service: demo, config: config/sensors/a.yaml}]\n")
    (root / "services.yaml").write_text(f"services: {{demo: {{path: {svc}}}}}\n")
    (root / "config" / "sensors" / "a.yaml").write_text("service: demo\nname: a\n")
    m, cat, descs = load_manifest(root), load_catalog(root), {"demo": load_descriptor("demo", svc)}

    with _docker_on_path(_PER_REF_DIGEST_SHIM) as shim:
        artifact = bake(root, m, cat, descs, {"PATH": f"{shim}:/usr/bin:/bin"}, "t")
    tree = unbake(artifact, pathlib.Path(tempfile.mkdtemp()))
    compose = (tree / "compose" / "a" / "docker-compose.yaml").read_text()
    assert "reg.test/foo@sha256:deadbeef" in compose     # built ref: digest-pinned
    assert "thirdparty/zenoh:1.0" in compose             # mirrored third-party: the TAG survives
    assert "thirdparty/zenoh@" not in compose            # ...never digest-pinned (fragile digests)
    meta = _yaml.safe_load((tree / "metadata.yaml").read_text())
    assert meta["images"]["reg.test/foo:t1"] == "reg.test/foo@sha256:deadbeef"
    assert meta["images"]["thirdparty/zenoh:1.0"] is None   # recorded unpinned in the audit map


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, "->", exc)
    sys.exit(1 if failures else 0)
