"""Read RGB and ideal geometric depth from independent render products."""
from pathlib import Path


class CameraRecorder:
    def __init__(self, config, paths, output):
        import omni.replicator.core as rep
        self.products, self.annotators = [], []
        self.output = Path(output)
        for camera, path in zip(config["cameras"], paths):
            product = rep.create.render_product(path, tuple(camera["resolution"]))
            rgb = rep.AnnotatorRegistry.get_annotator("rgb")
            depth = rep.AnnotatorRegistry.get_annotator("distance_to_image_plane")
            rgb.attach([product])
            depth.attach([product])
            self.products.append(product)
            self.annotators.append((camera["name"], rgb, depth))

    def capture(self, frame):
        import numpy as np
        from PIL import Image
        for name, rgb, depth in self.annotators:
            color, distance = rgb.get_data(), depth.get_data()
            if color.size == 0 or distance.size == 0:
                raise RuntimeError(f"Camera {name} has no rendered data")
            folder = self.output / "cameras" / name
            folder.mkdir(parents=True, exist_ok=True)
            Image.fromarray(color[..., :3]).save(folder / f"{frame:06d}.png")
            np.save(folder / f"{frame:06d}_depth.npy", distance)

    def close(self):
        for _, rgb, depth in self.annotators:
            rgb.detach()
            depth.detach()
        for product in self.products:
            product.destroy()
