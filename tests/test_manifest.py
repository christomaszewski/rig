"""Manifest validation. Run: `.venv/bin/python tests/test_manifest.py` (no pytest needed)."""
import pathlib
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError
from rig_cli.manifest import load_manifest


def _root_with(files: dict) -> pathlib.Path:
    d = tempfile.mkdtemp()
    for rel, body in files.items():
        p = pathlib.Path(d, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    return pathlib.Path(d)


def _expect_error(root: pathlib.Path, needle: str):
    try:
        load_manifest(root)
    except RigError as exc:
        assert needle in str(exc), f"got: {exc}"
    else:
        raise AssertionError(f"expected RigError containing {needle!r}")


def test_duplicate_name_rejected():
    _expect_error(_root_with({
        "vehicle.yaml": """
            vehicle: t
            sensors:
              - {name: dup, service: novatel, config: a.yaml}
              - {name: dup, service: novatel, config: b.yaml}
        """,
        "a.yaml": "service: novatel\nname: dup\nconnection: {type: file}\n",
        "b.yaml": "service: novatel\nname: dup\nconnection: {type: file}\n",
    }), "duplicate name")


def test_config_name_mismatch_rejected():
    _expect_error(_root_with({
        "vehicle.yaml": """
            vehicle: t
            sensors:
              - {name: gnss, service: novatel, config: c.yaml}
        """,
        "c.yaml": "service: novatel\nname: OTHER\nconnection: {type: file}\n",
    }), "name")


def test_nameless_profile_accepted():
    root = _root_with({
        "vehicle.yaml": """
            vehicle: t
            sensors:
              - {name: cam, service: camera-service, config: p.yaml}
        """,
        "p.yaml": "service: camera-service\ncamera: {type: gige}\ngige: {fake: true}\n",
    })
    assert [s.name for s in load_manifest(root).sensors] == ["cam"]


def test_order_sorts_and_shared_profile():
    # both instances share one nameless profile; names come from the manifest
    root = _root_with({
        "vehicle.yaml": """
            vehicle: t
            sensors:
              - {name: c, service: novatel, config: x.yaml, order: 30}
              - {name: a, service: novatel, config: x.yaml, order: 10}
        """,
        "x.yaml": "service: novatel\nconnection: {type: file}\n",
    })
    sel = load_manifest(root).select([], enabled_only=True)
    assert [s.name for s in sel] == ["a", "c"]


def test_vehicle_id_derives_domain():
    root = _root_with({
        "vehicle.yaml": "vehicle: v\nvehicle_id: 7\nros: {rmw: rmw_zenoh_cpp}\n"
                        "sensors: [{name: a, service: novatel, config: x.yaml}]\n",
        "x.yaml": "service: novatel\nconnection: {type: file}\n",
    })
    m = load_manifest(root)
    assert m.vehicle_id == 7 and m.ros.domain_id == 7
    explicit = _root_with({
        "vehicle.yaml": "vehicle: v\nvehicle_id: 7\nros: {domain_id: 0}\n"
                        "sensors: [{name: a, service: novatel, config: x.yaml}]\n",
        "x.yaml": "service: novatel\nconnection: {type: file}\n",
    })
    assert load_manifest(explicit).ros.domain_id == 0  # explicit ros.domain_id wins


def test_infra_comes_before_sensors_regardless_of_order():
    root = _root_with({
        "vehicle.yaml": """
            vehicle: v
            infra:
              - {name: router, service: zenoh-router, config: r.yaml, order: 99}
            sensors:
              - {name: gnss, service: novatel, config: x.yaml, order: 1}
        """,
        "r.yaml": "service: zenoh-router\n",
        "x.yaml": "service: novatel\nconnection: {type: file}\n",
    })
    m = load_manifest(root)
    assert [s.name for s in m.select([], enabled_only=True)] == ["router", "gnss"]  # tier beats order
    assert {s.name: s.tier for s in m.sensors} == {"router": "infra", "gnss": "sensor"}


def test_name_unique_across_infra_and_sensors():
    _expect_error(_root_with({
        "vehicle.yaml": "vehicle: v\ninfra: [{name: x, service: zenoh-router, config: r.yaml}]\n"
                        "sensors: [{name: x, service: novatel, config: s.yaml}]\n",
        "r.yaml": "service: zenoh-router\n",
        "s.yaml": "service: novatel\n",
    }), "duplicate name")


def test_image_registry_parsed():
    root = _root_with({
        "vehicle.yaml": """
            vehicle: t
            images: { registry: devbox:5000, tag: jp7 }
            sensors:
              - {name: a, service: novatel, config: x.yaml}
        """,
        "x.yaml": "service: novatel\nconnection: {type: file}\n",
    })
    m = load_manifest(root)
    assert m.image_registry == "devbox:5000" and m.image_tag == "jp7"
    empty = _root_with({
        "vehicle.yaml": "vehicle: t\nimages: {registry: ''}\nsensors: [{name: a, service: novatel, config: x.yaml}]\n",
        "x.yaml": "service: novatel\nconnection: {type: file}\n",
    })
    e = load_manifest(empty)
    assert e.image_registry is None and e.image_tag is None  # empty -> None (local images)


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
