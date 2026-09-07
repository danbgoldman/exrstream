"""Phase 0 #1b: NVENC throughput, per-frame cost, and pipeline latency on GB10.

Three numbers matter:
  fps        - sustained rate of the single encoder engine (playback ceiling)
  call       - cost of one Encode() (what the 200ms budget actually spends)
  flush      - wall time from pushing a frame to holding its packet, pushing
               back-to-back. This is the real interaction latency when paused,
               because a slider drag re-encodes the SAME frame and we can run
               the pipeline as fast as the engine allows.
"""
import time
import numpy as np
import PyNvVideoCodec as nvc

def make_nv12(w, h, n):
    xx, yy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    out = []
    for i in range(n):
        y = (127 + 100 * np.sin(xx * 0.02 + i * 0.3) * np.cos(yy * 0.017 + i * 0.2)).astype(np.uint8)
        uv = np.full((h // 2, w), 128, np.uint8)
        uv[:, 0::2] = (128 + 60 * np.sin(yy[:h // 2, 0::2] * 0.01 + i * 0.1)).astype(np.uint8)
        out.append(np.vstack([y, uv]))
    return out

def mk(w, h, codec, mbps):
    return nvc.CreateEncoder(w, h, "NV12", True, codec=codec, bitrate=int(mbps * 1e6),
                             rc="cbr", tuning_info="ultra_low_latency", preset="P3",
                             fps=24, gop=30, bf=0, idrperiod=30)

def bench(w, h, codec, mbps, nframes=240):
    frames = make_nv12(w, h, 24)
    enc = mk(w, h, codec, mbps)

    # sustained throughput + per-call cost
    for i in range(8):
        enc.Encode(frames[i % 24])
    calls, nbytes, got = [], 0, 0
    t0 = time.perf_counter()
    for i in range(nframes):
        t = time.perf_counter()
        pkts = enc.Encode(frames[i % 24])
        calls.append((time.perf_counter() - t) * 1e3)
        for p in pkts:
            nbytes += len(bytes(p["data"])); got += 1
    wall = time.perf_counter() - t0
    for p in enc.EndEncode():
        nbytes += len(bytes(p["data"])); got += 1

    # interaction latency: push one still repeatedly until its packet appears
    enc2 = mk(w, h, codec, mbps)
    still = frames[0]
    t = time.perf_counter()
    pushes = 0
    while True:
        pkts = enc2.Encode(still)
        pushes += 1
        if pkts:
            flush = (time.perf_counter() - t) * 1e3
            break
        if pushes > 16:
            flush = float("nan"); break
    enc2.EndEncode()

    c = np.array(calls)
    print(f"{codec:5s} {w}x{h} @{mbps:>4.0f}Mbps | {nframes/wall:7.1f} fps | "
          f"call p50={np.percentile(c,50):5.2f} p99={np.percentile(c,99):6.2f} ms | "
          f"flush {flush:5.1f} ms ({pushes} pushes) | "
          f"out {nbytes*8/(got/24)/1e6:5.1f} Mbps")

if __name__ == "__main__":
    for w, h, mbps in ((2048, 1080, 15), (3840, 2160, 20)):
        for codec in ("h264", "hevc", "av1"):
            bench(w, h, codec, mbps)
        print()
