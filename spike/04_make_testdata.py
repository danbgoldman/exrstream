"""Synthesise scene-linear EXR sequences with the content that actually stresses
the pipeline: smooth gradients (banding), a sun-value highlight (log-shaper
range), and saturated AP1 colours outside Rec.709 (gamut)."""
import sys, numpy as np, OpenImageIO as oiio
from pathlib import Path

def frame(w, h, i, nframes):
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    u, v = x / w, y / h
    t = i / nframes

    # smooth exposure ramp across ~20 stops: the banding torture test
    img = np.zeros((h, w, 3), np.float32)
    ramp = np.exp2(np.float32(-14.0) + 20.0 * u)
    img[...] = ramp[..., None]

    # soft vertical falloff so gradients exist in both axes
    img *= (0.25 + 0.75 * np.cos(v * np.pi * 0.5)[..., None] ** 2)

    # sun: tiny, extremely bright, drifts across frame
    cx, cy = 0.15 + 0.7 * t, 0.25
    r2 = (u - cx) ** 2 + ((v - cy) * (w / h)) ** 2
    img += (12000.0 * np.exp(-r2 * 4000.0))[..., None] * np.array([1.0, 0.95, 0.88], np.float32)

    # saturated primaries outside Rec.709, in a band
    band = (v > 0.72) & (v < 0.88)
    hue = np.clip(np.stack([np.sin(u * 6.28 + t), np.sin(u * 6.28 + 2.1 + t),
                            np.sin(u * 6.28 + 4.2 + t)], -1), 0, None) ** 2
    img[band] = (hue[band] * 4.0 + 0.002)

    # fine texture so the codec has real high-frequency work
    rng = np.random.default_rng(i)
    img *= (1.0 + 0.02 * rng.standard_normal((h, w, 1)).astype(np.float32))
    return np.ascontiguousarray(np.maximum(img, 0.0))

def write_seq(outdir, w, h, nframes, compression="zip"):
    outdir = Path(outdir); outdir.mkdir(parents=True, exist_ok=True)
    for i in range(nframes):
        a = frame(w, h, i, nframes).astype(np.float16)
        spec = oiio.ImageSpec(w, h, 3, "half")
        spec.attribute("compression", compression)
        spec.attribute("oiio:ColorSpace", "Linear")
        out = oiio.ImageOutput.create(str(outdir / f"test.{i:04d}.exr"))
        out.open(str(outdir / f"test.{i:04d}.exr"), spec)
        out.write_image(a)
        out.close()
    tot = sum(p.stat().st_size for p in outdir.glob("*.exr"))
    print(f"{outdir}  {nframes} frames {w}x{h} {compression}  "
          f"{tot/2**20:.0f} MB total, {tot/nframes/2**20:.1f} MB/frame")

if __name__ == "__main__":
    write_seq("testdata/2k", 2048, 1080, 48)
    write_seq("testdata/4k", 3840, 2160, 24)
    write_seq("testdata/4k_dwab", 3840, 2160, 24, "dwab")
