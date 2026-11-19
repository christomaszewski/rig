"""Atomic writes of the files rig owns and reads back (common.atomic_write_text): a run's
manifest.yaml, rig.lock, the machine identity file, a pushed roster. A reader, a power cut or an
interrupt must see the old file or the new one, never a truncated one.
Run: python3 tests/test_atomic_write.py"""
import os
import pathlib
import stat
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli.common import atomic_write_bytes, atomic_write_text  # noqa: E402

# Hermetic user state — never the developer's real ~/.rig.
os.environ.setdefault("RIG_HOME", str(pathlib.Path(tempfile.mkdtemp()) / "home"))


def _dir() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp())


def test_replaces_content_keeps_an_existing_mode_and_leaves_no_temporary_file():
    d = _dir()
    path = d / "manifest.yaml"
    path.write_text("old\n")
    path.chmod(0o640)
    atomic_write_text(path, "new\n")
    assert path.read_text() == "new\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert [p.name for p in d.iterdir()] == ["manifest.yaml"]


def test_a_new_file_gets_the_mode_a_plain_write_would_give_it():
    d = _dir()
    plain = d / "plain"
    plain.write_text("x")
    atomic_write_text(d / "atomic", "x")
    assert stat.S_IMODE((d / "atomic").stat().st_mode) == stat.S_IMODE(plain.stat().st_mode)


def test_an_interrupted_write_leaves_the_old_file_whole():
    """The failure this exists for: `Path.write_text` truncates first, so a power cut or a
    Ctrl-C between the truncate and the last byte left an empty manifest.yaml."""
    d = _dir()
    path = d / "manifest.yaml"
    path.write_text("run: a\nended: yes\n")
    real = os.fsync

    def cut(_fd):
        raise KeyboardInterrupt

    os.fsync = cut
    try:
        try:
            atomic_write_text(path, "run: a\nended: yes\ntags: [x]\n")
            raise AssertionError("the write was not interrupted")
        except KeyboardInterrupt:
            pass
    finally:
        os.fsync = real
    assert path.read_text() == "run: a\nended: yes\n"
    assert [p.name for p in d.iterdir()] == ["manifest.yaml"]      # the temporary file is gone


def test_a_symlink_is_written_through_and_a_hard_link_is_not():
    d = _dir()
    target = d / "real.yaml"
    target.write_text("old\n")
    link = d / "link.yaml"
    link.symlink_to(target)
    atomic_write_text(link, "new\n")
    assert link.is_symlink() and target.read_text() == "new\n"     # as an in-place write would
    # A hard link gets its own file: writing an export's linked copy must not reach the source.
    source = d / "source.yaml"
    source.write_text("profile: review\n")
    copy = d / "copy.yaml"
    os.link(source, copy)
    atomic_write_bytes(copy, b"profile: tiny\n")
    assert source.read_text() == "profile: review\n" and copy.read_text() == "profile: tiny\n"
    assert source.stat().st_ino != copy.stat().st_ino


def test_rig_lock_and_a_run_manifest_survive_an_interrupted_rewrite():
    from rig_cli import runs
    from rig_cli.lock import load_lock, save_lock

    root = _dir()
    save_lock(root, {"images": {"reg/a:1": "reg/a@sha256:" + "0" * 64}})
    before = (root / "rig.lock").read_text()
    run_dir = _dir() / "runs" / "20260901T100000Z_a"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.yaml").write_text("run: 20260901T100000Z_a\nstarted: 2026-09-01T10:00:00Z\n")
    manifest_before = (run_dir / "manifest.yaml").read_text()
    real = os.replace

    def cut(*_a, **_k):
        raise OSError(5, "Input/output error")

    os.replace = cut
    try:
        for write in (lambda: save_lock(root, {"images": {}}),
                      lambda: runs.mark_failed(run_dir, stage="startup", error="boom")):
            try:
                write()
                raise AssertionError("the rewrite was not interrupted")
            except OSError:
                pass
    finally:
        os.replace = real
    assert (root / "rig.lock").read_text() == before and load_lock(root)["images"]
    assert (run_dir / "manifest.yaml").read_text() == manifest_before
    assert sorted(p.name for p in root.iterdir()) == ["rig.lock"]
    assert sorted(p.name for p in run_dir.iterdir()) == ["manifest.yaml"]


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
