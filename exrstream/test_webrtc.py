"""The WebRTC data-channel transport, against a running server.

The browser is the real client, but the two things that can silently corrupt
the stream have no browser in them: reassembling fragments in the right order,
and recovering from a loss without feeding the decoder a delta whose reference
never arrived. Both are checked here.
"""
import asyncio, json, ssl, struct, sys
import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription

HDR = struct.Struct("<IIfII")           # frame, epoch, ev, flags(1=key), seq
FRAG = struct.Struct("<IBB")            # seq, index, count


def reassemble(parts):
    """The client's fragment logic: index-ordered, complete packets only."""
    out, buf = [], {}
    for msg in parts:
        seq, i, n = FRAG.unpack_from(msg, 0)
        body = msg[FRAG.size:]
        if n == 1:
            out.append((seq, body))
            continue
        f = buf.setdefault(seq, {})
        f[i] = body
        if len(f) == n:
            out.append((seq, b"".join(f[i] for i in range(n))))
            del buf[seq]
    return out


async def main(url="https://127.0.0.1:8099"):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx)) as sess:
        async with sess.ws_connect(url + "/ws") as ws:
            seqs = json.loads((await ws.receive()).data)
            assert seqs["type"] == "sequences" and seqs["items"], seqs

            pc = RTCPeerConnection()
            dc = pc.createDataChannel("v", ordered=False, maxRetransmits=0)
            got, opened = [], asyncio.Event()
            dc.on("open", opened.set)
            dc.on("message", got.append)

            await pc.setLocalDescription(await pc.createOffer())
            await ws.send_json({"type": "offer", "sdp": pc.localDescription.sdp})

            biggest = max(seqs["items"], key=lambda i: i["frames"])
            await ws.send_json({"type": "open", "key": biggest["key"], "count": 24})

            ready = None
            while ready is None:
                m = json.loads((await asyncio.wait_for(ws.receive(), 60)).data)
                if m["type"] == "answer":
                    await pc.setRemoteDescription(
                        RTCSessionDescription(m["sdp"], "answer"))
                elif m["type"] == "ready":
                    ready = m
                elif m["type"] == "error":
                    raise AssertionError(m["msg"])
            await asyncio.wait_for(opened.wait(), 15)
            print(f"  data channel open, {ready['w']}x{ready['h']}")

            # Pixels must actually arrive over the channel, not the socket.
            await ws.send_json({"type": "play", "on": True})
            for _ in range(400):
                if len(got) >= 30:
                    break
                await asyncio.sleep(0.05)
                for _ in range(8):                      # keep the ack budget open
                    await ws.send_json({"type": "ack", "frame": 0})
            await ws.send_json({"type": "play", "on": False})
            assert len(got) >= 30, f"only {len(got)} fragments over the channel"

            pkts = reassemble(got)
            assert pkts, "no packet reassembled"
            print(f"  {len(got)} fragments -> {len(pkts)} packets")

            hdrs = [HDR.unpack_from(p, 0) for _, p in pkts]
            assert all(h[4] == s for (s, _), h in zip(pkts, hdrs)), \
                "fragment seq does not match the packet it carries"
            nums = [h[4] for h in hdrs]
            assert nums == sorted(nums), f"packets reassembled out of order: {nums[:8]}"
            assert nums[-1] - nums[0] == len(nums) - 1, \
                f"gap in a sequence nothing dropped: {nums}"
            assert hdrs[0][3] == 1, "the first packet must be a key frame"
            # Every packet is a whole annex-b access unit, not a torn one.
            for _, p in pkts:
                assert p[HDR.size:HDR.size + 3] in (b"\x00\x00\x01", b"\x00\x00\x00"), \
                    "reassembled payload does not start on a NAL boundary"
            big = max(len(p) for _, p in pkts)
            print(f"  packets in order, largest {big} B, "
                  f"{max(FRAG.unpack_from(m,0)[2] for m in got)} fragments max")

            # Loss recovery: the client asks for an IDR mid-GOP and must get one
            # well before the next scheduled key frame (the GOP is a second).
            got.clear()
            await ws.send_json({"type": "idr"})
            for _ in range(100):
                if got:
                    break
                await asyncio.sleep(0.02)
            keyed = [h for _, p in reassemble(got) for h in [HDR.unpack_from(p, 0)]]
            assert keyed and keyed[0][3] == 1, \
                f"idr request did not produce a key frame: {keyed[:3]}"
            print("  idr request answered with a key frame")

            await pc.close()
    print("OK")


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
