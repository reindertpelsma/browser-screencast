#!/usr/bin/env python3
"""Linux validation matrix for browser-screencast.

Runs strict local coverage for the Linux/X11 and headless paths:
  - static/unit checks
  - Xvfb/openbox video smoke
  - X11 input e2e
  - X11 clipboard e2e
  - PulseAudio loopback audio smoke
  - server-managed headless Xvfb smoke
  - 2 Mbps real TCP proxy congestion test
"""

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
TOKEN = "testtoken"


class MatrixFailure(RuntimeError):
    pass


def _run(cmd, *, env=None, timeout=None):
    print(f"\n$ {' '.join(cmd)}", flush=True)
    merged = os.environ.copy()
    if env:
        merged.update(env)
    subprocess.run(cmd, cwd=ROOT, env=merged, timeout=timeout, check=True)


def _spawn(cmd, log_path, *, env=None):
    merged = os.environ.copy()
    if env:
        merged.update(env)
    log = open(log_path, "w")
    proc = subprocess.Popen(cmd, cwd=ROOT, env=merged, stdout=log, stderr=subprocess.STDOUT)
    proc._bs_log = log
    return proc


def _stop(procs):
    for proc in reversed(procs):
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
    deadline = time.monotonic() + 3
    for proc in reversed(procs):
        remaining = max(0.1, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log = getattr(proc, "_bs_log", None)
        if log:
            log.close()


def _wait_log(path, needle, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists() and needle in Path(path).read_text(errors="replace"):
            return
        time.sleep(0.1)
    text = Path(path).read_text(errors="replace") if Path(path).exists() else ""
    raise MatrixFailure(f"timed out waiting for {needle!r} in {path}\n{text[-4000:]}")


def _display_ready(display, timeout=5):
    env = os.environ.copy()
    env["DISPLAY"] = display
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if subprocess.run(["xdpyinfo"], env=env, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            return
        time.sleep(0.1)
    raise MatrixFailure(f"display {display} did not become ready")


def _require(exe):
    if not shutil.which(exe):
        raise MatrixFailure(f"{exe} is required")


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _xvfb_case(display, size, body):
    _require("Xvfb")
    _require("xdpyinfo")
    Path(f"/tmp/.X{display[1:]}-lock").unlink(missing_ok=True)
    procs = []
    try:
        procs.append(_spawn(["Xvfb", display, "-screen", "0", size, "-nolisten", "tcp"],
                            f"/tmp/bs-matrix-xvfb-{display[1:]}.log"))
        _display_ready(display)
        if shutil.which("openbox"):
            procs.append(_spawn(["openbox"], f"/tmp/bs-matrix-openbox-{display[1:]}.log",
                                env={"DISPLAY": display}))
            time.sleep(0.5)
        body(display, procs)
    finally:
        _stop(procs)
        Path(f"/tmp/.X{display[1:]}-lock").unlink(missing_ok=True)


def _start_server(display, procs, *, port=6081, audio=False, headless=False, extra=None):
    log = f"/tmp/bs-matrix-server-{port}.log"
    cmd = [PY, "server.py", "--listen", "127.0.0.1", "--port", str(port),
           "--password", TOKEN, "--codec", "h264"]
    if not audio:
        cmd.append("--no-audio")
    if headless:
        cmd += ["--headless", "--headless-display", display, "--headless-size", "640x360x24"]
    if extra:
        cmd += extra
    env = {} if headless else {"DISPLAY": display}
    if headless:
        env["HEADLESS_DISPLAY"] = display
        env.pop("DISPLAY", None)
    procs.append(_spawn(cmd, log, env=env))
    _wait_log(log, f"Listening 127.0.0.1:{port}", timeout=12)
    return log


def static_checks(_args):
    unit_tests = [p for p in sorted(Path("tests").glob("test_*.py"))
                  if p.name != "test_2mbps.py"]
    for test in unit_tests:
        _run([PY, str(test)])
    _run([PY, "-m", "py_compile", "server.py", *map(str, sorted(Path("mvs").glob("*.py"))),
          *map(str, sorted(Path("tests").glob("*.py")))])
    _run([PY, "server.py", "--print-caps"])


def video_smoke(_args):
    def body(display, procs):
        port = _free_port()
        procs.append(_spawn([PY, "tests/x11_animate.py"], "/tmp/bs-matrix-anim.log",
                            env={"DISPLAY": display, "STRESS_MODE": "balls", "ANIM_FPS": "60"}))
        time.sleep(1)
        _start_server(display, procs, port=port)
        _run([PY, "tests/ci_smoke.py", str(port), TOKEN], timeout=20)
    _xvfb_case(":91", "640x360x24", body)


def input_clipboard(_args):
    def body(display, procs):
        port = _free_port()
        _start_server(display, procs, port=port)
        _run([PY, "tests/x11_input_e2e.py", str(port), TOKEN],
             env={"DISPLAY": display}, timeout=15)
        _run([PY, "tests/x11_clipboard_e2e.py", str(port), TOKEN],
             env={"DISPLAY": display}, timeout=20)
    _xvfb_case(":92", "640x360x24", body)


def _start_pulse(procs):
    _require("pactl")
    _require("pulseaudio")
    subprocess.run(["pulseaudio", "--start", "--exit-idle-time=-1"], check=False)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if subprocess.run(["pactl", "info"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(0.1)
    else:
        raise MatrixFailure("PulseAudio did not start")
    subprocess.run(["pactl", "load-module", "module-null-sink", "sink_name=bs_test",
                    "sink_properties=device.description=bs_test", "rate=48000", "channels=2"],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["pactl", "set-default-sink", "bs_test"], check=True)
    procs.append(_spawn(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                         "-ac", "2", "-f", "pulse", "bs_test"],
                        "/tmp/bs-matrix-tone.log"))


def audio_smoke(_args):
    def body(display, procs):
        port = _free_port()
        _start_pulse(procs)
        procs.append(_spawn([PY, "tests/x11_animate.py"], "/tmp/bs-matrix-audio-anim.log",
                            env={"DISPLAY": display, "STRESS_MODE": "balls", "ANIM_FPS": "30"}))
        time.sleep(1)
        _start_server(display, procs, port=port, audio=True)
        _run([PY, "tests/ci_smoke.py", str(port), TOKEN], timeout=25)
    try:
        _xvfb_case(":93", "640x360x24", body)
    finally:
        subprocess.run(["pulseaudio", "-k"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)


def headless_smoke(_args):
    display = ":94"
    port = _free_port()
    Path(f"/tmp/.X{display[1:]}-lock").unlink(missing_ok=True)
    procs = []
    try:
        _start_server(display, procs, port=port, headless=True)
        procs.append(_spawn([PY, "tests/x11_animate.py"], "/tmp/bs-matrix-headless-anim.log",
                            env={"DISPLAY": display, "STRESS_MODE": "balls", "ANIM_FPS": "60"}))
        time.sleep(1)
        _run([PY, "tests/ci_smoke.py", str(port), TOKEN], timeout=20)
    finally:
        _stop(procs)
        Path(f"/tmp/.X{display[1:]}-lock").unlink(missing_ok=True)


def throttle_2mbps(args):
    duration = "45" if args.fast else "90"
    warmup = "25" if args.fast else "40"
    tail = "10" if args.fast else "15"

    def body(display, procs):
        server_port = _free_port()
        proxy_port = _free_port()
        procs.append(_spawn([PY, "tests/x11_animate.py"], "/tmp/bs-matrix-noise.log",
                            env={"DISPLAY": display, "STRESS_MODE": "noise",
                                 "NOISE_TILE": "128", "ANIM_FPS": "60"}))
        time.sleep(1)
        _start_server(display, procs, port=server_port,
                      extra=["--initial-bitrate", "30000000"])
        throttle_log = "/tmp/bs-matrix-throttle.log"
        procs.append(_spawn([PY, "tests/tcp_throttle.py", str(proxy_port),
                             str(server_port), "2000",
                             "--mode", "separate", "--max-buf", "256"], throttle_log))
        _wait_log(throttle_log, "tcp_throttle", timeout=5)
        env = {"DURATION": duration, "WARMUP_S": warmup, "TAIL_S": tail, "MIN_PEAK_LAG_MS": "500"}
        _run([PY, "tests/test_2mbps.py", str(proxy_port), TOKEN, "20", "1.4"], env=env,
             timeout=float(duration) + 20)
        if "BUF OVERRUN" in Path(throttle_log).read_text(errors="replace"):
            raise MatrixFailure("tcp proxy buffer overrun: TCP backpressure reached server")

    _xvfb_case(":95", "960x540x24", body)


CASES = [
    ("static", static_checks),
    ("video", video_smoke),
    ("input_clipboard", input_clipboard),
    ("audio", audio_smoke),
    ("headless", headless_smoke),
    ("throttle_2mbps", throttle_2mbps),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="shorten the 2 Mbps case for iteration")
    ap.add_argument("cases", nargs="*", help="case names to run")
    args = ap.parse_args()
    selected = set(args.cases)
    failures = []
    for name, fn in CASES:
        if selected and name not in selected:
            continue
        print(f"\n=== {name} ===", flush=True)
        try:
            fn(args)
            print(f"=== {name}: PASS ===", flush=True)
        except Exception as e:
            print(f"=== {name}: FAIL: {e} ===", flush=True)
            failures.append((name, str(e)))
    if failures:
        print("\nFAILURES:")
        for name, err in failures:
            print(f"  - {name}: {err}")
        sys.exit(1)
    print("\nLinux matrix PASS")


if __name__ == "__main__":
    main()
