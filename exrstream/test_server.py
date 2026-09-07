"""End-to-end protocol check against a running server. Exercises every message
the UI can send, so the browser only has to prove it looks right."""
import asyncio, json, ssl, struct, sys, time
import aiohttp

HDR = struct.Struct("<IIfII")


def nals(b):
    out, i = [], 0
    while True:
        j = b.find(b"\x00\x00\x01", i)
        if j < 0 or len(out) > 6:
            break
        out.append(b[j + 3] & 0x1F)
        i = j + 3
    return out


async def main(url="https://127.0.0.1:8099"):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = aiohttp.TCPConnector(ssl=ctx)
    async with aiohttp.ClientSession(connector=conn) as sess:
        async with sess.ws_connect(url + "/ws") as ws:
            seqs = json.loads((await ws.receive()).data)
            assert seqs["type"] == "sequences" and seqs["items"], seqs
            print(f"  sequences: {[i['name']+':'+str(i['frames']) for i in seqs['items']]}")
            assert "ACEScg" in seqs["colorspaces"]
            assert any("ACES 2.0" in v for v in seqs["views"])

            await ws.send_json({"type": "hello", "refreshHz": 30.0})
            # biggest sequence: a partially-downloaded or tiny one proves nothing
            biggest = max(seqs["items"], key=lambda i: i["frames"])
            key = biggest["key"]
            await ws.send_json({"type": "open", "key": key, "count": 24})

            saw_loading = False
            while True:
                m = json.loads((await asyncio.wait_for(ws.receive(), 60)).data)
                if m["type"] == "loading":
                    saw_loading = True
                elif m["type"] == "ready":
                    break
                elif m["type"] == "error":
                    raise AssertionError(m["msg"])
            assert saw_loading, "no loading progress reported"
            print(f"  ready: {m['w']}x{m['h']} {m['frames']}f fps={m['fps']} cache={m['cache_gb']}GB")
            assert m["note"] and "judder" in m["note"], "24fps on 30Hz must warn"
            print(f"  cadence note fired ok")

            async def next_frame(timeout=10):
                while True:
                    r = await asyncio.wait_for(ws.receive(), timeout)
                    if r.type is aiohttp.WSMsgType.BINARY:
                        h = HDR.unpack_from(r.data, 0)
                        await ws.send_json({"type": "ack", "frame": h[0]})
                        return h, r.data[HDR.size:]

            (fr, ep, ev, fl, _sq), body = await next_frame()
            assert 5 in nals(body) and 7 in nals(body), f"first frame not IDR+SPS: {nals(body)}"
            print("  first frame is IDR with in-band SPS/PPS")

            async def exposure_rtt(k, final):
                t0 = time.perf_counter()
                await ws.send_json({"type": "exposure", "ev": -3 + 0.5 * k,
                                    "epoch": k + 1, "final": final})
                while True:
                    (fr, ep, ev, fl, _sq), _ = await next_frame()
                    if ep == k + 1:
                        return (time.perf_counter() - t0) * 1e3

            # What the UI actually does on mouse-up: answered at once.
            lat = sorted([await exposure_rtt(k, True) for k in range(10)])
            print(f"  settled exposure -> packet p50 {lat[len(lat)//2]:.1f} ms")
            assert lat[len(lat) // 2] < 60, lat

            # A lone mid-drag event must still land, via the settle backstop,
            # so correctness does not depend on the client sending `final`.
            lat = sorted([await exposure_rtt(20 + k, False) for k in range(4)])
            print(f"  lone mid-drag exposure lands in {lat[len(lat)//2]:.0f} ms (settle backstop)")
            assert lat[len(lat) // 2] < 400, lat

            async def burst():
                """Drain until quiet. A flush emits the previously displayed
                frame ahead of the new one by design -- the viewer lingers
                rather than jumping somewhere unrelated -- so the LAST packet
                is the one that matters."""
                got = []
                while True:
                    try:
                        r = await asyncio.wait_for(ws.receive(), 0.6)
                    except asyncio.TimeoutError:
                        return got
                    if r.type is aiohttp.WSMsgType.BINARY:
                        h = HDR.unpack_from(r.data, 0)
                        await ws.send_json({"type": "ack", "frame": h[0]})
                        got.append(h)

            await burst()
            await ws.send_json({"type": "seek", "frame": 11})
            got = await burst()
            assert got and got[-1][0] == 11, f"seek ended on {got[-1][0] if got else None}"
            await ws.send_json({"type": "step", "delta": 1})
            got = await burst()
            assert got and got[-1][0] == 12, f"step ended on {got[-1][0] if got else None}"
            await ws.send_json({"type": "step", "delta": -1})
            got = await burst()
            assert got and got[-1][0] == 11, f"back-step ended on {got[-1][0] if got else None}"
            print("  seek and step are frame-exact, both directions")

            await ws.send_json({"type": "play", "on": True})
            n, t0, seen = 0, time.perf_counter(), []
            while time.perf_counter() - t0 < 3.0:
                (fr, *_), _ = await next_frame()
                seen.append(fr); n += 1
            await ws.send_json({"type": "play", "on": False})
            dup = sum(1 for a, b in zip(seen, seen[1:]) if a == b)
            print(f"  playback {n/3:.1f} fps, duplicate frames {dup}")
            assert n / 3 > 20, f"playback only {n/3:.1f} fps"

            await ws.send_json({"type": "fps", "fps": 30})
            while True:
                r = await asyncio.wait_for(ws.receive(), 10)
                if r.type is aiohttp.WSMsgType.BINARY:
                    await ws.send_json({"type": "ack",
                                        "frame": HDR.unpack_from(r.data, 0)[0]})
                    continue
                m = json.loads(r.data)
                if m["type"] == "ready":
                    break
            assert m["fps"] == 30 and not m["note"], f"30fps on 30Hz should not warn: {m['note']}"
            print("  fps change ok, warning cleared at 30fps on 30Hz")

            await ws.send_json({"type": "look", "src": "ACES2065-1"})
            (fr, *_), body = await next_frame()
            assert body, "no frame after colourspace change"
            print("  input colourspace change ok")
    print("OK")


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
