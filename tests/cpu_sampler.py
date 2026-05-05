"""Background sampler for server CPU usage during websocket stress tests."""
import subprocess
import threading
import time

SATURATION_PCT = 90.0
HEADROOM_PCT = 80.0


class CpuSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self.max_python = 0.0
        self.samples = 0

    def run(self):
        pid = self._find_pid("python3 server.py") or self._find_pid("python server.py") or self._find_pid("server.py")
        if pid is None:
            return
        while not self._stop.is_set():
            self._sample(pid)
            self.samples += 1
            time.sleep(0.5)

    def _sample(self, pid):
        try:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "%cpu="],
                text=True, timeout=1,
            ).strip()
            self.max_python = max(self.max_python, float(out))
        except Exception:
            pass

    def _find_pid(self, pattern):
        try:
            out = subprocess.check_output(["pgrep", "-f", pattern], text=True).strip()
            return int(out.split("\n")[0]) if out else None
        except Exception:
            return None

    def stop(self, timeout=2.0):
        self._stop.set()
        self.join(timeout=timeout)

    @property
    def saturated(self) -> bool:
        return self.max_python > SATURATION_PCT

    @property
    def runner_capped(self) -> bool:
        return self.samples > 0 and self.max_python < HEADROOM_PCT

    def summary(self) -> str:
        return f"python={self.max_python:.0f}% samples={self.samples}"
