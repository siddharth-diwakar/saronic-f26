"""P0 closed loop: an external boat model supplies each pose; the harbor renders what the cameras see.

Start the harbor in lockstep first (see README, "Gym-style stepping"), then:

    PYTHONPATH=src python3 examples/closed_loop.py --steps 100 --output output/closed_loop

DummyBoatModel stands in for Saronic's black box: it takes the camera images and
the current pose and returns the next pose. Replace it with the real model.
"""
import argparse
import csv
import math
from pathlib import Path

from saronic_env import HarborEnv

DT = 0.02  # Saronic's boat models predict 20 ms ahead.


def yaw_of(quaternion_xyzw):
    x, y, z, w = quaternion_xyzw
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


class DummyBoatModel:
    """Constant speed and turn rate; ignores the images. Same interface as the real black box."""

    def __init__(self, speed=3.0, turn_rate_degrees=6.0):
        self.speed = speed
        self.turn_rate = math.radians(turn_rate_degrees)

    def next_pose(self, images, position, orientation_xyzw, dt):
        yaw = yaw_of(orientation_xyzw) + self.turn_rate * dt
        x = position[0] + self.speed * math.cos(yaw) * dt
        y = position[1] + self.speed * math.sin(yaw) * dt
        return [x, y, position[2]], [0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8212")
    parser.add_argument("--steps", type=int, default=100)
    parser.add_argument("--output", type=Path, default=Path("output/closed_loop"))
    args = parser.parse_args()

    env = HarborEnv(args.url)
    model = DummyBoatModel()
    observation = env.reset()
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "poses.csv").open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["step", "time", "x", "y", "z", "qx", "qy", "qz", "qw"])
        for index in range(args.steps):
            boat = observation.state["boat"]
            images = {camera: observation.png(camera) for camera in observation.cameras}
            for camera, png in images.items():
                folder = args.output / camera
                folder.mkdir(exist_ok=True)
                (folder / f"{index:06d}.png").write_bytes(png)
            writer.writerow([index, observation.time, *boat["position"], *boat["orientation_xyzw"]])
            position, orientation = model.next_pose(images, boat["position"], boat["orientation_xyzw"], DT)
            # physics=False: the boat model owns motion; the harbor only places and renders.
            observation = env.step(pose={"position": position, "orientation_xyzw": orientation}, seconds=DT, physics=False)
    print(f"Closed loop finished: {args.steps} steps, frames and poses in {args.output}")


if __name__ == "__main__":
    main()
