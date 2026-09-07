# exrstream — design plan

Remote review of EXR sequences in a browser. Live exposure slider, ACES output
transform, **10–20 Mbps link**, GPU-rich server, ≤200 ms response to a slider
move.

## Verdict

Feasible, and the bandwidth constraint makes the design *simpler* than it first
appears. Grade on the server, encode display-referred, ship over a low-latency
transport. The GPU assumption you started with is load-bearing after all.

**Phase 0 is complete** — see `spike/RESULTS.md`. A working viewer streams EXR
sequences to a browser over Tailscale with a live exposure slider.

**Slider to pixels: 28 ms p50, 33 ms p95** against the 200 ms budget — 7x margin,
and essentially all of it network round trip (decode 0.1 ms, server 2.5 ms).
NVENC on GB10 has no session cap. Two design changes came out of it: the GPU
grade runs OCIO's own shader rather than a baked 3D LUT, and **the stream frame
rate must match the client's display refresh** (below).

## Why server-side grading is right here

The tempting alternative — ship a log-encoded intermediate and grade in a WebGL
shader — gives an instant slider and free extra viewers. It loses badly at
10–20 Mbps, for three reasons:

1. **A log carrier wastes over half its bits.** It must carry the full ~16-stop
   range at all times; the viewer sees maybe 8–10 stops at any one exposure.
   Display-referred output needs ~8–10 bits for the perceptual quality a log
   intermediate needs 11–12 bits to reach. Grade first and every bit encodes
   something actually on screen.
2. **Codec rate control assumes display-referred input.** Psy-RD and adaptive
   quantisation are tuned for roughly perceptually-uniform data. Log-encoded
   AP1 breaks those assumptions, which is why log footage needs substantially
   *higher* bitrates for equal quality — precisely backwards at 15 Mbps.
3. **4:2:0 is fine on display-referred Rec.709.** It is what 4:2:0 was designed
   for. The chroma artefact that rules it out for log delivery comes from
   subsampling AP1 chroma and *then* stretching it with an exposure push.
   Grading first removes the problem rather than mitigating it.

At 0.28 bpp (2K, 24 fps, 15 Mbps) dither would not survive the DCT anyway, so
the bit-depth tricks that rescue a log carrier at high bitrate are unavailable
at this one.

**Where the precision analysis still applies:** the on-disk mezzanine (below),
which has no bandwidth limit and can afford 10-bit 4:4:4 log at 200–400 Mbps.
The 10-bit ceiling only ever bit where bandwidth was scarce.

## Bitrate sanity

| format | bpp at 24 fps | note |
|--------|---------------|------|
| 2K (2048×1080) @ 15 Mbps | 0.28 | comfortable |
| 4K (3840×2160) @ 20 Mbps | 0.10 | Netflix ships 4K at ~15 Mbps |

Well-understood broadcast territory, and CG review content (clean gradients,
low grain) is easier than live action at the same bitrate.

## Architecture

```
EXR files ──► decode (OIIO, 20 cores) ──► RGBA half frame cache (RAM)
                                                    │
     slider ──► OCIO dynamic property ──────────────┤
                (one uniform, no rebuild)           ▼
                                    OCIO's own ACES 2.0 GLSL  (headless EGL)
                                                    ▼
                                            NVENC (ARGB in, 8/10-bit 4:2:0)
                                                    ▼
                                   WebSocket ──► WebCodecs VideoDecoder
                                                    ▼
                                                 canvas
```

The client is a dumb decoder. All colour lives on the server, so "add more
pipeline stages later" is a server-side change — which is where you want it,
because it also means the pipeline can never drift from what a reference tool
produces.

### The frame cache is the load-bearing piece

Re-reading EXRs on every slider move is the thing that would sink this. Decode
each frame **once** into half-float held in RAM; a parameter change then re-runs
only grade + encode.

Store it as **RGBA half, not RGB half.** The 4th channel is pure padding, but a
3-component texture upload makes the driver repack every row: 1.06 GB/s versus
21 GB/s, i.e. 44 ms versus 2.9 ms for a 4K frame. Pay the padding once at decode.

| resolution | per frame (RGBA half) | 1000 frames |
|------------|----------------------|-------------|
| 2K | 17.7 MB | 18 GB |
| 4K | 66.4 MB | 66 GB |

So a whole shot lives in RAM: ~900 frames of 4K (37 s at 24 fps) in a 60 GB
budget, far more at 2K. Sequences longer than that need tier 2.

### Tier 2: the disk mezzanine — measured away, probably never

This was going to exist because EXR decode looked slow. It is not: **4K ZIP
decodes at 76 fps on a single thread**, 763 fps at 2K, roughly 10× better than
the plan assumed. Cold-open time is a non-problem and nothing needs nvdec.

What survives is only a RAM-capacity question: sequences longer than ~900 4K
frames do not fit. If that day comes, write a 10-bit 4:4:4 log mezzanine via
NVENC on first read and decode it with nvdec — high local bitrate means the
precision analysis above is satisfied with room to spare, and 4:4:4 sidesteps
chroma since no browser is involved. Pair it with a **reference mode** that
fetches the true EXR frame on pause, since the mezzanine is lossy.

Do not build any of this until someone actually opens a sequence that long.
Caveat on the decode numbers: synthetic content compresses well, and real
renders with noise will be slower. Re-measure before deleting this section.

## Match the frame rate to the client's display refresh

This was not in the original plan and is the most important thing Phase 0 found
that was not about colour.

Playback juddered badly for reasons that looked like network jitter and were
not. The client's `requestAnimationFrame` was running at **30 Hz, idle and under
load alike** — the reviewer's MacBook was on battery, and macOS caps ProMotion
refresh on battery. 24 fps content on a 30 Hz clock is 1.25 refreshes per frame
and cannot be presented evenly: paints alternate 33/67 ms. Switching the stream
to 30 fps fixed it outright (paint gap p95 34.4 ms against a 33.3 ms target).

So: **the client reports its measured refresh rate, and the server picks a frame
rate that divides it.** NVENC's `Reconfigure` makes this cheap mid-session.

Do not hide this. A review tool that silently plays 24 fps at the wrong cadence
misrepresents motion, which is exactly the class of wrongness it exists to
prevent. Surface the effective rate in the UI, and say when it does not match
the source.

## Latency budget (200 ms)

| stage | budgeted | **measured** |
|-------|----------|--------------|
| slider → server | RTT/2, 20–40 ms | — |
| grade + encode (GPU, from RAM cache) | 5–10 ms | **2.6 ms (2K) / 22 ms (4K)** |
| transmit one frame @ 15 Mbps | ~30 ms | — |
| network → client | RTT/2 | — |
| WebCodecs decode + present | 10–30 ms | not yet measured |
| **total** | ~100–150 ms | **~175 ms of headroom left** |

The whole server side costs 2.6 ms at 2K. Comfortable even on a poor link. The
margin disappears if any stage touches disk, which is the argument for the RAM
cache.

Within that, the ACES shader itself is 0.12 ms at 2K and 0.42 ms at 4K —
essentially free. The cost is bus traffic: upload 2.9 ms and readback 19 ms at
4K. Reading back to host is the only real inefficiency left, and GL→CUDA
interop (feeding NVENC a device pointer directly) removes it if 4K playback
ever needs headroom. It does not today: 4K sustains 45 fps, nearly 2× realtime.

### The presentation clock, and why the slider must not touch it

Playback and the slider want opposite things, and both are right:

- **Slider (paused):** paint the moment the frame decodes. This is what makes
  28 ms feel instant.
- **Playback:** paint on a clock, draining a small queue. Painting on arrival
  makes network jitter into display jitter one-for-one.

So the slider bypasses the queue **only while paused**. While playing it must
not jump the queue or reset the server's send schedule: a drag fires ~60x/sec,
and doing either tore down the frame cadence for the whole drag while buying
nothing — exposure is applied to the next scheduled frame regardless.

Buffer depth *is* slider latency during playback (6 frames at 33 ms = 200 ms).
That is the knob to trade against smoothness.

Two clock bugs worth not rediscovering: a deadline that is already in the past
cannot catch up by adding one period at a time (it paints on every refresh
instead, draining the queue at refresh rate), and a **fixed** buffer erodes to
zero because the server and browser clocks always drift — the presentation
period has to track queue depth.

### Interaction design: don't send an IDR per slider event

A drag emits ~60 events/s, and an exposure change alters every pixel, so naive
handling means either an IDR storm or enormous P-frames. At 4K/20 Mbps a full
IDR takes several frame times to transmit and blows the latency budget.

Standard remote-desktop answer, and it is one knob:

- **During drag:** coalesce events to ~15–20 fps, raise QP (and optionally
  halve resolution). Quality drops, latency stays bounded, bitrate stays capped.
- **On release:** one IDR at full quality.

Users read this as "it sharpens when I let go", which is the expected idiom.

## Transport: WebCodecs over WebSocket

Send Annex-B / OBU chunks over a WebSocket; client runs `VideoDecoder.decode()`
and paints a canvas. ~100 lines of client code, no SFU, no SDP, no signalling
dance. Latency is one frame plus RTT. It also gives frame-exact scrubbing,
which `<video>` seeking cannot reliably do and a review tool eventually needs.

WebSocket is TCP, so no packet loss to conceal and no FEC — a large
simplification. The cost is that a degrading link builds an unbounded queue
instead of dropping frames, so latency grows without bound.

**Required backpressure knob:** the client acks each frame; the server keeps a
bounded in-flight budget and drops frames (not quality) when it falls behind.
This is not optional — TCP video without it is the classic way to turn a 150 ms
system into a 4 s one on a bad afternoon. Built, but never yet stressed on a
genuinely bad link.

**TCP head-of-line blocking is measurable here, not theoretical.** With arrival
p50 at 33 ms, Phase 0 saw occasional 280 ms stalls and one of 2.1 s. Buffering
hides these only by paying latency; it cannot remove them. That is the concrete
case for WebRTC, which can drop a late packet instead of stalling the stream
behind it. It is off-the-shelf machinery (pion, GStreamer `webrtcbin`), just
much more of it — so still Phase 2, but now for a measured reason.

### Serving it: WebCodecs needs a secure context

`VideoDecoder` does not exist over plain http to a LAN or Tailscale IP.
`localhost` is exempt, so an ssh tunnel works (`-N`, or the session exits
immediately) — but a **self-signed cert with a click-through is simpler and
better, because it measures the real network path.** Also: Annex-B needs in-band
SPS/PPS (`repeatspspps=1`) and no `description` in `configure()`, and each
connection needs a fresh encoder or a page reload joins mid-stream on a P-frame
and the decoder errors out.

## Multi-viewer

Server-side grading means one encoder session per *distinct parameter set*.

**GB10 has no concurrent-session cap** — 64 simultaneous 1080p sessions opened
and encoded without error, so it behaves like a professional part rather than a
consumer one (which historically capped at 3–8). The worry that shaped this
section is gone.

The real ceiling is engine throughput on the single NVENC engine: roughly
**20 concurrent 2K viewers, 6 at 4K HEVC, 9 at 4K AV1**.

**Presenter mode** — one person drives exposure, everyone else receives the
same stream — is therefore an optimisation rather than a workaround. It is
still worth having, because it is what a review session already is and it turns
N viewers into one encode. Just no longer load-bearing.

## Implementation stack

All of this is installed and measured; see `spike/`. **No ffmpeg anywhere** —
PyNvVideoCodec talks to NVENC directly and bundles libav* for muxing. Every
package came from an aarch64 wheel with no compilation.

- **EXR decode:** OpenImageIO, parallel across frames. Do not exceed ~4 workers;
  it is memory-bandwidth bound and more threads make it slower.
- **Grade on GPU: OCIO's own generated GLSL, not a baked LUT.** A 3D LUT cannot
  represent the ACES 2.0 output transform — its gamut compressor clips channels
  to exactly 0 along a surface in the colour cube, and trilinear interpolation
  smears that discontinuity with O(1/n) convergence (129³ still misses by
  33/255; 2 codes would need n≈2000). OCIO emits ~326 lines of real ACES 2.0
  math plus two small 1D tables and no 3D LUT, which is exact to 1 code.
- **Exposure: an OCIO dynamic property**, i.e. a plain uniform. A slider move
  sets one float — no shader rebuild, no LUT re-bake. Later pipeline stages are
  extra transforms in the group; OCIO regenerates the shader and it stays
  correct by construction. The LUT design would have lost this.
- **Headless GL:** PyOpenGL over the runtime libEGL, using the EGL
  device-platform extension. No X server, no `-dev` packages, no `sudo`. Gives
  GL 4.3 on the GB10. See `spike/egl_ctx.py`.
- **Encode:** PyNvVideoCodec, `ARGB` input. `Reconfigure` and `FORCEIDR` exist,
  which is exactly what drag-quality / release-IDR needs.
- **Output codec:** AV1 is fastest *and* best-looking at these rates on this
  hardware, so the choice now rests on client decode support, not the server.
  H.264 8-bit remains the universal floor.
- **Server:** whatever HTTP/WS framework is to hand. One WS endpoint, one job
  per session, one cache. It needs no framework opinion.

### Two traps worth keeping

1. `getProcessor("ACEScg", "sRGB - Display")` applies **no tone mapping** — it
   is a colourspace conversion, so 0.18 grey lands at 0.46 instead of 0.35 and
   saturated colours come back as `[1.219, 7.275, -3.026]`. The ACES output
   transform lives on the **view**, via `DisplayViewTransform`. A viewer built
   on the colourspace form ships with no filmic rolloff at all.
2. `glReadPixels(GL_BGRA)` on an RGBA framebuffer lands as B,G,R,A, which *is*
   NVENC's ARGB. Do not also swizzle in the shader; the two cancel.

## Phase 0 — complete

Full numbers in `spike/RESULTS.md`. Run the viewer with
`./spike/serve.sh restart --frames 48 --tls --fps 30`.

1. ~~NVENC on GB10, and session count~~ — **done.** H.264/HEVC/AV1, 10-bit,
   4:4:4, up to 8192². No session cap (64 tested). ~20 concurrent 2K viewers.
2. ~~End-to-end latency~~ — **done, both halves.** 28 ms p50 / 33 ms p95 slider
   to pixels over real Tailscale.
3. **Quality at rate** — outstanding, and needs *real* footage. Synthetic
   gradients are kinder than film grain. Also evaluate the drag-time QP bump
   visually.
4. ~~EXR decode throughput~~ — **done**, ~10× better than assumed. Re-measure
   on noisy renders.
5. ~~Round-trip colour~~ — **done.** GPU matches OCIO's CPU processor to 1 code
   across EV −6…+6, bar 17 pixels in 2.2M where OCIO's own CPU and GPU paths
   disagree on a physically implausible colour.

Still unmeasured: quality at rate on real footage, backpressure under a
genuinely degrading link, real EXR variety (multi-part, AOV layers, mismatched
data/display windows), and more than one concurrent viewer.

## Phase 1 — built

`exrstream/` (the spike stays in `spike/` as the record of what was measured).
Run it: `./run.sh --root /path/to/exrs --tls`, then open the https URL.

| module | what it owns |
|--------|--------------|
| `gl.py` | EGL context, OCIO shader grade, shared `grade_for()` pool |
| `seq.py` | sequence discovery, real-EXR reading, LRU frame cache |
| `server.py` | sessions, playback pump, WebSocket protocol |
| `static/index.html` | viewer, presentation clock, WebCodecs decode |
| `test_seq.py`, `test_server.py` | runnable checks |

Done: sequence discovery and picker; per-connection sessions (3 concurrent
viewers verified, independent exposure, shared cache); frame-exact seek and
step; input colourspace and view selection; LRU frame cache keyed by
`(path, mtime)`; frame-rate selection with a cadence warning; loading progress;
real-EXR handling (named channels, data/display windows, clear refusals).

Deliberately skipped, with the reason:

- **Presenter mode** — was a workaround for an NVENC session cap that does not
  exist on GB10. Still a nice optimisation; no longer needed.
- **Drag-quality / release-IDR** — the justification was an IDR storm during
  drags. We never force an IDR per event, and a slider move costs 2.5ms, so
  there is nothing to protect against. Revisit only if 4K over a thin link
  says otherwise.
- Accounts, ABR ladders, plugin system, database, WebRTC, disk mezzanine.

The original Phase 1 list, for reference:

- WS session: client opens a sequence, server decodes into the RAM cache,
  streams graded frames.
- **Negotiate frame rate against the client's reported display refresh**, and
  show the effective rate in the UI.
- Controls: exposure, play/pause, scrub, frame step. Scrub is exact — the
  server sends the frame you asked for.
- Drag-quality / release-IDR behaviour from above.
- Backpressure loop with the client's displayed-frame reports.
- LRU eviction on the frame cache, keyed by `(path, mtime)`.
- Presenter mode, since it is nearly free and defuses the session limit.

Explicitly **not** in Phase 1: accounts, ABR ladders, a plugin system for
pipeline stages, a database, WebRTC, the disk mezzanine.

## Validated on real footage

Tears of Steel original camera footage, (CC) Blender Foundation |
mango.blender.org, from `media.xiph.org/tearsofsteel/tearsofsteel-footage-exr/`.
Shot 01_1a in both 1920x1012 PIZ and 4096x2160 uncompressed.
Fetch more with `./fetch-footage.sh <shot> <linear|linear_hd> <n>`.

| | HD 1920x1012 (PIZ) | 4K 4096x2160 (uncompressed) |
|-|--------------------|------------------------------|
| on disk | 6.2 MB/frame | 50.7 MB/frame |
| decode, 1 thread | 133 fps | **21 fps — below realtime** |
| decode, 8 threads | — | 65 fps |
| cold open, 48 frames | <1 s | 3.1 s |
| slider -> packet | 3.3 ms | 25.6 ms |
| playback | 24.0 fps | 24.0 fps at exactly 20.0 Mbps |

Four things synthetic content hid:

1. **CBR overshoots the target by ~16% on real detail.** 15 -> 17.4 Mbps on
   water ripples, where synthetic gradients stayed within 3%. At a 10-20 Mbps
   budget that silently exceeds the link. `vbvbufsize` (1 frame) holds the rate
   exactly *and* caps peak frame size 1.40 -> 0.63 Mbit, which halves worst-case
   transmit time, so it buys latency as well. `maxbitrate` does nothing.
2. **Decode threading advice was backwards for uncompressed footage.** PIZ/ZIP
   flattens past ~4 workers *warm* (memory-bandwidth bound); uncompressed 4K
   needs 8+ just to beat realtime. Hence `--workers 8`. Cold from disk the
   flattening does not happen at all — see Phase 2 item 3.
3. **Frames that cannot be read must not sink the sequence.** A render still
   being written -- or, here, a half-downloaded file -- now renders black with
   the count and position reported, rather than failing the whole load or being
   silently skipped (which would misrepresent timing).
4. **EXRs carry no colourspace metadata**, so it cannot be detected. This
   footage is `Linear Rec.709 (sRGB)`, not ACEScg -- the Sony F65 raw went to
   ACES, then to Rec.709 scene-linear for the movie pipeline. Hence `--src`,
   and the per-session selector in the UI.

### Cold from disk, measured

The figures above are page-cache-warm. Cold (`posix_fadvise(DONTNEED)` on every
file first), 48 frames of uncompressed 4K through `FrameCache.load`:

| workers | cold open | fps | GB/s |
|---------|-----------|-----|------|
| 1 | 4.62 s | 10.4 | 0.55 |
| 4 | 1.53 s | 31.4 | 1.67 |
| 8 | **0.95 s** | **50.3** | **2.67** |
| 16 | 0.79 s | 60.7 | 3.22 |

Realtime is 1.27 GB/s. **A single reader gets 1.24 GB/s of raw sequential read
and 0.55 GB/s through the decoder — both under it.** Concurrency is what buys
the margin, and it is queue depth doing the work, not CPU: raw `read()` with no
decoding at all is only 1.24 GB/s cold against 12.5 GB/s warm.

So `--workers` is load-bearing cold in a way it was not warm, and it helps PIZ
too (HD cold: 42 fps at 1 worker, 164 at 8) — the "flattens past ~4" advice was
a warm-cache artefact. Diminishing past 8, which stays the default.

This also settles the item it was there to gate: **the disk mezzanine is not
needed.** Cold 4K decode is 2× realtime.

## Phase 2 — ranked by evidence

Phase 1 works and is committed. These are ordered by how much measurement backs
them, not by appeal.

1. ~~**WebRTC transport.**~~ **Built, measured, removed.** A data channel
   carrying the same packets loses to the WebSocket on the real link — see
   "WebRTC, measured and rejected" below. The defect that motivated it is still
   real; this answer to it is not.
2. **Negotiate frame rate automatically.** The client already measures its
   refresh and the server already warns when the rate cannot be presented
   evenly, but choosing the rate is still manual. NVENC's `Reconfigure` makes
   changing it mid-session cheap. Offer resampling explicitly, never as a
   default — it alters motion timing, which is a lie about the footage.
3. ~~**Cold 4K I/O**~~ — **done**, and it found a real defect: `FrameCache.load`
   decoded serially, so `--workers` only ever parallelised *across* sessions and
   a cold 4K open ran at 10.4 fps (0.55 GB/s) against a 1.27 GB/s realtime
   requirement. Decoding the frames concurrently takes it to 50.3 fps / 2.67
   GB/s, cold-opening 48 4K frames in 0.95 s instead of 4.62 s. Numbers above.
   It also closes item 8.
4. **More than ~3 concurrent viewers.** All sessions share one event loop and
   one GL context, so encodes serialise: 3 concurrent 2K viewers ran 21–24.5 fps
   each and it degrades from there. Fix is a render thread per session, each
   with its own `eglMakeCurrent`.
5. **GL→CUDA interop.** Removes the ~19 ms host readback that dominates the 4K
   chain (upload 2.9 ms, ACES shader 0.4 ms). Only worth it once 4K playback
   needs the headroom; it does not today.
6. **More pipeline stages.** Extra OCIO transforms appended to the group plus a
   control in the UI — *not* hand-written shader code. OCIO regenerates the
   shader and it stays correct by construction. This is the property that made
   running OCIO's own shader worth it.
7. **A/B compare, channel isolation, false colour, alpha checkerboard.** All
   cheap once the above exists.
8. ~~**Disk mezzanine + nvdec.**~~ Ruled out by item 3: EXR decode beats
   realtime cold as well as warm. What remains is only a RAM-capacity question
   for sequences too long to cache, which is a different feature.

## WebRTC, measured and rejected

WebRTC was top of the Phase 2 list for one measured reason — TCP head-of-line
blocking, seen as 280 ms stalls and one of 2.1 s — and not because the stream
needed to become a media track. So it was built as the small version: an
`RTCDataChannel` carrying the identical header + Annex-B packets, NVENC still
feeding WebCodecs directly, the WebSocket kept for control and signalling.

**It lost.** Same session, same 30 fps, same link (22 ms RTT), one click apart:

| target 33.3 ms | WebSocket | data channel |
|----------------|-----------|--------------|
| arrival gap p50 | **33.0** | 41.9 |
| arrival gap p95 | **39.6** | 81.0 |
| paint gap p95 | **34.3** | 67.4 |
| underruns | **5** | 30 |
| packets lost | 0 | 0 |

aiortc delivers 14.5 Mbps against a 15 Mbps target, so the average is right and
the distribution is not: p50 and p95 sit near 2x and 4x the round trip, which is
what congestion-window-limited delivery looks like — bytes arriving in
round-trip-quantised bursts rather than paced. **On loopback the two transports
were indistinguishable** (p95 42.4 vs 42.5 ms), because with RTT near zero the
window never binds. Every measurement made on this machine was therefore blind
to the only thing that mattered.

So it is deleted, along with `aiortc` and the PyAV it drags in. What that
verdict does *not* say: WebRTC is wrong. It says a pure-Python SCTP stack
cannot pace 15 Mbps over a real round trip. A native implementation — GStreamer
`webrtcbin` with a real media track — would not have this problem, at the cost
of giving up NVENC straight into WebCodecs and being a far larger build. If the
280 ms stalls ever become the binding constraint, that is the version to build.

Three things learned on the way, which outlive the transport:

- **A window sized by counting acks against sends leaks.** Every ack that does
  not arrive costs a slot permanently, and a few of those stop the pump for
  good — measured as 25 s arrival gaps and 477 underruns. Acks carry the
  sequence number and the window is `seq_no - acked`, so a later ack forgives
  the ones that never came. This survived the deletion: on the WebSocket a
  frame the decoder *rejects* is the same leak, which is why the client now
  acks before decoding rather than after.
- **Loss shows up as blockiness, not as corruption.** Every lost frame costs a
  forced IDR, and under strict CBR with a one-frame VBV an IDR gets no more
  bits than a P-frame, so it lands visibly blocky. One a second passes
  unnoticed; sixteen extra in 414 frames does not.
- **`maxRetransmits: 0` is the wrong knob for video at this frame size.** One
  frame is ~78 KB, which SCTP puts on the wire as ~65 UDP datagrams, so
  refusing retransmits loses the frame to any one of them — 4% of frames on a
  link with bandwidth to spare. `maxPacketLifeTime` says the useful thing
  instead: recover it if it can still be shown, abandon it if it cannot.

And one about measuring: the client recorded arrival gaps while **paused**,
where the server sends only on changes, so "max 19728 ms" was the length of
time nobody touched anything. It sent a debugging session chasing stalls that
were not there. Gaps are now recorded only during playback.

## Rejected

- **Client-side grading via a log intermediate** — wastes half the bitrate on
  off-screen range, fights the codec's rate control, and forces subsampled AP1
  chroma through an exposure stretch. Correct above ~60 Mbps, wrong at 15.
- **HLS / DASH / LL-HLS** — 2–6 s latency against a 200 ms budget, and no
  frame-exact scrub.
- **Pre-baked exposure variants** — combinatorial, and wrong the moment a
  second slider appears.
- **IDR per slider event** — several frame times to transmit at 4K; QP
  modulation during drag is the cheaper, standard answer.
- **8-bit log anything** — 4.4% code-value steps, contours under any push.
  (Display-referred 8-bit is fine; the two are not the same claim.)

## Open risks

1. ~~NVENC on GB10~~ — closed. No cap; throughput ceiling only.
2. ~~aarch64 packaging~~ — closed. Everything installed from wheels, no ffmpeg.
3. ~~The browser half of the loop~~ — closed for H.264 8-bit. 10-bit support
   across target browsers is still untested, but display-referred 8-bit is
   adequate, so this is an upgrade rather than a risk.
4. **Backpressure discipline.** Without it, TCP transport quietly converts a
   150 ms system into a multi-second one under load. Build it in Phase 1, not
   after the first complaint.
5. ~~Cold-open time on 4K~~ — closed. 2× realtime cold from disk at 8 decode
   workers; below realtime at one, which is why `load` decodes concurrently.
6. **EXR variety**: multi-part files, AOV layers, non-RGB channels, mismatched
   data/display windows. Phase 1 handles single-part RGB(A) and refuses the
   rest with a clear message.
7. **OCIO CPU/GPU divergence** on colours far outside the display gamut. Ours
   is the GPU answer; bounded by a self-check at <0.005% of pixels.
