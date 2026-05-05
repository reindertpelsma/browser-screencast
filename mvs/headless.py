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

    def start(self):
        if os.environ.get("DISPLAY") and os.environ["DISPLAY"] != self.display:
            log.info("DISPLAY already set to %s; headless Xvfb not started", os.environ["DISPLAY"])
            return False
        if not shutil.which("Xvfb"):
            raise RuntimeError("headless mode requires Xvfb on PATH")

        os.environ["DISPLAY"] = self.display
        if not self._display_ready():
            cmd = ["Xvfb", self.display, "-screen", "0", self.size, "-nolisten", "tcp"]
            log.info("Starting headless display: %s", " ".join(cmd))
            self._procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            self._started_xvfb = True
            self._wait_display()

        wm_cmd = self._select_wm()
        if wm_cmd:
            log.info("Starting headless window manager: %s", " ".join(wm_cmd))
            self._procs.append(subprocess.Popen(wm_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(0.5)
        else:
            log.warning("No lightweight window manager found; headless display will be bare X11")
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

    def _display_ready(self):
        if shutil.which("xdpyinfo"):
            return subprocess.run(["xdpyinfo"], env=os.environ.copy(),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        return False

    def _wait_display(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._display_ready():
                return
            time.sleep(0.1)
        raise RuntimeError(f"Xvfb display {self.display} did not become ready")

    def _select_wm(self):
        if self.wm == "none":
            return None
        if self.wm != "auto":
            exe = shutil.which(self.wm)
            if not exe:
                raise RuntimeError(f"headless window manager not found: {self.wm}")
            return [exe]
        for name in ("openbox", "xfwm4", "fluxbox", "i3"):
            exe = shutil.which(name)
            if exe:
                return [exe]
        return None
