"""Procedural harbor assets and boat-mounted cameras."""
from pathlib import Path


def build_scene(config, boat_usd=None):
    import omni.usd
    from pxr import Gf, Sdf, UsdGeom, UsdLux, UsdPhysics, UsdShade, PhysxSchema
    stage = omni.usd.get_context().get_stage()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    physics = UsdPhysics.Scene.Define(stage, "/World/Physics")
    physics.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
    physics.CreateGravityMagnitudeAttr(9.81)
    physics_api = PhysxSchema.PhysxSceneAPI.Apply(physics.GetPrim())
    physics_api.CreateTimeStepsPerSecondAttr(config["simulation"]["physics_hz"])
    physics_api.CreateEnableGPUDynamicsAttr(False)
    dome = UsdLux.DomeLight.Define(stage, "/World/Sky")
    dome.CreateIntensityAttr(700)
    dome.CreateColorAttr(Gf.Vec3f(.7, .82, 1))
    sun = UsdLux.DistantLight.Define(stage, "/World/Sun")
    sun.CreateIntensityAttr(2500)
    sun.CreateAngleAttr(1)
    UsdGeom.Xformable(sun).AddRotateXYZOp().Set(Gf.Vec3f(-35, -25, 0))

    def material(name, color, roughness=.5, metallic=0):
        mat = UsdShade.Material.Define(stage, "/World/Materials/"+name)
        shader = UsdShade.Shader.Define(stage, str(mat.GetPath())+"/Shader")
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color))
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(roughness)
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metallic)
        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
        return mat

    mats = {
        "water": material("Water", (.025, .19, .24), .18, .25),
        "hull": material("Hull", (.13, .2, .24), .3, .4),
        "deck": material("Deck", (.73, .76, .74)),
        "glass": material("Glass", (.04, .12, .18), .12, .3),
        "dock": material("Dock", (.38, .26, .16)),
        "land": material("Land", (.24, .32, .15)),
        "orange": material("Orange", (1, .22, .02)),
        "red": material("Red", (.8, .03, .02)),
        "green": material("Green", (.04, .5, .13)),
    }
    def box(path, dimensions, position, mat, collision=False):
        cube = UsdGeom.Cube.Define(stage, path)
        cube.CreateSizeAttr(1)
        cube.AddTranslateOp().Set(Gf.Vec3d(*position))
        cube.AddScaleOp().Set(Gf.Vec3f(*dimensions))
        UsdShade.MaterialBindingAPI.Apply(cube.GetPrim()).Bind(mats[mat])
        if collision:
            UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
        return cube

    level = config["water"]["level"]
    # A surface without collision: flotation comes from displaced-water forces.
    water = UsdGeom.Mesh.Define(stage, "/World/Water")
    water.CreatePointsAttr([(-100, -100, level), (100, -100, level), (100, 100, level), (-100, 100, level)])
    water.CreateFaceVertexCountsAttr([4])
    water.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
    water.CreateSubdivisionSchemeAttr("none")
    UsdShade.MaterialBindingAPI.Apply(water.GetPrim()).Bind(mats["water"])
    box("/World/Shore", (100, 12, 2), (0, 24, level-.5), "land", True)
    box("/World/Dock", (18, 3, .5), (1, 13, level+.5), "dock", True)
    for i, x in enumerate(range(-7, 11, 4)):
        box(f"/World/Pilings/Piling{i}", (.3, .3, 3), (x, 12, level), "dock", True)
    for i, (x, y, color) in enumerate([(10, -4, "red"), (10, 4, "green"), (24, -4, "red"), (24, 4, "green")]):
        buoy = UsdGeom.Cylinder.Define(stage, f"/World/Buoys/Buoy{i}")
        buoy.CreateRadiusAttr(.3)
        buoy.CreateHeightAttr(1.2)
        buoy.AddTranslateOp().Set(Gf.Vec3d(x, y, level+.3))
        UsdShade.MaterialBindingAPI.Apply(buoy.GetPrim()).Bind(mats[color])
        UsdPhysics.CollisionAPI.Apply(buoy.GetPrim())

    b = config["boat"]
    length, width, height = (b[k] for k in ("length", "width", "height"))
    boat = UsdGeom.Xform.Define(stage, "/World/Boat")
    boat.AddTranslateOp().Set(Gf.Vec3d(*b["position"]))
    boat.AddRotateZOp().Set(b["heading"])
    UsdPhysics.RigidBodyAPI.Apply(boat.GetPrim())
    mass = UsdPhysics.MassAPI.Apply(boat.GetPrim())
    mass.CreateMassAttr(b["mass"])
    mass.CreateCenterOfMassAttr(Gf.Vec3f(0, 0, 0))
    mass.CreateDiagonalInertiaAttr(Gf.Vec3f(*[b["mass"]*v/12 for v in (width**2+height**2, length**2+height**2, length**2+width**2)]))
    collision = box("/World/Boat/CollisionHull", (length, width, height), (0, 0, 0), "hull", True)
    collision.CreateVisibilityAttr("invisible")
    if boat_usd:
        model = UsdGeom.Xform.Define(stage, "/World/Boat/Model")
        model.GetPrim().GetReferences().AddReference(str(Path(boat_usd).resolve()))
        # Reject nested rigid bodies rather than silently applying two simulations.
        from pxr import Usd
        for prim in Usd.PrimRange(model.GetPrim()):
            if prim.HasAPI(UsdPhysics.RigidBodyAPI):
                raise ValueError("Custom boat model must be visual-only; remove its RigidBodyAPI")
    else:
        box("/World/Boat/Hull", (length, width, height), (0, 0, 0), "hull")
        box("/World/Boat/Deck", (length*.93, width*.93, .08), (0, 0, height/2), "deck")
        box("/World/Boat/Cabin", (length*.3, width*.65, .65), (-length*.15, 0, height/2+.36), "deck")
        box("/World/Boat/Windshield", (.025, width*.55, .35), (0.02, 0, height/2+.5), "glass")
        box("/World/Boat/Mast", (.07, .07, 1.5), (-length*.15, 0, height/2+1), "hull")
    UsdGeom.Xform.Define(stage, "/World/Boat/Cameras")
    camera_paths = []
    for c in config["cameras"]:
        path = "/World/Boat/Cameras/"+c["name"]
        camera = UsdGeom.Camera.Define(stage, path)
        eye, target = Gf.Vec3d(*c["position"]), Gf.Vec3d(*c["target"])
        transform = Gf.Matrix4d().SetLookAt(eye, target, Gf.Vec3d(0, 0, 1)).GetInverse()
        camera.AddTransformOp().Set(transform)
        camera.CreateFocalLengthAttr(c["focal_length"])
        camera.CreateHorizontalApertureAttr(20.955)
        camera.CreateVerticalApertureAttr(20.955*c["resolution"][1]/c["resolution"][0])
        camera.CreateClippingRangeAttr(Gf.Vec2f(.05, 250))
        camera_paths.append(path)
    overview = UsdGeom.Camera.Define(stage, "/World/Overview")
    overview.AddTransformOp().Set(Gf.Matrix4d().SetLookAt(Gf.Vec3d(-10, -13, 10), Gf.Vec3d(5, 3, 0), Gf.Vec3d(0, 0, 1)).GetInverse())
    return stage, camera_paths
