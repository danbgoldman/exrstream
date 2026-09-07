"""Regression test for NVENC's 3-frame pipeline lag.

NVENC returns the packet for the frame pushed 3 pushes earlier. Labelling that
packet with the session's CURRENT frame/exposure/look made step buttons appear
to move the wrong way and made a view change take no effect until playback
pushed more frames through.

Packet SIZE cannot detect any of this: strict CBR pads every frame to exactly
bitrate/fps bytes. So these checks read the metadata that travels with each
push, which is what the header is built from.
"""
import sys
import numpy as np

sys.path.insert(0, ".")
from exrstream import gl                                    # noqa: E402
from exrstream.seq import read_frame                        # noqa: E402


class Args:
    codec, mbps, vbv_frames = "h264", 15.0, 1.0
    src = "Linear Rec.709 (sRGB)"


def make_session(frames):
    from exrstream.server import Session
    s = Session({"args": Args()}, None)
    s.bind(type("Q", (), {"key": "t/t"})(), frames)
    return s


def main():
    gl.make_context()
    img = read_frame("footage/tos_hd/01_1a_00000.exr")
    frames = [img.copy() for _ in range(8)]
    for i, f in enumerate(frames):
        f[:, :, :3] *= np.float16(1.0 + 0.1 * i)            # make frames distinct
    s = make_session(frames)

    out = s.encode_current(flush=True)
    assert out, "no packets"
    assert out[-1].__class__ is tuple

    # Stepping: the LAST delivered packet must be the frame we asked for.
    for want in (1, 2, 3, 4, 3, 2):
        s.frame = want
        out = s.encode_current(flush=True)
        got = out[-1][0].frame
        assert got == want, f"step to {want} delivered frame {got}"
    print("  step delivers the requested frame")

    # Exposure: a settled value must be the one delivered.
    for ev in (-3.0, 2.5, 0.0):
        s.ev = ev
        out = s.encode_current(flush=True)
        assert out[-1][0].ev == ev, f"exposure {ev} delivered {out[-1][0].ev}"
    print("  settled exposure is the one delivered")

    # View: the change must reach the viewer with no further interaction.
    for view in ("Un-tone-mapped", "ACES 2.0 - SDR 100 nits (Rec.709)"):
        s.set_look(view=view)
        out = s.encode_current(flush=True)
        assert out[-1][0].view == view, f"view {view!r} delivered {out[-1][0].view!r}"
    print("  view change reaches the viewer immediately")

    # Without flush the lag is real: prove it, so the flush is not cargo cult.
    s.set_look(view="ACES 2.0 - SDR 100 nits (Rec.709)")
    s.encode_current(flush=True)
    s.frame = 7
    out = s.encode_current(flush=False)
    assert out and out[-1][0].frame != 7, \
        "expected stale pixels without flush; pipeline depth may have changed"
    print(f"  without flush the viewer sees frame {out[-1][0].frame}, not 7 (as expected)")
    print("OK")


if __name__ == "__main__":
    main()
