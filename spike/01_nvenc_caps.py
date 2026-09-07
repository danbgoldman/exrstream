"""Phase 0 #1a: does GB10 have NVENC, and what can it do?"""
import PyNvVideoCodec as nvc

KEYS = ["num_encoder_engines", "width_max", "height_max", "num_max_bframes",
        "support_10bit_encode", "support_yuv444_encode", "support_lossless_encode",
        "support_alpha_layer_encoding", "support_bframe_ref_mode",
        "support_constrained_encoding", "support_lookahead", "support_temporal_aq",
        "support_dyn_bitrate_change", "support_dyn_res_change"]

for codec in ("h264", "hevc", "av1"):
    try:
        caps = nvc.GetEncoderCaps(0, codec)
    except Exception as e:
        print(f"{codec:5s} UNSUPPORTED: {type(e).__name__}: {e}")
        continue
    have = {k: caps[k] for k in KEYS if k in caps}
    print(f"{codec:5s} OK  " + "  ".join(f"{k.replace('support_','').replace('_encode',''):18s}={v}" for k, v in have.items()))
    missing = set(caps) - set(KEYS)
    if codec == "h264":
        print("       (other caps:", ", ".join(sorted(missing)), ")")
