"""Shell completion engine — M1: grammar (verbs, groups, flags, enums) + fail-soft envelope.
Run: python3 tests/test_completions.py"""
import contextlib
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.cli import main  # noqa: E402
from rig_cli.completions import candidates  # noqa: E402


def _c(*words):
    """The LAST word passed is the one under the cursor (possibly "")."""
    words = list(words)
    return candidates(words, len(words) - 1)


def _run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(list(argv))
        except SystemExit as exc:  # argparse exits (--version/--help) must not kill the runner
            rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


# --- command menu (OQ-A: canonical only) ------------------------------------

def test_command_menu_is_canonical():
    menu = _c("")
    for verb in ("up", "down", "status", "logs", "pull", "doctor", "certify", "cleanup",
                 "init", "add", "fetch", "provision", "setup",
                 "config", "run", "artifact", "image", "service",
                 "registry", "pkg", "overlay", "fleet"):
        assert verb in menu, f"canonical '{verb}' missing from the menu"


def test_command_menu_hides_legacy_flat_spellings():
    menu = _c("")
    for legacy in ("new-run", "end-run", "runs", "config-render", "config-diff",
                   "bake", "unbake", "artifact-list", "build", "image-audit",
                   "rigify", "vendor"):
        assert legacy not in menu, f"legacy '{legacy}' should not be suggested"


def test_legacy_spellings_still_parse():
    # The menu policy must never affect parsing — permanent aliases stay permanent.
    from rig_cli.cli import build_parser
    assert build_parser().parse_args(["new-run"]).cmd == "new-run"
    assert build_parser().parse_args(["pkg", "rm", "x"]).pkg_cmd == "rm"


def test_command_prefix_filter():
    assert _c("do") == ["doctor", "down"]
    assert _c("stat") == ["status"]


def test_global_flags():
    assert set(_c("-")) == {"--root", "--version", "--help"}
    assert _c("--root", "") == []  # directory value — file fallback owns it


# --- noun groups ------------------------------------------------------------

def test_group_menus():
    assert _c("image", "") == ["audit", "build", "pull"]
    assert _c("run", "") == ["end", "list", "new"]
    assert _c("artifact", "") == ["bake", "list", "unbake"]
    assert _c("service", "") == ["certify", "rigify", "vendor"]


def test_config_offers_group_verbs_and_stays_legacy():
    menu = _c("config", "")
    for verb in ("show", "render", "diff"):
        assert verb in menu


def test_grouped_flags_equal_flat_flags():
    grouped = _c("image", "build", "--")
    flat = _c("build", "--")
    assert grouped == flat
    for flag in ("--registry", "--tag", "--platform", "--jobs", "--no-cache",
                 "--base-image", "--dry-run"):
        assert flag in grouped


def test_group_unknown_verb_completes_nothing():
    assert _c("image", "explode", "--") == []


# --- per-verb flags and enums -----------------------------------------------

def test_verb_flags():
    assert set(_c("up", "--")) >= {"--dry-run", "--force", "--run"}
    assert set(_c("logs", "--")) >= {"--follow", "--tail"}


def test_used_flag_not_resuggested():
    menu = _c("up", "--force", "--")
    assert "--force" not in menu and "--dry-run" in menu


def test_repeatable_flag_is_resuggested():
    # --infra is action=append — repeatable, so it stays in the menu after one use.
    menu = _c("init", "veh", "--infra", "zenoh-router", "--")
    assert "--infra" in menu


def test_enum_flag_values():
    assert _c("status", "--format", "") == ["json", "table"]
    assert _c("pkg", "search", "--kind", "") == ["overlay", "profile", "service", "suite", "vehicle"]
    assert _c("rigify", "dir", "--tier", "") == ["autonomy", "infra", "sensor"]


def test_enum_equals_form():
    assert _c("status", "--format=j") == ["--format=json"]


def test_value_flag_without_source_completes_nothing():
    assert _c("logs", "--tail", "") == []


# --- nested subparser groups ------------------------------------------------

def test_pkg_menu_hides_aliases():
    menu = _c("pkg", "")
    for verb in ("search", "info", "list", "add", "remove", "upgrade", "lock",
                 "save", "promote", "outdated", "repin", "rebase", "yank"):
        assert verb in menu, f"pkg verb '{verb}' missing"
    assert "install" not in menu and "rm" not in menu


def test_registry_and_fleet_and_overlay_menus():
    assert set(_c("registry", "")) == {"init", "add", "remove", "list", "sync",
                                   "validate", "index", "pending", "push", "discard"}
    assert set(_c("fleet", "")) == {"list", "status", "sync", "up", "down"}
    assert set(_c("overlay", "")) == {"apply", "remove", "reorder", "list"}


def test_nested_flags():
    assert set(_c("pkg", "promote", "--")) >= {"--to", "--all", "--kind", "--suite", "--adopt"}
    assert set(_c("fleet", "status", "--")) >= {"--fleet", "--jobs", "--verbose"}


# --- the hidden verb + fail-soft envelope -----------------------------------

def test_complete_verb_end_to_end():
    rc, out, err = _run("_complete", "0", "--")
    assert rc == 0 and err == ""
    assert "up" in out.splitlines() and "new-run" not in out.splitlines()


def test_describe_mode_carries_help():
    rc, out, _ = _run("_complete", "--describe", "0", "--")
    lines = dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)
    assert "up" in lines and "bring sensors up" in lines["up"]


def test_complete_verb_swallows_garbage():
    for argv in (["_complete"], ["_complete", "x", "--"], ["_complete", "5"],
                 ["_complete", "-3", "--", "up"], ["_complete", "99", "--", "pkg", "add"]):
        rc, out, err = _run(*argv)
        assert rc == 0 and err == "", f"not fail-soft for {argv}"


def test_nonsense_prefix_completes_nothing():
    assert _c("frobnicate", "xyz") == []
    assert _c("pkg", "frobnicate", "z") == []


def test_new_verb_completes_with_zero_completion_changes():
    # The DoD guarantee: extend the parser, and the menu follows without touching completions.
    import rig_cli.cli as cli
    real = cli.build_parser

    def extended():
        p = real()
        sub = next(a for a in p._actions
                   if a.__class__.__name__ == "_SubParsersAction")
        sub.add_parser("frobnicate", help="test-injected verb")
        return p

    cli.build_parser = extended
    try:
        assert "frobnicate" in _c("frob")
    finally:
        cli.build_parser = real


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
