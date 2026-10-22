"""Opt-in native Docker acquisition test using a disposable loopback registry.

Run with RIG_TEST_DOCKER=1. Requires registry:2 already cached; never pulls a
public image, touches the operator's registry, or removes an existing image tag.
"""
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli.image_catalog import docker_arch
from rig_cli.runtime_images import acquire_images, inspect_images


@unittest.skipUnless(os.environ.get("RIG_TEST_DOCKER") == "1", "set RIG_TEST_DOCKER=1 for isolated Docker test")
class NativeImageAcquisition(unittest.TestCase):
    def docker(self, *args, **kwargs):
        result = subprocess.run(["docker", *args], capture_output=True, timeout=60, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.decode().strip()

    def test_cold_pull_warm_cache_and_offline_identity(self):
        arch = docker_arch()
        self.docker("image", "inspect", "registry:2")
        name = "rig-replay-image-test-" + uuid.uuid4().hex[:12]
        ref = None
        self.docker("run", "--detach", "--rm", "--pull=never", "--name", name,
                    "--network", "host", "-e", "REGISTRY_HTTP_ADDR=127.0.0.1:0", "registry:2")
        try:
            # Bind an ephemeral loopback port in the daemon's own network namespace.
            # This also works on Desktop, where daemon localhost differs from macOS.
            for _ in range(100):
                logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, timeout=10)
                match = re.search(r"listening on 127\.0\.0\.1:(\d+)", logs.stdout + logs.stderr)
                if match:
                    break
                time.sleep(0.1)
            else:
                self.fail("disposable registry did not become ready")
            registry = "localhost:" + match[1]
            ref = registry + "/replay-fixture:" + name
            # Import a unique tiny native image, so no base download/build is involved.
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as tar:
                payload = name.encode()
                member = tarfile.TarInfo("replay-test-marker")
                member.size = len(payload)
                tar.addfile(member, io.BytesIO(payload))
            self.docker("import", "--platform", "linux/" + arch,
                        "--change", "LABEL io.rig.test.runtime=1", "-", ref, input=archive.getvalue())
            self.docker("push", ref)
            self.docker("image", "rm", ref)
            inventory = {"version": 1, "architecture": arch, "bundled": False,
                         "instances": {"player": {"images": [ref], "roles": {}}},
                         "images": {ref: {"source": ref, "owners": ["player"], "expected_ids": [],
                                          "required_labels": {"io.rig.test.runtime": "1"}}}}
            cold = inspect_images(inventory)
            self.assertEqual(cold["missing"], [ref])
            self.assertEqual(cold["proposed_pulls"], [ref])
            loaded = acquire_images(inventory, inspection=cold)
            self.assertTrue(loaded["ready"], loaded)
            self.assertEqual(loaded["pulled"], [ref])
            image_id = loaded["pins"]["player"]["images"][ref]
            actual = json.loads(self.docker("image", "inspect", ref))[0]
            self.assertEqual(image_id, actual["Id"])
            self.assertEqual(actual["Architecture"], arch)
            warm = acquire_images(inventory, inspection=inspect_images(inventory))
            self.assertTrue(warm["ready"])
            self.assertEqual(warm["pulled"], [])
            self.assertEqual(warm["pins"], loaded["pins"])
            self.docker("stop", name)
            self.assertTrue(inspect_images(inventory, policy="never")["ready"])
            self.docker("image", "rm", ref)
            offline = acquire_images(inventory, policy="never")
            self.assertFalse(offline["ready"])
            self.assertEqual(offline["proposed_pulls"], [])
        finally:
            # Names are generated above and belong exclusively to this test.
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
            if ref:
                subprocess.run(["docker", "image", "rm", ref], capture_output=True, timeout=30)

    @unittest.skipUnless(os.environ.get("RIG_TEST_PIN_HELPER") and os.environ.get("RIG_TEST_RUNTIME_IMAGE"),
                         "set helper path and cached runtime image for cross-repository pin test")
    def test_compose_starts_validated_id_after_temporary_tag_moves(self):
        image = os.environ["RIG_TEST_RUNTIME_IMAGE"]
        helper = Path(os.environ["RIG_TEST_PIN_HELPER"]).resolve(strict=True)
        arch = docker_arch()
        info = json.loads(self.docker("image", "inspect", image))[0]
        self.assertEqual(info["Architecture"], arch)
        image_id = info["Id"]
        self.docker("image", "inspect", "registry:2")
        project = "rig-replay-pin-test-" + uuid.uuid4().hex[:12]
        alias = project + ":fixture"
        with tempfile.TemporaryDirectory(prefix="rig-pin-test-") as temporary:
            compose = Path(temporary) / "compose.json"
            compose.write_text(json.dumps({"name": project, "services": {"bag-player": {
                "image": alias, "entrypoint": ["/bin/sh", "-c"], "command": ["exit 0"],
                "network_mode": "none", "read_only": True, "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"]}}}))
            self.docker("tag", image, alias)
            try:
                env = dict(os.environ, COMPOSE_PROJECT_NAME=project, RIG_TARGET_ARCH=arch)
                query = subprocess.run([sys.executable, str(helper), "runtime-images", "-f", str(compose)],
                                       env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(query.returncode, 0, query.stderr)
                self.assertEqual(json.loads(query.stdout)["images"], [alias])
                env["RIG_RUNTIME_IMAGE_PINS"] = json.dumps({"version": 1, "images": {alias: image_id}})
                # Move only our unique temporary tag to different cached bytes after validation.
                self.docker("tag", "registry:2", alias)
                launch = subprocess.run([sys.executable, str(helper), "pinned-up", "-f", str(compose),
                                         "--", "--detach"], env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(launch.returncode, 0, launch.stderr)
                container = self.docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + project)
                self.assertTrue(container)
                actual = json.loads(self.docker("inspect", container))[0]
                self.assertEqual(actual["Image"], image_id)
                self.assertEqual(self.docker("wait", container), "0")
            finally:
                subprocess.run(["docker", "compose", "-p", project, "-f", str(compose),
                                "down", "--remove-orphans", "--timeout", "1"], capture_output=True, timeout=30)
                subprocess.run(["docker", "image", "rm", alias], capture_output=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
