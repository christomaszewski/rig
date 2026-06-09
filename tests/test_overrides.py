"""§1 — config overrides & profiles. Run: `.venv/bin/python tests/test_overrides.py` (no pytest needed)."""
import pathlib
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.common import load_yaml
from rig_cli.manifest import load_manifest
from rig_cli.resolve import deep_merge, materialize_manifest


def test_deep_merge_dict_and_scalar_and_list():
    base = {"a": 1, "b": {"x": 1, "y": 2}, "l": [1, 2]}
    out = deep_merge(base, {"b": {"y": 9, "z": 3}, "l": [9], "c": 5})
    assert out == {"a": 1, "b": {"x": 1, "y": 9, "z": 3}, "l": [9], "c": 5}, out
    assert base["b"] == {"x": 1, "y": 2}  # inputs untouched


def test_deep_merge_null_deletes_key():
    assert deep_merge({"a": 1, "b": 2}, {"b": None}) == {"a": 1}


def _root_with(files: dict) -> pathlib.Path:
    d = tempfile.mkdtemp()
    for rel, body in files.items():
        p = pathlib.Path(d, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    return pathlib.Path(d)


def test_profile_shared_across_instances_and_passthrough():
    root = _root_with({
        "vehicle.yaml": """
            vehicle: t
            ros: {domain_id: 0}
            sensors:
              - {name: cam_front, service: camera-service, config: p.yaml, overrides: {gige: {camera_id: AAA}}}
              - {name: cam_rear,  service: camera-service, config: p.yaml, overrides: {gige: {camera_id: BBB}}}
              - {name: gnss,      service: novatel,     config: named.yaml}
        """,
        "p.yaml": "service: camera-service\ncamera: {type: gige, frame_rate: 20.0}\ngige: {fake: false}\n",
        "named.yaml": "service: novatel\nname: gnss\nconnection: {type: tcp}\n",
    })
    by = {s.name: s for s in materialize_manifest(load_manifest(root), root).sensors}

    # two instances share ONE profile, differ only by id; each rendered with its own injected name
    a, b = load_yaml(by["cam_front"].config), load_yaml(by["cam_rear"].config)
    assert a["name"] == "cam_front" and a["gige"]["camera_id"] == "AAA" and a["camera"]["frame_rate"] == 20.0
    assert b["name"] == "cam_rear" and b["gige"]["camera_id"] == "BBB"
    assert by["cam_front"].config != by["cam_rear"].config  # distinct rendered files

    # a complete named config with no overrides is passed through untouched (no render)
    assert by["gnss"].config == (root / "named.yaml").resolve()


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
