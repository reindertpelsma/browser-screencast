import fractions
import logging
import os
import platform
import shutil
import struct
import subprocess
from functools import lru_cache
from io import BytesIO

log = logging.getLogger("browser_screencast")

# ---------------------------------------------------------------------------
# PyAV (optional — JPEG fallback if unavailable)
# ---------------------------------------------------------------------------
try:
    import av as _av
    import av.logging as _av_logging
    _AV_OK = True
except ImportError:
    _av = None
    _av_logging = None
    _AV_OK = False
    log.warning("PyAV not installed (pip install av) — JPEG-only mode")


class _NullCapture(list):
    """Stand-in for av.logging.Capture on builds that lack it."""
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def capture_libav_logs():
    """Context manager collecting the libav messages emitted inside it."""
    if not _AV_OK:
        return _NullCapture()
    try:
        return _av_logging.Capture(local=True)
    except Exception:
        return _NullCapture()


# PyAV silences FFmpeg's own diagnostics by default, which is why a hardware
# encoder that refuses to open left behind nothing but a generic
# `avcodec_open2("hevc_nvenc", {})` — a large performance cliff with no reason
# recorded anywhere. Turning ERROR-level libav messages on costs nothing in
# normal operation (libav is silent unless something breaks) and turns that into
# the real cause, e.g. "cuCtxCreate failed -> CUDA_ERROR_OUT_OF_MEMORY /
# No capable devices found".
#
# The messages are generated but NOT let out through PyAV's own `libav.*`
# loggers: a failed hardware probe emits a dozen raw context-free lines
# ("nv12", "p010le", "DLL libamfrt64.so.1 failed to open"). capture_libav_logs()
# collects them per open attempt so one aggregated, attributed warning can be
# emitted instead (see mvs/encoder.py).
if _AV_OK:
    try:
        _av_logging.set_level(_av_logging.ERROR)
        logging.getLogger("libav").setLevel(logging.CRITICAL)
    except Exception:
        pass

# JPEG fallback encoder
try:
    import turbojpeg as _tj_mod
    _TJ = None
    for _p in ["/opt/homebrew/lib/libturbojpeg.dylib", "/usr/local/lib/libturbojpeg.dylib", None]:
        try: _TJ = _tj_mod.TurboJPEG(_p); break
        except: pass
except ImportError:
    _TJ = None

def _encode_jpeg(rgb, quality):
    if _TJ:
        import turbojpeg
        return _TJ.encode(rgb[:,:,::-1].copy(), quality=quality,
                          pixel_format=turbojpeg.TJPF_BGR, jpeg_subsample=turbojpeg.TJSAMP_422)
    from PIL import Image
    buf = BytesIO()
    Image.fromarray(rgb).save(buf, format="JPEG", quality=quality, subsampling=1)
    return buf.getvalue()

# ---------------------------------------------------------------------------
# Frame wire format
# Header = 18 bytes:
#   seq(4)  capture_ms(8)  codec(1)  flags(1)  payload_len(4)
# codec: 0=jpeg  1=h264  2=h265  3=av1  4=vp9
# flags: bit0=keyframe
# ---------------------------------------------------------------------------
CODEC_JPEG, CODEC_H264, CODEC_H265, CODEC_AV1, CODEC_VP9 = 0, 1, 2, 3, 4

_CODEC_PREFERENCE = [CODEC_H265, CODEC_AV1, CODEC_VP9, CODEC_H264]
_CODEC_NAME = {
    CODEC_H264: "h264",
    CODEC_H265: "h265",
    CODEC_AV1: "av1",
    CODEC_VP9: "vp9",
    CODEC_JPEG: "jpeg",
}
_CLIENT_CODEC_MAP = {
    "av1": CODEC_AV1,
    "h265": CODEC_H265, "hevc": CODEC_H265,
    "vp9": CODEC_VP9,
    "h264": CODEC_H264, "avc": CODEC_H264,
}
_SERVER_ENCODERS = {
    "h264": {
        "hw": ["h264_nvenc", "h264_qsv", "h264_amf"],
        "sw": ["libx264", "h264"],
    },
    "h265": {
        "hw": ["hevc_nvenc", "hevc_qsv", "hevc_amf"],
        "sw": ["libx265", "hevc"],
    },
    "av1": {
        "hw": ["av1_nvenc", "av1_qsv", "av1_amf"],
        "sw": ["libsvtav1", "libaom-av1"],
    },
    "vp9": {
        # VP9 VAAPI/QSV needs a hardware-frame upload path in EncoderPipeline.
        # Do not advertise it until the encoder cascade can actually open it.
        "hw": [],
        "sw": ["libvpx-vp9"],
    },
}


@lru_cache(maxsize=1)
def ffmpeg_encoders_text() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return ""
    try:
        return subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                              capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""


def probe_server_codecs() -> dict:
    """Return {codec: {hw: bool, sw: bool}} from FFmpeg/PyAV availability.

    The actual EncoderPipeline still performs a real open attempt. This probe is
    intentionally advisory: it gives the negotiation code a capability matrix
    without pretending that every driver listed by FFmpeg is usable on the host.
    """
    text = ffmpeg_encoders_text()
    caps = {}
    for codec, groups in _SERVER_ENCODERS.items():
        caps[codec] = {}
        for kind, names in groups.items():
            if kind == "hw":
                # A hardware encoder counts as available only when THIS PyAV/FFmpeg
                # build can actually open it. Neither of the cheaper signals is sound:
                #   * `ffmpeg -encoders` describes the SYSTEM ffmpeg CLI, a different
                #     FFmpeg build from the one PyAV encodes with — and frequently not
                #     installed at all (measured: absent on the live deployment);
                #   * _has_hw_encoder_device() only proves a device node exists, not
                #     that a context can be created on it.
                # Accepting either is how a host whose NVENC cannot initialise still
                # advertised h265 hw=True. select_codec() then picked H.265 on the
                # strength of that claim, the encoder cascade fell through to libx265,
                # and the server ran SOFTWARE HEVC — exactly what auto mode's
                # "never sw H.265" rule exists to prevent.
                caps[codec][kind] = bool(
                    _AV_OK and _has_hw_encoder_device(names)
                    and any(_pyav_codec_known(name) for name in names))
            else:
                listed = any(name in text for name in names) if text else False
                caps[codec][kind] = bool(
                    listed or (_AV_OK and any(_pyav_codec_known(name) for name in names)))
    if not _AV_OK:
        for c in caps.values():
            c["hw"] = False
            c["sw"] = False
    caps["jpeg"] = {"sw": True, "hw": False}
    return caps


@lru_cache(maxsize=None)
def _pyav_codec_known(name: str) -> bool:
    """Return True only if the codec can actually be opened, not just created.

    `av.CodecContext.create(name, "w")` always succeeds for any codec name the
    FFmpeg build knows — it does not check whether the underlying hardware driver
    can actually initialise.  For NVENC in particular, av1_nvenc on Ampere
    (RTX 30xx) returns ENOSYS from avcodec_open2 because AV1 NVENC was only added
    in Ada Lovelace.  We must call open() to catch that.
    """
    if not _AV_OK:
        return False
    try:
        with capture_libav_logs():
            cc = _av.CodecContext.create(name, "w")
            cc.width = 640
            cc.height = 480
            cc.pix_fmt = "yuv420p"
            cc.time_base = fractions.Fraction(1, 1000)
            cc.framerate = fractions.Fraction(60, 1)
            cc.bit_rate = 500_000
            cc.gop_size = 300
            cc.open()
        # VideoCodecContext has no close() — let cc go out of scope.
        return True
    except Exception:
        return False


def _has_hw_encoder_device(names) -> bool:
    sysname = platform.system()
    if any("nvenc" in n for n in names):
        if shutil.which("nvidia-smi") or os.path.exists("/dev/nvidiactl"):
            return True
    if any("qsv" in n for n in names):
        if os.path.isdir("/dev/dri"):
            try:
                return any(p.startswith("renderD") for p in os.listdir("/dev/dri"))
            except Exception:
                return False
    if any("amf" in n for n in names):
        return sysname == "Windows"
    if any("vaapi" in n for n in names):
        return os.path.isdir("/dev/dri")
    return False


def normalize_client_caps(client_caps):
    """Accept both the v0 macscreencast list and the browser-screencast matrix."""
    if isinstance(client_caps, dict):
        out = {}
        for codec, value in client_caps.items():
            key = "h265" if codec == "hevc" else codec
            if isinstance(value, str):
                out[key] = {"hw": value == "hw", "sw": value == "sw"}
            elif isinstance(value, dict):
                out[key] = {"hw": bool(value.get("hw")), "sw": bool(value.get("sw"))}
        return out
    out = {}
    for c in client_caps or []:
        key = "h265" if c == "hevc" else c
        if key in _CLIENT_CODEC_MAP:
            # Old clients did not distinguish HW/SW decode. Treat the support
            # as software to keep negotiation conservative and compatible.
            out[key] = {"hw": False, "sw": True}
    return out


def select_codec(server_caps, client_caps, explicit=False):
    """Select best codec given server encoder and client decoder capabilities.

    Auto mode (explicit=False) — conservative, CPU-safe:
      HW tier (best quality):
        AV1  hw encode + hw client decode only. AV1 sw decode is 3-5× H.264 and
             lags on pre-2020 hardware that lacks a dedicated AV1 decode block.
        H.265 hw encode + any client decode. SW H.265 decode is ~1.5× H.264 and
             most machines ≥2016 have hardware HEVC decode anyway.
      Fallback tier (universal):
        H.264 hw encode then sw encode + any decode.
      Never in auto: sw H.265 encode (5-10× heavier than x264), sw AV1 encode,
                     VP9 sw encode (comparable to H.264 sw but no hw VP9 encoder).

    Explicit mode (explicit=True) — user picked a codec, honour it:
      Full priority including sw H.265 / VP9 sw / H.264 sw.
      AV1 sw encode excluded regardless: libsvtav1 bitstreams are silently rejected
      by Chrome's WebCodecs AV1 decoder; sessions fall to JPEG with no clear error.
    """
    if explicit:
        candidates = [
            ("av1",  "hw", "hw"), ("av1",  "hw", "sw"),
            ("h265", "hw", "hw"), ("h265", "hw", "sw"),
            ("h265", "sw", "hw"), ("h265", "sw", "sw"),
            # AV1 sw encode excluded (Chrome WebCodecs AV1 bitstream rejection)
            ("vp9",  "sw", "hw"), ("vp9",  "sw", "sw"),
            ("h264", "hw", "hw"), ("h264", "hw", "sw"),
            ("h264", "sw", "hw"), ("h264", "sw", "sw"),
        ]
    else:
        candidates = [
            # H.265 hw first: proven compat with Chrome WebCodecs across hardware.
            # AV1 hw is gated on client hw decode AND moved below H.265 — NVENC AV1
            # bitstreams work in principle but large I-frames cause write-buffer stalls
            # on constrained paths; H.265 hw is the safer universal default.
            ("h265", "hw", "hw"), ("h265", "hw", "sw"),
            ("av1",  "hw", "hw"),
            ("h264", "hw", "hw"), ("h264", "hw", "sw"),
            ("h264", "sw", "hw"), ("h264", "sw", "sw"),
        ]

    for codec, s_kind, c_kind in candidates:
        if (server_caps.get(codec, {}).get(s_kind)
                and client_caps.get(codec, {}).get(c_kind)):
            return _CLIENT_CODEC_MAP[codec], s_kind, c_kind

    if server_caps.get("jpeg", {}).get("sw") and client_caps.get("jpeg", {}).get("sw", True):
        return CODEC_JPEG, "sw", "sw"
    return CODEC_JPEG, "sw", "sw"

def _select_codec(client_codecs):
    """Backward-compatible list negotiation."""
    client_set = {_CLIENT_CODEC_MAP[c] for c in client_codecs if c in _CLIENT_CODEC_MAP}
    for c in _CODEC_PREFERENCE:
        if c in client_set:
            return c
    return CODEC_H264

def _hdr(seq, capture_ms, codec, keyframe, plen):
    return struct.pack(">IQBBI", seq, capture_ms, codec, 1 if keyframe else 0, plen)
