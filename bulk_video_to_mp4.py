#!/usr/bin/env python3

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

VIDEO_EXTENSIONS: Set[str] = {
    ".m4v",
    ".mov",
    ".avi",
    ".mkv",
    ".wmv",
    ".flv",
    ".webm",
    ".ts",
}

MP4_EXTENSION = ".mp4"
PARTIAL_SUFFIX = ".mp4.partial"

SHORT_VIDEO_DIR_NAME = "short_videos"
SHORT_VIDEO_THRESHOLD_SEC = 60.0

DUPLICATES_DIR_NAME = "duplicates"

LOG_FILE_PREFIX = "convert_log_"

MAX_CONCURRENT_JOBS = 4
MIN_CONCURRENT_JOBS = 1

DISK_SAFETY_PERCENT = 0.05
DISK_SAFETY_MIN_BYTES = 2 * 1024 ** 3

DURATION_TOLERANCE_ABS_SEC = 1.0
DURATION_TOLERANCE_REL = 0.01

FFPROBE_TIMEOUT_SEC = 120
FFMPEG_TIMEOUT_SEC = 6 * 60 * 60
DECODE_CHECK_TIMEOUT_SEC = 2 * 60 * 60
FRAME_EXTRACT_TIMEOUT_SEC = 120

DISPATCH_POLL_INTERVAL_SEC = 0.5

DUP_DURATION_TOLERANCE_SEC = 2.0
AHASH_WIDTH = 16
AHASH_HEIGHT = 16
AHASH_PIXELS = AHASH_WIDTH * AHASH_HEIGHT
AHASH_HAMMING_THRESHOLD = 15


@dataclass
class StreamInfo:
    duration: Optional[float]
    video_streams: int
    audio_streams: int
    subtitle_streams: int
    raw_ok: bool
    error_message: str = ""

    @property
    def total_streams(self) -> int:
        return self.video_streams + self.audio_streams + self.subtitle_streams


@dataclass
class JobResult:
    source: Path
    success: bool
    action: str
    message: str = ""
    output: Optional[Path] = None
    moved_to_short: bool = False
    duration: Optional[float] = None


@dataclass
class RunStats:
    scanned_convertible: int = 0
    already_mp4: int = 0
    converted: int = 0
    failed: List[Tuple[str, str]] = field(default_factory=list)
    moved_short: int = 0
    interrupted: bool = False
    duplicates_found: int = 0
    duplicates_moved: int = 0
    hash_skipped: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class MediaMeta:
    path: Path
    duration: float
    width: int
    height: int
    size_bytes: int
    ahash: Optional[int] = None


_active_procs: Set[subprocess.Popen] = set()
_active_procs_lock = threading.Lock()
_shutdown_event = threading.Event()


def _register_proc(proc: subprocess.Popen) -> None:
    with _active_procs_lock:
        _active_procs.add(proc)


def _unregister_proc(proc: subprocess.Popen) -> None:
    with _active_procs_lock:
        _active_procs.discard(proc)


def _terminate_all_procs() -> None:
    with _active_procs_lock:
        procs = list(_active_procs)
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.terminate()
        except OSError:
            pass
    deadline = time.time() + 3.0
    for proc in procs:
        remaining = max(0.0, deadline - time.time())
        try:
            proc.wait(timeout=remaining if remaining > 0 else 0.1)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        except OSError:
            pass


def setup_logging(scan_root: Path) -> Tuple[logging.Logger, Path]:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = scan_root / f"{LOG_FILE_PREFIX}{ts}.log"

    logger = logging.getLogger("bulk_video_to_mp4")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger, log_path


def require_binaries(logger: logging.Logger) -> None:
    missing = []
    for name in ("ffmpeg", "ffprobe"):
        if shutil.which(name) is None:
            missing.append(name)
    if missing:
        msg = (
            f"Required binary not found on PATH: {', '.join(missing)}. "
            "Install ffmpeg (which provides both ffmpeg and ffprobe) and retry."
        )
        logger.error(msg)
        print(msg, file=sys.stderr)
        sys.exit(1)


def is_log_file(path: Path) -> bool:
    name = path.name
    return name.startswith(LOG_FILE_PREFIX) and name.endswith(".log")


def is_under_dir(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def unique_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    n = 1
    while True:
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def unique_moved_path(dest_dir: Path, source_file: Path, scan_root: Path) -> Path:
    plain = dest_dir / source_file.name
    if not plain.exists():
        return plain

    try:
        rel = source_file.resolve().relative_to(scan_root.resolve())
        prefix = "__".join(rel.parts[:-1]) if rel.parts[:-1] else "root"
        prefix = prefix.replace(os.sep, "__").replace(":", "_").replace("/", "__")
        prefixed = dest_dir / f"{prefix}__{source_file.name}"
        if not prefixed.exists():
            return prefixed
        return unique_path(prefixed)
    except ValueError:
        return unique_path(plain)


def unique_short_path(dest_dir: Path, source_mp4: Path, scan_root: Path) -> Path:
    return unique_moved_path(dest_dir, source_mp4, scan_root)


def safety_margin_bytes(total_bytes: int) -> int:
    return max(int(total_bytes * DISK_SAFETY_PERCENT), DISK_SAFETY_MIN_BYTES)


def run_command(
    args: List[str],
    timeout: float,
    logger: logging.Logger,
) -> Tuple[int, str, str]:
    if _shutdown_event.is_set():
        return -1, "", "shutdown requested before start"

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except OSError as exc:
        return 1, "", str(exc)

    _register_proc(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("Command timed out after %ss: %s", timeout, args[0])
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except OSError:
                pass
            return -2, "", f"timeout after {timeout}s"
        return int(proc.returncode if proc.returncode is not None else 1), stdout or "", stderr or ""
    finally:
        _unregister_proc(proc)


def run_command_binary(
    args: List[str],
    timeout: float,
    logger: logging.Logger,
) -> Tuple[int, bytes, str]:
    if _shutdown_event.is_set():
        return -1, b"", "shutdown requested before start"

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return 127, b"", str(exc)
    except OSError as exc:
        return 1, b"", str(exc)

    _register_proc(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("Command timed out after %ss: %s", timeout, args[0])
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except OSError:
                pass
            return -2, b"", f"timeout after {timeout}s"
        err_text = ""
        if stderr:
            try:
                err_text = stderr.decode("utf-8", errors="replace")
            except Exception:
                err_text = repr(stderr[:500])
        return (
            int(proc.returncode if proc.returncode is not None else 1),
            stdout or b"",
            err_text,
        )
    finally:
        _unregister_proc(proc)


def probe_file(path: Path, logger: logging.Logger) -> StreamInfo:
    args = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "default=noprint_wrappers=1",
        str(path),
    ]
    rc, stdout, stderr = run_command(args, FFPROBE_TIMEOUT_SEC, logger)
    if rc != 0:
        err = (stderr or stdout or f"ffprobe exit {rc}").strip()
        return StreamInfo(
            duration=None,
            video_streams=0,
            audio_streams=0,
            subtitle_streams=0,
            raw_ok=False,
            error_message=err or "ffprobe failed",
        )

    duration: Optional[float] = None
    video = audio = sub = 0
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("duration="):
            val = line.split("=", 1)[1].strip()
            if val and val.lower() != "n/a":
                try:
                    duration = float(val)
                except ValueError:
                    duration = None
        elif line.startswith("codec_type="):
            ctype = line.split("=", 1)[1].strip().lower()
            if ctype == "video":
                video += 1
            elif ctype == "audio":
                audio += 1
            elif ctype == "subtitle":
                sub += 1

    if video + audio + sub == 0 and duration is None:
        return StreamInfo(
            duration=None,
            video_streams=0,
            audio_streams=0,
            subtitle_streams=0,
            raw_ok=False,
            error_message="ffprobe returned no streams or duration",
        )

    return StreamInfo(
        duration=duration,
        video_streams=video,
        audio_streams=audio,
        subtitle_streams=sub,
        raw_ok=True,
        error_message=stderr.strip() if stderr.strip() else "",
    )


def duration_matches(src: Optional[float], out: Optional[float]) -> bool:
    if src is None and out is None:
        return True
    if src is None or out is None:
        return False
    tol = max(DURATION_TOLERANCE_ABS_SEC, abs(src) * DURATION_TOLERANCE_REL)
    return abs(out - src) <= tol


def streams_roughly_match(src: StreamInfo, out: StreamInfo) -> bool:
    if src.video_streams != out.video_streams:
        return False
    if src.audio_streams != out.audio_streams:
        return False
    if out.subtitle_streams > src.subtitle_streams:
        return False
    return True


def stage1_ok(src: StreamInfo, out: StreamInfo) -> Tuple[bool, str]:
    if not out.raw_ok:
        return False, f"output unreadable by ffprobe: {out.error_message}"
    if not streams_roughly_match(src, out):
        return (
            False,
            f"stream count mismatch "
            f"(src v/a/s={src.video_streams}/{src.audio_streams}/{src.subtitle_streams}, "
            f"out v/a/s={out.video_streams}/{out.audio_streams}/{out.subtitle_streams})",
        )
    if not duration_matches(src.duration, out.duration):
        return (
            False,
            f"duration mismatch (src={src.duration}, out={out.duration})",
        )
    if out.error_message:
        return False, f"ffprobe warnings on output: {out.error_message}"
    return True, ""


def _ffmpeg_err_summary(stderr: str, stdout: str = "", max_len: int = 2000) -> str:
    combined = "\n".join(x for x in (stderr or "", stdout or "") if x)
    keep: List[str] = []
    for line in combined.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if s.startswith("ffmpeg version"):
            continue
        if s.startswith("built with"):
            continue
        if s.startswith("configuration:"):
            continue
        if low.startswith("libav") and "copyright" in low:
            continue
        if any(
            x in low
            for x in (
                "libavutil",
                "libavcodec",
                "libavformat",
                "libavdevice",
                "libavfilter",
                "libswscale",
                "libswresample",
                "libpostproc",
            )
        ) and (s.startswith("  ") or low.startswith("lib")):
            continue
        keep.append(s)
    text = "\n".join(keep).strip()
    if not text:
        text = combined.strip()
        if len(text) > max_len:
            text = "..." + text[-max_len:]
        if not text:
            return "(no ffmpeg error text captured)"
        return text
    if len(text) > max_len:
        return "..." + text[-max_len:]
    return text


def full_decode_check(path: Path, logger: logging.Logger) -> Tuple[bool, str]:
    args = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(path),
        "-f", "null",
        "-",
    ]
    rc, stdout, stderr = run_command(args, DECODE_CHECK_TIMEOUT_SEC, logger)
    if _shutdown_event.is_set():
        return False, "interrupted during decode check"
    if rc != 0:
        return False, f"decode check exit {rc}: {_ffmpeg_err_summary(stderr, stdout)}"
    if stderr.strip():
        return False, f"decode errors: {_ffmpeg_err_summary(stderr, stdout)}"
    return True, ""


def remux_to_partial(
    source: Path,
    partial: Path,
    logger: logging.Logger,
) -> Tuple[bool, str]:
    attempts: List[Tuple[str, List[str]]] = [
        (
            "v0+a copy",
            [
                "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c", "copy",
                "-sn", "-dn",
                "-f", "mp4",
                "-movflags", "+faststart",
                str(partial),
            ],
        ),
        (
            "v+a copy",
            [
                "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source),
                "-map", "0:v", "-map", "0:a?",
                "-c", "copy",
                "-sn", "-dn",
                "-f", "mp4",
                "-movflags", "+faststart",
                str(partial),
            ],
        ),
        (
            "v+a aac_bsf",
            [
                "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                "-sn", "-dn",
                "-f", "mp4",
                "-movflags", "+faststart",
                str(partial),
            ],
        ),
        (
            "default copy",
            [
                "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source),
                "-c", "copy",
                "-f", "mp4",
                "-movflags", "+faststart",
                str(partial),
            ],
        ),
        (
            "video-only copy",
            [
                "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source),
                "-map", "0:v:0",
                "-c", "copy",
                "-an", "-sn", "-dn",
                "-f", "mp4",
                "-movflags", "+faststart",
                str(partial),
            ],
        ),
    ]

    errors: List[str] = []
    for label, args in attempts:
        if _shutdown_event.is_set():
            return False, "interrupted during remux"

        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass

        rc, stdout, stderr = run_command(args, FFMPEG_TIMEOUT_SEC, logger)

        if _shutdown_event.is_set():
            return False, "interrupted during remux"

        if rc == -2:
            return False, "ffmpeg timed out"

        if rc == 0 and partial.is_file() and partial.stat().st_size > 0:
            return True, ""

        err = _ffmpeg_err_summary(stderr, stdout)
        lower = err.lower()
        if "no space left" in lower or "enospc" in lower:
            return False, f"disk full during remux (ENOSPC): {err}"

        errors.append(f"{label}: exit={rc} {err}")

    return False, "ffmpeg stream-copy failed after retries | " + " | ".join(errors)


class ConcurrencyGate:
    def __init__(self, scan_root: Path, logger: logging.Logger) -> None:
        self.scan_root = scan_root
        self.logger = logger
        self._lock = threading.Lock()
        self._running: Dict[Path, int] = {}
        self._warned_low_space = False
        cores = os.cpu_count() or 1
        self._cpu_cap = max(1, min(MAX_CONCURRENT_JOBS, cores))

    def _free_and_total(self) -> Tuple[int, int]:
        usage = shutil.disk_usage(self.scan_root)
        return usage.free, usage.total

    def in_flight_bytes(self) -> int:
        with self._lock:
            return sum(self._running.values())

    def running_count(self) -> int:
        with self._lock:
            return len(self._running)

    def can_start(self, candidate_size: int) -> Tuple[bool, str]:
        free, total = self._free_and_total()
        margin = safety_margin_bytes(total)
        with self._lock:
            n = len(self._running)
            in_flight = sum(self._running.values())

        if n >= self._cpu_cap:
            return False, f"at CPU/cap limit ({n}/{self._cpu_cap})"

        projected = in_flight + max(0, candidate_size)
        remaining_after = free - projected
        if remaining_after >= margin:
            return True, ""

        if n < MIN_CONCURRENT_JOBS:
            if not self._warned_low_space:
                self.logger.warning(
                    "Low disk space (free=%s, margin=%s, candidate=%s). "
                    "Forcing minimum concurrency=%s rather than stalling.",
                    _human_bytes(free),
                    _human_bytes(margin),
                    _human_bytes(candidate_size),
                    MIN_CONCURRENT_JOBS,
                )
                self._warned_low_space = True
            return True, ""

        return (
            False,
            f"low disk space (free={_human_bytes(free)}, "
            f"in_flight={_human_bytes(in_flight)}, "
            f"candidate={_human_bytes(candidate_size)}, "
            f"margin={_human_bytes(margin)})",
        )

    def acquire(self, source: Path, size: int) -> None:
        with self._lock:
            self._running[source] = size

    def release(self, source: Path) -> None:
        with self._lock:
            self._running.pop(source, None)


def _human_bytes(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    val = float(n)
    for u in units:
        if abs(val) < 1024.0 or u == units[-1]:
            return f"{val:.1f} {u}"
        val /= 1024.0
    return f"{n} B"


def scan_directory(
    scan_root: Path,
    short_dir: Path,
    logger: logging.Logger,
    dup_dir: Optional[Path] = None,
) -> Tuple[List[Path], List[Path]]:
    to_convert: List[Path] = []
    already_mp4: List[Path] = []
    skip_dirs: List[Path] = [short_dir]
    skip_names = {SHORT_VIDEO_DIR_NAME, DUPLICATES_DIR_NAME}
    if dup_dir is not None:
        skip_dirs.append(dup_dir)

    for dirpath, dirnames, filenames in os.walk(scan_root):
        current = Path(dirpath)

        dirnames[:] = [
            d for d in dirnames
            if d not in skip_names
            and all((current / d).resolve() != sd.resolve() for sd in skip_dirs)
        ]

        if any(is_under_dir(current, sd) and current != scan_root for sd in skip_dirs):
            dirnames[:] = []
            continue

        for name in filenames:
            path = current / name
            if is_log_file(path):
                continue

            if name.endswith(PARTIAL_SUFFIX) or name.endswith(".partial"):
                continue

            ext = path.suffix.lower()
            if ext == MP4_EXTENSION:
                already_mp4.append(path)
            elif ext in VIDEO_EXTENSIONS:
                to_convert.append(path)

    to_convert.sort(key=lambda p: str(p).lower())
    already_mp4.sort(key=lambda p: str(p).lower())
    logger.info(
        "Scan complete: %d convertible, %d existing .mp4 (short-video pass)",
        len(to_convert),
        len(already_mp4),
    )
    return to_convert, already_mp4


def choose_final_mp4_path(source: Path) -> Path:
    base = source.with_suffix(MP4_EXTENSION)
    return unique_path(base)


def safe_unlink(path: Path, logger: logging.Logger) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError as exc:
        logger.warning("Could not remove %s: %s", path, exc)


def process_one(
    source: Path,
    scan_root: Path,
    short_dir: Path,
    logger: logging.Logger,
) -> JobResult:
    if _shutdown_event.is_set():
        return JobResult(source, False, "interrupted", "shutdown requested")

    rel = _rel(source, scan_root)
    logger.info("START  %s", rel)

    if not os.access(source, os.R_OK):
        msg = "permission denied reading source"
        logger.error("FAIL   %s — %s", rel, msg)
        return JobResult(source, False, "failed", msg)

    src_info = probe_file(source, logger)
    if not src_info.raw_ok:
        msg = f"source unreadable/corrupt: {src_info.error_message}"
        logger.error("SKIP   %s — %s", rel, msg)
        return JobResult(source, False, "failed", msg)

    final_mp4 = choose_final_mp4_path(source)
    if final_mp4 != source.with_suffix(MP4_EXTENSION):
        logger.warning(
            "Name collision for %s; using %s",
            source.with_suffix(MP4_EXTENSION).name,
            final_mp4.name,
        )

    partial = Path(str(final_mp4) + ".partial")
    if partial.exists():
        safe_unlink(partial, logger)

    parent = final_mp4.parent
    if not os.access(parent, os.W_OK):
        msg = f"permission denied writing to {parent}"
        logger.error("FAIL   %s — %s", rel, msg)
        return JobResult(source, False, "failed", msg)

    try:
        ok, err = remux_to_partial(source, partial, logger)
    except OSError as exc:
        errno = getattr(exc, "errno", None)
        if errno == 28:
            msg = f"disk full (ENOSPC): {exc}"
        else:
            msg = f"OS error during remux: {exc}"
        safe_unlink(partial, logger)
        logger.error("FAIL   %s — %s", rel, msg)
        return JobResult(source, False, "failed", msg)

    if not ok:
        safe_unlink(partial, logger)
        logger.error("FAIL   %s — %s", rel, err)
        return JobResult(source, False, "failed", err)

    if _shutdown_event.is_set():
        safe_unlink(partial, logger)
        return JobResult(source, False, "interrupted", "shutdown after remux")

    out_info = probe_file(partial, logger)
    s1_ok, s1_reason = stage1_ok(src_info, out_info)
    verified = False
    verify_detail = ""

    if s1_ok:
        verified = True
        verify_detail = "stage1 ffprobe ok"
        logger.info("VERIFY %s — stage1 passed", rel)
    else:
        logger.warning(
            "VERIFY %s — stage1 suspicious (%s); running full decode check",
            rel,
            s1_reason,
        )
        d_ok, d_reason = full_decode_check(partial, logger)
        if d_ok and out_info.raw_ok and out_info.total_streams > 0:
            verified = True
            verify_detail = f"stage2 decode ok (stage1: {s1_reason})"
            logger.info("VERIFY %s — stage2 passed", rel)
        elif d_ok:
            verify_detail = (
                f"stage2 decode reported clean but metadata unusable: {s1_reason}"
            )
        else:
            verify_detail = f"stage1: {s1_reason}; stage2: {d_reason}"

    if not verified:
        safe_unlink(partial, logger)
        msg = f"verification failed: {verify_detail}"
        logger.error("FAIL   %s — %s", rel, msg)
        return JobResult(source, False, "failed", msg)

    if _shutdown_event.is_set():
        safe_unlink(partial, logger)
        return JobResult(source, False, "interrupted", "shutdown after verify")

    try:
        os.replace(str(partial), str(final_mp4))
    except OSError as exc:
        safe_unlink(partial, logger)
        msg = f"could not finalize output: {exc}"
        logger.error("FAIL   %s — %s", rel, msg)
        return JobResult(source, False, "failed", msg)

    try:
        source.unlink()
    except OSError as exc:
        msg = (
            f"converted OK but could not delete original: {exc} "
            f"(output kept at {final_mp4.name})"
        )
        logger.error("FAIL   %s — %s", rel, msg)
        return JobResult(
            source,
            False,
            "failed",
            msg,
            output=final_mp4,
            duration=out_info.duration if out_info.duration is not None else src_info.duration,
        )

    duration = out_info.duration if out_info.duration is not None else src_info.duration
    logger.info(
        "DONE   %s -> %s (%s)",
        rel,
        final_mp4.name,
        verify_detail,
    )

    moved = False
    final_path = final_mp4
    if duration is not None and duration < SHORT_VIDEO_THRESHOLD_SEC:
        final_path, moved = move_to_short(final_mp4, short_dir, scan_root, logger)

    return JobResult(
        source,
        True,
        "converted",
        verify_detail,
        output=final_path,
        moved_to_short=moved,
        duration=duration,
    )


def move_to_short(
    mp4_path: Path,
    short_dir: Path,
    scan_root: Path,
    logger: logging.Logger,
) -> Tuple[Path, bool]:
    try:
        short_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Cannot create short-video dir %s: %s", short_dir, exc)
        return mp4_path, False

    if is_under_dir(mp4_path, short_dir):
        return mp4_path, False

    dest = unique_short_path(short_dir, mp4_path, scan_root)
    try:
        shutil.move(str(mp4_path), str(dest))
        logger.info(
            "MOVED  %s -> %s/ (duration < %.0fs)",
            _rel(mp4_path, scan_root),
            SHORT_VIDEO_DIR_NAME,
            SHORT_VIDEO_THRESHOLD_SEC,
        )
        return dest, True
    except OSError as exc:
        logger.warning("Could not move %s to short folder: %s", mp4_path, exc)
        return mp4_path, False


def process_existing_mp4(
    mp4_path: Path,
    scan_root: Path,
    short_dir: Path,
    logger: logging.Logger,
) -> JobResult:
    if _shutdown_event.is_set():
        return JobResult(mp4_path, True, "skipped_mp4", "interrupted")

    if is_under_dir(mp4_path, short_dir):
        return JobResult(mp4_path, True, "skipped_mp4", "already in short folder")

    info = probe_file(mp4_path, logger)
    if not info.raw_ok:
        logger.warning(
            "SKIP   existing mp4 unreadable: %s — %s",
            _rel(mp4_path, scan_root),
            info.error_message,
        )
        return JobResult(
            mp4_path,
            True,
            "skipped_mp4",
            f"unreadable: {info.error_message}",
        )

    moved = False
    out = mp4_path
    if info.duration is not None and info.duration < SHORT_VIDEO_THRESHOLD_SEC:
        out, moved = move_to_short(mp4_path, short_dir, scan_root, logger)

    return JobResult(
        mp4_path,
        True,
        "skipped_mp4",
        "already mp4",
        output=out,
        moved_to_short=moved,
        duration=info.duration,
    )


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def run_conversions(
    files: List[Path],
    scan_root: Path,
    short_dir: Path,
    logger: logging.Logger,
    stats: RunStats,
) -> None:
    if not files:
        return

    gate = ConcurrencyGate(scan_root, logger)
    logger.info(
        "Conversion pool: hard cap=%d, CPU-based cap=%d, safety margin=max(%.0f%% FS, %s)",
        MAX_CONCURRENT_JOBS,
        gate._cpu_cap,
        DISK_SAFETY_PERCENT * 100,
        _human_bytes(DISK_SAFETY_MIN_BYTES),
    )

    sizes: Dict[Path, int] = {}
    for p in files:
        try:
            sizes[p] = p.stat().st_size
        except OSError:
            sizes[p] = 0
            logger.warning("Could not stat %s; size estimate 0", p)

    pending = list(files)
    futures: Dict[Future, Path] = {}

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS) as pool:
        try:
            while pending or futures:
                if _shutdown_event.is_set():
                    break

                progressed = False
                still_pending: List[Path] = []
                for src in pending:
                    if _shutdown_event.is_set():
                        still_pending.append(src)
                        continue
                    if gate.running_count() >= gate._cpu_cap:
                        still_pending.append(src)
                        continue
                    ok, reason = gate.can_start(sizes.get(src, 0))
                    if not ok:
                        still_pending.append(src)
                        continue
                    gate.acquire(src, sizes.get(src, 0))
                    fut = pool.submit(
                        process_one, src, scan_root, short_dir, logger
                    )
                    futures[fut] = src
                    progressed = True

                pending = still_pending

                if not futures:
                    if pending and not _shutdown_event.is_set():
                        time.sleep(DISPATCH_POLL_INTERVAL_SEC)
                        src = pending.pop(0)
                        gate.acquire(src, sizes.get(src, 0))
                        fut = pool.submit(
                            process_one, src, scan_root, short_dir, logger
                        )
                        futures[fut] = src
                    else:
                        break
                    continue

                done, _ = wait(
                    list(futures.keys()),
                    timeout=DISPATCH_POLL_INTERVAL_SEC,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    if not progressed and pending:
                        pass
                    continue

                for fut in done:
                    src = futures.pop(fut)
                    gate.release(src)
                    try:
                        result: JobResult = fut.result()
                    except Exception as exc:
                        logger.exception("Unhandled error for %s: %s", src, exc)
                        result = JobResult(
                            src, False, "failed", f"unhandled: {exc}"
                        )
                    _apply_result(result, stats, logger)

        except KeyboardInterrupt:
            logger.warning("KeyboardInterrupt in dispatcher — shutting down…")
            _shutdown_event.set()
            _terminate_all_procs()
            stats.interrupted = True
            for fut, src in list(futures.items()):
                fut.cancel()
                gate.release(src)
            wait(list(futures.keys()), timeout=10)
            for fut, src in list(futures.items()):
                if fut.done():
                    try:
                        result = fut.result()
                        _apply_result(result, stats, logger)
                    except Exception:
                        pass
                futures.pop(fut, None)


def run_short_pass(
    mp4_files: List[Path],
    scan_root: Path,
    short_dir: Path,
    logger: logging.Logger,
    stats: RunStats,
) -> None:
    if not mp4_files or _shutdown_event.is_set():
        return
    logger.info("Short-video pass on %d existing .mp4 file(s)…", len(mp4_files))
    for path in mp4_files:
        if _shutdown_event.is_set():
            break
        try:
            result = process_existing_mp4(path, scan_root, short_dir, logger)
            if result.moved_to_short:
                stats.moved_short += 1
        except Exception as exc:
            logger.exception("Short-pass error for %s: %s", path, exc)


def _apply_result(result: JobResult, stats: RunStats, logger: logging.Logger) -> None:
    if result.action == "converted" and result.success:
        stats.converted += 1
        if result.moved_to_short:
            stats.moved_short += 1
    elif result.action == "failed" or not result.success:
        if result.action == "interrupted":
            stats.interrupted = True
            stats.failed.append((str(result.source), result.message or "interrupted"))
        else:
            stats.failed.append((str(result.source), result.message or "unknown error"))
    elif result.action == "interrupted":
        stats.interrupted = True


def _hamming_distance(a: int, b: int) -> int:
    x = a ^ b
    try:
        return x.bit_count()
    except AttributeError:
        return bin(x).count("1")


def _compute_ahash(pixels: bytes) -> int:
    n = len(pixels)
    avg = sum(pixels) / n
    h = 0
    for i, p in enumerate(pixels):
        if p > avg:
            h |= 1 << i
    return h


def extract_gray_frame(
    path: Path,
    timestamp_sec: float,
    logger: logging.Logger,
) -> Optional[bytes]:
    vf = (
        f"scale={AHASH_WIDTH}:{AHASH_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={AHASH_WIDTH}:{AHASH_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"format=gray"
    )
    ss = max(0.0, float(timestamp_sec))
    args = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{ss:.3f}",
        "-i", str(path),
        "-vframes", "1",
        "-vf", vf,
        "-f", "rawvideo",
        "pipe:1",
    ]
    rc, raw, err = run_command_binary(args, FRAME_EXTRACT_TIMEOUT_SEC, logger)
    if rc != 0:
        logger.debug(
            "Frame extract failed for %s @ %.3fs (exit %s): %s",
            path,
            ss,
            rc,
            _ffmpeg_err_summary(err),
        )
        return None
    if len(raw) != AHASH_PIXELS:
        logger.debug(
            "Frame extract size mismatch for %s @ %.3fs: got %d bytes, want %d",
            path,
            ss,
            len(raw),
            AHASH_PIXELS,
        )
        return None
    return raw


def compute_video_ahash(
    path: Path,
    duration: float,
    logger: logging.Logger,
) -> Optional[int]:
    if duration <= 0:
        return None
    fractions = (0.50, 0.25, 0.75)
    for frac in fractions:
        if _shutdown_event.is_set():
            return None
        ts = duration * frac
        if duration > 1.0:
            ts = min(ts, max(0.0, duration - 0.05))
        raw = extract_gray_frame(path, ts, logger)
        if raw is not None:
            return _compute_ahash(raw)
    return None


def probe_media_meta(path: Path, logger: logging.Logger) -> Optional[MediaMeta]:
    args = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "default=noprint_wrappers=1",
        str(path),
    ]
    rc, stdout, stderr = run_command(args, FFPROBE_TIMEOUT_SEC, logger)
    if rc != 0:
        logger.warning(
            "DUP skip probe failed: %s — %s",
            path,
            (stderr or stdout or f"exit {rc}").strip()[:300],
        )
        return None

    duration: Optional[float] = None
    width = 0
    height = 0
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("duration="):
            val = line.split("=", 1)[1].strip()
            if val and val.lower() != "n/a":
                try:
                    duration = float(val)
                except ValueError:
                    duration = None
        elif line.startswith("width="):
            try:
                width = int(line.split("=", 1)[1].strip())
            except ValueError:
                width = 0
        elif line.startswith("height="):
            try:
                height = int(line.split("=", 1)[1].strip())
            except ValueError:
                height = 0

    if duration is None or duration < 0:
        logger.warning("DUP skip no duration: %s", path)
        return None

    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        logger.warning("DUP skip stat failed: %s — %s", path, exc)
        return None

    return MediaMeta(
        path=path,
        duration=duration,
        width=max(0, width),
        height=max(0, height),
        size_bytes=size_bytes,
    )


def _quality_key(meta: MediaMeta) -> Tuple[int, int]:
    return (meta.width * meta.height, meta.size_bytes)


def scan_final_mp4s(
    scan_root: Path,
    dup_dir: Path,
    logger: logging.Logger,
) -> List[Path]:
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(scan_root):
        current = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if d != DUPLICATES_DIR_NAME
            and (current / d).resolve() != dup_dir.resolve()
        ]
        if is_under_dir(current, dup_dir) and current != scan_root:
            dirnames[:] = []
            continue

        for name in filenames:
            path = current / name
            if is_log_file(path):
                continue
            if name.endswith(PARTIAL_SUFFIX) or name.endswith(".partial"):
                continue
            if path.suffix.lower() == MP4_EXTENSION:
                found.append(path)

    found.sort(key=lambda p: str(p).lower())
    logger.info("Duplicate scan: found %d final .mp4 file(s)", len(found))
    return found


def _duration_groups(metas: List[MediaMeta]) -> List[List[MediaMeta]]:
    n = len(metas)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    order = sorted(range(n), key=lambda i: metas[i].duration)
    for a_idx in range(n):
        i = order[a_idx]
        for b_idx in range(a_idx + 1, n):
            j = order[b_idx]
            if metas[j].duration - metas[i].duration > DUP_DURATION_TOLERANCE_SEC:
                break
            if abs(metas[i].duration - metas[j].duration) <= DUP_DURATION_TOLERANCE_SEC:
                union(i, j)

    buckets: Dict[int, List[MediaMeta]] = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(metas[i])
    return [g for g in buckets.values() if len(g) >= 2]


def run_duplicate_scan(
    scan_root: Path,
    dup_dir: Path,
    logger: logging.Logger,
    stats: RunStats,
) -> None:
    if _shutdown_event.is_set():
        return

    logger.info("Starting perceptual duplicate scan (aHash)…")
    logger.info(
        "Duplicate rules: duration within ±%.1fs, aHash %dx%d, Hamming < %d",
        DUP_DURATION_TOLERANCE_SEC,
        AHASH_WIDTH,
        AHASH_HEIGHT,
        AHASH_HAMMING_THRESHOLD,
    )

    mp4s = scan_final_mp4s(scan_root, dup_dir, logger)
    if len(mp4s) < 2:
        logger.info("Duplicate scan: fewer than 2 files — nothing to compare")
        return

    metas: List[MediaMeta] = []
    for path in mp4s:
        if _shutdown_event.is_set():
            return
        meta = probe_media_meta(path, logger)
        if meta is None:
            stats.hash_skipped.append((str(path), "ffprobe duration/resolution failed"))
            continue
        metas.append(meta)

    if len(metas) < 2:
        logger.info("Duplicate scan: fewer than 2 probeable files")
        return

    groups = _duration_groups(metas)
    logger.info(
        "Duplicate scan: %d duration group(s) with 2+ candidates",
        len(groups),
    )

    for group in groups:
        if _shutdown_event.is_set():
            return

        hashed: List[MediaMeta] = []
        for meta in group:
            if _shutdown_event.is_set():
                return
            ah = compute_video_ahash(meta.path, meta.duration, logger)
            if ah is None:
                msg = "frame extract/hash failed (50%/25%/75%)"
                logger.warning("DUP hash skip: %s — %s", _rel(meta.path, scan_root), msg)
                stats.hash_skipped.append((str(meta.path), msg))
                continue
            meta.ahash = ah
            hashed.append(meta)

        if len(hashed) < 2:
            continue

        m = len(hashed)
        parent = list(range(m))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri

        for i in range(m):
            for j in range(i + 1, m):
                hi = hashed[i].ahash
                hj = hashed[j].ahash
                if hi is None or hj is None:
                    continue
                dist = _hamming_distance(hi, hj)
                if dist < AHASH_HAMMING_THRESHOLD:
                    union(i, j)
                    stats.duplicates_found += 1
                    logger.debug(
                        "aHash near-match: %s ~ %s (Hamming %d)",
                        hashed[i].path.name,
                        hashed[j].path.name,
                        dist,
                    )

        clusters: Dict[int, List[MediaMeta]] = {}
        for i in range(m):
            clusters.setdefault(find(i), []).append(hashed[i])

        for cluster in clusters.values():
            if len(cluster) < 2:
                continue
            if _shutdown_event.is_set():
                return

            cluster_sorted = sorted(cluster, key=_quality_key, reverse=True)
            keeper = cluster_sorted[0]
            losers = cluster_sorted[1:]

            logger.info(
                "Duplicate cluster: keeping %s (%dx%d, %s)",
                _rel(keeper.path, scan_root),
                keeper.width,
                keeper.height,
                _human_bytes(keeper.size_bytes),
            )

            try:
                dup_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.error("Cannot create duplicates dir %s: %s", dup_dir, exc)
                return

            for loser in losers:
                if _shutdown_event.is_set():
                    return
                if not loser.path.is_file():
                    continue
                if is_under_dir(loser.path, dup_dir):
                    continue

                keeper_rel = _rel(keeper.path, scan_root)
                loser_rel = _rel(loser.path, scan_root)
                dist = (
                    _hamming_distance(keeper.ahash, loser.ahash)
                    if keeper.ahash is not None and loser.ahash is not None
                    else -1
                )
                dest = unique_moved_path(dup_dir, loser.path, scan_root)
                try:
                    shutil.move(str(loser.path), str(dest))
                    stats.duplicates_moved += 1
                    logger.info(
                        "Duplicate found: %s and %s (Hamming distance: %d). "
                        "Moving %s to %s/.",
                        keeper_rel,
                        loser_rel,
                        dist,
                        loser_rel,
                        DUPLICATES_DIR_NAME,
                    )
                except OSError as exc:
                    logger.warning(
                        "Could not move duplicate %s -> %s: %s",
                        loser_rel,
                        dest,
                        exc,
                    )

    logger.info(
        "Duplicate scan complete: pairs_logged=%d, moved=%d, hash_skipped=%d",
        stats.duplicates_found,
        stats.duplicates_moved,
        len(stats.hash_skipped),
    )


def print_summary(
    stats: RunStats,
    logger: logging.Logger,
    log_path: Path,
) -> None:
    lines = [
        "",
        "=" * 60,
        "SUMMARY REPORT",
        "=" * 60,
        f"Convertible files scanned : {stats.scanned_convertible}",
        f"Already .mp4 (short pass) : {stats.already_mp4}",
        f"Successfully converted    : {stats.converted}",
        f"Failed                    : {len(stats.failed)}",
        f"Moved to short_videos/    : {stats.moved_short}",
        f"Duplicates found (pairs)  : {stats.duplicates_found}",
        f"Moved to duplicates/      : {stats.duplicates_moved}",
        f"Hash skipped (errors)     : {len(stats.hash_skipped)}",
    ]
    if stats.interrupted:
        lines.append("Run status                : INTERRUPTED (Ctrl+C)")
    else:
        lines.append("Run status                : complete")
    lines.append(f"Log file                  : {log_path}")

    if stats.failed:
        lines.append("")
        lines.append("Failures:")
        for path, reason in stats.failed:
            lines.append(f"  - {path}")
            lines.append(f"      reason: {reason}")

    if stats.hash_skipped:
        lines.append("")
        lines.append("Hash skipped:")
        for path, reason in stats.hash_skipped:
            lines.append(f"  - {path}")
            lines.append(f"      reason: {reason}")

    lines.append("=" * 60)
    text = "\n".join(lines)
    for line in lines:
        if line:
            logger.info(line)
        else:
            logger.info("")
    print(text)


def cleanup_partials_under(
    scan_root: Path,
    short_dir: Path,
    logger: logging.Logger,
    dup_dir: Optional[Path] = None,
) -> None:
    removed = 0
    skip_names = {SHORT_VIDEO_DIR_NAME, DUPLICATES_DIR_NAME}
    skip_dirs = [short_dir]
    if dup_dir is not None:
        skip_dirs.append(dup_dir)

    for dirpath, dirnames, filenames in os.walk(scan_root):
        current = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if d not in skip_names
            and all((current / d).resolve() != sd.resolve() for sd in skip_dirs)
        ]
        for name in filenames:
            if name.endswith(PARTIAL_SUFFIX) or name.endswith(".partial"):
                path = current / name
                if path.name.endswith(PARTIAL_SUFFIX) or path.suffix == ".partial":
                    try:
                        path.unlink()
                        removed += 1
                        logger.info("Cleaned partial: %s", path)
                    except OSError as exc:
                        logger.warning("Could not clean partial %s: %s", path, exc)
    if removed:
        logger.info("Removed %d partial output file(s)", removed)


def main() -> int:
    scan_root = Path(os.getcwd()).resolve()
    short_dir = (scan_root / SHORT_VIDEO_DIR_NAME).resolve()
    dup_dir = (scan_root / DUPLICATES_DIR_NAME).resolve()

    logger, log_path = setup_logging(scan_root)
    logger.info("Bulk Video-to-MP4 Remux Converter")
    logger.info("Script build: 2026-07-15-r4 (perceptual duplicate aHash pass)")
    logger.info("Scan root: %s", scan_root)
    logger.info("Short-video folder: %s", short_dir)
    logger.info("Duplicates folder: %s", dup_dir)
    logger.info("Log file: %s", log_path)

    require_binaries(logger)

    stats = RunStats()

    def _sigint_handler(signum, frame):
        if not _shutdown_event.is_set():
            logger.warning("Ctrl+C received — stopping new work, terminating ffmpeg…")
            _shutdown_event.set()
            _terminate_all_procs()
            stats.interrupted = True
        else:
            logger.error("Second interrupt — forcing exit")
            _terminate_all_procs()
            raise SystemExit(130)

    previous_handler = signal.signal(signal.SIGINT, _sigint_handler)

    try:
        to_convert, already_mp4 = scan_directory(
            scan_root, short_dir, logger, dup_dir=dup_dir
        )
        stats.scanned_convertible = len(to_convert)
        stats.already_mp4 = len(already_mp4)

        if not to_convert and not already_mp4:
            logger.info("No convertible/non-short-scan videos found; still checking finals…")

        if to_convert:
            run_conversions(to_convert, scan_root, short_dir, logger, stats)

        if not _shutdown_event.is_set():
            remaining_mp4 = [p for p in already_mp4 if p.is_file()]
            run_short_pass(remaining_mp4, scan_root, short_dir, logger, stats)

        if not _shutdown_event.is_set():
            try:
                run_duplicate_scan(scan_root, dup_dir, logger, stats)
            except Exception as exc:
                logger.exception("Duplicate scan failed (conversion summary still printed): %s", exc)

    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt at top level")
        _shutdown_event.set()
        _terminate_all_procs()
        stats.interrupted = True
    finally:
        try:
            cleanup_partials_under(scan_root, short_dir, logger, dup_dir=dup_dir)
        except Exception as exc:
            logger.warning("Partial cleanup error: %s", exc)
        try:
            signal.signal(signal.SIGINT, previous_handler)
        except Exception:
            pass

    print_summary(stats, logger, log_path)
    return 130 if stats.interrupted else (1 if stats.failed else 0)


if __name__ == "__main__":
    sys.exit(main())
