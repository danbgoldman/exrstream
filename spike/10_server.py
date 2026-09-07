"""Phase 0 #2 (browser half): stream NVENC output over a WebSocket to WebCodecs.

Measures the one number the server cannot measure alone: slider move -> new
exposure actually painted on the viewer's canvas.

Deliberately single-threaded. The whole server-side chain is 2.6ms at 2K, so
blocking the event loop with it costs ~6% of a 24fps frame interval and buys us
no GL context/thread affinity problems.
  ponytail: one session at a time, one GL context on the main thread. Per-session
  render threads (each with its own eglMakeCurrent) only when >1 viewer matters.
"""
import asyncio, struct, sys, time
from pathlib import Path
import numpy as np
import OpenImageIO as oiio
import PyNvVideoCodec as nvc
from aiohttp import web, WSMsgType

sys.path.insert(0, str(Path(__file__).parent))
from egl_ctx import make_context
from importlib import import_module

ROOT = Path(__file__).resolve().parent.parent
HDR = struct.Struct("<IIfI")          # frameIdx, epoch, ev, flags(1=key)
FPS = 24          # overridden by --fps
MAX_INFLIGHT = 3                      # backpressure: unacked frames allowed

def load_cache(d, limit=None):
    files = sorted(Path(d).glob("*.exr"))[:limit]
    out = []
    for f in files:
        inp = oiio.ImageInput.open(str(f))
        px = inp.read_image("half"); inp.close()
        h, w = px.shape[:2]
        out.append(np.ascontiguousarray(np.dstack([px, np.ones((h, w, 1), np.float16)])))
    if not out:
        raise SystemExit(f"no EXRs in {d}")
    return out

class Session:
    def __init__(self, cache, codec, mbps):
        OcioGL = import_module("07_ocio_gpu").OcioGL
        self.cache = cache
        self.h, self.w = cache[0].shape[:2]
        self.gl = OcioGL(self.w, self.h)
        self.enc = nvc.CreateEncoder(
            self.w, self.h, "ARGB", True, codec=codec, bitrate=int(mbps * 1e6),
            rc="cbr", fps=FPS, gop=30, bf=0, idrperiod=30,
            tuning_info="ultra_low_latency", preset="P3", repeatspspps=1)
        self.codec, self.mbps = codec, mbps
        self.ev, self.epoch, self.frame = 0.0, 0, 0
        self.playing, self.dirty, self.jump = False, True, False
        self.inflight = 0
        self.gl.set_exposure(0.0)

    def reset_encoder(self):
        """A client joining mid-stream would get a P-frame with no preceding
        IDR and the decoder would just error out -- which is what a page reload
        looks like. Cheapest reliable fix: a fresh encoder per connection, so
        frame one is always an IDR carrying SPS/PPS."""
        try:
            self.enc.EndEncode()
        except Exception:
            pass
        self.enc = nvc.CreateEncoder(
            self.w, self.h, "ARGB", True, codec=self.codec,
            bitrate=int(self.mbps * 1e6), rc="cbr", fps=FPS, gop=30, bf=0,
            idrperiod=30, tuning_info="ultra_low_latency", preset="P3",
            repeatspspps=1)
        self.dirty = True

    def encode_current(self, stats=None):
        """Push until the encoder yields packets. Paused slider drags run at
        engine speed, so NVENC's 3-frame pipeline costs ~2.6ms, not 3 frames."""
        out, pushes = [], 0
        for _ in range(8):
            pkts = self.enc.Encode(self.gl(self.cache[self.frame % len(self.cache)]))
            pushes += 1
            if pkts:
                out.extend(pkts)
                break
        if stats is not None:
            stats.append(pushes)
        return out

    def pack(self, pkt):
        data = bytes(pkt["data"])
        is_key = 1 if pkt.get("picture_type", 0) in (0, 3) else 0
        return HDR.pack(self.frame, self.epoch, self.ev, is_key) + data

async def ws_handler(request):
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    app = request.app
    s = app["session"]
    s.inflight = 0
    s.reset_encoder()
    def srv_stats():
        g, pk, pu = app["gaps"], app["pkts_per_send"], app["pushes"]
        f = lambda a: (sorted(a)[len(a)//2], sorted(a)[int(len(a)*.95)]) if a else (0, 0)
        return {"gap_p50": f(g)[0], "gap_p95": f(g)[1],
                "pkts_avg": sum(pk)/len(pk) if pk else 0,
                "pushes_avg": sum(pu)/len(pu) if pu else 0}
    app["srv_stats"] = srv_stats
    await ws.send_json({"type": "info", "w": s.w, "h": s.h,
                        "codec": app["codec_string"], "frames": len(s.cache), "fps": FPS})

    async def pump():
        period = 1.0 / FPS
        nxt = time.perf_counter()
        last_send = 0.0
        while not ws.closed:
            now = time.perf_counter()
            send = False
            if s.playing:
                # Do NOT let `dirty` pre-empt playback. A drag sets it ~60x/sec,
                # and when it took priority the frame index never advanced, so
                # playback stopped dead for the whole drag. It also needs no
                # special handling: set_exposure() has already updated the GL
                # uniform, so the next scheduled frame carries the new exposure
                # on its own. Full-rate playback with a live slider, not degraded.
                s.dirty = False
                if s.jump:
                    nxt, s.jump = now, False
                if s.inflight < MAX_INFLIGHT and now >= nxt:
                    s.frame = (s.frame + 1) % len(s.cache)
                    send = True
            elif s.dirty:
                s.dirty, send = False, True
            if send:
                t0 = time.perf_counter()
                pkts = s.encode_current(app["pushes"])
                enc_ms = (time.perf_counter() - t0) * 1e3
                for p in pkts:
                    await ws.send_bytes(s.pack(p))
                s.inflight += 1
                app["enc_ms"] = enc_ms
                if last_send:
                    app["gaps"].append((now - last_send) * 1e3)
                    del app["gaps"][:-300]
                app["pkts_per_send"].append(len(pkts))
                del app["pkts_per_send"][:-300]
                last_send = now
                nxt = max(nxt + period, now)
            await asyncio.sleep(0.001)

    task = asyncio.create_task(pump())
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            m = msg.json()
            t = m.get("type")
            if t == "exposure":
                s.ev = float(m["ev"]); s.epoch = int(m["epoch"])
                s.gl.set_exposure(s.ev); s.dirty = True
                # Only pull the schedule forward when PAUSED. While playing, a
                # drag fires ~60 times a second and resetting the send clock on
                # each one destroyed the frame cadence. The next scheduled frame
                # already carries the new exposure.
                if not s.playing:
                    s.jump = True
            elif t == "play":
                s.playing = bool(m["on"]); s.dirty = True
            elif t == "seek":
                s.frame = int(m["frame"]) % len(s.cache); s.dirty = True
            elif t == "ack":
                s.inflight = max(0, s.inflight - 1)
            elif t == "report":
                print(f"  client: slider->painted {m['ms']:6.1f} ms | "
                      f"arrival-gap p95 {m.get('jitter',0):5.1f} ms "
                      f"(target {1000/FPS:.1f}) | underruns {m.get('underruns',0)} | "
                      f"server enc {app.get('enc_ms',0):.1f} ms | "
                      f"paint p50 {m.get('paint50',0):.1f} p95 {m.get('paint95',0):.1f} | "
                      f"rAF {m.get('raf50',0):.1f} ms | q {m.get('q',0)}", flush=True)
                st = app["srv_stats"]()
                print(f"    server send-gap p50 {st['gap_p50']:.1f} p95 {st['gap_p95']:.1f} ms "
                      f"(target {1000/FPS:.1f}) | pkts/send {st['pkts_avg']:.2f} "
                      f"| pushes/send {st['pushes_avg']:.2f}", flush=True)
    finally:
        task.cancel()
    return ws

async def index(request):
    return web.FileResponse(Path(__file__).parent / "static" / "index.html")

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--seq", default=str(ROOT / "testdata" / "2k"))
    ap.add_argument("--codec", default="h264", choices=["h264", "hevc", "av1"])
    ap.add_argument("--mbps", type=float, default=15)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--fps", type=int, default=24,
                    help="24fps on a 60Hz display is an inherent 2:3 cadence. "
                         "Try 30 (clean 2:2) or 60 to tell rAF cadence judder "
                         "apart from network jitter.")
    ap.add_argument("--frames", type=int, default=48)
    ap.add_argument("--tls", action="store_true",
                    help="serve https with the self-signed dev cert. WebCodecs is "
                         "[SecureContext]: over plain http to a LAN/Tailscale IP "
                         "VideoDecoder does not exist. https with a click-through "
                         "warning still counts as secure, and unlike an ssh tunnel "
                         "it measures the real network path.")
    a = ap.parse_args()

    global FPS
    FPS = a.fps
    make_context()
    cache = load_cache(a.seq, a.frames)
    print(f"cached {len(cache)} frames {cache[0].shape[1]}x{cache[0].shape[0]} "
          f"({sum(c.nbytes for c in cache)/2**20:.0f} MB)")
    app = web.Application()
    app["session"] = Session(cache, a.codec, a.mbps)
    # WebCodecs needs a codec string; avc1.640028 = High@4.0, widely supported.
    app["codec_string"] = {"h264": "avc1.640028", "hevc": "hev1.1.6.L120.90",
                           "av1": "av01.0.08M.08"}[a.codec]
    app["enc_ms"] = 0.0
    app["gaps"], app["pkts_per_send"], app["pushes"] = [], [], []
    app.add_routes([web.get("/", index), web.get("/ws", ws_handler)])
    ssl_ctx = None
    if a.tls:
        import ssl
        here = Path(__file__).parent
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(here / "dev-cert.pem", here / "dev-key.pem")
    print(f"{'https' if a.tls else 'http'}://0.0.0.0:{a.port}/   "
          f"codec={a.codec} {a.mbps}Mbps")
    web.run_app(app, host="0.0.0.0", port=a.port, ssl_context=ssl_ctx,
                print=None, access_log=None)

if __name__ == "__main__":
    main()
