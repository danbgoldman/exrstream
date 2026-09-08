"""exrstream server: EXR sequences -> graded H.264/HEVC/AV1 -> WebSocket.

One session per connection. GL and NVENC run on the event loop (the whole
server-side chain is ~2.5ms at 2K); EXR decode runs in a thread pool because a
4K frame is ~13ms and OIIO releases the GIL.
"""
import argparse, asyncio, json, struct, time
from collections import deque, namedtuple
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import PyNvVideoCodec as nvc
from aiohttp import web, WSMsgType

from . import gl
from .seq import FrameCache, SequenceError, discover

# frameIdx, epoch, ev, flags(1=key), seq, then the source rectangle this frame
# actually contains: x, y, side, as fractions of the source. The region travels
# with the packet for the same reason everything else does -- NVENC hands back
# the frame pushed three pushes ago, so a packet labelled with the session's
# current region would paint the wrong rectangle and the picture would slide
# around under a pan.
HDR = struct.Struct("<IIfII3f")

# What a delivered packet actually contains. NVENC hands back the frame pushed
# 3 pushes ago, so this rides along with each push rather than being read off
# the session at send time.
Meta = namedtuple("Meta", "frame epoch ev src view roi token")
MAX_INFLIGHT = 3            # packets, not sends: a flush emits several at once
CODEC_STRINGS = {"h264": "avc1.640028", "hevc": "hev1.1.6.L120.90",
                 "av1": "av01.0.08M.08"}
HERE = Path(__file__).resolve().parent


class Session:
    """Per-connection encoder and playback state."""

    def __init__(self, app, ws):
        self.app, self.ws = app, ws
        self.seq = self.frames = self.grade = self.enc = None
        self.w = self.h = 0
        # Two rates, and conflating them is the bug this exists to prevent.
        # `src_fps` is what the footage is meant to run at; `out_fps` is what we
        # encode and send, chosen to divide the client's display refresh.
        self.src_fps = 24.0
        self.out_fps = 24.0
        self.pos = 0.0
        self.src = app["args"].src
        self.view = gl.VIEW
        self.ev, self.epoch, self.frame = 0.0, 0, 0
        # The B side of an A/B wipe: its own sequence and its own look, either
        # of which may match A's. One pane is a sequence plus a look, so
        # "compare two renders" and "compare two views" are the same feature.
        self.seq_b = self.frames_b = self.grade_b = None
        self.src_b = self.view_b = None
        self.wipe = 0.5
        # The source rectangle being shown: (x, y, side) as fractions of the
        # source, (0, 0, 1) being the whole frame.
        self.roi = (0.0, 0.0, 1.0)
        self.playing, self.dirty, self.jump = False, False, False
        self.flush_next = False
        self.settle_at = 0.0
        self.acked = 0
        # NVENC returns the packet for the frame pushed 3 pushes ago. Without
        # this FIFO the header describes the frame we just PUSHED while the
        # pixels are three older -- so a step button appeared to jump the wrong
        # way and a view change did not take effect until playback pushed more
        # frames through.
        self._pipe = deque()
        self.refresh_hz = 0.0
        self.seq_no = 0

    @property
    def compare(self):
        """Comparing is having a B side, not a separate switch to forget."""
        return self.frames_b is not None

    @property
    def inflight(self):
        """Packets sent and not yet accounted for.

        Counting acks one-for-one against sends leaks a slot of the window
        permanently whenever an ack does not arrive -- a decode error on the
        client is enough -- and a few of those stop the pump for good. Acks
        carry the sequence number instead, so a later ack subsumes every ack
        that never happened.
        """
        return max(0, self.seq_no - self.acked)

    def retune(self):
        """Pick the encode rate for the measured refresh. True if it changed.

        """
        was = self.out_fps
        self.out_fps = stream_fps(self.src_fps, self.refresh_hz)
        return abs(self.out_fps - was) > 1e-6

    def advance(self):
        """One output frame on. The source position moves at the SOURCE rate, so
        a 24 fps sequence streamed at 30 repeats one frame in five and still
        takes the same wall-clock time it would in any other player."""
        self.pos = (self.pos + self.src_fps / self.out_fps) % len(self.frames)
        self.frame = int(self.pos)

    def set_roi(self, x, y, side):
        """Clamp a requested region to something showable: never larger than the
        source (zooming out past fit would need minification the shader does not
        do), never smaller than 1/32, and never off the edge."""
        side = max(1 / 32, min(1.0, float(side)))
        x = max(0.0, min(1.0 - side, float(x)))
        y = max(0.0, min(1.0 - side, float(y)))
        self.roi = (x, y, side)

    def seek(self, frame):
        self.frame = frame % len(self.frames)
        self.pos = float(self.frame)

    def _make_encoder(self):
        """A fresh encoder whenever geometry or rate changes, and on (re)connect:
        a client joining mid-stream would otherwise hit a P-frame with no
        preceding IDR and the decoder simply errors."""
        if self.enc is not None:
            try:
                self.enc.EndEncode()
            except Exception:
                pass
        a = self.app["args"]
        gop = max(1, int(round(self.out_fps)))
        bits = int(a.mbps * 1e6)
        # VBV buffer of `vbv_frames` frames. Without it NVENC's CBR overshoots
        # by ~16% on real footage (15 -> 17.4 Mbps on Tears of Steel water
        # ripples; synthetic gradients stayed within 3% and hid this). It also
        # caps the peak frame -- 1.40 -> 0.63 Mbit -- which halves the worst-case
        # transmit time, so it buys latency as well as fitting the link.
        # `maxbitrate` does nothing here; only vbvbufsize constrains it.
        vbv = max(1, int(a.vbv_frames * bits / max(self.out_fps, 1)))
        self.enc = nvc.CreateEncoder(
            self.w, self.h, "ARGB", True, codec=a.codec,
            bitrate=bits, rc="cbr", fps=int(round(self.out_fps)),
            gop=gop, bf=0, idrperiod=gop, tuning_info="ultra_low_latency",
            preset="P3", repeatspspps=1, vbvbufsize=vbv, vbvinit=vbv)
        self._pipe.clear()
        self.dirty = True
        self.flush_next = True

    def bind(self, seq, frames):
        self.seq, self.frames = seq, frames
        self.h, self.w = frames[0].shape[:2]
        self.grade = gl.grade_for(self.w, self.h, self.src, gl.DISPLAY, self.view)
        # A sequence of a different size cannot share the output frame, and the
        # encoder is built for A's geometry.
        if self.frames_b is not None and self.frames_b[0].shape[:2] != (self.h, self.w):
            self.seq_b = self.frames_b = self.grade_b = None
        self.seek(0)
        self.retune()
        self._make_encoder()

    def bind_b(self, seq, frames):
        if frames[0].shape[:2] != (self.h, self.w):
            h, w = frames[0].shape[:2]
            raise SequenceError(
                f"{Path(seq.key).name} is {w}x{h}, but the A side is "
                f"{self.w}x{self.h}; a wipe needs one frame size")
        self.seq_b, self.frames_b = seq, frames
        self.src_b = self.src_b or self.src
        self.view_b = self.view_b or self.view
        self.grade_b = gl.grade_for(self.w, self.h, self.src_b, gl.DISPLAY, self.view_b)
        # Every `ready` makes the client build a fresh VideoDecoder, so every
        # `ready` has to be followed by an IDR or that decoder meets a P-frame
        # with no reference and errors out. A B-side open sends one, so it needs
        # a fresh encoder too, even though the geometry has not changed.
        self._make_encoder()

    def unbind_b(self):
        """Drop the B side. No `ready`, so no new decoder and no new encoder --
        the picture changes, which a flush already covers."""
        self.seq_b = self.frames_b = self.grade_b = None
        self.dirty = self.jump = self.flush_next = True

    def set_look(self, src=None, view=None, side="a"):
        if side == "b":
            self.src_b = src or self.src_b or self.src
            self.view_b = view or self.view_b or self.view
            self.grade_b = gl.grade_for(self.w, self.h, self.src_b,
                                        gl.DISPLAY, self.view_b)
        else:
            self.src = src or self.src
            self.view = view or self.view
            self.grade = gl.grade_for(self.w, self.h, self.src, gl.DISPLAY, self.view)
        self.dirty = True

    def render_current(self):
        """The picture for this frame, wipe and all, as BGRA the encoder takes.

        A and B are graded into the same framebuffer under a scissor, so the
        comparison costs one extra draw and no extra readback or encode -- and
        the output stays A's size, which is why a wipe needs no new encoder
        where a side-by-side would.
        """
        self.grade.set_exposure(self.ev)
        img = self.frames[self.frame % len(self.frames)]
        if not (self.compare and self.frames_b):
            self.grade.render(img, roi=self.roi)
            return self.grade.read()
        split = max(0, min(self.w, int(round(self.wipe * self.w))))
        self.grade.render(img, scissor=(0, 0, split, self.h), roi=self.roi)
        # Clamped, not wrapped: past the end of a shorter B, holding its last
        # frame is at least visibly wrong, where wrapping would look plausible
        # and compare the wrong pair.
        b = self.frames_b[min(self.frame, len(self.frames_b) - 1)]
        self.grade_b.set_exposure(self.ev)
        # Both panes show the same region: comparing two differently-framed
        # images is not comparing.
        self.grade_b.render(b, fbo=self.grade.fbo, roi=self.roi,
                            scissor=(split, 0, self.w - split, self.h))
        return self.grade.read()

    # ponytail: all sessions share one event loop and one GL context, so the
    # encodes below serialise. Measured fine to 3 concurrent 2K viewers
    # (21-24.5 fps each) and it degrades from there. Upgrade to a render thread
    # per session, each with its own eglMakeCurrent, when there are viewers to
    # spare -- not worth the context juggling before that.
    def encode_current(self, flush=False):
        """Encode the current frame, returning (meta, packet) pairs.

        Each push records what it actually contained; each emitted packet is
        matched to the push it came from, so headers never describe pixels the
        viewer is not looking at.

        `flush` keeps pushing the same frame until THIS push's packet emerges
        (4 pushes, given NVENC's depth of 3). Discrete changes -- a step, a
        seek, a view change, the end of a slider drag -- need it, or the change
        does not reach the viewer at all. Continuous changes do not: a drag
        pushes ~60 times a second and flushes itself, and flushing every event
        would cost 4x the encode work for no visible gain (36ms per event at
        4K, which will not keep up).

        Pushing the same frame repeatedly also means the packets that pop ahead
        of ours are the previously displayed frame, so the viewer lingers on
        the old frame rather than jumping somewhere unrelated.
        """
        token = object()                         # identity of THIS request
        tag, out = token, []
        for _ in range(8):
            # Everything that describes the delivered picture travels with the
            # push, so a packet is never labelled with state it does not carry.
            self._pipe.append(Meta(self.frame, self.epoch, self.ev,
                                   self.src, self.view, self.roi, tag))
            tag = None                           # only the first push is ours
            for p in self.enc.Encode(self.render_current()):
                meta = (self._pipe.popleft() if self._pipe else
                        Meta(self.frame, self.epoch, self.ev, self.src,
                             self.view, self.roi, None))
                out.append((meta, p))
            if not flush or any(m.token is token for m, _ in out):
                break
        return out

    def pack(self, meta, pkt):
        is_key = 1 if pkt.get("picture_type", 0) in (0, 3) else 0
        self.seq_no += 1
        return (HDR.pack(meta.frame, meta.epoch, meta.ev, is_key, self.seq_no,
                         *meta.roi) + bytes(pkt["data"]))

    def close(self):
        if self.enc is not None:
            try:
                self.enc.EndEncode()
            except Exception:
                pass
            self.enc = None


def stream_fps(src_fps, hz):
    """The rate to encode at: the divisor of the display refresh nearest the
    sequence rate.

    A rate that does not divide the refresh cannot be presented evenly -- 24 on
    30 Hz is 1.25 refreshes per frame, so paints alternate 33/67 ms and the
    result is judder that looks like a network fault. Streaming a divisor
    instead makes every paint land on a refresh; where that rate is not the
    sequence rate, `Session.advance` repeats or drops source frames to keep the
    sequence running at its own speed.
    """
    if not hz or hz <= 0:
        return src_fps
    return hz / max(1, round(hz / src_fps))


def cadence_note(src_fps, out_fps, hz):
    """Warn only when the display cannot keep up, i.e. frames are being dropped.

    Repeating frames to fill a faster refresh is not a problem and gets no
    warning: the sequence still runs at its own rate, so nothing is lost and a
    banner would be noise. The header states the two rates for anyone curious.
    Dropping is different -- there are frames of the cut you are not being
    shown, and no setting here can fix it.
    """
    if not hz or out_fps >= src_fps - 0.01:
        return None
    return (f"This display refreshes at {hz:.0f} Hz, below the sequence's "
            f"{src_fps:g} fps, so it is being played at {out_fps:g} fps and "
            f"{1 - out_fps / src_fps:.0%} of the frames are not shown. "
            f"{out_fps:g} fps is the most this display can present evenly; "
            f"a faster one would show the rest. (Laptops on battery often cap "
            f"refresh.)")


def note_for(s):
    return cadence_note(s.src_fps, s.out_fps, s.refresh_hz)


def ready_msg(s, app, first=0, bad=None):
    """Everything the client needs to draw and to time the stream.

    `fps` is the stream rate, which is what the presentation clock runs on;
    `src_fps` is the sequence's own rate, which is what the UI must show, or the
    viewer cannot tell a repeated frame from a fast one.
    """
    return {"type": "ready", "w": s.w, "h": s.h, "frames": len(s.frames),
            "first": first, "fps": s.out_fps, "src_fps": s.src_fps,
            "hz": round(s.refresh_hz, 1),
            "src": s.src, "view": s.view, "name": Path(s.seq.key).name,
            "cache_gb": round(app["cache"].bytes / 2**30, 2),
            "bad": bad or [], "note": note_for(s),
            "compare": s.compare, "wipe": s.wipe, "roi": list(s.roi),
            "b_key": s.seq_b.key if s.seq_b else None,
            "b_name": Path(s.seq_b.key).name if s.seq_b else None,
            "b_frames": len(s.frames_b) if s.frames_b else 0,
            "b_src": s.src_b, "b_view": s.view_b}


async def ws_handler(request):
    ws = web.WebSocketResponse(max_msg_size=0, heartbeat=30)
    await ws.prepare(request)
    app = request.app
    s = Session(app, ws)
    loop = asyncio.get_running_loop()

    await ws.send_json({"type": "sequences",
                        "items": [q.info() for q in app["sequences"]],
                        "colorspaces": gl.colorspaces(), "views": gl.views(),
                        "codec": CODEC_STRINGS[app["args"].codec]})

    async def pump():
        nxt = time.perf_counter()
        while not ws.closed:
            if s.frames is None:
                await asyncio.sleep(0.01)
                continue
            now = time.perf_counter()
            period = 1.0 / s.out_fps
            send = False
            # A discrete change (step, seek, view, settled exposure) pulls the
            # schedule forward and is answered at once. A mid-drag exposure
            # event does NOT: strict CBR pads every frame to bitrate/fps bytes,
            # so answering ~60 drag events a second would ship ~37 Mbps on a
            # 15 Mbps budget. Rate-limiting the drag to the frame period keeps
            # the bandwidth promise and costs at most one frame of lag.
            if s.jump:
                nxt, s.jump = now, False
            # Settle flush. A change that stops arriving must still reach the
            # viewer: without this a single non-final exposure event sits in
            # NVENC's pipeline forever, because nothing else pushes frames when
            # paused. The client's `final` flag makes this immediate rather
            # than necessary -- correctness must not depend on the client.
            if s.settle_at and now >= s.settle_at and not s.playing:
                s.settle_at, s.dirty, s.flush_next, s.jump = 0.0, True, True, True
                nxt = now
            if s.playing:
                # Never let `dirty` pre-empt playback: a drag sets it ~60x/sec
                # and the frame index would stop advancing for the whole drag.
                # It also needs no special casing -- set_exposure has already
                # updated the uniform, so the next scheduled frame carries it.
                s.dirty = False
                if s.inflight < MAX_INFLIGHT and now >= nxt:
                    s.advance()
                    send = True
            elif s.dirty and s.inflight < MAX_INFLIGHT and now >= nxt:
                s.dirty, send = False, True
            if send:
                flush, s.flush_next = s.flush_next and not s.playing, False
                try:
                    pkts = s.encode_current(flush=flush)
                except Exception as e:                       # noqa: BLE001
                    await ws.send_json({"type": "error", "msg": f"encode: {e}"})
                    return
                for meta, p in pkts:
                    await ws.send_bytes(s.pack(meta, p))
                nxt = max(nxt + period, now)
            await asyncio.sleep(0.001)

    async def do_open(key, first, count, side="a"):
        if side == "b" and not key:
            # The empty entry in B's picker. Not a separate "stop comparing"
            # control: B being unset is what not comparing means.
            s.unbind_b()
            await ws.send_json({"type": "state", "compare": False,
                                "b_key": None, "b_name": None})
            return
        if side == "b" and s.frames is None:
            await ws.send_json({"type": "error",
                                "msg": "open a sequence before comparing one"})
            return
        seq = next((q for q in app["sequences"] if q.key == key), None)
        if seq is None:
            await ws.send_json({"type": "error", "msg": f"no such sequence {key}"})
            return
        count = max(1, min(count or seq.nframes, seq.nframes - first))
        await ws.send_json({"type": "loading", "done": 0, "total": count})

        last = [0.0]

        def prog(i, n):
            t = time.perf_counter()
            if t - last[0] > 0.2 or i == n:
                last[0] = t
                asyncio.run_coroutine_threadsafe(
                    ws.send_json({"type": "loading", "done": i, "total": n}), loop)

        try:
            frames = await loop.run_in_executor(
                app["pool"], lambda: app["cache"].load(seq, first, count, prog))
            bad = list(app["cache"].bad)
        except SequenceError as e:
            await ws.send_json({"type": "error", "msg": str(e)})
            return
        except Exception as e:                                # noqa: BLE001
            await ws.send_json({"type": "error", "msg": f"load failed: {e}"})
            return
        try:
            if side == "b":
                s.bind_b(seq, frames)
            else:
                s.bind(seq, frames)
        except SequenceError as e:
            await ws.send_json({"type": "error", "msg": str(e)})
            return
        # A B-side open must not move the playhead: the point is to compare the
        # frame you are already looking at.
        await ws.send_json(ready_msg(s, app, first=s.frame if side == "b" else first,
                                     bad=bad))

    task = asyncio.create_task(pump())
    try:
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            t = m.get("type")
            if t == "hello":
                # The refresh can change under us -- plugging a laptop in is
                # enough -- so this arrives whenever the client's measurement
                # moves, not only at connect.
                s.refresh_hz = float(m.get("refreshHz") or 0)
                if s.retune() and s.frames:
                    s._make_encoder()
                    await ws.send_json(ready_msg(s, app))
                elif s.frames:
                    await ws.send_json({"type": "note", "note": note_for(s)})
            elif t == "open":
                await do_open(m["key"], int(m.get("first", 0)),
                              int(m.get("count", 0)), m.get("side", "a"))
            elif t == "exposure":
                s.ev = float(m["ev"]); s.epoch = int(m["epoch"])
                s.dirty = True
                if not s.playing:
                    # `final` is the range input's change event (mouse up):
                    # answer it at once. Mid-drag events are rate-limited and
                    # flush by repetition, with the settle timer as the backstop.
                    if m.get("final"):
                        s.jump = s.flush_next = True
                        s.settle_at = 0.0
                    else:
                        s.settle_at = time.perf_counter() + 0.12
            elif t == "play":
                s.playing = bool(m["on"]); s.dirty = True
                if not s.playing:
                    s.flush_next = s.jump = True
                s.settle_at = 0.0
            elif t == "seek" and s.frames:
                s.seek(int(m["frame"]))
                s.dirty = s.jump = s.flush_next = True
            elif t == "step" and s.frames:
                s.playing = False
                s.seek(s.frame + int(m["delta"]))
                s.dirty = s.jump = s.flush_next = True
            elif t == "fps" and s.frames:
                s.src_fps = max(1.0, float(m["fps"]))
                s.retune()
                s._make_encoder()
                await ws.send_json(ready_msg(s, app))
            elif t == "look" and s.frames:
                try:
                    s.set_look(m.get("src"), m.get("view"), m.get("side", "a"))
                    s.flush_next = True
                except Exception as e:                        # noqa: BLE001
                    await ws.send_json({"type": "error", "msg": f"look: {e}"})
            elif t == "roi" and s.frames:
                s.set_roi(m["x"], m["y"], m["s"])
                s.dirty = True
                if not s.playing:
                    # A pinch or a drag fires as fast as the pointer moves, so
                    # it gets the exposure slider's discipline: rate-limited to
                    # the frame period, flushed on release, settle as backstop.
                    if m.get("final"):
                        s.jump = s.flush_next = True
                        s.settle_at = 0.0
                    else:
                        s.settle_at = time.perf_counter() + 0.12
            elif t == "wipe" and s.frames:
                s.wipe = max(0.0, min(1.0, float(m["wipe"])))
                s.dirty = True
                if not s.playing:
                    # Same discipline as the exposure slider: a drag is
                    # rate-limited to the frame period and flushes by
                    # repetition, with the settle timer as the backstop.
                    if m.get("final"):
                        s.jump = s.flush_next = True
                        s.settle_at = 0.0
                    else:
                        s.settle_at = time.perf_counter() + 0.12
            elif t == "ack":
                s.acked = max(s.acked, int(m.get("seq", 0)))
    finally:
        task.cancel()
        s.close()
    return ws


async def index(request):
    return web.FileResponse(HERE / "static" / "index.html")


def build_app(a):
    gl.make_context()
    if a.src not in gl.colorspaces():
        raise SystemExit(f"unknown colourspace {a.src!r}.\nAvailable:\n  " +
                         "\n  ".join(gl.colorspaces()))
    app = web.Application()
    app["args"] = a
    app["cache"] = FrameCache(int(a.cache_gb * (1 << 30)), a.workers)
    app["pool"] = ThreadPoolExecutor(a.workers)
    app["sequences"] = discover(a.root)
    app.add_routes([web.get("/", index), web.get("/ws", ws_handler),
                    web.static("/static", HERE / "static")])
    return app


def main():
    p = argparse.ArgumentParser(prog="exrstream")
    p.add_argument("--root", default="testdata", help="directory to scan for EXR sequences")
    p.add_argument("--codec", default="h264", choices=list(CODEC_STRINGS))
    p.add_argument("--src", default=gl.DEFAULT_SRC,
                   help="input colourspace for this footage root. EXRs rarely "
                        "carry one, so it cannot be detected: Tears of Steel is "
                        "'Linear Rec.709 (sRGB)', a renderer is usually ACEScg.")
    p.add_argument("--mbps", type=float, default=15)
    p.add_argument("--vbv-frames", type=float, default=1.0,
                   help="VBV buffer in frames. 1 holds the target rate exactly "
                        "and caps peak frame size; raise toward 2 to trade rate "
                        "discipline for quality on hard frames.")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--cache-gb", type=float, default=40)
    p.add_argument("--workers", type=int, default=8,
                   help="EXR decode threads. Load-bearing cold from disk, where "
                        "one reader manages 0.55 GB/s against the 1.27 GB/s a 4K "
                        "sequence needs: 10.4 fps at 1, 50.3 at 8. Diminishing past 8.")
    p.add_argument("--tls", action="store_true",
                   help="serve https with spike/dev-cert.pem. WebCodecs is "
                        "[SecureContext]: VideoDecoder does not exist over plain "
                        "http to a LAN or Tailscale IP.")
    a = p.parse_args()

    app = build_app(a)
    print(f"{len(app['sequences'])} sequence(s) under {a.root}")
    for q in app["sequences"]:
        print(f"  {q.key}  {q.nframes}f  {q.w}x{q.h}  {q.channels}")
    ssl_ctx = None
    if a.tls:
        import ssl
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(HERE.parent / "spike" / "dev-cert.pem",
                                HERE.parent / "spike" / "dev-key.pem")
    print(f"{'https' if a.tls else 'http'}://0.0.0.0:{a.port}/  codec={a.codec} {a.mbps}Mbps")
    web.run_app(app, host="0.0.0.0", port=a.port, ssl_context=ssl_ctx,
                print=None, access_log=None)


if __name__ == "__main__":
    main()
