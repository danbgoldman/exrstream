"""Sequence discovery and the RAM frame cache.

Real EXRs are not the tidy 3-channel full-frame images the Phase 0 spike
generated. This module handles what actually comes out of a renderer -- named
channels in arbitrary order, data windows smaller (or larger) than the display
window, multi-part files -- and refuses clearly what it cannot handle, rather
than silently producing a wrong picture.
"""
import re, threading, time
from collections import OrderedDict
from pathlib import Path

import numpy as np
import OpenImageIO as oiio

# name.0001.exr / name_0001.exr / name0001.exr
_PAT = re.compile(r"^(?P<stem>.*?)[._]?(?P<num>\d{2,10})$")


class SequenceError(Exception):
    """Something we will not guess about. The message goes to the user."""


class Sequence:
    def __init__(self, key, files, w, h, channels):
        self.key, self.files = key, files
        self.w, self.h, self.channels = w, h, channels

    @property
    def nframes(self):
        return len(self.files)

    def info(self):
        # Include the parent directory: sequences from different shots or
        # resolutions routinely share a stem (tos_hd/01_1a vs tos_4k/01_1a).
        p = Path(self.key)
        return {"key": self.key, "name": f"{p.parent.name}/{p.name}",
                "frames": self.nframes, "w": self.w, "h": self.h,
                "channels": self.channels,
                "bytes": self.nframes * self.w * self.h * 8}


def _probe(path):
    inp = oiio.ImageInput.open(str(path))
    if inp is None:
        raise SequenceError(f"cannot open {Path(path).name}: {oiio.geterror()}")
    spec = inp.spec()
    inp.close()
    return spec


def discover(root, max_seqs=200):
    """Group EXRs under `root` into sequences by (directory, stem)."""
    root = Path(root)
    groups = {}
    for f in sorted(root.rglob("*.exr")):
        m = _PAT.match(f.stem)
        stem = m.group("stem") if m else f.stem
        groups.setdefault(str(f.parent / stem), []).append(f)
    out = []
    for key, files in sorted(groups.items())[:max_seqs]:
        try:
            spec = _probe(files[0])
        except SequenceError:
            continue
        out.append(Sequence(key, files, spec.full_width, spec.full_height,
                            list(spec.channelnames)))
    return out


def _rgb_indices(names):
    """Map channel names to R,G,B indices, tolerating AOV-laden files.

    Renderers emit things like ['R','G','B','A','Z','N.x',...] or a bare
    ['Y']. Taking the first three channels positionally is wrong often enough
    to matter, so match by name and fall back only when there is nothing to
    match on.
    """
    low = [n.lower() for n in names]
    idx = []
    for want in ("r", "g", "b"):
        for cand in (want, f"{want}gb"[0]):
            if cand in low:
                idx.append(low.index(cand))
                break
        else:
            idx.append(None)
    if all(i is not None for i in idx):
        return idx
    for lum in ("y", "luminance", "l"):
        if lum in low:
            i = low.index(lum)
            return [i, i, i]
    if len(names) >= 3:
        return [0, 1, 2]
    if len(names) == 1:
        return [0, 0, 0]
    raise SequenceError(f"cannot find RGB among channels {names}")


def read_frame(path):
    """One EXR -> (h, w, 4) float16 in the DISPLAY window, alpha padded to 1.

    RGBA rather than RGB is deliberate: a 3-component GL upload makes the driver
    repack every row, 1.06 GB/s vs 21 GB/s. The padding is paid once, here.
    """
    inp = oiio.ImageInput.open(str(path))
    if inp is None:
        raise SequenceError(f"cannot open {Path(path).name}: {oiio.geterror()}")
    try:
        spec = inp.spec()
        if spec.deep:
            raise SequenceError(f"{Path(path).name}: deep EXRs are not supported")
        px = inp.read_image("half")
        if px is None:
            raise SequenceError(f"{Path(path).name}: {inp.geterror()}")
        names = list(spec.channelnames)
    finally:
        inp.close()

    if px.ndim == 2:
        px = px[..., None]
    ri, gi, bi = _rgb_indices(names)
    rgb = px[..., [ri, gi, bi]]

    W, H = spec.full_width, spec.full_height
    out = np.zeros((H, W, 4), np.float16)
    out[..., 3] = 1.0
    # Data window may be offset from, and smaller or larger than, the display
    # window. Compositing into the display window is what every other tool
    # shows, so anything else would silently disagree with the reference.
    dx, dy = spec.x - spec.full_x, spec.y - spec.full_y
    sx0, sy0 = max(0, -dx), max(0, -dy)
    dx0, dy0 = max(0, dx), max(0, dy)
    cw = min(spec.width - sx0, W - dx0)
    ch = min(spec.height - sy0, H - dy0)
    if cw > 0 and ch > 0:
        out[dy0:dy0 + ch, dx0:dx0 + cw, :3] = rgb[sy0:sy0 + ch, sx0:sx0 + cw]
    return out


class FrameCache:
    """LRU over whole sequences, bounded by bytes.

    Keyed by (path, mtime) so a re-rendered sequence is not served stale.
    """

    def __init__(self, cap_bytes=40 << 30):
        self.cap = cap_bytes
        self.bad = []
        self._d = OrderedDict()
        self._lock = threading.Lock()

    @property
    def bytes(self):
        return sum(sum(f.nbytes for f in v) for v in self._d.values())

    def _key(self, seq, first, count):
        mt = max(f.stat().st_mtime_ns for f in seq.files[first:first + count])
        return (seq.key, first, count, mt)

    def get(self, seq, first, count):
        with self._lock:
            k = self._key(seq, first, count)
            if k in self._d:
                self._d.move_to_end(k)
                return self._d[k]
        return None

    def load(self, seq, first, count, progress=None):
        """Decode into the cache. Blocking -- call it in an executor."""
        k = self._key(seq, first, count)
        hit = self.get(seq, first, count)
        if hit is not None:
            return hit
        frames, bad, w, h = [], [], None, None
        for i, f in enumerate(seq.files[first:first + count]):
            try:
                a = read_frame(f)
            except Exception as e:                       # noqa: BLE001
                # A frame that is still being written -- an in-progress render,
                # or a half-downloaded file -- must not sink the whole sequence.
                # Substitute black and report it, so the gap is visible rather
                # than silently skipped (which would misrepresent timing).
                if w is None:
                    raise SequenceError(f"{f.name}: {e}") from None
                bad.append(first + i)
                frames.append(np.zeros((h, w, 4), np.float16))
                if progress and (i % 8 == 0 or i == count - 1):
                    progress(i + 1, count)
                continue
            if w is None:
                h, w = a.shape[:2]
            elif a.shape[:2] != (h, w):
                raise SequenceError(
                    f"{f.name} is {a.shape[1]}x{a.shape[0]}, expected {w}x{h}; "
                    "mixed resolutions in one sequence are not supported")
            frames.append(a)
            if progress and (i % 8 == 0 or i == count - 1):
                progress(i + 1, count)
        self.bad = bad
        with self._lock:
            self._d[k] = frames
            self._d.move_to_end(k)
            while self.bytes > self.cap and len(self._d) > 1:
                self._d.popitem(last=False)
        return frames
