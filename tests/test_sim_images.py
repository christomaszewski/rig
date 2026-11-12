"""What rig sim says about the images a session runs.

`rig sim check` is read-only: it reports images that are not on the host and never pulls.
`rig sim up` pulls them before it starts anything. The check's message says so and gives the
pull, and an image that rig.lock's build catalog does not cover is reported as the simulation
session's, with the remedy. The deployment is test_sim_workflow's fixture.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_sim_external as external
from rig_cli import runtime_images
from rig_cli.lock import save_lock

CHILD = "localhost:5000/planner@sha256:" + "a" * 64
OTHER = "localhost:5000/base-ros@sha256:" + "b" * 64


def absent(inventory, **kwargs):
    """An inspection that found none of the session's images on this host."""
    return dict(ready=False, pins={}, errors=[], proposed_pulls=[CHILD, OTHER], images={},
                inventory_fingerprint="fixture")


class SessionImages(external.ExternalSession):
    def check(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = self.rig("sim", *argv, "--recipe", "world", "--json")
        return rc, json.loads(out.getvalue())

    def test_check_says_which_command_acquires_the_images_and_how_to_pull_them(self):
        with self.host(), patch("rig_cli.runtime_images.inspect_images", side_effect=absent), \
                patch("rig_cli.runtime_images.acquire_images") as acquire:
            rc, plan = self.check("check")
        self.assertEqual(rc, 1)
        self.assertFalse(plan["ready_to_run"])
        acquire.assert_not_called()  # the check is read-only
        issue, = [i for i in plan["issues"] if i["code"] == "images"]
        self.assertEqual(issue["status"], "not_checked")
        message = issue["message"]
        self.assertIn(CHILD, message)
        self.assertIn(OTHER, message)
        self.assertIn("rig sim up pulls them before it starts anything", message)
        self.assertIn(f"docker pull --platform linux/amd64 {CHILD}", message)
        self.assertIn(f"docker pull --platform linux/amd64 {OTHER}", message)
        self.assertNotIn("must be acquired", message)

    def test_up_does_acquire_them_as_the_check_says(self):
        """What the message promises: `rig sim up` pulls, then starts."""
        def acquired(inventory, **kwargs):
            self.assertEqual(kwargs["policy"], "missing")
            return dict(ready=True, errors=[], proposed_pulls=[], images={}, pulled=[CHILD, OTHER],
                        pins={name: {"version": 1, "images": {"fixture:latest": "sha256:" + "1" * 64}}
                              for name in inventory["instances"]})
        with self.host(), patch("rig_cli.runtime_images.inspect_images", side_effect=absent), \
                patch("rig_cli.runtime_images.acquire_images", side_effect=acquired) as acquire:
            rc, plan = self.check("up")
        self.assertEqual(rc, 0, plan["issues"])
        acquire.assert_called_once()
        self.assertEqual([c["name"] for c in self.f.calls if c["verb"] == "up"], ["bag_logger", "planner"])

    def test_with_pull_never_the_check_does_not_promise_a_pull(self):
        def refused(inventory, **kwargs):
            return dict(ready=False, pins={}, proposed_pulls=[], images={}, errors=[
                "planner: fixture:latest: native linux/amd64 image is missing with --pull never; "
                "pull it explicitly or allow --pull missing"])
        with self.host(), patch("rig_cli.runtime_images.inspect_images", side_effect=refused):
            rc, plan = self.check("check", "--pull", "never")
        self.assertEqual(rc, 1)
        messages = [i["message"] for i in plan["issues"] if i["code"] == "images"]
        self.assertEqual(len(messages), 1)
        self.assertIn("--pull never", messages[0])
        self.assertNotIn("rig sim up pulls", messages[0])

    def test_an_image_the_build_catalog_does_not_cover_is_not_called_a_replay_image(self):
        """`images.base` set to an image rig did not build, once rig build has written a catalog."""
        save_lock(self.root, {"build_catalog": {"localhost:5000/planner:v1": {
            "image": "planner", "digest": "sha256:" + "c" * 64, "architectures": ["amd64"],
            "children": {"amd64": "sha256:" + "a" * 64}, "variant": None}}})
        with self.host():
            rc, plan = self.check("check")
        self.assertEqual(rc, 1)
        message, = [i["message"] for i in plan["issues"] if i["status"] == "error"]
        self.assertIn("fixture:latest", message)
        self.assertIn("`rig image adopt fixture:latest`", message)
        self.assertNotIn("eplay", message)
        self.assertFalse(external.says_isaac(message), message)


if __name__ == "__main__":
    unittest.main()
