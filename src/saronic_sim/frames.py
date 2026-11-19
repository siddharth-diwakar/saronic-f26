"""Captured camera frames kept for download as binary, bounded by memory.

The simulation thread stores raw arrays; HTTP threads encode on first download, so a step
does not wait for PNG compression. RGB downloads as PNG. Depth downloads as little-endian
float32 rows (distance to the image plane in meters), height x width, no header.
"""
import io
import itertools
import threading
from collections import OrderedDict

DEFAULT_MAX_BYTES = 512 * 1024 * 1024
ENCODINGS = {"rgb": ("png", "image/png"), "depth": ("f32le", "application/octet-stream")}


class FrameStore:
    def __init__(self, max_bytes=DEFAULT_MAX_BYTES, prefix=""):
        self.max_bytes = max_bytes
        self.prefix = prefix
        self._frames = OrderedDict()  # frame id -> {"array", "channel", "data", "bytes"}
        self._bytes = 0
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def add(self, channel, array):
        """Store one frame; returns (frame_id, encoding, width, height). The oldest frames are evicted first."""
        import numpy as np
        array = np.ascontiguousarray(array, dtype=np.uint8 if channel == "rgb" else "<f4")
        frame_id = f"{self.prefix}{next(self._ids)}"
        with self._lock:
            self._frames[frame_id] = {"array": array, "channel": channel, "shape": array.shape[:2], "data": None, "bytes": array.nbytes}
            self._bytes += array.nbytes
            while self._bytes > self.max_bytes and len(self._frames) > 1:
                _, old = self._frames.popitem(last=False)
                self._bytes -= old["bytes"]
        height, width = array.shape[:2]
        return frame_id, ENCODINGS[channel][0], width, height

    def get(self, frame_id):
        """(bytes, content type, headers), or None once evicted or unknown."""
        with self._lock:
            frame = self._frames.get(frame_id)
            if frame is None:
                return None
            array, channel, data, (height, width) = frame["array"], frame["channel"], frame["data"], frame["shape"]
        if data is None:
            data = _encode(channel, array)
            with self._lock:
                if frame_id in self._frames:
                    entry = self._frames[frame_id]
                    self._bytes += len(data) - entry["bytes"]
                    # Keep the encoded bytes only; the array is no longer needed.
                    entry.update(data=data, array=None, bytes=len(data))
        encoding, content_type = ENCODINGS[channel]
        headers = {"X-Frame-Channel": channel, "X-Frame-Encoding": encoding, "X-Frame-Width": str(width), "X-Frame-Height": str(height)}
        return data, content_type, headers


def _encode(channel, array):
    if channel == "depth":
        return array.tobytes()
    from PIL import Image
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="PNG")
    return buffer.getvalue()
