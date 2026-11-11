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
# Hermetic user state: rig remembers registries it touches in $RIG_HOME/catalog.yaml (rig catalog)
# — never the developer's real ~/.rig.
os.environ.setdefault("RIG_HOME", str(pathlib.Path(tempfile.mkdtemp()) / "home"))

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
@EXTRAVERBS@
  *) exit 0 ;;
esac
"""

DEFAULTS = {
    "@PRELUDE@": "",
    "@EXTRAVERBS@": "",
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


# The matrix variant: two platform builds (alpha/beta) + the override env the launcher honors.
# The conformant form surfaces $FAK_PLATFORM in the rendered output (an overlay-selection stand-in)
# and pulls :$RIG_IMAGE_TAG — which certify composes to certify-tag-x-<platform> per matrix entry.
RIGGING_MATRIX = RIGGING.replace(
    "build: { command: build.sh, images: [fak-core] }",
    "build: { command: build.sh, images: [fak-core], platforms: [alpha, beta] }",
) + "platform: { auto_detect: /etc/fak_release, override_env: FAK_PLATFORM }\n"

MATRIX_ENV = ("    environment:\n"
              "      ROS_DOMAIN_ID: \"$ROS_DOMAIN_ID\"\n"
              "      RMW_IMPLEMENTATION: $RMW_IMPLEMENTATION\n"
              "      FAK_PLATFORM: $FAK_PLATFORM")


def make_service(rigging: str = RIGGING, **tokens) -> pathlib.Path:
    repo = pathlib.Path(tempfile.mkdtemp(prefix="certify-fixture-"))
    (repo / "rigging.yaml").write_text(rigging)
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
    # This fixture models only a launcher. The absent build context is covered in test_build_probe.
    checks = [c for c in checks if c.name != 'build-no-cache']
    by_level = lambda lvl: {c.name for c in checks if c.level == lvl}  # noqa: E731
    return checks, by_level("ERROR"), by_level("WARN"), by_level("OK")


def test_conformant_fixture_is_green():
    checks, errors, warns, oks = run_certify(make_service())
    assert not errors, f"conformant fixture failed: {[(c.name, c.detail) for c in checks if c.level == 'ERROR']}"
    assert not warns, f"conformant fixture warned: {warns}"
    assert {"discipline", "project-name", "registry", "tag", "build-images", "ros-env",
            "volumes", "determinism", "identity", "fixed-names", "status"} <= oks


def test_declared_image_override_must_reach_compose():
    rigging = RIGGING + 'images: {override_env: {fak-core: FAK_IMAGE}}\n'
    _, errors, _, _ = run_certify(make_service(rigging))
    assert errors == {'image-overrides'}
    checks, errors, _, oks = run_certify(make_service(rigging,
        **{'@IMAGE@': '${FAK_IMAGE:-$RIG_IMAGE_REGISTRY/fak-core:$RIG_IMAGE_TAG}'}))
    assert not errors, [(c.name, c.detail) for c in checks if c.level == 'ERROR']
    assert 'image-overrides' in oks


def test_image_override_checked_for_each_runtime():
    rigging = RIGGING_MATRIX + 'images: {override_env: {fak-core: FAK_IMAGE}}\n'
    checks, errors, _, _ = run_certify(make_service(rigging, **{
        '@PRELUDE@': '    if [ "$FAK_PLATFORM" = "beta" ]; then unset FAK_IMAGE; fi',
        '@ENVBLOCK@': MATRIX_ENV,
        '@IMAGE@': '${FAK_IMAGE:-$RIG_IMAGE_REGISTRY/fak-core:$RIG_IMAGE_TAG}'}))
    assert errors == {'image-overrides'}, [(c.name, c.detail) for c in checks if c.level == 'ERROR']


def test_status_failure_without_docker_daemon_is_a_skip_not_an_error():
    # A failing `status` verb is a contract ERROR only when a docker daemon is reachable —
    # `docker compose ps` needs one, while every other check renders client-side. On a daemon-less
    # host (build container, thin CI runner) the same failure is an INFO skip: certify can't
    # attribute it to the launcher. Pin the cached probe both ways to prove both behaviors anywhere.
    from rig_cli import certify

    repo = make_service()
    launcher = repo / "fak-up"
    launcher.write_text(launcher.read_text().replace('ps) echo "[]" ;;', "ps) exit 1 ;;"))
    old = certify._DAEMON_OK
    try:
        certify._DAEMON_OK = False
        checks, errors, _, _ = run_certify(repo)
        assert "status" not in errors
        assert any(c.name == "status" and c.level == "INFO" and "skipped" in c.detail for c in checks)
        certify._DAEMON_OK = True
        _, errors, _, _ = run_certify(repo)
        assert "status" in errors
    finally:
        certify._DAEMON_OK = old


# --- the operational-state trio (v0.2.35) --------------------------------------------------------

RIGGING_STATES = RIGGING + "verbs: { standby: standby, activate: activate, state: state }\n"
_STATE_CASE = """  state) printf '{"state": "down", "detail": "no containers"}\\n' ;;"""


def test_state_trio_conformant_is_green():
    checks, errors, warns, oks = run_certify(
        make_service(RIGGING_STATES, **{"@EXTRAVERBS@": _STATE_CASE}))
    assert not errors, [(c.name, c.detail) for c in checks if c.level == "ERROR"]
    assert "state-verbs" in oks


def test_partial_state_trio_fails():
    # All three or none — the declaration is the claim rig dispatches on; a partial trio is a
    # broken claim even when the declared half works.
    partial = RIGGING + "verbs: { standby: standby }\n"
    _, errors, _, _ = run_certify(make_service(partial, **{"@EXTRAVERBS@": _STATE_CASE}))
    assert errors == {"state-verbs"}


def test_state_chatter_on_stdout_fails():
    chatter = """  state) echo "checking..."; printf '{"state": "down"}\\n' ;;"""
    _, errors, _, _ = run_certify(make_service(RIGGING_STATES, **{"@EXTRAVERBS@": chatter}))
    assert "state-verbs" in errors  # two lines on stdout = not one JSON object


def test_state_foreign_vocabulary_fails():
    foreign = """  state) printf '{"state": "lifecycle:inactive"}\\n' ;;"""
    _, errors, _, _ = run_certify(make_service(RIGGING_STATES, **{"@EXTRAVERBS@": foreign}))
    assert "state-verbs" in errors  # raw internals belong in `detail`, not `state`


def test_state_failure_without_docker_daemon_is_a_skip_not_an_error():
    # `state` ultimately asks compose which containers exist — same daemon caveat as `status`.
    from rig_cli import certify

    repo = make_service(RIGGING_STATES, **{"@EXTRAVERBS@": "  state) exit 1 ;;"})
    old = certify._DAEMON_OK
    try:
        certify._DAEMON_OK = False
        checks, errors, _, _ = run_certify(repo)
        assert "state-verbs" not in errors
        assert any(c.name == "state-verbs" and c.level == "INFO" and "skipped" in c.detail
                   for c in checks)
        certify._DAEMON_OK = True
        _, errors, _, _ = run_certify(repo)
        assert "state-verbs" in errors
    finally:
        certify._DAEMON_OK = old


def test_poisoned_target_state_trips_launchers_validating_beyond_up():
    # The posture token is honored/validated at `up` ONLY: a launcher that hard-fails every verb
    # on a bogus RIG_TARGET_STATE would break certify's own rendering — the poison makes that a
    # visible discipline failure instead of a silent field surprise.
    prelude = ('    if [ -n "$RIG_TARGET_STATE" ]; then '
               'echo "bad RIG_TARGET_STATE" >&2; exit 1; fi')
    _, errors, _, _ = run_certify(make_service(**{"@PRELUDE@": prelude}))
    assert "discipline" in errors


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


# --- fixed-names: host-global names that survive a rename ----------------------------------------
# `identity` greps the renamed render for the OLD name, so a constant — which holds neither name —
# passes it. These mutants are all identity-green and must trip fixed-names instead.

def _with_service_key(line: str) -> dict:
    return {"@ENVBLOCK@": DEFAULTS["@ENVBLOCK@"] + "\n" + line}


def test_fixed_container_name_fails():
    checks, errors, _, oks = run_certify(make_service(**_with_service_key("    container_name: fak_core")))
    assert errors == {"fixed-names"}, [(c.name, c.detail) for c in checks if c.level == "ERROR"]
    assert "identity" in oks  # the blind spot: the rename scan alone calls this conformant
    assert any("fak_core" in c.detail for c in checks if c.name == "fixed-names")


def test_instance_derived_container_name_passes():
    # Not the fleet convention (compose already names containers after the project), but it
    # cannot collide — certify fails proof of a collision, not a style choice.
    for line in ("    container_name: fak_${NAME}_core",
                 "    container_name: ${COMPOSE_PROJECT_NAME}-core"):
        checks, errors, warns, oks = run_certify(make_service(**_with_service_key(line)))
        assert not errors and not warns, [(c.name, c.detail) for c in checks if c.level != "OK"]
        assert "fixed-names" in oks


def test_fixed_network_and_volume_names_warn():
    # These still START — the instances silently share the resource — so a WARN, not an ERROR.
    block = DEFAULTS["@VOLBLOCK@"] + ("\n  cache:\n    name: fak_cache\n"
                                      "networks:\n  bus:\n    name: fak_bus")
    checks, errors, warns, _ = run_certify(make_service(**{"@VOLBLOCK@": block}))
    assert not errors and warns == {"fixed-names"}
    detail = " ".join(c.detail for c in checks if c.name == "fixed-names")
    assert "volume 'fak_cache'" in detail and "network 'fak_bus'" in detail


def test_external_and_instance_scoped_names_are_not_fixed_names():
    # `external: true` says sharing is meant (a fleet network every stack joins); a name carrying
    # the instance name or the project is per-instance. Neither is a finding.
    block = DEFAULTS["@VOLBLOCK@"] + ("\n  cache:\n    name: fak_${NAME}_cache\n"
                                      "networks:\n"
                                      "  fleet:\n    name: fleet_bus\n    external: true\n"
                                      "  default:\n    name: ${COMPOSE_PROJECT_NAME}_default")
    checks, errors, warns, oks = run_certify(make_service(**{"@VOLBLOCK@": block}))
    assert not errors and not warns, [(c.name, c.detail) for c in checks if c.level != "OK"]
    assert "fixed-names" in oks


def test_hardcoded_project_reports_project_name_not_its_derived_names():
    # A launcher passing `-p` makes every compose-derived `<project>_<key>` name constant too.
    # That is ONE defect — project-name's — not a second finding per network.
    block = DEFAULTS["@VOLBLOCK@"] + "\nnetworks:\n  default:\n    name: hardcoded-project_default"
    _, errors, warns, _ = run_certify(make_service(**{"@PROJECT@": "hardcoded-project",
                                                      "@VOLBLOCK@": block}))
    assert errors == {"project-name"} and not warns


# --- build-images: the declared images are the ones the compose pulls ----------------------------
# `registry` and `tag` judge a ref only when its repo IS a declared build image. Pulled under any
# other name, nothing judged it — and `tag` used to report OK anyway.

def test_declared_build_image_pulled_under_another_name_warns():
    checks, errors, warns, oks = run_certify(make_service(
        **{"@IMAGE@": "$RIG_IMAGE_REGISTRY/fak-runtime:$RIG_IMAGE_TAG"}))
    assert not errors and warns == {"build-images"}
    assert "tag" not in oks  # no built image was seen: an OK here would be vacuous
    detail = " ".join(c.detail for c in checks if c.name == "build-images")
    assert "fak-core" in detail and "fak-runtime" in detail  # declared AND what is pulled instead


def test_built_image_from_a_hardcoded_registry_fails_registry():
    # Same image name, wrong place: before, this was only the "upstream image" WARN.
    for image in ("ghcr.io/acme/fak-core:$RIG_IMAGE_TAG",            # another registry
                  "$RIG_IMAGE_REGISTRY/acme/fak-core:$RIG_IMAGE_TAG"):  # a namespace rig build never pushes
        checks, errors, warns, oks = run_certify(make_service(**{"@IMAGE@": image}))
        assert errors == {"registry"}, (image, [(c.name, c.detail) for c in checks if c.level != "OK"])
        assert not warns  # one finding: not also "upstream image", not also build-images
        assert "tag" not in oks and "build-images" not in oks


def test_same_basename_upstream_image_beside_the_built_one_only_warns():
    # The relocation rule fires only while the declared image is otherwise unpulled. With the
    # real one present, `vendor/fak-core` is just an upstream image that shares a basename.
    _, errors, warns, oks = run_certify(make_service(
        **{"@EXTRASVC@": "  helper:\n    image: docker.io/vendor/fak-core:1.0"}))
    assert not errors and warns == {"registry"}
    assert {"tag", "build-images"} <= oks


def test_partly_pulled_build_images_is_info_not_a_warning():
    # An image behind a config option this config leaves off is unseen, not wrong. Say so (the
    # checks did not cover it) without a warning every such service would learn to ignore.
    rigging = RIGGING.replace("images: [fak-core]", "images: [fak-core, fak-plugin]")
    checks, errors, warns, oks = run_certify(make_service(rigging))
    assert not errors and not warns
    assert "tag" in oks and "build-images" not in oks
    assert any(c.name == "build-images" and c.level == "INFO" and "fak-plugin" in c.detail
               for c in checks)


def test_build_image_pulled_only_on_another_platform_counts_as_seen():
    # A matrix service may select an image on one platform only (camera-service's cam-dev). The
    # suite renders AS alpha; beta's render must still count toward what was examined.
    rigging = RIGGING_MATRIX.replace("images: [fak-core]", "images: [fak-core, fak-beta]")
    prelude = ('    EXTRA=""\n'
               '    if [ "$FAK_PLATFORM" = "beta" ]; then\n'
               '      EXTRA="$(printf \'  extra:\\n    image: %s/fak-beta:%s\' '
               '"$RIG_IMAGE_REGISTRY" "$RIG_IMAGE_TAG")"\n'
               '    fi')
    checks, errors, warns, oks = run_certify(make_service(rigging, **{
        "@ENVBLOCK@": MATRIX_ENV, "@PRELUDE@": prelude, "@EXTRASVC@": "$EXTRA"}))
    assert not errors and not warns, [(c.name, c.detail) for c in checks if c.level != "OK"]
    assert {"build-images", "platform"} <= oks


# --- ros-env without `ros_distro` ------------------------------------------------------------------
# The wrong-value rule used to run only for a DECLARED ROS service — so the service most likely to
# hard-code its domain id (one nobody has declared yet) was the one never checked.

RIGGING_NO_ROS = RIGGING.replace("ros_distro: lyrical\n", "")


def test_hardcoded_ros_env_fails_without_ros_distro():
    for block in ("    environment:\n      ROS_DOMAIN_ID: \"7\"",
                  "    environment:\n      RMW_IMPLEMENTATION: rmw_fastrtps_cpp\n"
                  "      ROS_DOMAIN_ID: \"$ROS_DOMAIN_ID\""):
        checks, errors, _, _ = run_certify(make_service(RIGGING_NO_ROS, **{"@ENVBLOCK@": block}))
        assert errors == {"ros-env"}, [(c.name, c.detail) for c in checks if c.level == "ERROR"]


def test_non_ros_service_without_ros_env_reports_nothing():
    # No declaration and no ROS env is simply a non-ROS service (mavlink-router): the absence
    # WARN belongs to declared ROS services only.
    checks, errors, warns, _ = run_certify(make_service(RIGGING_NO_ROS,
                                                        **{"@ENVBLOCK@": "    environment: {}"}))
    assert not errors and not warns
    assert not [c for c in checks if c.name == "ros-env"]


def test_undeclared_service_passing_fleet_ros_env_through_is_ok():
    # e.g. a zenoh client that needs only the domain id: what it carries arrived unmodified.
    for block in (DEFAULTS["@ENVBLOCK@"], "    environment:\n      ROS_DOMAIN_ID: \"$ROS_DOMAIN_ID\""):
        _, errors, warns, oks = run_certify(make_service(RIGGING_NO_ROS, **{"@ENVBLOCK@": block}))
        assert not errors and not warns and "ros-env" in oks


def test_stdout_chatter_fails_discipline():
    _, errors, _, _ = run_certify(make_service(**{"@PRELUDE@": '    echo "fak-up: rendering $NAME"'}))
    assert errors == {"discipline"}


def test_discipline_nonzero_exit_short_circuits():
    # discipline failures early-return: exit nonzero AFTER rendering a plausible compose, so a
    # dropped early return would score `ok discipline` off the stdout and run every later check
    repo = make_service()
    launcher = repo / "fak-up"
    launcher.write_text(launcher.read_text().replace("EOF\n    ;;", "EOF\n    exit 2\n    ;;"))
    checks, errors, _, _ = run_certify(repo)
    assert errors == {"discipline"}
    assert {c.name for c in checks} == {"discipline"}  # nothing after the early return
    assert any("exited 2" in c.detail for c in checks)


def test_discipline_non_yaml_stdout_short_circuits():
    checks, errors, _, _ = run_certify(make_service(**{"@PRELUDE@": "    printf '{{{'\n    exit 0"}))
    assert errors == {"discipline"}
    assert {c.name for c in checks} == {"discipline"}
    assert any("not YAML" in c.detail for c in checks)


def test_discipline_yaml_without_services_short_circuits():
    checks, errors, _, _ = run_certify(make_service(**{"@PRELUDE@": "    echo '[]'\n    exit 0"}))
    assert errors == {"discipline"}
    assert {c.name for c in checks} == {"discipline"}
    assert any("no `services:`" in c.detail for c in checks)


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
    assert {c.name for c in checks if c.level == "WARN"} == {"binds", "build-no-cache"}
    assert not {c.name for c in checks if c.level == "ERROR"}


def test_matrix_conformant_launcher_is_green_including_platform():
    # The suite runs AS platforms[0] (alpha): tag agreement expects the COMPOSED certify-tag-x-alpha,
    # and the platform check renders beta too — different tag + FAK_PLATFORM in the output = honored.
    checks, errors, warns, oks = run_certify(make_service(RIGGING_MATRIX, **{"@ENVBLOCK@": MATRIX_ENV}))
    assert not errors, f"matrix fixture failed: {[(c.name, c.detail) for c in checks if c.level == 'ERROR']}"
    assert {"tag", "platform", "determinism", "identity"} <= oks


def test_matrix_hardcoded_platform_tag_fails_platform_check():
    # Simulates a host-probing launcher: it always picks alpha's tag. Passes the main suite (which
    # runs as alpha) but the beta render still pulls :certify-tag-x-alpha -> platform ERROR.
    repo = make_service(RIGGING_MATRIX, **{
        "@ENVBLOCK@": MATRIX_ENV,
        "@IMAGE@": "$RIG_IMAGE_REGISTRY/fak-core:certify-tag-x-alpha",
    })
    _, errors, _, _ = run_certify(repo)
    assert errors == {"platform"}


def test_matrix_platform_blind_launcher_fails_identical_renders():
    # Ignores the platform entirely (hardcoded tag, no FAK_PLATFORM in the output): alpha and beta
    # renders are byte-identical — proof the declared platform never reached the rendering.
    repo = make_service(RIGGING_MATRIX, **{"@IMAGE@": "$RIG_IMAGE_REGISTRY/fak-core:certify-tag-x-alpha"})
    checks, errors, _, _ = run_certify(repo)
    assert "platform" in errors
    details = " ".join(c.detail for c in checks if c.name == "platform")
    assert "byte-identical" in details


def test_matrix_platform_that_cannot_render_fails():
    # A launcher that bails on any platform but the (simulated) host's breaks declared-wins: bake
    # renders on dev boxes that aren't the target, so every matrix entry must render anywhere.
    prelude = '    if [ "$FAK_PLATFORM" != "alpha" ]; then echo "unsupported platform" >&2; exit 3; fi'
    checks, errors, _, _ = run_certify(make_service(RIGGING_MATRIX, **{
        "@ENVBLOCK@": MATRIX_ENV, "@PRELUDE@": prelude}))
    assert errors == {"platform"}
    details = " ".join(c.detail for c in checks if c.name == "platform")
    assert "fails to render" in details


def test_repo_mode_defaults_to_the_declared_example():
    from rig_cli import cli

    repo = make_service()
    (repo / "instance.yaml").write_text("service: fak\ncamera: {}\n")
    rigging = repo / "rigging.yaml"
    rigging.write_text(rigging.read_text() + "examples: [instance.yaml]\n")
    assert cli.main(["certify", "--repo", str(repo)]) == 0          # no --config needed
    rigging.write_text(RIGGING)                                      # examples removed again
    assert cli.main(["certify", "--repo", str(repo)]) == 1          # ...now it must be passed


def test_emit_normalizes_host_paths_and_diff():
    # surface the ABSOLUTE repo path in the render (an env value is how a real launcher leaks
    # it; ros-env only inspects its two poison keys, so the extra key trips nothing else) —
    # otherwise the "not in text" assertion is vacuously true and proves no tokenization
    envblock = DEFAULTS["@ENVBLOCK@"] + "\n      RIG_PARAMS: $REPO/params.yaml"
    repo = make_service(**{"@ENVBLOCK@": envblock})
    out_a = repo / "emit-a.yaml"
    _, errors, _, _ = run_certify(repo, emit=out_a)
    assert not errors
    text = out_a.read_text()
    assert "${REPO}/params.yaml" in text, "emit must tokenize the repo path for cross-host diffing"
    assert str(repo) not in text
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
