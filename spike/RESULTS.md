# Phase 0 results

Measured on the GB10 (aarch64 DGX Spark, 121 GB unified, 20 cores, driver
580.159.03, CUDA 13). Synthetic test content: 20-stop exposure ramp, a
12000-nit sun, and a band of saturated AP1 primaries — deliberately harder than
real footage. Re-measure on real renders before trusting the decode numbers.

## Verdict: the architecture holds, with one design change

Server-side grading at 10–20 Mbps is confirmed. Every latency and throughput
target is met with large margin. **But the GPU grade must run OCIO's own
shader, not a baked 3D LUT** — see "3D LUT" below.

## 1. NVENC on GB10 — yes, and unrestricted

| codec | max res | 10-bit | 4:4:4 | lossless |
|-------|---------|--------|-------|----------|
| H.264 | 4096²   | yes    | yes   | yes      |
| HEVC  | 8192²   | yes    | yes   | yes      |
| AV1   | 8192²   | yes    | no    | no       |

One encoder engine. **No concurrent-session limit** — 64 simultaneous 1080p
sessions opened and encoded without error, so GB10 behaves like a professional
part, not a capped consumer one.

Presenter mode is therefore an optimisation, not a workaround. The real ceiling
is engine throughput: ~20 concurrent 2K viewers, ~6 at 4K HEVC, ~9 at 4K AV1.

### Throughput and latency

| codec | 2K sustained | 4K sustained | 4K flush |
|-------|-------------|--------------|----------|
| H.264 | 664 fps | 194 fps | 8.6 ms |
| HEVC  | 483 fps | 140 fps | 11.2 ms |
| AV1   | 758 fps | 220 fps | 8.5 ms |

NVENC buffers 3 frames before emitting, on every tuning preset. This is *not*
3 frame-intervals of latency: pushing back-to-back, a still flushes in 2.5 ms
at 2K and 8.5–11 ms at 4K. It only costs real time during playback, where a
parameter change appears ~3 frames later. Since people ride an exposure slider
while **paused**, the pipeline can be pushed at engine speed and the delay
effectively vanishes.

Rate control lands within 3% of target (AV1 at 4K overshoots to 23 Mbps on this
content). `Reconfigure` and `FORCEIDR` both exist — exactly the primitives the
drag-quality / release-IDR design needs.

AV1 is both the fastest and the best-looking at these bitrates. Codec choice
now rests on client decode support, not server capability.

## 2. aarch64 packaging — a non-issue

`PyNvVideoCodec`, `OpenImageIO`, `OpenColorIO`, `CuPy` (cuda13x) and `PyOpenGL`
all installed from wheels with no compilation. **No ffmpeg needed anywhere** —
PyNvVideoCodec talks to NVENC directly and bundles libav* for muxing. Risk #2
from the plan is closed.

OCIO 2.5 ships **built-in ACES 2.0 configs**
(`ocio://cg-config-v4.0.0_aces-v2.0_ocio-v2.5`), so there is no config file to
ship or manage.

## 3. EXR decode — much faster than estimated

| sequence | 1 thread | best |
|----------|----------|------|
| 2K ZIP   | 763 fps (1.3 ms) | 763 fps |
| 4K ZIP   | 76 fps (13.2 ms) | 120 fps @ 4 threads |
| 4K DWAB  | 31 fps (32.4 ms) | 106 fps @ 20 threads |

The plan assumed 15–40 ms at 2K and 60–150 ms at 4K. Reality is ~10× better:
**4K ZIP beats 24 fps realtime on a single thread.**

Consequence: the tier-2 disk mezzanine and nvdec are no longer justified by
throughput at all. They remain only a RAM-capacity question for sequences too
long to cache. Threading past ~4 workers does not help and sometimes hurts —
memory-bandwidth bound, not CPU bound.

Caveat: synthetic content compresses well. Real renders with noise will be
slower. Re-measure before removing the mezzanine from the roadmap for good.

## 4. The grade — a 3D LUT does not work here

The obvious design (bake ACES to a shaper 1D + 3D LUT, apply with CuPy) fails,
and it is worth recording why.

**ACES 2.0's gamut compressor clips channels to exactly 0 along a surface in
the colour cube.** Trilinear interpolation smears that discontinuity, so error
falls only as O(1/n):

| lattice | max error (8-bit codes) |
|---------|------------------------|
| 65³  | 42 |
| 129³ | 33 |
| 193³ | 18 |
| 257³ | 22 |

Reaching 2 codes would need n≈2000. Mean and p99 were fine (0.05 / 1.4); the
failures are 0.3–0.6% of pixels, all on saturated colour, which is exactly
where a review tool gets distrusted. Tetrahedral interpolation would reduce but
not remove this — the function is discontinuous, not merely curved.

### What works: OCIO's own shader, headless

OCIO emits ~326 lines of real ACES 2.0 GLSL plus two 363-entry 1D tables and
**no 3D LUT**. Running it directly is exact:

| | mean | p99.9 | max | pixels >2 codes |
|-|------|-------|-----|-----------------|
| EV −6 … +2 | 0.017–0.023 | 1.0 | 1.0 | 0 |
| EV +6 | 0.022 | 1.0 | 255 | 17 (0.0008%) |

The 17 outliers are a genuine OCIO **CPU-vs-GPU divergence**, not our bug and
not exposure-related: AP1 green at 250× mid-grey is linear Rec.709
`[-155.6, 285.9, -32.2]`. CPU renders it white, GPU keeps hue. Physically
implausible content; documented and bounded by the self-check.

Headless GL works with **no X server and no -dev packages** (none are installed
and sudo wants a password): PyOpenGL is ctypes over the runtime libEGL, and the
EGL device-platform extension binds the GPU directly. Gives GL 4.3 on
`NVIDIA GB10/PCIe`.

**Exposure is an OCIO dynamic property**, i.e. a plain uniform. A slider move
sets one float — no shader rebuild, no LUT re-bake. Extra pipeline stages later
are extra transforms in the group; OCIO regenerates the shader and it stays
correct by construction. This is the property that makes "add stages later"
cheap, and the LUT design would have lost it.

### The bug worth remembering

`config.getProcessor("ACEScg", "sRGB - Display")` applies **no tone mapping**.
It is a colourspace conversion — primaries matrix plus transfer function — so
0.18 grey lands at 0.46 instead of 0.35 and saturated colours come back as
`[1.219, 7.275, -3.026]`. The ACES output transform lives on the **view**:

```python
ocio.DisplayViewTransform(src="ACEScg", display="sRGB - Display",
                          view="ACES 2.0 - SDR 100 nits (Rec.709)")
```

Every "gamut" anomaly in the first LUT experiments traced back to this. A
viewer built on the colourspace form would have shipped with no filmic rolloff
at all.

## 5. GL transfer costs — where the time actually goes

The ACES shader itself is nearly free: **0.12 ms at 2K, 0.42 ms at 4K.**
Everything else is bus traffic.

| path | 2K | 4K | note |
|------|----|----|------|
| upload RGB16F | 11.7 ms | 43.7 ms | 1.06 GB/s — driver repacks 3-component rows |
| upload RGBA16F | 0.79 ms | 2.91 ms | **21 GB/s — 20× faster** |
| upload via PBO | 1.81 ms | 6.71 ms | slower than direct; not worth it |
| readback RGB | 0.50 ms | 2.34 ms | |
| readback BGRA | 5.17 ms | 19.1 ms | but this is what NVENC's ARGB needs |

So the frame cache stores **RGBA half**, padding paid once at decode. 33% more
memory for a 20× faster upload.

Readback is now the bottleneck at 4K. Reading RGB and padding on the CPU gives
it all back (20.6 ms), so there is no host-side win. The fix is GL→CUDA interop
feeding NVENC a device pointer and never touching host memory — worth doing
when 4K playback needs headroom, unnecessary for the 200 ms budget.

Byte-order trap: `glReadPixels(GL_BGRA)` on an RGBA framebuffer lands as
B,G,R,A, which *is* NVENC ARGB. Do not also swizzle in the shader; the two
cancel.

## 6. Full pipeline: EXR cache → grade → NVENC

| | playback | slider (paused) | bitrate |
|-|----------|-----------------|---------|
| 2K h264 | 398 fps | 2.6 ms | 14.8 Mbps |
| 2K hevc | 400 fps | 2.6 ms | 15.8 Mbps |
| 2K av1  | 407 fps | 2.5 ms | 14.6 Mbps |
| 4K h264 | 44 fps  | 23.0 ms | 17.8 Mbps |
| 4K hevc | 45 fps  | 21.7 ms | 18.8 Mbps |
| 4K av1  | 46 fps  | 20.5 ms | 23.0 Mbps |

**Server-side cost of a slider move: 2.6 ms at 2K, ~22 ms at 4K.** Against a
200 ms budget that leaves ~175 ms for network round trip and client decode,
which is generous even on a poor link.

Playback at 4K sustains 45 fps with the cache warm — nearly 2× realtime, so
decode can run in parallel workers without missing frames.

## 7. Browser half — measured, over real Tailscale

`spike/10_server.py` + `spike/static/index.html`: NVENC output over a WebSocket
into WebCodecs, painted to a canvas. Measured from a MacBook Pro over Tailscale,
not localhost.

| | measured |
|-|----------|
| slider -> painted, **paused** | **28.1 ms p50, 32.7 ms p95** |
| slider -> painted, **playing** | 200 ms (= the presentation buffer, by design) |
| WebCodecs decode | 0.1 ms |
| paint gap | p50 33.3, p95 34.4 ms (target 33.3) |
| server send gap | p50 33.3, p95 36.1 ms |
| packets per send | 1.00 (no duplicate frames) |

**28 ms against a 200 ms budget, 7x margin, and essentially all of it is network
round trip** — decode is 0.1 ms and the server is 2.5 ms. There is nothing to
optimise on either endpoint; the budget is travel time.

The playing-mode figure is the jitter buffer being counted honestly, not a
regression: 6 frames at 33.3 ms is 200 ms. Buffer depth *is* the slider latency
during playback, so it is the knob to turn if that matters more than smoothness.

### The finding that mattered most: match frame rate to display refresh

Playback juddered badly and none of the obvious causes were real. The instrument
that settled it was measuring rAF in **milliseconds** rather than counting
refreshes, and measuring it while **idle** as well as while playing:

```
rAF playing p50 33.3 ms -> 30 Hz    rAF idle p50 33.3 ms -> 30 Hz
```

The panel was at 30 Hz, idle and loaded alike — so not our rendering cost. The
laptop was **on battery, and macOS caps ProMotion refresh on battery.** 24 fps
content on a 30 Hz clock is 1.25 refreshes per frame and *cannot* be presented
evenly; paints alternate 33/67 ms. Switching the stream to 30 fps made it
smooth immediately (paint gap p95 34.4 against a 33.3 target).

This is a real design requirement, not a lab artifact: **the client must report
its measured refresh rate and the server must pick a frame rate that divides
it.** NVENC's `Reconfigure` makes changing rate mid-session cheap. A review tool
that silently plays 24 fps at the wrong cadence misrepresents motion, which is
precisely the kind of wrongness it exists to prevent — so surface it in the UI
rather than hide it.

### Presentation clock: three bugs worth remembering

1. **Paint-on-arrival.** Painting each frame as it decodes is what makes the
   slider feel instant and is exactly why playback judders — arrival jitter
   becomes display jitter one-for-one. Playback needs a clock; the slider needs
   to bypass it. They are opposite requirements and both are right.
2. **A deadline that cannot catch up.** On underrun, `nextPresent += period`
   leaves an already-past deadline in the past, so every subsequent rAF tick
   paints and the queue drains at refresh rate. Symptom: refreshes-per-frame
   collapses to 1x, then underruns, then a burst. Needs a hard resync when more
   than ~2 periods behind.
3. **A fixed buffer erodes to zero.** The server clock and the browser clock are
   independent and always drift; arrival p50 41.8 ms against a nominal 41.67 ms
   drain is enough to empty a 6-frame buffer, after which every jitter spike
   underruns. Fix is an adaptive period tracking queue depth (run ~6% slow when
   short, ~6% fast when deep). Queue then sits near target instead of pinned at 0.

Also: while **playing**, a slider move must *not* jump the presentation queue or
reset the server's send schedule. A drag fires ~60x/sec, and doing either tore
down the frame cadence and made playback janky for the whole drag. It buys
nothing, because exposure is applied to the next scheduled frame regardless.

### WebCodecs practicalities

- **Secure context is mandatory.** `VideoDecoder` does not exist over plain http
  to a LAN or Tailscale IP. `localhost` is exempt (ssh `-L`, and note `-N` or the
  session exits immediately), but a **self-signed cert with a click-through is
  simpler and better** — it measures the real network path, which a localhost
  tunnel does not. Tailscale HTTPS certs were unavailable on this tailnet.
- **Annex-B needs in-band SPS/PPS** (`repeatspspps=1`), and no `description` in
  `configure()`.
- **A fresh encoder per connection.** Otherwise a page reload joins mid-stream,
  hits a P-frame with no preceding IDR, and the decoder simply errors.

### Transport: TCP head-of-line blocking is real here

Arrival gaps showed occasional **280 ms, and once 2.1 s**, stalls while p50 sat
at 33 ms. Buffering hides these only by paying latency; it cannot remove them.
This is the concrete argument for the WebRTC transport listed in Phase 2 — it
can drop a late packet instead of stalling the whole stream behind it.

## What is still unmeasured

1. **Quality at rate on real footage** — synthetic gradients are kinder than
   film grain. Also the drag-time QP bump has not been evaluated visually.
2. **Backpressure behaviour** under a genuinely degrading link (the ack-based
   limiter exists and was never stressed).
3. **Real EXR variety** — multi-part, AOV layers, mismatched data/display
   windows, and decode speed on noisy renders.
4. **More than one concurrent viewer.** Everything so far is single-session.

## Artifacts

- `spike/01_nvenc_caps.py` … `09_demo.py` — each runnable standalone
- `spike/egl_ctx.py` — headless GL context, no X, no dev headers
- `spike/07_ocio_gpu.py` — the grade; `_selftest()` asserts against OCIO CPU
- `testdata/frame_ev0.png`, `testdata/sweep.264` — EV −4→+4 sweep, 96 frames
- `spike/10_server.py`, `spike/static/index.html` — the streaming viewer
- `spike/serve.sh` — start/stop by PID file (`restart`, `stop`). Do not use
  `pkill -f 10_server.py`: the pattern matches the invoking shell's own command
  line and kills the caller.
- `spike/11_wsprobe.py` — headless protocol/bitstream check
- `spike/restart-llama.sh` — restores the llama-server stopped for these runs
  (mode 700; it carries an API key from the process command line)
