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
from aiortc import RTCPeerConnection, RTCSessionDescription

from . import gl
from .seq import FrameCache, SequenceError, discover

HDR = struct.Struct("<IIfII")         # frameIdx, epoch, ev, flags(1=key), seq
# Fragment header for the WebRTC data channel: seq, index, count.
FRAG = struct.Struct("<IBB")
# SCTP reassembly limits differ per browser (Chrome advertises 256 KB, others
# 64 KB) and one CBR frame at 2K/15 Mbps is already 78 KB, so fragment
# unconditionally rather than branch on what the peer says it can take.
FRAG_MTU = 16384

# What a delivered packet actually contains. NVENC hands back the frame pushed
# 3 pushes ago, so this rides along with each push rather than being read off
# the session at send time.
Meta = namedtuple("Meta", "frame epoch ev src view token")
MAX_INFLIGHT = 3
CODEC_STRINGS = {"h264": "avc1.640028", "hevc": "hev1.1.6.L120.90",
                 "av1": "av01.0.08M.08"}
HERE = Path(__file__).resolve().parent


class Session:
    """Per-connection encoder and playback state."""

    def __init__(self, app, ws):
        self.app, self.ws = app, ws
        self.seq = self.frames = self.grade = self.enc = None
        self.w = self.h = 0
        self.fps = 24.0
        self.src = app["args"].src
        self.view = gl.VIEW
        self.ev, self.epoch, self.frame = 0.0, 0, 0
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
        self.pc = self.dc = None
        self.seq_no = 0
        self.force_idr = False

    @property
    def inflight(self):
        """Packets sent and not yet accounted for.

        Counting acks one-for-one against sends only works on a transport that
        cannot lose a packet. The data channel can, so an unacked packet used to
        leak a slot of the window permanently: eight losses and the pump stopped
        for good. Acks carry the sequence number instead, so a later ack
        subsumes every ack that never happened.
        """
        return max(0, self.seq_no - self.acked)

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
        gop = max(1, int(round(self.fps)))
        bits = int(a.mbps * 1e6)
        # VBV buffer of `vbv_frames` frames. Without it NVENC's CBR overshoots
        # by ~16% on real footage (15 -> 17.4 Mbps on Tears of Steel water
        # ripples; synthetic gradients stayed within 3% and hid this). It also
        # caps the peak frame -- 1.40 -> 0.63 Mbit -- which halves the worst-case
        # transmit time, so it buys latency as well as fitting the link.
        # `maxbitrate` does nothing here; only vbvbufsize constrains it.
        vbv = max(1, int(a.vbv_frames * bits / max(self.fps, 1)))
        self.enc = nvc.CreateEncoder(
            self.w, self.h, "ARGB", True, codec=a.codec,
            bitrate=bits, rc="cbr", fps=int(round(self.fps)),
            gop=gop, bf=0, idrperiod=gop, tuning_info="ultra_low_latency",
            preset="P3", repeatspspps=1, vbvbufsize=vbv, vbvinit=vbv)
        self._pipe.clear()
        self.dirty = True
        self.flush_next = True

    def bind(self, seq, frames):
        self.seq, self.frames = seq, frames
        self.h, self.w = frames[0].shape[:2]
        self.grade = gl.grade_for(self.w, self.h, self.src, gl.DISPLAY, self.view)
        self.frame = 0
        self._make_encoder()

    def set_look(self, src=None, view=None):
        self.src = src or self.src
        self.view = view or self.view
        self.grade = gl.grade_for(self.w, self.h, self.src, gl.DISPLAY, self.view)
        self.dirty = True

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
        self.grade.set_exposure(self.ev)
        img = self.frames[self.frame % len(self.frames)]
        # A viewer that lost a packet is stuck until the next IDR, and the GOP
        # is a whole second. Honour the request on the very next push.
        flags, self.force_idr = int(nvc.FORCEIDR) if self.force_idr else 0, False
        token = object()                         # identity of THIS request
        tag, out = token, []
        for _ in range(8):
            # Everything that describes the delivered picture travels with the
            # push, so a packet is never labelled with state it does not carry.
            self._pipe.append(Meta(self.frame, self.epoch, self.ev,
                                   self.src, self.view, tag))
            tag = None                           # only the first push is ours
            for p in self.enc.Encode(self.grade(img), flags):
                meta = (self._pipe.popleft() if self._pipe else
                        Meta(self.frame, self.epoch, self.ev, self.src, self.view, None))
                out.append((meta, p))
            if not flush or any(m.token is token for m, _ in out):
                break
        return out

    def pack(self, meta, pkt):
        is_key = 1 if pkt.get("picture_type", 0) in (0, 3) else 0
        self.seq_no += 1
        return (HDR.pack(meta.frame, meta.epoch, meta.ev, is_key, self.seq_no)
                + bytes(pkt["data"]))

    async def send_packet(self, data):
        """Data channel when it is up, WebSocket otherwise.

        The channel is unordered with no retransmits, which is the whole point:
        TCP cannot drop a late packet, so one loss stalls every frame behind it
        (Phase 1 measured 280 ms stalls and one of 2.1 s against a 33 ms
        arrival gap). Here a lost frame is just a lost frame, and the client
        asks for an IDR.
        """
        dc = self.dc
        if dc is None or dc.readyState != "open":
            await self.ws.send_bytes(data)
            return
        seq = HDR.unpack_from(data)[4]
        n = max(1, (len(data) + FRAG_MTU - 1) // FRAG_MTU)
        for i in range(n):
            dc.send(FRAG.pack(seq, i, n)
                    + data[i * FRAG_MTU:(i + 1) * FRAG_MTU])

    async def start_rtc(self, sdp):
        """Answer the client's offer. No trickle ICE: aiortc's
        setLocalDescription already waits for gathering, and with no STUN
        server the host candidates are ready immediately."""
        await self.stop_rtc()
        pc = self.pc = RTCPeerConnection()

        @pc.on("datachannel")
        def _(ch):
            ch.binaryType = "arraybuffer"
            self.dc = ch

            @ch.on("close")
            def _():
                self.dc = None

        await pc.setRemoteDescription(RTCSessionDescription(sdp, "offer"))
        await pc.setLocalDescription(await pc.createAnswer())
        await self.ws.send_json({"type": "answer",
                                 "sdp": pc.localDescription.sdp})

    async def stop_rtc(self):
        pc, self.pc, self.dc = self.pc, None, None
        if pc is not None:
            await pc.close()

    def close(self):
        if self.enc is not None:
            try:
                self.enc.EndEncode()
            except Exception:
                pass
            self.enc = None


def cadence_note(fps, hz):
    """Say plainly when the display cannot present this rate evenly.

    Resampling to the refresh rate would look smoother and would be a lie about
    motion timing, so it is offered as an explicit choice, never a default.
    """
    if not hz:
        return None
    ratio = hz / fps
    if abs(ratio - round(ratio)) < 0.02:
        return None
    return (f"{fps:g} fps on a {hz:.0f} Hz display is {ratio:.2f} refreshes per "
            f"frame, so motion will judder. Laptops on battery often cap "
            f"refresh. Playing at {hz:.0f} fps would look smooth but would "
            f"alter motion timing.")


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
            period = 1.0 / s.fps
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
                    s.frame = (s.frame + 1) % len(s.frames)
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
                    await s.send_packet(s.pack(meta, p))
                nxt = max(nxt + period, now)
            await asyncio.sleep(0.001)

    async def do_open(key, first, count):
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
        s.bind(seq, frames)
        await ws.send_json({
            "type": "ready", "w": s.w, "h": s.h, "frames": len(frames),
            "first": first, "fps": s.fps, "src": s.src, "view": s.view,
            "name": Path(seq.key).name,
            "cache_gb": round(app["cache"].bytes / 2**30, 2),
            "bad": bad,
            "note": cadence_note(s.fps, s.refresh_hz)})

    task = asyncio.create_task(pump())
    try:
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            t = m.get("type")
            if t == "hello":
                s.refresh_hz = float(m.get("refreshHz") or 0)
                if s.frames:
                    await ws.send_json({"type": "note",
                                        "note": cadence_note(s.fps, s.refresh_hz)})
            elif t == "open":
                await do_open(m["key"], int(m.get("first", 0)), int(m.get("count", 0)))
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
                s.frame = int(m["frame"]) % len(s.frames)
                s.dirty = s.jump = s.flush_next = True
            elif t == "step" and s.frames:
                s.playing = False
                s.frame = (s.frame + int(m["delta"])) % len(s.frames)
                s.dirty = s.jump = s.flush_next = True
            elif t == "fps" and s.frames:
                s.fps = max(1.0, float(m["fps"]))
                s._make_encoder()
                await ws.send_json({"type": "ready", "w": s.w, "h": s.h,
                                    "frames": len(s.frames), "first": 0,
                                    "fps": s.fps, "src": s.src, "view": s.view,
                                    "name": Path(s.seq.key).name,
                                    "cache_gb": round(app["cache"].bytes / 2**30, 2),
                                    "note": cadence_note(s.fps, s.refresh_hz)})
            elif t == "look" and s.frames:
                try:
                    s.set_look(m.get("src"), m.get("view"))
                    s.flush_next = True
                except Exception as e:                        # noqa: BLE001
                    await ws.send_json({"type": "error", "msg": f"look: {e}"})
            elif t == "ack":
                s.acked = max(s.acked, int(m.get("seq", 0)))
            elif t == "offer":
                try:
                    await s.start_rtc(m["sdp"])
                except Exception as e:                        # noqa: BLE001
                    # The WebSocket path still works; say so rather than
                    # dying, since this is an optimisation over a fallback.
                    await ws.send_json({"type": "note",
                                        "note": f"WebRTC unavailable: {e}"})
            elif t == "idr":
                # The client asks for this when it has stopped receiving, which
                # is also the one case where the window can be full of packets
                # whose acks are never coming. Clear it, or the recovery frame
                # it is asking for cannot be sent.
                s.force_idr = True
                s.dirty = s.jump = True
                s.acked = s.seq_no
    finally:
        task.cancel()
        await s.stop_rtc()
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
