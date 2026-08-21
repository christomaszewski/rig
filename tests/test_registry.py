"""Registry core — scaffold, validation rules, index generation. Run: python3 tests/test_registry.py"""
import contextlib
import io
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from rig_cli import RigError  # noqa: E402
from rig_cli.registry import (  # noqa: E402
    constraint_satisfied, generate_index, load_registry, render_index, validate_registry, write_index,
)
from rig_cli.registry_scaffold import registry_init  # noqa: E402

FULL_SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


def _registry(ns="testns") -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp()) / "reg"
    with contextlib.redirect_stderr(io.StringIO()):
        registry_init(root, namespace=ns)
    return root


def _pkg(root: pathlib.Path, plural: str, name: str, manifest: dict, files: dict = ()) -> None:
    d = root / plural / name
    d.mkdir(parents=True)
    (d / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    for rel, body in dict(files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def _service(root, name="camera-service", version="1.4.2", **over):
    m = {"kind": "service", "name": name, "version": version,
         "source": {"repo": "https://example.com/x.git", "rev": FULL_SHA}}
    m.update(over)
    _pkg(root, "services", name, m)


def _profile(root, name="siyi-zr30", version="1.0.0", requires="camera-service@^1.4",
             payload="service: camera-service\ngige: {camera_id: zr30}\n", dir_svc=None, **over):
    m = {"kind": "profile", "name": name, "version": version,
         "provides": {"sensor": [{"model": "SIYI ZR30", "match": ["zr30", "siyi-zr30"]}]},
         "requires": {"service": requires},
         "config": {"payload": "config/payload.yaml"}}
    m.update(over)
    # schema 2: profiles nest under their target service; dir_svc overrides for placement tests
    svc = dir_svc or str(requires).rpartition("/")[-1].partition("@")[0]
    _pkg(root, "profiles", f"{svc}/{name}", m, {"config/payload.yaml": payload})


def _overlay(root, name="zr30-gideon", version="1.0.0", targets=None, project="gideon",
             payload="gige: {fps: 15}\n", **over):
    m = {"kind": "overlay", "name": name, "version": version,
         "targets": targets or [{"service": "camera-service"}], "project": project,
         "config": {"payload": "config/delta.yaml"}}
    m.update(over)
    _pkg(root, "overlays", name, m, {"config/delta.yaml": payload})


def _reindex(root):
    write_index(load_registry(root, []))


def _issues(root, **kw):
    _, issues = validate_registry(root, **kw)
    return [f"{i.where}: {i.message}" for i in issues]


def _assert_issue(root, needle, **kw):
    msgs = _issues(root, **kw)
    assert any(needle in m for m in msgs), f"expected issue containing {needle!r}, got: {msgs}"


def test_scaffold_is_self_valid_and_complete():
    root = _registry()
    assert _issues(root) == []
    assert (root / "tools" / "validate").stat().st_mode & 0o111
    assert (root / "tools" / "gen-index").stat().st_mode & 0o111
    assert (root / ".github" / "workflows" / "ci.yaml").is_file()
    assert (root / ".gitlab-ci.yml").is_file()  # both CI wrappers ship (OQ-7)
    assert len(list((root / "schemas").glob("*.schema.json"))) == 7   # registry + index + 5 kinds
    meta = yaml.safe_load((root / "registry.yaml").read_text())
    assert meta["namespace"] == "testns" and meta["schema"] == 2


def test_scaffold_refuses_nonempty_dir():
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "junk.txt").write_text("x")
    try:
        registry_init(root)
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "not empty" in str(exc)


def test_full_valid_registry_is_green():
    root = _registry()
    _service(root)
    _profile(root)
    _overlay(root)
    _pkg(root, "suites", "gideon-boat", {
        "kind": "suite", "name": "gideon-boat", "version": "1.0.0", "project": "gideon",
        "members": {"profiles": ["testns/camera-service:siyi-zr30@1.0.0"],
                    "overlays": ["testns/zr30-gideon@1.0.0"],
                    "services": ["other/thing@2.0.0"]}})  # cross-ns: format-checked only
    _reindex(root)
    assert _issues(root) == []


def test_rejects_short_or_branch_rev():
    root = _registry()
    _service(root, source={"repo": "https://example.com/x.git", "rev": "main"})
    _assert_issue(root, "full commit SHA", check_index=False)
    root2 = _registry()
    _service(root2, source={"repo": "https://example.com/x.git", "rev": "abc123"})
    _assert_issue(root2, "full commit SHA", check_index=False)


def test_rejects_tag_image_refs():
    root = _registry()
    _service(root, source=None, image={"ref": "ghcr.io/x/cam:latest"})
    _assert_issue(root, "digest-pinned", check_index=False)
    root2 = _registry()  # tag AND digest — still rejected (digest-only means digest-ONLY)
    _service(root2, source=None, image={"ref": f"ghcr.io/x/cam:jp7@{DIGEST}"})
    _assert_issue(root2, "TAG", check_index=False)
    root3 = _registry()  # registry host WITH port is not a tag
    _service(root3, source=None, image={"ref": f"devbox:5000/cam@{DIGEST}"})
    assert not [m for m in _issues(root3, check_index=False) if "image" in m]


def test_service_needs_source_or_image():
    root = _registry()
    _pkg(root, "services", "empty-svc", {"kind": "service", "name": "empty-svc", "version": "1.0.0"})
    _assert_issue(root, "source", check_index=False)


def test_rejects_stale_index():
    root = _registry()
    _service(root)
    _reindex(root)
    assert _issues(root) == []
    _profile(root)  # new package, index NOT regenerated
    _assert_issue(root, "STALE")


def test_rejects_suite_inline_config_and_bad_refs():
    root = _registry()
    _profile(root, requires="camera-service@1.4.2")
    _service(root)
    _pkg(root, "suites", "bad-suite", {
        "kind": "suite", "name": "bad-suite", "version": "1.0.0",
        "members": {"profiles": ["siyi-zr30"]}},  # not fully qualified
        {"config/x.yaml": "a: 1\n"})              # inline config dir
    msgs = _issues(root, check_index=False)
    assert any("REFERENCES ONLY" in m for m in msgs)
    assert any("fully-qualified" in m for m in msgs)


def test_suite_member_version_must_match_registry_head():
    root = _registry()
    _service(root)
    _pkg(root, "suites", "s", {"kind": "suite", "name": "s", "version": "1.0.0",
                               "members": {"services": ["testns/camera-service@9.9.9"]}})
    _assert_issue(root, "carries 1.4.2", check_index=False)


def test_profile_rules():
    root = _registry()  # named payload -> rejected
    _service(root)
    _profile(root, payload="name: nope\nservice: camera-service\n")
    _assert_issue(root, "NAMELESS", check_index=False)
    root2 = _registry()  # unresolved requires
    _profile(root2, requires="ghost-service@^1.0")
    _assert_issue(root2, "does not resolve", check_index=False)
    root3 = _registry()  # resolved but version-unsatisfied
    _service(root3, version="1.2.0")
    _profile(root3, requires="camera-service@^1.4")
    _assert_issue(root3, "not satisfied", check_index=False)


def test_profile_payload_validates_against_own_schema():
    schema = json.dumps({"type": "object", "required": ["gige"],
                         "properties": {"gige": {"type": "object"}}})
    root = _registry()
    _service(root)
    m = {"kind": "profile", "name": "p", "version": "1.0.0",
         "requires": {"service": "camera-service@^1.4"},
         "config": {"payload": "config/payload.yaml", "overrides_schema": "config/schema.json"}}
    _pkg(root, "profiles", "camera-service/p", m,
         {"config/payload.yaml": "rtsp: {url: x}\n",  # missing `gige`
          "config/schema.json": schema})
    _assert_issue(root, "missing required key `gige`", check_index=False)


def test_overlay_rules():
    root = _registry()  # unresolved target service
    _overlay(root, targets=[{"service": "ghost"}])
    _assert_issue(root, "does not resolve", check_index=False)
    root2 = _registry()  # identity keys banned in deltas
    _service(root2)
    _overlay(root2, payload="name: sneaky\n")
    _assert_issue(root2, "must not set `name`", check_index=False)
    root3 = _registry()  # instance targets are ROS-safe names
    _overlay(root3, targets=[{"instance": "cam-front"}])
    _assert_issue(root3, "ROS-safe", check_index=False)
    root4 = _registry()  # instance-scoped target with a good name: fine (can't resolve locally)
    _overlay(root4, targets=[{"instance": "cam_front"}])
    assert not [m for m in _issues(root4, check_index=False) if "cam_front" in m]


def test_name_collision_across_kinds():
    root = _registry()  # flat kinds still share one key space (profiles are colon-keyed apart)
    _service(root, name="kraken")
    _overlay(root, name="kraken", targets=[{"service": "kraken"}])
    _assert_issue(root, "unique", check_index=False)


def test_profile_placement_must_match_requires():
    root = _registry()  # dir says gimbal/, requires says camera-service -> placement error
    _service(root)
    _profile(root, dir_svc="gimbal")
    _assert_issue(root, "placement", check_index=False)


def test_flat_profile_layout_rejected():
    root = _registry()
    _service(root)
    _pkg(root, "profiles", "old-style", {"kind": "profile", "name": "old-style", "version": "1.0.0",
                                         "requires": {"service": "camera-service@^1.4"},
                                         "config": {"payload": "config/payload.yaml"}},
         {"config/payload.yaml": "a: 1\n"})
    _assert_issue(root, "flat profile layout", check_index=False)


def test_schema_1_registry_refused_with_migration_pointer():
    root = _registry()
    meta = yaml.safe_load((root / "registry.yaml").read_text())
    meta["schema"] = 1
    (root / "registry.yaml").write_text(yaml.safe_dump(meta))
    try:
        load_registry(root, [])
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "migrate" in str(exc) and "profiles/<service>/<short>/" in str(exc)


def test_kind_and_name_must_match_directory():
    root = _registry()
    _pkg(root, "services", "svc-a", {"kind": "profile", "name": "svc-a", "version": "1.0.0"})
    _assert_issue(root, "`kind` must be 'service'", check_index=False)
    root2 = _registry()
    _service(root2)
    (root2 / "services" / "camera-service" / "manifest.yaml").write_text(
        yaml.safe_dump({"kind": "service", "name": "other-name", "version": "1.0.0",
                        "source": {"repo": "r", "rev": FULL_SHA}}))
    _assert_issue(root2, "must match the package dir", check_index=False)


def test_index_sensor_tiers_and_projects():
    root = _registry()
    _service(root)
    _profile(root, name="zr30-exact",
             provides={"sensor": [{"model": "ZR30", "match": ["zr30", "usb:2*", "*"]}]},
             requires="camera-service@^1.4")
    _overlay(root, project="gideon")
    reg = load_registry(root, [])
    index = generate_index(reg)
    assert index["sensors"]["zr30"] == [{"profile": "camera-service:zr30-exact", "tier": "exact"}]
    assert index["sensors"]["usb:2*"][0]["tier"] == "glob"
    assert index["sensors"]["*"][0]["tier"] == "fallback"
    assert index["projects"]["gideon"] == ["zr30-gideon"]
    assert render_index(index) == render_index(generate_index(reg))  # deterministic


def test_authored_against_drift_warns_without_failing():
    root = _registry()
    _service(root, version="2.0.0")
    _overlay(root, authored_against={"service": "testns/camera-service@1.4.2"})
    _reindex(root)
    reg, issues = validate_registry(root)
    warnings = [i for i in issues if i.level == "warning"]
    errors = [i for i in issues if i.level == "error"]
    assert not errors and len(warnings) == 1
    assert "now carries camera-service@2.0.0" in warnings[0].message
    from rig_cli.registry import cli_validate
    import contextlib, io
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert cli_validate(root) == 0                              # warnings never fail CI
    assert "[!]" in err.getvalue() and "1 warning(s)" in err.getvalue()


def test_newer_registry_schema_is_refused_with_upgrade_pointer():
    root = _registry()
    (root / "registry.yaml").write_text("schema: 99\nname: reg\nnamespace: testns\n")
    try:
        validate_registry(root)
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "upgrade rig" in str(exc)


def test_constraint_satisfaction():
    assert constraint_satisfied("1.4", True, "1.4.2")       # ^1.4 ok
    assert constraint_satisfied("1.4", True, "1.9.0")       # same major, higher minor
    assert not constraint_satisfied("1.4", True, "2.0.0")   # major bump breaks caret
    assert not constraint_satisfied("1.4", True, "1.3.9")   # below base
    assert constraint_satisfied("1.4.2", False, "1.4.2")    # exact
    assert not constraint_satisfied("1.4.2", False, "1.4.3")
    assert constraint_satisfied("1.4", False, "1.4.0")      # two-part exact means .0


def test_cli_end_to_end():
    from rig_cli.cli import main
    root = pathlib.Path(tempfile.mkdtemp()) / "cli-reg"
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert main(["registry", "init", str(root), "--namespace", "pub"]) == 0
        assert main(["registry", "validate", str(root)]) == 0
    _service(root)
    with contextlib.redirect_stderr(err):
        assert main(["registry", "validate", str(root)]) == 1   # stale index now
        assert main(["registry", "index", str(root)]) == 0
        assert main(["registry", "validate", str(root)]) == 0
    assert "STALE" in err.getvalue()


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
