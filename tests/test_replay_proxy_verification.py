"""Full verification and opt-in persistent hash reuse retain source/artifact checks."""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rig_cli import RigError, service_exports as se


class VerificationCacheScope(unittest.TestCase):
    def test_linux_scope_changes_with_host_or_boot(self):
        with patch.object(se.os, "uname", return_value=SimpleNamespace(sysname="Linux", nodename="vehicle")), \
                patch.object(Path, "read_text", return_value="12345678-1234-1234-1234-123456789abc\n"):
            first = se._verification_cache_scope()
            self.assertIsNotNone(first)
            self.assertEqual(se._verification_cache_scope(), first)
            with patch.object(Path, "read_text", return_value="abcdefab-abcd-abcd-abcd-abcdefabcdef\n"):
                self.assertNotEqual(se._verification_cache_scope(), first)

    def test_darwin_boot_time_binding_ignores_struct_padding_and_refuses_denial(self):
        import ctypes

        class DarwinTimeval(ctypes.Structure):
            _fields_ = [("seconds", ctypes.c_long), ("microseconds", ctypes.c_int)]

        class Sysctl:
            result = 0

            def __call__(self, name, output, size, _new, _length):
                self.name = name
                ctypes.memset(output, 0xA5, ctypes.sizeof(DarwinTimeval))
                value = ctypes.cast(output, ctypes.POINTER(DarwinTimeval)).contents
                value.seconds, value.microseconds = 1234567890, 123456
                ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t)).contents.value = ctypes.sizeof(DarwinTimeval)
                return self.result

        syscall = Sysctl()
        with patch.object(se.os, "uname", return_value=SimpleNamespace(sysname="Darwin", nodename="mac")), \
                patch.object(Path, "read_text", side_effect=FileNotFoundError), \
                patch.object(ctypes, "CDLL", return_value=SimpleNamespace(sysctlbyname=syscall)):
            self.assertEqual(se._verification_cache_scope(), "mac:1234567890:123456")
            self.assertEqual(syscall.name, b"kern.boottime")
            syscall.result = -1
            self.assertIsNone(se._verification_cache_scope())


class ProxyVerification(unittest.TestCase):
    def setUp(self):
        # Host boot discovery may be denied in a sandbox. Cache behavior is independent
        # of that platform permission; use one explicit machine/boot identity per test.
        scope = patch.object(se, "_verification_cache_scope", return_value="verification-test-boot")
        scope.start()
        self.addCleanup(scope.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.run = self.root / "run"
        self.folder = self.run / "exports/services/camera/h264/staging"
        self.folder.mkdir(parents=True)
        definition = dict(version=1, id="h264", revision=1,
                          format="camera-recording-proxy/v1", lossy=True)
        (self.root / "h264.json").write_text(json.dumps(definition))
        self.descriptor = SimpleNamespace(
            service="camera", repo=self.root, replay_source=SimpleNamespace(binding="direct"),
            export_source=SimpleNamespace(modes=["service"], default_profile="h264",
                                          profiles={"h264": "h264.json"}))
        self.bindings = []
        recordings = []
        for name in ("front", "rear"):
            relative = f"recordings/{name}"
            for base, prefix in ((self.run, b"original"), (self.folder, b"proxy")):
                (base / relative).mkdir(parents=True)
                for suffix in ("mkv", "csv"):
                    (base / relative / f"s1.{suffix}").write_bytes(prefix + name.encode())
            paths = [f"{relative}/s1.{suffix}" for suffix in ("mkv", "csv")]
            recordings.append(dict(name=name, source_path=relative, path=relative,
                sessions=[dict(id="s1", files=se._inventory(self.folder, paths),
                               source_files=se._inventory(self.run, paths))]))
            self.bindings.append(dict(path=str(self.run / relative), recorded_as=name,
                                      sessions=["s1"], files=[str(self.run / p) for p in paths]))
        self.document = dict(version=1, kind=se.KIND, complete=True, service="camera",
            profile=dict(id="h264", revision=1, sha256=se.digest(definition), definition=definition),
            recordings=recordings, validation=dict(ok=True))
        self.document["export_id"] = se.digest(self.document)
        (self.folder / "export.yaml").write_text(json.dumps(self.document))
        final = self.folder.with_name(self.document["export_id"])
        self.folder.rename(final)
        self.folder = final
        self.outputs = [self.folder / f["path"] for r in recordings
                        for s in r["sessions"] for f in s["files"]]
        self.originals = [self.run / f["path"] for r in recordings
                          for s in r["sessions"] for f in s["source_files"]]
        self.hashes = Counter()
        self.progress = []
        real_sha256 = se.file_sha256

        def counted(path):
            self.hashes[Path(path)] += 1
            return real_sha256(path)

        self.counted_sha256 = counted
        self.hash_patch = patch.object(se, "file_sha256", side_effect=counted)
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)

    def resolve(self, binding, verification=None):
        return se.resolve_proxy_binding(self.run, binding, service="camera",
            descriptor=self.descriptor, verification=verification)

    def context(self):
        return se.VerificationContext(progress=self.progress.append)

    def persistent_context(self, *, reuse=False, writable=True):
        return se.VerificationContext(progress=self.progress.append,
            cache_path=self.root / "var/replay/verification-cache.json", reuse=reuse, writable=writable)

    def prime_cache(self):
        context = self.persistent_context()
        for binding in self.bindings:
            self.resolve(binding, context)
        context.save()
        self.assertTrue((self.root / "var/replay/verification-cache.json").is_file())
        return context

    def corrupt_preserving_mtime(self, path):
        before = path.stat()
        data = path.read_bytes()
        path.write_bytes(b"!" + data[1:])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def test_shared_export_and_repeated_plans_hash_every_input_once(self):
        context = self.context()
        for _ in range(3):  # initial planning, preparation, prepared revalidation
            for binding in self.bindings:
                resolved = self.resolve(binding, context)
                self.assertEqual(resolved["original"], binding)
        self.assertEqual(self.hashes, Counter({p: 1 for p in
            [*self.outputs, *self.originals, self.folder / "export.yaml"]}))
        self.assertEqual(len(self.progress), len(self.hashes))
        for path in self.hashes:
            self.assertTrue(any(f"{path} ({path.stat().st_size} bytes)" in message
                                for message in self.progress))

    def test_new_invocation_rehashes_unchanged_files(self):
        for _ in range(2):
            context = self.context()
            for binding in self.bindings:
                self.resolve(binding, context)
        self.assertEqual(self.hashes, Counter({p: 2 for p in
            [*self.outputs, *self.originals, self.folder / "export.yaml"]}))

    def test_without_context_each_call_still_verifies_bytes(self):
        for _ in range(2):
            self.resolve(self.bindings[0])
        self.assertTrue(all(self.hashes[p] == 2 for p in self.outputs + self.originals[:2]))

    def test_original_and_output_mutation_invalidate_even_when_mtime_restored(self):
        for path in (self.outputs[0], self.originals[0]):
            with self.subTest(path=path):
                context = self.context()
                self.resolve(self.bindings[0], context)
                original = path.read_bytes()
                before_hashes = self.hashes[path]
                self.corrupt_preserving_mtime(path)
                with self.assertRaisesRegex(RigError, "missing or changed file"):
                    self.resolve(self.bindings[0], context)
                self.assertEqual(self.hashes[path], before_hashes + 1)
                path.write_bytes(original)

    def test_replacement_with_same_size_and_mtime_is_rejected(self):
        context = self.context()
        self.resolve(self.bindings[0], context)
        path = self.outputs[0]
        before = path.stat()
        replacement = path.with_suffix(".tmp")
        replacement.write_bytes(b"!" * before.st_size)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(path)
        with self.assertRaisesRegex(RigError, "missing or changed file"):
            self.resolve(self.bindings[0], context)
        self.assertEqual(self.hashes[path], 2)

    def test_mutation_during_hash_is_rejected_and_not_cached(self):
        context = self.context()
        path = self.outputs[0]
        original = path.read_bytes()

        def mutate_after_hash(candidate):
            checksum = self.counted_sha256(candidate)
            if Path(candidate) == path:
                self.corrupt_preserving_mtime(path)
            return checksum

        with patch.object(se, "file_sha256", side_effect=mutate_after_hash):
            with self.assertRaisesRegex(RigError, "source changed while hashing"):
                self.resolve(self.bindings[0], context)
        path.write_bytes(original)
        self.resolve(self.bindings[0], context)
        self.assertEqual(self.hashes[path], 2)

    def test_manifest_and_complete_inventory_are_checked_on_reuse(self):
        context = self.context()
        self.resolve(self.bindings[0], context)
        extra = self.folder / "extra"
        extra.write_text("unexpected")
        with self.assertRaisesRegex(RigError, "unexpected or missing artifact files"):
            self.resolve(self.bindings[0], context)
        extra.unlink()
        self.document["recordings"][0]["sessions"][0]["files"][0]["sha256"] = "0" * 64
        self.document["export_id"] = se.digest({k: v for k, v in self.document.items()
                                               if k != "export_id"})
        (self.folder / "export.yaml").write_text(json.dumps(self.document))
        # A valid new manifest must still compare its expected hashes with actual bytes.
        with self.assertRaisesRegex(RigError, "missing or changed file"):
            se.load_artifact(self.folder, verification=context)

    def test_containment_is_checked_on_reuse(self):
        context = self.context()
        self.resolve(self.bindings[0], context)
        path = self.outputs[0]
        outside = self.root / "outside.mkv"
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaisesRegex(RigError, "path escapes its root"):
            se.load_artifact(self.folder, verification=context)

    def test_manifest_mutation_during_verification_is_rejected(self):
        manifest = self.folder / "export.yaml"

        def mutate_manifest(candidate):
            checksum = self.counted_sha256(candidate)
            manifest.write_text(manifest.read_text() + "\n")
            return checksum

        with patch.object(se, "file_sha256", side_effect=mutate_manifest):
            with self.assertRaisesRegex(RigError, "manifest changed during verification"):
                se.load_artifact(self.folder, verification=self.context())

    def test_source_alias_uses_same_verified_file_identity(self):
        context = self.context()
        self.resolve(self.bindings[0], context)
        alias = self.root / "alias"
        alias.symlink_to(self.bindings[0]["path"], target_is_directory=True)
        aliased_binding = {**self.bindings[0], "path": str(alias)}
        self.resolve(aliased_binding, context)
        self.assertTrue(all(count == 1 for count in self.hashes.values()))

    def test_full_verification_seeds_fast_reuse_without_repeat_hashing(self):
        full = self.prime_cache()
        expected = {*self.outputs, *self.originals, self.folder / "export.yaml"}
        self.assertEqual(full.summary(), dict(mode="full", hashed_files=len(expected),
            hashed_bytes=sum(path.stat().st_size for path in expected), cached_files=0, cached_bytes=0))
        before = self.hashes.copy()
        cached = self.persistent_context(reuse=True)
        for _ in range(3):
            for binding in self.bindings:
                self.resolve(binding, cached)
        self.assertEqual(self.hashes, before)
        self.assertEqual(cached.summary(), dict(mode="cached", hashed_files=0, hashed_bytes=0,
            cached_files=len(expected), cached_bytes=sum(path.stat().st_size for path in expected)))
        # Full mode remains a fresh byte scan even when persistent facts are available.
        again = self.persistent_context()
        for binding in self.bindings:
            self.resolve(binding, again)
        self.assertEqual(self.hashes, Counter({path: 2 for path in expected}))

    def test_fast_mode_with_no_cache_hashes_and_read_only_mode_never_writes(self):
        cached = self.persistent_context(reuse=True, writable=False)
        self.resolve(self.bindings[0], cached)
        cached.save()
        self.assertFalse((self.root / "var").exists())
        self.assertGreater(cached.summary()["hashed_files"], 0)
        self.assertEqual(cached.summary()["cached_files"], 0)
        self.prime_cache()
        path = self.root / "var/replay/verification-cache.json"
        contents, identity = path.read_bytes(), se._file_identity(path)
        read_only = self.persistent_context(reuse=True, writable=False)
        self.resolve(self.bindings[0], read_only)
        read_only.save()
        self.assertEqual((path.read_bytes(), se._file_identity(path)), (contents, identity))

    def test_fast_reuse_detects_replacements_and_same_size_restored_mtime_edits(self):
        self.prime_cache()
        for path, replace in ((self.outputs[0], False), (self.originals[0], True)):
            with self.subTest(path=path):
                original, before = path.read_bytes(), path.stat()
                if replace:
                    replacement = path.with_suffix(".replacement")
                    replacement.write_bytes(b"!" * before.st_size)
                    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                    replacement.replace(path)
                else:
                    self.corrupt_preserving_mtime(path)
                count = self.hashes[path]
                context = self.persistent_context(reuse=True)
                with self.assertRaisesRegex(RigError, "missing or changed file"):
                    self.resolve(self.bindings[0], context)
                self.assertEqual(self.hashes[path], count + 1)
                path.write_bytes(original)

    def test_fast_reuse_still_checks_manifest_inventory_and_containment(self):
        self.prime_cache()
        extra = self.folder / "extra"
        extra.write_text("unexpected")
        with self.assertRaisesRegex(RigError, "unexpected or missing artifact files"):
            self.resolve(self.bindings[0], self.persistent_context(reuse=True))
        extra.unlink()
        manifest = self.folder / "export.yaml"
        original_manifest = manifest.read_bytes()
        self.document["recordings"][0]["sessions"][0]["files"][0]["sha256"] = "0" * 64
        self.document["export_id"] = se.digest({k: v for k, v in self.document.items() if k != "export_id"})
        manifest.write_text(json.dumps(self.document))
        with self.assertRaisesRegex(RigError, "missing or changed file"):
            se.load_artifact(self.folder, verification=self.persistent_context(reuse=True))
        manifest.write_bytes(original_manifest)
        outside = self.root / "outside.mkv"
        self.outputs[0].rename(outside)
        self.outputs[0].symlink_to(outside)
        with self.assertRaisesRegex(RigError, "path escapes its root"):
            se.load_artifact(self.folder, verification=self.persistent_context(reuse=True))

    def test_bad_cache_version_scope_or_entries_fall_back_to_full_hashing(self):
        self.prime_cache()
        path = self.root / "var/replay/verification-cache.json"
        original = json.loads(path.read_text())
        bad_entries = json.loads(path.read_text())
        first_entry = next(iter(bad_entries["checksums"].values()))
        first_entry["identity"][3] = "not-a-size"
        bad_digest = json.loads(path.read_text())
        next(iter(bad_digest["checksums"].values()))["sha256"] = "not-a-hash"
        documents = ["not json", "null", json.dumps({**original, "version": 999}),
                     json.dumps({**original, "scope": "another-host-boot"}),
                     json.dumps(bad_entries), json.dumps(bad_digest)]
        for document in documents:
            with self.subTest(document=document[:60]):
                path.write_text(document)
                context = self.persistent_context(reuse=True)
                before = sum(self.hashes.values())
                self.resolve(self.bindings[0], context)
                self.assertGreater(sum(self.hashes.values()), before)
                self.assertEqual(context.summary()["cached_files"], 0)

    def test_changed_during_hash_is_not_persisted(self):
        context = self.persistent_context()
        path = self.outputs[0]

        def mutate(candidate):
            checksum = self.counted_sha256(candidate)
            if Path(candidate) == path:
                self.corrupt_preserving_mtime(path)
            return checksum

        with patch.object(se, "file_sha256", side_effect=mutate):
            with self.assertRaisesRegex(RigError, "source changed while hashing"):
                self.resolve(self.bindings[0], context)
        context.save()
        saved = json.loads((self.root / "var/replay/verification-cache.json").read_text())
        self.assertNotIn(str(path), saved["checksums"])

    def test_unknown_host_scope_disables_persistent_reuse_and_writes(self):
        with patch.object(se, "_verification_cache_scope", return_value=None):
            context = self.persistent_context(reuse=True)
            self.resolve(self.bindings[0], context)
            context.save()
        self.assertFalse((self.root / "var").exists())
        self.assertEqual(context.summary()["cached_files"], 0)

    def test_cache_save_failure_does_not_fail_verified_replay(self):
        context = self.persistent_context()
        self.resolve(self.bindings[0], context)
        with patch.object(se.os, "replace", side_effect=OSError("cache is read-only")):
            context.save()
        self.assertTrue(any("could not save verification cache" in message for message in self.progress))
        self.assertFalse(list((self.root / "var/replay").glob(".verification-cache-*.tmp")))

    def test_launch_revalidation_rejects_mutated_or_removed_cached_input(self):
        self.prime_cache()
        context = self.persistent_context(reuse=True)
        self.resolve(self.bindings[0], context)
        before = self.hashes.copy()
        context.revalidate()
        self.assertEqual(self.hashes, before)
        self.corrupt_preserving_mtime(self.outputs[0])
        with self.assertRaisesRegex(RigError, "changed since verification"):
            context.revalidate()
        self.assertNotIn(str(self.outputs[0]), context._checksums)
        self.originals[0].unlink()
        with self.assertRaisesRegex(RigError, "changed since verification"):
            context.revalidate()


if __name__ == "__main__":
    unittest.main()
