"""Phase 0 #5: the grade, on GPU, and its error against OCIO's own CPU processor.

Two facts shape this:
  - exposure is a per-channel multiply in linear and the shaper is per-channel,
    so exposure FOLDS INTO the 1D shaper LUT: a slider move rebuilds 4096 floats.
  - grading server-side means the shaper only sees post-exposure values, so the
    shaper's range is spent where the viewer is actually looking. Client-side
    grading has to keep the whole range live at once; this does not.

Grade = 1D LUT (exposure + lin->ACEScct) -> 3D LUT (ACEScct -> display).

The 3D LUT lattice MUST span ACEScct's full output range (~1.468 at half-float
max), not [0,1]. [0,1] tops out at linear 222 and flattens every highlight
above it -- 167/255 of error on a sun pixel at EV+6.
"""
import time
import numpy as np
import PyOpenColorIO as ocio
import cupy as cp
from cupyx.scipy.ndimage import map_coordinates

CONFIG = "ocio://cg-config-v4.0.0_aces-v2.0_ocio-v2.5"
SRC, CCT = "ACEScg", "ACEScct"
DISPLAY, VIEW = "sRGB - Display", "ACES 2.0 - SDR 100 nits (Rec.709)"

# NOT `getProcessor(SRC, "sRGB - Display")`. That is a colourspace conversion --
# primaries matrix plus transfer function, with NO tone mapping and NO gamut
# compression, so 0.18 grey maps to 0.46 instead of 0.35 and saturated colours
# come back as [1.2, 7.3, -3.0]. The output transform lives on the *view*.
HALF_MAX = 65504.0
cfg = ocio.Config.CreateFromFile(CONFIG)

def cpu_proc(src, dst):
    return cfg.getProcessor(src, dst).getDefaultCPUProcessor()

def display_proc():
    return cfg.getProcessor(ocio.DisplayViewTransform(
        src=SRC, display=DISPLAY, view=VIEW))

def cct_display_proc():
    """ACEScct -> display, for baking the 3D LUT."""
    grp = ocio.GroupTransform()
    grp.appendTransform(ocio.ColorSpaceTransform(src=CCT, dst=SRC))
    grp.appendTransform(ocio.DisplayViewTransform(
        src=SRC, display=DISPLAY, view=VIEW))
    return cfg.getProcessor(grp).getDefaultCPUProcessor()

def apply_cpu(proc, arr):
    a = np.ascontiguousarray(arr, np.float32)
    proc.applyRGB(a.reshape(-1, 3))
    return a

CCT_MAX = float(apply_cpu(cpu_proc(SRC, CCT),
                          np.full((1, 3), HALF_MAX, np.float32))[0, 0])

LO, HI, N1D, LUT_N = -20.0, 20.0, 8192, 65

def build_1d(ev):
    """log2-uniform samples of (lin * 2^ev) -> ACEScct, normalised to [0,1]."""
    l2 = np.linspace(LO, HI, N1D, dtype=np.float32)
    lin = np.exp2(l2 + np.float32(ev))
    cct = apply_cpu(cpu_proc(SRC, CCT), np.repeat(lin[:, None], 3, 1))[:, 0]
    return l2, np.ascontiguousarray(cct / CCT_MAX)

def build_3d(n=65):
    """Lattice over the FULL ACEScct range, clamped to display range.

    Clamping at bake time is load-bearing, not cosmetic. OCIO's display
    transform returns unclamped values -- an out-of-gamut saturated pixel comes
    back as e.g. [1.219, 7.275, -3.026]. Interpolating a cell whose corners hold
    -3 and +7 gives nonsense, which then clips to 0 where it should clip to 1:
    a full-scale error, seen as dark fringes on saturated highlights. Clamp
    first and the function is smooth and bounded, so trilinear behaves.
    """
    g = np.linspace(0.0, CCT_MAX, n, dtype=np.float32)
    lat = np.stack(np.meshgrid(g, g, g, indexing="ij"), -1).reshape(-1, 3)
    return np.clip(apply_cpu(cct_display_proc(), lat), 0.0, 1.0).reshape(n, n, n, 3)

class GpuGrade:
    def __init__(self, lut3d):
        self.lut3d = [cp.ascontiguousarray(cp.asarray(lut3d[..., c])) for c in range(3)]
        self.n = lut3d.shape[0]
        self.set_exposure(0.0)

    def set_exposure(self, ev):
        l2, cct = build_1d(ev)
        self.xs, self.ys = cp.asarray(l2), cp.asarray(cct)

    def __call__(self, lin):
        l2 = cp.log2(cp.maximum(lin, 1e-12))
        cct = cp.interp(l2, self.xs, self.ys)               # already normalised
        c = cp.clip(cct, 0.0, 1.0) * (self.n - 1)
        coords = cp.stack([c[..., 0], c[..., 1], c[..., 2]]).reshape(3, -1)
        out = cp.empty((3,) + lin.shape[:2], cp.float32)
        # ponytail: 3 separate map_coordinates passes; a fused RawKernel would be
        # ~3x faster. Only worth it if 4K playback needs more headroom.
        for ch in range(3):
            out[ch] = map_coordinates(self.lut3d[ch], coords, order=1,
                                      mode="nearest").reshape(lin.shape[:2])
        return cp.ascontiguousarray(out.transpose(1, 2, 0))

def _selftest():
    import OpenImageIO as oiio
    print(f"ACEScct range 0..{CCT_MAX:.4f}  (lattice spans all of it)")
    lut = build_3d(LUT_N); g = GpuGrade(lut)
    inp = oiio.ImageInput.open("testdata/2k/test.0000.exr")
    lin = inp.read_image("float"); inp.close()
    ref_proc, d = display_proc().getDefaultCPUProcessor(), cp.asarray(lin)
    worst = 0.0
    for ev in (-6.0, -2.0, 0.0, 2.0, 6.0):
        ref = np.clip(apply_cpu(ref_proc, lin * np.exp2(np.float32(ev))), 0, 1)
        g.set_exposure(ev)
        got = np.clip(cp.asnumpy(g(d)), 0, 1)
        err = np.abs(got - ref) * 255.0
        worst = max(worst, err.max())
        print(f"  EV{ev:+5.1f}  mean {err.mean():6.3f}  p99 {np.percentile(err,99):6.3f}"
              f"  max {err.max():6.3f}  (8-bit codes)")
    assert worst < 2.0, f"grade deviates from OCIO by {worst:.1f} codes"

    for res, name in (((1080, 2048), "2K"), ((2160, 3840), "4K")):
        a = cp.asarray(np.random.rand(*res, 3).astype(np.float32) * 8)
        for _ in range(3): g(a)
        cp.cuda.Stream.null.synchronize()
        t = time.perf_counter()
        for _ in range(20): g(a)
        cp.cuda.Stream.null.synchronize()
        dt = (time.perf_counter() - t) / 20
        print(f"  {name} grade {dt*1e3:5.2f} ms/frame ({1/dt:5.0f} fps)")
    print("OK")

if __name__ == "__main__":
    _selftest()
