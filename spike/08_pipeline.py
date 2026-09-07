"""Phase 0 #2 (server half): EXR -> RAM cache -> grade -> NVENC, end to end.

Reports the two numbers the design turns on:
  playback   - sustained fps of decode+grade+encode (needs >= 24)
  slider     - ms from an exposure change to holding the encoded packet for the
               current frame. This is the paused case, which is when people
               actually ride an exposure slider, so the pipeline is pushed as
               fast as the engine allows rather than at frame rate.
"""
import sys, time
from pathlib import Path
import numpy as np
import OpenImageIO as oiio
import PyNvVideoCodec as nvc
from egl_ctx import make_context

sys.path.insert(0, str(Path(__file__).parent))

def load_cache(d, limit=None):
    """Decode EXRs into the RAM frame cache as RGBA half (padded for fast upload)."""
    files = sorted(Path(d).glob("*.exr"))[:limit]
    out = []
    for f in files:
        inp = oiio.ImageInput.open(str(f))
        px = inp.read_image("half"); inp.close()
        h, w = px.shape[:2]
        out.append(np.ascontiguousarray(
            np.dstack([px, np.ones((h, w, 1), np.float16)])))
    return out

def run(seq, codec, mbps, label):
    from importlib import import_module
    OcioGL = import_module("07_ocio_gpu").OcioGL
    cache = load_cache(seq)
    h, w = cache[0].shape[:2]
    g = OcioGL(w, h)
    enc = nvc.CreateEncoder(w, h, "ARGB", True, codec=codec,
                            bitrate=int(mbps * 1e6), rc="cbr", fps=24, gop=30, bf=0,
                            tuning_info="ultra_low_latency", preset="P3")
    g.set_exposure(0.0)
    for i in range(6):
        enc.Encode(g(cache[i % len(cache)]))

    # sustained playback
    n, nbytes, got = 96, 0, 0
    t0 = time.perf_counter()
    for i in range(n):
        for p in enc.Encode(g(cache[i % len(cache)])):
            nbytes += len(bytes(p["data"])); got += 1
    fps = n / (time.perf_counter() - t0)

    # slider latency, paused on one frame
    lat = []
    for k in range(20):
        t = time.perf_counter()
        g.set_exposure(-3.0 + 0.3 * k)
        pushes = 0
        while pushes < 8:
            pkts = enc.Encode(g(cache[0])); pushes += 1
            if pkts: break
        lat.append((time.perf_counter() - t) * 1e3)
    enc.EndEncode()
    lat = np.array(lat)
    print(f"{label:3s} {codec:5s} {w}x{h}  playback {fps:6.1f} fps  |  "
          f"slider p50 {np.percentile(lat,50):5.1f} p99 {np.percentile(lat,99):5.1f} ms  |  "
          f"{nbytes*8/(got/24)/1e6:5.1f} Mbps")

if __name__ == "__main__":
    make_context()
    for seq, mbps, label in (("testdata/2k", 15, "2K"), ("testdata/4k", 20, "4K")):
        for codec in ("h264", "hevc", "av1"):
            run(seq, codec, mbps, label)
        print()
