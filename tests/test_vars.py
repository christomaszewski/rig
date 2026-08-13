"""Vehicle-local vars: {{var}} interpolation, source precedence, mandatory markers, env passthrough.
Run: python3 tests/test_vars.py"""
import contextlib
import os
import pathlib
import sys
import tempfile
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from rig_cli import RigError  # noqa: E402
from rig_cli.dispatch import fleet_env  # noqa: E402
from rig_cli.interpolate import referenced_vars, resolve_map, substitute, substitute_scalar  # noqa: E402
from rig_cli.manifest import load_manifest  # noqa: E402
from rig_cli.resolve import materialize_manifest  # noqa: E402


@contextlib.contextmanager
def _env(**over):
    old = {k: os.environ.get(k) for k in over}
    for k, v in over.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, str(v))
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _deployment(vehicle_yaml: str, files: dict = (), local: str | None = None) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "vehicle.yaml").write_text(textwrap.dedent(vehicle_yaml))
    for rel, body in dict(files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    if local is not None:
        (root / "vehicle.local.yaml").write_text(textwrap.dedent(local))
    return root


_NO_MACHINE = str(pathlib.Path(tempfile.mkdtemp()) / "absent.yaml")  # hermetic: no /etc/rig leak


def test_interpolate_primitives():
    v = {"vehicle_id": 7, "name": "skiff", "port": 8554}
    assert substitute_scalar("{{vehicle_id}}", v, where="t") == 7          # whole-marker keeps type
    assert substitute_scalar("10.160.{{vehicle_id}}.80", v, where="t") == "10.160.7.80"
    assert substitute({"a": ["x{{name}}", {"b": "{{port}}"}]}, v, where="t") == \
        {"a": ["xskiff", {"b": 8554}]}
    try:
        substitute_scalar("{{nope}}", v, where="t")
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "unknown var" in str(exc) and "RIG_VAR_nope" in str(exc)
    assert referenced_vars({"a": "{{x}}", "b": ["{{y}}-{{x}}"]}) == {"x", "y"}


def test_resolve_map_chains_and_cycles():
    out = resolve_map({"vehicle_id": 7, "camera_ip": "10.160.{{vehicle_id}}.25",
                       "rtsp": "rtsp://{{camera_ip}}:8554"}, where="t")
    assert out["rtsp"] == "rtsp://10.160.7.25:8554"
    try:
        resolve_map({"a": "{{b}}", "b": "{{a}}"}, where="t")
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "cycle" in str(exc)


def test_precedence_shell_local_machine_yaml():
    machine_dir = pathlib.Path(tempfile.mkdtemp())
    machine = machine_dir / "vehicle.local.yaml"
    machine.write_text("vehicle_id: 3\nvars: {camera_ip: 10.0.3.1}\n")
    root = _deployment("vehicle: fleet\nvehicle_id: 1\nvars: {camera_ip: 10.0.1.1, extra: base}\n")
    with _env(RIG_VEHICLE_LOCAL=str(machine), RIG_VEHICLE_ID=None, RIG_VAR_camera_ip=None):
        m = load_manifest(root)
        assert m.vehicle_id == 3 and m.vars["camera_ip"] == "10.0.3.1"   # machine beats yaml
        assert m.vars["extra"] == "base"                                  # yaml default survives
        assert m.ros.domain_id == 3                                       # domain from resolved id
    (root / "vehicle.local.yaml").write_text("vehicle_id: 5\n")
    with _env(RIG_VEHICLE_LOCAL=str(machine), RIG_VEHICLE_ID=None):
        assert load_manifest(root).vehicle_id == 5                        # deployment-local beats machine
    with _env(RIG_VEHICLE_LOCAL=str(machine), RIG_VEHICLE_ID="9", RIG_VAR_camera_ip="10.9.9.9"):
        m = load_manifest(root)
        assert str(m.vehicle_id) == "9" and m.vars["camera_ip"] == "10.9.9.9"  # shell beats all


def test_mandatory_marker_requires_local_and_hint():
    root = _deployment('vehicle: "{{vehicle}}"\nvehicle_id: "{{vehicle_id}}"\n')
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VEHICLE_NAME=None):
        try:
            load_manifest(root)
            raise AssertionError("expected RigError")
        except RigError as exc:
            assert "supplied per vehicle" in str(exc) and "rig provision" in str(exc)
    machine = pathlib.Path(tempfile.mkdtemp()) / "id.yaml"
    machine.write_text("vehicle: skiff-07\nvehicle_id: 7\n")
    with _env(RIG_VEHICLE_LOCAL=str(machine)):
        m = load_manifest(root)
        assert m.vehicle == "skiff-07" and m.vehicle_id == 7 and m.ros.domain_id == 7


def test_render_interpolates_configs_and_passthrough_rule():
    root = _deployment(
        """
        vehicle: t
        vehicle_id: 4
        vars: {rtsp_port: 8554}
        sensors:
          - {name: cam, service: camera-service, config: config/sensors/cam.yaml}
          - {name: plain, service: novatel, config: config/sensors/plain.yaml}
        """,
        files={
            "config/sensors/cam.yaml":
                "service: camera-service\nname: cam\n"
                "rtsp: {url: 'rtsp://10.160.{{vehicle_id}}.80:{{rtsp_port}}/main', latency_ms: 200}\n"
                "compose_ref: ${NOT_TOUCHED}\n",
            "config/sensors/plain.yaml": "service: novatel\nname: plain\nconnection: {type: tcp}\n",
        })
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE):
        m = materialize_manifest(load_manifest(root), root)
        cam = yaml.safe_load(pathlib.Path(next(s.config for s in m.sensors if s.name == "cam")).read_text())
        assert cam["rtsp"]["url"] == "rtsp://10.160.4.80:8554/main"       # markers resolved at render
        assert cam["compose_ref"] == "${NOT_TOUCHED}"                     # compose refs pass through
        plain = next(s.config for s in m.sensors if s.name == "plain")
        assert plain == (root / "config" / "sensors" / "plain.yaml").resolve()  # marker-free passthrough


def test_unknown_var_in_config_is_a_hard_render_error():
    root = _deployment(
        """
        vehicle: t
        vehicle_id: 4
        sensors:
          - {name: cam, service: cs, config: config/sensors/cam.yaml}
        """,
        files={"config/sensors/cam.yaml": "service: cs\nname: cam\nip: '{{ghost}}'\n"})
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE):
        try:
            materialize_manifest(load_manifest(root), root)
            raise AssertionError("expected RigError")
        except RigError as exc:
            assert "unknown var" in str(exc) and "ghost" in str(exc)


def test_env_map_interpolated_exported_and_guarded():
    root = _deployment("vehicle: t\nvehicle_id: 6\nenv: {SIYI_IP: '10.160.{{vehicle_id}}.25'}\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE):
        m = load_manifest(root)
        assert m.extra_env == {"SIYI_IP": "10.160.6.25"}
        env = fleet_env(m)
        assert env["SIYI_IP"] == "10.160.6.25" and env["VEHICLE_ID"] == "6"
    bad = _deployment("vehicle: t\nenv: {VEHICLE_ID: 9}\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE):
        try:
            load_manifest(bad)
            raise AssertionError("expected RigError")
        except RigError as exc:
            assert "rig-owned" in str(exc)
    lower = _deployment("vehicle: t\nenv: {lower_case: 1}\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE):
        try:
            load_manifest(lower)
            raise AssertionError("expected RigError")
        except RigError as exc:
            assert "UPPERCASE" in str(exc)


def test_unquoted_leading_marker_error_names_the_fix():
    root = _deployment('vehicle: t\nvehicle_id: {{vehicle_id}}\n')   # UNQUOTED — the classic trap
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE):
        try:
            load_manifest(root)
            raise AssertionError("expected RigError")
        except RigError as exc:
            assert "must be quoted" in str(exc) and '"{{vehicle_id}}"' in str(exc)


def test_local_file_key_whitelist():
    root = _deployment("vehicle: t\n", local="vehicle_id: 2\nsensors: []\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE):
        try:
            load_manifest(root)
            raise AssertionError("expected RigError")
        except RigError as exc:
            assert "unknown key" in str(exc) and "sensors" in str(exc)


def test_local_can_set_registry_and_data_dir():
    root = _deployment("vehicle: t\nvehicle_id: 1\nimages: {registry: fleet:5000, tag: jp7}\n",
                       local="images: {registry: bench:5000}\ndata_dir: /data/bench\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE):
        m = load_manifest(root)
        assert m.image_registry == "bench:5000" and m.image_tag == "jp7"  # per-key override
        assert m.data_dir == "/data/bench" and m.vars["data_dir"] == "/data/bench"


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
