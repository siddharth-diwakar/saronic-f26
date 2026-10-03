"""Create a small Isaac Sim scene from a command-line deployment request."""

import argparse
import math
import os
from pathlib import Path


def finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("coordinates and angles must be finite")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create an Isaac Sim boat scene")
    subcommands = parser.add_subparsers(dest="command", required=True)
    deploy = subcommands.add_parser("deploy", help="Place an asset in a new scene")
    deploy.add_argument("asset_type", choices=["boat"])
    deploy.add_argument("--x", type=finite_float, required=True, help="World X in meters")
    deploy.add_argument("--y", type=finite_float, required=True, help="World Y in meters")
    deploy.add_argument("--z", type=finite_float, default=0.25, help="Hull center height in meters")
    deploy.add_argument("--heading", type=finite_float, default=0.0, help="Yaw in degrees about +Z")
    deploy.add_argument("--boat-usd", type=Path, help="Optional local USD boat model")
    deploy.add_argument(
        "--output", type=Path, default=Path("/workspace/output/boat_scene.usd"),
        help="USD scene output path",
    )
    return parser.parse_args()


def add_box(stage, path, scale, color, offset=(0.0, 0.0, 0.0)):
    from pxr import Gf, UsdGeom

    box = UsdGeom.Cube.Define(stage, path)
    box.CreateSizeAttr(1.0)
    box.AddTranslateOp().Set(Gf.Vec3d(*offset))
    box.AddScaleOp().Set(Gf.Vec3f(*scale))
    box.CreateDisplayColorAttr([Gf.Vec3f(*color)])


def create_scene(args: argparse.Namespace) -> None:
    output = args.output.resolve()
    if output.suffix.lower() not in {".usd", ".usda", ".usdc"}:
        raise ValueError("Output must be a USD file (.usd, .usda, or .usdc)")
    if args.boat_usd:
        model_path = args.boat_usd.resolve()
        if not model_path.is_file() or model_path.suffix.lower() not in {".usd", ".usda", ".usdc"}:
            raise ValueError(f"Boat USD asset not found: {model_path}")

    # Kit-dependent imports must follow SimulationApp construction.
    from isaacsim import SimulationApp

    app = SimulationApp({"headless": True})
    try:
        import omni.usd
        from pxr import Gf, UsdGeom

        stage = omni.usd.get_context().get_stage()
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(stage, 1.0)
        world = UsdGeom.Xform.Define(stage, "/World")
        stage.SetDefaultPrim(world.GetPrim())

        # The water is only a visual reference. Buoyancy and dynamics come later.
        add_box(stage, "/World/Water", (200.0, 200.0, 0.02), (0.1, 0.35, 0.65), (0.0, 0.0, -0.01))

        boat = UsdGeom.Xform.Define(stage, "/World/Boat")
        boat.AddTranslateOp().Set(Gf.Vec3d(args.x, args.y, args.z))
        boat.AddRotateZOp().Set(args.heading)

        if args.boat_usd:
            model = UsdGeom.Xform.Define(stage, "/World/Boat/Model")
            model.GetPrim().GetReferences().AddReference(os.path.relpath(model_path, output.parent))
        else:
            add_box(stage, "/World/Boat/Hull", (3.0, 1.2, 0.5), (0.85, 0.25, 0.12))
            add_box(stage, "/World/Boat/Cabin", (1.0, 0.9, 0.6), (0.9, 0.9, 0.85), (-0.3, 0.0, 0.55))

        output.parent.mkdir(parents=True, exist_ok=True)
        if not stage.GetRootLayer().Export(str(output)):
            raise RuntimeError(f"Failed to save scene: {output}")
        print(f"Saved {output} with boat at ({args.x}, {args.y}, {args.z}) m, heading {args.heading}°")
    finally:
        app.close()


if __name__ == "__main__":
    create_scene(parse_args())
