"""Portable sparse exports: fleet transport, import/archive, catalog, and current sizes."""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml

from rig_cli import RigError, datadir, fleet, runcatalog, runs
from rig_cli.manifest import load_manifest
from rig_cli.service_exports import load_artifact
from test_fleet_verbs import _env, _export_tree, _fleet_yaml, _no_rsync, _run


def _file(path: pathlib.Path, root: pathlib.Path) -> dict:
    return {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _artifact(run: pathlib.Path) -> pathlib.Path:
    """An opaque media fixture: Rig verifies bytes; the camera reader owns codec validation."""
    stage = run / "exports" / "services" / "camera-service" / "h264" / ".stage-fixture"
    media = stage / "recordings" / "cam" / "s1.mkv"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"proxy-frame" * 2048)
    original = run / "recordings" / "cam" / "s1.mkv"
    definition = {"version": 1, "id": "h264", "revision": 1,
                  "format": "camera-recording-proxy/v1", "lossy": True}
    profile_hash = hashlib.sha256(json.dumps(definition, sort_keys=True,
                                             separators=(",", ":")).encode()).hexdigest()
    document = {
        "version": 1, "kind": "rig-service-export", "complete": True,
        "service": "camera-service",
        "profile": {"id": "h264", "revision": 1, "sha256": profile_hash,
                    "definition": definition},
        "producer": {"service_revision": "fixture", "image": "fixture@sha256:" + "2" * 64},
        "request_fingerprint": "3" * 64,
        "recordings": [{"name": "cam", "source_path": "recordings/cam", "path": "recordings/cam",
                        "sessions": [{"id": "s1", "source_files": [_file(original, run)],
                                      "files": [_file(media, stage)], "frame_count": 1}]}],
        "validation": {"ok": True},
    }
    document["export_id"] = hashlib.sha256(json.dumps(document, sort_keys=True,
                                                     separators=(",", ":")).encode()).hexdigest()
    (stage / "export.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
    artifact = stage.with_name(document["export_id"])
    stage.rename(artifact)
    load_artifact(artifact, verify=True, source_root=run)
    return artifact


def _fixture(*, remote=False):
    data = pathlib.Path(tempfile.mkdtemp()) / "data"
    deployment = _export_tree("vehicle", data)
    run = data / "runs" / "20260901T100000Z_survey"
    artifact = _artifact(run)
    row = {"id": 1, "name": "vehicle", "path": str(deployment), "data_dir": str(data)}
    if remote:
        row["host"] = "remotehost"
    roster = _fleet_yaml([row], mode="field")
    into = pathlib.Path(tempfile.mkdtemp()) / "harvest"
    destination = into / "survey" / "vehicle" / run.name
    return deployment, run, artifact, roster, into, destination


def test_sync_service_exports_requires_opt_in_and_preserves_nested_artifacts():
    for remote in (False, True):
        _, run, artifact, roster, into, destination = _fixture(remote=remote)
        unfinished = artifact.parent / ("e" * 64)
        unfinished.mkdir()
        (unfinished / "export.yaml").write_text("complete: false\n")
        abandoned = artifact.parent / ".stage-abandoned"
        abandoned.mkdir()
        (abandoned / "secret.mkv").write_text("unfinished")
        legacy = run / "exports" / "review"
        legacy.mkdir()
        (legacy / "manifest.yaml").write_text("run: legacy\n")
        rc, _, err = _run("fleet", "sync", "--fleet", str(roster), "--into", str(into))
        assert rc == 0, err
        assert not (destination / "exports").exists()
        rc, _, err = _run("fleet", "sync", "--fleet", str(roster), "--into", str(into),
                          "--include-service-exports")
        assert rc == 0 and "1 service export(s) verified" in err, err
        copied = destination / artifact.relative_to(run)
        assert load_artifact(copied, source_root=destination)["export_id"] == artifact.name
        assert (destination / "recordings" / "cam" / "s1.mkv").read_bytes() == b"v" * 8192
        assert not (destination / "export.yaml").exists()
        assert not (destination / "exports" / "review").exists()
        assert not (copied.parent / unfinished.name).exists()
        assert not list(copied.parent.glob(".*"))
        rc, _, err = _run("fleet", "sync", "--fleet", str(roster), "--into", str(into),
                          "--include-service-exports")
        assert rc == 0 and "1 service export(s) verified" not in err, err


def test_sync_rejects_corrupt_artifact_without_publishing_a_partial_generation():
    _, run, artifact, roster, into, destination = _fixture()
    (artifact / "recordings" / "cam" / "s1.mkv").write_bytes(b"broken")
    rc, _, err = _run("fleet", "sync", "--fleet", str(roster), "--into", str(into),
                      "--include-service-exports")
    assert rc == 1, err
    assert (destination / "manifest.yaml").is_file()  # original run transfer still succeeded
    assert not (destination / artifact.relative_to(run)).exists()
    assert not list((destination / "exports" / "services").rglob(".sync-*"))


def test_sync_without_rsync_transfers_only_committed_exports():
    _, run, artifact, roster, into, destination = _fixture(remote=True)
    unfinished = artifact.parent / ".stage-unfinished"
    unfinished.mkdir()
    (unfinished / "incomplete.mkv").write_text("not a committed artifact")
    log = pathlib.Path(tempfile.mkdtemp()) / "scp.log"
    with _no_rsync(), _env(SHIM_SCP_LOG=str(log)):
        rc, _, err = _run("fleet", "sync", "--fleet", str(roster), "--into", str(into),
                          "--include-service-exports")
    assert rc == 0, err
    copied = destination / artifact.relative_to(run)
    assert load_artifact(copied, source_root=destination)["export_id"] == artifact.name
    transfers = log.read_text().splitlines()
    assert any(f"{run}/manifest.yaml " in line for line in transfers)
    assert any(f"{artifact} " in line for line in transfers)
    assert not any(f"{run} " in line for line in transfers)  # never copy-then-drop all exports
    assert not any("unfinished" in line for line in transfers)
    # Already-pulled originals do not prevent adding/verifying immutable generations via scp.
    shutil.rmtree(copied)
    with _no_rsync():
        rc, _, err = _run("fleet", "sync", "--fleet", str(roster), "--into", str(into),
                          "--include-service-exports")
    assert rc == 0 and "1 service export(s) verified" in err, err
    assert load_artifact(copied, source_root=destination)


def test_sync_rejects_linked_artifact_before_scp_can_follow_it():
    _, run, artifact, roster, into, destination = _fixture(remote=True)
    outside = pathlib.Path(tempfile.mkdtemp()) / artifact.name
    shutil.move(artifact, outside)
    artifact.symlink_to(outside, target_is_directory=True)
    with _no_rsync():
        rc, _, err = _run("fleet", "sync", "--fleet", str(roster), "--into", str(into),
                          "--include-service-exports")
    assert rc == 1 and "linked service export" in err, err
    assert not (destination / artifact.relative_to(run)).exists()


def test_sync_rejects_manifest_in_wrong_service_namespace():
    _, run, artifact, roster, into, destination = _fixture()
    wrong = artifact.parent.parent.with_name("unrelated-service")
    artifact.parent.parent.rename(wrong)
    rc, _, err = _run("fleet", "sync", "--fleet", str(roster), "--into", str(into),
                      "--include-service-exports")
    assert rc == 1 and "invalid committed service export manifest" in err, err
    assert not (destination / wrong.relative_to(run)).exists()


def test_sync_cannot_flatten_service_exports_through_legacy_profile_mode():
    _, _, _, roster, into, _ = _fixture()
    rc, _, err = _run("fleet", "sync", "--fleet", str(roster), "--into", str(into),
                      "--profile", "review", "--include-service-exports")
    assert rc == 1 and "--include-service-exports" in err and "--profile" in err, err
    assert not into.exists()


def test_import_archive_catalog_and_size_preserve_service_exports():
    deployment, run, artifact, _, _, _ = _fixture()
    manifest = load_manifest(deployment)
    doc = yaml.safe_load((run / "manifest.yaml").read_text())
    doc["disk_kb"] = 1  # seal-time size must not hide newly generated proxy bytes
    (run / "manifest.yaml").write_text(yaml.safe_dump(doc))
    before = (run / "manifest.yaml").read_bytes()
    assert runs.run_disk_kb(run, doc) > 1
    catalog = runcatalog.scan_root(run.parent.parent, "registry")
    assert len(catalog) == 1 and catalog[0].disk_kb > 1
    assert len(runcatalog.scan_root(run / "exports", "directory", max_depth=10)) == 0
    imported = pathlib.Path(tempfile.mkdtemp()) / "20261021T100000Z_imported"
    shutil.copytree(run, imported)
    assert runs.import_runs(manifest, [str(imported)]) == 0
    adopted = run.parent / imported.name
    assert load_artifact(adopted / artifact.relative_to(run), source_root=adopted)
    archive = pathlib.Path(tempfile.mkdtemp()) / "archive"
    archive.mkdir()
    assert datadir.archive_runs(manifest, [run.name], str(archive)) == 0
    assert run.is_symlink()
    assert load_artifact(run / artifact.relative_to(run), source_root=run)
    assert (run / "manifest.yaml").read_bytes() == before


def test_archive_verification_detects_same_size_artifact_corruption():
    _, run, artifact, _, _, _ = _fixture()
    copied = pathlib.Path(tempfile.mkdtemp()) / "copy"
    shutil.copytree(run, copied)
    media = copied / artifact.relative_to(run) / "recordings" / "cam" / "s1.mkv"
    media.write_bytes(b"!" * media.stat().st_size)
    assert datadir._tree_stats(run) == datadir._tree_stats(copied)
    try:
        datadir.verify_copy(run, copied)
    except RigError:
        pass
    else:
        raise AssertionError("same-size proxy corruption passed archive verification")
    assert artifact.exists()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:
                failures += 1
                print("FAIL", name, "->", exc)
    sys.exit(bool(failures))
