"""Replay hash reuse keeps integrity checks local to an invocation."""
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


class ProxyVerification(unittest.TestCase):
    def setUp(self):
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


if __name__ == "__main__":
    unittest.main()
