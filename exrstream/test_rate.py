"""Frame-rate negotiation: the arithmetic that decides what the viewer sees.

The claim this feature makes is that a 24 fps sequence streamed at 30 fps still
runs at 24 fps. If that is wrong the tool misrepresents motion, which is the
class of wrongness it exists to prevent, so it is checked rather than asserted
in a comment.
"""
from exrstream.server import Session, stream_fps, cadence_note


def _sess(src_fps, hz, nframes=100, resample=False):
    s = Session.__new__(Session)                 # no app, no socket, no encoder
    s.src_fps, s.refresh_hz, s.resample = src_fps, hz, resample
    s.frames = [None] * nframes
    s.pos, s.frame, s.out_fps = 0.0, 0, src_fps
    s.retune()
    return s


def test_stream_rate_divides_the_refresh():
    for hz in (30, 59.94, 60, 120, 144):
        for src in (23.976, 24, 25, 29.97, 30, 48, 60):
            out = stream_fps(src, hz)
            k = hz / out
            assert abs(k - round(k)) < 1e-9, f"{src} on {hz} gave {out}, not a divisor"
            assert out <= hz + 1e-9, f"{out} exceeds the {hz} Hz refresh"


def test_picks_the_divisor_nearest_the_sequence_rate():
    assert stream_fps(24, 30) == 30          # 1.25 refreshes/frame -> stream at 30
    assert stream_fps(24, 60) == 30          # 2.5 -> 30, not 60
    assert stream_fps(24, 120) == 24         # divides already; leave it alone
    assert stream_fps(30, 30) == 30
    assert stream_fps(48, 30) == 30          # faster than the display: drop, not lie
    assert abs(stream_fps(23.976, 59.94) - 29.97) < 1e-9
    assert stream_fps(24, 0) == 24           # refresh unknown: do nothing


def test_repeats_keep_the_sequence_at_its_own_rate():
    """N output frames take N/out seconds, and must cover that many seconds of
    sequence -- src * N / out frames of it."""
    for src, hz in ((24, 30), (25, 60), (23.976, 59.94), (48, 30), (30, 30)):
        s = _sess(src, hz, nframes=100000)
        for _ in range(1000):
            s.advance()
        want = 1000 * src / s.out_fps
        assert abs(s.pos - want) < 1e-6, (
            f"{src} fps on {hz} Hz: 1000 output frames ({1000/s.out_fps:.2f}s) "
            f"advanced {s.pos:.3f} source frames, not {want:.3f}")


def test_repeats_land_on_real_frames_and_wrap():
    s = _sess(24, 30, nframes=4)
    got = []
    for _ in range(10):
        s.advance()
        got.append(s.frame)
    assert all(0 <= f < 4 for f in got), got
    assert got == [0, 1, 2, 3, 0, 0, 1, 2, 3, 0], got   # one repeat in five


def test_resample_is_opt_in_and_admits_what_it_did():
    s = _sess(24, 30)
    assert s.src_fps == 24 and s.out_fps == 30, "the default must not alter timing"
    n = cadence_note(s.src_fps, s.out_fps, s.refresh_hz, s.resample)
    assert "repeating" in n and "24 fps" in n, n

    r = _sess(24, 30, resample=True)
    assert r.src_fps == 24, "the declared sequence rate must survive resampling"
    assert r.out_fps == 30, "resample runs at the display rate"
    for _ in range(30):
        r.advance()
    assert abs(r.pos - 30) < 1e-6, "resampled playback covers 30 frames a second"
    n = cadence_note(r.src_fps, r.out_fps, r.refresh_hz, r.resample)
    assert "1.25x speed" in n, n


def test_silent_when_there_is_nothing_to_say():
    assert cadence_note(30, 30, 30, False) is None
    assert cadence_note(24, 24, 120, False) is None
    assert cadence_note(24, 24, 0, False) is None       # refresh unknown


def main():
    for fn in (test_stream_rate_divides_the_refresh,
               test_picks_the_divisor_nearest_the_sequence_rate,
               test_repeats_keep_the_sequence_at_its_own_rate,
               test_repeats_land_on_real_frames_and_wrap,
               test_resample_is_opt_in_and_admits_what_it_did,
               test_silent_when_there_is_nothing_to_say):
        fn()
        print(f"  ok  {fn.__name__}")
    print("OK")


if __name__ == "__main__":
    main()
