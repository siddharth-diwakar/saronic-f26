"""Approximate calm-water forces in SI units, independent of Kit.

Eight vertical displacement columns provide buoyancy and restoring torque.
This is a low-speed box-hull approximation, not a calibrated vessel model.
"""
import numpy as np

GRAVITY = 9.81


def rotation_matrix(quaternion_xyzw):
    x, y, z, w = np.asarray(quaternion_xyzw, dtype=float)
    norm = np.linalg.norm([x, y, z, w])
    if norm < 1e-12:
        raise ValueError("Invalid zero quaternion")
    x, y, z, w = np.array([x, y, z, w]) / norm
    return np.array([
        [1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
        [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
        [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)],
    ])


def forces(config, position, quaternion_xyzw, velocity, throttle=0.0, steering=0.0):
    boat, water = config["boat"], config["water"]
    rotation = rotation_matrix(quaternion_xyzw)
    position, velocity = np.asarray(position), np.asarray(velocity)
    length, width, height = (boat[k] for k in ("length", "width", "height"))
    points = np.array([(x*length, y*width, 0) for x in (-.375, -.125, .125, .375) for y in (-.25, .25)])
    offsets = points @ rotation.T
    depth = np.clip(water["level"] - (position[2] + offsets[:, 2]) + height/2, 0, height)
    buoyancy = water["density"] * GRAVITY * length * width / len(points) * depth
    lift = np.column_stack((np.zeros(len(points)), np.zeros(len(points)), buoyancy))
    force = lift.sum(axis=0)
    torque = np.cross(offsets, lift).sum(axis=0)
    relative = rotation.T @ (velocity[:3] - np.asarray(water["current"]))
    drag = -np.asarray(boat["linear_drag"])*relative - np.asarray(boat["quadratic_drag"])*relative*np.abs(relative)
    force += rotation @ drag
    angular = rotation.T @ velocity[3:]
    torque += rotation @ (-np.asarray(boat["angular_drag"])*angular)
    # Differential thrust: positive steering turns toward +Y (port).
    for y, command in ((width*.35, throttle-steering), (-width*.35, throttle+steering)):
        thrust = rotation @ np.array([np.clip(command, -1, 1)*boat["max_thrust"], 0, 0])
        force += thrust
        torque += np.cross(rotation @ np.array([-length*.4, y, 0]), thrust)
    return force, torque
