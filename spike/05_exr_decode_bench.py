"""Phase 0 #4: EXR decode throughput. Sets cold-open time and decides whether
the disk mezzanine (tier 2) is needed in Phase 1 or can wait."""
import time, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np, OpenImageIO as oiio

def read(path):
    inp = oiio.ImageInput.open(str(path))
    px = inp.read_image("half")
    inp.close()
    return px

def bench(d, threads):
    files = sorted(Path(d).glob("*.exr"))
    read(files[0])                                   # warm page cache
    t = time.perf_counter()
    if threads == 1:
        for f in files: read(f)
    else:
        with ThreadPoolExecutor(threads) as ex:
            list(ex.map(read, files))
    dt = time.perf_counter() - t
    return len(files) / dt, dt / len(files) * 1e3

if __name__ == "__main__":
    oiio.attribute("threads", 1)   # per-image threads off; we parallelise across frames
    for d in ("testdata/2k", "testdata/4k", "testdata/4k_dwab"):
        row = []
        for n in (1, 4, 10, 20):
            fps, ms = bench(d, n)
            row.append(f"{n:2d}t {fps:6.1f} fps ({ms:6.1f} ms/f)")
        print(f"{d:18s} " + " | ".join(row))
