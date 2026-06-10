"""bake compose-transforms. Run: `.venv/bin/python tests/test_bake.py`."""
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
    assert "docker load" in (tree / "load.sh").read_text()
    assert "load)" in (tree / "run.sh").read_text()
    meta = (tree / "metadata.yaml").read_text()
    assert "pinning: tag+bundle" in meta and "fakeimageid" in meta    # audit ids recorded
    assert "reg.test/foo@sha256:deadbeef" in meta                     # digest kept as metadata, not as ref


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
