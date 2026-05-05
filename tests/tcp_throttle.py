#!/usr/bin/env python3
"""tcp_throttle.py — rate-limiting TCP proxy for local congestion testing.

The server writes into the proxy's buffer freely (localhost, drains instantly,
no TCP backpressure). The proxy drains toward the client at the requested rate,
accumulating data in its own virtually-infinite buffer. Upload (client→server)
is always unthrottled so lag reports flow freely.

Two buffer modes:
  separate (default) — each TCP connection has its own independent token bucket.
                       Metric/ping channel is unaffected by video load.
  shared             — all connections draw from one bucket (total budget = kbps).
                       Realistic: video crowding the link inflates ping RTTs.

Optional network impairments (downstream only):
  --latency N   Fixed one-way delay in ms added to every chunk.
  --jitter N    Std-dev of per-chunk Gaussian jitter in ms (total delay = latency + N(0,jitter)).

Backpressure guard:
  --max-buf N   If the proxy's in-flight buffer exceeds N MB, a warning is
                printed and the overrun is counted. A non-zero count at exit
                means the server received real TCP backpressure — the test is
                invalid (the buffer was not virtually infinite).

Usage:
  # Separate buffers, no impairment (current simple mode):
  python3 tests/tcp_throttle.py 6082 6081 2000

  # Shared buffer, 30ms latency, 15ms jitter (realistic hotel WiFi):
  python3 tests/tcp_throttle.py 6082 6081 2000 --mode shared --latency 30 --jitter 15

  # Then in another terminal:
  python3 tests/test_2mbps.py 6082 guacweb 20 1.7
"""
import asyncio, argparse, random, sys, time

# ---------------------------------------------------------------------------
ap = argparse.ArgumentParser(description="Rate-limiting TCP proxy")
ap.add_argument("listen_port",   type=int)
ap.add_argument("upstream_port", type=int)
ap.add_argument("kbps",          type=int)
ap.add_argument("--mode",      choices=["separate", "shared"], default="separate")
ap.add_argument("--latency",   type=float, default=0,  metavar="MS",
                help="Fixed one-way downstream delay in ms (default 0)")
ap.add_argument("--jitter",    type=float, default=0,  metavar="MS",
                help="Gaussian jitter std-dev in ms (default 0)")
ap.add_argument("--max-buf",   type=float, default=50, metavar="MB",
                help="Soft buffer ceiling in MB — overrun = backpressure warning (default 50)")
args = ap.parse_args()

RATE    = args.kbps * 1000 / 8           # bytes / sec
MAX_BUF = int(args.max_buf * 1024 * 1024)

# ---------------------------------------------------------------------------
# Shared token bucket (used when --mode=shared)
# ---------------------------------------------------------------------------
class _SharedBucket:
    def __init__(self):
        self._tokens  = 0.0
        self._last_t  = time.monotonic()
        self._lock    = asyncio.Lock()

    async def consume(self, n: int) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._tokens + (now - self._last_t) * RATE, RATE * 2)
            self._last_t = now
            deficit = n - self._tokens
            if deficit > 0:
                self._tokens = 0.0
                await asyncio.sleep(deficit / RATE)
            else:
                self._tokens -= n

_shared = _SharedBucket()

# ---------------------------------------------------------------------------
# Global stats
# ---------------------------------------------------------------------------
_queued_bytes    = 0          # bytes in proxy buffer right now
_buf_overruns    = 0          # times the ceiling was breached
_total_conns     = 0

# ---------------------------------------------------------------------------
async def _throttled(src: asyncio.StreamReader, dst: asyncio.StreamWriter,
                     tag: str, shared_bucket) -> None:
    global _queued_bytes, _buf_overruns
    tokens = 0.0
    last_t = time.monotonic()
    total  = 0

    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break

            # --- Backpressure guard -----------------------------------------
            _queued_bytes += len(chunk)
            if _queued_bytes > MAX_BUF:
                _buf_overruns += 1
                print(f"  [{tag}] BUF OVERRUN {_queued_bytes/1024/1024:.1f}MB "
                      f"> {args.max_buf}MB — backpressure is reaching the server",
                      flush=True)

            # --- Rate limit -------------------------------------------------
            if shared_bucket is not None:
                await shared_bucket.consume(len(chunk))
            else:
                now    = time.monotonic()
                tokens = min(tokens + (now - last_t) * RATE, RATE * 2)
                last_t = now
                deficit = len(chunk) - tokens
                if deficit > 0:
                    await asyncio.sleep(deficit / RATE)
                    tokens = 0.0
                else:
                    tokens -= len(chunk)

            # --- Latency + jitter -------------------------------------------
            if args.latency > 0 or args.jitter > 0:
                delay = max(0.0, args.latency + random.gauss(0, args.jitter))
                await asyncio.sleep(delay / 1000.0)

            dst.write(chunk)
            await dst.drain()
            total         += len(chunk)
            _queued_bytes -= len(chunk)

    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        # Ensure queued counter doesn't go stale if we exit mid-stream
        pass

    print(f"  [{tag}] closed  {total/1024:.0f}KB", flush=True)
    try:
        dst.close()
    except Exception:
        pass


async def _passthrough(src: asyncio.StreamReader, dst: asyncio.StreamWriter,
                       tag: str) -> None:
    total = 0
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
            total += len(chunk)
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    print(f"  [{tag}] closed  {total/1024:.0f}KB", flush=True)
    try:
        dst.close()
    except Exception:
        pass


async def _handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter):
    global _total_conns
    _total_conns += 1
    conn_id = _total_conns
    peer = client_w.get_extra_info("peername")
    print(f"  [conn {conn_id}] {peer} → :{args.upstream_port}", flush=True)

    server_r, server_w = await asyncio.open_connection("127.0.0.1", args.upstream_port)
    bucket = _shared if args.mode == "shared" else None

    await asyncio.gather(
        _throttled(server_r, client_w, f"conn{conn_id} ↓", bucket),
        _passthrough(client_r, server_w, f"conn{conn_id} ↑"),
        return_exceptions=True,
    )


async def main():
    srv = await asyncio.start_server(_handle, "127.0.0.1", args.listen_port)
    print(
        f"tcp_throttle  :{args.listen_port} → :{args.upstream_port}  "
        f"{args.kbps}kbps  mode={args.mode}  "
        f"latency={args.latency}ms  jitter={args.jitter}ms  "
        f"max_buf={args.max_buf}MB",
        flush=True,
    )
    try:
        async with srv:
            await srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\ntcp_throttle stats: conns={_total_conns}  buf_overruns={_buf_overruns}")
        if _buf_overruns:
            print("  *** BACKPRESSURE DETECTED — test results are invalid ***")
            sys.exit(2)


asyncio.run(main())
