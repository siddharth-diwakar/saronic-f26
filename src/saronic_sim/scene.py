"""Procedural harbor assets and boat-mounted cameras."""
from pathlib import Path

# Initial lighting and overview camera; runtime commands report changes relative to these.
SKY_INTENSITY = 700.0
SUN_INTENSITY = 2500.0
SUN_ROTATION = [-35.0, -25.0, 0.0]
OVERVIEW_POSITION = [-10.0, -13.0, 10.0]
OVERVIEW_TARGET = [5.0, 3.0, 0.0]
OVERVIEW_FOCAL_LENGTH = 50.0  # USD camera default


def camera_transform(position, target):
    """Camera-to-parent transform aiming from position at target with +Z up."""
    from pxr import Gf
    return Gf.Matrix4d().SetLookAt(Gf.Vec3d(*position), Gf.Vec3d(*target), Gf.Vec3d(0, 0, 1)).GetInverse()


def build_harbor(config):
    """The static harbor: physics scene, lights, water, shore, dock, pilings, buoys, overview camera."""
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
    dome.CreateIntensityAttr(SKY_INTENSITY)
    dome.CreateColorAttr(Gf.Vec3f(.7, .82, 1))
    sun = UsdLux.DistantLight.Define(stage, "/World/Sun")
    sun.CreateIntensityAttr(SUN_INTENSITY)
    sun.CreateAngleAttr(1)
    UsdGeom.Xformable(sun).AddRotateXYZOp().Set(Gf.Vec3f(*SUN_ROTATION))

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

    overview = UsdGeom.Camera.Define(stage, "/World/Overview")
    overview.AddTransformOp().Set(camera_transform(OVERVIEW_POSITION, OVERVIEW_TARGET))
    overview.CreateFocalLengthAttr(OVERVIEW_FOCAL_LENGTH)
    global _MATERIALS
    _MATERIALS = mats
    return stage


_MATERIALS = {}
ENTITIES_ROOT = "/World/Entities"
CAMERAS_ROOT = "/World/Cameras"


def entity_path(entity_id):
    return f"{ENTITIES_ROOT}/{entity_id}"


def camera_path(camera):
    parent = camera["parent_entity_id"]
    return f"{entity_path(parent)}/Cameras/{camera['id']}" if parent else f"{CAMERAS_ROOT}/{camera['id']}"


def remove_world(stage):
    for root in (ENTITIES_ROOT, CAMERAS_ROOT):
        if stage.GetPrimAtPath(root):
            stage.RemovePrim(root)


def build_world(stage, world, catalog, boat_usd=None):
    """Entities and cameras for a validated world (worldconfig.parse_world).

    Returns {camera id: prim path}. Every entity is a rigid body at /World/Entities/<id>;
    external entities are kinematic, so physics never moves them.
    """
    from pxr import Gf, Usd, UsdGeom, UsdPhysics, UsdShade
    mats = _MATERIALS
    UsdGeom.Xform.Define(stage, ENTITIES_ROOT)
    UsdGeom.Xform.Define(stage, CAMERAS_ROOT)

    def shape(define, path, mat, collision, **attrs):
        prim = define(stage, path)
        for name, value in attrs.items():
            getattr(prim, f"Create{name}Attr")(value)
        UsdShade.MaterialBindingAPI.Apply(prim.GetPrim()).Bind(mats[mat])
        if collision:
            UsdPhysics.CollisionAPI.Apply(prim.GetPrim())
        return prim

    def box(path, dimensions, position, mat, collision=False):
        cube = shape(UsdGeom.Cube.Define, path, mat, collision, Size=1)
        cube.AddTranslateOp().Set(Gf.Vec3d(*position))
        cube.AddScaleOp().Set(Gf.Vec3f(*dimensions))
        return cube

    for entity in world["entities"]:
        asset = catalog[entity["asset_id"]]
        path = entity_path(entity["id"])
        root = UsdGeom.Xform.Define(stage, path)
        pose = entity["pose"]
        root.AddTranslateOp().Set(Gf.Vec3d(*pose["position_m"]))
        x, y, z, w = pose["orientation_xyzw"]
        root.AddOrientOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Quatd(w, x, y, z))
        rigid = UsdPhysics.RigidBodyAPI.Apply(root.GetPrim())
        rigid.CreateKinematicEnabledAttr(entity["motion_type"] == "external")
        mass_api = UsdPhysics.MassAPI.Apply(root.GetPrim())
        if asset["kind"] == "boat":
            length, width, height, mass = (asset[k] for k in ("length", "width", "height", "mass"))
            mass_api.CreateMassAttr(mass)
            mass_api.CreateCenterOfMassAttr(Gf.Vec3f(0, 0, 0))
            mass_api.CreateDiagonalInertiaAttr(Gf.Vec3f(*[mass*v/12 for v in (width**2+height**2, length**2+height**2, length**2+width**2)]))
            collision = box(path+"/CollisionHull", (length, width, height), (0, 0, 0), "hull", True)
            collision.CreateVisibilityAttr("invisible")
            if boat_usd:
                model = UsdGeom.Xform.Define(stage, path+"/Model")
                model.GetPrim().GetReferences().AddReference(str(Path(boat_usd).resolve()))
                # Reject nested rigid bodies rather than silently applying two simulations.
                for prim in Usd.PrimRange(model.GetPrim()):
                    if prim.HasAPI(UsdPhysics.RigidBodyAPI):
                        raise ValueError("Custom boat model must be visual-only; remove its RigidBodyAPI")
            else:
                box(path+"/Hull", (length, width, height), (0, 0, 0), "hull")
                box(path+"/Deck", (length*.93, width*.93, .08), (0, 0, height/2), "deck")
                box(path+"/Cabin", (length*.3, width*.65, .65), (-length*.15, 0, height/2+.36), "deck")
                box(path+"/Windshield", (.025, width*.55, .35), (0.02, 0, height/2+.5), "glass")
                box(path+"/Mast", (.07, .07, 1.5), (-length*.15, 0, height/2+1), "hull")
        elif asset["kind"] == "buoy":
            mass_api.CreateMassAttr(50.0)
            shape(UsdGeom.Cylinder.Define, path+"/Body", asset["color"], True, Radius=asset["radius"], Height=asset["height"])
        else:
            mass_api.CreateMassAttr(100.0)
            box(path+"/Body", asset["size"], (0, 0, 0), asset["color"], True)
    ops = UsdOps(stage)
    paths = {}
    for camera in world["cameras"]:
        path = camera_path(camera)
        if camera["parent_entity_id"]:
            UsdGeom.Xform.Define(stage, path.rsplit("/", 1)[0])
        usd_camera = UsdGeom.Camera.Define(stage, path)
        usd_camera.AddTransformOp()
        ops.update_camera(path, camera)
        paths[camera["id"]] = path
    return paths


class UsdOps:
    """Stage edits the world makes on the simulation thread."""

    def __init__(self, stage):
        self.stage = stage

    def update_camera(self, path, camera):
        from pxr import Gf, UsdGeom
        import worldconfig
        usd_camera = UsdGeom.Camera(self.stage.GetPrimAtPath(path))
        usd_camera.GetOrderedXformOps()[0].Set(Gf.Matrix4d(worldconfig.usd_camera_matrix(camera["pose"])))
        optics = worldconfig.usd_optics(camera)
        usd_camera.CreateFocalLengthAttr().Set(optics["focal_length"])
        usd_camera.CreateHorizontalApertureAttr().Set(optics["horizontal_aperture"])
        usd_camera.CreateVerticalApertureAttr().Set(optics["vertical_aperture"])
        usd_camera.CreateHorizontalApertureOffsetAttr().Set(optics["horizontal_aperture_offset"])
        usd_camera.CreateVerticalApertureOffsetAttr().Set(optics["vertical_aperture_offset"])
        usd_camera.CreateClippingRangeAttr().Set(Gf.Vec2f(*optics["clipping_range"]))

    def update_lights(self, environment):
        from pxr import Gf, UsdGeom, UsdLux
        sun = self.stage.GetPrimAtPath("/World/Sun")
        UsdLux.DistantLight(sun).GetIntensityAttr().Set(environment["sun_intensity"])
        UsdGeom.Xformable(sun).GetOrderedXformOps()[0].Set(Gf.Vec3f(*environment["sun_rotation"]))
        UsdLux.DomeLight(self.stage.GetPrimAtPath("/World/Sky")).GetIntensityAttr().Set(environment["sky_intensity"])

    def update_viewer(self, spec, aim):
        from pxr import UsdGeom
        camera = UsdGeom.Camera(self.stage.GetPrimAtPath("/World/Overview"))
        if aim:
            camera.GetOrderedXformOps()[0].Set(camera_transform(spec["position"], spec["target"]))
        camera.GetFocalLengthAttr().Set(spec["focal_length"])
