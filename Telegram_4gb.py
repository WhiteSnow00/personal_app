from __future__ import annotations

import atexit
import json
import logging
import math
import os
import hashlib
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

__version__ = "1.2.0"

THRESHOLD_BYTES = 4 * 1024 ** 3
TARGET_BYTES = 3800 * 1024 ** 2
SAMPLE_CLIP_COUNT = 5
SAMPLE_CLIP_DURATION = 10
PROBE_CRF_POINTS = [22, 28, 34, 40]
CRF_CLAMP_MIN = 15
CRF_CLAMP_MAX = 51
SAFETY_MARGIN = 0.97
CONTAINER_OVERHEAD_FACTOR = 0.98
MAX_RETRIES = 2
AUDIO_BITRATE_KBPS = 128
PRESET = "fast"
ENCODE_CODEC = "libx264"

ENCODE_TIMEOUT = 86400
FAIL_MARKER_SUFFIX = ".encode_fail"
STATE_DIR_NAME = ".encode_state"
MAX_FILENAME_BYTES = 240
MAX_OUTPUT_NAME_ATTEMPTS = 1000
SCALE_720P = (
    "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease,"
    "scale=trunc(iw/2)*2:trunc(ih/2)*2"
)

CODEC_EFFICIENCY = {
    "h264": 1.0,
    "avc": 1.0,
    "hevc": 1.8,
    "h265": 1.8,
    "av1": 2.5,
    "vp9": 2.2,
    "vp8": 1.3,
    "mpeg4": 0.7,
    "mpeg2video": 0.5,
}

SUPPORTED_EXTS = {
    ".3g2", ".3gp", ".asf", ".avi", ".divx", ".dv", ".f4v", ".flv",
    ".m2t", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg",
    ".mts", ".mxf", ".ogm", ".ogv", ".rm", ".rmvb", ".ts", ".vob",
    ".webm", ".wmv",
}
FINAL_OUTPUT_SUFFIX = "_encoded"
H264_VIDEO_CODECS = {"h264", "avc", "avc1", "avc3"}
MP4_AUDIO_COPY_CODECS = {"aac", "alac", "mp3", "ac3", "eac3", "opus"}
MP4_SUBTITLE_COPY_CODECS = {"mov_text", "tx3g", "webvtt"}
TEXT_SUBTITLE_CODECS = {
    "subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "tx3g",
    "text", "microdvd", "mpl2", "sami", "realtext", "subviewer",
}
IMAGE_SUBTITLE_CODECS = {
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvdsub", "pgssub", "xsub", "dvb_subtitle",
}

_CURRENT_TEMPS: List[Path] = []
_CURRENT_TEMP_OUTPUT: Optional[Path] = None
_CURRENT_LOCK: Optional[Path] = None
_INTERRUPTED = False


def _sigint_handler(signum: Any, frame: Any) -> None:
    global _INTERRUPTED
    _INTERRUPTED = True
    print("\n[INTERRUPT] Shutdown requested — finishing current operation...", flush=True)


signal.signal(signal.SIGINT, _sigint_handler)


def setup_logging(work_dir: Path) -> None:
    log_path = work_dir / "encode_log.txt"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def run_cmd(cmd: List[str], timeout: int = 300, **kwargs: Any) -> subprocess.CompletedProcess:
    logging.debug("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Required executable not found: {cmd[0]}. "
            f"Please ensure {cmd[0]} is installed and on your PATH."
        ) from exc
    if result.returncode != 0:
        err = result.stderr.strip()
        if len(err) > 2000:
            err = err[:2000] + "\n... [truncated]"
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=err)
    return result


def get_file_size(path: Path) -> int:
    return path.stat().st_size


def format_size(num_bytes: int) -> str:
    num = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0:
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} PiB"


def format_time(seconds: float) -> str:
    if seconds < 0:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def short_token(value: Any, length: int = 12) -> str:
    data = str(value).encode("utf-8", "surrogatepass")
    return hashlib.sha1(data).hexdigest()[:length]


def path_token(path: Path) -> str:
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    return short_token(key)


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def truncate_utf8(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    if utf8_len(text) <= max_bytes:
        return text

    result: List[str] = []
    used = 0
    for char in text:
        char_len = utf8_len(char)
        if used + char_len > max_bytes:
            break
        result.append(char)
        used += char_len
    return "".join(result)


def safe_filename(stem: str, suffix: str, max_bytes: int = MAX_FILENAME_BYTES) -> str:
    raw = f"{stem}{suffix}"
    if utf8_len(raw) <= max_bytes:
        return raw

    digest = short_token(raw, 10)
    suffix_with_digest = f"_{digest}{suffix}"
    stem_budget = max_bytes - utf8_len(suffix_with_digest)
    short_stem = truncate_utf8(stem, stem_budget).rstrip(" ._-")
    if not short_stem:
        short_stem = "video"
    return f"{short_stem}{suffix_with_digest}"


def safe_path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def state_dir_for(video_path: Path) -> Path:
    return video_path.parent / STATE_DIR_NAME


def state_path_for(video_path: Path, suffix: str) -> Path:
    return state_dir_for(video_path) / f"{path_token(video_path)}{suffix}"


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def process_identity(pid: int) -> Optional[str]:
    """Stable identity for a live process; changes if the PID is reused."""
    if not is_pid_alive(pid):
        return None

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            process_query_limited = 0x1000
            handle = kernel32.OpenProcess(process_query_limited, False, pid)
            if handle:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()
                ok = kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                )
                kernel32.CloseHandle(handle)
                if ok:
                    return f"{pid}:{creation.dwHighDateTime:08x}{creation.dwLowDateTime:08x}"
        except Exception:
            pass
        return f"{pid}"

    try:
        return f"{pid}:{os.stat(f'/proc/{pid}').st_ctime}"
    except OSError:
        return f"{pid}"


def cleanup_paths(paths: Sequence[Path]) -> None:
    for p in paths:
        try:
            if safe_path_exists(p):
                p.unlink()
                logging.debug("Cleaned up: %s", p)
        except OSError as exc:
            logging.warning("Failed to remove %s: %s", p, exc)


def fail_marker_path(video_path: Path) -> Path:
    return state_path_for(video_path, FAIL_MARKER_SUFFIX)


def legacy_fail_marker_path(video_path: Path) -> Path:
    return video_path.with_suffix(video_path.suffix + FAIL_MARKER_SUFFIX)


def has_fail_marker(video_path: Path) -> bool:
    return safe_path_exists(fail_marker_path(video_path)) or safe_path_exists(legacy_fail_marker_path(video_path))


def set_fail_marker(video_path: Path, reason: str = "") -> None:
    try:
        marker = fail_marker_path(video_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        body = f"{int(time.time())}\n{video_path.name}\n"
        if reason:
            body += f"{reason}\n"
        marker.write_text(body, encoding="utf-8")
    except OSError as exc:
        logging.warning("[MARKER] Failed to write fail marker for %s: %s", video_path.name, exc)


def clear_fail_marker(video_path: Path) -> None:
    for p in (fail_marker_path(video_path), legacy_fail_marker_path(video_path)):
        try:
            if safe_path_exists(p):
                p.unlink()
        except OSError:
            pass


def lock_path_for(video_path: Path) -> Path:
    return state_path_for(video_path, ".lock")


def _parse_lock_payload(text: str) -> Tuple[Optional[int], Optional[str]]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None, None
    try:
        pid = int(lines[0])
    except ValueError:
        return None, None
    identity = lines[1] if len(lines) > 1 else None
    return pid, identity


def is_lock_holder_alive(lock: Path) -> bool:
    try:
        pid, identity = _parse_lock_payload(lock.read_text(encoding="utf-8"))
    except OSError:
        return False
    if pid is None:
        return False
    live_id = process_identity(pid)
    if live_id is None:
        return False
    if identity is not None:
        return live_id == identity
    return True


def is_locked(video_path: Path) -> bool:
    lock = lock_path_for(video_path)
    if not safe_path_exists(lock):
        return False
    if is_lock_holder_alive(lock):
        return True
    try:
        lock.unlink()
    except OSError:
        pass
    return False


def acquire_lock(video_path: Path) -> Optional[Path]:
    lock = lock_path_for(video_path)
    pid = os.getpid()
    identity = process_identity(pid) or str(pid)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logging.warning("[LOCK] Failed to create state dir for %s: %s", video_path.name, exc)
        return None
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"{pid}\n{identity}\n")
            return lock
        except FileExistsError:
            if not is_lock_holder_alive(lock):
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            return None
        except OSError:
            return None
    return None


def release_lock(lock: Path) -> None:
    try:
        if safe_path_exists(lock):
            lock.unlink()
    except OSError as exc:
        logging.warning("Failed to release lock %s: %s", lock, exc)


def ffprobe_json(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = run_cmd(cmd, timeout=60)
    return json.loads(result.stdout)


def get_duration(info: dict) -> float:
    fmt = info.get("format", {})
    dur = fmt.get("duration")
    if dur is not None:
        return float(dur)
    total = 0.0
    for stream in info.get("streams", []):
        sd = stream.get("duration")
        if sd is not None:
            total = max(total, float(sd))
    return total


def get_video_stream(info: dict) -> Optional[dict]:
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return None


def get_audio_streams(info: dict) -> List[dict]:
    return [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]


def get_audio_stream(info: dict) -> Optional[dict]:
    streams = get_audio_streams(info)
    return streams[0] if streams else None


def has_subtitle_streams(info: dict) -> bool:
    return any(s.get("codec_type") == "subtitle" for s in info.get("streams", []))


def get_stream_codecs(info: dict, codec_type: str) -> List[str]:
    codecs: List[str] = []
    for stream in info.get("streams", []):
        if stream.get("codec_type") == codec_type:
            codecs.append(str(stream.get("codec_name", "unknown")).lower())
    return codecs


def get_audio_codecs(info: dict) -> List[str]:
    return get_stream_codecs(info, "audio")


def get_subtitle_codecs(info: dict) -> List[str]:
    return get_stream_codecs(info, "subtitle")


def is_h264_video_codec(codec_name: str) -> bool:
    return codec_name.lower() in H264_VIDEO_CODECS


def can_copy_audio_to_mp4(info: dict) -> bool:
    audio_codecs = get_audio_codecs(info)
    return not audio_codecs or all(codec in MP4_AUDIO_COPY_CODECS for codec in audio_codecs)


def can_copy_subtitles_to_mp4(info: dict) -> bool:
    subtitle_codecs = get_subtitle_codecs(info)
    return not subtitle_codecs or all(codec in MP4_SUBTITLE_COPY_CODECS for codec in subtitle_codecs)


def subtitle_plan(info: dict) -> str:
    codecs = get_subtitle_codecs(info)
    if not codecs:
        return "none"
    if any(c in IMAGE_SUBTITLE_CODECS for c in codecs):
        return "drop"
    if all(c in MP4_SUBTITLE_COPY_CODECS for c in codecs):
        return "copy"
    if all(c in TEXT_SUBTITLE_CODECS or c in MP4_SUBTITLE_COPY_CODECS for c in codecs):
        return "mov_text"
    return "drop"


def stream_bitrate_kbps(stream: dict, default: int = AUDIO_BITRATE_KBPS) -> int:
    br = stream.get("bit_rate")
    if br is not None:
        try:
            return max(1, int(float(br) / 1000))
        except (TypeError, ValueError):
            pass
    tags = stream.get("tags") or {}
    bps = tags.get("BPS") or tags.get("bps")
    if bps is not None:
        try:
            return max(1, int(float(bps) / 1000))
        except (TypeError, ValueError):
            pass
    return default


def planned_audio_kbps(info: dict) -> int:
    streams = get_audio_streams(info)
    if not streams:
        return 0
    if can_copy_audio_to_mp4(info):
        return sum(stream_bitrate_kbps(s) for s in streams)
    return AUDIO_BITRATE_KBPS * len(streams)


def is_generated_output(path: Path) -> bool:
    if path.suffix.lower() != ".mp4":
        return False
    stem = path.stem.lower()
    suffix = FINAL_OUTPUT_SUFFIX.lower()
    if stem.endswith(suffix):
        return True
    marker = f"{suffix}_"
    if marker in stem:
        tail = stem.rsplit(marker, 1)[1]
        return tail.isdigit()
    return False


def is_temporary_video(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".tmp.mp4") or ".tmp." in name or name.startswith(".tg4gb_")


def is_delete_failed_source(path: Path) -> bool:
    return "_delete_failed" in path.stem.lower()


def final_output_path_for(source: Path) -> Path:
    """Place output beside the source so nested library layouts are preserved."""
    out_dir = source.parent
    desired = out_dir / safe_filename(source.stem, f"{FINAL_OUTPUT_SUFFIX}.mp4")
    try:
        if not desired.exists() or desired.resolve() == source.resolve():
            if desired.stem != f"{source.stem}{FINAL_OUTPUT_SUFFIX}":
                logging.warning(
                    "[OUTPUT] Source name too long; using shortened output name: %s",
                    desired.name,
                )
            return desired
    except OSError:
        if not desired.exists():
            return desired

    for counter in range(2, MAX_OUTPUT_NAME_ATTEMPTS + 2):
        candidate = out_dir / safe_filename(source.stem, f"{FINAL_OUTPUT_SUFFIX}_{counter}.mp4")
        if not candidate.exists():
            logging.warning("[OUTPUT] %s exists; using %s", desired.name, candidate.name)
            return candidate

    raise RuntimeError(f"Could not allocate unique output name near {source.name}")


def temp_output_path_for(output_path: Path, label: str) -> Path:
    return output_path.with_name(
        f".tg4gb_{label}_{os.getpid()}_{path_token(output_path)}.tmp.mp4"
    )


def verify_mp4_output(path: Path) -> int:
    if path.suffix.lower() != ".mp4":
        raise ValueError(f"Output is not MP4: {path.name}")
    if not path.exists() or get_file_size(path) <= 0:
        raise ValueError(f"Output missing or empty: {path.name}")
    info = ffprobe_json(path)
    if get_video_stream(info) is None:
        raise ValueError(f"Output has no video stream: {path.name}")
    return get_file_size(path)


def get_audio_bitrate(info: dict) -> int:
    """First-stream audio bitrate (legacy helper; prefer planned_audio_kbps)."""
    audio = get_audio_stream(info)
    if audio is None:
        return 0
    return stream_bitrate_kbps(audio)


def get_video_bitrate(info: dict) -> int:
    video = get_video_stream(info)
    if video is not None:
        br = video.get("bit_rate")
        if br is not None:
            return int(float(br) / 1000)
    fmt = info.get("format", {})
    file_size_str = fmt.get("size")
    duration = get_duration(info)
    if file_size_str and duration > 0:
        file_size = int(float(file_size_str))
        audio_kbps = planned_audio_kbps(info) if get_audio_streams(info) else 0
        if not can_copy_audio_to_mp4(info):
            audio_kbps = sum(stream_bitrate_kbps(s) for s in get_audio_streams(info)) or AUDIO_BITRATE_KBPS
        total_kbps = int(file_size * 8 / duration / 1000)
        return max(100, total_kbps - audio_kbps)
    return 0


def get_resolution(info: dict) -> Tuple[int, int]:
    video = get_video_stream(info)
    if video is None:
        return (0, 0)
    return (int(video.get("width") or 0), int(video.get("height") or 0))


def get_fps(info: dict) -> float:
    video = get_video_stream(info)
    if video is None:
        return 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = video.get(key, "")
        if "/" in str(rate):
            try:
                num, den = str(rate).split("/")
                den_f = float(den)
                if den_f == 0:
                    continue
                return float(num) / den_f
            except (ValueError, ZeroDivisionError):
                pass
    return 0.0


def get_codec_name(info: dict) -> str:
    video = get_video_stream(info)
    if video is None:
        return "unknown"
    return str(video.get("codec_name", "unknown"))


def get_codec_efficiency(info: dict) -> float:
    codec = get_codec_name(info).lower()
    return CODEC_EFFICIENCY.get(codec, 1.0)


def analyze_source_quality(info: dict) -> Dict[str, Any]:
    width, height = get_resolution(info)
    fps = get_fps(info)
    video_kbps = get_video_bitrate(info)
    codec_name = get_codec_name(info)
    efficiency = get_codec_efficiency(info)

    result: Dict[str, Any] = {
        "bpp": 0.0,
        "effective_bpp": 0.0,
        "category": "unknown",
        "video_kbps": video_kbps,
        "resolution": (width, height),
        "fps": fps,
        "codec": codec_name,
        "codec_efficiency": efficiency,
        "message": "",
        "should_warn": False,
    }

    if width <= 0 or height <= 0 or fps <= 0 or video_kbps <= 0:
        result["message"] = "Cannot analyze source quality: missing metadata"
        return result
        
    bpp = (video_kbps * 1000.0) / (width * height * fps)
    effective_bpp = bpp * efficiency
    result["bpp"] = bpp
    result["effective_bpp"] = effective_bpp

    if effective_bpp > 0.3:
        result["category"] = "high"
        result["message"] = (
            f"High quality source (BPP={bpp:.4f}, effective={effective_bpp:.4f}, codec={codec_name})"
        )
    elif effective_bpp >= 0.15:
        result["category"] = "medium"
        result["message"] = (
            f"Medium quality source (BPP={bpp:.4f}, effective={effective_bpp:.4f}, codec={codec_name})"
        )
    elif effective_bpp >= 0.08:
        result["category"] = "compressed"
        result["message"] = (
            f"Already compressed source (BPP={bpp:.4f}, effective={effective_bpp:.4f}, "
            f"codec={codec_name}). Quality may be poor."
        )
        result["should_warn"] = True
    else:
        result["category"] = "heavily_compressed"
        result["message"] = (
            f"Heavily compressed source (BPP={bpp:.4f}, effective={effective_bpp:.4f}, "
            f"codec={codec_name}). Consider stream-copy if size is acceptable."
        )
        result["should_warn"] = True

    logging.info("[QUALITY] %s", result["message"])
    return result


def calculate_target_bitrate(
    duration_sec: float,
    audio_kbps: int,
    target_size_bytes: int = TARGET_BYTES,
    overhead_factor: float = CONTAINER_OVERHEAD_FACTOR,
    safety_margin: float = SAFETY_MARGIN,
) -> int:
    if duration_sec <= 0:
        raise ValueError(f"Invalid duration: {duration_sec}")

    target_size_mb = target_size_bytes / (1024 * 1024)
    total_kbps = (target_size_mb * 8000 * overhead_factor / duration_sec) * safety_margin
    video_kbps = max(total_kbps - max(audio_kbps, 0), 100)

    logging.info(
        "[BITRATE] target=%s | duration=%.1fs | audio=%dkbps | video=%dkbps",
        format_size(target_size_bytes), duration_sec, audio_kbps, int(video_kbps),
    )
    return int(video_kbps)


def _sample_start_times(duration: float) -> List[float]:
    """Build unique clip start times; collapse for short sources."""
    if duration <= 0:
        return [0.0]
    if duration <= SAMPLE_CLIP_DURATION:
        return [0.0]
    if duration <= SAMPLE_CLIP_DURATION * 2:
        return [0.0, max(0.0, duration - SAMPLE_CLIP_DURATION)]

    percentages = [0.10, 0.30, 0.50, 0.70, 0.90][:SAMPLE_CLIP_COUNT]
    starts: List[float] = []
    for pct in percentages:
        start = max(0.0, duration * pct)
        if start + SAMPLE_CLIP_DURATION > duration:
            start = max(0.0, duration - SAMPLE_CLIP_DURATION)
        if not starts or abs(starts[-1] - start) > 0.5:
            starts.append(start)
    return starts or [0.0]


def extract_sample(source: Path, output: Path, duration: float) -> None:
    starts = _sample_start_times(duration)
    logging.info("[SAMPLE] Extracting %d clip(s) from %s", len(starts), source.name)

    temp_dir = Path(tempfile.gettempdir())
    local_temps: List[Path] = []
    clips: List[Path] = []
    token = path_token(source)

    try:
        for idx, start in enumerate(starts, start=1):
            clip_path = temp_dir / f"tg4gb_{token}_clip{idx}_{os.getpid()}.mkv"
            clips.append(clip_path)
            local_temps.append(clip_path)
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{start:.3f}",
                "-i", str(source),
                "-t", str(min(SAMPLE_CLIP_DURATION, max(duration, 0.1))),
                "-map", "0:v:0",
                "-c:v", ENCODE_CODEC, "-preset", "ultrafast", "-crf", "23",
                "-an", "-sn", "-dn",
                "-avoid_negative_ts", "make_zero",
                str(clip_path),
            ]
            logging.info("[SAMPLE] Clip %d/%d @ %.1fs (ultrafast)", idx, len(starts), start)
            run_cmd(cmd, timeout=120)

        if len(clips) == 1:
            clips[0].replace(output)
            return

        list_path = temp_dir / f"tg4gb_{token}_concat_{os.getpid()}.txt"
        local_temps.append(list_path)

        list_lines = []
        for c in clips:
            escaped = str(c.resolve()).replace("'", r"'\''")
            list_lines.append(f"file '{escaped}'")
        list_path.write_text("\n".join(list_lines) + "\n", encoding="utf-8")

        run_cmd([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            str(output),
        ], timeout=120)

    finally:
        cleanup_paths(local_temps)


def probe_sample(sample_path: Path, crf: int, temp_dir: Path) -> int:
    """Unconstrained CRF probe (no maxrate) so size-vs-CRF curve is meaningful."""
    probe_out = temp_dir / f"{sample_path.stem}_probe_crf{crf}_{os.getpid()}.mp4"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(sample_path),
        "-c:v", ENCODE_CODEC, "-preset", PRESET, "-crf", str(crf),
        "-an", "-sn", "-dn",
        str(probe_out),
    ]
    logging.info("[PROBE] CRF %d (unconstrained)", crf)
    try:
        run_cmd(cmd, timeout=300)
        size = get_file_size(probe_out)
    except subprocess.CalledProcessError:
        logging.warning("[PROBE] CRF %d probe failed", crf)
        size = -1
    finally:
        cleanup_paths([probe_out])
    return size


def estimate_crf_for_target(
    probe_results: List[Tuple[int, int]],
    target_bytes: int,
    r_time: float,
    audio_bytes: int = 0,
) -> Optional[float]:
    valid = [(crf, sz) for crf, sz in probe_results if sz > 0]
    if len(valid) < 2:
        return None

    valid.sort(key=lambda x: x[0])
    video_target = max(target_bytes - audio_bytes, 1)
    extrapolated = [(crf, sz * r_time) for crf, sz in valid]
    c1 = s1 = c2 = s2 = None  
    for i in range(len(extrapolated) - 1):
        ca, sa = extrapolated[i]
        cb, sb = extrapolated[i + 1]
        if min(sa, sb) <= video_target <= max(sa, sb):
            c1, s1, c2, s2 = ca, sa, cb, sb
            break

    if c1 is None:
        sizes = [s for _, s in extrapolated]
        if video_target > max(sizes):
            c1, s1 = extrapolated[0]
            c2, s2 = extrapolated[1]
        elif video_target < min(sizes):
            c1, s1 = extrapolated[-2]
            c2, s2 = extrapolated[-1]
        else:
            return None

    if s1 <= 0 or s2 <= 0 or s1 == s2 or c1 == c2:
        return None

    ln_s1 = math.log(s1)
    ln_s2 = math.log(s2)
    ln_target = math.log(video_target)
    k = (ln_s1 - ln_s2) / (c1 - c2)
    if k == 0 or math.isnan(k) or math.isinf(k):
        return None

    return c1 + (ln_target - ln_s1) / k


def run_crf_feasibility_probe(
    sample_path: Path,
    temp_dir: Path,
    r_time: float,
    target_bytes: int,
    audio_kbps: int,
    duration_sec: float,
) -> Dict[str, Any]:
    logging.info("[PROBE] Running CRF feasibility probe (unconstrained)...")
    probe_results: List[Tuple[int, int]] = []
    audio_bytes = int(max(audio_kbps, 0) * 1000 / 8 * max(duration_sec, 0.0))

    for crf in PROBE_CRF_POINTS:
        if _INTERRUPTED:
            raise KeyboardInterrupt
        size = probe_sample(sample_path, crf, temp_dir)
        if size > 0:
            probe_results.append((crf, size))
            full_est = int(size * r_time) + audio_bytes
            logging.info(
                "[PROBE] CRF %d -> sample_v=%s | full_est=%s",
                crf, format_size(size), format_size(full_est),
            )

    result: Dict[str, Any] = {
        "probe_results": probe_results,
        "estimated_crf": None,
        "is_feasible": False,
        "quality_warning": None,
        "can_reach_target": False,
        "suggest_scale": False,
        "bitrate_scale": 1.0,
        "audio_bytes": audio_bytes,
    }

    if len(probe_results) < 2:
        result["quality_warning"] = "Insufficient probe results -- proceeding with caution"
        result["can_reach_target"] = True  
        return result

    est_crf = estimate_crf_for_target(probe_results, target_bytes, r_time, audio_bytes)
    result["estimated_crf"] = est_crf
    max_crf_point = max(probe_results, key=lambda x: x[0])
    min_full_est = int(max_crf_point[1] * r_time) + audio_bytes
    min_crf_point = min(probe_results, key=lambda x: x[0])
    max_full_est = int(min_crf_point[1] * r_time) + audio_bytes

    if est_crf is not None:
        logging.info("[PROBE] Estimated CRF for target: %.2f", est_crf)
        if est_crf > CRF_CLAMP_MAX or min_full_est > target_bytes:
            result["quality_warning"] = (
                f"Target difficult: est_CRF={est_crf:.1f}, min_full_est={format_size(min_full_est)}"
            )
            result["suggest_scale"] = True
            result["can_reach_target"] = min_full_est <= THRESHOLD_BYTES
            result["is_feasible"] = False
        elif est_crf > 45:
            result["can_reach_target"] = True
            result["is_feasible"] = True
            result["quality_warning"] = f"Estimated CRF {est_crf:.1f} -- very low quality expected"
        elif est_crf > 40:
            result["can_reach_target"] = True
            result["is_feasible"] = True
            result["quality_warning"] = f"Estimated CRF {est_crf:.1f} -- low quality, but achievable"
        else:
            result["can_reach_target"] = True
            result["is_feasible"] = True
    else:
        if min_full_est <= target_bytes:
            result["can_reach_target"] = True
            result["is_feasible"] = True
        else:
            result["quality_warning"] = (
                f"Target may not be achievable (min est {format_size(min_full_est)})"
            )
            result["suggest_scale"] = True
            result["can_reach_target"] = min_full_est <= THRESHOLD_BYTES
    if max_full_est > 0:
        headroom = target_bytes / max_full_est
        if headroom < 0.85:
            result["bitrate_scale"] = max(0.55, min(1.0, headroom * 0.95))
        elif headroom > 1.4 and (est_crf is None or est_crf < 28):
            result["bitrate_scale"] = min(1.15, headroom * 0.5 + 0.5)

    return result


def build_ffmpeg_video_args(video_kbps: int, scale: Optional[str] = None) -> List[str]:
    args = [
        "-c:v", ENCODE_CODEC,
        "-preset", PRESET,
        "-b:v", f"{video_kbps}k",
        "-maxrate", f"{int(video_kbps * 1.5)}k",
        "-bufsize", f"{int(video_kbps * 2)}k",
    ]
    if scale:
        args += ["-vf", scale]
    return args


def build_audio_args(audio_codecs: List[str]) -> List[str]:
    if not audio_codecs or all(codec in MP4_AUDIO_COPY_CODECS for codec in audio_codecs):
        return ["-c:a", "copy"]
    logging.info(
        "[ENCODE] Re-encoding audio %s -> AAC %dkbps/track",
        ",".join(audio_codecs), AUDIO_BITRATE_KBPS,
    )
    return ["-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_KBPS}k"]


def build_subtitle_args(plan: str) -> Tuple[List[str], List[str]]:
    """
    Returns (input_map_extra, codec_args).
    Video/audio maps are added by the caller.
    """
    if plan == "copy":
        return ["-map", "0:s?"], ["-c:s", "copy"]
    if plan == "mov_text":
        return ["-map", "0:s?"], ["-c:s", "mov_text"]
    return [], ["-sn"]


def two_pass_encode_with_progress(
    source: Path,
    output: Path,
    video_kbps: int,
    total_duration_sec: float,
    audio_codecs: List[str],
    subtitle_mode: str,
    stderr_path: Path,
    passlogfile: Path,
    scale: Optional[str] = None,
) -> None:
    video_args = build_ffmpeg_video_args(video_kbps, scale)
    passlog_prefix = str(passlogfile)
    audio_args = build_audio_args(audio_codecs)
    sub_maps, sub_args = build_subtitle_args(subtitle_mode)

    input_maps = ["-map", "0:v:0", "-map", "0:a?"] + sub_maps

    logging.info("[ENCODE] Pass 1/2 -- analysis (subs=%s)", subtitle_mode)
    pass1_cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source),
        "-map", "0:v:0",
    ] + video_args + [
        "-pass", "1",
        "-passlogfile", passlog_prefix,
        "-an",
        "-sn", "-dn",
        "-f", "null",
        os.devnull if os.name != "nt" else "NUL",
    ]

    try:
        run_cmd(pass1_cmd, timeout=ENCODE_TIMEOUT)
    except subprocess.CalledProcessError:
        cleanup_paths([Path(f"{passlog_prefix}-0.log"), Path(f"{passlog_prefix}-0.log.mbtree")])
        raise

    logging.info("[ENCODE] Pass 2/2 -- encoding with progress")
    pass2_cmd = [
        "ffmpeg", "-y", "-hide_banner",
        "-i", str(source),
    ] + input_maps + video_args + [
        "-pass", "2",
        "-passlogfile", passlog_prefix,
    ] + audio_args + sub_args + [
        "-map_metadata", "0",
        "-dn",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        str(output),
    ]

    stderr_fh = open(stderr_path, "w", encoding="utf-8")
    process: Optional[subprocess.Popen] = None
    try:
        process = subprocess.Popen(
            pass2_cmd,
            stdout=subprocess.PIPE,
            stderr=stderr_fh,
            text=True,
            bufsize=1,
        )

        out_time_sec = 0.0
        fps = 0.0
        speed = 0.0
        last_print = 0.0
        start_time = time.time()

        try:
            assert process.stdout is not None
            while True:
                if _INTERRUPTED:
                    _terminate_process(process)
                    break

                line = process.stdout.readline()
                if not line:
                    break

                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        out_time_sec = int(line.split("=", 1)[1]) / 1_000_000.0
                    except ValueError:
                        pass
                elif line.startswith("out_time_ms="):
                    try:
                        out_time_sec = int(line.split("=", 1)[1]) / 1_000_000.0
                    except ValueError:
                        pass
                elif line.startswith("out_time=") and not line.startswith("out_time_"):
                    # HH:MM:SS.micro
                    try:
                        t = line.split("=", 1)[1].strip()
                        parts = t.split(":")
                        if len(parts) == 3:
                            out_time_sec = (
                                int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                            )
                    except ValueError:
                        pass
                elif line.startswith("fps="):
                    try:
                        fps = float(line.split("=", 1)[1])
                    except ValueError:
                        pass
                elif line.startswith("speed="):
                    try:
                        speed = float(line.split("=", 1)[1].rstrip("x"))
                    except ValueError:
                        pass
                elif line == "progress=end":
                    break

                now = time.time()
                if now - last_print >= 1.0 and total_duration_sec > 0:
                    pct = min(100.0, out_time_sec / total_duration_sec * 100.0)
                    elapsed = now - start_time
                    eta = (elapsed / pct * 100.0) - elapsed if pct > 0 else 0.0
                    print(
                        f"\r[ENCODE] {pct:5.1f}% | "
                        f"Elapsed {format_time(elapsed)} | ETA {format_time(eta)} | "
                        f"fps {fps:.1f} | {speed:.2f}x      ",
                        end="", flush=True,
                    )
                    last_print = now

        finally:
            if process.poll() is None:
                if _INTERRUPTED:
                    _terminate_process(process)
                else:
                    try:
                        process.wait(timeout=ENCODE_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            print()

    finally:
        stderr_fh.close()
        cleanup_paths([
            Path(f"{passlog_prefix}-0.log"),
            Path(f"{passlog_prefix}-0.log.mbtree"),
        ])

    if _INTERRUPTED:
        raise KeyboardInterrupt

    assert process is not None
    if process.returncode != 0:
        stderr_text = "(stderr empty or unreadable)"
        try:
            text = stderr_path.read_text(encoding="utf-8").strip()
            if text:
                stderr_text = text
        except OSError:
            pass
        raise subprocess.CalledProcessError(
            process.returncode, pass2_cmd, output="", stderr=stderr_text
        )


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            process.send_signal(signal.SIGINT)
    except OSError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait()
        except OSError:
            pass


def calculate_retry_bitrate(
    old_video_kbps: int,
    actual_size: int,
    target_size: int = TARGET_BYTES,
) -> int:
    if actual_size <= 0:
        return max(int(old_video_kbps * 0.9), 100)

    ratio = target_size / actual_size
    new_kbps = int(old_video_kbps * ratio * 0.95)
    new_kbps = max(new_kbps, 100)
    logging.info(
        "[RETRY] Bitrate correction: %dkbps -> %dkbps (ratio=%.3f)",
        old_video_kbps, new_kbps, ratio,
    )
    return new_kbps


def delete_source_file(source: Path) -> Tuple[bool, Optional[Path]]:
    try:
        source.unlink()
        logging.info("[CLEANUP] Deleted source: %s", source.name)
        return True, None
    except OSError as exc:
        failed_name = source.with_name(
            safe_filename(source.stem, f"_DELETE_FAILED{source.suffix}")
        )
        logging.critical("[CLEANUP] Cannot delete %s: %s", source.name, exc)
        remaining = source
        try:
            if failed_name.exists():
                failed_name = source.with_name(
                    safe_filename(source.stem, f"_DELETE_FAILED_{int(time.time())}{source.suffix}")
                )
            source.rename(failed_name)
            remaining = failed_name
            logging.critical("[CLEANUP] Renamed undeletable source to %s", failed_name.name)
        except OSError as exc2:
            logging.critical("[CLEANUP] Rename failed: %s", exc2)
        set_fail_marker(remaining, reason="DELETE_FAILED")
        return False, remaining


def fast_remux_to_mp4(source: Path, temp_output: Path, info: dict) -> int:
    cleanup_paths([temp_output])

    sub_mode = subtitle_plan(info)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source),
        "-map", "0:v:0",
        "-map", "0:a?",
    ]

    if sub_mode == "copy":
        cmd += ["-map", "0:s?", "-c:v", "copy", "-c:a", "copy", "-c:s", "copy"]
        logging.info("[REMUX] Keeping copy-safe subtitles")
    elif sub_mode == "mov_text":
        cmd += ["-c:v", "copy", "-c:a", "copy", "-sn"]
        logging.info("[REMUX] Dropping non-copy-safe text subs (remux is stream-copy only)")
    else:
        cmd += ["-c:v", "copy", "-c:a", "copy", "-sn"]
        if sub_mode == "drop":
            logging.info("[REMUX] Dropping image/unsupported subtitles")

    cmd += [
        "-map_metadata", "0",
        "-dn",
        "-movflags", "+faststart",
        str(temp_output),
    ]

    logging.info("[REMUX] Fast MP4 remux with stream copy")
    try:
        run_cmd(cmd, timeout=ENCODE_TIMEOUT)
        return verify_mp4_output(temp_output)
    except Exception:
        cleanup_paths([temp_output])
        raise


def process_file(source: Path, work_dir: Path, delete_source: bool = True) -> bool:
    global _CURRENT_TEMPS, _CURRENT_TEMP_OUTPUT, _CURRENT_LOCK

    if has_fail_marker(source):
        logging.info("[SKIP] Has fail marker: %s", source.name)
        return False

    _CURRENT_TEMPS = []
    _CURRENT_TEMP_OUTPUT = None

    try:
        s1_size = get_file_size(source)
    except OSError as exc:
        logging.warning("[SKIP] Cannot stat %s: %s", source.name, exc)
        return False

    time.sleep(1.0)
    try:
        if get_file_size(source) != s1_size:
            logging.info("[SKIP] File size changing: %s", source.name)
            return False
    except OSError:
        logging.info("[SKIP] File disappeared during size check: %s", source.name)
        return False

    lock = acquire_lock(source)
    if lock is None:
        logging.info("[LOCK] Locked by another process: %s", source.name)
        return False
    _CURRENT_LOCK = lock

    temp_dir = Path(tempfile.gettempdir())

    log_summary: Dict[str, Any] = {
        "source": str(source.name),
        "original_size": s1_size,
        "video_codec": None,
        "audio_codecs": None,
        "action": None,
        "source_quality": None,
        "target_video_kbps": None,
        "probe_estimated_crf": None,
        "retries_used": 0,
        "resolution_reduced": False,
        "output_size": None,
        "status": "FAILED",
    }

    sample_path: Optional[Path] = None
    temp_output: Optional[Path] = None
    output_path: Optional[Path] = None
    encoding_success = False
    permanent_fail = False
    current_video_kbps = 0
    audio_budget_kbps = 0
    output_size = 0

    try:
        logging.info("=" * 60)
        logging.info("[START] %s (%s)", source.name, format_size(log_summary["original_size"]))

        try:
            info = ffprobe_json(source)
        except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError) as exc:
            logging.error("[META] ffprobe failed for %s: %s", source.name, exc)
            log_summary["status"] = "SKIP_PROBE_ERROR"
            return False

        duration = get_duration(info)
        if duration <= 0:
            logging.error("[META] No duration: %s", source.name)
            log_summary["status"] = "SKIP_NO_DURATION"
            permanent_fail = True
            return False

        video_stream = get_video_stream(info)
        if video_stream is None:
            logging.error("[META] No video stream: %s", source.name)
            log_summary["status"] = "SKIP_NO_VIDEO"
            permanent_fail = True
            return False

        audio_codecs = get_audio_codecs(info)
        subtitle_codecs = get_subtitle_codecs(info)
        video_codec = get_codec_name(info).lower()
        audio_budget_kbps = planned_audio_kbps(info)
        width, height = get_resolution(info)
        fps = get_fps(info)
        sub_mode = subtitle_plan(info)
        output_path = final_output_path_for(source)
        log_summary["video_codec"] = video_codec
        log_summary["audio_codecs"] = ",".join(audio_codecs) if audio_codecs else "none"

        logging.info(
            "[META] Duration=%.2fs | Res=%dx%d | FPS=%.2f | Video=%s | Audio=%s "
            "(budget %dkbps) | Subs=%s (plan=%s)",
            duration,
            width,
            height,
            fps,
            video_codec,
            ",".join(audio_codecs) if audio_codecs else "none",
            audio_budget_kbps,
            ",".join(subtitle_codecs) if subtitle_codecs else "none",
            sub_mode,
        )

        audio_copy_ok = can_copy_audio_to_mp4(info)
        fast_remux_candidate = is_h264_video_codec(video_codec) and audio_copy_ok

        if is_h264_video_codec(video_codec) and not audio_copy_ok:
            logging.info("[REMUX] H264 source has non-MP4-copy-safe audio; using encode pipeline")

        if fast_remux_candidate and log_summary["original_size"] <= THRESHOLD_BYTES:
            temp_output = temp_output_path_for(output_path, "remux")
            _CURRENT_TEMPS.append(temp_output)
            _CURRENT_TEMP_OUTPUT = temp_output
            log_summary["action"] = "FAST_REMUX"
            try:
                output_size = fast_remux_to_mp4(source, temp_output, info)
                log_summary["output_size"] = output_size
                if output_path.exists() and output_path.resolve() != temp_output.resolve():
                    # Extremely rare race: pick a new free name.
                    output_path = final_output_path_for(source)
                temp_output.rename(output_path)
                if temp_output in _CURRENT_TEMPS:
                    _CURRENT_TEMPS.remove(temp_output)
                _CURRENT_TEMP_OUTPUT = None
                encoding_success = True
                clear_fail_marker(source)
                log_summary["status"] = "REMUX_SUCCESS"
                logging.info(
                    "[DONE] %s -> %s (%s)",
                    source.name, output_path.name, format_size(output_size),
                )

                if delete_source:
                    deleted, _remaining = delete_source_file(source)
                    if not deleted:
                        log_summary["status"] = "REMUX_SUCCESS_SOURCE_DELETE_FAILED"
                        # Output is valid; do not treat as encode failure.
                return True
            except subprocess.CalledProcessError as exc:
                logging.warning(
                    "[REMUX] Failed; falling back to encode: %s",
                    (exc.stderr[:1000] if exc.stderr else "no stderr"),
                )
                cleanup_paths([temp_output])
                if temp_output in _CURRENT_TEMPS:
                    _CURRENT_TEMPS.remove(temp_output)
                temp_output = None
                _CURRENT_TEMP_OUTPUT = None
                log_summary["action"] = "ENCODE_AFTER_REMUX_FAIL"
            except Exception as exc:
                logging.warning("[REMUX] Failed; falling back to encode: %s", exc)
                cleanup_paths([temp_output])
                if temp_output in _CURRENT_TEMPS:
                    _CURRENT_TEMPS.remove(temp_output)
                temp_output = None
                _CURRENT_TEMP_OUTPUT = None
                log_summary["action"] = "ENCODE_AFTER_REMUX_FAIL"
        elif fast_remux_candidate:
            logging.info(
                "[REMUX] H264 source is stream-copy compatible, but exceeds threshold; "
                "using encode pipeline"
            )

        quality = analyze_source_quality(info)
        log_summary["source_quality"] = quality["category"]
        if quality["should_warn"]:
            logging.warning("[QUALITY] %s", quality["message"])

        if (
            quality["category"] in ("compressed", "heavily_compressed")
            and log_summary["original_size"] <= THRESHOLD_BYTES
        ):
            logging.info(
                "[QUALITY] Already compressed and under threshold -- encode required for "
                "MP4 compatibility"
            )

        current_video_kbps = calculate_target_bitrate(duration, audio_budget_kbps)
        log_summary["target_video_kbps"] = current_video_kbps

        if current_video_kbps < 200:
            logging.warning(
                "[BITRATE] Very low target bitrate (%dkbps). Quality will be poor.",
                current_video_kbps,
            )

        source_token = path_token(source)
        sample_path = temp_dir / f"tg4gb_{source_token}_sample_{os.getpid()}.mkv"
        _CURRENT_TEMPS.append(sample_path)

        if _INTERRUPTED:
            raise KeyboardInterrupt

        extract_sample(source, sample_path, duration)

        sample_info = ffprobe_json(sample_path)
        sample_dur = get_duration(sample_info)
        if sample_dur <= 0:
            sample_dur = float(max(len(_sample_start_times(duration)), 1) * min(
                SAMPLE_CLIP_DURATION, max(duration, 0.1)
            ))
        r_time = duration / sample_dur if sample_dur > 0 else 1.0
        logging.info("[SAMPLE] sample_dur=%.1fs | R_time=%.2f", sample_dur, r_time)

        if _INTERRUPTED:
            raise KeyboardInterrupt

        feasibility = run_crf_feasibility_probe(
            sample_path, temp_dir, r_time, TARGET_BYTES, audio_budget_kbps, duration,
        )
        log_summary["probe_estimated_crf"] = feasibility["estimated_crf"]

        if feasibility["quality_warning"]:
            logging.warning("[FEASIBILITY] %s", feasibility["quality_warning"])

        scale_filter: Optional[str] = None
        if feasibility.get("suggest_scale") and width > 1280 and height > 720:
            logging.warning(
                "[FEASIBILITY] Pre-scaling %dx%d -> fit 1280x720 based on probe",
                width, height,
            )
            scale_filter = SCALE_720P
            log_summary["resolution_reduced"] = True
            current_video_kbps = calculate_target_bitrate(duration, audio_budget_kbps)
            log_summary["target_video_kbps"] = current_video_kbps

        bitrate_scale = float(feasibility.get("bitrate_scale") or 1.0)
        if bitrate_scale != 1.0:
            scaled = max(int(current_video_kbps * bitrate_scale), 100)
            logging.info(
                "[PROBE] Adjusting target bitrate %dkbps -> %dkbps (scale=%.3f)",
                current_video_kbps, scaled, bitrate_scale,
            )
            current_video_kbps = scaled
            log_summary["target_video_kbps"] = current_video_kbps

        if not feasibility["can_reach_target"]:
            logging.error(
                "[FEASIBILITY] Target likely impossible even after probe adjustments -- will try"
            )

        cleanup_paths([sample_path])
        if sample_path in _CURRENT_TEMPS:
            _CURRENT_TEMPS.remove(sample_path)
        sample_path = None

        log_summary["action"] = log_summary["action"] or "ENCODE"
        temp_output = temp_output_path_for(output_path, "encode")
        stderr_path = temp_dir / f"encode_stderr_{os.getpid()}_{source_token}.txt"
        passlogfile = source.parent / f".x264_pass_{source_token}_{os.getpid()}"

        _CURRENT_TEMPS.append(temp_output)
        _CURRENT_TEMPS.append(stderr_path)
        _CURRENT_TEMPS.append(passlogfile)
        _CURRENT_TEMP_OUTPUT = temp_output

        active_sub_mode = sub_mode if sub_mode in ("copy", "mov_text") else "none"
        retries_done = 0

        while retries_done <= MAX_RETRIES:
            if _INTERRUPTED:
                raise KeyboardInterrupt

            try:
                two_pass_encode_with_progress(
                    source=source,
                    output=temp_output,
                    video_kbps=current_video_kbps,
                    total_duration_sec=duration,
                    audio_codecs=audio_codecs,
                    subtitle_mode=active_sub_mode,
                    stderr_path=stderr_path,
                    passlogfile=passlogfile,
                    scale=scale_filter,
                )
            except subprocess.CalledProcessError:
                if active_sub_mode != "none":
                    logging.warning("[ENCODE] Failed with subtitles -- retrying without subtitles")
                    active_sub_mode = "none"
                    try:
                        two_pass_encode_with_progress(
                            source=source,
                            output=temp_output,
                            video_kbps=current_video_kbps,
                            total_duration_sec=duration,
                            audio_codecs=audio_codecs,
                            subtitle_mode="none",
                            stderr_path=stderr_path,
                            passlogfile=passlogfile,
                            scale=scale_filter,
                        )
                    except subprocess.CalledProcessError:
                        raise
                else:
                    raise

            try:
                output_size = verify_mp4_output(temp_output)
                log_summary["output_size"] = output_size
            except Exception as exc:
                logging.error("[VERIFY] ffprobe failed: %s", exc)
                log_summary["status"] = "FAIL_FFPROBE_SANITY"
                permanent_fail = True
                return False

            if output_size <= THRESHOLD_BYTES:
                log_summary["status"] = "SUCCESS" if output_size <= TARGET_BYTES else "ACCEPTABLE"
                logging.info("[VERIFY] Output: %s", format_size(output_size))
                break

            retries_done += 1
            log_summary["retries_used"] = retries_done

            if retries_done <= MAX_RETRIES:
                logging.warning(
                    "[RETRY %d/%d] Oversize: %s > %s",
                    retries_done, MAX_RETRIES,
                    format_size(output_size), format_size(THRESHOLD_BYTES),
                )
                current_video_kbps = calculate_retry_bitrate(
                    current_video_kbps, output_size, TARGET_BYTES
                )
                log_summary["target_video_kbps"] = current_video_kbps
                cleanup_paths([temp_output])
            else:
                if width > 1280 and height > 720 and not log_summary["resolution_reduced"]:
                    logging.warning(
                        "[FALLBACK] Reducing resolution %dx%d -> fit 1280x720",
                        width, height,
                    )
                    scale_filter = SCALE_720P
                    log_summary["resolution_reduced"] = True
                    retries_done = 0
                    current_video_kbps = calculate_target_bitrate(duration, audio_budget_kbps)
                    log_summary["target_video_kbps"] = current_video_kbps
                    cleanup_paths([temp_output])
                else:
                    logging.error("[FALLBACK] Cannot reach target size")
                    log_summary["status"] = "FAIL_CANNOT_REACH_TARGET"
                    permanent_fail = True
                    return False

        if output_size <= THRESHOLD_BYTES:
            if output_path.exists():
                try:
                    if output_path.resolve() != temp_output.resolve():
                        output_path = final_output_path_for(source)
                except OSError:
                    output_path = final_output_path_for(source)
            temp_output.rename(output_path)
            if temp_output in _CURRENT_TEMPS:
                _CURRENT_TEMPS.remove(temp_output)
            _CURRENT_TEMP_OUTPUT = None
            encoding_success = True
            clear_fail_marker(source)
            logging.info(
                "[DONE] %s -> %s (%s)",
                source.name, output_path.name, format_size(output_size),
            )

            if delete_source:
                deleted, _remaining = delete_source_file(source)
                if not deleted:
                    log_summary["status"] = (
                        f"{log_summary['status']}_SOURCE_DELETE_FAILED"
                        if log_summary["status"] in ("SUCCESS", "ACCEPTABLE")
                        else "SUCCESS_SOURCE_DELETE_FAILED"
                    )
            return True

        log_summary["status"] = "OVERSIZE"
        permanent_fail = True
        return False

    except KeyboardInterrupt:
        log_summary["status"] = "INTERRUPTED"
        raise

    except subprocess.CalledProcessError as exc:
        logging.error("[FFMPEG ERROR] %s", exc.stderr[:2000] if exc.stderr else "no stderr")
        log_summary["status"] = "FFMPEG_ERROR"
        permanent_fail = True
        return False

    except Exception as exc:
        logging.error("[ERROR] %s: %s", type(exc).__name__, exc)
        log_summary["status"] = f"ERROR:{type(exc).__name__}"
        permanent_fail = True
        return False

    finally:
        if not encoding_success and temp_output and safe_path_exists(temp_output):
            cleanup_paths([temp_output])

        for p in list(_CURRENT_TEMPS):
            for suffix in ("-0.log", "-0.log.mbtree"):
                cleanup_paths([Path(f"{p}{suffix}")])

        cleanup_paths(_CURRENT_TEMPS)
        release_lock(lock)
        _CURRENT_LOCK = None
        _CURRENT_TEMPS = []
        _CURRENT_TEMP_OUTPUT = None

        if permanent_fail and not encoding_success and not _INTERRUPTED:
            set_fail_marker(source, reason=str(log_summary.get("status") or "FAILED"))

        logging.info(
            "[SUMMARY] %s | action=%s | vcodec=%s | acodec=%s | orig=%s | quality=%s | "
            "video_kbps=%s | est_crf=%s | retries=%d | res_reduced=%s | out=%s | %s",
            log_summary["source"],
            log_summary["action"] or "N/A",
            log_summary["video_codec"] or "N/A",
            log_summary["audio_codecs"] or "N/A",
            format_size(log_summary["original_size"]) if log_summary["original_size"] else "N/A",
            log_summary["source_quality"] or "N/A",
            str(log_summary["target_video_kbps"]) if log_summary["target_video_kbps"] else "N/A",
            f"{log_summary['probe_estimated_crf']:.1f}" if log_summary["probe_estimated_crf"] else "N/A",
            log_summary["retries_used"],
            log_summary["resolution_reduced"],
            format_size(log_summary["output_size"]) if log_summary["output_size"] else "N/A",
            log_summary["status"],
        )


def _shutdown_cleanup() -> None:
    cleanup_paths(list(_CURRENT_TEMPS))
    if _CURRENT_TEMP_OUTPUT and safe_path_exists(_CURRENT_TEMP_OUTPUT):
        cleanup_paths([_CURRENT_TEMP_OUTPUT])
    if _CURRENT_LOCK:
        release_lock(_CURRENT_LOCK)


atexit.register(_shutdown_cleanup)


def iter_video_targets(work_dir: Path) -> List[Path]:
    targets: List[Path] = []
    for path in sorted(work_dir.rglob("*")):
        if not path.is_file():
            continue
        if STATE_DIR_NAME in path.parts:
            continue
        if path.suffix.lower() not in SUPPORTED_EXTS:
            continue
        if is_temporary_video(path) or is_generated_output(path):
            logging.info("[SCAN] Skipping generated/temp output: %s", path.name)
            continue
        if is_delete_failed_source(path):
            logging.info("[SCAN] Skipping delete-failed source: %s", path.name)
            continue
        if is_locked(path):
            logging.info("[SCAN] Skipping locked: %s", path.name)
            continue
        if has_fail_marker(path):
            logging.info("[SCAN] Skipping failed: %s", path.name)
            continue
        targets.append(path)
    return targets


def main() -> int:
    work_dir = Path(os.path.dirname(os.path.abspath(__file__)))
    setup_logging(work_dir)

    logging.info("=" * 60)
    logging.info(
        "Batch re-encoder v%s | dir=%s | threshold=%s | target=%s | method=2-pass-VBR",
        __version__, work_dir, format_size(THRESHOLD_BYTES), format_size(TARGET_BYTES),
    )

    pass_number = 0
    try:
        while True:
            pass_number += 1
            logging.info("=" * 60)
            logging.info("[PASS %d] Scanning...", pass_number)

            targets = iter_video_targets(work_dir)

            if not targets:
                logging.info("[PASS %d] No files. Done.", pass_number)
                break

            logging.info("[PASS %d] %d file(s) to process.", pass_number, len(targets))

            for fpath in targets:
                if _INTERRUPTED:
                    raise KeyboardInterrupt
                process_file(fpath, work_dir, delete_source=True)

    except KeyboardInterrupt:
        logging.info("[SHUTDOWN] Interrupted.")
        _shutdown_cleanup()
        return 130

    except FileNotFoundError as exc:
        logging.critical("[SHUTDOWN] %s", exc)
        _shutdown_cleanup()
        return 127

    logging.info("[SHUTDOWN] Finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
