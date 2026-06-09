"""certify — the launcher-contract conformance suite. Run: python3 tests/test_certify.py

A pure-sh fixture launcher (no docker needed) plays a conformant service; each mutant violates exactly one
contract rule and must trip exactly that check. This is the executable form of the contract: if a rule in
certify.py changes, a mutant here should break.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.certify import NAME_A, certify_target, diff_emits  # noqa: E402
from rig_cli.descriptor import load_descriptor  # noqa: E402

RIGGING = """\
service: fak
launcher: fak-up
ros_distro: lyrical
build: { command: build.sh, images: [fak-core] }
external_volumes: ["fak_{name}_sock"]
"""

# @TOKENS@ are filled per-variant; the sh itself interpolates the poison env + the config's `name`.
SCRIPT = """\
#!/bin/sh
CONFIG="$1"; shift
VERB="$1"
NAME=$(sed -n 's/^name:[[:space:]]*//p' "$CONFIG" | head -1)
REPO="$(cd "$(dirname "$0")" && pwd)"
case "$VERB" in
  config)
    echo "fak-up: rendering $NAME" >&2
@PRELUDE@
    cat <<EOF
name: @PROJECT@
services:
  core:
    image: @IMAGE@
@ENVBLOCK@
    volumes:
      - type: bind
        source: $RIG_DATA_DIR/recordings/$NAME
        target: /data/recordings
@EXTRASVC@
@VOLBLOCK@
EOF
    ;;
  ps) echo "[]" ;;
  *) exit 0 ;;
esac
"""

DEFAULTS = {
    "@PRELUDE@": "",
    "@PROJECT@": "$COMPOSE_PROJECT_NAME",
    "@IMAGE@": "$RIG_IMAGE_REGISTRY/fak-core:$RIG_IMAGE_TAG",
    "@ENVBLOCK@": ("    environment:\n"
                   "      ROS_DOMAIN_ID: \"$ROS_DOMAIN_ID\"\n"
                   "      RMW_IMPLEMENTATION: $RMW_IMPLEMENTATION"),
    "@EXTRASVC@": "",
    "@VOLBLOCK@": ("volumes:\n"
                   "  fak_${NAME}_sock:\n"
                   "    external: true\n"
                   "    name: fak_${NAME}_sock"),
}


def make_service(**tokens) -> pathlib.Path:
    repo = pathlib.Path(tempfile.mkdtemp(prefix="certify-fixture-"))
    (repo / "rigging.yaml").write_text(RIGGING)
    body = SCRIPT
    for key, value in {**DEFAULTS, **tokens}.items():
        body = body.replace(key, value)
    launcher = repo / "fak-up"
    launcher.write_text(body)
    launcher.chmod(0o755)
    return repo


def run_certify(repo: pathlib.Path, emit=None):
    cfg = repo / "instance.yaml"
    cfg.write_text("service: fak\ncamera: {}\n")
    checks = certify_target(load_descriptor("fak", repo), cfg, dict(os.environ), emit=emit)
    by_level = lambda lvl: {c.name for c in checks if c.level == lvl}  # noqa: E731
    return checks, by_level("ERROR"), by_level("WARN"), by_level("OK")


def test_conformant_fixture_is_green():
    checks, errors, warns, oks = run_certify(make_service())
    assert not errors, f"conformant fixture failed: {[(c.name, c.detail) for c in checks if c.level == 'ERROR']}"
    assert not warns, f"conformant fixture warned: {warns}"
    assert {"discipline", "project-name", "registry", "tag", "ros-env",
            "volumes", "determinism", "identity", "status"} <= oks


def test_hardcoded_project_name_fails():
    _, errors, _, _ = run_certify(make_service(**{"@PROJECT@": "hardcoded-project"}))
    assert errors == {"project-name"}


def test_unprefixed_built_image_fails_registry():
    _, errors, _, _ = run_certify(make_service(**{"@IMAGE@": "fak-core:$RIG_IMAGE_TAG"}))
    assert errors == {"registry"}


def test_upstream_image_only_warns():
    _, errors, warns, _ = run_certify(make_service(
        **{"@EXTRASVC@": "  helper:\n    image: docker.io/library/busybox:stable"}))
    assert not errors and warns == {"registry"}


def test_wrong_tag_fails_build_pull_agreement():
    _, errors, _, _ = run_certify(make_service(**{"@IMAGE@": "$RIG_IMAGE_REGISTRY/fak-core:arm64"}))
    assert errors == {"tag"}


def test_nondeterministic_output_fails():
    _, errors, _, _ = run_certify(make_service(**{"@EXTRASVC@": "  x-run-stamp:\n    image: \"img:$$\""}))
    # $$ = the launcher's PID — differs per run, exactly like a timestamp or host probe would.
    assert "determinism" in errors


def test_hardcoded_identity_fails_rename():
    literal = DEFAULTS["@VOLBLOCK@"].replace("${NAME}", NAME_A)
    _, errors, _, _ = run_certify(make_service(**{"@VOLBLOCK@": literal}))
    assert errors == {"identity"}


def test_stdout_chatter_fails_discipline():
    _, errors, _, _ = run_certify(make_service(**{"@PRELUDE@": '    echo "fak-up: rendering $NAME"'}))
    assert errors == {"discipline"}


def test_missing_ros_env_warns_for_ros_service():
    # Absence is only a WARN: rig can't know whether this config runs a ROS node (plugin-less camera,
    # ros1 without RMW). A WRONG value, by contrast, is proof of hardcoding -> ERROR (next test).
    _, errors, warns, _ = run_certify(make_service(**{"@ENVBLOCK@": "    environment: {}"}))
    assert not errors and warns == {"ros-env"}


def test_hardcoded_ros_env_value_fails():
    block = "    environment:\n      ROS_DOMAIN_ID: \"7\"\n      RMW_IMPLEMENTATION: $RMW_IMPLEMENTATION"
    _, errors, _, _ = run_certify(make_service(**{"@ENVBLOCK@": block}))
    assert errors == {"ros-env"}


def test_stale_volume_pattern_warns():
    _, errors, warns, _ = run_certify(make_service(**{"@VOLBLOCK@": ""}))
    assert not errors and warns == {"volumes"}


def test_repo_dir_bind_warns():
    repo = make_service()
    body = (repo / "fak-up").read_text().replace("$RIG_DATA_DIR/recordings/$NAME", "$REPO/output")
    (repo / "fak-up").write_text(body)
    cfg = repo / "instance.yaml"
    cfg.write_text("service: fak\n")
    checks = certify_target(load_descriptor("fak", repo), cfg, dict(os.environ))
    assert {c.name for c in checks if c.level == "WARN"} == {"binds"}
    assert not {c.name for c in checks if c.level == "ERROR"}


def test_emit_normalizes_host_paths_and_diff():
    repo = make_service()
    out_a = repo / "emit-a.yaml"
    _, errors, _, _ = run_certify(repo, emit=out_a)
    assert not errors
    text = out_a.read_text()
    assert str(repo) not in text, "emit must tokenize the repo path for cross-host diffing"
    assert diff_emits(out_a, out_a) == 0
    out_b = repo / "emit-b.yaml"
    out_b.write_text(text.replace("fak-core", "other-core"))
    assert diff_emits(out_a, out_b) == 1


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
