"""Per-repo bare git mirrors (gitmirror): one fetch path for every git source a build needs, and
offline resolution from it. Upstreams are local repos; "offline" deletes or ignores them, so any
network attempt would fail. Run: python3 tests/test_gitmirror.py"""
import contextlib
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rig_cli import RigError, gitmirror  # noqa: E402
from rig_cli.install import _fetch_source  # noqa: E402

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


@contextlib.contextmanager
def _home():
    old = {k: os.environ.get(k) for k in ("RIG_HOME", *GIT_ENV)}
    os.environ.update(GIT_ENV, RIG_HOME=tempfile.mkdtemp())
    gitmirror.reset()
    try:
        yield
    finally:
        gitmirror.reset()
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _git(*args, cwd):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _repo(name="up", files=None) -> pathlib.Path:
    repo = pathlib.Path(tempfile.mkdtemp()) / name
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _commit(repo, files or {"a.txt": "one\n"})
    return repo


def _commit(repo, files) -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "c", cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


def _offline():
    gitmirror.offline = True
    gitmirror._fetched.clear()


def test_resolve_creates_one_mirror_and_refetches_only_on_a_miss():
    with _home():
        up = _repo()
        first = _git("rev-parse", "HEAD", cwd=up)
        _git("tag", "v1", cwd=up)
        assert gitmirror.resolve(str(up), "v1") == first
        assert gitmirror.resolve(str(up), "main") == first
        mirror = gitmirror.mirror_path(str(up))
        assert (mirror / "HEAD").is_file() and not (mirror / ".git").exists()  # bare
        assert gitmirror.mirror_path(str(up) + "/") == mirror  # the src cache's slug key
        second = _commit(up, {"b.txt": "two\n"})
        assert gitmirror.resolve(str(up), "main") == first   # fetched once this process
        assert gitmirror.resolve(str(up), second) == second  # a miss refetches
        assert gitmirror.resolve(str(up), "main") == second
        _git("checkout", "-q", "--detach", cwd=up)
        _git("branch", "-q", "-D", "main", cwd=up)  # the upstream drops the branch: pins survive gc
        _git("gc", "-q", "--prune=now", cwd=mirror)
        assert gitmirror.resolve(str(up), first) == first


def test_offline_resolves_from_the_mirror_and_names_what_is_missing():
    with _home():
        up = _repo()
        sha = gitmirror.resolve(str(up), "main")
        shutil.rmtree(up)
        _offline()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert gitmirror.resolve(str(up), "main") == sha
        assert "branch 'main' resolved offline to " + sha[:12] in err.getvalue()
        assert "mirror fetched" in err.getvalue()
        for url, ref, needle in ((str(up), "v9", "ref 'v9' not found in the local mirror"),
                                 ("https://example.invalid/x.git", "v1", "not in the local mirror")):
            try:
                gitmirror.resolve(url, ref)
                raise AssertionError("resolved a missing ref offline")
            except RigError as exc:
                assert needle in str(exc) and "rig src sync" in str(exc), exc


def test_online_fetch_failure_points_at_offline_only_when_a_mirror_exists():
    with _home():
        try:  # no mirror yet: offline could not help either, so no such hint
            gitmirror.resolve(str(pathlib.Path(tempfile.mkdtemp()) / "missing"), "v1")
            raise AssertionError("fetched a missing repo")
        except RigError as exc:
            assert "cannot fetch" in str(exc) and "--offline" not in str(exc), exc
        assert not list((pathlib.Path(os.environ["RIG_HOME"]) / "cache" / "git").glob("*.git"))
        up = _repo()
        gitmirror.resolve(str(up), "main")
        shutil.rmtree(up)
        gitmirror.reset()
        try:  # a mirror exists: name its age and the offline way out
            gitmirror.resolve(str(up), "main")
            raise AssertionError("fetched a deleted repo")
        except RigError as exc:
            assert "the local mirror has a copy from" in str(exc), exc
            assert "rig build --offline" in str(exc), exc


def test_upstream_branch_renamed_below_its_old_name_does_not_wedge_the_mirror():
    with _home():
        up = _repo()
        _git("branch", "feature", cwd=up)
        pinned = gitmirror.resolve(str(up), "feature")
        _git("branch", "-D", "feature", cwd=up)
        _git("branch", "feature/x", cwd=up)  # refs/heads/feature -> refs/heads/feature/x
        gitmirror.reset()
        assert gitmirror.resolve(str(up), "feature/x") == pinned
        assert gitmirror.resolve(str(up), pinned) == pinned  # the pin survived the prune


def test_relative_local_repo_urls_resolve_against_the_callers_cwd():
    with _home():
        up = _repo("rel")
        cwd = os.getcwd()
        os.chdir(up.parent)
        try:
            assert gitmirror.resolve("./rel", "main") == _git("rev-parse", "HEAD", cwd=up)
        finally:
            os.chdir(cwd)


def test_stale_gitmodules_entries_and_update_none_are_skipped_like_git():
    with _home():
        lib = _repo("lib", {"lib.txt": "lib\n"})
        top = _repo("top", {"x.txt": "x\n"})
        _git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "vendor/lib",
             cwd=top)
        _git("config", "-f", ".gitmodules", "submodule.gone.path", "vendor/gone", cwd=top)
        _git("config", "-f", ".gitmodules", "submodule.gone.url", str(lib), cwd=top)
        _git("config", "-f", ".gitmodules", "submodule.vendor/lib.update", "none", cwd=top)
        _git("add", "-A", cwd=top)
        _git("commit", "-q", "-m", "stale entry + update=none", cwd=top)
        sha = gitmirror.resolve(str(top), "main")
        dest = pathlib.Path(tempfile.mkdtemp()) / "co"
        gitmirror.clone(str(top), sha, dest)
        gitmirror.update_submodules(dest, str(top))  # the stale entry no longer breaks it
        assert not (dest / "vendor/lib/lib.txt").exists()  # update = none: left alone
        assert _git("remote", "get-url", "origin", cwd=dest) == str(top)  # origin untouched


def test_unadvertised_pinned_commit_is_fetched_by_id():
    with _home():
        up = _repo()
        _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=up)
        gitmirror.resolve(str(up), "main")
        _git("checkout", "-q", "-b", "scratch", cwd=up)
        orphan = _commit(up, {"c.txt": "pinned, then its branch deleted\n"})
        _git("checkout", "-q", "main", cwd=up)
        _git("branch", "-q", "-D", "scratch", cwd=up)
        assert gitmirror.resolve(str(up), orphan) == orphan


def test_clone_and_export_work_offline():
    with _home():
        up = _repo(files={"a.txt": "one\n", "pkg/b.txt": "two\n"})
        sha = gitmirror.resolve(str(up), "main")
        shutil.rmtree(up)
        _offline()
        dest = pathlib.Path(tempfile.mkdtemp()) / "co"
        gitmirror.clone(str(up), sha, dest)
        assert _git("rev-parse", "HEAD", cwd=dest) == sha
        assert _git("remote", "get-url", "origin", cwd=dest) == str(up)
        out = pathlib.Path(tempfile.mkdtemp()) / "tree"
        gitmirror.export(str(up), sha, out)
        assert sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()) == \
            ["a.txt", "pkg/b.txt"]


def test_submodules_resolve_through_their_mirrors_offline():
    with _home():
        inner = _repo("inner", {"deep.txt": "deep\n"})
        lib = _repo("lib", {"lib.txt": "lib\n"})
        _git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "inner",
             cwd=lib)
        _git("commit", "-q", "-m", "inner", cwd=lib)
        top = pathlib.Path(tempfile.mkdtemp()) / "top"
        top.mkdir()
        _git("init", "-q", "-b", "main", cwd=top)
        (top / "x.txt").write_text("x\n")
        # a RELATIVE submodule URL: resolved against the superproject's remote, as git does
        rel = os.path.relpath(lib, top)
        _git("-c", "protocol.file.allow=always", "submodule", "add", "-q", rel, "vendor/lib",
             cwd=top)
        _git("add", "-A", cwd=top)
        _git("commit", "-q", "-m", "top", cwd=top)
        sha = gitmirror.resolve(str(top), "main")
        gitmirror.sync_submodules(str(top), sha)  # `rig src sync`: mirrors only, no checkout
        for repo in (top, lib, inner):
            shutil.rmtree(repo.parent)
        _offline()
        dest = pathlib.Path(tempfile.mkdtemp()) / "co"
        gitmirror.clone(str(top), sha, dest)
        gitmirror.update_submodules(dest, str(top))
        assert (dest / "vendor/lib/inner/deep.txt").read_text() == "deep\n"
        out = pathlib.Path(tempfile.mkdtemp()) / "tree"
        gitmirror.export(str(top), sha, out, submodules=True)
        assert (out / "vendor/lib/inner/deep.txt").read_text() == "deep\n"
        assert not list(out.rglob(".git"))


def test_fetch_source_checks_out_a_new_rev_offline_from_the_mirror():
    with _home():
        up = _repo(files={"svc/rigging.yaml": "service: svc\n"})
        old = _git("rev-parse", "HEAD", cwd=up)
        new = _commit(up, {"svc/more.txt": "more\n"})
        gitmirror.resolve(str(up), "main")  # an online fetch while the pin was still `old`
        shutil.rmtree(up)
        _offline()
        for rev in (old, new):
            got = _fetch_source("svc", {"repo": str(up), "rev": rev, "path": "svc"})
            assert _git("rev-parse", "HEAD", cwd=got) == rev


def test_absolute_submodule_urls():
    cases = [("../c.git", "https://h/a/b.git", "https://h/a/c.git"),
             ("./c", "https://h/a/b", "https://h/a/b/c"),
             ("../../c.git", "https://h/a/b.git", "https://h/c.git"),
             ("../c.git", "git@h:a/b.git", "git@h:a/c.git"),
             ("../../c.git", "git@h:a/b.git", "git@h:c.git"),
             ("https://x/y.git", "https://h/a/b.git", "https://x/y.git")]
    for url, base, want in cases:
        assert gitmirror._absolute(url, base) == want, (url, base, gitmirror._absolute(url, base))


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
