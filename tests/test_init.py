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


def test_init_defaults_identity_to_per_host_markers():
    # v0.2.20: no --vehicle-id -> MARKERS, not "vehicle 1 by accident". The tree loads lazily (manifest
    # reports the identity as missing) and identity-consuming verbs point at provision/RIG_VEHICLE_ID;
    # a vehicle-plan install onto such a tree keeps the markers (fleet-style reproduction).
    from rig_cli.manifest import load_manifest
    target = pathlib.Path(tempfile.mkdtemp()) / "skiff3"
    init(target, no_git=True)
    body = (target / "vehicle.yaml").read_text()
    assert 'vehicle: "{{vehicle}}"' in body and 'vehicle_id: "{{vehicle_id}}"' in body
    m = load_manifest(target)
    assert m.vehicle_id is None and {"vehicle", "vehicle_id"} <= set(m.missing_identity)


def test_init_scaffolds_vars_convention_and_gitignores_operational_files():
    target = pathlib.Path(tempfile.mkdtemp()) / "skiff2"
    init(target, no_git=True)
    body = (target / "vehicle.yaml").read_text()
    assert "# vars:" in body and "gcs_ip" in body               # the convention is VISIBLE
    assert "{{map fleet_peer_ids peer_endpoint}}" in body       # worked example, format-escaped
    assert "Planned" not in body                                # fleet mode shipped; comment fixed
    ignored = (target / ".gitignore").read_text()
    assert "vehicle.local.yaml" in ignored and "fleet.yaml" in ignored  # operational state


def _rig_infra_ws() -> pathlib.Path:
    """A workspace with a rig-infra-style checkout beside the target: zenoh-router + both bag loggers
    (which collide on instance name `bag_logger`, like the real ones)."""
    ws = pathlib.Path(tempfile.mkdtemp())
    coll = ws / "rig-infra"
    for svc, inst in (("zenoh-router", "zenoh-router"), ("ros2-bag-logger", "bag_logger"),
                      ("ros1-bag-logger", "bag_logger")):
        d = coll / svc
        (d / "config").mkdir(parents=True)
        (d / "rigging.yaml").write_text(f"service: {svc}\nlauncher: {svc}-up\ntier: infra\n")
        (d / f"{svc}-up").write_text("#!/bin/sh\n")
        (d / f"{svc}-up").chmod(0o755)
        (d / "config" / f"{svc}.example.yaml").write_text(f"service: {svc}\nname: {inst}\n")
    return ws


def test_init_infra_wires_workspace_service_and_doctor_is_green():
    from rig_cli import doctor
    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor
    from rig_cli.manifest import load_manifest

    target = _rig_infra_ws() / "veh"
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


def test_init_infra_rejects_unknown_service_and_collisions():
    target = _rig_infra_ws() / "veh"
    try:
        init(target, infra=["nope"])
        raise AssertionError("expected RigError for unknown service")
    except RigError as exc:
        assert "rig-infra" in str(exc)  # points at the checkout to clone
    target2 = _rig_infra_ws() / "veh"
    try:  # both bag loggers claim instance `bag_logger` (and mix distros) — refuse early
        init(target2, infra=["ros2-bag-logger", "ros1-bag-logger"])
        raise AssertionError("expected RigError for instance collision")
    except RigError as exc:
        assert "bag_logger" in str(exc)


def test_init_failed_run_leaves_nothing_and_retry_succeeds():
    # Pre-flight validation: a bad --infra list must write NOTHING, so the corrected retry just works
    # (previously the first service's config leaked out and wedged the retry on a bogus collision).
    target = _rig_infra_ws() / "veh"
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
    target = _rig_infra_ws() / "veh"
    init(target, infra=["zenoh-router/"])  # tab-completion slash
    body = (target / "vehicle.yaml").read_text()
    assert "service: zenoh-router," in body and "order: 0 }" in body  # key clean, router still pinned
    assert "zenoh-router/" not in (target / "services.yaml").read_text().replace("rig-infra/zenoh-router", "")


def test_init_yaml_hostile_dirname_is_quoted():
    import yaml as _yaml

    target = pathlib.Path(tempfile.mkdtemp()) / "veh #1"
    init(target, vehicle_id=1)   # literal-identity shape: the dir name seeds `vehicle:`
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


def _infra_collection_ws() -> pathlib.Path:
    """A workspace holding a rig-infra-style COLLECTION repo: service dirs one level down."""
    ws = pathlib.Path(tempfile.mkdtemp())
    coll = ws / "my-infra"
    for svc, inst in (("zenoh-router", "zr"), ("widget", "wid")):
        d = coll / svc
        (d / "config").mkdir(parents=True)
        (d / "rigging.yaml").write_text(f"service: {svc}\nlauncher: {svc}-up\ntier: infra\n")
        (d / f"{svc}-up").write_text("#!/bin/sh\n")
        (d / f"{svc}-up").chmod(0o755)
        (d / "config" / f"{svc}.example.yaml").write_text(f"service: {svc}\nname: {inst}\n")
    return ws


def test_init_infra_bare_name_resolves_from_workspace_not_templates():
    from rig_cli import doctor
    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor
    from rig_cli.manifest import load_manifest

    ws = _infra_collection_ws()
    target = ws / "veh"
    init(target, infra=["zenoh-router"])          # bare name -> one-level workspace scan wins
    services = (target / "services.yaml").read_text()
    assert "../my-infra/zenoh-router" in services and "templates" not in services
    body = (target / "vehicle.yaml").read_text()
    assert "- { name: zr, service: zenoh-router, config: config/infra/zr.yaml, enabled: true, order: 0 }" in body
    manifest, catalog = load_manifest(target), load_catalog(target)   # pin survives; doctor green
    descs = {s.service: load_descriptor(s.service, catalog[s.service].path) for s in manifest.sensors}
    assert not [i for i in doctor.collect(manifest, catalog, descs) if i.level == "ERROR"]


def test_init_infra_path_token_wires_by_descriptor_service():
    ws = _infra_collection_ws()
    target = ws / "veh"
    init(target, infra=[str(ws / "my-infra" / "widget")])   # contains '/' -> path form
    services = (target / "services.yaml").read_text()
    assert "widget:" in services and "../my-infra/widget" in services  # catalog key = descriptor service
    assert (target / "config" / "infra" / "wid.yaml").is_file()


def test_init_infra_bare_name_ambiguity_is_an_error():
    ws = _infra_collection_ws()
    other = ws / "other-infra" / "zenoh-router"
    (other / "config").mkdir(parents=True)
    (other / "rigging.yaml").write_text("service: zenoh-router\nlauncher: zenoh-router-up\ntier: infra\n")
    (other / "config" / "z.example.yaml").write_text("service: zenoh-router\nname: zr2\n")
    try:
        init(ws / "veh", infra=["zenoh-router"])
        raise AssertionError("expected RigError for ambiguous bare name")
    except RigError as exc:
        assert "ambiguous" in str(exc) and "my-infra" in str(exc) and "other-infra" in str(exc)
    assert not (ws / "veh" / "vehicle.yaml").exists()        # pre-flight: nothing written


def test_init_discover_descends_one_level_and_skips_var():
    ws = _infra_collection_ws()
    trap = ws / "my-infra" / "var"                            # rendered state, never a service
    trap.mkdir()
    (trap / "rigging.yaml").write_text("service: trap\nlauncher: trap-up\n")
    target = ws / "veh"
    init(target, discover=ws)
    services = (target / "services.yaml").read_text()
    assert "zenoh-router:" in services and "widget:" in services   # found one level down
    assert "../my-infra/zenoh-router" in services
    assert "trap" not in services                                  # var/ skipped
    vehicle = (target / "vehicle.yaml").read_text()  # nameless-profile copy -> stub named from the stem
    assert "# - { name: zenoh_router, service: zenoh-router, config: config/infra/zenoh-router.yaml" in vehicle


def test_init_infra_plus_discover_overlap_is_quiet():
    # THE recommended invocation (--infra + --discover over one workspace) rediscovers the very dirs
    # --infra just wired — that must not print "duplicate service" noise about its own designed behavior.
    import contextlib
    import io

    ws = _infra_collection_ws()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        init(ws / "veh", infra=["zenoh-router"], discover=ws)
    assert "duplicate" not in err.getvalue()
    services = (ws / "veh" / "services.yaml").read_text()
    assert services.count("zenoh-router:") == 1          # routed once, by --infra
    assert "widget:" in services                          # the rest of the collection still discovered
    vehicle = (ws / "veh" / "vehicle.yaml").read_text()
    assert vehicle.count("service: zenoh-router") == 1    # enabled row only — no duplicate menu stub


def test_init_discover_conflicting_checkouts_still_warn():
    # Two DIFFERENT dirs claiming one service key is a real decision — the warning stays, naming both.
    import contextlib
    import io

    ws = _infra_collection_ws()
    other = ws / "other-infra" / "zenoh-router"
    (other / "config").mkdir(parents=True)
    (other / "rigging.yaml").write_text("service: zenoh-router\nlauncher: zenoh-router-up\ntier: infra\n")
    (other / "config" / "z.example.yaml").write_text("service: zenoh-router\nname: zr2\n")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        init(ws / "veh", discover=ws)
    out = err.getvalue()
    assert "duplicate service 'zenoh-router'" in out and "keeping" in out
    assert (ws / "veh" / "services.yaml").read_text().count("zenoh-router:") == 1


def test_stale_bundled_templates_path_errors_with_rig_infra_pointer():
    # templates/ was deleted in v0.1.35 — an old services.yaml path under it must fail WITH the
    # rig-infra pointer, not a generic "no rigging.yaml" mystery.
    import rig_cli
    from rig_cli.descriptor import load_descriptor

    bundled = pathlib.Path(rig_cli.__file__).resolve().parent.parent / "templates"
    try:
        load_descriptor("zenoh-router", bundled / "no-such-service")
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "rig-infra" in str(exc) and "../rig-infra/no-such-service" in str(exc)


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
