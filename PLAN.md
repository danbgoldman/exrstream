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
| grade + encode (GPU, from RAM cache) | 5–10 ms | **2.7 ms (2K) / 10.3 ms (4K)** |
| transmit one frame @ 15 Mbps | ~30 ms | — |
| network → client | RTT/2 | — |
| WebCodecs decode + present | 10–30 ms | not yet measured |
| **total** | ~100–150 ms | **~175 ms of headroom left** |

The whole server side costs 2.6 ms at 2K. Comfortable even on a poor link. The
margin disappears if any stage touches disk, which is the argument for the RAM
cache.

Within that, the ACES shader itself is 0.12 ms at 2K and 0.42 ms at 4K —
essentially free. The cost is bus traffic: upload 4.5 ms and readback 2.1 ms at
4K, the readback having been 16 ms until it went through a pixel buffer object
(see "Getting the frame off the GPU"). 4K now sustains **97 fps**, three times
realtime, against 45 fps before.

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
2. ~~**Negotiate frame rate automatically.**~~ **Done — see "Two rates" below.**
   The client reports its refresh whenever the measurement moves, the server
   streams at the divisor of that refresh nearest the sequence rate, and the
   source position advances at the sequence rate regardless. Resampling is a
   checkbox, never a default.
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
5. ~~**GL→CUDA interop.**~~ **Done, by a different route.** A pixel buffer
   object gets most of the win with none of the machinery: 4K grade 22.6 → 7.2
   ms, grade+encode 10.3 ms, a 97 fps ceiling. True zero-copy is blocked on
   PyNvVideoCodec, not on us — see "Getting the frame off the GPU".
6. **More pipeline stages.** Extra OCIO transforms appended to the group plus a
   control in the UI — *not* hand-written shader code. OCIO regenerates the
   shader and it stays correct by construction. This is the property that made
   running OCIO's own shader worth it.
7. **A/B compare — done.** See "The A/B wipe" below. Channel isolation, false
   colour and alpha checkerboard are still open, and are now cheaper still: each
   is one more way to fill a pane.
8. ~~**Disk mezzanine + nvdec.**~~ Ruled out by item 3: EXR decode beats
   realtime cold as well as warm. What remains is only a RAM-capacity question
   for sequences too long to cache, which is a different feature.

## Phase 3 — the viewer as an instrument — built

Phase 1 and 2 built a correct picture and put every control in one row above it.
That row was the least considered part of the tool. These are cosmetic in the
sense that none of them changes a pixel, and not cosmetic in the sense that a
review tool is mostly the experience of scrubbing and comparing.

The layout that came out of it: **above the picture, what changes the picture**
(exposure, then A and B as one identical row each). **Below it, where you are in
it** — timeline, transport, sequence rate. Glyphs carry U+FE0E so they render as
text rather than colour emoji.

1. ~~**Transport controls move below the picture**, with the frame rate selector,
   and take standard glyphs: ⏮ ◀ ⏵/⏸ ▶ ⏭. Controls under the image is what every
   player does, and it puts them next to the timeline they act on. What is left
   above is what changes the *picture* — sequence, colourspace, view — which is
   a real distinction rather than a tidy-up: the top row alters what you are
   looking at, the bottom row alters where you are in it.~~ First and last frame
   buttons came along with the glyph set, since ⏮ and ⏭ imply them.

2. ~~**Drop "match display"; warn instead when the display cannot keep up.**
   Resampling is the one control that lies about motion, and since the stream
   rate already repeats frames to hold the sequence at its own speed, it exists
   only to make things smooth by making them wrong. Removing it removes the
   only way to see this footage play smoothly-but-fast, deliberately.

   "Cannot keep up" needs a precise meaning, and it is not the same as "cannot
   present evenly": 24 fps on 30 Hz presents unevenly and is handled honestly by
   repeats, with nothing to warn about. The case that deserves a warning is
   `out_fps < src_fps` — the refresh is *below* the sequence rate, so frames are
   being dropped and you are not seeing the cut. Say which rate the display can
   actually sustain, and that the footage is not being shown in full.~~ So
   24-on-30 is now silent, where it used to explain itself at length: the header
   states both rates and there is nothing wrong. 48-on-30 says which fraction of
   the frames you are not being shown.

3. ~~**Delete the wipe slider.** The seam is dragged on the picture; a second
   control for the same value is a thing to keep in sync for no benefit. Note
   that this books a collision with Phase 4, where drag also means pan; the
   resolution is a grab zone around the seam, and it is Phase 4's problem.~~

4. ~~**A and B become the same control row, twice.** `sequence · in · view` on
   each side, identical widgets, and the compare toggle goes away: B's sequence
   pulldown gains an empty entry, and *B set* is what "compare" means. Empty
   greys out B's look controls and disables wiping. One less piece of state, and
   the state that remains is visible rather than inferred from a highlighted
   button.~~ Server side `compare` is now a property returning
   `frames_b is not None`, so the state cannot disagree with itself, and the
   empty entry in B's picker is the unbind path. Clearing B sends a `state`
   rather than a `ready`: the picture changes, which a flush covers, and there
   is no reason to rebuild the client's decoder for it.

5. ~~**Stats becomes a cog.**~~ It is a settings/diagnostics affordance, not a
   noun.

Not in Phase 3: pan and zoom, which turned out to be a phase of its own, and
anything that changes colour.

## Phase 4 — pan and zoom, properly

Zoom looked like a Phase 3 checkbox and is not. A client-side transform is
responsive and magnifies *decoded* video — interpolated 8-bit 4:2:0 — which is
the confidently-wrong answer this tool exists to prevent. A server-side render
of the region is pixel-accurate and arrives a round trip late. **Both are
required, and the interesting part is the handoff between them.**

### The server renders a region; the frame carries the region it rendered

The encoder is built for one output size and **never changes size**, which is
what makes this cheap: zooming is not a different-sized picture, it is a
different *source rectangle* rendered into the same one. In the shader that is
two uniforms — sample `roi.xy + gl_FragCoord.xy/res * roi.zw` instead of
`gl_FragCoord.xy/res`. No encoder rebuild, no IDR, no bitrate change.

**The region must travel with the frame, in the packet header.** This is the
same trap the pipeline already has a mechanism for: NVENC returns the packet for
the frame pushed three pushes earlier, so a packet labelled with the session's
*current* region would paint the wrong rectangle and the picture would slide
around under a pan. The region belongs in `Meta` and in the header beside
frame/epoch/ev/flags/seq — `<IIfII>` becomes `<IIfII4f>`, 20 bytes to 36 — in
normalised source coordinates, so the client needs to know nothing about the
source size to place it.

### The handoff is the feature

During a gesture the client transforms the last decoded frame at once: correct
framing, stale pixels. The server answers a round trip later with a frame whose
header says which rectangle it actually contains. So **every paint is
`drawImage(frame, srcRect → dstRect)` derived from (region received, region
wanted)**, and the two agree in the steady state. Painting that way rather than
switching modes is what removes the snap when the server catches up: a frame
that is one gesture behind is simply drawn slightly wrong, and the next one is
not.

Gesture discipline is the exposure slider's, unchanged: coalesce during the
drag, flush on release, settle timer as the backstop. A pan needs no new
rate-limiting either, since it changes what is rendered rather than how often.

### Things worth knowing before building it

- **A pan is cheap for the codec and a zoom is not.** Translation is what motion
  vectors are for; scaling is not, so a pinch will cost more bits than a drag of
  the same magnitude. Watch the drag-quality question return here, having been
  correctly dismissed for the exposure slider.
- **Zooming out past fit introduces minification**, which today never happens —
  the output is source-sized and the browser does the shrinking. A region larger
  than the source sampled through `GL_NEAREST` will alias badly, so this wants
  `GL_LINEAR` plus mipmaps, or a floor at fit. Magnification must stay
  `GL_NEAREST`: at 4:1 you want to see pixels, not a smooth lie about them.
- **Pixel-accurate still is not lossless.** A server-rendered region is real
  source pixels, but they still cross an 8-bit 4:2:0 codec at 15 Mbps. The
  natural companion is a lossless still of the region on pause — and at high
  zoom the region is small, so it is cheap. That is the "reference mode" the
  mezzanine section already argues for, arriving from a different direction.
- **A/B pans as one.** The region applies to both panes; comparing two
  differently-framed images is not comparing. This is also where Phase 3's
  deleted wipe slider is paid for: the seam needs a grab zone so a drag near it
  wipes and a drag elsewhere pans.
- **Label the zoom factor.** With two zoom paths that look identical and differ
  in truthfulness, the UI has to say which one is on screen — "2:1" when the
  server has caught up, and visibly provisional when it has not.

### Order

Client-side transform first, with the factor labelled and the limitation stated:
it is most of the usefulness (framing, navigation) for a fraction of the work,
and it is a complete feature on its own. Then the region in the header and the
server render, which turns the same gesture pixel-accurate without changing how
it feels.

## Two rates, and why they are not the same number

Phase 0 found that 24 fps on a 30 Hz display cannot be presented evenly and
fixed it by switching the stream to 30 fps. That fix was a lie: it also ran the
footage 25% fast. Which is fine as an explicit choice and disastrous as a
default in a tool people use to judge motion.

The resolution is that there are two rates and they were being conflated:

- **`src_fps`** — what the sequence is meant to run at. Declared, not detected;
  EXRs carry no frame rate any more than they carry a colourspace.
- **`out_fps`** — what we encode and send. Chosen as `hz / round(hz / src_fps)`,
  the divisor of the display refresh nearest the sequence rate, so every frame
  lands on exactly one refresh.

When they differ, the source position advances by `src_fps / out_fps` per output
frame, so a 24 fps sequence streamed at 30 repeats one frame in five and still
takes exactly as long to play as it would anywhere else. Measured over the
socket: **30.7 fps of stream carrying 24.0 fps of sequence.** That is broadcast
pulldown, and the residual unevenness is inherent to 24-in-30 rather than
something the tool added.

Cases worth knowing, all in `test_rate.py`:

| sequence | display | stream | what happens |
|----------|---------|--------|--------------|
| 24 | 30 Hz | 30 | repeat 1 in 5 |
| 24 | 60 Hz | 30 | repeat 1 in 5 (60 would repeat 1 in 2, no better) |
| 24 | 120 Hz | 24 | nothing; 24 already divides 120 |
| 23.976 | 59.94 Hz | 29.97 | repeat 1 in 5 |
| 48 | 30 Hz | 30 | *drop* frames — faster than the display, so something must go |

**Resampling stays an opt-in checkbox** that advances one source frame per
output frame, and the note then says "1.25x speed" rather than hiding it.

**The refresh is re-reported, not asked once.** Unplugging a laptop halves it,
which is exactly the case that started all this, so the client sends its
measurement whenever the median moves more than 1.5 Hz and the server re-picks
the rate. A new encoder rather than NVENC's `Reconfigure`: rate changes are
rare, `_make_encoder` already exists, and the cost is one IDR the client is
already prepared for. `Reconfigure` is the upgrade if that ever stops being true.

**Known cost, not measured.** Streaming 24 fps content at 30 spends strict-CBR
bits on repeated frames, so each frame gets 62.5 KB where it used to get 78 KB
at 15 Mbps. The repeats are not entirely wasted — NVENC refines the same
picture, improving the reference for the next distinct frame — but quality at a
given bitrate is lower than it was. Raise `--mbps` if it shows.

## The A/B wipe

Nothing was blocking it. The render path already drew a fullscreen triangle into
an FBO, so a wipe is two scissored draws into one buffer and a single readback —
one extra draw, no extra readback, no extra encode, and **no new encoder**,
because a wipe keeps A's output size where a side-by-side would double it and
halve the bits per pixel.

**A pane is a sequence plus a look**, which is the model rather than two special
cases. Change B's sequence and you are comparing two renders; change B's view or
input colourspace and you are comparing two pipelines on the same footage;
change both and you are doing whatever you meant to do. Exposure is shared,
deliberately — a wipe with two exposures compares nothing.

Rules it enforces rather than guesses at, in the style of the rest of the tool:

- **B must be A's frame size.** Refused with the two sizes named, not letterboxed.
- **Past the end of a shorter B, its last frame is held**, and the UI says so.
  Wrapping would look plausible and compare the wrong pair, which is the failure
  mode worth spending a branch on.
- **A B-side open does not move the playhead.** The point is to compare the
  frame you are already looking at.

**The seam is dragged on the picture, and drawn by the client.** Server-side it
would cost bitrate and, worse, put a drawn pixel among the pixels under review;
`paint()` strokes it after `drawImage`, so it never touches the stream. The
canvas is letterboxed by CSS, so the split comes from its rendered box rather
than its pixel width. Pointer capture means the drag survives leaving the
canvas, and the release is the one event that flushes the encoder — the same
discipline the exposure slider uses.

**An invariant worth stating, because breaking it is silent:** every `ready`
makes the client build a fresh `VideoDecoder`, so **every `ready` must be
followed by an IDR**, or that decoder meets a P-frame with no reference and
errors out. A B-side open sends a `ready`, so it rebuilds the encoder even
though the geometry has not changed. Toggling compare deliberately sends a
lightweight `state` message instead, since it needs no new decoder.
`test_server` checks the IDR.

Costs, measured: **+0.97 ms at 2K, +3.86 ms at 4K** for the extra upload and
draw, so 4K with a wipe is 11.1 ms of grade against a 33 ms budget. Both
sequences are resident, so RAM is the real limit — two 4K sequences at 66 MB a
frame.

`test_compare.py` checks it at pixel level: each side matches that side graded
alone, the wipe ends are whole frames, two different views actually differ, and
a short B holds rather than wraps. A wipe that silently shows the same picture
on both sides is worse than no wipe — it answers "no difference" without having
compared anything.

## Getting the frame off the GPU

The ACES shader is 0.58 ms at 4K. Everything else in the chain was bus traffic,
and reading the result back to host memory was 16 ms of it — 70% of the frame.

**A pixel buffer object removes most of it.** `glReadPixels` into a PBO is a
transfer the driver can DMA; `glReadPixels` to a client pointer stalls the
pipeline and copies row by row. Map the PBO, copy once into a reused array,
unmap:

| 4096×2160 | before | after |
|-----------|--------|-------|
| whole grade | 22.6 ms | **7.2 ms** |
| grade + encode | ~22 ms | **10.3 ms** |
| ceiling | 45 fps | **97 fps** |

2K goes 2.3 → 2.0 ms, which matters much less; this is a 4K fix. `test_grade`
covers it without knowing about it, since it compares the shipped grade against
OCIO's CPU processor and a broken readback fails that.

**True zero-copy works and cannot be used.** Registering the PBO with
`cuGraphicsGLRegisterBuffer` and handing NVENC the mapped device pointer takes
the grade to **5.6 ms** and is byte-identical to the host path — measured, not
assumed. It is unusable because `PyNvVideoCodec` refuses every
`__cuda_array_interface__` object:

```
Error Type : incorrect usage of CPU input buffer   at PyNvEncoder.cpp:731
```

That is with `usecpuinputbuffer=False`, with and without an explicit
`cudacontext`, for CuPy arrays and hand-rolled wrappers alike, in every shape
and pixel format, through all three `Encode` overloads, and on **every
published version from 2.0.0 to 2.2.2**. So it is the wheel's device-input path
on this platform, not a version regression and not our call site. The remaining
2.7 ms is one host copy, which also needs NVENC to own the pixels past the
unmap — `GL_MAP_PERSISTENT_BIT` would avoid it but wants GL 4.4 and this
context is 4.3.

Worth retrying when PyNvVideoCodec updates; the GL half is proven and is six
lines away.

## On other hardware: an estimate, not a measurement

Everything here was measured on a GB10, which has **unified memory**. That is
load-bearing in a way it is easy to miss, because two stages of the chain are
host transfers that happen to be nearly free on this machine and would cross
PCIe on a discrete card. Nothing below has been run on one.

Per-stage cost at 4096x2160, measured:

| stage | bytes | GB10 | rate |
|-------|-------|------|------|
| upload half → texture | 71 MB | 3.26 ms | 21.7 GB/s |
| ACES shader | — | 0.58 ms | — |
| fbo → PBO | 35 MB | 0.64 ms | 55.5 GB/s |
| map + host copy | 35 MB | 1.17 ms | 30.2 GB/s |

Those rates *are* the unified memory. On an RTX A6000 (Ampere, PCIe 4.0 x16,
768 GB/s of GDDR6) the chain splits in two directions:

- **Faster:** the shader, on nearly 3x the memory bandwidth — perhaps 0.3 ms.
  And `fbo → PBO`, which stays entirely in VRAM instead of crossing shared
  memory at 55 GB/s: well under 0.2 ms.
- **Slower:** both host crossings. `glTexSubImage2D` from a pageable numpy array
  is the textbook slow PCIe upload, 6–12 GB/s realistic, so 71 MB becomes 6–12
  ms. `glMapBufferRange` on a PBO that now lives in VRAM forces a real
  device→host DMA, so map+copy becomes maybe 3–6 ms. Then NVENC copies that host
  buffer *back* across PCIe, which is nearly free here and another 2–4 ms there.

Estimate: **4K grade+encode 15–25 ms against 10.3 ms here**, so the ceiling
falls from 97 fps to roughly 40–65. Still above the 30 fps a client negotiates,
but the headroom halves. 2K would be 5–7 ms and uninteresting.

Three things that change what you would actually do:

1. **The blocked GL→CUDA interop stops being a nicety.** Here it saves 2.7 ms.
   There it removes *two* PCIe crossings and roughly halves the 4K frame cost,
   so `PyNvVideoCodec`'s broken device-input path becomes the main thing between
   you and 2x at 4K rather than a footnote.
2. **AV1 is gone.** Ampere has no AV1 encoder; that arrived with Ada. The codec
   section above prefers AV1 on this hardware — on an A6000 the choice is HEVC.
   This is the one hard functional difference rather than a performance one.
3. **Upload wants a PBO too**, which it does not here: 21.7 GB/s is already
   near memory speed, so staging it buys nothing on a GB10 and is the classic
   2x win from pageable memory over PCIe. First change to make.

Two things get better. The frame cache stops competing with the GPU for the same
physical memory, which on a GB10 it does — 40 GB of cache and the grade are
drawing on one pool. And cold EXR I/O, the 1.27 GB/s at 24 fps, is a storage
question that does not care what card is fitted.

Less certain than the rest: NVENC engine count on GA102. One, most likely, the
same as here, so the ~20-concurrent-2K-viewer ceiling would not move — but that
is worth checking rather than trusting.

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
