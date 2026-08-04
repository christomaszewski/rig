"""init — deployment scaffold. Run: python3 tests/test_init.py"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError  # noqa: E402
from rig_cli.init import init  # noqa: E402


def test_init_scaffolds_infra_and_sensors_config_dirs():
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    init(target)
    assert (target / "config" / "sensors" / ".gitkeep").is_file()
    assert (target / "config" / "infra" / ".gitkeep").is_file()  # infra tier scaffolded alongside sensors
    assert (target / "config" / "autonomy" / ".gitkeep").is_file()  # autonomy tier too (§3d)
    assert (target / "services").is_dir()
    assert (target / "vehicle.yaml").is_file() and (target / "services.yaml").is_file()
    assert "autonomy:" in (target / "vehicle.yaml").read_text()  # bare header, uncommentable menu section


def test_init_refuses_an_existing_deployment():
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    init(target)
    try:
        init(target)
        raise AssertionError("expected RigError on re-init")
    except RigError:
        pass


def test_init_seeds_vehicle_name_and_id_from_args():
    target = pathlib.Path(tempfile.mkdtemp()) / "skiff1"
    init(target, vehicle_id=7)
    body = (target / "vehicle.yaml").read_text()
    assert "vehicle: skiff1" in body and "vehicle_id: 7" in body


def test_init_infra_wires_template_and_doctor_is_green():
    from rig_cli import doctor
    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor
    from rig_cli.manifest import load_manifest

    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    init(target, infra=["zenoh-router", "ros2-bag-logger"])
    body = (target / "vehicle.yaml").read_text()
    assert "- { name: zenoh-router, service: zenoh-router, config: config/infra/zenoh-router.yaml, enabled: true, order: 0 }" in body
    assert "- { name: bag_logger, service: ros2-bag-logger" in body
    assert (target / "config" / "infra" / "zenoh-router.yaml").is_file()
    assert (target / "config" / "infra" / "bag_logger.yaml").is_file()
    # THE acceptance criterion: init --infra -> doctor green with zero manual edits.
    manifest, catalog = load_manifest(target), load_catalog(target)
    descs = {s.service: load_descriptor(s.service, catalog[s.service].path) for s in manifest.sensors}
    issues = doctor.collect(manifest, catalog, descs)
    assert not [i for i in issues if i.level == "ERROR"], [i.message for i in issues]


def test_init_infra_rejects_unknown_template_and_collisions():
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    try:
        init(target, infra=["nope"])
        raise AssertionError("expected RigError for unknown template")
    except RigError as exc:
        assert "zenoh-router" in str(exc)  # lists what IS available
    target2 = pathlib.Path(tempfile.mkdtemp()) / "veh"
    try:  # both bag loggers claim instance `bag_logger` (and mix distros) — refuse early
        init(target2, infra=["ros2-bag-logger", "ros1-bag-logger"])
        raise AssertionError("expected RigError for instance collision")
    except RigError as exc:
        assert "bag_logger" in str(exc)


def test_init_failed_run_leaves_nothing_and_retry_succeeds():
    # Pre-flight validation: a bad --infra list must write NOTHING, so the corrected retry just works
    # (previously the first template's config leaked out and wedged the retry on a bogus collision).
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    for bad in (["ros2-bag-logger", "ros1-bag-logger"], ["zenoh-router", "zenoh-rooter"]):
        try:
            init(target, infra=bad)
            raise AssertionError("expected RigError")
        except RigError:
            pass
        assert not (target / "vehicle.yaml").exists()
        assert not list((target / "config" / "infra").glob("*.yaml")) if (target / "config").exists() else True
    init(target, infra=["ros2-bag-logger"])  # the retry the error message suggests
    assert (target / "config" / "infra" / "bag_logger.yaml").is_file()


def test_init_infra_token_is_normalized():
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    init(target, infra=["zenoh-router/"])  # tab-completion slash
    body = (target / "vehicle.yaml").read_text()
    assert "service: zenoh-router," in body and "order: 0 }" in body  # key clean, router still pinned
    assert "zenoh-router/" not in (target / "services.yaml").read_text().replace("templates/zenoh-router", "")


def test_init_yaml_hostile_dirname_is_quoted():
    import yaml as _yaml

    target = pathlib.Path(tempfile.mkdtemp()) / "veh #1"
    init(target)
    data = _yaml.safe_load((target / "vehicle.yaml").read_text())
    assert data["vehicle"] == "veh #1"  # quoted, not truncated to "veh"


def _fake_workspace() -> pathlib.Path:
    ws = pathlib.Path(tempfile.mkdtemp())
    foo = ws / "foo_driver"                       # dirname != service name — the routing trap
    (foo / "sensors").mkdir(parents=True)
    (foo / "rigging.yaml").write_text("service: foo\nlauncher: foo-up\nexamples: [sensors/foo.example.yaml]\n")
    (foo / "foo-up").write_text("#!/bin/sh\n")
    (foo / "foo-up").chmod(0o755)                 # doctor checks the launcher is executable
    (foo / "sensors" / "foo.example.yaml").write_text("service: foo\nname: front\nconnection: {type: tcp}\n")
    bar = ws / "bar"                              # infra-tier, no example
    bar.mkdir()
    (bar / "rigging.yaml").write_text("service: bar\nlauncher: bar-up\ntier: infra\n")
    nav = ws / "nav_repo"                         # autonomy-tier, example via the config/ glob fallback
    (nav / "config").mkdir(parents=True)
    (nav / "rigging.yaml").write_text("service: nav\nlauncher: nav-up\ntier: autonomy\n")
    (nav / "config" / "nav.example.yaml").write_text("service: nav\nname: brain\nplanner: {rate: 10}\n")
    (ws / "plain").mkdir()                        # not a service repo -> ignored
    broken = ws / "broken"
    broken.mkdir()
    (broken / "rigging.yaml").write_text("service: [unclosed\n")
    return ws


def test_init_discover_catalogs_menus_and_profiles():
    from rig_cli.manifest import load_manifest

    ws = _fake_workspace()
    target = ws / "veh"
    init(target, discover=ws)
    services = (target / "services.yaml").read_text()
    assert "foo:" in services and "../foo_driver" in services   # routed by DESCRIPTOR name, not dirname
    assert "bar:" in services and "plain" not in services and "broken" not in services
    vehicle = (target / "vehicle.yaml").read_text()
    assert "# - { name: foo, service: foo, config: config/sensors/foo.yaml" in vehicle  # commented MENU
    assert "# - { name: bar, service: bar, config: config/infra/bar.yaml" in vehicle    # tier: infra hint
    assert "# - { name: nav, service: nav, config: config/autonomy/nav.yaml" in vehicle  # tier: autonomy hint
    assert vehicle.index("autonomy:") > vehicle.index("sensors:")  # menu section lands under `autonomy:`
    nav_cfg = (target / "config" / "autonomy" / "nav.yaml").read_text()  # example copied to the autonomy dir
    assert "# name: brain" in nav_cfg                                     # as a nameless profile
    copied = (target / "config" / "sensors" / "foo.yaml").read_text()
    assert "# name: front" in copied and "\nname:" not in copied  # nameless profile: manifest stamps the name
    assert not load_manifest(target).sensors                       # menu = nothing enabled

    # uncommenting ONE line is the whole remaining workflow — and doctor must be GREEN after it:
    from rig_cli import doctor
    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor

    v = target / "vehicle.yaml"
    v.write_text(v.read_text().replace("# - { name: foo,", "- { name: foo,"))
    manifest, catalog = load_manifest(target), load_catalog(target)
    assert [s.name for s in manifest.sensors] == ["foo"]
    descs = {s.service: load_descriptor(s.service, catalog[s.service].path) for s in manifest.sensors}
    issues = doctor.collect(manifest, catalog, descs)
    assert not [i for i in issues if i.level == "ERROR"], [i.message for i in issues]


def test_init_discover_pointed_at_a_single_repo():
    ws = _fake_workspace()
    target = pathlib.Path(tempfile.mkdtemp()) / "veh"
    init(target, discover=ws / "foo_driver")     # AT the repo, not its parent workspace
    assert "foo:" in (target / "services.yaml").read_text()


def test_init_discover_odd_name_spelling_falls_back_verbatim():
    from rig_cli.manifest import load_manifest

    ws = _fake_workspace()
    odd = ws / "odd"
    (odd / "sensors").mkdir(parents=True)
    (odd / "rigging.yaml").write_text("service: odd\nlauncher: odd-up\nexamples: [sensors/odd.example.yaml]\n")
    (odd / "sensors" / "odd.example.yaml").write_text("service: odd\nname:\n  front\n")  # next-line value
    target = ws / "veh"
    init(target, discover=ws)
    copied = (target / "config" / "sensors" / "odd.yaml").read_text()
    assert "name:" in copied and "# name:" not in copied          # verbatim: transform couldn't neutralize
    vehicle = (target / "vehicle.yaml").read_text()
    assert "# - { name: front, service: odd," in vehicle          # stub uses the file's OWN name
    v = target / "vehicle.yaml"
    v.write_text(vehicle.replace("# - { name: front,", "- { name: front,"))
    assert "front" in [s.name for s in load_manifest(target).sensors]  # cross-check happy


def test_descriptor_accepts_autonomy_tier():
    from rig_cli.descriptor import load_descriptor

    repo = pathlib.Path(tempfile.mkdtemp())
    (repo / "rigging.yaml").write_text("service: nav\nlauncher: nav-up\ntier: autonomy\n")
    assert load_descriptor("nav", repo).tier == "autonomy"


def test_descriptor_rejects_a_tier_typo():
    from rig_cli.descriptor import load_descriptor

    repo = pathlib.Path(tempfile.mkdtemp())
    (repo / "rigging.yaml").write_text("service: x\nlauncher: x-up\ntier: infrastructure\n")
    try:
        load_descriptor("x", repo)
        raise AssertionError("expected RigError for tier typo")
    except RigError as exc:
        assert "tier" in str(exc)


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
