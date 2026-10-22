"""Hermetic runtime-image inventory, native acquisition, and immutable launch identity tests."""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rig_cli import RigError, artifact_images, runtime_images as images
from rig_cli.descriptor import Descriptor
from rig_cli.manifest import Manifest, RosSettings, Sensor

IMAGE_ID = "sha256:" + "1" * 64
CHILD = "sha256:" + "2" * 64
INDEX = "sha256:" + "3" * 64
OTHER_ID = "sha256:" + "4" * 64
LABEL = "io.rig.camera.replay-bindings"


def image_info(*, architecture="arm64", image_id=IMAGE_ID, labels=None):
    return dict(Id=image_id, Os="linux", Architecture=architecture,
                Config={"Labels": labels or {}})


class RuntimeImages(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        launcher = self.root / "up"
        launcher.write_text("#!/bin/sh\nexit 99\n")
        self.desc = Descriptor(service="camera", repo=self.root, launcher="up",
                               verbs={"runtime-images": "runtime-images"}, ros_distro=None,
                               external_volumes=[], host_ports=[])
        self.rows = [Sensor(name="cam", service="camera", config=self.root / "unused.yaml",
                            enabled=False, order=0)]
        self.manifest = Manifest(vehicle="test", vehicle_id=7, ros=RosSettings(7, "rmw_zenoh_cpp", None),
                                 sensors=self.rows, platform="dev")
        self.configs = {"cam": {"name": "cam", "replay": {"path": "/absolute/source"}}}
        self.responses = {"cam": {"version": 1, "images": ["localhost:5000/cam:v1"],
                                  "roles": {"cam-dev": "localhost:5000/cam:v1"}}}
        self.local = {}
        self.calls = []
        self.queries = []
        self.pull_info = image_info()
        self.pull_error = None
        self.inspect_error = None
        self.query_error = None
        self.mock = patch("rig_cli.runtime_images.subprocess.run", side_effect=self.run_command)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def run_command(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] != "docker":
            config = yaml.safe_load(Path(argv[1]).read_text())
            self.queries.append((config, kwargs))
            if isinstance(self.query_error, Exception):
                raise self.query_error
            if self.query_error:
                return subprocess.CompletedProcess(argv, 2, "", self.query_error)
            payload = self.responses[config["name"]]
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        if argv[1:3] == ["image", "inspect"]:
            if isinstance(self.inspect_error, Exception):
                raise self.inspect_error
            if self.inspect_error:
                return subprocess.CompletedProcess(argv, 1, "", self.inspect_error)
            source = argv[-1]
            if source not in self.local:
                return subprocess.CompletedProcess(argv, 1, "", f"Error: No such image: {source}")
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.local[source]), "")
        if argv[1] == "pull":
            if isinstance(self.pull_error, BaseException):
                raise self.pull_error
            if self.pull_error:
                return subprocess.CompletedProcess(argv, self.pull_error)
            self.local[argv[-1]] = copy.deepcopy(self.pull_info)
            return subprocess.CompletedProcess(argv, 0)
        raise AssertionError(f"Unexpected Docker operation: {argv}")

    def inventory(self, **kwargs):
        return images.inventory(self.manifest, {"camera": self.desc}, self.rows, self.configs,
                                architecture="arm64", env={"VEHICLE_ID": "7", "CUSTOM": "passed"}, **kwargs)

    def docker_calls(self, verb):
        return [argv for argv, _ in self.calls if argv[:2] == ["docker", verb]]

    def catalog(self):
        self.manifest.image_registry = "localhost:5000"
        self.manifest.build_catalog = {"build-host:5000/cam:v1": {
            "image": "cam", "digest": INDEX, "architectures": ["arm64", "amd64"],
            "children": {"arm64": CHILD, "amd64": OTHER_ID}, "variant": "dev"}}

    def second_instance(self, *, ref="localhost:5000/cam:v1"):
        self.rows.append(Sensor(name="cam2", service="camera", config=self.root / "unused2.yaml",
                                enabled=False, order=1))
        self.configs["cam2"] = {"name": "cam2"}
        self.responses["cam2"] = {"version": 1, "images": [ref], "roles": {"cam-dev": ref}}

    def test_selected_disabled_instance_gets_exact_config_and_instance_environment(self):
        env = {"VEHICLE_ID": "7", "CUSTOM": "passed", "RIG_RUNTIME_IMAGE_MAP": "old",
               "RIG_RUNTIME_IMAGE_PINS": "old"}
        result = images.inventory(self.manifest, {"camera": self.desc}, self.rows, self.configs,
                                  architecture="arm64", env=env)
        self.assertEqual(result["images"]["localhost:5000/cam:v1"]["owners"], ["cam"])
        config, options = self.queries[0]
        self.assertEqual(config, self.configs["cam"])
        self.assertEqual(options["env"]["COMPOSE_PROJECT_NAME"], "cam-vehicle-7")
        self.assertEqual(options["env"]["CUSTOM"], "passed")
        self.assertNotIn("RIG_RUNTIME_IMAGE_MAP", options["env"])
        self.assertNotIn("RIG_RUNTIME_IMAGE_PINS", options["env"])
        self.assertEqual(options["timeout"], images.QUERY_TIMEOUT)
        self.assertEqual(list(self.root.iterdir()), [self.root / "up"])
        self.assertIn("RIG_RUNTIME_IMAGE_MAP", env)  # caller's environment remains unchanged

    def test_missing_contract_never_falls_back_to_build_outputs(self):
        self.desc.verbs.clear()
        self.desc.build_images = ["not-the-runtime-image"]
        with self.assertRaisesRegex(RigError, "upgrade.*verbs.runtime-images"):
            self.inventory()
        self.assertEqual(self.calls, [])

    def test_inventory_response_validation(self):
        for value in ([], {}, {"version": True, "images": ["repo:tag"]},
                      {"version": 2, "images": ["repo:tag"]},
                      {"version": 1, "images": []}, {"version": 1, "images": ["repo:tag", "repo:tag"]},
                      {"version": 1, "images": ["{{image}}"]},
                      {"version": 1, "images": ["repo@broken"]},
                      {"version": 1, "images": ["repo:tag"], "roles": {"reader": "other:tag"}}):
            with self.subTest(value=value), self.assertRaises(RigError):
                images.parse_inventory(json.dumps(value), instance="cam")
        with self.assertRaisesRegex(RigError, "JSON"):
            images.parse_inventory("logging noise\n{}", instance="cam")

    def test_query_failure_and_timeout_are_actionable(self):
        for problem in ("unresolved image configuration", subprocess.TimeoutExpired(["up"], 15)):
            self.query_error = problem
            with self.subTest(problem=problem), self.assertRaisesRegex(RigError, "cam: runtime-images query failed"):
                self.inventory()
        self.assertEqual(self.docker_calls("pull"), [])

    def test_artifact_queries_share_bounded_invocation_and_keep_yaml_list_contract(self):
        self.desc.verbs["images"] = "images"
        with patch("rig_cli.artifact_images.subprocess.run", side_effect=subprocess.TimeoutExpired([], 15)):
            with self.assertRaisesRegex(RigError, "cam: images query failed"):
                artifact_images.run_image_query(self.rows[0], self.desc, {})
        self.assertEqual(artifact_images.parse_image_refs("- repo:tag\n- repo2:tag\n", instance="cam"),
                         ["repo:tag", "repo2:tag"])
        for output in ("images: [repo:tag]", "[]", "- '{{unresolved}}'", "- [nested]"):
            with self.subTest(output=output), self.assertRaisesRegex(RigError, "YAML list"):
                artifact_images.parse_image_refs(output, instance="cam")

    def test_catalog_localization_native_child_and_selected_only_deduplication(self):
        self.catalog()
        self.manifest.build_catalog["build-host:5000/unselected:v1"] = {
            "image": "unselected", "digest": OTHER_ID, "architectures": ["amd64"], "children": {}}
        self.second_instance(ref="localhost:5000/cam@" + INDEX)
        result = self.inventory()
        self.assertEqual({value["source"] for value in result["images"].values()},
                         {"localhost:5000/cam@" + CHILD})
        self.local["localhost:5000/cam@" + CHILD] = image_info()
        report = images.inspect_images(result)
        self.assertTrue(report["ready"], report)
        self.assertEqual(len(self.docker_calls("image")), 1)
        self.assertEqual(set(report["pins"]), {"cam", "cam2"})
        self.assertEqual(report["pins"]["cam"]["images"], {"localhost:5000/cam:v1": IMAGE_ID})

    def test_uncovered_actual_override_and_wrong_companion_cpu_runtime_fail(self):
        self.catalog()
        self.responses["cam"]["images"] = ["custom-player:override"]
        self.responses["cam"]["roles"] = {}
        with self.assertRaisesRegex(RigError, "custom-player:override.*companion"):
            self.inventory()
        self.responses["cam"]["images"] = ["localhost:5000/cam:v1"]
        row = next(iter(self.manifest.build_catalog.values()))
        for field, value, message in (("architectures", ["amd64"], "no arm64"),
                                       ("children", {}, "child digest"),
                                       ("variant", "jp7", "runtime jp7")):
            previous = row[field]
            row[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(RigError, message):
                self.inventory()
            row[field] = previous
        self.assertEqual(self.docker_calls("pull"), [])

    def test_cold_cache_proposal_is_read_only_and_acquisition_pulls_once(self):
        self.second_instance()
        inventory = self.inventory()
        report = images.inspect_images(inventory)
        self.assertFalse(report["ready"])
        self.assertEqual(report["proposed_pulls"], ["localhost:5000/cam:v1"])
        self.assertEqual(self.docker_calls("pull"), [])
        acquired = images.acquire_images(inventory, inspection=report)
        self.assertTrue(acquired["ready"], acquired)
        self.assertEqual(self.docker_calls("pull"), [
            ["docker", "pull", "--platform", "linux/arm64", "localhost:5000/cam:v1"]])
        self.assertEqual(len(self.docker_calls("image")), 2)  # once before and once after pulling
        self.assertEqual(len(self.queries), 2)  # acquisition never reruns launcher queries
        pull_options = next(options for argv, options in self.calls if argv[:2] == ["docker", "pull"])
        self.assertIs(pull_options["stdout"], sys.stderr)
        self.assertEqual(pull_options["timeout"], images.PULL_TIMEOUT)

    def test_warm_native_cache_does_not_pull_or_reinspect_under_missing(self):
        inventory = self.inventory()
        self.local["localhost:5000/cam:v1"] = image_info()
        report = images.inspect_images(inventory)
        self.assertTrue(report["ready"])
        acquired = images.acquire_images(inventory, inspection=report)
        self.assertTrue(acquired["ready"])
        self.assertEqual(self.docker_calls("pull"), [])
        self.assertEqual(len(self.docker_calls("image")), 1)

    def test_foreign_cache_member_pulls_native_once_and_revalidates(self):
        inventory = self.inventory()
        self.local["localhost:5000/cam:v1"] = image_info(architecture="amd64")
        report = images.inspect_images(inventory)
        self.assertEqual(report["missing"], ["localhost:5000/cam:v1"])
        self.assertTrue(images.acquire_images(inventory, inspection=report)["ready"])
        self.assertEqual(len(self.docker_calls("pull")), 1)
        self.local["localhost:5000/cam:v1"] = image_info(architecture="amd64")
        self.pull_info = image_info(architecture="amd64")
        rejected = images.acquire_images(inventory)
        self.assertFalse(rejected["ready"])
        self.assertTrue(any("native linux/arm64" in error for error in rejected["errors"]))
        self.assertEqual(len(self.docker_calls("pull")), 2)  # no retry loop on an incompatible pull

    def test_capability_labels_follow_actual_roles_and_missing_never_refreshes_them(self):
        requirements = {"cam": {"cam-core": {LABEL: "1"}, "cam-dev": {LABEL: "1"}}}
        inventory = self.inventory(required_labels=requirements)
        self.local["localhost:5000/cam:v1"] = image_info(labels={LABEL: "0"})
        report = images.acquire_images(inventory)
        self.assertFalse(report["ready"])
        self.assertTrue(any(LABEL in error for error in report["errors"]))
        self.assertEqual(self.docker_calls("pull"), [])
        self.assertEqual(report["pins"], {})

    def test_missing_reader_role_and_conflicting_role_requirements_fail(self):
        with self.assertRaisesRegex(RigError, "active replay reader role"):
            self.inventory(required_labels={"cam": {"unreported": {LABEL: "1"}}})
        self.second_instance()
        with self.assertRaisesRegex(RigError, "disagree"):
            self.inventory(required_labels={"cam": {"cam-dev": {LABEL: "1"}},
                                            "cam2": {"cam-dev": {LABEL: "2"}}})

    def test_always_defers_cached_labels_until_refresh_and_remains_unready(self):
        inventory = self.inventory(required_labels={"cam": {"cam-dev": {LABEL: "1"}}})
        self.local["localhost:5000/cam:v1"] = image_info(labels={LABEL: "0"})
        report = images.inspect_images(inventory, policy="always")
        self.assertFalse(report["ready"])
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["validation_errors"])
        self.assertEqual(report["pins"], {})
        self.pull_info = image_info(labels={LABEL: "1"})
        acquired = images.acquire_images(inventory, policy="always", inspection=report)
        self.assertTrue(acquired["ready"], acquired)
        warm = images.inspect_images(inventory, policy="always")
        self.assertFalse(warm["ready"])
        self.assertEqual(warm["proposed_pulls"], ["localhost:5000/cam:v1"])

    def test_never_and_offline_missing_are_actionable_without_network(self):
        inventory = self.inventory()
        for kwargs, message in (({"policy": "never"}, "--pull never"),
                                ({"offline": True}, "offline")):
            with self.subTest(kwargs=kwargs):
                report = images.acquire_images(inventory, **kwargs)
                self.assertFalse(report["ready"])
                self.assertEqual(report["proposed_pulls"], [])
                self.assertTrue(any(message in error and "localhost:5000/cam:v1" in error
                                    for error in report["errors"]))
        with self.assertRaisesRegex(RigError, "conflicts"):
            images.inspect_images(inventory, policy="always", offline=True)
        self.assertEqual(self.docker_calls("pull"), [])

    def test_docker_errors_and_malformed_inspection_never_authorize_pulls(self):
        inventory = self.inventory()
        for problem in ("permission denied", "cannot connect to Docker", "unrecognized image response",
                        subprocess.TimeoutExpired(["docker"], 15)):
            self.inspect_error = problem
            with self.subTest(problem=problem):
                report = images.acquire_images(inventory, policy="always")
                self.assertFalse(report["ready"])
                self.assertTrue(report["errors"])
                self.assertEqual(report["missing"], [])
        self.inspect_error = None
        for malformed in ([], {}, dict(image_info(), Config="invalid")):
            self.local["localhost:5000/cam:v1"] = malformed
            with self.subTest(malformed=malformed):
                report = images.acquire_images(inventory)
                self.assertEqual(report["images"]["localhost:5000/cam:v1"]["status"], "unavailable")
        self.assertEqual(self.docker_calls("pull"), [])

    def bundle(self):
        alias = "rig-bundle/camera:" + IMAGE_ID[7:]
        return {"refs": {"localhost:5000/cam:v1": alias}, "image_ids": {alias: IMAGE_ID},
                "accepted_ids": {alias: [IMAGE_ID, OTHER_ID]}, "target": {"runtime": "dev", "arch": "arm64"}}

    def test_bundle_uses_recorded_alias_and_store_specific_accepted_id(self):
        bundle = self.bundle()
        inventory = self.inventory(bundle=bundle)
        alias = bundle["refs"]["localhost:5000/cam:v1"]
        self.local[alias] = image_info(image_id=OTHER_ID)
        report = images.acquire_images(inventory)
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["pins"]["cam"]["images"], {"localhost:5000/cam:v1": OTHER_ID})
        self.assertEqual(self.docker_calls("pull"), [])
        with self.assertRaisesRegex(RigError, "conflicts"):
            images.inspect_images(inventory, policy="always")

    def test_bundle_missing_wrong_identity_or_unbundled_companion_never_pulls(self):
        bundle = self.bundle()
        inventory = self.inventory(bundle=bundle)
        report = images.acquire_images(inventory)
        self.assertTrue(any("load the verified bundle" in error for error in report["errors"]))
        alias = bundle["refs"]["localhost:5000/cam:v1"]
        self.local[alias] = image_info(image_id=CHILD)
        report = images.acquire_images(inventory)
        self.assertTrue(any("identity differs" in error for error in report["errors"]))
        self.second_instance(ref="extra-player:v1")
        with self.assertRaisesRegex(RigError, "unbundled image extra-player:v1"):
            self.inventory(bundle=bundle)
        self.assertEqual(self.docker_calls("pull"), [])

    def test_invalid_bundle_and_wrong_architecture_fail_before_any_pull(self):
        for bundle in ({}, {**self.bundle(), "target": {"arch": "amd64"}},
                       {**self.bundle(), "accepted_ids": []}):
            with self.subTest(bundle=bundle), self.assertRaises(RigError):
                self.inventory(bundle=bundle)
        self.assertEqual(self.docker_calls("pull"), [])

    def test_ready_instance_pins_survive_other_missing_instances(self):
        self.second_instance(ref="other:v1")
        inventory = self.inventory()
        self.local["localhost:5000/cam:v1"] = image_info()
        report = images.inspect_images(inventory)
        self.assertFalse(report["ready"])
        self.assertEqual(set(report["pins"]), {"cam"})
        self.assertEqual(report["proposed_pulls"], ["other:v1"])

    def test_changed_inventory_cannot_reuse_previous_inspection(self):
        inventory = self.inventory()
        report = images.inspect_images(inventory)
        inventory["images"]["localhost:5000/cam:v1"]["source"] = "changed:ref"
        with self.assertRaisesRegex(RigError, "changed since inspection"):
            images.acquire_images(inventory, inspection=report)
        self.assertEqual(self.docker_calls("pull"), [])

    def test_pull_failure_timeout_and_cancellation_never_issue_pins(self):
        inventory = self.inventory()
        for failure in (9, subprocess.TimeoutExpired(["docker", "pull"], images.PULL_TIMEOUT)):
            self.pull_error = failure
            with self.subTest(failure=failure), self.assertRaisesRegex(RigError, "could not pull"):
                images.acquire_images(inventory)
        self.pull_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            images.acquire_images(inventory)

    def test_immutable_local_ids_and_orphan_bundle_aliases_are_never_registry_pulls(self):
        self.responses["cam"] = {"version": 1, "images": [IMAGE_ID]}
        inventory = self.inventory()
        report = images.acquire_images(inventory)
        self.assertTrue(any("local image ID is missing" in error for error in report["errors"]))
        self.local[IMAGE_ID] = image_info()
        self.assertTrue(images.inspect_images(inventory, policy="always")["ready"])
        self.responses["cam"] = {"version": 1, "images": ["rig-bundle/camera:hash"]}
        with self.assertRaisesRegex(RigError, "requires its recorded bundle metadata"):
            self.inventory()
        self.assertEqual(self.docker_calls("pull"), [])


if __name__ == "__main__":
    unittest.main()
