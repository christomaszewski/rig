"""Shell completion — M3: the `completion` emitter, bash --line protocol, wrapper scripts.
Run: python3 tests/test_completions_scripts.py"""
import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.cli import main  # noqa: E402
from rig_cli.completions import candidates, script  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
WORDBREAKS = " \t\n\"'><=;|&(:"


def _c(*words):
    words = list(words)
    return candidates(words, len(words) - 1)


def _run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def _env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    for k, v in kv.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _line(line, home=None):
    with _env(COMP_LINE=line, COMP_POINT=str(len(line)), COMP_WORDBREAKS=WORDBREAKS,
              **({"RIG_HOME": home} if home else {})):
        rc, out, err = _run("_complete", "--line")
    assert rc == 0 and err == ""
    return out.splitlines()


TMP = pathlib.Path(tempfile.mkdtemp())
HOME = TMP / "home"
(HOME / "cache" / "registries" / "public").mkdir(parents=True)
(HOME / "registries.yaml").write_text(
    "registries:\n  - { name: public, type: git, url: https://example.invalid/r.git }\n")
(HOME / "cache" / "registries" / "public" / "index.json").write_text(json.dumps({
    "packages": {"zenoh-router": {"kind": "service", "version": "1.0.0"}},
    "sensors": {"2b:0033": []}}))


# --- the `completion` verb ---------------------------------------------------

def test_completion_verb_emits_bash():
    rc, out, err = _run("completion", "bash")
    assert rc == 0
    assert "complete -o default -F _rig_completions rig" in out
    assert '"${COMP_WORDS[0]}" _complete --line' in out  # completes the binary as typed


def test_completion_verb_emits_zsh():
    rc, out, _ = _run("completion", "zsh")
    assert rc == 0
    assert out.startswith("#compdef rig")
    assert "compdef _rig rig" in out and "_describe" in out and "_files" in out


def test_completion_verb_is_in_its_own_menu():
    assert _c("comp") == ["completion"]
    assert _c("completion", "") == ["bash", "zsh"]


def test_scripts_are_version_stamped():
    from rig_cli import __version__
    assert __version__ in script("bash") and __version__ in script("zsh")


# --- the bash --line protocol ------------------------------------------------

def test_line_mode_command_menu():
    assert set(_line("rig do")) == {"doctor", "down"}
    assert "up" in _line("rig ")  # trailing space = fresh word


def test_line_mode_trims_at_colon():
    # bash splits `sensor:2b:0` at the colons; the emitted candidate must be just the
    # region readline will replace (after the LAST colon).
    assert _line("rig pkg add sensor:2b:0", home=str(HOME)) == ["0033"]
    assert "sensor:2b:0033" in _line("rig pkg add sen", home=str(HOME))  # no break char typed


def test_line_mode_trims_at_equals():
    assert _line("rig status --format=j") == ["json"]


def test_line_mode_point_mid_line():
    line = "rig do --force"
    with _env(COMP_LINE=line, COMP_POINT="6", COMP_WORDBREAKS=WORDBREAKS):
        rc, out, _ = _run("_complete", "--line")
    assert rc == 0 and set(out.splitlines()) == {"doctor", "down"}


def test_line_mode_fail_soft():
    for line in ("", "rig 'unterminated", "rig \\"):
        with _env(COMP_LINE=line, COMP_POINT=str(len(line)), COMP_WORDBREAKS=WORDBREAKS):
            rc, _, err = _run("_complete", "--line")
        assert rc == 0 and err == "", f"not fail-soft for {line!r}"


# --- the wrapper scripts, against real shells (capability-detected) ----------

def test_bash_script_parses():
    if not shutil.which("bash"):
        print("    (skipped: no bash)")
        return
    proc = subprocess.run(["bash", "-n"], input=script("bash"), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_zsh_script_parses():
    if not shutil.which("zsh"):
        print("    (skipped: no zsh)")
        return
    proc = subprocess.run(["zsh", "-n"], input=script("zsh"), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_bash_function_end_to_end():
    """Source the generated script in a real bash, drive _rig_completions the way readline
    would, and read COMPREPLY — the shim path as COMP_WORDS[0] proves 'the binary as typed'."""
    if not shutil.which("bash"):
        print("    (skipped: no bash)")
        return
    shim = REPO / "rig"
    script_file = TMP / "rig.bash"
    script_file.write_text(script("bash"))  # macOS bash 3.2 mis-sources <(…) — use a file
    snippet = (
        f'source "{script_file}"; '
        f'COMP_WORDS=("{shim}" "do"); COMP_CWORD=1; '
        f'COMP_LINE="{shim} do"; COMP_POINT=${{#COMP_LINE}}; '
        f'_rig_completions; printf "%s\\n" "${{COMPREPLY[@]}}"')
    proc = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert set(proc.stdout.split()) == {"doctor", "down"}, proc.stdout


def test_shim_completes_end_to_end():
    proc = subprocess.run([str(REPO / "rig"), "_complete", "0", "--"],
                          capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stderr == ""
    lines = proc.stdout.splitlines()
    assert "up" in lines and "new-run" not in lines


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
