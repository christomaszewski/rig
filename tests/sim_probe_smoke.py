"""Opt-in native ROS probe smoke; run with a sourced ROS Python, without Isaac.

The caller must provide an isolated ROS domain/network. Uses actual rclpy endpoints
and QoS, never a GPU or the user's simulator. Each fixture process is always reaped.
Example: source /opt/ros/jazzy/setup.bash; python3 tests/sim_probe_smoke.py
"""
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time


def publisher(mode):
    import rclpy
    from geometry_msgs.msg import Twist
    from sensor_msgs.msg import Image
    from rosgraph_msgs.msg import Clock
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
    rclpy.init()
    node = rclpy.create_node("rig_smoke_fixture")
    best = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT,
                      durability=DurabilityPolicy.VOLATILE)
    image_type = Twist if mode == "wrong_type" else Image
    image = node.create_publisher(image_type, "/rig_smoke/image", best)
    node.create_subscription(Twist, "/rig_smoke/cmd", lambda _: None, 10)
    if mode == "qos_mismatch":
        node.create_subscription(Image, "/rig_smoke/image", lambda _: None, 10)
    clock = node.create_publisher(Clock, "/clock", best) if mode != "missing_clock" else None
    competing_clock = node.create_publisher(Clock, "/clock", best) if mode == "duplicate_clock" else None
    started = time.monotonic()
    def tick():
        if clock:
            msg = Clock()
            ns = int((time.monotonic() - started) * 1e9)
            if mode == "paused_clock":
                ns = 0
            elif mode == "reset_clock":
                ns %= 500000000
            msg.clock.sec, msg.clock.nanosec = divmod(ns, 1000000000)
            clock.publish(msg)
            if competing_clock:
                competing_clock.publish(msg)
        image.publish(image_type())
    node.create_timer(0.05, tick)
    running = [True]
    signal.signal(signal.SIGTERM, lambda *_: running.__setitem__(0, False))
    try:
        while running[0] and rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


def smoke():
    source = (Path(__file__).resolve().parents[1] / "rig_cli/sim_probe.py").read_text()
    request = dict(ros=dict(distro=os.environ["ROS_DISTRO"],
                           rmw=os.environ["RMW_IMPLEMENTATION"],
                           domain_id=int(os.environ["ROS_DOMAIN_ID"])), timeout_s=4,
                   publishes=[dict(topic="/rig_smoke/image", type="sensor_msgs/msg/Image")],
                   subscribes=[dict(topic="/rig_smoke/cmd", type="geometry_msgs/msg/Twist")])
    results = []
    for mode, expected, code in (("healthy", True, None),
                                  ("missing_clock", False, "clock_authority"),
                                  ("duplicate_clock", False, "clock_authority"),
                                  ("paused_clock", False, "clock_stopped"),
                                  ("reset_clock", False, "clock_reset"),
                                  ("wrong_type", False, "topic_type"),
                                  ("qos_mismatch", False, "qos_incompatible")):
        fixture = subprocess.Popen([sys.executable, __file__, "--publisher", mode],
                                   stdout=subprocess.DEVNULL)
        try:
            time.sleep(1)
            if fixture.poll() is not None:
                raise RuntimeError(f"ROS fixture exited: {fixture.returncode}")
            probe = subprocess.run([sys.executable, "-c", source], input=json.dumps(request),
                                   capture_output=True, text=True, timeout=20)
            result = json.loads(probe.stdout)
            assert result["ready"] is expected, result
            assert probe.returncode == (0 if expected else 1), probe.stderr
            if code:
                assert any(i["code"] == code for i in result["issues"]), result
            results.append(dict(scenario=mode, result=result))
        finally:
            fixture.terminate()
            try:
                fixture.wait(timeout=5)
            except subprocess.TimeoutExpired:
                fixture.kill()
                fixture.wait(timeout=5)
    print(json.dumps(dict(ros=request["ros"], scenarios=results), indent=2))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--publisher"]:
        publisher(sys.argv[2])
    else:
        smoke()
