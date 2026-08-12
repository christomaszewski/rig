"""rig.lock foundation — load/save determinism, records, hash verification, bake merge.
Run: python3 tests/test_lock.py"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from rig_cli import RigError  # noqa: E402
from rig_cli.lock import (  # noqa: E402
    load_lock, record_instance, record_package, record_registry, save_lock, sha256_file,
    verify_payload,
)


def _root() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp())


def test_empty_lock_roundtrip_and_determinism():
    root = _root()
    lock = load_lock(root)
    assert lock == {"schema": 1}
    save_lock(root, lock)
    first = (root / "rig.lock").read_text()
    assert "GENERATED" in first
    save_lock(root, load_lock(root))
    assert (root / "rig.lock").read_text() == first  # byte-stable round trip


def test_records_and_reload():
    root = _root()
    lock = load_lock(root)
    record_registry(lock, "public", rtype="git", location="https://x/reg", commit="abc123")
    record_registry(lock, "dev", rtype="local-dir", location="/x/reg", commit=None)
    record_package(lock, "public/camera-service@1.4.2",
                   {"kind": "service", "source": {"repo": "r", "rev": "a" * 40}, "image": None})
    record_package(lock, "public/siyi-zr30@1.0.0",
                   {"kind": "profile", "payload_sha256": "d" * 64,
                    "requires": "public/camera-service@1.4.2"})
    record_instance(lock, "zr30", profile="public/siyi-zr30@1.0.0", base_sha256="d" * 64,
                    overlays=["internal/zr30-gideon@1.0.0"])
    save_lock(root, lock)
    back = load_lock(root)
    assert back["registries"]["public"] == {"type": "git", "url": "https://x/reg", "commit": "abc123"}
    assert back["registries"]["dev"] == {"type": "local-dir", "path": "/x/reg"}
    assert "image" not in back["packages"]["public/camera-service@1.4.2"]  # empties dropped
    assert back["instances"]["zr30"]["overlays"] == ["internal/zr30-gideon@1.0.0"]


def test_newer_lock_schema_refused():
    root = _root()
    (root / "rig.lock").write_text("schema: 99\n")
    try:
        load_lock(root)
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "upgrade rig" in str(exc)


def test_payload_hash_verification():
    root = _root()
    payload = root / "p.yaml"
    payload.write_text("a: 1\n")
    good = sha256_file(payload)
    verify_payload(payload, good, what="test")  # no raise
    try:
        verify_payload(payload, "0" * 64, what="test")
        raise AssertionError("expected RigError")
    except RigError as exc:
        assert "hash mismatch" in str(exc)


def test_bake_merges_images_into_existing_lock():
    # bake stages rig.lock = deployment lock + images section; simulate the merge path directly.
    from rig_cli.lock import load_lock as ll, save_lock as sl
    root, staging = _root(), _root()
    lock = ll(root)
    record_package(lock, "public/x@1.0.0", {"kind": "service", "source": {"repo": "r", "rev": "b" * 40}})
    sl(root, lock)
    merged = ll(root)
    merged["images"] = {"reg/cam:jp7": "sha256:" + "e" * 64}
    sl(staging, merged)
    staged = yaml.safe_load((staging / "rig.lock").read_text())
    assert "public/x@1.0.0" in staged["packages"] and staged["images"]  # both sections present


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
