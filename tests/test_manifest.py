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
    }), "duplicate sensor name")


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
              - {name: cam, service: gige-vision, config: p.yaml}
        """,
        "p.yaml": "service: gige-vision\ncamera: {fake: true}\n",
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


def test_image_registry_parsed():
    root = _root_with({
        "vehicle.yaml": """
            vehicle: t
            images: { registry: devbox:5000 }
            sensors:
              - {name: a, service: novatel, config: x.yaml}
        """,
        "x.yaml": "service: novatel\nconnection: {type: file}\n",
    })
    assert load_manifest(root).image_registry == "devbox:5000"
    empty = _root_with({
        "vehicle.yaml": "vehicle: t\nimages: {registry: ''}\nsensors: [{name: a, service: novatel, config: x.yaml}]\n",
        "x.yaml": "service: novatel\nconnection: {type: file}\n",
    })
    assert load_manifest(empty).image_registry is None  # empty -> None (local images)


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
