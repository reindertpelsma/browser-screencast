"""
Encode → decode round-trip tests for H264, H265, VP9.

Tests the full encoder pipeline in isolation from the browser.  If a codec
passes here, the bitstream is valid; if it passes here but corrupts in the
browser, the bug is in the WebCodecs frontend.  If it fails here, the encoder
itself is broken.

Run:
    cd /workspace/browser-screencast
    sudo -u ubuntu python3 tests/test_encoder_roundtrip.py [-v]
"""
import argparse
import sys
import numpy as np

sys.path.insert(0, "/workspace/browser-screencast")

import av  # noqa: E402 — must come after sys.path tweak
from mvs.codec import CODEC_H264, CODEC_H265, CODEC_VP9  # noqa: E402
from mvs.encoder import EncoderPipeline  # noqa: E402

# ---------------------------------------------------------------------------
# Frame generation
# ---------------------------------------------------------------------------

def make_frame(frame_num: int, w: int = 320, h: int = 240) -> np.ndarray:
    """Return an RGB frame with content that changes deterministically per frame.

    Uses a per-channel gradient + a moving bright bar so both colour and
    spatial structure vary — exercising both intra and inter coding paths.
    """
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    # Per-channel background gradient
    for y in range(h):
        rgb[y, :, 0] = int((frame_num * 13 + y) % 256)
        rgb[y, :, 1] = int((frame_num * 7  + y * 2) % 256)
        rgb[y, :, 2] = int((255 - frame_num * 5 + y) % 256)
    # Moving vertical bright bar (strong inter-frame signal)
    bar_x = (frame_num * 12) % w
    rgb[:, bar_x : bar_x + 16, :] = 255
    return rgb


# ---------------------------------------------------------------------------
# Encode helpers
# ---------------------------------------------------------------------------

def encode_frames(
    codec_id: int,
    frames: list,
    force_kf_at: set = None,
    mid_bitrate: int = None,
    mid_at: int = None,
    w: int = 320,
    h: int = 240,
    bitrate: int = 1_500_000,
) -> tuple:
    """Encode frames, returning (packets, source_map).

    packets    — list of (payload_bytes, is_kf) pairs (non-None only).
    source_map — parallel list: source_map[j] = index into `frames` for packets[j].

    SW codecs (libx264: 1-frame, libx265: 2-frame pipeline) buffer frames before
    outputting — encode(frame_i) yields frame_{i-N}.  The IDR retry loop leaves
    (retries-1) extra copies of the IDR in the pipeline.  source_map lets callers
    compare each decoded packet against the correct source frame regardless of
    pipeline depth or extra IDR copies.

    Model: pipeline_queue is a FIFO of source-frame indices currently buffered in
    the encoder.  Submitting frame i pushes i; receiving output pops the oldest.
    After each KF retry: reset queue to [kf_src]*(retries-1) — this reflects both
    the initial empty-pipeline case and the mid-stream case where the retries
    consumed (and discarded) any P-frames that were already buffered.
    """
    from collections import deque

    enc = EncoderPipeline(codec_id, w, h, bitrate)
    if enc.actual_codec != codec_id:
        raise RuntimeError(f"Encoder fell back to JPEG — codec unavailable")

    force_kf_at = force_kf_at or {0}
    packets: list = []
    source_map: list = []
    cap_ms = 1000
    i = 0
    max_retries = 8  # H265 needs ≤3 retries; give headroom

    # FIFO of source-frame indices buffered inside the encoder.
    # VP9: always empty (no pipeline delay, immediate output).
    # libx264: depth 1.  libx265: depth 2.
    pipeline_queue: deque = deque()

    while i < len(frames):
        cap_ms += 33
        if mid_bitrate is not None and i == mid_at:
            enc.set_bitrate(mid_bitrate)

        if i in force_kf_at:
            # Retry until the encoder outputs an actual IDR
            payload = None
            retries = 0
            for _ in range(max_retries):
                retries += 1
                payload, is_kf, _ = enc.encode_keyframe(frames[i], cap_ms, 85)
                cap_ms += 1
                if payload is not None:
                    break
            if payload is None:
                raise RuntimeError(f"encode_keyframe returned None {max_retries} times at frame {i}")
            packets.append((payload, True))
            source_map.append(i)
            # After the retry loop the encoder holds (retries-1) extra IDR copies.
            # Reset queue to reflect that: the discarded P-frames (mid-stream) or
            # the no-output priming frames (initial) have all been consumed.
            pipeline_queue.clear()
            pipeline_queue.extend([i] * (retries - 1))
        else:
            # Push this frame's source index, then encode.
            pipeline_queue.append(i)
            payload, is_kf, _ = enc.encode(frames[i], cap_ms, 75)
            if payload is not None:
                # Encoder produced output: dequeue the oldest buffered frame.
                src = pipeline_queue.popleft()
                source_map.append(src)
                packets.append((payload, is_kf))
            # If payload is None the frame stayed buffered; pipeline_queue is correct.

        i += 1

    enc.close()
    return packets, source_map


# ---------------------------------------------------------------------------
# Decode helpers
# ---------------------------------------------------------------------------

_DECODER_NAME = {
    CODEC_H264: "h264",
    CODEC_H265: "hevc",
    CODEC_VP9:  "vp9",
}


def decode_packets(codec_id: int, packets: list) -> list:
    """Decode (payload, is_kf) pairs, return list of RGB numpy arrays.

    Uses a memory-backed container (mp4 muxer) to give PyAV proper
    framing context, which also handles the decoder flush cleanly.
    Falls back to raw CodecContext if container mux fails.
    """
    name = _DECODER_NAME[codec_id]
    # Write encoded packets into an in-memory container so the demuxer
    # handles Annex-B → AVCC conversion and decoder flush for us.
    buf = _encode_to_container(codec_id, packets)
    if buf is not None:
        return _decode_from_container(buf)
    # Fallback: raw CodecContext (no flush, but good enough for testing)
    return _decode_raw(name, packets)


def _encode_to_container(codec_id: int, packets: list):
    """Mux raw packets into an in-memory MP4/WebM, return BytesIO or None."""
    import io
    fmt = "mp4" if codec_id in (CODEC_H264, CODEC_H265) else "webm"
    name = _DECODER_NAME[codec_id]
    buf = io.BytesIO()
    try:
        container = av.open(buf, mode="w", format=fmt)
        # Find dimensions from first packet by quick-decoding
        cc_tmp = av.CodecContext.create(name, "r")
        cc_tmp.open()
        w, h = 320, 240
        for payload, _ in packets[:4]:
            pkt = av.Packet(payload)
            frames = list(cc_tmp.decode(pkt))
            if frames:
                w, h = frames[0].width, frames[0].height
                break
        stream = container.add_stream(name, rate=30)
        stream.width = w
        stream.height = h
        stream.pix_fmt = "yuv420p"
        for payload, is_kf in packets:
            pkt = av.Packet(payload)
            pkt.stream = stream
            pkt.pts = None  # let muxer assign
            container.mux(pkt)
        container.close()
        buf.seek(0)
        return buf
    except Exception:
        return None


def _decode_from_container(buf) -> list:
    frames = []
    container = av.open(buf, mode="r")
    for stream in container.streams.video:
        stream.codec_context.options["flags2"] = "fast"
    for frame in container.decode(video=0):
        frames.append(frame.to_ndarray(format="rgb24"))
    container.close()
    return frames


def _decode_raw(name: str, packets: list) -> list:
    """Decode packets directly via CodecContext (no container framing)."""
    cc = av.CodecContext.create(name, "r")
    cc.open()
    decoded_frames = []
    for payload, _ in packets:
        pkt = av.Packet(payload)
        for frame in cc.decode(pkt):
            decoded_frames.append(frame.to_ndarray(format="rgb24"))
    # Flush
    try:
        for frame in cc.decode(av.Packet(b"")):
            decoded_frames.append(frame.to_ndarray(format="rgb24"))
    except Exception:
        pass
    return decoded_frames


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def psnr(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return 0.0
    mse = np.mean((a.astype(np.float32) - b.astype(np.float32)) ** 2)
    if mse < 1e-10:
        return float("inf")
    return 10.0 * np.log10(255.0 ** 2 / mse)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"

CODEC_NAMES = {CODEC_H264: "H264", CODEC_H265: "H265", CODEC_VP9: "VP9"}

# PSNR floor for lossy codecs — 22 dB catches real corruption (force-cfr=1 or
# IDR misalignment produce 7–13 dB) while tolerating H265's rate-control dips
# on high-motion frames (which naturally land in the 23–25 dB range).
# H264 and VP9 comfortably exceed 27 dB on the same sequences.
PSNR_FLOOR = 22.0


def _run(label: str, codec_id: int, frames: list, packets: list,
         source_map: list, verbose: bool) -> str:
    try:
        decoded = decode_packets(codec_id, packets)
    except Exception as e:
        print(f"  {FAIL}: decode raised {e}")
        return FAIL

    n = min(len(packets), len(decoded))
    if n == 0:
        print(f"  {FAIL}: no decoded frames")
        return FAIL

    scores = [psnr(frames[source_map[j]], decoded[j]) for j in range(n)]
    low = [(j, s) for j, s in enumerate(scores) if s < PSNR_FLOOR]
    avg = float(np.mean(scores))

    if verbose:
        for j, s in enumerate(scores):
            is_kf = packets[j][1]
            tag = "  KF" if is_kf else "    "
            print(f"    {tag} pkt {j:3d} (src={source_map[j]:3d}): PSNR={s:6.1f} dB")

    if low:
        bad = ", ".join(f"pkt {j} src={source_map[j]} ({s:.1f} dB)" for j, s in low[:5])
        print(f"  {FAIL}: low PSNR at {bad}  (avg={avg:.1f} dB, floor={PSNR_FLOOR} dB)")
        return FAIL

    print(f"  {PASS}: {n} packets decoded, avg PSNR={avg:.1f} dB, min={min(scores):.1f} dB")
    return PASS


def test_initial_keyframe_and_pframes(codec_id: int, verbose: bool) -> str:
    """15 P-frames after the initial IDR must all decode cleanly."""
    frames = [make_frame(i) for i in range(16)]
    packets, source_map = encode_frames(codec_id, frames, force_kf_at={0})
    kf_count = sum(1 for _, is_kf in packets if is_kf)
    if verbose:
        print(f"    {len(packets)} packets, {kf_count} keyframe(s)")
    # Expect at least 1 keyframe (pipeline bonus IDRs are accounted for by source_map)
    if kf_count == 0:
        print(f"  {FAIL}: no keyframe in packet stream")
        return FAIL
    return _run("initial_kf", codec_id, frames, packets, source_map, verbose)


def test_forced_keyframe_midstream(codec_id: int, verbose: bool) -> str:
    """Force a second IDR at frame 10, verify frames before and after are clean."""
    frames = [make_frame(i) for i in range(20)]
    packets, source_map = encode_frames(codec_id, frames, force_kf_at={0, 10})
    kf_count = sum(1 for _, is_kf in packets if is_kf)
    if verbose:
        kf_idx = [i for i, (_, is_kf) in enumerate(packets) if is_kf]
        print(f"    {len(packets)} packets, keyframes at packet indices {kf_idx}")
    if kf_count < 2:
        print(f"  {FAIL}: expected ≥2 keyframes, got {kf_count}")
        return FAIL
    return _run("mid_kf", codec_id, frames, packets, source_map, verbose)


def test_bitrate_switch_no_forced_idr(codec_id: int, verbose: bool) -> str:
    """Switch bitrate at frame 8; encoder must NOT inject a spontaneous IDR.

    A spontaneous IDR after bitrate change would not have the is_kf flag set
    by encode(), causing the browser to receive an IDR labeled type:'delta' —
    the decoder DPB resets but the JS side thinks it is a P-frame reference,
    leading to corruption.
    """
    frames = [make_frame(i) for i in range(20)]
    packets, source_map = encode_frames(
        codec_id, frames,
        force_kf_at={0},
        mid_bitrate=500_000,
        mid_at=8,
    )
    kf_packets = [(i, is_kf) for i, (_, is_kf) in enumerate(packets) if is_kf]
    # Pipeline bonus IDRs all appear at source_map[j] == 0 (first KF), never mid-stream.
    # A spontaneous encoder-injected IDR would appear with a non-KF source at j>2.
    spontaneous = [i for i, _ in kf_packets if i > 2]  # allow H265 startup bonus
    if verbose:
        print(f"    keyframe packets at indices: {[i for i, _ in kf_packets]}")
    if spontaneous:
        print(f"  {FAIL}: spontaneous IDR at packet {spontaneous} after bitrate switch")
        return FAIL
    return _run("bitrate_switch", codec_id, frames, packets, source_map, verbose)


def test_high_motion(codec_id: int, verbose: bool) -> str:
    """30 frames with maximum per-frame change (stress P-frame reference chain)."""
    # Alternate between two very different frames to maximise inter-frame diff
    frames = [make_frame(i * 60 % 256) for i in range(30)]
    packets, source_map = encode_frames(codec_id, frames, force_kf_at={0}, bitrate=2_000_000)
    return _run("high_motion", codec_id, frames, packets, source_map, verbose)


def test_static_then_motion(codec_id: int, verbose: bool) -> str:
    """10 identical frames (static screen), then 10 frames of motion."""
    static = make_frame(42)
    motion = [make_frame(i + 43) for i in range(10)]
    frames = [static] * 10 + motion
    packets, source_map = encode_frames(codec_id, frames, force_kf_at={0})
    return _run("static_then_motion", codec_id, frames, packets, source_map, verbose)


TESTS = [
    ("initial_keyframe_and_pframes", test_initial_keyframe_and_pframes),
    ("forced_keyframe_midstream",    test_forced_keyframe_midstream),
    ("bitrate_switch_no_idr",        test_bitrate_switch_no_forced_idr),
    ("high_motion",                  test_high_motion),
    ("static_then_motion",           test_static_then_motion),
]

CODECS = [CODEC_H264, CODEC_H265, CODEC_VP9]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--codec", choices=["h264", "h265", "vp9"], help="Test only this codec")
    p.add_argument("--test", help="Run only tests whose name contains this substring")
    args = p.parse_args()

    codecs = CODECS
    if args.codec:
        codecs = [{"h264": CODEC_H264, "h265": CODEC_H265, "vp9": CODEC_VP9}[args.codec]]

    results = {}
    total = fail = 0

    for codec_id in codecs:
        cname = CODEC_NAMES[codec_id]
        print(f"\n{'='*50}")
        print(f"  {cname}")
        print(f"{'='*50}")

        for test_name, test_fn in TESTS:
            if args.test and args.test not in test_name:
                continue
            print(f"\n  [{test_name}]")
            try:
                result = test_fn(codec_id, args.verbose)
            except Exception as e:
                print(f"  {FAIL}: exception: {e}")
                result = FAIL
            results[(cname, test_name)] = result
            total += 1
            if result == FAIL:
                fail += 1

    print(f"\n{'='*50}")
    print(f"Results: {total - fail}/{total} passed")
    if fail:
        print("FAILURES:")
        for (c, t), r in results.items():
            if r == FAIL:
                print(f"  {c} / {t}")
    print()
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
