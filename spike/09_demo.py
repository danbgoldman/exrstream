"""Produce something a human can look at: an exposure sweep through the real
pipeline, plus a single graded PNG."""
import numpy as np, OpenImageIO as oiio, PyNvVideoCodec as nvc
from egl_ctx import make_context
from importlib import import_module

make_context()
OcioGL = import_module("07_ocio_gpu").OcioGL

files = sorted(__import__("pathlib").Path("testdata/2k").glob("*.exr"))
cache = []
for f in files:
    inp = oiio.ImageInput.open(str(f)); px = inp.read_image("half"); inp.close()
    h, w = px.shape[:2]
    cache.append(np.ascontiguousarray(np.dstack([px, np.ones((h, w, 1), np.float16)])))
h, w = cache[0].shape[:2]
g = OcioGL(w, h)

# single frame at EV0 -> PNG
g.set_exposure(0.0)
bgra = g(cache[0])
rgb = np.ascontiguousarray(bgra[..., [2, 1, 0]])
out = oiio.ImageOutput.create("testdata/frame_ev0.png")
out.open("testdata/frame_ev0.png", oiio.ImageSpec(w, h, 3, "uint8"))
out.write_image(rgb); out.close()
print("wrote testdata/frame_ev0.png")

# exposure sweep -4 -> +4 EV, 96 frames, H.264 elementary stream
enc = nvc.CreateEncoder(w, h, "ARGB", True, codec="h264", bitrate=15_000_000,
                        rc="cbr", fps=24, gop=30, bf=0, preset="P4")
n = 96
with open("testdata/sweep.264", "wb") as fh:
    for i in range(n):
        g.set_exposure(-4.0 + 8.0 * i / (n - 1))
        for p in enc.Encode(g(cache[i % len(cache)])):
            fh.write(bytes(p["data"]))
    for p in enc.EndEncode():
        fh.write(bytes(p["data"]))
import os
print(f"wrote testdata/sweep.264  ({os.path.getsize('testdata/sweep.264')/2**20:.1f} MB, "
      f"{n} frames, EV -4 -> +4)")
