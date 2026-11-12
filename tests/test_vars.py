"""Vehicle-local vars: {{var}} interpolation, source precedence, mandatory markers, env passthrough.
Run: python3 tests/test_vars.py"""
from __future__ import annotations

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
# Hermetic user state: rig remembers registries it touches in $RIG_HOME/catalog.yaml (rig catalog)
# — never the developer's real ~/.rig.
os.environ.setdefault("RIG_HOME", str(pathlib.Path(tempfile.mkdtemp()) / "home"))


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
for _stray in [k for k in os.environ
               if k in ("RIG_VEHICLE_ID", "RIG_VEHICLE_NAME") or k.startswith("RIG_VAR_")]:
    os.environ.pop(_stray)  # hermetic: a host's stray identity/RIG_VAR_* must not feed the tiers


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


def test_mandatory_marker_lazy_load_and_strict_gate():
    root = _deployment('vehicle: "{{vehicle}}"\nvehicle_id: "{{vehicle_id}}"\nsensors: []\n')
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VEHICLE_NAME=None):
        m = load_manifest(root)                                        # LAZY: loading succeeds…
        assert m.missing_identity == ("vehicle", "vehicle_id")
        from rig_cli.cli import main
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = main(["--root", str(root), "up", "--dry-run"])        # …but CONSUMING identity gates
        assert rc == 1
        assert "unresolved per-vehicle" in err.getvalue() and "rig provision" in err.getvalue()
    machine = pathlib.Path(tempfile.mkdtemp()) / "id.yaml"
    machine.write_text("vehicle: skiff-07\nvehicle_id: 7\n")
    with _env(RIG_VEHICLE_LOCAL=str(machine)):
        m = load_manifest(root)
        assert m.vehicle == "skiff-07" and m.vehicle_id == 7 and m.ros.domain_id == 7
        assert m.missing_identity == ()


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


def test_non_identity_markers_are_also_lazy():
    # data_dir: "{{log_dir}}" with nothing providing log_dir: loading (and management verbs)
    # still work; only identity-consuming commands gate, naming the unresolved value.
    root = _deployment('vehicle: t\nvehicle_id: 1\ndata_dir: "{{log_dir}}"\nsensors: []\n')
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE):
        m = load_manifest(root)
        assert m.missing_identity == ("data_dir",) and m.data_dir is None
        from rig_cli.cli import main
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = main(["--root", str(root), "up", "--dry-run"])
        assert rc == 1 and "data_dir" in err.getvalue()


def test_fleet_build_needs_no_identity():
    root = _deployment('vehicle: "{{vehicle}}"\nvehicle_id: "{{vehicle_id}}"\nsensors: []\n',
                       files={"services.yaml": "services: {}\n"})
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VEHICLE_NAME=None):
        from rig_cli.cli import main
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(["--root", str(root), "image", "build", "--dry-run"])
        assert rc == 0, err.getvalue()                                  # no identity gate
        assert "unresolved per-vehicle" not in err.getvalue()


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


# --- {{map}} form + fleet_peer_ids derived built-in (v0.1.65) -----------------------------------


def test_map_form_primitives():
    v = {"fleet_ids": [1, 2, 7], "tmpl": "tcp/10.0.0.{}:7447", "csv": "3, 9", "bad_tmpl": "x"}
    assert substitute_scalar("{{map fleet_ids tmpl}}", v, where="t") == \
        ["tcp/10.0.0.1:7447", "tcp/10.0.0.2:7447", "tcp/10.0.0.7:7447"]   # ints stringified
    assert substitute_scalar("{{map csv tmpl}}", v, where="t") == \
        ["tcp/10.0.0.3:7447", "tcp/10.0.0.9:7447"]                        # comma-string coerced
    for text, msg in (("{{map fleet_ids nope}}", "unknown var"),
                      ("{{map nope tmpl}}", "unknown var"),
                      ("{{map fleet_ids bad_tmpl}}", "placeholder"),
                      ("prefix {{map fleet_ids tmpl}}", "whole-scalar only")):
        try:
            substitute_scalar(text, v, where="t")
            raise AssertionError(f"expected RigError for {text!r}")
        except RigError as exc:
            assert msg in str(exc), f"{text!r}: {exc}"
    assert referenced_vars({"a": "{{map fleet_peer_ids tmpl}}"}) == {"fleet_peer_ids", "tmpl"}


def test_map_form_rejected_in_vars_mapping():
    try:
        resolve_map({"peers": "{{map fleet_ids tmpl}}"}, where="vars")
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "CONFIG files" in str(exc)


def test_fleet_peer_ids_derivation_and_typing():
    # int-vs-str across sources: YAML id 7 must exclude "7" and 7 alike.
    root = _deployment("vehicle: t\nvehicle_id: 7\nvars: {fleet_ids: ['7', 8, 9]}\nsensors: []\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None):
        m = load_manifest(root)
        assert m.vars["fleet_peer_ids"] == [8, 9]           # self excluded; native types kept
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID="8"):
        assert load_manifest(root).vars["fleet_peer_ids"] == ["7", 9]  # shell id (str) excludes int 8
    # shell-supplied comma string works end to end
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VAR_fleet_ids="7,8"):
        assert load_manifest(root).vars["fleet_peer_ids"] == ["8"]
    # no fleet_ids -> derived var absent (unknown-var error on reference, house style)
    bare = _deployment("vehicle: t\nvehicle_id: 7\nsensors: []\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None):
        assert "fleet_peer_ids" not in load_manifest(bare).vars
    # unprovisioned (no vehicle_id) -> absent, never a self-including list
    unprov = _deployment('vehicle: t\nvehicle_id: "{{vehicle_id}}"\n'
                         "vars: {fleet_ids: [1, 2]}\nsensors: []\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VEHICLE_NAME=None):
        assert "fleet_peer_ids" not in load_manifest(unprov).vars
    # explicit operator override wins (setdefault)
    over = _deployment("vehicle: t\nvehicle_id: 7\n"
                       "vars: {fleet_ids: [7, 8], fleet_peer_ids: [99]}\nsensors: []\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None):
        assert load_manifest(over).vars["fleet_peer_ids"] == [99]


def test_map_form_rejected_in_env_and_manifest_fields():
    root = _deployment("vehicle: t\nvehicle_id: 7\n"
                       "vars: {fleet_ids: [7, 8], tmpl: 'tcp/{}'}\n"
                       'env: {PEERS: "{{map fleet_peer_ids tmpl}}"}\nsensors: []\n')
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None):
        try:
            load_manifest(root)
            raise AssertionError("expected RigError")
        except RigError as exc:
            assert "CONFIG files" in str(exc)


def test_map_renders_peer_endpoints_excluding_self():
    root = _deployment(
        """
        vehicle: t
        vehicle_id: 3
        vars:
          fleet_ids: [3, 9]
          peer_endpoint: tcp/10.0.0.{}:7447
        sensors:
          - {name: router, service: zenoh-router, config: config/sensors/router.yaml}
        """,
        files={"config/sensors/router.yaml":
               'service: zenoh-router\nname: router\n'
               'connect: "{{map fleet_peer_ids peer_endpoint}}"\n'})
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VAR_peer_endpoint=None):
        m = materialize_manifest(load_manifest(root), root)
        cfg = yaml.safe_load(pathlib.Path(m.sensors[0].config).read_text())
        assert cfg["connect"] == ["tcp/10.0.0.9:7447"]      # a real LIST, self excluded
    # THE SIL swap: same deployment, ports instead of IPs, via the shell tier
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None,
              RIG_VAR_peer_endpoint="tcp/127.0.0.1:744{}"):
        m = materialize_manifest(load_manifest(root), root)
        cfg = yaml.safe_load(pathlib.Path(m.sensors[0].config).read_text())
        assert cfg["connect"] == ["tcp/127.0.0.1:7449"]


# --- fleet.yaml as the fleet-vars tier (v0.1.68) ------------------------------------------------


def test_fleet_vars_source_derives_from_roster():
    from rig_cli.fleet import vars_source
    p = pathlib.Path(tempfile.mkdtemp()) / "fleet.yaml"
    p.write_text(textwrap.dedent("""\
        fleet: t
        mode: sil
        gcs_ip: 10.0.0.10
        vehicles:
          - {id: 3, name: a, path: /x}
          - {name: broken-row-without-id, path: /y}
          - {id: 9, name: b, path: /z}
        vars: {peer_endpoint: "tcp/10.0.0.{}:7447", gcs_ip: 10.9.9.9}
        """))
    src = vars_source(p)["vars"]
    assert src["fleet_ids"] == [3, 9]                  # roster order; id-less rows skipped
    assert src["fleet_mode"] == "sil"
    assert src["gcs_ip"] == "10.9.9.9"                 # explicit fleet vars: beats derived
    assert src["peer_endpoint"] == "tcp/10.0.0.{}:7447"
    p.write_text("fleet: t\nbogus: 1\n")
    try:
        vars_source(p)
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "unknown key" in str(exc)


def test_fleet_tier_precedence():
    # fleet.yaml beats vehicle.yaml defaults; local file and shell beat fleet.yaml.
    root = _deployment(
        "vehicle: t\nvehicle_id: 3\nvars: {gcs_ip: 10.0.0.1, only_here: base}\nsensors: []\n")
    (root / "fleet.yaml").write_text(
        "fleet: t\ngcs_ip: 10.0.0.2\nvehicles: [{id: 3, name: a, path: /x}]\n"
        "vars: {camera_ip: 10.5.5.5}\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VAR_gcs_ip=None):
        m = load_manifest(root)
        assert m.vars["gcs_ip"] == "10.0.0.2"          # fleet tier beats vehicle.yaml
        assert m.vars["only_here"] == "base"           # vehicle.yaml defaults survive
        assert m.vars["fleet_ids"] == [3] and m.vars["fleet_peer_ids"] == []
    (root / "vehicle.local.yaml").write_text("vars: {gcs_ip: 10.0.0.3}\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VAR_gcs_ip=None):
        assert load_manifest(root).vars["gcs_ip"] == "10.0.0.3"   # local beats fleet
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VAR_gcs_ip="10.0.0.4"):
        assert load_manifest(root).vars["gcs_ip"] == "10.0.0.4"   # shell beats all


def test_fleet_roster_to_peer_endpoints_with_zero_duplication():
    # The whole chain from ONE file: roster -> fleet_ids -> fleet_peer_ids -> endpoints.
    root = _deployment(
        """
        vehicle: t
        vehicle_id: 3
        sensors:
          - {name: router, service: zenoh-router, config: config/sensors/router.yaml}
        """,
        files={"config/sensors/router.yaml":
               'service: zenoh-router\nname: router\n'
               'connect: "{{map fleet_peer_ids peer_endpoint}}"\n'})
    (root / "fleet.yaml").write_text(
        "fleet: t\nvehicles: [{id: 3, name: a, path: /x}, {id: 9, name: b, path: /y}]\n"
        "vars: {peer_endpoint: 'tcp/10.0.0.{}:7447'}\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None):
        m = materialize_manifest(load_manifest(root), root)
        cfg = yaml.safe_load(pathlib.Path(m.sensors[0].config).read_text())
        assert cfg["connect"] == ["tcp/10.0.0.9:7447"]  # no fleet vars in vehicle.yaml at ALL


# --- which tier supplied a value, and per-vehicle values in the roster --------------------------

_SWARM = """\
fleet: swarm
vars: {altitude_band: 10}
vehicles:
  - {id: 3, name: uav-03, path: /x, vars: {altitude_band: 30}}
  - {id: 7, name: uav-07, path: /y, vars: {altitude_band: 40}}
  - {id: 9, name: spare-1, path: /z}
"""


def _machine(body: str) -> pathlib.Path:
    path = pathlib.Path(tempfile.mkdtemp()) / "vehicle.local.yaml"
    path.write_text(body)
    return path


_SWARM_BUILT = """\
fleet: swarm
unique: [altitude_band]
vars: {altitude_band: "{{spacing}}{{vehicle_id}}", spacing: 1}
vehicles:
  - {id: 3, name: uav-03, path: /x}
  - {id: 7, name: uav-07, path: /y}
"""


def _preflight(root: pathlib.Path) -> tuple[int, str]:
    import io
    from rig_cli.cli import main
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        rc = main(["--root", str(root), "up", "--dry-run"])
    return rc, err.getvalue()


def test_a_vehicle_file_may_not_change_what_a_unique_value_is_built_from():
    # The ground station checks the values the roster hands out. A vehicle's own file that sets
    # a `unique:` name, or a var it is built from, takes the vehicle off that check: refused
    # at preflight, whichever roster level supplies the value.
    root = _deployment("vehicle: t\nvehicle_id: 3\nsensors: []\n",
                       files={"services.yaml": "services: {}\n"})
    (root / "fleet.yaml").write_text(_SWARM_BUILT)
    clean = dict(RIG_VEHICLE_ID=None, RIG_VAR_altitude_band=None, RIG_VAR_spacing=None)
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, **clean):
        rc, err = _preflight(root)
        assert rc == 0 and "preflight" not in err, err
    machine = _machine("vars: {altitude_band: 17}\n")               # the name itself
    with _env(RIG_VEHICLE_LOCAL=str(machine), **clean):
        rc, err = _preflight(root)
        assert rc == 1 and "preflight failed" in err, err
        assert "altitude_band" in err and "17" in err and str(machine) in err, err
        machine.write_text("vars: {spacing: 2}\n")                  # what it is built from
        rc, err = _preflight(root)
        assert rc == 1 and "spacing" in err and "altitude_band" in err, err
        machine.write_text("vars: {spacing: 1}\n")                  # the roster's own value
        rc, err = _preflight(root)
        assert rc == 0, err
        machine.write_text("vars: {mast: 4}\n")                     # nothing unique depends on it
        rc, err = _preflight(root)
        assert rc == 0, err
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, **{**clean, "RIG_VAR_spacing": "2"}):
        rc, err = _preflight(root)                                  # the shell: said, not blocked
        assert rc == 0, err
        assert any("spacing" in ln and "shell" in ln for ln in err.splitlines()), err


def test_a_unique_value_built_from_the_vehicle_name_needs_the_rosters_name():
    # A vehicle finds its row by id, so the id always agrees. Its name is its own.
    root = _deployment("vehicle: t\nvehicle_id: 3\nsensors: []\n",
                       files={"services.yaml": "services: {}\n"})
    (root / "fleet.yaml").write_text(_SWARM_BUILT.replace(
        '"{{spacing}}{{vehicle_id}}"', '"band-{{vehicle}}"'))
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VEHICLE_NAME=None):
        rc, err = _preflight(root)
        assert rc == 1 and "preflight failed" in err, err
        assert "uav-03" in err and "'t'" in err and "altitude_band" in err, err
        (root / "vehicle.yaml").write_text("vehicle: uav-03\nvehicle_id: 3\nsensors: []\n")
        rc, err = _preflight(root)
        assert rc == 0, err


def test_a_templated_vehicle_name_is_checked_after_it_resolves():
    root = _deployment("vehicle: t\nvehicle_id: 3\nsensors: []\n",
                       files={"services.yaml": "services: {}\n"})
    (root / "fleet.yaml").write_text(_SWARM_BUILT.replace(
        '"{{spacing}}{{vehicle_id}}"', '"band-{{vehicle}}"'))
    machine = _machine('vehicle: "{{call_sign}}"\nvars: {call_sign: wrong-name}\n')
    with _env(RIG_VEHICLE_LOCAL=str(machine), RIG_VEHICLE_ID=None, RIG_VEHICLE_NAME=None):
        rc, err = _preflight(root)
        assert rc == 1 and "preflight failed" in err, err
        assert "uav-03" in err and "wrong-name" in err and "altitude_band" in err, err
        machine.write_text('vehicle: "{{call_sign}}"\nvars: {call_sign: uav-03}\n')
        rc, err = _preflight(root)
        assert rc == 0, err


def test_tolerant_load_keeps_unresolved_vehicle_names_behind_the_identity_gate():
    from rig_cli.manifest import require_identity
    root = _deployment('vehicle: "{{call_sign}}"\nvehicle_id: 7\nsensors: []\n')
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VEHICLE_NAME=None):
        manifest = load_manifest(root, tolerate_vars=True)
        try:
            require_identity(manifest, what="running verb", tolerate_env=True)
        except RigError as exc:
            assert "vehicle" in str(exc) and "unresolved" in str(exc), exc
        else:
            raise AssertionError("a running verb accepted an unresolved vehicle name")


def test_var_sources_names_the_tier_that_supplied_each_value():
    machine = pathlib.Path(tempfile.mkdtemp()) / "vehicle.local.yaml"
    root = _deployment("vehicle: t\nvehicle_id: 1\nvars: {band: 1, only_here: base}\nsensors: []\n")
    with _env(RIG_VEHICLE_LOCAL=str(machine), RIG_VEHICLE_ID=None, RIG_VAR_band=None):
        m = load_manifest(root)
        assert (m.vars["band"], m.var_sources["band"]) == (1, "vehicle.yaml")
        assert m.var_sources["vehicle_id"] == "vehicle.yaml"
        assert m.var_sources["ros_domain_id"] == "derived"
        (root / "fleet.yaml").write_text(
            "fleet: t\nvehicles: [{id: 1, name: a, path: /x}, {id: 2, name: b, path: /y}]\n"
            "vars: {band: 2}\n")
        m = load_manifest(root)
        assert (m.vars["band"], m.var_sources["band"]) == (2, "fleet.yaml")
        assert m.var_sources["fleet_ids"] == "fleet.yaml"          # read off the roster
        assert m.var_sources["fleet_peer_ids"] == "derived"
        assert m.var_sources["only_here"] == "vehicle.yaml"        # a lower tier's value survives
        machine.write_text("vehicle_id: 3\nvars: {band: 3}\n")
        m = load_manifest(root)
        assert (m.vars["band"], m.var_sources["band"]) == (3, str(machine))
        assert m.var_sources["vehicle_id"] == str(machine)
        (root / "vehicle.local.yaml").write_text("vars: {band: 4}\n")
        m = load_manifest(root)
        assert (m.vars["band"], m.var_sources["band"]) == (4, "vehicle.local.yaml")
        assert m.var_sources["vehicle_id"] == str(machine)         # per key, not per file
    with _env(RIG_VEHICLE_LOCAL=str(machine), RIG_VEHICLE_ID="9", RIG_VAR_band="5"):
        m = load_manifest(root)
        assert (m.vars["band"], m.var_sources["band"]) == ("5", "shell")
        assert m.var_sources["vehicle_id"] == "shell"
        assert set(m.var_sources) == set(m.vars)                   # nothing resolved goes unnamed


def test_row_value_beats_fleet_level_and_the_machine_file_beats_the_row():
    root = _deployment('vehicle: t\nvehicle_id: "{{vehicle_id}}"\nsensors: []\n')
    (root / "fleet.yaml").write_text(_SWARM)
    for vid, band, source in ((3, 30, "fleet.yaml row uav-03"), (7, 40, "fleet.yaml row uav-07"),
                              (9, 10, "fleet.yaml")):   # a row without one: the fleet-level value
        with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=vid, RIG_VAR_altitude_band=None):
            m = load_manifest(root)                     # the shell id is "3", the roster's is 3
            assert (m.vars["altitude_band"], m.var_sources["altitude_band"]) == (band, source), vid
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VAR_altitude_band=None):
        m = load_manifest(root)                         # no id yet: no row is this vehicle's
        assert m.missing_identity == ("vehicle_id",)
        assert (m.vars["altitude_band"], m.var_sources["altitude_band"]) == (10, "fleet.yaml")
    machine = _machine("vehicle_id: 3\nvars: {altitude_band: 50}\n")
    with _env(RIG_VEHICLE_LOCAL=str(machine), RIG_VEHICLE_ID=None, RIG_VAR_altitude_band=None):
        m = load_manifest(root)
        assert (m.vars["altitude_band"], m.var_sources["altitude_band"]) == (50, str(machine))
        from rig_cli import doctor
        errors = [i.message for i in doctor.collect(m, {}, {}) if i.level == doctor.ERROR]
        assert any("altitude_band" in e for e in errors), errors   # the roster's 30 is not lost quietly


def test_vehicle_refuses_a_roster_that_holds_its_id_twice():
    root = _deployment("vehicle: t\nvehicle_id: 3\nsensors: []\n")
    (root / "fleet.yaml").write_text(
        "fleet: swarm\nvehicles:\n"
        "  - {id: 3, name: uav-03, path: /x, vars: {altitude_band: 30}}\n"
        "  - {id: '3', name: spare-1, path: /y, vars: {altitude_band: 40}}\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None):
        try:
            load_manifest(root)
            raise AssertionError("expected RigError")
        except RigError as exc:
            assert "uav-03" in str(exc) and "spare-1" in str(exc), exc


def test_row_without_a_referenced_value_fails_at_render():
    root = _deployment(
        """
        vehicle: t
        vehicle_id: "{{vehicle_id}}"
        sensors:
          - {name: nav, service: navsvc, config: config/sensors/nav.yaml}
        """,
        files={"config/sensors/nav.yaml": 'service: navsvc\nname: nav\nband: "{{altitude_band}}"\n'})
    (root / "fleet.yaml").write_text(_SWARM.replace("vars: {altitude_band: 10}\n", ""))
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID="3", RIG_VAR_altitude_band=None):
        m = materialize_manifest(load_manifest(root), root)
        assert yaml.safe_load(pathlib.Path(m.sensors[0].config).read_text())["band"] == 30
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID="9", RIG_VAR_altitude_band=None):
        try:                                            # the spare: no band of its own, and it
            materialize_manifest(load_manifest(root), root)   # must not borrow a neighbour's
            raise AssertionError("expected RigError")
        except RigError as exc:
            assert "unknown var" in str(exc) and "altitude_band" in str(exc)


def test_down_does_not_need_an_exported_value_that_is_gone_but_up_does():
    """`env:` may export a roster value. When the roster stops supplying it, `up` must refuse
    and `down` must not: stopping what is running needs no such value."""
    import io
    from rig_cli.cli import main
    root = _deployment('vehicle: t\nvehicle_id: 3\nenv: {ALT_BAND: "{{altitude_band}}"}\nsensors: []\n',
                       files={"services.yaml": "services: {}\n"})
    (root / "fleet.yaml").write_text(_SWARM.replace("vars: {altitude_band: 10}\n", "").replace(
        ", vars: {altitude_band: 30}", ""))

    def rig(*argv) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = main(["--root", str(root), *argv])
        return rc, err.getvalue()

    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VAR_altitude_band=None):
        rc, err = rig("up", "--dry-run")
        assert rc == 1 and "env.ALT_BAND" in err, err
        rc, err = rig("down", "--dry-run")
        assert rc == 0 and "env.ALT_BAND" in err, err               # said, not fatal


def test_stale_higher_tier_value_blocks_up_and_a_shell_override_is_only_reported():
    import io
    from rig_cli.cli import main
    root = _deployment("vehicle: t\nvehicle_id: 3\nsensors: []\n",
                       files={"services.yaml": "services: {}\n"})
    (root / "fleet.yaml").write_text(_SWARM)

    def rig(*argv) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = main(["--root", str(root), *argv])
        return rc, err.getvalue()

    def up(*extra) -> tuple[int, str]:
        return rig("up", "--dry-run", *extra)

    machine = _machine("vars: {altitude_band: 50}\n")   # an old `rig provision --var`
    with _env(RIG_VEHICLE_LOCAL=str(machine), RIG_VEHICLE_ID=None, RIG_VAR_altitude_band=None):
        rc, err = up()
        assert rc == 1 and "preflight failed" in err, err
        assert "altitude_band" in err and "50" in err and "30" in err       # both values…
        assert str(machine) in err and "fleet.yaml row uav-03" in err       # …and both tiers
        rc, err = up("--force")
        assert rc == 0, err
        machine.write_text("vars: {altitude_band: 30}\n")                   # the same value twice
        rc, err = up()
        assert rc == 0 and "preflight" not in err, err
    (root / "vehicle.local.yaml").write_text("vars: {altitude_band: 50}\n")
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VAR_altitude_band=None):
        rc, err = up()
        assert rc == 1 and "vehicle.local.yaml" in err and "altitude_band" in err, err
    (root / "vehicle.local.yaml").unlink()
    with _env(RIG_VEHICLE_LOCAL=_NO_MACHINE, RIG_VEHICLE_ID=None, RIG_VAR_altitude_band="50"):
        rc, err = up()                                  # one invocation, on purpose: say so, go on
        assert rc == 0, err
        said = next((ln for ln in err.splitlines() if "altitude_band" in ln), "")
        assert "shell" in said and "50" in said and "30" in said, err
        rc, err = rig("doctor")                         # doctor says it too, as a warning
        assert rc == 0, err
        said = next((ln for ln in err.splitlines() if "[!]" in ln and "altitude_band" in ln), "")
        assert "shell" in said and "50" in said and "30" in said, err


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
