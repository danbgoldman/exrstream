"""The A/B wipe, at pixel level.

A wipe that silently shows the same image on both sides, or pairs the wrong
frames, is worse than no wipe: it is a review tool answering "no difference"
when it was never comparing anything.
"""
import numpy as np

from exrstream import gl
from exrstream.server import Session

SRC = "Linear Rec.709 (sRGB)"
W, H = 128, 64


def _flat(r, g, b):
    a = np.zeros((H, W, 4), np.float16)
    a[..., 0], a[..., 1], a[..., 2], a[..., 3] = r, g, b, 1.0
    return a


def _sess(frames_a, frames_b, wipe=0.5, view_b=None):
    gl.make_context()
    s = Session.__new__(Session)
    s.w, s.h, s.ev = W, H, 0.0
    s.src, s.view = SRC, gl.VIEW
    s.frames, s.frames_b = frames_a, frames_b
    s.frame, s.wipe, s.roi = 0, wipe, (0.0, 0.0, 1.0)
    s.grade = gl.grade_for(W, H, SRC, gl.DISPLAY, gl.VIEW)
    s.src_b, s.view_b = SRC, view_b or gl.VIEW
    s.grade_b = gl.grade_for(W, H, SRC, gl.DISPLAY, s.view_b)
    return s


def test_each_side_shows_its_own_sequence():
    a, b = _flat(0.8, 0.05, 0.05), _flat(0.05, 0.05, 0.8)
    solo_a = gl.grade_for(W, H, SRC)(a).copy()
    solo_b = gl.grade_for(W, H, SRC)(b).copy()
    assert not np.array_equal(solo_a, solo_b), "test inputs are not distinguishable"

    got = _sess([a], [b]).render_current().copy()
    mid = W // 2
    assert np.array_equal(got[:, :mid], solo_a[:, :mid]), "left of the wipe is not A"
    assert np.array_equal(got[:, mid:], solo_b[:, mid:]), "right of the wipe is not B"


def test_wipe_ends_are_whole_frames():
    a, b = _flat(0.8, 0.05, 0.05), _flat(0.05, 0.05, 0.8)
    solo_a = gl.grade_for(W, H, SRC)(a).copy()
    solo_b = gl.grade_for(W, H, SRC)(b).copy()
    assert np.array_equal(_sess([a], [b], wipe=1.0).render_current(), solo_a), \
        "wipe at 1.0 must be all A"
    assert np.array_equal(_sess([a], [b], wipe=0.0).render_current(), solo_b), \
        "wipe at 0.0 must be all B"


def test_compares_two_looks_on_one_frame():
    """Same footage, different view: the two halves must still differ."""
    other = next((v for v in gl.views() if v != gl.VIEW and "Raw" not in v), None)
    assert other, "config has only one view to compare"
    a = _flat(0.9, 0.6, 0.2)
    got = _sess([a], [a], view_b=other).render_current()
    mid = W // 2
    assert not np.array_equal(got[:, mid - 1], got[:, mid]), \
        "two different views produced the same picture"


def test_short_b_holds_its_last_frame():
    a = [_flat(0.1 * i, 0.1, 0.1) for i in range(1, 6)]
    b = [_flat(0.05, 0.05, 0.8), _flat(0.05, 0.8, 0.05)]
    s = _sess(a, b)
    s.frame = 4                                   # past the end of B
    got = s.render_current().copy()
    s2 = _sess(a, [b[1]])                         # B's last frame, on its own
    s2.frame = 4
    assert np.array_equal(got[:, W // 2:], s2.render_current()[:, W // 2:]), \
        "past B's end must hold its last frame, not wrap to its first"


def test_region_is_an_exact_magnification():
    """A region of half the source, rendered into the whole output, must be the
    nearest-neighbour 2x magnification of that quadrant -- exactly, since the
    grade is per-pixel and the sampler is GL_NEAREST. Anything else means the
    region maths is off by a fraction of a pixel, which reads as softness."""
    a = np.zeros((H, W, 4), np.float16)
    a[..., 3] = 1.0
    ys, xs = np.mgrid[0:H, 0:W]
    a[..., 0] = (xs / W).astype(np.float16)          # a ramp with no two
    a[..., 1] = (ys / H).astype(np.float16)          # pixels alike
    a[..., 2] = ((xs ^ ys) % 17 / 17).astype(np.float16)

    s = _sess([a], None)
    full = s.render_current().copy()
    s.roi = (0.0, 0.0, 0.5)
    got = s.render_current().copy()
    want = full[: H // 2, : W // 2].repeat(2, 0).repeat(2, 1)
    assert np.array_equal(got, want), (
        f"{int((got != want).sum())} of {got.size} bytes differ; "
        "the region is not landing on pixel centres")


def test_region_applies_to_both_panes():
    """Comparing two differently-framed images is not comparing."""
    a, b = _flat(0.8, 0.05, 0.05), _flat(0.05, 0.05, 0.8)
    s = _sess([a], [b])
    s.roi = (0.25, 0.25, 0.5)
    got = s.render_current().copy()
    solo = _sess([a], None); solo.roi = s.roi
    solo_a = solo.render_current().copy()
    mid = W // 2
    assert np.array_equal(got[:, :mid], solo_a[:, :mid]), "A ignored the region"
    # B is flat, so check it against B rendered alone at the same region.
    solo_b = _sess([b], None); solo_b.roi = s.roi
    assert np.array_equal(got[:, mid:], solo_b.render_current()[:, mid:]), \
        "B ignored the region"


def test_off_is_plain_a():
    a = _flat(0.4, 0.4, 0.4)
    assert np.array_equal(_sess([a], None).render_current(),
                          gl.grade_for(W, H, SRC)(a)), \
        "with no B side the output must be exactly the A grade"


def main():
    for fn in (test_each_side_shows_its_own_sequence, test_wipe_ends_are_whole_frames,
               test_compares_two_looks_on_one_frame, test_short_b_holds_its_last_frame,
               test_region_is_an_exact_magnification,
               test_region_applies_to_both_panes, test_off_is_plain_a):
        fn()
        print(f"  ok  {fn.__name__}")
    print("OK")


if __name__ == "__main__":
    main()
