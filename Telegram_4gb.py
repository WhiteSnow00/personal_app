from __future__ import annotations
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__version__ = "1.1.0"

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

_CURRENT_TEMPS: List[Path] = []
_CURRENT_OUTPUT: Optional[Path] = None
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


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def cleanup_paths(paths: List[Path]) -> None:
    for p in paths:
        try:
            if p.exists():
                p.unlink()
                logging.debug("Cleaned up: %s", p)
        except OSError as exc:
            logging.warning("Failed to remove %s: %s", p, exc)


def fail_marker_path(video_path: Path) -> Path:
    return video_path.with_suffix(video_path.suffix + FAIL_MARKER_SUFFIX)


def has_fail_marker(video_path: Path) -> bool:
    return fail_marker_path(video_path).exists()


def set_fail_marker(video_path: Path) -> None:
    try:
        fail_marker_path(video_path).write_text(str(int(time.time())))
    except OSError:
        pass


def clear_fail_marker(video_path: Path) -> None:
    try:
        p = fail_marker_path(video_path)
        if p.exists():
            p.unlink()
    except OSError:
        pass


def lock_path_for(video_path: Path) -> Path:
    return video_path.with_suffix(video_path.suffix + ".lock")


def is_locked(video_path: Path) -> bool:
    lock = lock_path_for(video_path)
    if not lock.exists():
        return False
    try:
        pid = int(lock.read_text().strip())
    except (ValueError, OSError):
        try:
            lock.unlink()
        except OSError:
            pass
        return False
    if is_pid_alive(pid):
        return True
    try:
        lock.unlink()
    except OSError:
        pass
    return False


def acquire_lock(video_path: Path) -> Optional[Path]:
    lock = lock_path_for(video_path)
    pid = os.getpid()
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(pid))
            return lock
        except FileExistsError:
            try:
                existing_pid = int(lock.read_text().strip())
            except (ValueError, OSError):
                existing_pid = None
            if existing_pid is not None and not is_pid_alive(existing_pid):
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
        if lock.exists():
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


def get_audio_stream(info: dict) -> Optional[dict]:
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            return s
    return None


def has_subtitle_streams(info: dict) -> bool:
    return any(s.get("codec_type") == "subtitle" for s in info.get("streams", []))


def get_stream_codecs(info: dict, codec_type: str) -> List[str]:
    codecs: List[str] = []
    for stream in info.get("streams", []):
        if stream.get("codec_type") == codec_type:
            codecs.append(str(stream.get("codec_name", "unknown")).lower())
    return codecs


def get_audio_codec(info: dict) -> str:
    audio = get_audio_stream(info)
    if audio is None:
        return "none"
    return audio.get("codec_name", "unknown")


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
    return name.endswith(".tmp.mp4") or ".tmp." in name


def final_output_path_for(source: Path, work_dir: Path) -> Path:
    desired = work_dir / f"{source.stem}{FINAL_OUTPUT_SUFFIX}.mp4"
    try:
        if not desired.exists() or desired.resolve() == source.resolve():
            return desired
    except OSError:
        if not desired.exists():
            return desired

    counter = 2
    while True:
        candidate = work_dir / f"{source.stem}{FINAL_OUTPUT_SUFFIX}_{counter}.mp4"
        if not candidate.exists():
            logging.warning("[OUTPUT] %s exists; using %s", desired.name, candidate.name)
            return candidate
        counter += 1


def temp_output_path_for(output_path: Path, label: str) -> Path:
    return output_path.with_name(f".{output_path.stem}_{label}_{os.getpid()}.tmp.mp4")


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
    audio = get_audio_stream(info)
    if audio is None:
        return AUDIO_BITRATE_KBPS
    br = audio.get("bit_rate")
    if br is not None:
        return int(float(br) / 1000)
    tags = audio.get("tags", {})
    bps = tags.get("BPS") or tags.get("bps")
    if bps is not None:
        return int(float(bps) / 1000)
    return AUDIO_BITRATE_KBPS


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
        audio_kbps = get_audio_bitrate(info)
        total_kbps = int(file_size * 8 / duration / 1000)
        return max(100, total_kbps - audio_kbps)
    return 0


def get_resolution(info: dict) -> Tuple[int, int]:
    video = get_video_stream(info)
    if video is None:
        return (0, 0)
    return (video.get("width", 0), video.get("height", 0))


def get_fps(info: dict) -> float:
    video = get_video_stream(info)
    if video is None:
        return 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = video.get(key, "")
        if "/" in str(rate):
            try:
                num, den = str(rate).split("/")
                return float(num) / float(den)
            except (ValueError, ZeroDivisionError):
                pass
    return 0.0


def get_codec_name(info: dict) -> str:
    video = get_video_stream(info)
    if video is None:
        return "unknown"
    return video.get("codec_name", "unknown")


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

    bpp = video_kbps / (width * height * fps)
    effective_bpp = bpp * efficiency
    result["bpp"] = bpp
    result["effective_bpp"] = effective_bpp

    if effective_bpp > 0.3:
        result["category"] = "high"
        result["message"] = f"High quality source (BPP={bpp:.4f}, effective={effective_bpp:.4f}, codec={codec_name})"
    elif effective_bpp >= 0.15:
        result["category"] = "medium"
        result["message"] = f"Medium quality source (BPP={bpp:.4f}, effective={effective_bpp:.4f}, codec={codec_name})"
    elif effective_bpp >= 0.08:
        result["category"] = "compressed"
        result["message"] = f"Already compressed source (BPP={bpp:.4f}, effective={effective_bpp:.4f}, codec={codec_name}). Quality may be poor."
        result["should_warn"] = True
    else:
        result["category"] = "heavily_compressed"
        result["message"] = f"Heavily compressed source (BPP={bpp:.4f}, effective={effective_bpp:.4f}, codec={codec_name}). Consider -c copy if size is acceptable."
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

    target_size_MB = target_size_bytes / (1024 * 1024)
    total_kbps = (target_size_MB * 8000 * overhead_factor / duration_sec) * safety_margin
    video_kbps = max(total_kbps - audio_kbps, 100)

    logging.info(
        "[BITRATE] target=%s | duration=%.1fs | audio=%dkbps | video=%dkbps",
        format_size(target_size_bytes), duration_sec, audio_kbps, int(video_kbps),
    )
    return int(video_kbps)


def extract_sample(source: Path, output: Path, duration: float) -> None:
    logging.info("[SAMPLE] Extracting %d clips from %s", SAMPLE_CLIP_COUNT, source.name)

    temp_dir = Path(tempfile.gettempdir())
    local_temps: List[Path] = []
    clips: List[Path] = []
    percentages = [0.10, 0.30, 0.50, 0.70, 0.90]

    try:
        for idx, pct in enumerate(percentages, start=1):
            start = max(0.0, duration * pct)
            if start + SAMPLE_CLIP_DURATION > duration:
                start = max(0.0, duration - SAMPLE_CLIP_DURATION)

            clip_path = temp_dir / f"{source.stem}_clip{idx}_{os.getpid()}.mkv"
            clips.append(clip_path)
            local_temps.append(clip_path)

            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", str(start),
                "-i", str(source),
                "-t", str(SAMPLE_CLIP_DURATION),
                "-c:v", ENCODE_CODEC, "-preset", "ultrafast", "-crf", "23",
                "-c:a", "copy",
                "-avoid_negative_ts", "make_zero",
                str(clip_path),
            ]
            logging.info("[SAMPLE] Clip %d/%d @ %.1fs (ultrafast)", idx, SAMPLE_CLIP_COUNT, start)
            run_cmd(cmd, timeout=120)

        list_path = temp_dir / f"{source.stem}_concat_list_{os.getpid()}.txt"
        local_temps.append(list_path)

        list_lines = []
        for c in clips:
            escaped = str(c.resolve()).replace("'", "'\\''")
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


def probe_sample(sample_path: Path, crf: int, maxrate_kbps: int, bufsize_kbps: int, temp_dir: Path) -> int:
    probe_out = temp_dir / f"{sample_path.stem}_probe_crf{crf}_{os.getpid()}.mp4"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(sample_path),
        "-c:v", ENCODE_CODEC, "-preset", PRESET, "-crf", str(crf),
        "-maxrate", f"{maxrate_kbps}k",
        "-bufsize", f"{bufsize_kbps}k",
        "-c:a", "copy", "-sn", "-dn",
        str(probe_out),
    ]
    logging.info("[PROBE] CRF %d (maxrate=%dk, bufsize=%dk)", crf, maxrate_kbps, bufsize_kbps)
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
) -> Optional[float]:
    valid = [(crf, sz) for crf, sz in probe_results if sz > 0]
    if len(valid) < 2:
        return None

    valid.sort(key=lambda x: x[0])

    extrapolated = [(crf, sz * r_time) for crf, sz in valid]

    below = None
    above = None
    for crf, sz in extrapolated:
        if sz <= target_bytes:
            below = (crf, sz)
        if sz >= target_bytes and above is None:
            above = (crf, sz)

    if below is None and above is None:
        return None

    if above is None:
        c1, s1 = extrapolated[0]
        c2, s2 = extrapolated[1]
    elif below is None:
        c1, s1 = extrapolated[-2]
        c2, s2 = extrapolated[-1]
    else:
        c1, s1 = above
        c2, s2 = below

    if s1 <= 0 or s2 <= 0 or s1 == s2:
        return None

    ln_s1 = math.log(s1)
    ln_s2 = math.log(s2)
    ln_target = math.log(target_bytes)

    k = (ln_s1 - ln_s2) / (c1 - c2)
    if k == 0 or math.isnan(k) or math.isinf(k):
        return None

    return c1 + (ln_s1 - ln_target) / k


def run_crf_feasibility_probe(
    sample_path: Path,
    temp_dir: Path,
    r_time: float,
    target_bytes: int,
    target_video_kbps: int,
) -> Dict[str, Any]:
    logging.info("[PROBE] Running 4-point CRF feasibility probe...")
    probe_results: List[Tuple[int, int]] = []

    maxrate = int(target_video_kbps * 1.5)
    bufsize = int(target_video_kbps * 2)

    for crf in PROBE_CRF_POINTS:
        if _INTERRUPTED:
            raise KeyboardInterrupt
        size = probe_sample(sample_path, crf, maxrate, bufsize, temp_dir)
        if size > 0:
            probe_results.append((crf, size))
            full_est = size * r_time
            logging.info("[PROBE] CRF %d -> sample=%s | full_est=%s", crf, format_size(size), format_size(int(full_est)))

    result: Dict[str, Any] = {
        "probe_results": probe_results,
        "estimated_crf": None,
        "is_feasible": False,
        "quality_warning": None,
        "can_reach_target": False,
    }

    if len(probe_results) < 2:
        result["quality_warning"] = "Insufficient probe results -- proceeding with caution"
        return result

    est_crf = estimate_crf_for_target(probe_results, target_bytes, r_time)
    result["estimated_crf"] = est_crf

    if est_crf is not None:
        logging.info("[PROBE] Estimated CRF for target: %.2f", est_crf)
        if est_crf > CRF_CLAMP_MAX:
            result["quality_warning"] = f"Impossible target: estimated CRF {est_crf:.1f} exceeds max {CRF_CLAMP_MAX}"
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
        max_crf = max(probe_results, key=lambda x: x[0])
        max_full_est = max_crf[1] * r_time
        if max_full_est < target_bytes:
            result["can_reach_target"] = True
            result["is_feasible"] = True
        else:
            result["quality_warning"] = "Target may not be achievable"

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


def two_pass_encode_with_progress(
    source: Path,
    output: Path,
    video_kbps: int,
    total_duration_sec: float,
    audio_codecs: List[str],
    has_subs: bool,
    stderr_path: Path,
    passlogfile: Path,
    scale: Optional[str] = None,
) -> None:
    video_args = build_ffmpeg_video_args(video_kbps, scale)
    passlog_prefix = str(passlogfile)
    audio_label = ",".join(audio_codecs) if audio_codecs else "none"

    if not audio_codecs or all(codec in MP4_AUDIO_COPY_CODECS for codec in audio_codecs):
        audio_args = ["-c:a", "copy"]
    else:
        audio_args = ["-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_KBPS}k"]
        logging.info("[ENCODE] Re-encoding audio %s -> AAC %dkbps", audio_label, AUDIO_BITRATE_KBPS)

    sub_args: List[str] = ["-sn"]
    input_maps = ["-map", "0:v:0", "-map", "0:a?"]

    logging.info("[ENCODE] Pass 1/2 -- analysis")
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
    try:
        process = subprocess.Popen(pass2_cmd, stdout=subprocess.PIPE, stderr=stderr_fh, text=True)

        out_time_ms = 0
        fps = 0.0
        speed = 0.0
        last_print = 0.0
        start_time = time.time()

        try:
            while True:
                if _INTERRUPTED:
                    process.send_signal(signal.SIGINT)
                    break

                line = process.stdout.readline()
                if not line:
                    break

                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        out_time_ms = int(line.split("=", 1)[1]) // 1000
                    except ValueError:
                        pass
                elif line.startswith("out_time_ms="):
                    try:
                        out_time_ms = int(line.split("=", 1)[1]) // 1000
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
                    pct = min(100.0, (out_time_ms / 1000.0) / total_duration_sec * 100)
                    elapsed = now - start_time
                    eta = (elapsed / pct * 100) - elapsed if pct > 0 else 0.0
                    print(
                        f"\r[ENCODE] {pct:5.1f}% | "
                        f"Elapsed {format_time(elapsed)} | ETA {format_time(eta)} | "
                        f"fps {fps:.1f} | {speed:.2f}x      ",
                        end="", flush=True,
                    )
                    last_print = now

        finally:
            if _INTERRUPTED:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
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

    if process.returncode != 0:
        stderr_text = "(stderr empty or unreadable)"
        try:
            text = stderr_path.read_text(encoding="utf-8").strip()
            if text:
                stderr_text = text
        except OSError:
            pass
        raise subprocess.CalledProcessError(process.returncode, pass2_cmd, output="", stderr=stderr_text)


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


def safe_move(src: Path, dest_dir: Path) -> None:
    if src.parent == dest_dir:
        return
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.stem}_{int(time.time())}{src.suffix}"
    try:
        shutil.move(str(src), str(dest))
        logging.info("[MOVE] %s -> %s", src.name, dest_dir.name)
    except Exception as e:
        logging.error("[MOVE] Failed to move %s: %s", src.name, e)


def delete_source_file(source: Path) -> bool:
    try:
        source.unlink()
        logging.info("[CLEANUP] Deleted source: %s", source.name)
        return True
    except OSError as exc:
        failed_name = source.with_name(f"{source.stem}_DELETE_FAILED{source.suffix}")
        logging.critical("[CLEANUP] Cannot delete %s: %s", source.name, exc)
        try:
            source.rename(failed_name)
        except OSError as exc2:
            logging.critical("[CLEANUP] Rename failed: %s", exc2)
        return False


def fast_remux_to_mp4(source: Path, temp_output: Path, info: dict) -> int:
    cleanup_paths([temp_output])

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source),
        "-map", "0:v:0",
        "-map", "0:a?",
    ]

    if has_subtitle_streams(info):
        cmd += ["-map", "0:s?"]

    cmd += [
        "-c:v", "copy",
        "-c:a", "copy",
        "-sn",
    ]

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
    global _CURRENT_TEMPS, _CURRENT_OUTPUT, _CURRENT_LOCK

    if has_fail_marker(source):
        logging.info("[SKIP] Has fail marker: %s", source.name)
        return False

    _CURRENT_TEMPS = []
    _CURRENT_OUTPUT = None

    s1_size = get_file_size(source)
    time.sleep(1.0)
    if get_file_size(source) != s1_size:
        logging.info("[SKIP] File size changing: %s", source.name)
        return False

    lock = acquire_lock(source)
    if lock is None:
        logging.info("[LOCK] Locked by another process: %s", source.name)
        return False
    _CURRENT_LOCK = lock

    temp_dir = Path(tempfile.gettempdir())

    log_summary = {
        "source": str(source.name),
        "original_size": get_file_size(source),
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
    current_video_kbps: int = 0

    try:
        logging.info("=" * 60)
        logging.info("[START] %s (%s)", source.name, format_size(log_summary["original_size"]))

        info = ffprobe_json(source)
        duration = get_duration(info)
        if duration <= 0:
            logging.error("[META] No duration: %s", source.name)
            log_summary["status"] = "SKIP_NO_DURATION"
            return False

        video_stream = get_video_stream(info)
        if video_stream is None:
            logging.error("[META] No video stream: %s", source.name)
            log_summary["status"] = "SKIP_NO_VIDEO"
            return False

        has_subs = has_subtitle_streams(info)
        audio_codecs = get_audio_codecs(info)
        subtitle_codecs = get_subtitle_codecs(info)
        video_codec = get_codec_name(info).lower()
        audio_kbps = get_audio_bitrate(info)
        width, height = get_resolution(info)
        fps = get_fps(info)
        output_path = final_output_path_for(source, work_dir)
        _CURRENT_OUTPUT = output_path
        log_summary["video_codec"] = video_codec
        log_summary["audio_codecs"] = ",".join(audio_codecs) if audio_codecs else "none"

        logging.info(
            "[META] Duration=%.2fs | Res=%dx%d | FPS=%.2f | Video=%s | Audio=%s (%dkbps) | Subs=%s",
            duration,
            width,
            height,
            fps,
            video_codec,
            ",".join(audio_codecs) if audio_codecs else "none",
            audio_kbps,
            ",".join(subtitle_codecs) if subtitle_codecs else "none",
        )

        audio_copy_ok = can_copy_audio_to_mp4(info)
        fast_remux_candidate = is_h264_video_codec(video_codec) and audio_copy_ok

        if is_h264_video_codec(video_codec) and not audio_copy_ok:
            logging.info("[REMUX] H264 source has non-MP4-copy-safe audio; using encode pipeline")

        if fast_remux_candidate and log_summary["original_size"] <= THRESHOLD_BYTES:
            temp_output = temp_output_path_for(output_path, "remux")
            _CURRENT_TEMPS.append(temp_output)
            log_summary["action"] = "FAST_REMUX"
            try:
                output_size = fast_remux_to_mp4(source, temp_output, info)
                log_summary["output_size"] = output_size
                temp_output.rename(output_path)
                encoding_success = True
                clear_fail_marker(source)
                log_summary["status"] = "REMUX_SUCCESS"
                logging.info("[DONE] %s -> %s (%s)", source.name, output_path.name, format_size(output_size))

                if delete_source and not delete_source_file(source):
                    log_summary["status"] = "FAIL_SOURCE_DELETE"
                    encoding_success = False
                    return False

                return True
            except subprocess.CalledProcessError as exc:
                logging.warning("[REMUX] Failed; falling back to encode: %s", exc.stderr[:1000] if exc.stderr else "no stderr")
                cleanup_paths([temp_output])
                if temp_output in _CURRENT_TEMPS:
                    _CURRENT_TEMPS.remove(temp_output)
                temp_output = None
                log_summary["action"] = "ENCODE_AFTER_REMUX_FAIL"
            except Exception as exc:
                logging.warning("[REMUX] Failed; falling back to encode: %s", exc)
                cleanup_paths([temp_output])
                if temp_output in _CURRENT_TEMPS:
                    _CURRENT_TEMPS.remove(temp_output)
                temp_output = None
                log_summary["action"] = "ENCODE_AFTER_REMUX_FAIL"
        elif fast_remux_candidate:
            logging.info("[REMUX] H264 source is stream-copy compatible, but exceeds threshold; using encode pipeline")

        quality = analyze_source_quality(info)
        log_summary["source_quality"] = quality["category"]
        if quality["should_warn"]:
            logging.warning("[QUALITY] %s", quality["message"])

        if quality["category"] in ("compressed", "heavily_compressed") and log_summary["original_size"] <= THRESHOLD_BYTES:
            logging.info("[QUALITY] Already compressed and under threshold -- encode required for MP4 compatibility")

        current_video_kbps = calculate_target_bitrate(duration, audio_kbps)
        log_summary["target_video_kbps"] = current_video_kbps

        if current_video_kbps < 200:
            logging.warning("[BITRATE] Very low target bitrate (%dkbps). Quality will be poor.", current_video_kbps)

        sample_path = temp_dir / f"{source.stem}_sample_{os.getpid()}.mkv"
        _CURRENT_TEMPS.append(sample_path)

        if _INTERRUPTED:
            raise KeyboardInterrupt

        extract_sample(source, sample_path, duration)

        sample_info = ffprobe_json(sample_path)
        sample_dur = get_duration(sample_info)
        if sample_dur <= 0:
            sample_dur = float(SAMPLE_CLIP_COUNT * SAMPLE_CLIP_DURATION)
        r_time = duration / sample_dur if sample_dur > 0 else 1.0
        logging.info("[SAMPLE] sample_dur=%.1fs | R_time=%.2f", sample_dur, r_time)

        if _INTERRUPTED:
            raise KeyboardInterrupt

        feasibility = run_crf_feasibility_probe(sample_path, temp_dir, r_time, TARGET_BYTES, current_video_kbps)
        log_summary["probe_estimated_crf"] = feasibility["estimated_crf"]

        if feasibility["quality_warning"]:
            logging.warning("[FEASIBILITY] %s", feasibility["quality_warning"])

        if not feasibility["can_reach_target"]:
            logging.error("[FEASIBILITY] Target likely impossible -- will try anyway")

        cleanup_paths([sample_path])
        if sample_path in _CURRENT_TEMPS:
            _CURRENT_TEMPS.remove(sample_path)

        log_summary["action"] = log_summary["action"] or "ENCODE"
        temp_output = temp_output_path_for(output_path, "encode")
        stderr_path = temp_dir / f"encode_stderr_{os.getpid()}.txt"
        passlogfile = work_dir / f".x264_pass_{source.stem}_{os.getpid()}"

        _CURRENT_TEMPS.append(temp_output)
        _CURRENT_TEMPS.append(stderr_path)
        _CURRENT_TEMPS.append(passlogfile)

        scale_filter: Optional[str] = None
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
                    has_subs=has_subs,
                    stderr_path=stderr_path,
                    passlogfile=passlogfile,
                    scale=scale_filter,
                )
            except subprocess.CalledProcessError:
                if has_subs and retries_done == 0 and scale_filter is None:
                    logging.warning("[ENCODE] Failed -- retrying without subtitles...")
                    try:
                        two_pass_encode_with_progress(
                            source=source,
                            output=temp_output,
                            video_kbps=current_video_kbps,
                            total_duration_sec=duration,
                            audio_codecs=audio_codecs,
                            has_subs=False,
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
                current_video_kbps = calculate_retry_bitrate(current_video_kbps, output_size, TARGET_BYTES)
                log_summary["target_video_kbps"] = current_video_kbps
                try:
                    temp_output.unlink()
                except OSError:
                    pass
            else:
                if width > 1280 and height > 720 and not log_summary["resolution_reduced"]:
                    logging.warning("[FALLBACK] Reducing resolution %dx%d -> fit 1280x720", width, height)
                    scale_filter = "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2"
                    log_summary["resolution_reduced"] = True
                    retries_done = 0
                    current_video_kbps = calculate_target_bitrate(duration, audio_kbps)
                    log_summary["target_video_kbps"] = current_video_kbps
                    try:
                        temp_output.unlink()
                    except OSError:
                        pass
                else:
                    logging.error("[FALLBACK] Cannot reach target size")
                    log_summary["status"] = "FAIL_CANNOT_REACH_TARGET"
                    return False

        if output_size <= THRESHOLD_BYTES:
            temp_output.rename(output_path)
            encoding_success = True
            clear_fail_marker(source)
            logging.info("[DONE] %s -> %s (%s)", source.name, output_path.name, format_size(output_size))

            if delete_source:
                if not delete_source_file(source):
                    log_summary["status"] = "FAIL_SOURCE_DELETE"
                    encoding_success = False
                    return False

            return True
        else:
            log_summary["status"] = "OVERSIZE"
            return False

    except KeyboardInterrupt:
        log_summary["status"] = "INTERRUPTED"
        raise

    except subprocess.CalledProcessError as exc:
        logging.error("[FFMPEG ERROR] %s", exc.stderr[:2000] if exc.stderr else "no stderr")
        log_summary["status"] = "FFMPEG_ERROR"
        set_fail_marker(source)
        return False

    except Exception as exc:
        logging.error("[ERROR] %s: %s", type(exc).__name__, exc)
        log_summary["status"] = f"ERROR:{type(exc).__name__}"
        set_fail_marker(source)
        return False

    finally:
        if not encoding_success and temp_output and temp_output.exists():
            try:
                temp_output.unlink()
            except OSError:
                pass

        for p in list(_CURRENT_TEMPS):
            for suffix in ("-0.log", "-0.log.mbtree"):
                pl = Path(f"{p}{suffix}")
                cleanup_paths([pl])

        cleanup_paths(_CURRENT_TEMPS)
        release_lock(lock)
        _CURRENT_LOCK = None
        _CURRENT_TEMPS = []
        _CURRENT_OUTPUT = None

        if not encoding_success and not _INTERRUPTED:
            set_fail_marker(source)

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

            targets: List[Path] = []
            for path in sorted(work_dir.rglob("*")):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in SUPPORTED_EXTS:
                    continue
                if is_temporary_video(path) or is_generated_output(path):
                    logging.info("[SCAN] Skipping generated/temp output: %s", path.name)
                    continue
                if is_locked(path):
                    logging.info("[SCAN] Skipping locked: %s", path.name)
                    continue
                if has_fail_marker(path):
                    logging.info("[SCAN] Skipping failed: %s", path.name)
                    continue
                targets.append(path)

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
        cleanup_paths(_CURRENT_TEMPS)
        if _CURRENT_OUTPUT and _CURRENT_OUTPUT.exists() and _CURRENT_OUTPUT in _CURRENT_TEMPS:
            try:
                _CURRENT_OUTPUT.unlink()
            except OSError:
                pass
        if _CURRENT_LOCK:
            release_lock(_CURRENT_LOCK)
        return 130

    except FileNotFoundError as exc:
        logging.critical("[SHUTDOWN] %s", exc)
        return 127

    logging.info("[SHUTDOWN] Finished.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
