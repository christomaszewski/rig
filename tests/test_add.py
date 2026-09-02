"""rig add — wiring one more service into an EXISTING deployment. Run: python3 tests/test_add.py"""
import contextlib
import io
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError  # noqa: E402
from rig_cli.init import add_service, init  # noqa: E402
from rig_cli.manifest import load_manifest  # noqa: E402


def _ws() -> pathlib.Path:
    """Workspace: a rig-infra-style collection, a sensor repo, an autonomy repo, and a bare deployment."""
    ws = pathlib.Path(tempfile.mkdtemp())
    zr = ws / "my-infra" / "zenoh-router"
    (zr / "config").mkdir(parents=True)
    (zr / "rigging.yaml").write_text("service: zenoh-router\nlauncher: zenoh-router-up\ntier: infra\n")
    (zr / "zenoh-router-up").write_text("#!/bin/sh\n")
    (zr / "zenoh-router-up").chmod(0o755)
    (zr / "config" / "zenoh-router.example.yaml").write_text("service: zenoh-router\nname: zr\n")
    foo = ws / "foo_driver"
    (foo / "sensors").mkdir(parents=True)
    (foo / "rigging.yaml").write_text("service: foo\nlauncher: foo-up\nexamples: [sensors/foo.example.yaml]\n")
    (foo / "foo-up").write_text("#!/bin/sh\n")
    (foo / "foo-up").chmod(0o755)
    (foo / "sensors" / "foo.example.yaml").write_text("service: foo\nname: front\nconnection: {type: tcp}\n")
    nav = ws / "nav"
    (nav / "config").mkdir(parents=True)
    (nav / "rigging.yaml").write_text("service: nav\nlauncher: nav-up\ntier: autonomy\n")
    (nav / "nav-up").write_text("#!/bin/sh\n")
    (nav / "nav-up").chmod(0o755)
    (nav / "config" / "nav.example.yaml").write_text("service: nav\nname: brain\n")
    init(ws / "veh")  # bare deployment: empty `services: {}` catalog, headers only
    return ws


def test_add_infra_bare_name_wires_enabled_row():
    from rig_cli import doctor
    from rig_cli.catalog import load_catalog
    from rig_cli.descriptor import load_descriptor

    ws = _ws()
    veh = ws / "veh"
    add_service(veh, "zenoh-router")                     # bare name -> workspace scan
    assert "zenoh-router: { path: ../my-infra/zenoh-router }" in (veh / "services.yaml").read_text()
    body = (veh / "vehicle.yaml").read_text()
    assert "- { name: zr, service: zenoh-router, config: config/infra/zr.yaml, enabled: true, order: 0 }" in body
    assert (veh / "config" / "infra" / "zr.yaml").is_file()
    manifest, catalog = load_manifest(veh), load_catalog(veh)
    assert [s.name for s in manifest.sensors] == ["zr"]  # ENABLED — infra is decidable
    descs = {s.service: load_descriptor(s.service, catalog[s.service].path) for s in manifest.sensors}
    assert not [i for i in doctor.collect(manifest, catalog, descs) if i.level == "ERROR"]


def test_add_sensor_path_token_menu_row_only():
    ws = _ws()
    veh = ws / "veh"
    add_service(veh, str(ws / "foo_driver"))             # path form; tier sensor -> MENU only
    body = (veh / "vehicle.yaml").read_text()
    assert "# - { name: foo, service: foo, config: config/sensors/foo.yaml, enabled: true, order: 10 }" in body
    copied = (veh / "config" / "sensors" / "foo.yaml").read_text()
    assert "# name: front" in copied                     # nameless profile: the row stamps the name
    assert not load_manifest(veh).sensors                # nothing enabled behind the operator's back
    v = veh / "vehicle.yaml"                             # the whole remaining workflow: uncomment ONE line
    v.write_text(v.read_text().replace("# - { name: foo,", "- { name: foo,"))
    assert [s.name for s in load_manifest(veh).sensors] == ["foo"]


def test_add_autonomy_lands_under_autonomy_header():
    ws = _ws()
    veh = ws / "veh"
    add_service(veh, "nav")
    body = (veh / "vehicle.yaml").read_text()
    assert "# - { name: nav, service: nav, config: config/autonomy/nav.yaml" in body
    assert body.index("config/autonomy/nav.yaml") > body.index("autonomy:")


def test_add_appends_missing_section_header():
    # A pre-autonomy deployment has no `autonomy:` header — add creates the section at EOF.
    ws = _ws()
    veh = ws / "veh"
    (veh / "vehicle.yaml").write_text("vehicle: veh\nsensors:\n")
    add_service(veh, "nav")
    body = (veh / "vehicle.yaml").read_text()
    assert "autonomy:" in body and "# - { name: nav," in body
    assert load_manifest(veh) is not None                # still a valid manifest


def test_add_tier_override_promotes_sensor_to_infra():
    # A repo that declares no tier (sensor default) but IS infra for this vehicle: --tier flips both
    # the section AND the enabled-vs-menu behavior.
    ws = _ws()
    veh = ws / "veh"
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        add_service(veh, str(ws / "foo_driver"), tier="infra")
    body = (veh / "vehicle.yaml").read_text()
    assert "- { name: front, service: foo, config: config/infra/front.yaml, enabled: true, order: 5 }" in body
    assert body.index("service: foo") < body.index("sensors:")        # row landed in the infra section
    assert (veh / "config" / "infra" / "front.yaml").is_file()        # config follows the tier subdir
    assert [s.tier for s in load_manifest(veh).sensors] == ["infra"]  # runtime tier = section
    assert "overrides foo's declared 'sensor'" in err.getvalue()      # nudge toward fixing the rigging


def test_add_tier_override_demotes_infra_to_menu():
    ws = _ws()
    veh = ws / "veh"
    add_service(veh, "zenoh-router", tier="autonomy")                 # deliberate nonsense tier — honored
    body = (veh / "vehicle.yaml").read_text()
    assert "# - { name: zenoh_router, service: zenoh-router, config: config/autonomy/zenoh-router.yaml" in body
    assert not load_manifest(veh).sensors                             # menu row: nothing enabled
    try:
        add_service(veh, "nav", tier="bogus")
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "--tier" in str(exc)


def test_add_duplicate_service_errors():
    ws = _ws()
    veh = ws / "veh"
    add_service(veh, "zenoh-router")
    try:
        add_service(veh, "zenoh-router")
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "already routed" in str(exc)


def test_add_flow_style_catalog_falls_back_to_snippet():
    ws = _ws()
    veh = ws / "veh"
    (veh / "services.yaml").write_text("services: { x: { path: /tmp/x } }\n")  # hand-authored flow style
    before_svc = (veh / "services.yaml").read_text()
    before_veh = (veh / "vehicle.yaml").read_text()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        add_service(veh, "zenoh-router")
    assert "paste" in err.getvalue() and "zenoh-router: { path:" in err.getvalue()
    assert (veh / "services.yaml").read_text() == before_svc   # BOTH files untouched
    assert (veh / "vehicle.yaml").read_text() == before_veh


def test_add_instance_name_collision_errors():
    ws = _ws()
    wid = ws / "my-infra" / "widget"                     # second infra service whose example ALSO says zr
    (wid / "config").mkdir(parents=True)
    (wid / "rigging.yaml").write_text("service: widget\nlauncher: widget-up\ntier: infra\n")
    (wid / "config" / "widget.example.yaml").write_text("service: widget\nname: zr\n")
    veh = ws / "veh"
    add_service(veh, "zenoh-router")
    try:
        add_service(veh, "widget")
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "zr" in str(exc) and "already exists" in str(exc)


def test_append_tier_row_understands_pyyaml_indentless_sections():
    import yaml

    from rig_cli.init import _append_tier_row
    # what yaml.safe_dump writes (every captured/baked vehicle.yaml): `- name:` at column 0
    text = yaml.safe_dump({"vehicle": "v",
                           "sensors": [{"name": "cam", "service": "s", "order": 10}],
                           "autonomy": [{"name": "nav", "service": "p", "order": 20}],
                           "data_dir": "/d"}, sort_keys=False)
    assert "\n- name: nav" in text  # the shape under test really is indentless
    row = "- { name: bag_player, service: ros2-bag-player, config: c.yaml, enabled: false, order: 999 }"
    doc = yaml.safe_load(_append_tier_row(text, "autonomy", row))
    assert [r["name"] for r in doc["autonomy"]] == ["nav", "bag_player"] and doc["data_dir"] == "/d"
    doc2 = yaml.safe_load(_append_tier_row(text, "sensors", row))
    assert [r["name"] for r in doc2["sensors"]] == ["cam", "bag_player"] and len(doc2["autonomy"]) == 1
    # the generated (indented) form is untouched by the change
    gen = "vehicle: v\nautonomy:\n  - { name: nav, service: p, order: 20 }\n\ndata_dir: /d\n"
    out = _append_tier_row(gen, "autonomy", row)
    assert "\n  - { name: bag_player" in out and yaml.safe_load(out)["autonomy"][1]["order"] == 999


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
