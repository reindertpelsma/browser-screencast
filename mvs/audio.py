import asyncio
import logging
import os
import platform
import queue
import shutil
import struct
import subprocess
import threading
import time

import numpy as np

from mvs.codec import _AV_OK

log = logging.getLogger("browser_screencast")

_audio_clients = 0
_audio_subs = {}
_audio_subs_lock = threading.Lock()
_audio_pcm_q: queue.Queue = queue.Queue(maxsize=500)
_audio_threads_started = False


def _put_drop_oldest(aq, msg):
    while aq.full():
        try:
            aq.get_nowait()
        except Exception:
            break
    try:
        aq.put_nowait(msg)
    except Exception:
        pass


def _run(cmd, timeout=2):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


def _has_audio_clients() -> bool:
    return _audio_clients > 0


def _ffmpeg_devices_text(ffmpeg):
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-devices"],
                              capture_output=True, text=True, timeout=4)
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception:
        return ""


def _linux_audio_input():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    pactl = shutil.which("pactl")
    if pactl:
        sink = _run([pactl, "get-default-sink"])
        if sink:
            return [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "pulse", "-i", f"{sink}.monitor",
                    "-ac", "2", "-ar", "48000", "-f", "f32le", "pipe:1"]
        sources = _run([pactl, "list", "short", "sources"]).splitlines()
        for line in sources:
            parts = line.split()
            if len(parts) >= 2 and parts[1].endswith(".monitor"):
                return [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-f", "pulse", "-i", parts[1],
                        "-ac", "2", "-ar", "48000", "-f", "f32le", "pipe:1"]
    if shutil.which("pw-cli"):
        return [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "pipewire", "-i", "default",
                "-ac", "2", "-ar", "48000", "-f", "f32le", "pipe:1"]
    return [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "alsa", "-i", "default",
            "-ac", "2", "-ar", "48000", "-f", "f32le", "pipe:1"]


def _windows_audio_input():
    ffmpeg = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    explicit = os.environ.get("BROWSER_SCREENCAST_AUDIO_DEVICE") or os.environ.get("AUDIO_DEVICE")
    devices = _ffmpeg_devices_text(ffmpeg)
    if " wasapi " in devices or "\n D  wasapi" in devices:
        return [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "wasapi", "-i", explicit or "default",
                "-ac", "2", "-ar", "48000", "-f", "f32le", "pipe:1"]
    if explicit and (" dshow " in devices or "\n D  dshow" in devices):
        return [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-f", "dshow", "-i", f"audio={explicit}",
                "-ac", "2", "-ar", "48000", "-f", "f32le", "pipe:1"]
    log.warning("Windows audio capture needs FFmpeg wasapi support or BROWSER_SCREENCAST_AUDIO_DEVICE for dshow")
    return None


def _audio_input_cmd():
    if platform.system() == "Windows":
        return _windows_audio_input()
    if platform.system() == "Linux":
        return _linux_audio_input()
    return None


def _audio_capture_thread():
    frame_bytes = 960 * 2 * 4  # 20 ms, stereo, float32 interleaved
    while True:
        if not _has_audio_clients():
            time.sleep(0.25)
            continue
        cmd = _audio_input_cmd()
        if not cmd:
            log.warning("Audio capture disabled: ffmpeg/audio source not found")
            time.sleep(10)
            continue
        log.info("Audio capture: %s", " ".join(cmd[:8]) + " ...")
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            while True:
                if not _has_audio_clients():
                    break
                raw = proc.stdout.read(frame_bytes)
                if len(raw) != frame_bytes:
                    break
                while _audio_pcm_q.full():
                    try:
                        _audio_pcm_q.get_nowait()
                    except queue.Empty:
                        break
                _audio_pcm_q.put_nowait(raw)
        except Exception as e:
            log.debug("audio capture: %s", e)
        finally:
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=1)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        time.sleep(2)


def _audio_encoder_thread():
    if not _AV_OK:
        log.warning("Audio encoder disabled: PyAV unavailable")
        return
    try:
        import av as _av_audio
        codec_ctx = _av_audio.CodecContext.create("libopus", "w")
        codec_ctx.sample_rate = 48000
        codec_ctx.format = "flt"
        codec_ctx.bit_rate = 128000
        try:
            codec_ctx.layout = "stereo"
        except AttributeError:
            try:
                codec_ctx.channel_layout = "stereo"
            except AttributeError:
                codec_ctx.channels = 2
        codec_ctx.open()
        log.info("Audio encoder: Opus ready (48 kHz stereo)")
    except Exception as e:
        log.warning("Audio encoder init failed (%s); audio disabled", e)
        return

    pts = 0
    while True:
        try:
            raw = _audio_pcm_q.get(timeout=5)
        except queue.Empty:
            continue
        if _audio_clients == 0:
            continue
        try:
            interleaved = np.frombuffer(raw, dtype=np.float32)
            interleaved = np.ascontiguousarray(np.clip(interleaved, -1.0, 1.0))
            frame = _av_audio.AudioFrame.from_ndarray(
                interleaved.reshape(1, -1), format="flt", layout="stereo")
            frame.sample_rate = 48000
            frame.pts = pts
            for pkt in codec_ctx.encode(frame):
                ts_us = pts * 1_000_000 // 48000
                msg = struct.pack(">Q", ts_us) + bytes(pkt)
                with _audio_subs_lock:
                    subs = list(_audio_subs.values())
                for aq, loop in subs:
                    try:
                        loop.call_soon_threadsafe(_put_drop_oldest, aq, msg)
                    except Exception:
                        pass
            pts += 960
        except Exception as e:
            log.debug("audio encode frame: %s", e)


def _ensure_audio_threads():
    global _audio_threads_started
    if _audio_threads_started:
        return
    _audio_threads_started = True
    threading.Thread(target=_audio_capture_thread, daemon=True, name="audio-capture").start()
    threading.Thread(target=_audio_encoder_thread, daemon=True, name="audio-encoder").start()


async def audio_session(ws):
    global _audio_clients
    import uuid

    qid = uuid.uuid4().hex
    aq = asyncio.Queue(maxsize=25)
    loop = asyncio.get_event_loop()

    with _audio_subs_lock:
        _audio_subs[qid] = (aq, loop)
    _audio_clients += 1
    _ensure_audio_threads()
    log.info("Audio client connected (total %d)", _audio_clients)
    try:
        while True:
            try:
                msg = await asyncio.wait_for(aq.get(), timeout=10)
                await ws.send(msg)
            except asyncio.TimeoutError:
                await ws.ping()
    except Exception:
        pass
    finally:
        _audio_clients -= 1
        with _audio_subs_lock:
            _audio_subs.pop(qid, None)
        log.info("Audio client disconnected (remaining %d)", _audio_clients)
