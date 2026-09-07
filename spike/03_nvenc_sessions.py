"""Phase 0 #1c: how many concurrent NVENC sessions does GB10 allow?

Consumer parts have historically capped at 3-8 regardless of engine throughput.
This is the per-box viewer ceiling for independent (non-presenter) sessions.
"""
import numpy as np, PyNvVideoCodec as nvc

f = np.random.randint(0, 255, (1080 * 3 // 2, 1920), dtype=np.uint8)
encs = []
try:
    for i in range(1, 65):
        e = nvc.CreateEncoder(1920, 1080, "NV12", True, codec="h264",
                              bitrate=5_000_000, rc="cbr", fps=24, gop=30, bf=0)
        e.Encode(f)          # a session isn't real until it encodes
        encs.append(e)
        print(f"  session {i:3d} ok", flush=True)
except Exception as ex:
    print(f"FAILED at session {len(encs)+1}: {type(ex).__name__}: {ex}")
else:
    print("no limit hit by 64")
print(f"=> {len(encs)} concurrent 1080p sessions")
