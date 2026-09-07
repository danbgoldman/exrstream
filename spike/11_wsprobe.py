"""Headless check of the WS protocol and bitstream, so the browser only has to
prove WebCodecs decode + real latency, not basic wiring."""
import asyncio, json, struct, sys, time
import aiohttp

HDR = struct.Struct("<IIfI")

def nal_types(buf):
    """Annex-B NAL unit types present (H.264: 7=SPS 8=PPS 5=IDR 1=non-IDR)."""
    out, i = [], 0
    while True:
        j = buf.find(b"\x00\x00\x01", i)
        if j < 0: break
        out.append(buf[j + 3] & 0x1F)
        i = j + 3
    return out

async def main(url="http://127.0.0.1:8099"):
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url + "/ws") as ws:
            info = json.loads((await ws.receive()).data)
            print("info:", info)

            # 1. an exposure change must produce a frame promptly
            lat = []
            for k in range(12):
                t0 = time.perf_counter()
                await ws.send_json({"type": "exposure", "ev": -4 + 0.5 * k, "epoch": k + 1})
                while True:
                    m = await asyncio.wait_for(ws.receive(), timeout=5)
                    if m.type is aiohttp.WSMsgType.BINARY:
                        frame, ep, ev, flags = HDR.unpack_from(m.data, 0)
                        if ep == k + 1:
                            lat.append((time.perf_counter() - t0) * 1e3)
                            break
                await ws.send_json({"type": "ack", "frame": frame})
            lat.sort()
            print(f"server-side slider->packet: p50 {lat[len(lat)//2]:.1f} ms  max {lat[-1]:.1f} ms")

            # 2. the bitstream must be Annex-B with in-band SPS/PPS
            await ws.send_json({"type": "exposure", "ev": 0.0, "epoch": 99})
            seen, payloads = set(), 0
            t0 = time.perf_counter()
            await ws.send_json({"type": "play", "on": True})
            while time.perf_counter() - t0 < 3.0:
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type is not aiohttp.WSMsgType.BINARY: continue
                frame, ep, ev, flags = HDR.unpack_from(m.data, 0)
                body = m.data[HDR.size:]
                seen.update(nal_types(body)); payloads += 1
                await ws.send_json({"type": "ack", "frame": frame})
            await ws.send_json({"type": "play", "on": False})
            print(f"frames in 3s: {payloads} ({payloads/3:.1f}/s)   NAL types seen: {sorted(seen)}")
            assert payloads > 40, f"only {payloads} frames in 3s; playback stalled"
            assert 7 in seen and 8 in seen, f"no SPS/PPS in band (got {sorted(seen)}) -- WebCodecs annex-b needs them"
            assert 5 in seen, "no IDR"
            print("OK: annex-b with in-band SPS/PPS, playback sustains rate")

asyncio.run(main(*sys.argv[1:]))
