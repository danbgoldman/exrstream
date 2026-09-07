# Working on exrstream

Operational notes for agents and contributors. `README.md` is the human-facing
overview; `PLAN.md` is the design and the reasoning behind it; `spike/RESULTS.md`
is every Phase 0 measurement with the numbers that justify each decision.

## Setup

Neither the venv nor the dev certificate is tracked, so a fresh clone needs
both before `--tls` will start:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout spike/dev-key.pem -out spike/dev-cert.pem -subj "/CN=$(hostname)" \
  -addext "subjectAltName=IP:127.0.0.1,DNS:localhost,DNS:$(hostname)"
```

Add the machine's LAN or Tailscale IP to `subjectAltName` if you will reach it
from another host. The certificate is self-signed on purpose: browsers still
treat a click-through https origin as secure, which is all WebCodecs needs.

## Run it

```bash
./run.sh --root /path/to/exr/sequences --tls    # start; PID file, log at /tmp/exrstream.log
./run.sh restart --root footage --tls --src "Linear Rec.709 (sRGB)"
./run.sh stop
```

Open `https://<host>:8099/` and accept the self-signed certificate.

Options: `--codec h264|hevc|av1`, `--mbps`, `--src`, `--port`, `--cache-gb`,
`--workers`, `--vbv-frames`, `--tls`.

**Never `pkill -f server.py`.** The pattern matches the invoking shell's own
command line, so it kills the caller. That is what `run.sh`'s PID file is for.

**HTTPS is not optional.** WebCodecs' `VideoDecoder` is `[SecureContext]` and
does not exist over plain http to a LAN or Tailscale IP. `localhost` is exempt,
so `ssh -N -L 8099:localhost:8099 user@host` works too (`-N`, or the session
exits immediately) — but the self-signed cert is simpler and measures the real
network path.

## Tests

```bash
.venv/bin/python -m exrstream.test_seq        # channel + data/display window handling
.venv/bin/python -m exrstream.test_grade      # shipped grade vs OCIO's CPU processor
.venv/bin/python -m exrstream.test_pipeline   # NVENC pipeline-lag regression
.venv/bin/python -m exrstream.test_server     # protocol; needs a running server
```

`test_pipeline` is the one to keep green. NVENC returns the packet for the frame
pushed three pushes earlier, and labelling packets with the session's state at
send time made step buttons move the wrong way and view changes appear not to
work. Push metadata travels with each push; discrete changes flush until their
own packet emerges. Packet *size* cannot detect any of this — strict CBR pads
every frame to `bitrate/fps` bytes — so the tests read that metadata.

## Test footage

```bash
./fetch-footage.sh 01_1a linear_hd 240 0 footage/tos_hd   # 1920x1012 PIZ
./fetch-footage.sh 01_1a linear    72 0 footage/tos_4k    # 4096x2160 uncompressed
```

Tears of Steel original camera footage, (CC) Blender Foundation |
mango.blender.org. It is Rec.709 scene-linear, **not** ACEScg — and EXRs carry
no colourspace metadata, so this cannot be detected. Set `--src` or pick it in
the UI. Guessing produces a plausible but wrong image, which is the worst
failure mode this tool has.

## Traps worth knowing

- `getProcessor(src, "sRGB - Display")` applies **no tone mapping** — it is a
  colourspace conversion, so 0.18 grey lands at 0.46 instead of 0.35. The ACES
  output transform lives on the **view**, via `DisplayViewTransform`.
- `glReadPixels(GL_BGRA)` on an RGBA framebuffer lands as B,G,R,A, which *is*
  NVENC's ARGB. Do not also swizzle in the shader; the two cancel.
- Upload textures as RGBA, never RGB: a 3-component upload makes the driver
  repack every row, 1.06 GB/s vs 21 GB/s.
- Strict CBR (`vbvbufsize`) is deliberate. Without it NVENC overshoots the
  target ~16% on real footage, and the peak frame doubles, which costs latency.

## Conventions

- Deliberate shortcuts carry a `ponytail: <ceiling>, <upgrade trigger>` comment.
  `grep -rnE '(#|//) ?ponytail:' .` is the ledger — keep them in real comments,
  not docstrings, or the scan misses them.
- `spike/` is frozen Phase 0 research: the provenance for every number in
  `RESULTS.md`, not code to maintain or import. `spike/07_ocio_gpu.py` duplicates
  `exrstream/gl.py`'s grade and is the most likely thing to drift.
- Not tracked, and must stay that way: `spike/dev-key.pem` (TLS private key) and
  `spike/restart-llama.sh` (carries an API key). See Setup to regenerate the
  certificate.
- This machine also runs a `llama-server` holding ~67 GB. Memory-heavy work
  here (4K sequences, several viewers) needs it stopped first, and restarted
  afterwards — `spike/restart-llama.sh` does that, and is untracked because the
  command line carries an API key. Check with `nvidia-smi` before blaming
  exrstream for an allocation failure.

## Requires

NVIDIA GPU with NVENC and a GPU-capable EGL driver (no X server needed).
Developed on a GB10 (aarch64). Every dependency installs from a wheel;
**ffmpeg is not used anywhere** — PyNvVideoCodec talks to NVENC directly.
