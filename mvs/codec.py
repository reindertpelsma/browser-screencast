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
    _AV_OK = True
except ImportError:
    _av = None
    _AV_OK = False
    log.warning("PyAV not installed (pip install av) — JPEG-only mode")

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

_CODEC_PREFERENCE = [CODEC_AV1, CODEC_H265, CODEC_VP9, CODEC_H264]
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
        "hw": ["vp9_vaapi", "vp9_qsv"],
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
            listed = any(name in text for name in names) if text else False
            pyav_known = any(_pyav_codec_known(name) for name in names) if _AV_OK else False
            caps[codec][kind] = (listed or pyav_known) and (kind == "sw" or _has_hw_encoder_device(names))
    if not _AV_OK:
        for c in caps.values():
            c["hw"] = False
            c["sw"] = False
    caps["jpeg"] = {"sw": True, "hw": False}
    return caps


@lru_cache(maxsize=None)
def _pyav_codec_known(name: str) -> bool:
    if not _AV_OK:
        return False
    try:
        _av.CodecContext.create(name, "w")
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


def select_codec(server_caps, client_caps):
    codec_priority = ["av1", "h265", "vp9", "h264"]
    group_priority = [("hw", "hw"), ("hw", "sw"), ("sw", "hw"), ("sw", "sw")]
    for s_kind, c_kind in group_priority:
        for codec in codec_priority:
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
