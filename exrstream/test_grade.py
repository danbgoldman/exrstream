"""The shipped grade must match OCIO's own CPU processor.

This is the check that used to live on `Grade.cpu`. It belongs here: the app
never reads a CPU processor, so building one per grade and writing it on every
exposure change was pure runtime cost for a test-time need.

Content is deliberately hostile -- a 20-stop ramp, a sun far above display
white, and saturated AP1 primaries well outside Rec.709 -- because that is
where a grade goes wrong in ways a reviewer would trust.
"""
import sys

import numpy as np
import PyOpenColorIO as ocio

sys.path.insert(0, ".")
from exrstream import gl                                    # noqa: E402

SRC = "ACEScg"


def torture(w=512, h=256):
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    u, v = x / w, y / h
    img = np.repeat(np.exp2(np.float32(-14.0) + 20.0 * u)[..., None], 3, -1)
    img *= (0.25 + 0.75 * np.cos(v * np.pi * 0.5)[..., None] ** 2)
    r2 = (u - 0.4) ** 2 + ((v - 0.3) * (w / h)) ** 2
    img += (12000.0 * np.exp(-r2 * 4000.0))[..., None] * np.array([1.0, .95, .88], np.float32)
    band = (v > 0.72) & (v < 0.88)
    hue = np.clip(np.stack([np.sin(u * 6.28), np.sin(u * 6.28 + 2.1),
                            np.sin(u * 6.28 + 4.2)], -1), 0, None) ** 2
    img[band] = hue[band] * 4.0 + 0.002
    rgba = np.zeros((h, w, 4), np.float16)
    rgba[..., :3] = np.maximum(img, 0.0)
    rgba[..., 3] = 1.0
    return rgba


def cpu_reference(rgba, ev):
    grp = ocio.GroupTransform()
    grp.appendTransform(ocio.ExposureContrastTransform(
        style=ocio.EXPOSURE_CONTRAST_LINEAR, exposure=ev, dynamicExposure=False))
    grp.appendTransform(ocio.DisplayViewTransform(
        src=SRC, display=gl.DISPLAY, view=gl.VIEW))
    proc = gl.config().getProcessor(grp).getDefaultCPUProcessor()
    a = np.ascontiguousarray(rgba[..., :3], np.float32)
    proc.applyRGB(a.reshape(-1, 3))
    return (np.clip(a, 0, 1) * 255.0 + 0.5).astype(np.uint8).astype(np.float32)


def main():
    gl.make_context()
    rgba = torture()
    h, w = rgba.shape[:2]
    g = gl.grade_for(w, h, SRC)
    worst_frac = 0.0
    for ev in (-6.0, -2.0, 0.0, 2.0, 6.0):
        g.set_exposure(ev)
        got = g(rgba)[..., [2, 1, 0]].astype(np.float32)     # BGRA -> RGB
        err = np.abs(got - cpu_reference(rgba, ev))
        frac = float((err.max(-1) > 2).sum()) / (h * w)
        worst_frac = max(worst_frac, frac)
        assert np.percentile(err, 99.9) <= 2.0, \
            f"EV{ev}: p99.9 = {np.percentile(err, 99.9)}"
        print(f"  EV{ev:+5.1f}  mean {err.mean():6.4f}  p99.9 {np.percentile(err,99.9):4.1f}"
              f"  outliers {frac*100:.4f}%")
    # OCIO's own CPU and GPU paths disagree on colours far outside the display
    # gamut -- AP1 green at 250x mid-grey is linear Rec.709 [-155, 286, -32].
    # Physically implausible; bounded so a real regression still trips.
    assert worst_frac < 5e-4, f"{worst_frac*100:.4f}% deviate; expected well under that"

    # A view needing a GPU 3D LUT must refuse, not silently render wrong colour.
    try:
        gl.Grade(64, 64, SRC, gl.DISPLAY, gl.VIEW)._lut3d(
            type("T", (), {"edgeLen": 33, "samplerName": "x"})())
    except RuntimeError as e:
        assert "not wired up" in str(e)
    else:
        raise AssertionError("3D LUT path must refuse loudly")
    print("  unwired 3D-LUT view refuses loudly")
    print("OK")


if __name__ == "__main__":
    main()
