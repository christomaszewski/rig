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
