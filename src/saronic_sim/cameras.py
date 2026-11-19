"""Read RGB and ideal geometric depth from independent render products."""
from pathlib import Path

ANNOTATORS = {"rgb": "rgb", "depth": "distance_to_image_plane"}


class CameraRecorder:
    def __init__(self, cameras, paths, output):
        """cameras: world camera configs; paths: {camera id: prim path}. Attaches only configured channels."""
        import omni.replicator.core as rep
        self.products, self.annotators = [], {}
        self.output = Path(output)
        for camera in cameras:
            product = rep.create.render_product(paths[camera["id"]], tuple(camera["resolution"]))
            self.products.append(product)
            self.annotators[camera["id"]] = {}
            for channel in camera["channels"]:
                annotator = rep.AnnotatorRegistry.get_annotator(ANNOTATORS[channel])
                annotator.attach([product])
                self.annotators[camera["id"]][channel] = annotator

    def read(self, plan=None):
        """{camera id: {channel: array}}: RGB uint8 HxWx3; depth float32 HxW in meters.

        plan is [(camera, channels)] from World.capture_plan; None reads everything.
        """
        wanted = {camera_id: list(channels) for camera_id, channels in self.annotators.items()} if plan is None else {c["id"]: channels for c, channels in plan}
        frames = {}
        for camera_id, channels in wanted.items():
            frames[camera_id] = {}
            for channel in channels:
                data = self.annotators[camera_id][channel].get_data()
                if data.size == 0:
                    raise RuntimeError(f"Camera {camera_id} has no rendered {channel} data")
                frames[camera_id][channel] = data[..., :3] if channel == "rgb" else data
        return frames

    def capture(self, frame):
        import numpy as np
        from PIL import Image
        for camera_id, channels in self.read().items():
            folder = self.output / "cameras" / camera_id
            folder.mkdir(parents=True, exist_ok=True)
            if "rgb" in channels:
                Image.fromarray(channels["rgb"]).save(folder / f"{frame:06d}.png")
            if "depth" in channels:
                np.save(folder / f"{frame:06d}_depth.npy", channels["depth"])

    def close(self):
        for channels in self.annotators.values():
            for annotator in channels.values():
                annotator.detach()
        for product in self.products:
            product.destroy()
