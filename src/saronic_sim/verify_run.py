"""Check recorded GPU runs without starting another Isaac Sim application."""
import argparse
import csv
import json
from pathlib import Path
import numpy as np
from PIL import Image


def verify(folder, powered):
    folder = Path(folder)
    runs = sorted(folder.glob("run_*"))
    if not runs:
        raise RuntimeError(f"No run data in {folder}")
    run = runs[-1]
    config = json.loads((run / "scenario.json").read_text())
    with (run / "trajectory.csv").open() as file:
        rows = list(csv.DictReader(file))
    if len(rows) < 10 or float(rows[-1]["time"]) < 7:
        raise RuntimeError("Run did not reach the expected duration")
    states = np.array([[float(row[key]) for key in ("x", "y", "z", "qx", "qy", "qz", "qw", "vx", "vy", "vz", "wx", "wy", "wz")] for row in rows])
    if not np.isfinite(states).all():
        raise RuntimeError("Non-finite boat state")
    b, water = config["boat"], config["water"]
    equilibrium = water["level"]+b["height"]/2-b["mass"]/(water["density"]*b["length"]*b["width"])
    if np.max(np.abs(states[:, 2]-equilibrium)) > .15:
        raise RuntimeError("Boat did not remain near its expected flotation height")
    displacement = states[-1, :2] - np.asarray(b["position"][:2])
    if powered:
        if displacement[0] < 1 or displacement[1] < .1:
            raise RuntimeError(f"Expected forward and port motion; displacement={displacement}")
    elif np.linalg.norm(displacement) > .15:
        raise RuntimeError("Idle boat drifted in still water")
    for camera in config["cameras"]:
        images = sorted((run / "cameras" / camera["name"]).glob("*.png"))
        if len(images) < 10:
            raise RuntimeError(f"Missing frames for {camera['name']}")
        image = np.asarray(Image.open(images[-1]))
        width, height = camera["resolution"]
        if image.shape != (height, width, 3) or image.std() < 2:
            raise RuntimeError(f"Invalid or blank RGB from {camera['name']}")
        depth = np.load(images[-1].with_name(images[-1].stem+"_depth.npy"))
        if depth.shape != (height, width) or not np.any(np.isfinite(depth) & (depth > 0)):
            raise RuntimeError(f"Invalid depth from {camera['name']}")
    print(f"PASS: {'powered' if powered else 'idle'} flotation, motion, RGB, and depth in {run}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--powered", action="store_true")
    args = parser.parse_args()
    verify(args.folder, args.powered)
