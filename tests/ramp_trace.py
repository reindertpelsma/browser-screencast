#!/usr/bin/env python3
"""ramp_trace.py — measure how fast the server ramps bitrate after an idle period.

Connects like the browser does (caps handshake, 10/s honest lag reports) and
records, per 500ms bucket: frames, wire bytes, throughput, mean frame size.
Correlates the trace against a phase-marker file written by the X11 fixture
(``idle:20,active:10,...``) and reports, for every idle→active transition:

  * time to the first frame larger than IDLE_FRAME_BYTES
  * time for the 1s trailing throughput to cross TARGET_MBPS

Usage:
  python3 tests/ramp_trace.py --port 6099 [--token T] [--markers /tmp/phases.txt]
                              [--duration 70] [--target-mbps 2.0] [--out trace.json]
"""
import argparse
import asyncio
import json
import struct
import sys
import time

HDR = ">IQBBI"
HDR_LEN = struct.calcsize(HDR)


def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6081)
    p.add_argument("--token", default="")
    p.add_argument("--markers", default="")
    p.add_argument("--duration", type=float, default=70.0)
    p.add_argument("--target-mbps", type=float, default=2.0)
    p.add_argument("--idle-frame-bytes", type=int, default=4000,
                   help="a frame bigger than this counts as 'real content'")
    p.add_argument("--codec", default="h264")
    p.add_argument("--w", type=int, default=1280)
    p.add_argument("--h", type=int, default=720)
    p.add_argument("--maxkbps", type=int, default=0)
    p.add_argument("--out", default="")
    p.add_argument("--label", default="trace")
    return p.parse_args(argv)


async def run(a):
    import websockets

    url = f"ws://{a.host}:{a.port}/" + (f"?token={a.token}" if a.token else "")
    frames = []          # (t_rel, nbytes, age_ms)
    t0 = None
    last_lag = 0.0
    # Mirror frontend/index.html's _linkAgeFromServerTimestamp: server and client
    # clocks are never synchronised, so the raw (now - send_ts) carries clock skew
    # plus one-way latency. The browser calibrates that out by tracking the minimum
    # raw age seen and reporting only the growth above it. Reporting the raw value
    # instead makes every frame on a 150ms WAN path look like 150ms of congestion,
    # which pins the controller at the bitrate floor and invalidates the trace.
    clock_offset = None

    async with websockets.connect(url, open_timeout=10,
                                  max_size=32 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "t": "caps", "webcodecs": True, "codecs": [a.codec],
            "codecCaps": {a.codec: {"hw": False, "sw": True}},
            "explicit": True, "w": a.w, "h": a.h,
        }))
        if a.maxkbps:
            await ws.send(json.dumps({"t": "quality", "cap_h": 0, "fps": 0,
                                      "maxkbps": a.maxkbps, "lag_ms": 0}))
        t0 = time.monotonic()
        while time.monotonic() - t0 < a.duration:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
            except asyncio.TimeoutError:
                continue
            if not isinstance(msg, (bytes, bytearray)) or len(msg) < HDR_LEN:
                continue
            _, ts_ms, _codec, _kf, _plen = struct.unpack_from(HDR, msg)
            now = time.monotonic()
            raw_age = int(time.time() * 1000) - ts_ms
            if clock_offset is None or raw_age < clock_offset:
                clock_offset = raw_age
            age = max(1, raw_age - clock_offset)
            frames.append((now - t0, len(msg), age))
            if now - last_lag >= 0.1:
                last_lag = now
                # Honest report: loopback, so age is tiny → "path is clear".
                await ws.send(json.dumps({"t": "lag", "age_ms": age}))
    return frames, t0


def bucketize(frames, width=0.5):
    if not frames:
        return []
    end = frames[-1][0]
    n = int(end / width) + 1
    out = []
    for i in range(n):
        lo, hi = i * width, (i + 1) * width
        sel = [f for f in frames if lo <= f[0] < hi]
        nbytes = sum(f[1] for f in sel)
        out.append({
            "t": round(lo, 2),
            "frames": len(sel),
            "fps": len(sel) / width,
            "bytes": nbytes,
            "mbps": nbytes * 8 / 1e6 / width,
            "mean_frame": (nbytes // len(sel)) if sel else 0,
            "max_frame": max((f[1] for f in sel), default=0),
        })
    return out


def trailing_mbps(frames, t, window=1.0):
    sel = [f for f in frames if t - window <= f[0] <= t]
    return sum(f[1] for f in sel) * 8 / 1e6 / window


def read_markers(path):
    marks = []
    try:
        for line in open(path):
            parts = line.split()
            if len(parts) >= 2:
                # "<monotonic> [<wallclock>] <phase>"
                marks.append((float(parts[0]), parts[-1]))
    except OSError:
        pass
    return marks


def analyse(frames, marks, a, offset=0.0):
    """offset = client t0 minus fixture t0, in seconds."""
    res = []
    actives = [(t + offset, n) for t, n in marks if n == "active"]
    for t_act, _ in actives:
        first_real = None
        reach = None
        for ft, nb, _age in frames:
            if ft < t_act:
                continue
            if first_real is None and nb > a.idle_frame_bytes:
                first_real = ft - t_act
            if reach is None and trailing_mbps(frames, ft, 1.0) >= a.target_mbps:
                reach = ft - t_act
            if first_real is not None and reach is not None:
                break
        res.append({"active_at": round(t_act, 2),
                    "t_first_real_frame_s": None if first_real is None else round(first_real, 3),
                    f"t_reach_{a.target_mbps}mbps_s": None if reach is None else round(reach, 3)})
    return res


def main(argv=None):
    a = parse_args(argv)
    frames, t0_abs = asyncio.run(run(a))
    buckets = bucketize(frames)
    # Marker file holds absolute CLOCK_MONOTONIC stamps (shared across processes
    # on Linux), so phases line up with the client's own timeline exactly.
    marks = [(t - t0_abs, n) for t, n in read_markers(a.markers)] if a.markers else []
    report = {
        "label": a.label,
        "total_frames": len(frames),
        "duration_s": round(frames[-1][0], 2) if frames else 0,
        "buckets": buckets,
        "markers": marks,
        "ramp": analyse(frames, marks, a) if marks else [],
    }
    print(f"=== {a.label} ===")
    print(f"frames={len(frames)}  duration={report['duration_s']}s")
    print(" t(s)   fps    Mbps   meanFrame  maxFrame")
    for b in buckets:
        print(f"{b['t']:6.1f} {b['fps']:5.1f} {b['mbps']:7.3f} {b['mean_frame']:10d} {b['max_frame']:9d}")
    if marks:
        print("markers:", marks)
        for r in report["ramp"]:
            print("  ramp:", r)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(report, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
