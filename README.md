# exrstream

View EXR sequences in a browser over a thin link. Server-side ACES grade on the
GPU, NVENC encode, WebCodecs decode. Built for remote review at 10–20 Mbps with
a live exposure slider.

**Measured: 28 ms from slider to pixels over Tailscale** (decode 0.1 ms, server
2.5 ms — the rest is network round trip).

## Run

```bash
./run.sh --root /path/to/exr/sequences --tls      # start (PID file, logs to /tmp/exrstream.log)
./run.sh stop
```

Then open `https://<host>:8099/` and accept the self-signed certificate.

**HTTPS is not optional.** WebCodecs' `VideoDecoder` is `[SecureContext]` and
simply does not exist over plain http to a LAN or Tailscale IP. `localhost` is
exempt, so `ssh -N -L 8099:localhost:8099 user@host` also works — but the
self-signed cert is simpler and measures the real network path.

Options: `--codec h264|hevc|av1`, `--mbps`, `--port`, `--cache-gb`, `--workers`.

## Design

`PLAN.md` is the design and the reasoning; `spike/RESULTS.md` is every Phase 0
measurement. The three findings that shaped it:

1. **Grade on the server, not the client.** At 10–20 Mbps a log intermediate
   wastes over half its bitrate on off-screen range and fights the codec's rate
   control. Display-referred output also makes 4:2:0 chroma harmless.
2. **Run OCIO's own shader, not a baked 3D LUT.** ACES 2.0's gamut compressor
   clips channels to exactly 0 along a surface in the colour cube; trilinear
   interpolation smears that discontinuity with only O(1/n) convergence.
3. **Match frame rate to the client's display refresh.** 24 fps on a 30 Hz
   panel is 1.25 refreshes per frame and cannot be presented evenly. The app
   warns rather than silently misrepresenting motion.

## Test footage

```bash
./fetch-footage.sh 01_1a linear_hd 240 0 footage/tos_hd   # 1920x1012 PIZ
./fetch-footage.sh 01_1a linear    72 0 footage/tos_4k    # 4096x2160 uncompressed
./run.sh restart --root footage --tls --src "Linear Rec.709 (sRGB)"
```

Tears of Steel original camera footage, (CC) Blender Foundation |
mango.blender.org. It is Rec.709 scene-linear, **not** ACEScg -- and EXRs carry
no colourspace metadata, so this cannot be detected. Set `--src` to match your
footage or pick it in the UI.

## Tests

```bash
.venv/bin/python -m exrstream.test_seq        # channel + window handling
.venv/bin/python -m exrstream.test_grade      # grade vs OCIO's CPU processor
.venv/bin/python -m exrstream.test_pipeline   # NVENC pipeline-lag regression
.venv/bin/python -m exrstream.test_server     # protocol, needs a running server
```

## Requires

NVIDIA GPU with NVENC, and a GPU-capable EGL driver (no X needed). Developed on
a GB10 (aarch64); every dependency installs from wheels and ffmpeg is not used.
