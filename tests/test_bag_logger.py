"""bag-logger templates — config -> record argv. Run: python3 tests/test_bag_logger.py

Imports each template's bag_cmd.py directly (the testable arg-builder), so the config→command mapping is
covered without docker or a ROS graph. The launcher↔compose plumbing is covered by `rig certify` in CI.
"""
import importlib.util
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(rel):
    spec = importlib.util.spec_from_file_location(rel.replace("/", "_"), ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ros2 = _load("templates/ros2-bag-logger/tools/bag_cmd.py")
ros1 = _load("templates/ros1-bag-logger/tools/bag_cmd.py")


# --- ROS 2 ---------------------------------------------------------------------------------------------

def test_ros2_exclude_default_drops_images():
    _, sub, args, _ = ros2.build_args({"name": "bl", "record": {"mode": "exclude"}})
    assert sub == "bags"
    assert "--all" in args
    i = args.index("--exclude-regex")
    assert "image_raw" in args[i + 1]


def test_ros2_exclude_combines_topics_and_images():
    _, _, args, _ = ros2.build_args(
        {"record": {"mode": "exclude", "topics": ["/noisy/.*"], "exclude_images": True}})
    rx = args[args.index("--exclude-regex") + 1]
    assert "/noisy/.*" in rx and "image_raw" in rx and "|" in rx


def test_ros2_allow_is_positional_and_ignores_images():
    _, _, args, warns = ros2.build_args(
        {"record": {"mode": "allow", "topics": ["/gnss/fix", "/imu/data"]}})
    assert args[-2:] == ["/gnss/fix", "/imu/data"]
    assert "--all" not in args and "--exclude-regex" not in args
    assert any("exclude_images ignored" in w for w in warns)


def test_ros2_all_keeps_images_when_disabled():
    _, _, args, _ = ros2.build_args({"record": {"mode": "all", "exclude_images": False}})
    assert "--all" in args and "--exclude-regex" not in args


def test_ros2_storage_compression_and_split():
    _, _, args, _ = ros2.build_args({"output": {
        "storage": "mcap", "compression": "zstd", "split_duration_s": 300, "max_size_mb": 500}})
    assert args[:2] == ["-s", "mcap"]
    assert "--compression-mode" in args and args[args.index("--compression-format") + 1] == "zstd"
    assert args[args.index("--max-bag-duration") + 1] == "300"
    assert args[args.index("--max-bag-size") + 1] == str(500 * 1024 * 1024)  # MB -> bytes


def test_ros2_allow_without_topics_errors():
    try:
        ros2.build_args({"record": {"mode": "allow", "topics": []}})
        raise AssertionError("expected SystemExit")
    except SystemExit:
        pass


def test_ros2_render_is_restart_safe_and_sources_ros():
    repo = pathlib.Path(tempfile.mkdtemp())
    name, path = ros2.render({"name": "bl", "record": {"mode": "exclude"}}, repo)
    body = pathlib.Path(path).read_text()
    assert name == "bl"
    assert "date -u" in body and "-o " in body          # runtime-stamped output dir (no clobber on restart)
    assert "/opt/ros/$ROS_DISTRO/setup.bash" in body     # sources ROS
    assert "exec ros2 bag record" in body


def test_ros2_services_key_warns_not_implemented():
    _, _, _, warns = ros2.build_args({"record": {"mode": "all"}, "services": ["/srv"]})
    assert any("service calls is not implemented" in w for w in warns)


# --- ROS 1 ---------------------------------------------------------------------------------------------

def test_ros1_exclude_default_drops_images():
    _, _, args, _ = ros1.build_args({"record": {"mode": "exclude"}})
    assert "-a" in args and "image_raw" in args[args.index("-x") + 1]


def test_ros1_allow_positional():
    _, _, args, _ = ros1.build_args({"record": {"mode": "allow", "topics": ["/gps/fix"]}})
    assert "/gps/fix" in args and "-a" not in args


def test_ros1_compression_and_split_flags():
    _, _, args, _ = ros1.build_args({"output": {"compression": "lz4", "split_duration_s": 0, "max_size_mb": 1024}})
    assert "--lz4" in args and "--split" in args and "--size=1024" in args
    assert not any(a.startswith("--duration") for a in args)  # 0 -> no duration split


def test_ros1_bad_compression_warns():
    _, _, args, warns = ros1.build_args({"output": {"compression": "zstd"}})
    assert "--lz4" not in args and any("lz4|bz2" in w for w in warns)


def test_ros1_render_uses_rosbag_prefix_native_stamp():
    repo = pathlib.Path(tempfile.mkdtemp())
    name, path = ros1.render({"name": "bl", "record": {"mode": "all", "exclude_images": False}}, repo)
    body = pathlib.Path(path).read_text()
    assert "exec rosbag record" in body and '-o "$base/bl"' in body  # rosbag appends _<date>.bag itself


if __name__ == "__main__":
    failures = 0
    for nm, fn in sorted(globals().items()):
        if nm.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", nm)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", nm, "->", exc)
    sys.exit(1 if failures else 0)
