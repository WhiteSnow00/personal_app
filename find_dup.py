from __future__ import annotations
import csv
import hashlib
import html
import json
import math
import os
import pickle
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
import numpy as np

_POPCOUNT_LUT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
TRASH_LOW_RES_DIRNAME = "_TRASH_LOW_RES"
GROUP_DIR_PREFIX = "Group_"
CACHE_DB_FILENAME = "video_cache.sqlite3"
FAST_SIG_CHUNK_BYTES = 64 * 1024

class Thresholds:

    MIN_RESOLUTION_PX: int = 480
    PHASH_BITS: int = 64
    DURATION_TOLERANCE_S: float = 1.0
    MTIME_TOLERANCE_S: float = 2.0
    PROGRESS_INTERVAL: int = 25
    COARSE_STEP: int = 5
    COARSE_THRESHOLD: int = 200

    DURATION_PADDING_S: float = 60.0
    DURATION_PADDING_RATIO: float = 0.12
    FRAME_SIZE: int = 32

    CACHE_SCHEMA_VERSION: int = 2

try:
    import imagehash
except Exception:
    imagehash = None

def _timestamp() -> str:
    return time.strftime("%H:%M:%S")


def _noop(*_args, **_kwargs) -> None:
    return None


def _human_readable_size(size_bytes: int) -> str:
    try:
        size = float(max(0, int(size_bytes)))
    except Exception:
        return "N/A"

    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    unit_index = 0
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1

    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.2f} {units[unit_index]}"


def _format_duration(seconds: float) -> str:
    try:
        total_seconds = int(round(float(seconds)))
    except Exception:
        return "N/A"

    if total_seconds < 0:
        total_seconds = 0

    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _format_eta(seconds: float | int | None) -> str:
    if seconds is None:
        return "ETA: --"
    try:
        remaining = max(0, int(round(float(seconds))))
    except Exception:
        return "ETA: --"

    hours, remainder = divmod(remaining, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"ETA: ~{hours}h {minutes:02d}m"
    if minutes > 0:
        return f"ETA: ~{minutes}m {secs:02d}s"
    return f"ETA: ~{secs}s"


def _safe_resolve_path(path: str | Path, *, strict: bool = False) -> Path:
    path_obj = Path(path).expanduser()
    try:
        return path_obj.resolve(strict=strict)
    except TypeError:
        pass
    except Exception:
        pass
    try:
        return Path(os.path.abspath(str(path_obj)))
    except Exception:
        return path_obj

def _safe_exists(path: str | Path) -> bool:
    try:
        return Path(path).exists()
    except OSError:
        return False

def _safe_is_file(path: str | Path) -> bool:
    try:
        return Path(path).is_file()
    except OSError:
        return False

def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFC", value)

def _path_sort_key(path: Path) -> str:
    return _normalized_text(str(path)).casefold()

def _make_cache_identity(root_dir: str | Path, video_path: str | Path) -> tuple[str, str]:
    root_path = _safe_resolve_path(root_dir, strict=False)
    target_path = _safe_resolve_path(video_path, strict=False)
    try:
        rel_path = target_path.relative_to(root_path)
        rel_text = _normalized_text(rel_path.as_posix())
        return rel_text, rel_text
    except Exception:
        abs_text = _normalized_text(target_path.as_posix())
        return f"ABS::{abs_text}", abs_text

def _fast_file_signature(
    path: str | Path,
    file_size: int,
    *,
    chunk_size: int = FAST_SIG_CHUNK_BYTES,
) -> Optional[str]:
    if file_size < 0:
        return None

    file_path = Path(path)
    hasher = hashlib.blake2b(digest_size=16)
    hasher.update(int(file_size).to_bytes(8, byteorder="little", signed=False))

    positions = [0]
    if file_size > chunk_size:
        tail = max(0, file_size - chunk_size)
        if tail not in positions:
            positions.append(tail)
    if file_size > (chunk_size * 2):
        middle = max(0, (file_size // 2) - (chunk_size // 2))
        if middle not in positions:
            positions.append(middle)

    try:
        with file_path.open("rb", buffering=0) as handle:
            for position in positions:
                handle.seek(position)
                chunk = handle.read(chunk_size)
                hasher.update(int(position).to_bytes(8, byteorder="little", signed=False))
                hasher.update(chunk)
    except OSError:
        return None

    return hasher.hexdigest()

def _launch_detached(args: list[str]) -> bool:
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen(args, **kwargs)
        return True
    except OSError:
        return False

def _run_quiet(args: list[str], *, timeout_s: int = 5) -> bool:
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": timeout_s,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = subprocess.run(args, **kwargs)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

def reveal_in_file_manager(path: str | Path) -> tuple[bool, str]:
    target = _safe_resolve_path(path, strict=False)
    if not _safe_exists(target):
        return False, f"Path does not exist: {target}"

    try:
        is_file = target.is_file()
    except OSError as exc:
        return False, f"Path is inaccessible: {exc}"

    if os.name == "nt":
        explorer_target = os.path.normpath(str(target))
        args = ["explorer.exe"]
        if is_file:

            args.extend(["/select,", explorer_target])
        else:
            args.append(explorer_target)
        return (_launch_detached(args), "Windows Explorer")

    if sys.platform == "darwin":
        if is_file:
            return (_launch_detached(["open", "-R", str(target)]), "Finder")
        return (_launch_detached(["open", str(target)]), "Finder")

    if is_file:
        uri = target.as_uri()
        if shutil.which("gdbus") and _run_quiet(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.FileManager1",
                "--object-path",
                "/org/freedesktop/FileManager1",
                "--method",
                "org.freedesktop.FileManager1.ShowItems",
                f"['{uri}']",
                "",
            ]
        ):
            return True, "org.freedesktop.FileManager1"

        if shutil.which("dbus-send") and _run_quiet(
            [
                "dbus-send",
                "--session",
                "--print-reply=literal",
                "--dest=org.freedesktop.FileManager1",
                "--type=method_call",
                "/org/freedesktop/FileManager1",
                "org.freedesktop.FileManager1.ShowItems",
                f"array:string:{uri}",
                "string:",
            ]
        ):
            return True, "org.freedesktop.FileManager1"

        file_manager_commands: list[list[str]] = []
        if shutil.which("nautilus"):
            file_manager_commands.append(["nautilus", "--new-window", "--select", str(target)])
        if shutil.which("dolphin"):
            file_manager_commands.append(["dolphin", "--new-window", "--select", str(target)])
        if shutil.which("thunar"):

            file_manager_commands.append(["thunar", str(target)])

        for command in file_manager_commands:
            if _launch_detached(command):
                return True, Path(command[0]).name

    open_target = target.parent if is_file else target
    for command in (["xdg-open", str(open_target)], ["gio", "open", str(open_target)]):
        if shutil.which(command[0]) and _launch_detached(command):
            return True, command[0]

    return False, "No supported file-manager reveal mechanism found."

def _effective_duplicate_similarity(base_threshold: float, overlap_frames: int) -> float:
    if overlap_frames <= 0:
        return 1.0
    if overlap_frames < 3:
        return max(base_threshold, 0.995)
    if overlap_frames < 5:
        return max(base_threshold, 0.97)
    if overlap_frames < 10:
        return max(base_threshold, 0.92)
    return base_threshold


def _duration_compare_limit_s(duration_a: float, duration_b: float) -> float:
    longer = max(float(duration_a), float(duration_b), 0.0)
    return max(float(Thresholds.DURATION_PADDING_S), longer * float(Thresholds.DURATION_PADDING_RATIO))


def _durations_comparable(duration_a: float, duration_b: float) -> bool:
    return abs(float(duration_a) - float(duration_b)) <= _duration_compare_limit_s(duration_a, duration_b)


def _effective_sample_fps(duration_s: float, sample_fps: float, max_frames: int) -> float:
    duration_s = max(float(duration_s), 1e-6)
    sample_fps = max(float(sample_fps), 1e-6)
    max_frames = max(int(max_frames), 1)
    span_fps = max_frames / duration_s
    return min(sample_fps, span_fps)


def _prefer_as_original(a: "VideoFingerprint", b: "VideoFingerprint") -> tuple["VideoFingerprint", "VideoFingerprint"]:
    area_a = max(0, int(a.width)) * max(0, int(a.height))
    area_b = max(0, int(b.width)) * max(0, int(b.height))
    if area_a != area_b:
        return (a, b) if area_a >= area_b else (b, a)
    if int(a.file_size) != int(b.file_size):
        return (a, b) if int(a.file_size) >= int(b.file_size) else (b, a)

    if _normalized_text(a.path).casefold() <= _normalized_text(b.path).casefold():
        return a, b
    return b, a


def _is_better_alignment(
    similarity: float,
    overlap_frames: int,
    offset_frames: int,
    best_similarity: float,
    best_overlap_frames: int,
    best_offset_frames: int,
) -> bool:
    epsilon = 1e-12
    if similarity > (best_similarity + epsilon):
        return True
    if abs(similarity - best_similarity) > epsilon:
        return False
    if overlap_frames > best_overlap_frames:
        return True
    if overlap_frames < best_overlap_frames:
        return False
    if abs(offset_frames) < abs(best_offset_frames):
        return True
    if abs(offset_frames) > abs(best_offset_frames):
        return False
    return offset_frames < best_offset_frames


def _prefilter_hamming_radius(base_similarity: float, frame_count: int, *, min_radius: int = 14) -> int:
    radius = int(round((1.0 - float(base_similarity)) * float(Thresholds.PHASH_BITS)))
    radius = max(0, min(Thresholds.PHASH_BITS, radius))

    radius = max(radius, int(min_radius))
    if frame_count < 30:
        radius = min(Thresholds.PHASH_BITS, radius + 4)
    elif frame_count < 90:
        radius = min(Thresholds.PHASH_BITS, radius + 2)
    return radius


@dataclass(frozen=True, slots=True)
class FFmpegBinaries:
    ffmpeg_path: str
    ffprobe_path: str

class FFmpegLocator:
    @staticmethod
    def _candidate_dirs() -> list[Path]:
        cwd = _safe_resolve_path(os.getcwd(), strict=False)
        script_dir = _safe_resolve_path(Path(__file__).parent, strict=False)
        return [cwd, cwd / "bin", script_dir, script_dir / "bin"]

    @staticmethod
    def _binary_name(stem: str) -> str:
        return f"{stem}.exe" if os.name == "nt" else stem

    @staticmethod
    def _is_usable_binary(path: Path) -> bool:
        if not path.is_file():
            return False
        if os.name == "nt":
            return True
        return os.access(str(path), os.X_OK)

    @classmethod
    def locate_or_raise(cls) -> FFmpegBinaries:
        ffmpeg_name = cls._binary_name("ffmpeg")
        ffprobe_name = cls._binary_name("ffprobe")

        for base_dir in cls._candidate_dirs():
            ffmpeg_path = base_dir / ffmpeg_name
            ffprobe_path = base_dir / ffprobe_name
            if cls._is_usable_binary(ffmpeg_path) and cls._is_usable_binary(ffprobe_path):
                return FFmpegBinaries(str(_safe_resolve_path(ffmpeg_path, strict=False)), str(_safe_resolve_path(ffprobe_path, strict=False)))

        ffmpeg_path = shutil.which(ffmpeg_name) or shutil.which("ffmpeg")
        ffprobe_path = shutil.which(ffprobe_name) or shutil.which("ffprobe")
        if ffmpeg_path and ffprobe_path:
            return FFmpegBinaries(str(_safe_resolve_path(ffmpeg_path, strict=False)), str(_safe_resolve_path(ffprobe_path, strict=False)))

        candidates = "\n".join(f"- {p}" for p in cls._candidate_dirs())
        raise FileNotFoundError(
            "FFmpeg binaries not found.\n"
            f"Looked for `{ffmpeg_name}` and `{ffprobe_name}` next to the app, in ./bin, and in PATH.\n"
            f"Bundled search locations:\n{candidates}"
        )

class VideoScanner:
    def __init__(self, on_log: Callable[[str], None] = _noop):
        self._on_log = on_log

    def scan(self, root_dir: str | Path) -> list[Path]:
        root_path = _safe_resolve_path(root_dir, strict=False)
        if not _safe_exists(root_path):
            self._on_log(f"[ERROR] {root_path} does not exist.")
            return []
        try:
            if not root_path.is_dir():
                self._on_log(f"[ERROR] {root_path} is not a directory.")
                return []
        except OSError as exc:
            self._on_log(f"[ERROR] Could not access {root_path}: {exc}")
            return []

        video_paths: list[Path] = []
        trash_lower = TRASH_LOW_RES_DIRNAME.lower()
        group_lower = GROUP_DIR_PREFIX.lower()

        def _walk_error(exc: OSError) -> None:
            self._on_log(f"[WARN] {_timestamp()} Directory walk skipped: {exc}")

        for dirpath, dirnames, filenames in os.walk(root_path, onerror=_walk_error):
            dirnames[:] = [
                d
                for d in dirnames
                if d.lower() != trash_lower and not d.lower().startswith(group_lower)
            ]
            for filename in filenames:
                suffix = Path(filename).suffix.lower()
                if suffix not in VIDEO_EXTENSIONS:
                    continue
                try:
                    video_paths.append(_safe_resolve_path(Path(dirpath) / filename, strict=False))
                except Exception as exc:
                    self._on_log(f"[WARN] {_timestamp()} Skipping path {filename!r}: {exc}")

        video_paths.sort(key=_path_sort_key)
        self._on_log(f"[INFO] Found {len(video_paths)} video(s) under {root_path}.")
        return video_paths

@dataclass(frozen=True, slots=True)
class VideoMetadata:
    duration_s: float
    width: int
    height: int

class FFprobeValidator:
    def __init__(self, ffprobe_path: str, timeout_s: int = 30):
        self._ffprobe_path = ffprobe_path
        self._timeout_s = timeout_s

    def validate(self, video_path: str | Path) -> tuple[Optional[VideoMetadata], Optional[str]]:
        path_str = str(video_path)
        if not _safe_is_file(path_str):
            return None, "Not a file."

        run_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "timeout": self._timeout_s,
        }
        if os.name == "nt":
            run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        cmd = [
            self._ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=width,height,duration",
            "-of",
            "json",
            path_str,
        ]
        try:
            proc = subprocess.run(cmd, **run_kwargs)
        except subprocess.TimeoutExpired:
            return None, "ffprobe timeout."
        except subprocess.SubprocessError as exc:
            return None, f"ffprobe subprocess error: {exc}"
        except OSError as exc:
            return None, f"File access error: {exc}"
        except Exception as exc:
            return None, f"Unexpected error: {type(exc).__name__}: {exc}"

        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            return None, err or f"ffprobe return code {proc.returncode}"
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            return None, f"Invalid ffprobe JSON output: {exc}"

        streams = payload.get("streams") or []
        if not streams:
            return None, "No video stream."

        stream0 = streams[0] or {}
        width = int(stream0.get("width") or 0)
        height = int(stream0.get("height") or 0)

        duration_candidates = [
            ((payload.get("format") or {}).get("duration") or "").strip(),
            str(stream0.get("duration") or "").strip(),
        ]
        duration_s = 0.0
        for duration_str in duration_candidates:
            if not duration_str:
                continue
            try:
                duration_s = float(duration_str)
            except Exception:
                continue
            if math.isfinite(duration_s) and duration_s > 0:
                break
        else:
            duration_s = 0.0

        if width <= 0 or height <= 0:
            return None, "Invalid resolution."
        if not math.isfinite(duration_s) or duration_s <= 0:
            return None, "Invalid duration."

        return VideoMetadata(duration_s=duration_s, width=width, height=height), None

@dataclass(frozen=True, slots=True)
class FingerprintConfig:
    sample_fps: float = 1.0
    frame_size: int = Thresholds.FRAME_SIZE
    max_frames: int = 1800
    ffmpeg_timeout_s: int = 0
    ffprobe_timeout_s: int = 30

@dataclass(frozen=True, slots=True)
class MatchConfig:
    duplicate_similarity: float = 0.85
    consensus_prefilter_similarity: float = 0.75
    consensus_min_hamming_radius: int = 14
    max_offset_s: int = 600
    min_overlap_frames: int = 10
    require_comparable_duration: bool = True


@dataclass(frozen=True, slots=True)
class EngineConfig:
    fingerprint: FingerprintConfig = FingerprintConfig()
    match: MatchConfig = MatchConfig()
    max_threads: int = min(os.cpu_count() or 4, 6)
    duration_tolerance_s: float = Thresholds.DURATION_TOLERANCE_S


@dataclass(frozen=True, slots=True)
class VideoFingerprint:
    path: str
    duration_s: float
    hashes: np.ndarray
    consensus_hash: int
    file_size: int
    width: int
    height: int
    sample_fps: float = 1.0


@dataclass(frozen=True, slots=True)
class EngineCallbacks:
    log: Callable[[str], None] = _noop
    progress: Callable[[int], None] = _noop
    stage: Callable[[str], None] = _noop
    result: Callable[[dict], None] = _noop
    status: Callable[[dict], None] = _noop
    should_cancel: Callable[[], bool] = lambda: False

class DedupDatabase:
    _EXPECTED_COLUMNS = {
        "cache_key",
        "display_rel_path",
        "file_size",
        "mtime_ns",
        "fast_sig",
        "duration",
        "width",
        "height",
        "phash_data",
        "consensus_hash",
        "sample_fps",
        "schema_version",
    }

    def __init__(self, db_path: str | Path, *, commit_every: int = 100):
        self._db_path = _safe_resolve_path(db_path, strict=False)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._commit_every = max(1, int(commit_every))
        self._pending_writes = 0
        self._conn = sqlite3.connect(str(self._db_path), timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA temp_store=MEMORY;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            existing = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='video_cache'"
            ).fetchone()
            if existing is not None:
                columns = {
                    str(row["name"])
                    for row in self._conn.execute("PRAGMA table_info(video_cache)").fetchall()
                }
                if not self._EXPECTED_COLUMNS.issubset(columns):
                    self._conn.execute("DROP TABLE IF EXISTS video_cache")
                    self._conn.commit()

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS video_cache (
                    cache_key TEXT PRIMARY KEY,
                    display_rel_path TEXT,
                    file_size INTEGER NOT NULL,
                    mtime_ns INTEGER,
                    fast_sig TEXT,
                    duration REAL NOT NULL,
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    phash_data BLOB NOT NULL,
                    consensus_hash TEXT NOT NULL,
                    sample_fps REAL NOT NULL DEFAULT 1.0,
                    schema_version INTEGER NOT NULL DEFAULT 2
                );
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_video_cache_display_rel_path ON video_cache(display_rel_path)"
            )
            self._conn.commit()

    @staticmethod
    def _consensus_to_db(consensus_hash: int) -> str:
        masked = int(consensus_hash) & ((1 << Thresholds.PHASH_BITS) - 1)
        return hex(masked)

    @staticmethod
    def _consensus_from_db(value: object) -> Optional[int]:
        if value is None:
            return None

        if isinstance(value, (int, np.integer)):
            v = int(value)
            if v < 0:
                v &= ((1 << Thresholds.PHASH_BITS) - 1)
            return v

        if isinstance(value, memoryview):
            value = value.tobytes()

        if isinstance(value, (bytes, bytearray)):
            try:
                value = value.decode("utf-8")
            except Exception:
                return None

        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return int(text, 16)
            except ValueError:
                try:
                    v = int(text)
                except ValueError:
                    return None
                if v < 0:
                    v &= ((1 << Thresholds.PHASH_BITS) - 1)
                return v

        return None

    def get_fingerprint(
        self,
        cache_key: str,
        *,
        file_size: int,
        mtime_ns: int,
        fast_sig: Optional[str],
    ) -> Optional[tuple[float, np.ndarray, int, int, int, float]]:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT file_size, mtime_ns, fast_sig, duration, width, height, "
                    "phash_data, consensus_hash, sample_fps, schema_version "
                    "FROM video_cache WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
        except sqlite3.Error:
            return None

        if row is None:
            return None

        if int(row["file_size"] or -1) != int(file_size):
            return None

        try:
            schema_version = int(row["schema_version"] or 0)
        except Exception:
            schema_version = 0
        if schema_version != int(Thresholds.CACHE_SCHEMA_VERSION):
            try:
                self.delete(cache_key)
            except Exception:
                pass
            return None

        row_fast_sig = row["fast_sig"]
        row_mtime_ns = row["mtime_ns"]
        mtime_tol_ns = int(float(Thresholds.MTIME_TOLERANCE_S) * 1_000_000_000)

        if row_fast_sig:

            if fast_sig is None or str(row_fast_sig) != str(fast_sig):
                return None
        else:

            if row_mtime_ns is None:
                return None
            if abs(int(row_mtime_ns) - int(mtime_ns)) > mtime_tol_ns:
                return None

        blob = row["phash_data"]
        consensus = row["consensus_hash"]
        duration = row["duration"]
        width = row["width"]
        height = row["height"]
        if blob is None or consensus is None or duration is None:
            return None

        try:
            blob_bytes = bytes(blob) if isinstance(blob, memoryview) else blob
            hashes = pickle.loads(blob_bytes)
        except Exception:
            try:
                self.delete(cache_key)
            except Exception:
                pass
            return None

        if not isinstance(hashes, np.ndarray):
            try:
                self.delete(cache_key)
            except Exception:
                pass
            return None

        consensus_int = self._consensus_from_db(consensus)
        if consensus_int is None:
            try:
                self.delete(cache_key)
            except Exception:
                pass
            return None

        try:
            sample_fps = float(row["sample_fps"] or 1.0)
        except Exception:
            sample_fps = 1.0
        if not math.isfinite(sample_fps) or sample_fps <= 0:
            sample_fps = 1.0

        return (
            float(duration),
            hashes.astype(np.uint64, copy=False),
            int(consensus_int),
            int(width or 0),
            int(height or 0),
            float(sample_fps),
        )

    def upsert_fingerprint(
        self,
        *,
        cache_key: str,
        display_rel_path: str,
        file_size: int,
        mtime_ns: int,
        fast_sig: Optional[str],
        duration: float,
        width: int,
        height: int,
        hashes: np.ndarray,
        consensus_hash: int,
        sample_fps: float = 1.0,
    ) -> None:
        hashes_arr = np.asarray(hashes, dtype=np.uint64)
        blob = sqlite3.Binary(pickle.dumps(hashes_arr, protocol=pickle.HIGHEST_PROTOCOL))
        consensus_hex = self._consensus_to_db(consensus_hash)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO video_cache(
                    cache_key, display_rel_path, file_size, mtime_ns, fast_sig, duration,
                    width, height, phash_data, consensus_hash, sample_fps, schema_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    display_rel_path=excluded.display_rel_path,
                    file_size=excluded.file_size,
                    mtime_ns=excluded.mtime_ns,
                    fast_sig=excluded.fast_sig,
                    duration=excluded.duration,
                    width=excluded.width,
                    height=excluded.height,
                    phash_data=excluded.phash_data,
                    consensus_hash=excluded.consensus_hash,
                    sample_fps=excluded.sample_fps,
                    schema_version=excluded.schema_version
                """,
                (
                    cache_key,
                    display_rel_path,
                    int(file_size),
                    int(mtime_ns),
                    fast_sig,
                    float(duration),
                    int(width),
                    int(height),
                    blob,
                    consensus_hex,
                    float(sample_fps),
                    int(Thresholds.CACHE_SCHEMA_VERSION),
                ),
            )
            self._pending_writes += 1
            if self._pending_writes >= self._commit_every:
                self._conn.commit()
                self._pending_writes = 0

    def flush(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            except sqlite3.Error:
                return
            self._pending_writes = 0

    def delete(self, cache_key: str) -> None:
        with self._lock:
            try:
                self._conn.execute("DELETE FROM video_cache WHERE cache_key = ?", (cache_key,))
            except sqlite3.Error:
                return
            self._pending_writes += 1
            if self._pending_writes >= self._commit_every:
                self._conn.commit()
                self._pending_writes = 0

    def close(self) -> None:
        with self._lock:
            try:
                if self._pending_writes:
                    self._conn.commit()
                    self._pending_writes = 0
                self._conn.close()
            except Exception:
                pass

    def cleanup_stale_entries(self, valid_cache_keys: set[str]) -> int:
        with self._lock:
            try:
                cursor = self._conn.execute("SELECT cache_key FROM video_cache")
                cached_keys = {str(row[0]) for row in cursor.fetchall()}
            except sqlite3.Error:
                return 0

            stale_keys = cached_keys - valid_cache_keys
            if not stale_keys:
                return 0

            self._conn.executemany("DELETE FROM video_cache WHERE cache_key = ?", ((key,) for key in stale_keys))
            self._conn.commit()
            self._pending_writes = 0
            return len(stale_keys)


class BKTree:
    class _Node:
        __slots__ = ("value", "items", "children")

        def __init__(self, value: int, item):
            self.value = int(value)
            self.items = [item]
            self.children: dict[int, "BKTree._Node"] = {}

    def __init__(self, distance_fn: Callable[[int, int], int]):
        self._distance_fn = distance_fn
        self._root: Optional[BKTree._Node] = None

    def add(self, value: int, item) -> None:
        value = int(value)
        if self._root is None:
            self._root = BKTree._Node(value, item)
            return

        node = self._root
        while True:
            dist = int(self._distance_fn(value, node.value))
            if dist == 0:
                node.items.append(item)
                return
            child = node.children.get(dist)
            if child is None:
                node.children[dist] = BKTree._Node(value, item)
                return
            node = child

    def query(self, value: int, max_distance: int) -> list:
        if self._root is None:
            return []
        value = int(value)
        max_distance = int(max_distance)
        results: list = []

        stack = [self._root]
        while stack:
            node = stack.pop()
            dist = int(self._distance_fn(value, node.value))
            if dist <= max_distance:
                results.extend(node.items)

            low = dist - max_distance
            high = dist + max_distance
            for edge_dist, child in node.children.items():
                if low <= edge_dist <= high:
                    stack.append(child)

        return results

def _dct_matrix_1d(n: int) -> np.ndarray:
    indices = np.arange(n, dtype=np.float32)
    k_values = np.arange(n, dtype=np.float32)[:, None]
    matrix = np.cos((indices + 0.5) * k_values * (np.pi / n))
    matrix[0, :] *= 1.0 / np.sqrt(n)
    matrix[1:, :] *= np.sqrt(2 / n)
    return matrix

_DCT32 = _dct_matrix_1d(Thresholds.FRAME_SIZE)

_USE_IMAGEHASH = os.environ.get("VIDEO_DEDUP_USE_IMAGEHASH", "").strip() in {"1", "true", "yes", "on"}

def phash_uint64_from_gray_32x32(gray: np.ndarray) -> int:
    expected_shape = (Thresholds.FRAME_SIZE, Thresholds.FRAME_SIZE)
    if gray.shape != expected_shape:
        raise ValueError(f"Expected {expected_shape}, got {gray.shape}")
    if _USE_IMAGEHASH and imagehash is not None:
        from PIL import Image

        img = Image.fromarray(gray, mode="L")
        h = imagehash.phash(img, hash_size=8, highfreq_factor=4)
        return int(str(h), 16)
    pixels = gray.astype(np.float32, copy=False)
    dct = _DCT32 @ pixels @ _DCT32.T
    dct_low = dct[:8, :8]
    median = float(np.median(dct_low.flatten()[1:]))
    bits = (dct_low > median).astype(np.uint8, copy=False).reshape(-1)
    packed = np.packbits(bits, bitorder="little")
    return int.from_bytes(packed.tobytes(), byteorder="little", signed=False)

def consensus_hash_uint64(hashes: np.ndarray) -> int:
    if hashes.size == 0:
        return 0
    bytes_view = hashes.astype(np.uint64, copy=False).view(np.uint8).reshape(-1, 8)
    bits = np.unpackbits(bytes_view, axis=1, bitorder="little")
    bit_means = bits.mean(axis=0)
    consensus_bits = (bit_means > 0.5).astype(np.uint8, copy=False)
    packed = np.packbits(consensus_bits, bitorder="little")
    return int.from_bytes(packed.tobytes(), byteorder="little", signed=False)

def hamming_distance_uint64_scalar(a: int, b: int) -> int:
    return int(a ^ b).bit_count()

def bitwise_count_uint64(arr: np.ndarray) -> np.ndarray:
    xor_view = arr.astype(np.uint64, copy=False)
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(xor_view)
    bytes_view = xor_view.view(np.uint8).reshape(-1, 8)
    return _POPCOUNT_LUT[bytes_view].sum(axis=1).astype(np.uint8)

def best_time_shift_similarity(
    hashes_a: np.ndarray,
    hashes_b: np.ndarray,
    *,
    max_offset_frames: int,
    min_overlap_frames: int,
    early_exit_threshold: float = 0.0,
) -> tuple[float, int, int]:
    if hashes_a.size == 0 or hashes_b.size == 0:
        return 0.0, 0, 0

    a = hashes_a.astype(np.uint64, copy=False)
    b = hashes_b.astype(np.uint64, copy=False)
    len_a = int(a.size)
    len_b = int(b.size)

    effective_min_overlap = max(1, min(int(min_overlap_frames), len_a, len_b))
    full_range_low = -(len_a - effective_min_overlap)
    full_range_high = len_b - effective_min_overlap
    low = max(full_range_low, -int(max_offset_frames))
    high = min(full_range_high, int(max_offset_frames))
    if low > high:
        return 0.0, 0, 0

    best_similarity = -1.0
    best_offset = 0
    best_overlap = 0
    max_possible_overlap = min(len_a, len_b)
    exit_threshold = float(early_exit_threshold) if early_exit_threshold > 0 else 0.0

    def eval_offset(offset_frames: int) -> tuple[float, int]:
        a_start = max(0, -offset_frames)
        b_start = max(0, offset_frames)
        overlap = min(len_a - a_start, len_b - b_start)
        if overlap < effective_min_overlap:
            return 0.0, 0

        a_seg = a[a_start : a_start + overlap]
        b_seg = b[b_start : b_start + overlap]
        distance_sum = int(bitwise_count_uint64(np.bitwise_xor(a_seg, b_seg)).sum())
        similarity = 1.0 - (distance_sum / float(Thresholds.PHASH_BITS * overlap))
        return similarity, overlap

    def record_candidate(offset_frames: int) -> tuple[bool, float, int]:
        nonlocal best_similarity, best_offset, best_overlap
        similarity, overlap = eval_offset(offset_frames)
        if overlap < effective_min_overlap:
            return False, similarity, overlap

        if _is_better_alignment(
            similarity,
            overlap,
            offset_frames,
            best_similarity,
            best_overlap,
            best_offset,
        ):
            best_similarity = similarity
            best_offset = offset_frames
            best_overlap = overlap


        early_exit = (
            exit_threshold > 0
            and overlap >= max_possible_overlap
            and similarity >= max(exit_threshold, 0.999)
        ) or (
            similarity >= 1.0 - 1e-12
            and overlap >= max_possible_overlap
        )
        return early_exit, similarity, overlap

    coarse_step = max(1, int(Thresholds.COARSE_STEP))
    offset_span = (high - low) + 1
    use_coarse = coarse_step > 1 and offset_span > int(Thresholds.COARSE_THRESHOLD)

    if not use_coarse:
        for offset_frames in range(low, high + 1):
            early_exit, _similarity, _overlap = record_candidate(offset_frames)
            if early_exit:
                break
        if best_similarity < 0:
            return 0.0, 0, 0
        return best_similarity, best_offset, best_overlap


    coarse_offsets = list(range(low, high + 1, coarse_step))
    if coarse_offsets[-1] != high:
        coarse_offsets.append(high)
    if low <= 0 <= high and 0 not in coarse_offsets:
        coarse_offsets.append(0)
    coarse_offsets = sorted(set(coarse_offsets))

    coarse_results: list[tuple[float, int, int]] = []
    best_coarse_similarity = -1.0
    for offset_frames in coarse_offsets:
        early_exit, similarity, overlap = record_candidate(offset_frames)
        if overlap >= effective_min_overlap:
            coarse_results.append((similarity, overlap, offset_frames))
            if similarity > best_coarse_similarity:
                best_coarse_similarity = similarity
        if early_exit:
            return best_similarity, best_offset, best_overlap

    if not coarse_results:
        return 0.0, 0, 0

    coarse_results.sort(
        key=lambda item: (item[0], item[1], -abs(int(item[2])), -int(item[2])),
        reverse=True,
    )


    selected_offsets = {offset for _sim, _ov, offset in coarse_results[:12]}
    similarity_margin = 0.08 if best_coarse_similarity < 0.8 else 0.04
    for similarity, overlap, offset_frames in coarse_results:
        if similarity >= (best_coarse_similarity - similarity_margin):
            selected_offsets.add(offset_frames)

    by_offset = {off: (sim, ov) for sim, ov, off in coarse_results}
    sorted_off = sorted(by_offset)
    for idx, off in enumerate(sorted_off):
        sim, _ov = by_offset[off]
        left = by_offset[sorted_off[idx - 1]][0] if idx > 0 else -1.0
        right = by_offset[sorted_off[idx + 1]][0] if idx + 1 < len(sorted_off) else -1.0
        if sim >= left and sim >= right and sim >= 0.55:
            selected_offsets.add(off)

    fine_offsets: set[int] = set()
    half = coarse_step
    for coarse_offset in selected_offsets:
        fine_low = max(low, int(coarse_offset) - half)
        fine_high = min(high, int(coarse_offset) + half)
        fine_offsets.update(range(fine_low, fine_high + 1))

    if low <= 0 <= high:
        fine_offsets.add(0)

    for offset_frames in sorted(fine_offsets):
        early_exit, _similarity, _overlap = record_candidate(offset_frames)
        if early_exit:
            break


    if best_similarity >= 0 and best_similarity < 0.97 and coarse_step > 1:
        denser_half = max(coarse_step * 2, 8)
        denser_low = max(low, best_offset - denser_half)
        denser_high = min(high, best_offset + denser_half)
        for offset_frames in range(denser_low, denser_high + 1):
            if offset_frames in fine_offsets:
                continue
            early_exit, _similarity, _overlap = record_candidate(offset_frames)
            if early_exit:
                break

    if best_similarity < 0:
        return 0.0, 0, 0
    return best_similarity, best_offset, best_overlap


def _fingerprint_video_worker(
    video_path: str,
    cache_key: str,
    display_rel_path: str,
    binaries: FFmpegBinaries,
    fingerprint_config: FingerprintConfig,
    cancel_event: threading.Event,
    db: DedupDatabase,
    on_log: Callable[[str], None],
) -> dict:
    def _kill_chain(p: subprocess.Popen[bytes]) -> None:
        if p.poll() is not None:
            return
        try:
            p.terminate()
        except Exception:
            pass
        try:
            p.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            p.kill()
        except Exception:
            pass
        try:
            p.wait(timeout=2)
        except Exception:
            pass

    path_obj = _safe_resolve_path(video_path, strict=False)
    path_str = str(path_obj)
    video_name = path_obj.name
    on_log(f"[START] Processing: {video_name}")

    try:
        stat = path_obj.stat()
        file_size = int(stat.st_size)
        mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
    except Exception as exc:
        on_log(f"[DONE] Finished: {video_name} (skipped: stat failed: {exc})")
        return {"path": path_str, "ok": False, "error": f"stat failed: {exc}"}

    width = 0
    height = 0

    def _failed(message: str, *, cancelled: bool = False) -> dict:
        payload = {
            "path": path_str,
            "ok": False,
            "error": message,
            "file_size": file_size,
            "mtime_ns": mtime_ns,
            "width": width,
            "height": height,
        }
        if cancelled:
            payload.pop("error", None)
            payload["cancelled"] = True
        return payload

    if cancel_event.is_set():
        on_log(f"[DONE] Finished: {video_name} (cancelled)")
        return _failed("cancelled", cancelled=True)


    fast_sig = _fast_file_signature(path_obj, file_size)
    cached = db.get_fingerprint(
        cache_key,
        file_size=file_size,
        mtime_ns=mtime_ns,
        fast_sig=fast_sig,
    )

    if cached is not None:
        duration_s, hashes_arr, consensus, width, height, sample_fps = cached
        on_log(f"[HASH] Loaded {int(hashes_arr.size)} frames from cache for: {video_name}")
        on_log(f"[DONE] Finished: {video_name} (cached)")
        return {
            "path": path_str,
            "ok": True,
            "duration_s": float(duration_s),
            "hashes": hashes_arr,
            "consensus": int(consensus),
            "file_size": file_size,
            "mtime_ns": mtime_ns,
            "cached": True,
            "width": int(width),
            "height": int(height),
            "sample_fps": float(sample_fps),
        }

    validator = FFprobeValidator(binaries.ffprobe_path, timeout_s=fingerprint_config.ffprobe_timeout_s)
    meta, err = validator.validate(path_str)
    if meta is None:
        on_log(f"[DONE] Finished: {video_name} (skipped: {err or 'Invalid video'})")
        return _failed(err or "Invalid video.")

    width = int(meta.width)
    height = int(meta.height)

    frame_size = int(fingerprint_config.frame_size)
    if frame_size != Thresholds.FRAME_SIZE:
        on_log(f"[DONE] Finished: {video_name} (skipped: frame_size must be {Thresholds.FRAME_SIZE})")
        return _failed(f"frame_size must be {Thresholds.FRAME_SIZE}.")
    bytes_per_frame = frame_size * frame_size

    max_frames = int(fingerprint_config.max_frames)
    if max_frames <= 0:
        on_log(f"[DONE] Finished: {video_name} (skipped: max_frames must be > 0)")
        return _failed("max_frames must be > 0.")


    sample_fps = _effective_sample_fps(
        meta.duration_s,
        float(fingerprint_config.sample_fps),
        max_frames,
    )
    if sample_fps + 1e-9 < float(fingerprint_config.sample_fps):
        on_log(
            f"[HASH] Long video: using sample_fps={sample_fps:.4f} "
            f"(span full {meta.duration_s:.1f}s within {max_frames} frames) for: {video_name}"
        )

    vf = f"fps={sample_fps:.6f},scale={frame_size}:{frame_size}:flags=bicubic,format=gray"
    cmd = [
        binaries.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-threads",
        "1",
        "-i",
        path_str,
        "-vf",
        vf,
        "-an",
        "-sn",
        "-dn",
        "-frames:v",
        str(max_frames),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]

    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    proc: subprocess.Popen[bytes] | None = None
    hashes: list[int] = []
    stderr_buffer = bytearray()
    stderr_thread: threading.Thread | None = None
    wait_timed_out = False

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)

        if proc.stderr is not None:
            stderr_pipe = proc.stderr

            def _drain_stderr(pipe) -> None:
                try:
                    while True:
                        chunk = pipe.read(4096)
                        if not chunk:
                            break
                        stderr_buffer.extend(chunk)
                        if len(stderr_buffer) > 65536:
                            del stderr_buffer[:-65536]
                except Exception:
                    pass

            stderr_thread = threading.Thread(target=_drain_stderr, args=(stderr_pipe,), daemon=True)
            stderr_thread.start()

        assert proc.stdout is not None
        while len(hashes) < max_frames:
            if cancel_event.is_set():
                _kill_chain(proc)
                on_log(f"[DONE] Finished: {video_name} (cancelled)")
                return _failed("cancelled", cancelled=True)

            raw = proc.stdout.read(bytes_per_frame)
            if len(raw) != bytes_per_frame:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((frame_size, frame_size))
            hashes.append(phash_uint64_from_gray_32x32(frame))

        on_log(f"[HASH] Extracted {len(hashes)} frames for: {video_name}")


        configured_timeout = int(fingerprint_config.ffmpeg_timeout_s or 0)
        wait_timeout_s = max(
            30,
            configured_timeout if configured_timeout > 0 else 0,
            min(300, int(meta.duration_s // 20) + 20),
        )
        try:
            proc.wait(timeout=wait_timeout_s)
        except subprocess.TimeoutExpired:
            wait_timed_out = True
            _kill_chain(proc)
            on_log(
                f"[WARN] {_timestamp()} FFmpeg lingered after frame extraction for {video_name}; "
                f"using {len(hashes)} collected frame(s)."
            )

        if stderr_thread is not None:
            stderr_thread.join(timeout=1.0)

        stderr = bytes(stderr_buffer).decode(errors="replace").strip()
        if proc.returncode not in (None, 0) and not wait_timed_out:
            error_message = stderr or f"ffmpeg rc={proc.returncode}"
            on_log(f"[DONE] Finished: {video_name} (failed: {error_message})")
            return _failed(error_message)
    except Exception as exc:
        if proc:
            _kill_chain(proc)
        on_log(f"[DONE] Finished: {video_name} (failed: ffmpeg error)")
        return _failed(f"ffmpeg failed: {exc}")
    finally:
        try:
            if proc:
                _kill_chain(proc)
        except Exception:
            pass
        try:
            if proc and proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
        try:
            if proc and proc.stderr:
                proc.stderr.close()
        except Exception:
            pass

    if not hashes:
        on_log(f"[DONE] Finished: {video_name} (failed: no frames extracted)")
        return _failed("No frames extracted.")

    hashes_arr = np.array(hashes, dtype=np.uint64)
    consensus = consensus_hash_uint64(hashes_arr)
    duration_s = float(meta.duration_s)
    try:
        db.upsert_fingerprint(
            cache_key=cache_key,
            display_rel_path=display_rel_path,
            file_size=file_size,
            mtime_ns=mtime_ns,
            fast_sig=fast_sig,
            duration=duration_s,
            width=width,
            height=height,
            hashes=hashes_arr,
            consensus_hash=int(consensus),
            sample_fps=float(sample_fps),
        )
    except Exception as exc:
        on_log(f"[WARN] {_timestamp()} Cache write failed for {video_name}: {exc}")

    on_log(f"[DONE] Finished: {video_name}")
    return {
        "path": path_str,
        "ok": True,
        "duration_s": duration_s,
        "hashes": hashes_arr,
        "consensus": int(consensus),
        "file_size": file_size,
        "mtime_ns": mtime_ns,
        "cached": False,
        "width": width,
        "height": height,
        "sample_fps": float(sample_fps),
    }


class VideoDedupEngine:
    def __init__(
        self,
        binaries: FFmpegBinaries,
        *,
        config: EngineConfig | None = None,
        callbacks: EngineCallbacks | None = None,
    ):
        self._binaries = binaries
        self._config = config or EngineConfig()
        self._callbacks = callbacks or EngineCallbacks()

    def _list_videos_in_root(self, root_dir: str | Path) -> list[Path]:
        scanner = VideoScanner(on_log=self._callbacks.log)
        return scanner.scan(root_dir)

    def _safe_move_to_dir(self, src: Path, dest_dir: Path, *, collision: str = "counter") -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        candidate = dest_dir / src.name
        if not candidate.exists():
            shutil.move(str(src), str(candidate))
            return candidate

        stem = src.stem
        suffix = src.suffix
        if collision == "timestamp":
            ts = time.strftime("%Y%m%d_%H%M%S")
            candidate = dest_dir / f"{stem}_{ts}{suffix}"
            if not candidate.exists():
                shutil.move(str(src), str(candidate))
                return candidate
            candidate = dest_dir / f"{stem}_{ts}_{time.time_ns()}{suffix}"
            shutil.move(str(src), str(candidate))
            return candidate

        i = 1
        while True:
            candidate = dest_dir / f"{stem} ({i}){suffix}"
            if not candidate.exists():
                shutil.move(str(src), str(candidate))
                return candidate
            i += 1

    def _cleanup_group_folders(self, root_dir: str | Path, prefix: str) -> dict:
        log = self._callbacks.log
        progress = self._callbacks.progress
        should_cancel = self._callbacks.should_cancel

        root_path = _safe_resolve_path(root_dir, strict=False)
        folders = sorted([p for p in root_path.glob(f"{prefix}*") if p.is_dir()], key=lambda p: p.name.lower())
        if not folders:
            return {"moved_back": 0, "removed_folders": 0}

        moved_back = 0
        removed_folders = 0
        total_files = sum(1 for folder in folders for path in folder.rglob("*") if path.is_file())
        done = 0

        log(f"[INFO] {_timestamp()} Cleaning up {len(folders)} folder(s) matching {prefix}* ...")
        for folder in folders:
            file_paths = sorted((p for p in folder.rglob("*") if p.is_file()), key=_path_sort_key)
            for file_path in file_paths:
                if should_cancel():
                    log(f"[INFO] {_timestamp()} Cleanup cancelled.")
                    return {"moved_back": moved_back, "removed_folders": removed_folders, "cancelled": True}
                try:
                    self._safe_move_to_dir(file_path, root_path)
                except Exception as exc:
                    log(f"[WARN] {_timestamp()} Cleanup failed to move {file_path.name}: {exc}")
                    continue
                moved_back += 1
                done += 1
                if total_files:
                    progress(int((done / total_files) * 100))


            try:
                for sub in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                    if sub.is_dir():
                        try:
                            sub.rmdir()
                        except OSError:
                            pass
                folder.rmdir()
                removed_folders += 1
            except OSError:
                pass

        progress(100)
        return {"moved_back": moved_back, "removed_folders": removed_folders}

    def run_cleanup_low_res(self, root_dir: Path) -> dict:
        log = self._callbacks.log
        progress = self._callbacks.progress
        stage = self._callbacks.stage
        should_cancel = self._callbacks.should_cancel

        root_path = _safe_resolve_path(root_dir, strict=False)
        stage("Cleanup")
        scanner = VideoScanner(on_log=log)
        video_paths = scanner.scan(root_path)
        total = len(video_paths)
        if total == 0:
            progress(0)
            return {"scanned": 0, "kept": 0, "trashed_low_res": 0, "duration_items": [], "cancelled": False}

        validator = FFprobeValidator(self._binaries.ffprobe_path, timeout_s=self._config.fingerprint.ffprobe_timeout_s)
        duration_items: list[tuple[float, Path]] = []
        trashed_low_res = 0
        progress(0)

        for idx, p in enumerate(video_paths, start=1):
            if should_cancel():
                log(f"[INFO] {_timestamp()} Cleanup cancelled.")
                return {
                    "scanned": idx - 1,
                    "kept": len(duration_items),
                    "trashed_low_res": trashed_low_res,
                    "duration_items": duration_items,
                    "cancelled": True,
                }

            meta, err = validator.validate(p)
            if meta is None:
                log(f"[WARN] {_timestamp()} Cleanup skipping {p.name} ({err or 'invalid'})")
                progress(int((idx / total) * 100))
                continue

            if min(int(meta.width), int(meta.height)) < Thresholds.MIN_RESOLUTION_PX:
                try:
                    trash_dir = root_path / TRASH_LOW_RES_DIRNAME
                    moved_to = self._safe_move_to_dir(p, trash_dir, collision="timestamp")
                    trashed_low_res += 1
                    log(
                        f"[WARN] {_timestamp()} Cleanup moved low-res (<{Thresholds.MIN_RESOLUTION_PX}px) to {TRASH_LOW_RES_DIRNAME}: "
                        f"{moved_to.name} ({int(meta.width)}x{int(meta.height)})"
                    )
                except Exception as exc:
                    log(f"[WARN] {_timestamp()} Cleanup failed to move {p.name} to {TRASH_LOW_RES_DIRNAME}: {exc}")
            else:
                duration_items.append((float(meta.duration_s), p))

            progress(int((idx / total) * 100))

        return {
            "scanned": total,
            "kept": len(duration_items),
            "trashed_low_res": trashed_low_res,
            "duration_items": duration_items,
            "cancelled": False,
        }

    def run_phase_1_size(self, root_dir: str | Path) -> dict:
        log = self._callbacks.log
        progress = self._callbacks.progress
        should_cancel = self._callbacks.should_cancel

        root_path = _safe_resolve_path(root_dir, strict=False)
        videos = self._list_videos_in_root(root_path)
        if not videos:
            progress(0)
            log(f"[INFO] {_timestamp()} Phase 1: no videos found in root.")
            return {"processed": 0, "groups_created": 0, "files_moved": 0, "cancelled": False}

        size_groups: dict[int, list[Path]] = {}
        for p in videos:
            try:
                size_groups.setdefault(int(p.stat().st_size), []).append(p)
            except Exception:
                continue

        duplicate_groups = [paths for paths in size_groups.values() if len(paths) > 1]
        duplicate_groups.sort(key=lambda group: (len(group), group[0].name.lower()), reverse=True)

        groups_created = 0
        files_moved = 0
        total_to_move = sum(len(g) for g in duplicate_groups)
        moved_so_far = 0
        progress(0)

        for i, group in enumerate(duplicate_groups, start=1):
            if should_cancel():
                log(f"[INFO] {_timestamp()} Phase 1 cancelled.")
                return {
                    "processed": len(videos),
                    "groups_created": groups_created,
                    "files_moved": files_moved,
                    "cancelled": True,
                }

            dest = root_path / f"Group_Size_{i}"
            dest.mkdir(parents=True, exist_ok=True)
            groups_created += 1

            try:
                size_value = int(group[0].stat().st_size)
            except Exception:
                size_value = -1
            log(f"[INFO] {_timestamp()} Phase 1: Group_Size_{i} (size={size_value} bytes, files={len(group)})")
            for src in group:
                if should_cancel():
                    log(f"[INFO] {_timestamp()} Phase 1 cancelled.")
                    return {
                        "processed": len(videos),
                        "groups_created": groups_created,
                        "files_moved": files_moved,
                        "cancelled": True,
                    }
                try:
                    self._safe_move_to_dir(src, dest)
                except Exception as exc:
                    log(f"[WARN] {_timestamp()} Phase 1 failed to move {src.name}: {exc}")
                    continue
                files_moved += 1
                moved_so_far += 1
                if total_to_move:
                    progress(int((moved_so_far / total_to_move) * 100))

        progress(100)
        log("Phase 1 Complete. Please manually check the `Group_Size_X` folders. Delete unwanted files.")
        return {"processed": len(videos), "groups_created": groups_created, "files_moved": files_moved, "cancelled": False}

    def run_phase_2_duration(self, root_dir: str | Path) -> dict:
        log = self._callbacks.log
        progress = self._callbacks.progress
        stage = self._callbacks.stage
        should_cancel = self._callbacks.should_cancel

        root_path = _safe_resolve_path(root_dir, strict=False)
        stage("Phase 2: Duration Matching")

        cleanup = self._cleanup_group_folders(root_path, "Group_Size_")
        if cleanup.get("cancelled"):
            return {"processed": 0, "groups_created": 0, "files_moved": 0, "trashed_low_res": 0, "cancelled": True}

        cleanup2 = self.run_cleanup_low_res(root_path)
        if cleanup2.get("cancelled"):
            return {
                "processed": 0,
                "groups_created": 0,
                "files_moved": 0,
                "trashed_low_res": int(cleanup2.get("trashed_low_res") or 0),
                "cancelled": True,
            }

        duration_items = list(cleanup2.get("duration_items") or [])
        trashed_low_res = int(cleanup2.get("trashed_low_res") or 0)
        if not duration_items:
            progress(0)
            log(f"[INFO] {_timestamp()} Phase 2: no eligible videos found after cleanup.")
            return {
                "processed": 0,
                "groups_created": 0,
                "files_moved": 0,
                "trashed_low_res": trashed_low_res,
                "cancelled": False,
            }

        duration_items.sort(key=lambda item: item[0])
        tolerance = max(0.0, float(self._config.duration_tolerance_s))
        log(
            f"[INFO] {_timestamp()} Phase 2: grouping durations with absolute tolerance "
            f"<= {tolerance:.2f}s from cluster base."
        )


        clusters: list[list[tuple[float, Path]]] = []
        current_cluster: list[tuple[float, Path]] = []
        cluster_base: float | None = None
        for item in duration_items:
            duration_s, _path = item
            if not current_cluster or cluster_base is None:
                current_cluster = [item]
                cluster_base = duration_s
                continue

            if (duration_s - cluster_base) <= tolerance:
                current_cluster.append(item)
            else:
                clusters.append(current_cluster)
                current_cluster = [item]
                cluster_base = duration_s

        if current_cluster:
            clusters.append(current_cluster)

        duplicate_clusters = [cluster for cluster in clusters if len(cluster) > 1]
        groups_created = 0
        files_moved = 0
        total_to_move = sum(len(cluster) for cluster in duplicate_clusters)
        moved_so_far = 0
        progress(0)

        for i, cluster in enumerate(duplicate_clusters, start=1):
            if should_cancel():
                log(f"[INFO] {_timestamp()} Phase 2 cancelled.")
                return {
                    "processed": len(duration_items),
                    "groups_created": groups_created,
                    "files_moved": files_moved,
                    "trashed_low_res": trashed_low_res,
                    "cancelled": True,
                }

            dest = root_path / f"Group_Time_{i}"
            dest.mkdir(parents=True, exist_ok=True)
            groups_created += 1

            base_dur = cluster[0][0]
            log(f"[INFO] {_timestamp()} Phase 2: Group_Time_{i} (~{base_dur:.3f}s, files={len(cluster)})")
            for _dur, src in cluster:
                if should_cancel():
                    log(f"[INFO] {_timestamp()} Phase 2 cancelled.")
                    return {
                        "processed": len(duration_items),
                        "groups_created": groups_created,
                        "files_moved": files_moved,
                        "trashed_low_res": trashed_low_res,
                        "cancelled": True,
                    }
                try:
                    self._safe_move_to_dir(src, dest)
                except Exception as exc:
                    log(f"[WARN] {_timestamp()} Phase 2 failed to move {src.name}: {exc}")
                    continue
                files_moved += 1
                moved_so_far += 1
                if total_to_move:
                    progress(int((moved_so_far / total_to_move) * 100))

        progress(100)
        log("Phase 2 Complete. Please check `Group_Time_X` folders.")
        return {
            "processed": len(duration_items),
            "groups_created": groups_created,
            "files_moved": files_moved,
            "trashed_low_res": trashed_low_res,
            "cancelled": False,
        }

    def run_phase_3_prepare(self, root_dir: str | Path) -> dict:
        log = self._callbacks.log
        stage = self._callbacks.stage

        root_path = _safe_resolve_path(root_dir, strict=False)
        stage("Phase 3 Prep")
        cleanup = self._cleanup_group_folders(root_path, "Group_Time_")
        if cleanup.get("cancelled"):
            return {**cleanup, "prepared_videos": []}

        prepared_videos = [str(path) for path in self._list_videos_in_root(root_path)]
        log(
            f"[INFO] {_timestamp()} Phase 3 prepare: prepared {len(prepared_videos)} video(s) "
            "for content comparison."
        )
        return {**cleanup, "prepared_videos": prepared_videos}

    def run_phase_3_content(self, root_dir: str | Path, *, video_paths: Optional[list[str | Path]] = None) -> dict:
        log = self._callbacks.log
        progress = self._callbacks.progress
        stage = self._callbacks.stage
        on_result = self._callbacks.result
        on_status = self._callbacks.status
        should_cancel = self._callbacks.should_cancel

        root_path = _safe_resolve_path(root_dir, strict=False)
        db: DedupDatabase | None = None
        try:
            stage("Hashing")
            log(f"[INFO] {_timestamp()} Phase 3: Loading fingerprints (cache/ffmpeg)...")

            if video_paths is None:
                scanner = VideoScanner(on_log=log)
                source_paths = scanner.scan(root_path)
            else:
                source_paths = []
                seen_paths: set[str] = set()
                for candidate in video_paths:
                    resolved = _safe_resolve_path(candidate, strict=False)
                    resolved_str = str(resolved)
                    key = _normalized_text(resolved_str).casefold()
                    if key in seen_paths:
                        continue
                    seen_paths.add(key)
                    if not _safe_is_file(resolved):
                        continue
                    if resolved.suffix.lower() not in VIDEO_EXTENSIONS:
                        continue
                    source_paths.append(resolved)
                source_paths.sort(key=_path_sort_key)
                log(
                    f"[INFO] {_timestamp()} Phase 3: Using provided prepared video list "
                    f"({len(source_paths)} item(s)); skipping directory rescan."
                )

            video_records = [(p, *_make_cache_identity(root_path, p)) for p in source_paths]
            total_videos = len(video_records)
            if total_videos == 0:
                progress(0)
                on_status({"stage": "Hashing", "total": 0, "completed": 0, "current_file": ""})
                log(f"[INFO] {_timestamp()} Phase 3: Nothing to do.")
                return {"processed": 0, "duplicates": 0, "cancelled": False}

            db = DedupDatabase(root_path / CACHE_DB_FILENAME)
            stale_count = db.cleanup_stale_entries({cache_key for _p, cache_key, _rel in video_records})
            if stale_count > 0:
                log(f"[INFO] {_timestamp()} Removed {stale_count} stale cache entries.")
            cancel_event = threading.Event()

            fp_config = self._config.fingerprint
            match_config = self._config.match
            min_overlap = int(match_config.min_overlap_frames)
            max_threads = max(1, min(16, int(self._config.max_threads)))
            require_duration = bool(match_config.require_comparable_duration)

            fingerprints: list[VideoFingerprint] = []
            cancelling = False
            completed = 0
            comparisons_skipped_duration = 0
            progress(0)
            on_status({"stage": "Hashing", "total": total_videos, "completed": 0, "current_file": ""})

            def log_error_for(path_str: str, error: str) -> None:
                log(f"[WARN] {_timestamp()} Skipping: {path_str} ({error})")

            path_iter = iter(video_records)
            pending: set = set()

            def submit_next(executor: ThreadPoolExecutor) -> bool:
                try:
                    path, cache_key, display_rel_path = next(path_iter)
                except StopIteration:
                    return False
                on_status(
                    {
                        "stage": "Hashing",
                        "total": total_videos,
                        "completed": completed,
                        "current_file": path.name,
                    }
                )
                fut = executor.submit(
                    _fingerprint_video_worker,
                    str(path),
                    cache_key,
                    display_rel_path,
                    self._binaries,
                    fp_config,
                    cancel_event,
                    db,
                    log,
                )
                pending.add(fut)
                return True

            with ThreadPoolExecutor(max_workers=max_threads) as executor:
                for _ in range(max_threads):
                    if not submit_next(executor):
                        break
                while pending:
                    if should_cancel() and not cancelling:
                        cancelling = True
                        cancel_event.set()
                        log(f"[INFO] {_timestamp()} Cancellation requested. Stopping...")
                        for fut in list(pending):
                            fut.cancel()

                    done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    for fut in done:
                        completed += 1
                        progress(int((completed / total_videos) * 100))
                        on_status({"stage": "Hashing", "total": total_videos, "completed": completed})

                        if cancelling:
                            continue

                        try:
                            result = fut.result()
                        except Exception as exc:
                            log_error_for("<worker>", f"Unhandled worker exception: {exc}")
                            log(traceback.format_exc())
                            continue

                        if not result.get("ok"):
                            if result.get("cancelled"):
                                cancelling = True
                                cancel_event.set()
                                log(f"[INFO] {_timestamp()} Cancellation received from worker.")
                                continue
                            log_error_for(str(result.get("path", "")), str(result.get("error", "Unknown error")))
                            continue

                        fingerprints.append(
                            VideoFingerprint(
                                path=str(result["path"]),
                                duration_s=float(result["duration_s"]),
                                hashes=result["hashes"],
                                consensus_hash=int(result["consensus"]),
                                file_size=int(result.get("file_size") or 0),
                                width=int(result.get("width") or 0),
                                height=int(result.get("height") or 0),
                                sample_fps=float(result.get("sample_fps") or fp_config.sample_fps or 1.0),
                            )
                        )

                    while not cancelling and len(pending) < max_threads:
                        if not submit_next(executor):
                            break

            if db is not None:
                db.flush()

            if cancelling:
                log(f"[INFO] {_timestamp()} Cancelled.")
                return {"processed": len(fingerprints), "duplicates": 0, "cancelled": True}

            if not fingerprints:
                progress(100)
                on_status({"stage": "Hashing", "total": total_videos, "completed": total_videos, "current_file": ""})
                log(f"[INFO] {_timestamp()} Phase 3: No valid videos to compare.")
                return {"processed": 0, "duplicates": 0, "cancelled": False}

            stage("Comparing")
            progress(0)
            on_status({"stage": "Comparing", "total": len(fingerprints), "completed": 0, "current_file": ""})

            base_max_hamming = _prefilter_hamming_radius(
                float(match_config.consensus_prefilter_similarity),
                frame_count=max(int(fp.hashes.size) for fp in fingerprints),
                min_radius=int(match_config.consensus_min_hamming_radius),
            )
            log(
                f"[INFO] {_timestamp()} Phase 3: BK-Tree prefilter <= {base_max_hamming} bit(s) "
                "(consensus-hash loose gate)"
            )
            if require_duration:
                log(
                    f"[INFO] {_timestamp()} Phase 3: Duration gate "
                    f"pad>={Thresholds.DURATION_PADDING_S:.0f}s or "
                    f"{Thresholds.DURATION_PADDING_RATIO:.0%} of longer file."
                )
            log(f"[INFO] {_timestamp()} Phase 3: Coarse-to-fine offset search across BK-Tree candidates...")
            tree = BKTree(hamming_distance_uint64_scalar)
            duplicates_found = 0
            comparisons_run = 0
            reported_pairs: set[tuple[str, str]] = set()

            for i, fp in enumerate(fingerprints):
                if should_cancel():
                    cancelling = True
                    break

                on_status(
                    {
                        "stage": "Comparing",
                        "total": len(fingerprints),
                        "completed": i,
                        "current_file": Path(fp.path).name,
                    }
                )

                max_hamming = _prefilter_hamming_radius(
                    float(match_config.consensus_prefilter_similarity),
                    frame_count=int(fp.hashes.size),
                    min_radius=int(match_config.consensus_min_hamming_radius),
                )
                neighbors = tree.query(fp.consensus_hash, max_hamming)
                for j in neighbors:
                    if should_cancel():
                        cancelling = True
                        break

                    other = fingerprints[j]
                    if require_duration and not _durations_comparable(other.duration_s, fp.duration_s):
                        comparisons_skipped_duration += 1
                        continue


                    pair_fps = max(
                        1e-6,
                        min(float(other.sample_fps or 1.0), float(fp.sample_fps or 1.0)),
                    )
                    fps_ratio = max(float(other.sample_fps or 1.0), float(fp.sample_fps or 1.0)) / pair_fps
                    if fps_ratio > 1.25:

                        comparisons_skipped_duration += 1
                        continue

                    max_offset_frames = int(round(float(match_config.max_offset_s) * pair_fps))
                    pair_min_overlap = max(1, min(min_overlap, int(other.hashes.size), int(fp.hashes.size)))

                    comparisons_run += 1
                    similarity, offset_frames, overlap_frames = best_time_shift_similarity(
                        other.hashes,
                        fp.hashes,
                        max_offset_frames=max_offset_frames,
                        min_overlap_frames=pair_min_overlap,
                        early_exit_threshold=float(match_config.duplicate_similarity),
                    )

                    required_similarity = _effective_duplicate_similarity(
                        float(match_config.duplicate_similarity),
                        int(overlap_frames),
                    )
                    if similarity >= required_similarity:
                        pair_key = tuple(
                            sorted(
                                (
                                    _normalized_text(other.path).casefold(),
                                    _normalized_text(fp.path).casefold(),
                                )
                            )
                        )
                        if pair_key in reported_pairs:
                            continue
                        reported_pairs.add(pair_key)

                        keep, drop = _prefer_as_original(other, fp)


                        if keep.path == other.path:
                            signed_offset_frames = offset_frames
                        else:
                            signed_offset_frames = -offset_frames
                        offset_s = signed_offset_frames / pair_fps

                        duplicates_found += 1
                        on_result(
                            {
                                "original": keep.path,
                                "duplicate": drop.path,
                                "original_size": int(keep.file_size),
                                "duplicate_size": int(drop.file_size),
                                "original_duration_s": float(keep.duration_s),
                                "duplicate_duration_s": float(drop.duration_s),
                                "original_width": int(keep.width),
                                "original_height": int(keep.height),
                                "duplicate_width": int(drop.width),
                                "duplicate_height": int(drop.height),
                                "similarity": float(similarity * 100.0),
                                "offset_s": float(offset_s),
                                "overlap_frames": int(overlap_frames),
                            }
                        )

                if cancelling:
                    break
                tree.add(fp.consensus_hash, i)
                if i % Thresholds.PROGRESS_INTERVAL == 0 or i == len(fingerprints) - 1:
                    progress(int(((i + 1) / len(fingerprints)) * 100))
                on_status(
                    {
                        "stage": "Comparing",
                        "total": len(fingerprints),
                        "completed": i + 1,
                    }
                )

            if cancelling:
                log(f"[INFO] {_timestamp()} Cancelled.")
                return {"processed": len(fingerprints), "duplicates": duplicates_found, "cancelled": True}

            progress(100)
            on_status(
                {
                    "stage": "Comparing",
                    "total": len(fingerprints),
                    "completed": len(fingerprints),
                    "current_file": "",
                }
            )
            log(
                f"[INFO] {_timestamp()} Done. Processed {len(fingerprints)} valid video(s). "
                f"Compared {comparisons_run} pair(s)"
                + (
                    f", skipped {comparisons_skipped_duration} by duration/fps gate."
                    if comparisons_skipped_duration
                    else "."
                )
            )
            return {"processed": len(fingerprints), "duplicates": duplicates_found, "cancelled": False}
        except Exception as exc:
            log(f"[ERROR] {_timestamp()} Engine failed: {exc}")
            log(traceback.format_exc())
            return {"processed": 0, "duplicates": 0, "cancelled": True}
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def run(self, root_dir: str | Path) -> dict:
        return self.run_phase_3_content(root_dir)


def run_gui(binaries: FFmpegBinaries) -> int:
    from PyQt5.QtCore import QSettings, QThread, QTimer, Qt, pyqtSignal
    from PyQt5.QtGui import QBrush, QColor, QDesktopServices, QFont, QKeySequence, QPalette, QTextCursor
    from PyQt5.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDoubleSpinBox,
        QFileDialog,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMenu,
        QMessageBox,
        QProgressBar,
        QProgressDialog,
        QPushButton,
        QShortcut,
        QSpinBox,
        QSplitter,
        QTableWidget,
        QTableWidgetItem,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
    from PyQt5.QtCore import QUrl

    PHASE_IDLE = 0
    PHASE_SIZE = 1
    PHASE_TIME = 2
    PHASE_CONTENT = 3

    SCAN_MODE_FULL = "full"
    SCAN_MODE_QUICK = "quick"
    SCAN_MODE_SIZE_ONLY = "size_only"
    SCAN_MODE_DURATION_ONLY = "duration_only"

    PATH_ROLE = Qt.UserRole
    SORT_ROLE = Qt.UserRole + 1
    PAYLOAD_ROLE = Qt.UserRole + 2

    class SortableTableWidgetItem(QTableWidgetItem):
        def __lt__(self, other) -> bool:
            if isinstance(other, QTableWidgetItem):
                left = self.data(SORT_ROLE)
                right = other.data(SORT_ROLE)
                if left is not None and right is not None:
                    try:
                        return left < right
                    except Exception:
                        try:
                            return float(left) < float(right)
                        except Exception:
                            pass
            return super().__lt__(other)

    class WorkerThread(QThread):
        signal_log = pyqtSignal(str)
        signal_progress = pyqtSignal(int)
        signal_stage = pyqtSignal(str)
        signal_status = pyqtSignal(dict)
        signal_result = pyqtSignal(dict)
        signal_done = pyqtSignal(str, dict)
        signal_finished = pyqtSignal()

        def __init__(
            self,
            task: str,
            root_dir: str,
            binaries: FFmpegBinaries,
            max_threads: int,
            duration_tolerance_s: float,
            prepared_video_paths: Optional[list[str]] = None,
            parent=None,
        ):
            super().__init__(parent)
            self._task = str(task)
            self._root_dir = root_dir
            self._binaries = binaries
            self._max_threads = int(max_threads)
            self._duration_tolerance_s = float(duration_tolerance_s)
            self._prepared_video_paths = list(prepared_video_paths or [])
            self._cancel_event = threading.Event()

        def request_cancel(self) -> None:
            self._cancel_event.set()
            self.requestInterruption()

        def _should_cancel(self) -> bool:
            return self._cancel_event.is_set() or self.isInterruptionRequested()

        def _log(self, message: str) -> None:
            try:
                print(message, flush=True)
            except Exception:
                pass
            self.signal_log.emit(message)

        def run(self) -> None:
            callbacks = EngineCallbacks(
                log=self._log,
                progress=self.signal_progress.emit,
                stage=self.signal_stage.emit,
                result=self.signal_result.emit,
                status=self.signal_status.emit,
                should_cancel=self._should_cancel,
            )
            config = EngineConfig(
                max_threads=max(1, min(16, self._max_threads)),
                duration_tolerance_s=max(0.0, self._duration_tolerance_s),
            )
            engine = VideoDedupEngine(self._binaries, config=config, callbacks=callbacks)

            if self._task == "phase1_size":
                self.signal_stage.emit("Phase 1: Size Matching")
                summary = engine.run_phase_1_size(self._root_dir)
            elif self._task == "phase2_time":
                self.signal_stage.emit("Phase 2: Duration Matching")
                summary = engine.run_phase_2_duration(self._root_dir)
            elif self._task == "phase3_prepare":
                self.signal_stage.emit("Phase 3 Prep")
                summary = engine.run_phase_3_prepare(self._root_dir)
            elif self._task == "phase3_direct":
                self.signal_stage.emit("Cleanup")
                self._log(
                    f"[INFO] {_timestamp()} Quick scan: running pre-scan cleanup for <{Thresholds.MIN_RESOLUTION_PX}px videos..."
                )
                cleanup = engine.run_cleanup_low_res(Path(self._root_dir))
                kept_paths = [str(path) for _duration, path in (cleanup.get("duration_items") or [])]
                if cleanup.get("cancelled"):
                    summary = {"cancelled": True, "error": "Cancelled during cleanup.", **cleanup}
                else:
                    self._log("[INFO] Cleanup done. Continuing with content scan using the prepared file list.")
                    summary = engine.run_phase_3_content(self._root_dir, video_paths=kept_paths)
                    summary = {
                        **summary,
                        "trashed_low_res_precleanup": int(cleanup.get("trashed_low_res") or 0),
                        "prepared_videos": kept_paths,
                    }
            elif self._task == "phase3_content":
                self.signal_stage.emit("Hashing")
                summary = engine.run_phase_3_content(
                    self._root_dir,
                    video_paths=self._prepared_video_paths or None,
                )
            else:
                summary = {"cancelled": True, "error": f"Unknown task: {self._task}"}

            self.signal_done.emit(self._task, summary)
            self.signal_finished.emit()

    class MainWindow(QMainWindow):
        def __init__(self, binaries: FFmpegBinaries):
            super().__init__()
            self._binaries = binaries
            self._worker: WorkerThread | None = None
            self._settings = QSettings("VideoDedupTool", "Deduplicator")
            self._prepared_phase3_videos: list[str] = []
            self._scan_started_at: float | None = None
            self._stage_started_at: float | None = None
            self._status_info: dict[str, object] = {"stage": "Idle", "total": 0, "completed": 0, "current_file": ""}
            self._log_entries: list[tuple[str, str]] = []
            self._selected_delete_paths: set[str] = set()
            self.current_phase = PHASE_IDLE
            self._active_task: str | None = None
            self._root_dir: str | None = None
            self._scan_mode = SCAN_MODE_FULL

            self.setWindowTitle("Video Deduplicator v1.1")
            self.setMinimumSize(1280, 780)
            self.setAcceptDrops(True)

            palette = QApplication.instance().palette()
            self._dark_mode = palette.color(QPalette.Window).lightness() < 128
            self._conflict_brush = QBrush(QColor(120, 50, 50) if self._dark_mode else QColor(255, 220, 220))
            self._resolved_brush = QBrush(QColor(50, 120, 50) if self._dark_mode else QColor(220, 255, 220))
            self._delete_selection_brush = QBrush(QColor(140, 60, 60) if self._dark_mode else QColor(255, 185, 185))
            self._delete_selection_foreground = QBrush(QColor(255, 255, 255) if self._dark_mode else QColor(120, 0, 0))

            central = QWidget(self)
            self.setCentralWidget(central)

            last_folder = str(self._settings.value("last_folder", os.getcwd()) or os.getcwd())

            self.path_edit = QLineEdit()
            self.path_edit.setPlaceholderText("Select a folder to scan...")
            self.path_edit.setText(last_folder)

            self.browse_button = QPushButton("Browse...")
            self.start_button = QPushButton("Start Scan")
            self.start_button.setDefault(True)
            self.continue_button = QPushButton("Continue")
            self.continue_button.setEnabled(False)
            self.continue_button.setVisible(False)
            self.stop_button = QPushButton("Stop/Cancel")
            self.stop_button.setEnabled(False)

            self.scan_mode_combo = QComboBox()
            self.scan_mode_combo.addItem("Full Scan (Size → Duration → Content)", SCAN_MODE_FULL)
            self.scan_mode_combo.addItem("Quick Scan (Content Only)", SCAN_MODE_QUICK)
            self.scan_mode_combo.addItem("Size Match Only", SCAN_MODE_SIZE_ONLY)
            self.scan_mode_combo.addItem("Duration Match Only", SCAN_MODE_DURATION_ONLY)
            self.scan_mode_combo.setCurrentIndex(0)

            self.max_threads_spin = QSpinBox()
            self.max_threads_spin.setRange(1, 16)
            self.max_threads_spin.setValue(min(os.cpu_count() or 4, 6))
            self.max_threads_spin.setToolTip("Limit concurrent FFmpeg workers to reduce CPU/Disk pressure.")

            self.duration_tolerance_spin = QDoubleSpinBox()
            self.duration_tolerance_spin.setRange(0.1, 10.0)
            self.duration_tolerance_spin.setDecimals(1)
            self.duration_tolerance_spin.setSingleStep(0.1)
            self.duration_tolerance_spin.setValue(Thresholds.DURATION_TOLERANCE_S)
            self.duration_tolerance_spin.setSuffix(" s")
            self.duration_tolerance_spin.setToolTip("Duration tolerance used for Phase 2 grouping.")

            self.progress_bar = QProgressBar()
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.stage_label = QLabel("Stage: Idle")

            self.clear_log_button = QPushButton("Clear Log")
            self.save_log_button = QPushButton("Save Log")
            self.info_checkbox = QCheckBox("INFO")
            self.warn_checkbox = QCheckBox("WARN")
            self.error_checkbox = QCheckBox("ERROR")
            for checkbox in (self.info_checkbox, self.warn_checkbox, self.error_checkbox):
                checkbox.setChecked(True)

            self.log_box = QTextEdit()
            self.log_box.setReadOnly(True)
            self.log_box.setFont(QFont("Consolas", 10))

            self.export_button = QPushButton("Export Results")
            self.export_button.setEnabled(False)
            self.auto_select_smaller_button = QPushButton("Auto-select duplicates (keep larger)")
            self.auto_select_smaller_button.setEnabled(False)
            self.auto_select_lower_res_button = QPushButton("Auto-select duplicates (keep higher-res)")
            self.auto_select_lower_res_button.setEnabled(False)
            self.delete_selected_button = QPushButton("Delete Selected")
            self.delete_selected_button.setEnabled(False)

            headers = [
                "Original File",
                "Duplicate File",
                "Original Size",
                "Duplicate Size",
                "Original Duration",
                "Duplicate Duration",
                "Similarity %",
                "Time Offset",
            ]
            self.results_table = QTableWidget(0, len(headers))
            self.results_table.setHorizontalHeaderLabels(headers)
            self.results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
            self.results_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            for col in range(2, len(headers)):
                self.results_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
            self.results_table.horizontalHeader().setMinimumSectionSize(90)
            self.results_table.setColumnWidth(0, 320)
            self.results_table.setColumnWidth(1, 320)
            self.results_table.setAlternatingRowColors(True)
            self.results_table.setSortingEnabled(False)
            self.results_table.setContextMenuPolicy(Qt.CustomContextMenu)
            self.results_table.customContextMenuRequested.connect(self._on_results_context_menu)
            self.results_table.cellDoubleClicked.connect(self._on_results_double_clicked)

            self._refresh_timer = QTimer(self)
            self._refresh_timer.setInterval(2000)
            self._refresh_timer.timeout.connect(self._refresh_table_state)
            self._refresh_timer.start()

            input_row = QHBoxLayout()
            input_row.addWidget(QLabel("Folder:"))
            input_row.addWidget(self.path_edit, 1)
            input_row.addWidget(self.browse_button)

            control_row = QHBoxLayout()
            control_row.addWidget(self.start_button)
            control_row.addWidget(self.continue_button)
            control_row.addWidget(self.stop_button)
            control_row.addSpacing(12)
            control_row.addWidget(QLabel("Scan Mode:"))
            control_row.addWidget(self.scan_mode_combo)
            control_row.addWidget(QLabel("Max Threads:"))
            control_row.addWidget(self.max_threads_spin)
            control_row.addWidget(QLabel("Duration Tolerance:"))
            control_row.addWidget(self.duration_tolerance_spin)
            control_row.addStretch(1)

            log_controls = QHBoxLayout()
            log_controls.addWidget(QLabel("Logs:"))
            log_controls.addWidget(self.clear_log_button)
            log_controls.addWidget(self.save_log_button)
            log_controls.addSpacing(12)
            log_controls.addWidget(self.info_checkbox)
            log_controls.addWidget(self.warn_checkbox)
            log_controls.addWidget(self.error_checkbox)
            log_controls.addStretch(1)

            log_panel = QWidget()
            log_layout = QVBoxLayout(log_panel)
            log_layout.setContentsMargins(0, 0, 0, 0)
            log_layout.addLayout(log_controls)
            log_layout.addWidget(self.log_box)

            result_controls = QHBoxLayout()
            result_controls.addWidget(QLabel("Results:"))
            result_controls.addWidget(self.export_button)
            result_controls.addWidget(self.auto_select_smaller_button)
            result_controls.addWidget(self.auto_select_lower_res_button)
            result_controls.addWidget(self.delete_selected_button)
            result_controls.addStretch(1)

            results_panel = QWidget()
            results_layout = QVBoxLayout(results_panel)
            results_layout.setContentsMargins(0, 0, 0, 0)
            results_layout.addLayout(result_controls)
            results_layout.addWidget(self.results_table)

            splitter = QSplitter(Qt.Vertical)
            splitter.addWidget(log_panel)
            splitter.addWidget(results_panel)
            splitter.setStretchFactor(0, 2)
            splitter.setStretchFactor(1, 3)

            layout = QVBoxLayout()
            layout.addLayout(input_row)
            layout.addLayout(control_row)
            layout.addWidget(self.stage_label)
            layout.addWidget(self.progress_bar)
            layout.addWidget(splitter, 1)
            central.setLayout(layout)

            self.summary_label = QLabel("Summary: —")
            self.statusBar().addPermanentWidget(self.summary_label, 1)

            self.browse_button.clicked.connect(self._on_browse)
            self.start_button.clicked.connect(self._on_start)
            self.continue_button.clicked.connect(self._on_continue)
            self.stop_button.clicked.connect(self._on_stop)
            self.clear_log_button.clicked.connect(self._clear_log)
            self.save_log_button.clicked.connect(self._save_log)
            self.info_checkbox.toggled.connect(self._rebuild_log_view)
            self.warn_checkbox.toggled.connect(self._rebuild_log_view)
            self.error_checkbox.toggled.connect(self._rebuild_log_view)
            self.export_button.clicked.connect(self._export_results_to_csv)
            self.auto_select_smaller_button.clicked.connect(self._auto_select_keep_larger)
            self.auto_select_lower_res_button.clicked.connect(self._auto_select_keep_higher_res)
            self.delete_selected_button.clicked.connect(self._delete_selected_files)

            QShortcut(QKeySequence("Ctrl+O"), self, activated=self._on_browse)
            QShortcut(QKeySequence("Return"), self, activated=self._on_primary_action_shortcut)
            QShortcut(QKeySequence("Enter"), self, activated=self._on_primary_action_shortcut)
            QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._on_start)
            QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self._on_start)
            QShortcut(QKeySequence("Escape"), self, activated=self._on_stop)
            QShortcut(QKeySequence("Delete"), self.results_table, activated=self._delete_current_selected_file)
            QShortcut(QKeySequence("Ctrl+E"), self, activated=self._export_results_to_csv)

        def _on_primary_action_shortcut(self) -> None:
            if self.current_phase == PHASE_IDLE:
                self._on_start()
            elif self.continue_button.isVisible() and self.continue_button.isEnabled():
                self._on_continue()

        def dragEnterEvent(self, event) -> None:
            mime = event.mimeData()
            if mime is not None and mime.hasUrls():
                for url in mime.urls():
                    local = url.toLocalFile()
                    if local and Path(local).is_dir():
                        event.acceptProposedAction()
                        return
            event.ignore()

        def dropEvent(self, event) -> None:
            mime = event.mimeData()
            if mime is None or not mime.hasUrls():
                event.ignore()
                return
            for url in mime.urls():
                local = url.toLocalFile()
                if local and Path(local).is_dir():
                    self.path_edit.setText(local)
                    self.statusBar().showMessage(f"Folder set from drag-and-drop: {local}", 4000)
                    event.acceptProposedAction()
                    return
            event.ignore()

        def _classify_log_level(self, message: str) -> str:
            text_upper = str(message).upper()
            if "[ERROR]" in text_upper:
                return "ERROR"
            if "[WARN]" in text_upper:
                return "WARN"
            if "[DONE]" in text_upper:
                return "DONE"
            return "INFO"

        def _is_log_visible(self, level: str) -> bool:
            if level == "ERROR":
                return self.error_checkbox.isChecked()
            if level == "WARN":
                return self.warn_checkbox.isChecked()
            return self.info_checkbox.isChecked()

        def _log_color(self, level: str) -> str:
            if level == "ERROR":
                return "#ff6b6b" if self._dark_mode else "#b00020"
            if level == "WARN":
                return "#ffb454" if self._dark_mode else "#c56a00"
            if level == "DONE":
                return "#6fdc8c" if self._dark_mode else "#0a7f2e"
            return "#d0d0d0" if self._dark_mode else "#202020"

        def _render_log_entry_html(self, level: str, message: str) -> str:
            escaped = html.escape(message).replace("\n", "<br>")
            color = self._log_color(level)
            return f'<span style="color:{color}; white-space:pre-wrap;">{escaped}</span>'

        def _append_log(self, message: str) -> None:
            level = self._classify_log_level(message)
            self._log_entries.append((level, message))
            if not self._is_log_visible(level):
                return
            self.log_box.moveCursor(QTextCursor.End)
            self.log_box.insertHtml(self._render_log_entry_html(level, message) + "<br>")
            self.log_box.moveCursor(QTextCursor.End)
            self.log_box.ensureCursorVisible()

        def _rebuild_log_view(self) -> None:
            fragments = [
                self._render_log_entry_html(level, message)
                for level, message in self._log_entries
                if self._is_log_visible(level)
            ]
            self.log_box.setHtml("<br>".join(fragments))
            self.log_box.moveCursor(QTextCursor.End)
            self.log_box.ensureCursorVisible()

        def _clear_log(self) -> None:
            self._log_entries.clear()
            self.log_box.clear()
            self.statusBar().showMessage("Log cleared.", 3000)

        def _save_log(self) -> None:
            default_name = f"video_deduplicator_log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            chosen, _ = QFileDialog.getSaveFileName(
                self,
                "Save Log",
                str(_safe_resolve_path(default_name, strict=False)),
                "Text Files (*.txt);;All Files (*)",
            )
            if not chosen:
                return
            try:
                with open(chosen, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(message for _level, message in self._log_entries))
                self.statusBar().showMessage(f"Log saved: {chosen}", 5000)
            except Exception as exc:
                QMessageBox.warning(self, "Save failed", f"Could not save log:\n\n{chosen}\n\n{exc}")

        def _on_browse(self) -> None:
            chosen = QFileDialog.getExistingDirectory(
                self,
                "Select Folder",
                self.path_edit.text().strip() or os.getcwd(),
            )
            if chosen:
                self.path_edit.setText(chosen)

        def _selected_scan_mode(self) -> str:
            return str(self.scan_mode_combo.currentData() or SCAN_MODE_FULL)

        def _on_start(self) -> None:
            if self.current_phase != PHASE_IDLE:
                QMessageBox.information(self, "Workflow in progress", "Finish the current workflow or cancel it first.")
                return

            root_dir = self.path_edit.text().strip()
            if not root_dir:
                QMessageBox.warning(self, "Missing folder", "Please choose a folder to scan.")
                return
            if not Path(root_dir).exists():
                QMessageBox.warning(self, "Invalid folder", "Selected folder does not exist.")
                return

            self._settings.setValue("last_folder", root_dir)
            self._scan_started_at = time.time()
            self._stage_started_at = self._scan_started_at
            self._prepared_phase3_videos = []
            self._selected_delete_paths.clear()
            self._status_info = {"stage": "Starting", "total": 0, "completed": 0, "current_file": ""}
            self._scan_mode = self._selected_scan_mode()

            self.continue_button.setVisible(False)
            self.continue_button.setEnabled(False)
            self.results_table.setSortingEnabled(False)
            self.results_table.setRowCount(0)
            self._log_entries.clear()
            self.log_box.clear()
            self.progress_bar.setValue(0)
            self.summary_label.setText("Summary: —")
            self._update_result_action_states()

            self._root_dir = root_dir
            if self._scan_mode == SCAN_MODE_QUICK:
                self.current_phase = PHASE_CONTENT
                self.continue_button.setEnabled(False)
                self.continue_button.setVisible(False)
                self._start_task("phase3_direct")
            elif self._scan_mode == SCAN_MODE_DURATION_ONLY:

                self.current_phase = PHASE_TIME
                self.continue_button.setEnabled(False)
                self.continue_button.setVisible(False)
                self._start_task("phase2_time")
            else:

                self.current_phase = PHASE_SIZE
                self._start_task("phase1_size")

        def _on_continue(self) -> None:
            if self._worker and self._worker.isRunning():
                return
            if not self._root_dir:
                return

            if self.current_phase == PHASE_SIZE and self._scan_mode == SCAN_MODE_FULL:
                self.current_phase = PHASE_TIME
                self._start_task("phase2_time")
            elif self.current_phase == PHASE_TIME and self._scan_mode == SCAN_MODE_FULL:
                self._start_task("phase3_prepare")

        def _start_task(self, task: str, prepared_video_paths: Optional[list[str]] = None) -> None:
            self._active_task = task
            self.start_button.setEnabled(False)
            self.browse_button.setEnabled(False)
            self.scan_mode_combo.setEnabled(False)
            self.max_threads_spin.setEnabled(False)
            self.duration_tolerance_spin.setEnabled(False)
            self.continue_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.progress_bar.setValue(0)
            self._set_stage("Working...")

            self._worker = WorkerThread(
                task,
                self._root_dir or "",
                self._binaries,
                self.max_threads_spin.value(),
                self.duration_tolerance_spin.value(),
                prepared_video_paths=prepared_video_paths,
                parent=self,
            )
            self._worker.signal_log.connect(self._append_log)
            self._worker.signal_progress.connect(self._on_progress)
            self._worker.signal_stage.connect(self._on_stage_signal)
            self._worker.signal_status.connect(self._on_status_update)
            self._worker.signal_result.connect(self._add_result_row)
            self._worker.signal_done.connect(self._on_task_done)
            self._worker.signal_finished.connect(self._on_finished)
            self._worker.start()

        def _on_stop(self) -> None:
            if self._worker and self._worker.isRunning():
                self._append_log(f"[INFO] {_timestamp()} Stop requested...")
                self.stop_button.setEnabled(False)
                self._worker.request_cancel()

        def _reset_ui(self) -> None:
            self.current_phase = PHASE_IDLE
            self._active_task = None
            self._root_dir = None
            self._prepared_phase3_videos = []
            self.start_button.setEnabled(True)
            self.browse_button.setEnabled(True)
            self.scan_mode_combo.setEnabled(True)
            self.max_threads_spin.setEnabled(True)
            self.duration_tolerance_spin.setEnabled(True)
            self.continue_button.setEnabled(False)
            self.continue_button.setVisible(False)
            self.stop_button.setEnabled(False)
            self.progress_bar.setValue(0)
            self._set_stage("Idle")
            self._update_result_action_states()

        def _set_stage(self, stage: str) -> None:
            stage_str = str(stage).strip() or "Working..."
            if str(self._status_info.get("stage") or "") != stage_str:
                self._stage_started_at = time.time()
            self._status_info["stage"] = stage_str
            self._render_stage_label()

        def _on_stage_signal(self, stage: str) -> None:
            self._set_stage(stage)

        def _on_status_update(self, payload: dict) -> None:
            if not isinstance(payload, dict):
                return
            stage_name = str(payload.get("stage") or self._status_info.get("stage") or "Working...")
            if stage_name != str(self._status_info.get("stage") or ""):
                self._stage_started_at = time.time()
            for key, value in payload.items():
                self._status_info[key] = value
            self._status_info["stage"] = stage_name
            self._render_stage_label()

        def _on_progress(self, value: int) -> None:
            try:
                self.progress_bar.setValue(max(0, min(100, int(value))))
            except Exception:
                self.progress_bar.setValue(0)
            self._render_stage_label()

        def _render_stage_label(self) -> None:
            stage_name = str(self._status_info.get("stage") or "Working...")
            total = int(self._status_info.get("total") or 0)
            completed = int(self._status_info.get("completed") or 0)
            current_file = str(self._status_info.get("current_file") or "").strip()
            current_file = Path(current_file).name if current_file else ""

            message = f"Stage: {stage_name}"
            if total > 0:
                message += f" — {completed}/{total}"
                eta_seconds = None
                if self._stage_started_at and completed > 0 and completed < total:
                    elapsed = max(0.001, time.time() - self._stage_started_at)
                    eta_seconds = (elapsed / completed) * max(0, total - completed)
                if eta_seconds is not None:
                    message += f" — {_format_eta(eta_seconds)}"
                elif completed >= total:
                    message += " — ETA: done"
            if current_file:
                message += f" — {current_file}"
            self.stage_label.setText(message)

        def _collect_result_payloads(self) -> list[dict]:
            payloads: list[dict] = []
            for row in range(self.results_table.rowCount()):
                payload = self._get_row_payload(row)
                if payload:
                    payloads.append(payload)
            return payloads

        def _union_find_masters(self, payloads: list[dict], strategy: str) -> tuple[dict[str, str], dict[str, int]]:
            parent: dict[str, str] = {}
            size_map: dict[str, int] = {}
            area_map: dict[str, int] = {}

            def find(x: str) -> str:
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union(a: str, b: str) -> None:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

            for payload in payloads:
                original = str(payload.get("original") or "").strip()
                duplicate = str(payload.get("duplicate") or "").strip()
                if not original or not duplicate:
                    continue
                for path, size_key, w_key, h_key in (
                    (original, "original_size", "original_width", "original_height"),
                    (duplicate, "duplicate_size", "duplicate_width", "duplicate_height"),
                ):
                    parent.setdefault(path, path)
                    size_map[path] = max(size_map.get(path, 0), int(payload.get(size_key) or 0))
                    area_map[path] = max(
                        area_map.get(path, 0),
                        int(payload.get(w_key) or 0) * int(payload.get(h_key) or 0),
                    )
                union(original, duplicate)

            components: dict[str, list[str]] = {}
            for path in parent:
                components.setdefault(find(path), []).append(path)

            master_of: dict[str, str] = {}
            for members in components.values():
                if strategy == "smaller":
                    master = max(members, key=lambda p: (size_map.get(p, 0), area_map.get(p, 0), p))
                else:
                    master = max(members, key=lambda p: (area_map.get(p, 0), size_map.get(p, 0), p))
                for path in members:
                    master_of[path] = master
            return master_of, size_map

        def _summary_message(self, processed: int, duplicates: int) -> str:

            payloads = self._collect_result_payloads()
            master_of, size_map = self._union_find_masters(payloads, "smaller")
            reclaimable = sum(
                size_map.get(path, 0)
                for path, master in master_of.items()
                if path != master and size_map.get(path, 0) > 0
            )

            elapsed = None
            if self._scan_started_at is not None:
                elapsed = max(0.0, time.time() - self._scan_started_at)

            return (
                f"Scanned {processed} videos | Found {duplicates} duplicate pairs | "
                f"Total space reclaimable: {_human_readable_size(reclaimable)} | "
                f"Scan time: {_format_duration(elapsed or 0.0)}"
            )

        def _finish_scan_summary(self, processed: int, duplicates: int) -> None:
            summary = self._summary_message(processed, duplicates)
            self.summary_label.setText(f"Summary: {summary}")
            self.statusBar().showMessage(summary, 12000)

        def _on_task_done(self, task: str, summary: dict) -> None:
            cancelled = bool(summary.get("cancelled"))
            if cancelled:
                self._append_log(f"[INFO] {_timestamp()} Task cancelled.")
                self._refresh_table_state()
                self._reset_ui()
                return

            if task == "phase1_size":
                if self._root_dir:
                    self._open_directory_in_file_manager(self._root_dir)
                if self._scan_mode == SCAN_MODE_SIZE_ONLY:
                    self.statusBar().showMessage("Size Match Only workflow complete.", 5000)
                    self._reset_ui()
                    return

                self.continue_button.setVisible(True)
                self.continue_button.setEnabled(True)
                self._append_log("Phase 1 Complete. Click Continue when ready.")
            elif task == "phase2_time":
                if self._root_dir:
                    self._open_directory_in_file_manager(self._root_dir)
                if self._scan_mode == SCAN_MODE_DURATION_ONLY:
                    self.statusBar().showMessage("Duration Match Only workflow complete.", 5000)
                    self._reset_ui()
                    return

                self.continue_button.setVisible(True)
                self.continue_button.setEnabled(True)
                self._append_log("Phase 2 Complete. Click Continue to proceed.")
            elif task == "phase3_prepare":
                self._prepared_phase3_videos = list(summary.get("prepared_videos") or [])
                reply = QMessageBox.question(
                    self,
                    "Proceed to Content Scan?",
                    "Phase 1 and Phase 2 are complete. Do you want to continue with the content scan using the prepared file list?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )
                if reply != QMessageBox.Yes:
                    self._append_log("[INFO] Advanced content scan skipped. Workflow complete.")
                    self._reset_ui()
                    return
                self.current_phase = PHASE_CONTENT
                self.results_table.setSortingEnabled(False)
                self.results_table.setRowCount(0)
                self._selected_delete_paths.clear()
                self.progress_bar.setValue(0)
                self._start_task("phase3_content", prepared_video_paths=self._prepared_phase3_videos)
            elif task in {"phase3_content", "phase3_direct"}:
                processed = int(summary.get("processed") or 0)
                duplicates = int(summary.get("duplicates") or 0)
                self.results_table.setSortingEnabled(self.results_table.rowCount() > 0)
                self._refresh_table_state()
                self._finish_scan_summary(processed, duplicates)
                if processed > 0 and duplicates == 0:
                    QMessageBox.information(
                        self,
                        "Scan Complete",
                        f"Scan complete. No duplicates found among {processed} processed videos.",
                    )
                self._reset_ui()

        def _on_finished(self) -> None:
            if self.sender() is self._worker:
                self.stop_button.setEnabled(False)
                self._worker = None
                self._update_result_action_states()

        def _open_directory_in_file_manager(self, path: str) -> None:
            target = _safe_resolve_path(path, strict=False)
            if not _safe_exists(target):
                self._append_log(f"[WARN] {_timestamp()} Folder not found: {path}")
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

        def _open_file_location(self, path: str) -> None:
            path_str = str(path).strip()
            if not path_str:
                return
            p = _safe_resolve_path(path_str, strict=False)
            if not _safe_exists(p):
                self._append_log(f"[WARN] {_timestamp()} File not found: {path_str}")
                QMessageBox.warning(self, "File not found", f"File does not exist:\n\n{path_str}")
                return

            ok, mechanism = reveal_in_file_manager(p)
            if not ok:
                self._append_log(f"[WARN] {_timestamp()} Failed to reveal in file manager: {mechanism}")
                QMessageBox.warning(
                    self,
                    "Reveal failed",
                    f"Could not reveal the file in the system file manager.\n\n{path_str}\n\n{mechanism}",
                )

        def _play_video(self, path: str) -> None:
            path_str = str(path).strip()
            if not path_str:
                return
            p = _safe_resolve_path(path_str, strict=False)
            if not _safe_exists(p):
                self._append_log(f"[WARN] {_timestamp()} File not found: {path_str}")
                QMessageBox.warning(self, "File not found", f"File does not exist:\n\n{path_str}")
                return
            try:
                if os.name == "nt":
                    os.startfile(str(p))
                else:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
            except Exception as exc:
                self._append_log(f"[WARN] {_timestamp()} Failed to play video: {exc}")

        def _open_mediainfo(self, path: str) -> None:
            path_str = str(path).strip()
            if not path_str:
                return
            p = _safe_resolve_path(path_str, strict=False)
            if not _safe_exists(p):
                self._append_log(f"[WARN] {_timestamp()} File not found: {path_str}")
                QMessageBox.warning(self, "File not found", f"File does not exist:\n\n{path_str}")
                return

            exe = shutil.which("MediaInfo.exe") or shutil.which("mediainfo")
            if not exe:
                self._append_log(f"[WARN] {_timestamp()} MediaInfo not found in PATH.")
                QMessageBox.information(
                    self,
                    "MediaInfo Not Found",
                    "MediaInfo was not found in your PATH.\n\nInstall MediaInfo CLI or add MediaInfo.exe to PATH.",
                )
                return

            if os.name != "nt" and Path(exe).name.lower() == "mediainfo":
                try:
                    proc = subprocess.run(
                        [exe, str(p)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=20,
                    )
                    output = (proc.stdout or proc.stderr or "").strip()
                    if proc.returncode != 0:
                        raise RuntimeError(output or f"mediainfo rc={proc.returncode}")
                    QMessageBox.information(self, "MediaInfo", output or f"No MediaInfo output for:\n\n{p}")
                except Exception as exc:
                    self._append_log(f"[WARN] {_timestamp()} Failed to run MediaInfo: {exc}")
                    QMessageBox.warning(self, "MediaInfo failed", f"Could not run MediaInfo:\n\n{path_str}\n\n{exc}")
                return

            try:
                subprocess.Popen([exe, str(p)])
            except Exception as exc:
                self._append_log(f"[WARN] {_timestamp()} Failed to open MediaInfo: {exc}")

        def _probe_file_info(self, path: str) -> dict:
            path_str = str(path).strip()
            p = _safe_resolve_path(path_str, strict=False)
            file_size = 0
            try:
                file_size = int(p.stat().st_size)
            except Exception:
                file_size = 0

            duration_s = 0.0
            width = 0
            height = 0
            validator = FFprobeValidator(self._binaries.ffprobe_path, timeout_s=15)
            meta, _err = validator.validate(p)
            if meta is not None:
                duration_s = float(meta.duration_s)
                width = int(meta.width)
                height = int(meta.height)

            return {
                "path": str(p),
                "file_size": file_size,
                "duration_s": duration_s,
                "width": width,
                "height": height,
            }

        def _get_row_payload(self, row: int) -> dict:
            for col in (0, 1):
                item = self.results_table.item(row, col)
                if item is None:
                    continue
                payload = item.data(PAYLOAD_ROLE)
                if isinstance(payload, dict):
                    return payload
            return {}

        def _payload_info_for_path(self, payload: dict, path: str) -> Optional[dict]:
            path_str = str(path)
            if path_str == str(payload.get("original") or ""):
                return {
                    "path": path_str,
                    "file_size": int(payload.get("original_size") or 0),
                    "duration_s": float(payload.get("original_duration_s") or 0.0),
                    "width": int(payload.get("original_width") or 0),
                    "height": int(payload.get("original_height") or 0),
                }
            if path_str == str(payload.get("duplicate") or ""):
                return {
                    "path": path_str,
                    "file_size": int(payload.get("duplicate_size") or 0),
                    "duration_s": float(payload.get("duplicate_duration_s") or 0.0),
                    "width": int(payload.get("duplicate_width") or 0),
                    "height": int(payload.get("duplicate_height") or 0),
                }
            return None

        def _lookup_file_info(self, path: str, payload: Optional[dict] = None) -> dict:
            if payload:
                info = self._payload_info_for_path(payload, path)
                if info is not None:
                    return info
            return self._probe_file_info(path)

        def _copy_file_info(self, path: str, payload: Optional[dict] = None) -> None:
            info = self._lookup_file_info(path, payload)
            resolution = (
                f"{int(info.get('width') or 0)}x{int(info.get('height') or 0)}"
                if int(info.get("width") or 0) > 0 and int(info.get("height") or 0) > 0
                else "N/A"
            )
            text = "\n".join(
                [
                    f"Filename: {Path(str(info.get('path') or path)).name}",
                    f"Path: {str(info.get('path') or path)}",
                    f"Size: {_human_readable_size(int(info.get('file_size') or 0))}",
                    f"Duration: {_format_duration(float(info.get('duration_s') or 0.0))}",
                    f"Resolution: {resolution}",
                ]
            )
            QApplication.clipboard().setText(text)
            self.statusBar().showMessage("File info copied to clipboard.", 4000)

        def _delete_paths(self, paths: list[str]) -> None:
            unique_paths = sorted({str(path).strip() for path in paths if str(path).strip()})
            if not unique_paths:
                return

            try:
                from send2trash import send2trash as _send2trash

                send2trash_available = True
            except Exception:
                _send2trash = None
                send2trash_available = False

            if len(unique_paths) == 1:
                msg = f"Are you sure you want to delete this file?\n\n{unique_paths[0]}"
            else:
                msg = (
                    f"Are you sure you want to delete these {len(unique_paths)} files?\n\n"
                    + "\n".join(unique_paths[:10])
                )
                if len(unique_paths) > 10:
                    msg += f"\n... and {len(unique_paths) - 10} more."

            if not send2trash_available:
                msg += "\n\n(send2trash not available; this may permanently delete the file.)"

            reply = QMessageBox.question(
                self,
                "Confirm Deletion",
                msg,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

            deleted = 0
            failed = 0
            for path_str in unique_paths:
                p = _safe_resolve_path(path_str, strict=False)
                if not _safe_exists(p):
                    self._append_log(f"[WARN] {_timestamp()} File not found: {path_str}")
                    self._selected_delete_paths.discard(str(p))
                    failed += 1
                    continue
                try:
                    if _send2trash is not None:
                        _send2trash(str(p))
                    else:
                        os.remove(str(p))
                    deleted += 1
                    self._selected_delete_paths.discard(str(p))
                    self._append_log(f"[INFO] {_timestamp()} Deleted: {path_str}")
                except Exception as exc:
                    failed += 1
                    self._append_log(f"[WARN] {_timestamp()} Delete failed for {path_str}: {exc}")

            self._refresh_table_state()
            self._update_result_action_states()
            if deleted > 0:
                self.statusBar().showMessage(f"Deleted {deleted} file(s).", 5000)
            if failed > 0:
                QMessageBox.warning(self, "Delete incomplete", f"Deleted {deleted} file(s); {failed} failed.")

        def _delete_file(self, path: str) -> None:
            self._delete_paths([path])

        def _delete_selected_files(self) -> None:
            self._delete_paths(sorted(self._selected_delete_paths))

        def _delete_current_selected_file(self) -> None:
            row = self.results_table.currentRow()
            col = self.results_table.currentColumn()
            if row < 0:
                return
            if col not in (0, 1):
                col = 1 if self._get_raw_path(row, 1) else 0
            path = self._get_raw_path(row, col)
            if path:
                self._delete_file(path)

        def _get_raw_path(self, row: int, col: int) -> str:
            item = self.results_table.item(row, col)
            if item is None:
                return ""
            raw = item.data(PATH_ROLE)
            if isinstance(raw, str) and raw.strip():
                return raw
            return item.text().replace(" (Deleted)", "").strip()

        def _apply_row_visual_state(self, row: int, *, exists_a: bool, exists_b: bool) -> None:
            path_a = self._get_raw_path(row, 0)
            path_b = self._get_raw_path(row, 1)
            row_brush = self._conflict_brush if (exists_a and exists_b) else self._resolved_brush

            for col in range(self.results_table.columnCount()):
                item = self.results_table.item(row, col)
                if item is None:
                    continue
                item.setBackground(row_brush)
                item.setForeground(QBrush())
                font = item.font()
                if col in (0, 1):
                    font.setBold(False)
                item.setFont(font)

            for col, exists in ((0, exists_a), (1, exists_b)):
                item = self.results_table.item(row, col)
                if item is None:
                    continue
                raw_path = self._get_raw_path(row, col)
                item.setData(PATH_ROLE, raw_path)
                item.setText(raw_path + ("" if exists else " (Deleted)"))
                font = item.font()
                font.setStrikeOut(not exists)
                item.setFont(font)

            for col, selected_path in ((0, path_a), (1, path_b)):
                if selected_path and selected_path in self._selected_delete_paths:
                    item = self.results_table.item(row, col)
                    if item is not None:
                        item.setBackground(self._delete_selection_brush)
                        item.setForeground(self._delete_selection_foreground)
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)

        def _refresh_table_state(self) -> None:
            rows = self.results_table.rowCount()
            if rows <= 0:
                self._update_result_action_states()
                return

            if self._worker is not None and self._worker.isRunning():
                self._update_result_action_states()
                return

            unique_paths: set[str] = set()
            for row in range(rows):
                for col in (0, 1):
                    raw_path = self._get_raw_path(row, col)
                    if raw_path:
                        unique_paths.add(raw_path)

            existence_map = {path: _safe_exists(path) for path in unique_paths}

            for row in range(rows):
                path_a = self._get_raw_path(row, 0)
                path_b = self._get_raw_path(row, 1)
                exists_a = existence_map.get(path_a, False) if path_a else False
                exists_b = existence_map.get(path_b, False) if path_b else False
                self._apply_row_visual_state(row, exists_a=exists_a, exists_b=exists_b)

            self._update_result_action_states()

        def _build_file_tooltip(
            self,
            path: str,
            file_size: int,
            duration_s: float,
            width: int,
            height: int,
        ) -> str:
            resolution = f"{width}x{height}" if width > 0 and height > 0 else "N/A"
            return "\n".join(
                [
                    path,
                    f"Size: {_human_readable_size(file_size)}",
                    f"Duration: {_format_duration(duration_s)}",
                    f"Resolution: {resolution}",
                ]
            )

        def _make_path_item(
            self,
            path: str,
            payload: dict,
            *,
            file_size: int,
            duration_s: float,
            width: int,
            height: int,
        ) -> SortableTableWidgetItem:
            item = SortableTableWidgetItem(path)
            item.setData(PATH_ROLE, path)
            item.setData(SORT_ROLE, _normalized_text(path).casefold())
            item.setData(PAYLOAD_ROLE, payload)
            item.setToolTip(self._build_file_tooltip(path, file_size, duration_s, width, height))
            return item

        def _make_value_item(self, text: str, sort_value) -> SortableTableWidgetItem:
            item = SortableTableWidgetItem(text)
            item.setData(SORT_ROLE, sort_value)
            return item

        def _update_result_action_states(self) -> None:
            has_results = self.results_table.rowCount() > 0
            worker_running = self._worker is not None and self._worker.isRunning()
            self.export_button.setEnabled(has_results)
            self.auto_select_smaller_button.setEnabled(has_results and not worker_running)
            self.auto_select_lower_res_button.setEnabled(has_results and not worker_running)
            self.delete_selected_button.setEnabled(bool(self._selected_delete_paths) and not worker_running)

        def _on_results_double_clicked(self, row: int, col: int) -> None:
            if col not in (0, 1):
                return
            path = self._get_raw_path(row, col)
            if path:
                self._open_file_location(path)

        def _on_results_context_menu(self, pos) -> None:
            item = self.results_table.itemAt(pos)
            if item is None:
                return
            row = item.row()
            col = item.column()
            payload = self._get_row_payload(row)

            if col in (0, 1):
                path = self._get_raw_path(row, col)
            else:
                path = self._get_raw_path(row, 1) or self._get_raw_path(row, 0)
            if not path:
                return

            menu = QMenu(self)
            action_play = menu.addAction("Play Video")
            action_reveal = menu.addAction("Reveal in File Manager")
            action_copy_info = menu.addAction("Copy File Info")
            action_mediainfo = menu.addAction("MediaInfo")
            menu.addSeparator()
            action_delete = menu.addAction("Delete File")

            chosen = menu.exec_(self.results_table.viewport().mapToGlobal(pos))
            if chosen is None:
                return

            if chosen == action_play:
                self._play_video(path)
            elif chosen == action_reveal:
                self._open_file_location(path)
            elif chosen == action_copy_info:
                self._copy_file_info(path, payload)
            elif chosen == action_mediainfo:
                self._open_mediainfo(path)
            elif chosen == action_delete:
                self._delete_file(path)

        def _add_result_row(self, payload: dict) -> None:
            original = str(payload.get("original") or "")
            duplicate = str(payload.get("duplicate") or "")
            original_size = int(payload.get("original_size") or 0)
            duplicate_size = int(payload.get("duplicate_size") or 0)
            original_duration = float(payload.get("original_duration_s") or 0.0)
            duplicate_duration = float(payload.get("duplicate_duration_s") or 0.0)
            original_width = int(payload.get("original_width") or 0)
            original_height = int(payload.get("original_height") or 0)
            duplicate_width = int(payload.get("duplicate_width") or 0)
            duplicate_height = int(payload.get("duplicate_height") or 0)
            similarity = float(payload.get("similarity") or 0.0)
            offset_s = float(payload.get("offset_s") or 0.0)

            row = self.results_table.rowCount()
            self.results_table.insertRow(row)

            item_original = self._make_path_item(
                original,
                payload,
                file_size=original_size,
                duration_s=original_duration,
                width=original_width,
                height=original_height,
            )
            item_duplicate = self._make_path_item(
                duplicate,
                payload,
                file_size=duplicate_size,
                duration_s=duplicate_duration,
                width=duplicate_width,
                height=duplicate_height,
            )

            self.results_table.setItem(row, 0, item_original)
            self.results_table.setItem(row, 1, item_duplicate)
            self.results_table.setItem(row, 2, self._make_value_item(_human_readable_size(original_size), original_size))
            self.results_table.setItem(row, 3, self._make_value_item(_human_readable_size(duplicate_size), duplicate_size))
            self.results_table.setItem(row, 4, self._make_value_item(_format_duration(original_duration), original_duration))
            self.results_table.setItem(row, 5, self._make_value_item(_format_duration(duplicate_duration), duplicate_duration))
            self.results_table.setItem(row, 6, self._make_value_item(f"{similarity:.2f}", similarity))
            sign = "+" if offset_s >= 0 else "-"
            self.results_table.setItem(row, 7, self._make_value_item(f"{sign}{abs(offset_s):.1f}s", offset_s))

            self._apply_row_visual_state(row, exists_a=True, exists_b=True)
            self._update_result_action_states()
            self.statusBar().showMessage(f"Found {self.results_table.rowCount()} duplicate pair(s).", 3000)

        def _auto_select_by_strategy(self, strategy: str, label: str) -> None:
            payloads = self._collect_result_payloads()
            master_of, _size_map = self._union_find_masters(payloads, strategy)
            self._selected_delete_paths = {
                path for path, master in master_of.items() if path != master
            }
            self._refresh_table_state()
            self.statusBar().showMessage(
                f"Auto-selected {len(self._selected_delete_paths)} file(s) to delete by {label}.",
                5000,
            )

        def _auto_select_keep_larger(self) -> None:
            self._auto_select_by_strategy("smaller", "size")

        def _auto_select_keep_higher_res(self) -> None:
            self._auto_select_by_strategy("lower_res", "resolution")

        def _export_results_to_csv(self) -> None:
            if self.results_table.rowCount() <= 0:
                return
            default_name = f"video_deduplicator_results_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            chosen, _ = QFileDialog.getSaveFileName(
                self,
                "Export Results to CSV",
                str(_safe_resolve_path(default_name, strict=False)),
                "CSV Files (*.csv);;All Files (*)",
            )
            if not chosen:
                return

            headers = [self.results_table.horizontalHeaderItem(col).text() for col in range(self.results_table.columnCount())]
            try:
                with open(chosen, "w", newline="", encoding="utf-8-sig") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(headers)
                    for row in range(self.results_table.rowCount()):
                        writer.writerow(
                            [
                                self._get_raw_path(row, 0),
                                self._get_raw_path(row, 1),
                                self.results_table.item(row, 2).text() if self.results_table.item(row, 2) else "",
                                self.results_table.item(row, 3).text() if self.results_table.item(row, 3) else "",
                                self.results_table.item(row, 4).text() if self.results_table.item(row, 4) else "",
                                self.results_table.item(row, 5).text() if self.results_table.item(row, 5) else "",
                                self.results_table.item(row, 6).text() if self.results_table.item(row, 6) else "",
                                self.results_table.item(row, 7).text() if self.results_table.item(row, 7) else "",
                            ]
                        )
                self.statusBar().showMessage(f"Results exported: {chosen}", 5000)
            except Exception as exc:
                QMessageBox.warning(self, "Export failed", f"Could not export results:\n\n{chosen}\n\n{exc}")

        def closeEvent(self, event) -> None:
            if self._worker and self._worker.isRunning():
                reply = QMessageBox.question(
                    self,
                    "Scan in progress",
                    "A scan is still running. Cancel and exit?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )
                if reply != QMessageBox.Yes:
                    event.ignore()
                    return
                self._worker.request_cancel()
                progress_dialog = QProgressDialog("Cancelling scan...", None, 0, 0, self)
                progress_dialog.setWindowModality(Qt.ApplicationModal)
                progress_dialog.setCancelButton(None)
                progress_dialog.setMinimumDuration(0)
                progress_dialog.show()

                deadline = time.time() + 30.0
                while self._worker.isRunning() and time.time() < deadline:
                    QApplication.processEvents()
                    self._worker.wait(200)

                progress_dialog.close()
                if self._worker.isRunning():
                    QMessageBox.warning(
                        self,
                        "Still stopping",
                        "The scan is still shutting down. Please wait a moment and try closing again.",
                    )
                    event.ignore()
                    return
            event.accept()

    app = QApplication(sys.argv)
    window = MainWindow(binaries)
    window.show()
    return app.exec_()

def main() -> int:
    try:
        binaries = FFmpegLocator.locate_or_raise()
    except FileNotFoundError:
        from PyQt5.QtWidgets import QApplication, QMessageBox

        app = QApplication(sys.argv)
        QMessageBox.critical(
            None,
            "FFmpeg Missing",
            "FFmpeg was not found.\n\n"
            "Install ffmpeg/ffprobe in PATH, or place matching binaries next to this script or in a ./bin folder.",
        )
        return 1

    return run_gui(binaries)

if __name__ == "__main__":
    raise SystemExit(main())
