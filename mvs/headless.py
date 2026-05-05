import logging
import os
import shutil
import subprocess
import time

log = logging.getLogger("browser_screencast")


class HeadlessSession:
    """Owns an Xvfb display and optional lightweight window manager."""

    def __init__(self, cfg):
        self.display = getattr(cfg, "headless_display", ":99") or ":99"
        self.size = getattr(cfg, "headless_size", "1920x1080x24") or "1920x1080x24"
        self.wm = getattr(cfg, "headless_wm", "auto") or "auto"
        self._procs = []
        self._started_xvfb = False
        self._old_display = None
        self._mutated_display = False

    def start(self):
        old_display = os.environ.get("DISPLAY")
        if old_display and old_display != self.display:
            log.info("DISPLAY already set to %s; headless Xvfb not started", old_display)
            return False
        if not shutil.which("Xvfb"):
            raise RuntimeError("headless mode requires Xvfb on PATH")

        env = os.environ.copy()
        env["DISPLAY"] = self.display
        try:
            if not self._display_ready(env):
                cmd = ["Xvfb", self.display, "-screen", "0", self.size, "-nolisten", "tcp"]
                log.info("Starting headless display: %s", " ".join(cmd))
                self._procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL))
                self._started_xvfb = True
                self._wait_display(env)

            self._old_display = old_display
            if old_display != self.display:
                os.environ["DISPLAY"] = self.display
                self._mutated_display = True

            if not self._start_wm(env):
                log.warning("No working lightweight window manager found; "
                            "headless display will be bare X11")
        except Exception:
            self.stop()
            raise
        return True

    def stop(self):
        for proc in reversed(self._procs):
            try:
                proc.terminate()
            except Exception:
                pass
        for proc in reversed(self._procs):
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._procs.clear()
        if self._mutated_display:
            if self._old_display is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = self._old_display
            self._mutated_display = False

    def _display_ready(self, env=None):
        if shutil.which("xdpyinfo"):
            return subprocess.run(["xdpyinfo"], env=env or os.environ.copy(),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        return False

    def _wait_display(self, env=None):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._display_ready(env):
                return
            time.sleep(0.1)
        raise RuntimeError(f"Xvfb display {self.display} did not become ready")

    def _wm_candidates(self):
        if self.wm == "none":
            return []
        if self.wm != "auto":
            exe = shutil.which(self.wm)
            if not exe:
                raise RuntimeError(f"headless window manager not found: {self.wm}")
            return [[exe]]
        candidates = []
        for name in ("openbox", "xfwm4", "fluxbox", "i3"):
            exe = shutil.which(name)
            if exe:
                candidates.append([exe])
        return candidates

    def _start_wm(self, env):
        for wm_cmd in self._wm_candidates():
            log.info("Starting headless window manager: %s", " ".join(wm_cmd))
            try:
                proc = subprocess.Popen(wm_cmd, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, env=env)
            except Exception as e:
                log.warning("Window manager failed to start: %s: %s", " ".join(wm_cmd), e)
                continue
            time.sleep(0.5)
            if proc.poll() is None:
                self._procs.append(proc)
                return True
            log.warning("Window manager exited immediately: %s status=%s",
                        " ".join(wm_cmd), proc.returncode)
        return False
