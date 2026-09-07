"""Checks for the EXR reading rules that silently produce a wrong picture when
they are wrong: channel identification and data-vs-display window placement."""
import tempfile
from pathlib import Path

import numpy as np
import OpenImageIO as oiio

from exrstream.seq import read_frame, discover, _rgb_indices, SequenceError


def _write(path, pixels, channels, full=None, origin=(0, 0)):
    h, w = pixels.shape[:2]
    spec = oiio.ImageSpec(w, h, len(channels), "half")
    spec.channelnames = channels
    if full:
        spec.full_width, spec.full_height = full
        spec.full_x, spec.full_y = 0, 0
    spec.x, spec.y = origin
    out = oiio.ImageOutput.create(str(path))
    out.open(str(path), spec)
    out.write_image(np.ascontiguousarray(pixels, np.float16))
    out.close()


def test_channel_order():
    assert _rgb_indices(["R", "G", "B"]) == [0, 1, 2]
    assert _rgb_indices(["B", "G", "R"]) == [2, 1, 0]          # order respected
    assert _rgb_indices(["R", "G", "B", "A", "Z"]) == [0, 1, 2]  # AOVs ignored
    assert _rgb_indices(["Y"]) == [0, 0, 0]                     # luminance only


def test_reads_bgr_in_right_order(tmp):
    px = np.zeros((4, 4, 3), np.float16)
    px[..., 0] = 0.1   # stored as B
    px[..., 1] = 0.2   # G
    px[..., 2] = 0.3   # R
    p = tmp / "bgr.exr"
    _write(p, px, ["B", "G", "R"])
    got = read_frame(p)
    assert abs(float(got[0, 0, 0]) - 0.3) < 1e-3, "R must come from the R channel"
    assert abs(float(got[0, 0, 2]) - 0.1) < 1e-3, "B must come from the B channel"


def test_data_window_smaller_than_display(tmp):
    """A 4x4 data window at (2,3) inside an 8x8 display window."""
    px = np.ones((4, 4, 3), np.float16) * 0.5
    p = tmp / "crop.exr"
    _write(p, px, ["R", "G", "B"], full=(8, 8), origin=(2, 3))
    got = read_frame(p)
    assert got.shape == (8, 8, 4), got.shape
    assert float(got[0, 0, 0]) == 0.0, "outside the data window must be black"
    assert abs(float(got[3, 2, 0]) - 0.5) < 1e-3, "data window landed at the wrong offset"
    assert abs(float(got[6, 5, 0]) - 0.5) < 1e-3
    assert float(got[7, 6, 0]) == 0.0, "data window bled past its extent"
    assert float(got[..., 3].min()) == 1.0, "alpha must be padded to 1"


def test_data_window_larger_than_display(tmp):
    """Overscan: 8x8 of data, only the middle 4x4 is the display window."""
    px = np.arange(8 * 8 * 3, dtype=np.float32).reshape(8, 8, 3) / 1000.0
    p = tmp / "overscan.exr"
    _write(p, px, ["R", "G", "B"], full=(4, 4), origin=(-2, -2))
    got = read_frame(p)
    assert got.shape == (4, 4, 4), got.shape
    assert abs(float(got[0, 0, 0]) - float(px[2, 2, 0])) < 1e-3, "wrong overscan crop"


def test_mixed_resolution_refused(tmp):
    _write(tmp / "s.0001.exr", np.zeros((4, 4, 3), np.float16), ["R", "G", "B"])
    _write(tmp / "s.0002.exr", np.zeros((6, 6, 3), np.float16), ["R", "G", "B"])
    from exrstream.seq import FrameCache
    seqs = [s for s in discover(tmp) if s.nframes == 2]
    assert seqs, "sequence grouping failed"
    try:
        FrameCache().load(seqs[0], 0, 2)
    except SequenceError as e:
        assert "mixed resolutions" in str(e)
    else:
        raise AssertionError("mixed resolutions must be refused, not guessed")


def test_discovery_groups_by_stem(tmp):
    for i in range(3):
        _write(tmp / f"shotA.{i:04d}.exr", np.zeros((2, 2, 3), np.float16), ["R", "G", "B"])
    for i in range(2):
        _write(tmp / f"shotB_{i:04d}.exr", np.zeros((2, 2, 3), np.float16), ["R", "G", "B"])
    got = {Path(s.key).name: s.nframes for s in discover(tmp)}
    assert got.get("shotA") == 3 and got.get("shotB") == 2, got


def main():
    test_channel_order()
    for fn in (test_reads_bgr_in_right_order, test_data_window_smaller_than_display,
               test_data_window_larger_than_display, test_mixed_resolution_refused,
               test_discovery_groups_by_stem):
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
        print(f"  ok  {fn.__name__}")
    print("OK")


if __name__ == "__main__":
    main()
