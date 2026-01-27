from __future__ import annotations
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

class Thresholds:

    MIN_RESOLUTION_PX: int = 480
    PHASH_BITS: int = 64
    DURATION_TOLERANCE_S: float = 0.1
    MTIME_TOLERANCE_S: float = 2.0 
    PROGRESS_INTERVAL: int = 25
    COARSE_STEP: int = 5
    COARSE_THRESHOLD: int = 200
    DURATION_PADDING_S: int = 60
    FRAME_SIZE: int = 32

try:
    import imagehash
except Exception:
    imagehash = None

def _timestamp() -> str:
    return time.strftime("%H:%M:%S")

def _noop(*_args, **_kwargs) -> None:
    return None

@dataclass(frozen=True, slots=True)
class FFmpegBinaries:
    ffmpeg_path: str
    ffprobe_path: str

class FFmpegLocator:
    @staticmethod
    def _candidate_dirs() -> list[Path]:
        cwd = Path(os.getcwd()).resolve()
        script_dir = Path(__file__).resolve().parent
        return [cwd, cwd / "bin", script_dir, script_dir / "bin"]

    @staticmethod
    def _binary_name(stem: str) -> str:
        return f"{stem}.exe" if os.name == "nt" else stem

    @classmethod
    def locate_or_raise(cls) -> FFmpegBinaries:
        ffmpeg_name = cls._binary_name("ffmpeg")
        ffprobe_name = cls._binary_name("ffprobe")

        for base_dir in cls._candidate_dirs():
            ffmpeg_path = base_dir / ffmpeg_name
            ffprobe_path = base_dir / ffprobe_name
            if ffmpeg_path.is_file() and ffprobe_path.is_file():
                return FFmpegBinaries(str(ffmpeg_path), str(ffprobe_path))

        candidates = "\n".join(f"- {p}" for p in cls._candidate_dirs())
        raise FileNotFoundError(
            "Portable FFmpeg binaries not found.\n"
            f"Expected `{ffmpeg_name}` and `{ffprobe_name}` in one of:\n{candidates}"
        )

class VideoScanner:
    def __init__(self, on_log: Callable[[str], None] = _noop):
        self._on_log = on_log

    def scan(self, root_dir: str | Path) -> list[Path]:
        root_path = Path(root_dir).expanduser().resolve()
        if not root_path.exists():
            self._on_log(f"[ERROR] {root_path} does not exist.")
            return []
        if not root_path.is_dir():
            self._on_log(f"[ERROR] {root_path} is not a directory.")
            return []

        video_paths: list[Path] = []
        trash_lower = TRASH_LOW_RES_DIRNAME.lower()
        group_lower = GROUP_DIR_PREFIX.lower()
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [
                d
                for d in dirnames
                if d.lower() != trash_lower and not d.lower().startswith(group_lower)
            ]
            for filename in filenames:
                suffix = Path(filename).suffix.lower()
                if suffix in VIDEO_EXTENSIONS:
                    video_paths.append((Path(dirpath) / filename).resolve())

        video_paths.sort(key=lambda p: str(p).lower())
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
        if not Path(path_str).is_file():
            return None, "Not a file."

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        cmd = [
            self._ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=width,height",
            "-of",
            "json",
            path_str,
        ]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self._timeout_s,
                creationflags=creationflags,
            )
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

        duration_str = ((payload.get("format") or {}).get("duration") or "").strip()
        try:
            duration_s = float(duration_str)
        except Exception:
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
    max_offset_s: int = 600
    min_overlap_frames: int = 10

@dataclass(frozen=True, slots=True)
class EngineConfig:
    fingerprint: FingerprintConfig = FingerprintConfig()
    match: MatchConfig = MatchConfig()
    max_threads: int = 3

@dataclass(frozen=True, slots=True)
class VideoFingerprint:
    path: str
    duration_s: float
    hashes: np.ndarray
    consensus_hash: int
    file_size: int

@dataclass(frozen=True, slots=True)
class EngineCallbacks:
    log: Callable[[str], None] = _noop
    progress: Callable[[int], None] = _noop
    stage: Callable[[str], None] = _noop
    result: Callable[[dict], None] = _noop
    should_cancel: Callable[[], bool] = lambda: False

class DedupDatabase:
    def __init__(self, db_path: str | Path, *, commit_every: int = 10):
        self._db_path = Path(db_path).expanduser().resolve()
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
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS video_cache (
                    path TEXT PRIMARY KEY,
                    file_size INTEGER,
                    mtime REAL,
                    duration REAL,
                    phash_data BLOB,
                    consensus_hash INTEGER
                );
                """
            )
            self._conn.commit()

    def get_fingerprint(
        self, path: str, file_size: int, mtime: float
    ) -> Optional[tuple[float, np.ndarray, int]]:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT file_size, mtime, duration, phash_data, consensus_hash FROM video_cache WHERE path = ?",
                    (path,),
                ).fetchone()
        except sqlite3.Error:
            return None

        if row is None:
            return None

        row_size = int(row["file_size"] or -1)
        row_mtime = float(row["mtime"] or -1.0)
        if row_size != int(file_size):
            return None
        if not (abs(row_mtime - float(mtime)) < Thresholds.MTIME_TOLERANCE_S):
            return None

        blob = row["phash_data"]
        consensus = row["consensus_hash"]
        duration = row["duration"]
        if blob is None or consensus is None or duration is None:
            return None

        try:
            blob_bytes = bytes(blob) if isinstance(blob, memoryview) else blob
            hashes = pickle.loads(blob_bytes)
        except Exception:
            try:
                self.delete(path)
            except Exception:
                pass
            return None

        if not isinstance(hashes, np.ndarray):
            try:
                self.delete(path)
            except Exception:
                pass
            return None

        return float(duration), hashes.astype(np.uint64, copy=False), int(consensus)

    def upsert_fingerprint(
        self,
        *,
        path: str,
        file_size: int,
        mtime: float,
        duration: float,
        hashes: np.ndarray,
        consensus_hash: int,
    ) -> None:
        blob = sqlite3.Binary(pickle.dumps(hashes, protocol=pickle.HIGHEST_PROTOCOL))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO video_cache(path, file_size, mtime, duration, phash_data, consensus_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    file_size=excluded.file_size,
                    mtime=excluded.mtime,
                    duration=excluded.duration,
                    phash_data=excluded.phash_data,
                    consensus_hash=excluded.consensus_hash
                """,
                (path, int(file_size), float(mtime), float(duration), blob, int(consensus_hash)),
            )
            self._pending_writes += 1
            if self._pending_writes >= self._commit_every:
                self._conn.commit()
                self._pending_writes = 0

    def delete(self, path: str) -> None:
        with self._lock:
            try:
                self._conn.execute("DELETE FROM video_cache WHERE path = ?", (path,))
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
                self._conn.close()
            except Exception:
                pass

    def cleanup_stale_entries(self, valid_paths: set[str]) -> int:

        if not valid_paths:
            return 0

        with self._lock:
            try:
                cursor = self._conn.execute("SELECT path FROM video_cache")
                cached_paths = {row[0] for row in cursor.fetchall()}
            except sqlite3.Error:
                return 0

            stale_paths = cached_paths - valid_paths
            if not stale_paths:
                return 0

            for path in stale_paths:
                try:
                    self._conn.execute("DELETE FROM video_cache WHERE path = ?", (path,))
                except sqlite3.Error:
                    continue

            self._conn.commit()
            self._pending_writes = 0
            return len(stale_paths)

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
    consensus_bits = (bit_means >= 0.5).astype(np.uint8, copy=False)
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

    full_range_low = -(len_b - 1)
    full_range_high = len_a - 1
    low = max(full_range_low, -max_offset_frames)
    high = min(full_range_high, max_offset_frames)

    best_similarity = 0.0
    best_offset = 0
    best_overlap = 0

    offset_span = high - low
    coarse_step = 1
    if offset_span > Thresholds.COARSE_THRESHOLD:
        coarse_step = Thresholds.COARSE_STEP

    def eval_offset(offset_frames: int) -> tuple[float, int]:
        a_start = max(0, -offset_frames)
        b_start = max(0, offset_frames)
        overlap = min(len_a - a_start, len_b - b_start)
        if overlap < min_overlap_frames:
            return 0.0, 0
        a_seg = a[a_start : a_start + overlap]
        b_seg = b[b_start : b_start + overlap]
        mean_dist = float(bitwise_count_uint64(np.bitwise_xor(a_seg, b_seg)).mean())
        return 1.0 - (mean_dist / float(Thresholds.PHASH_BITS)), overlap

    top_candidates: list[tuple[float, int, int]] = []
    for offset_frames in range(low, high + 1, coarse_step):
        similarity, overlap = eval_offset(offset_frames)
        if overlap < min_overlap_frames:
            continue

        if similarity > best_similarity:
            best_similarity = similarity
            best_offset = offset_frames
            best_overlap = overlap
            if early_exit_threshold > 0 and best_similarity >= early_exit_threshold:
                return best_similarity, best_offset, best_overlap

        if coarse_step > 1:
            top_candidates.append((similarity, offset_frames, overlap))
            top_candidates.sort(reverse=True, key=lambda x: x[0])
            del top_candidates[3:]

    if coarse_step > 1 and top_candidates:
        refined_offsets: set[int] = set()
        for _sim, candidate_offset, _overlap in top_candidates:
            for off in range(candidate_offset - coarse_step, candidate_offset + coarse_step + 1):
                if low <= off <= high:
                    refined_offsets.add(off)

        for offset_frames in sorted(refined_offsets):
            similarity, overlap = eval_offset(offset_frames)
            if overlap < min_overlap_frames:
                continue
            if similarity > best_similarity:
                best_similarity = similarity
                best_offset = offset_frames
                best_overlap = overlap
                if early_exit_threshold > 0 and best_similarity >= early_exit_threshold:
                    return best_similarity, best_offset, best_overlap

    return best_similarity, best_offset, best_overlap

def _fingerprint_video_worker(
    video_path: str,
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

    path_obj = Path(video_path).expanduser()
    try:
        path_obj = path_obj.resolve()
    except Exception:
        path_obj = Path(os.path.abspath(str(path_obj)))
    path_str = str(path_obj)
    video_name = path_obj.name
    on_log(f"[START] Processing: {video_name}")

    if cancel_event.is_set():
        on_log(f"[DONE] Finished: {video_name} (cancelled)")
        return {"path": path_str, "ok": False, "cancelled": True}

    try:
        stat = path_obj.stat()
        file_size = int(stat.st_size)
        mtime = float(stat.st_mtime)
    except Exception as exc:
        on_log(f"[DONE] Finished: {video_name} (skipped: stat failed: {exc})")
        return {"path": path_str, "ok": False, "error": f"stat failed: {exc}"}

    cached = db.get_fingerprint(path_str, file_size, mtime)
    if cached is not None:
        duration_s, hashes_arr, consensus = cached
        on_log(f"[HASH] Loaded {int(hashes_arr.size)} frames from cache for: {video_name}")
        on_log(f"[DONE] Finished: {video_name} (cached)")
        return {
            "path": path_str,
            "ok": True,
            "duration_s": float(duration_s),
            "hashes": hashes_arr,
            "consensus": int(consensus),
            "file_size": file_size,
            "mtime": mtime,
            "cached": True,
        }

    validator = FFprobeValidator(binaries.ffprobe_path, timeout_s=fingerprint_config.ffprobe_timeout_s)
    meta, err = validator.validate(path_str)
    if meta is None:
        on_log(f"[DONE] Finished: {video_name} (skipped: {err or 'Invalid video'})")
        return {"path": path_str, "ok": False, "error": err or "Invalid video."}

    frame_size = int(fingerprint_config.frame_size)
    if frame_size != Thresholds.FRAME_SIZE:
        on_log(f"[DONE] Finished: {video_name} (skipped: frame_size must be {Thresholds.FRAME_SIZE})")
        return {"path": path_str, "ok": False, "error": f"frame_size must be {Thresholds.FRAME_SIZE}."}
    bytes_per_frame = frame_size * frame_size

    sample_fps = float(fingerprint_config.sample_fps)
    max_frames = int(fingerprint_config.max_frames)
    if max_frames <= 0:
        on_log(f"[DONE] Finished: {video_name} (skipped: max_frames must be > 0)")
        return {"path": path_str, "ok": False, "error": "max_frames must be > 0."}

    vf = f"fps={sample_fps},scale={frame_size}:{frame_size}:flags=bicubic,format=gray"
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
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc: subprocess.Popen[bytes] | None = None
    hashes: list[int] = []
    capture_stderr = os.environ.get("VIDEO_DEDUP_DEBUG_FFMPEG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    stderr_buffer: list[bytes] = []
    stderr_thread: threading.Thread | None = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
            creationflags=creationflags,
        )

        if capture_stderr and proc.stderr is not None:
            stderr_pipe = proc.stderr

            def _drain_stderr(pipe) -> None:
                try:
                    data = pipe.read()
                    if data:
                        stderr_buffer.append(data)
                except Exception:
                    pass

            stderr_thread = threading.Thread(target=_drain_stderr, args=(stderr_pipe,), daemon=True)
            stderr_thread.start()

        assert proc.stdout is not None
        while len(hashes) < max_frames:
            if cancel_event.is_set():
                _kill_chain(proc)
                on_log(f"[DONE] Finished: {video_name} (cancelled)")
                return {"path": path_str, "ok": False, "cancelled": True}

            raw = proc.stdout.read(bytes_per_frame)
            if len(raw) != bytes_per_frame:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((frame_size, frame_size))
            hashes.append(phash_uint64_from_gray_32x32(frame))

        on_log(f"[HASH] Extracted {len(hashes)} frames for: {video_name}")

        if fingerprint_config.ffmpeg_timeout_s and fingerprint_config.ffmpeg_timeout_s > 0:
            proc.wait(timeout=fingerprint_config.ffmpeg_timeout_s)
        else:
            proc.wait()

        if stderr_thread is not None:
            stderr_thread.join(timeout=0.5)
        stderr = b"".join(stderr_buffer).decode(errors="replace").strip()
        if proc.returncode != 0 and not hashes:
            on_log(f"[DONE] Finished: {video_name} (failed: {stderr or f'ffmpeg rc={proc.returncode}'})")
            return {"path": path_str, "ok": False, "error": stderr or f"ffmpeg rc={proc.returncode}"}
    except subprocess.TimeoutExpired:
        if proc:
            _kill_chain(proc)
        on_log(f"[DONE] Finished: {video_name} (failed: ffmpeg timeout)")
        return {"path": path_str, "ok": False, "error": "ffmpeg timeout."}
    except Exception as exc:
        if proc:
            _kill_chain(proc)
        on_log(f"[DONE] Finished: {video_name} (failed: ffmpeg error)")
        return {"path": path_str, "ok": False, "error": f"ffmpeg failed: {exc}"}
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
        return {"path": path_str, "ok": False, "error": "No frames extracted."}

    hashes_arr = np.array(hashes, dtype=np.uint64)
    consensus = consensus_hash_uint64(hashes_arr)
    duration_s = float(meta.duration_s)
    try:
        db.upsert_fingerprint(
            path=path_str,
            file_size=file_size,
            mtime=mtime,
            duration=duration_s,
            hashes=hashes_arr,
            consensus_hash=int(consensus),
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
        "mtime": mtime,
        "cached": False,
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

        root_path = Path(root_dir).expanduser().resolve()
        folders = sorted([p for p in root_path.glob(f"{prefix}*") if p.is_dir()], key=lambda p: p.name.lower())
        if not folders:
            return {"moved_back": 0, "removed_folders": 0}

        moved_back = 0
        removed_folders = 0
        total_files = sum(1 for folder in folders for path in folder.glob("*") if path.is_file())
        done = 0

        log(f"[INFO] {_timestamp()} Cleaning up {len(folders)} folder(s) matching {prefix}* ...")
        for folder in folders:
            for file_path in [p for p in folder.iterdir() if p.is_file()]:
                if should_cancel():
                    log(f"[INFO] {_timestamp()} Cleanup cancelled.")
                    return {"moved_back": moved_back, "removed_folders": removed_folders, "cancelled": True}
                self._safe_move_to_dir(file_path, root_path)
                moved_back += 1
                done += 1
                if total_files:
                    progress(int((done / total_files) * 100))

            try:
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

        root_path = Path(root_dir).expanduser().resolve()
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

        root_path = Path(root_dir).expanduser().resolve()
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
                self._safe_move_to_dir(src, dest)
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
        should_cancel = self._callbacks.should_cancel

        root_path = Path(root_dir).expanduser().resolve()
        cleanup = self._cleanup_group_folders(root_path, "Group_Size_")
        if cleanup.get("cancelled"):
            return {"processed": 0, "groups_created": 0, "files_moved": 0, "trashed_low_res": 0, "cancelled": True}

        cleanup2 = self.run_cleanup_low_res(root_path)
        if cleanup2.get("cancelled"):
            return {"processed": 0, "groups_created": 0, "files_moved": 0, "trashed_low_res": int(cleanup2.get("trashed_low_res") or 0), "cancelled": True}
        duration_items = list(cleanup2.get("duration_items") or [])
        trashed_low_res = int(cleanup2.get("trashed_low_res") or 0)
        if not duration_items:
            progress(0)
            log(f"[INFO] {_timestamp()} Phase 2: no eligible videos found after cleanup.")
            return {"processed": 0, "groups_created": 0, "files_moved": 0, "trashed_low_res": trashed_low_res, "cancelled": False}

        duration_items.sort(key=lambda t: t[0])
        tolerance = Thresholds.DURATION_TOLERANCE_S

        clusters: list[list[tuple[float, Path]]] = []
        for item in duration_items:
            if not clusters:
                clusters.append([item])
                continue
            current = clusters[-1]
            durs = [duration for duration, _path in current]
            new_min = min(min(durs), item[0])
            new_max = max(max(durs), item[0])
            if (new_max - new_min) < tolerance:
                current.append(item)
            else:
                clusters.append([item])

        duplicate_clusters = [c for c in clusters if len(c) > 1]
        groups_created = 0
        files_moved = 0
        total_to_move = sum(len(c) for c in duplicate_clusters)
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
                self._safe_move_to_dir(src, dest)
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
        root_path = Path(root_dir).expanduser().resolve()
        return self._cleanup_group_folders(root_path, "Group_Time_")

    def run_phase_3_content(self, root_dir: str | Path) -> dict:
        log = self._callbacks.log
        progress = self._callbacks.progress
        stage = self._callbacks.stage
        on_result = self._callbacks.result
        should_cancel = self._callbacks.should_cancel

        root_path = Path(root_dir).expanduser().resolve()
        db: DedupDatabase | None = None
        try:
            stage("Hashing/Loading")
            log(f"[INFO] {_timestamp()} Phase 3: Loading fingerprints (cache/ffmpeg)...")
            scanner = VideoScanner(on_log=log)
            video_paths = scanner.scan(root_path)
            total_videos = len(video_paths)
            if total_videos == 0:
                progress(0)
                log(f"[INFO] {_timestamp()} Phase 3: Nothing to do.")
                return {"processed": 0, "duplicates": 0, "cancelled": False}

            db = DedupDatabase(root_path / CACHE_DB_FILENAME)
            stale_count = db.cleanup_stale_entries({str(p) for p in video_paths})
            if stale_count > 0:
                log(f"[INFO] {_timestamp()} Removed {stale_count} stale cache entries.")
            cancel_event = threading.Event()

            fp_config = self._config.fingerprint
            match_config = self._config.match
            max_offset_frames = int(round(match_config.max_offset_s * fp_config.sample_fps))
            min_overlap = int(match_config.min_overlap_frames)
            max_threads = max(1, min(16, int(self._config.max_threads)))

            fingerprints: list[VideoFingerprint] = []
            cancelling = False
            completed = 0
            progress(0)

            def log_error_for(path_str: str, error: str) -> None:
                log(f"[WARN] {_timestamp()} Skipping: {path_str} ({error})")

            path_iter = iter([str(p) for p in video_paths])
            pending: set = set()

            def submit_next(executor: ThreadPoolExecutor) -> bool:
                try:
                    path = next(path_iter)
                except StopIteration:
                    return False
                fut = executor.submit(
                    _fingerprint_video_worker,
                    path,
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

                        if cancelling:
                            continue

                        result = fut.result()
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
                            )
                        )

                    while not cancelling and len(pending) < max_threads:
                        if not submit_next(executor):
                            break

            if cancelling:
                log(f"[INFO] {_timestamp()} Cancelled.")
                return {"processed": len(fingerprints), "duplicates": 0, "cancelled": True}

            if not fingerprints:
                progress(100)
                log(f"[INFO] {_timestamp()} Phase 3: No valid videos to compare.")
                return {"processed": 0, "duplicates": 0, "cancelled": False}

            stage("Indexing/Comparing")
            max_hamming = int(round((1.0 - float(match_config.consensus_prefilter_similarity)) * float(Thresholds.PHASH_BITS)))
            max_hamming = max(0, min(Thresholds.PHASH_BITS, max_hamming))
            log(f"[INFO] {_timestamp()} Phase 3: BK-Tree prefilter <= {max_hamming} bit(s)")
            log(f"[INFO] {_timestamp()} Phase 3: Streaming BK-Tree compare...")
            progress(0)
            tree = BKTree(hamming_distance_uint64_scalar)
            duplicates_found = 0
            comparisons_run = 0

            for i, fp in enumerate(fingerprints):
                if should_cancel():
                    cancelling = True
                    break

                neighbors = tree.query(fp.consensus_hash, max_hamming)
                for j in neighbors:
                    if should_cancel():
                        cancelling = True
                        break

                    other = fingerprints[j]
                    if abs(fp.duration_s - other.duration_s) > (match_config.max_offset_s + Thresholds.DURATION_PADDING_S):
                        continue

                    comparisons_run += 1
                    similarity, offset_frames, overlap_frames = best_time_shift_similarity(
                        other.hashes,
                        fp.hashes,
                        max_offset_frames=max_offset_frames,
                        min_overlap_frames=min_overlap,
                        early_exit_threshold=float(match_config.duplicate_similarity),
                    )

                    if similarity >= match_config.duplicate_similarity:
                        duplicates_found += 1
                        offset_s = offset_frames / fp_config.sample_fps if fp_config.sample_fps else 0.0
                        on_result(
                            {
                                "original": other.path,
                                "duplicate": fp.path,
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
            if cancelling:
                log(f"[INFO] {_timestamp()} Cancelled.")
                return {"processed": len(fingerprints), "duplicates": duplicates_found, "cancelled": True}
            progress(100)
            log(
                f"[INFO] {_timestamp()} Done. Processed {len(fingerprints)} valid video(s). "
                f"Compared {comparisons_run} pair(s)."
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
    from PyQt5.QtCore import QThread, QTimer, Qt, pyqtSignal
    from PyQt5.QtGui import QBrush, QColor, QDesktopServices, QFont, QTextCursor
    from PyQt5.QtWidgets import (
        QApplication,
        QCheckBox,
        QFileDialog,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMenu,
        QMessageBox,
        QProgressDialog,
        QPushButton,
        QProgressBar,
        QSpinBox,
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

    class WorkerThread(QThread):
        signal_log = pyqtSignal(str)
        signal_progress = pyqtSignal(int)
        signal_stage = pyqtSignal(str)
        signal_result = pyqtSignal(dict)
        signal_done = pyqtSignal(str, dict)
        signal_finished = pyqtSignal()

        def __init__(self, task: str, root_dir: str, binaries: FFmpegBinaries, max_threads: int, parent=None):
            super().__init__(parent)
            self._task = str(task)
            self._root_dir = root_dir
            self._binaries = binaries
            self._max_threads = int(max_threads)
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
                should_cancel=self._should_cancel,
            )
            config = EngineConfig(max_threads=max(1, min(16, self._max_threads)))
            engine = VideoDedupEngine(self._binaries, config=config, callbacks=callbacks)
            if self._task == "phase1_size":
                summary = engine.run_phase_1_size(self._root_dir)
            elif self._task == "phase2_time":
                summary = engine.run_phase_2_duration(self._root_dir)
            elif self._task == "phase3_prepare":
                summary = engine.run_phase_3_prepare(self._root_dir)
            elif self._task == "phase3_direct":
                self._log(
                    f"[INFO] Checkbox enabled: Running pre-scan cleanup for <{Thresholds.MIN_RESOLUTION_PX}px videos..."
                )
                cleanup = engine.run_cleanup_low_res(Path(self._root_dir))
                if cleanup.get("cancelled"):
                    summary = {"cancelled": True, "error": "Cancelled during cleanup.", **cleanup}
                else:
                    self._log("[INFO] Cleanup done. Moving low-res files to Trash.")
                    summary = engine.run_phase_3_content(self._root_dir)
                    summary = {
                        **summary,
                        "trashed_low_res_precleanup": int(cleanup.get("trashed_low_res") or 0),
                    }
            elif self._task == "phase3_content":
                summary = engine.run_phase_3_content(self._root_dir)
            else:
                summary = {"cancelled": True, "error": f"Unknown task: {self._task}"}

            self.signal_done.emit(self._task, summary)
            self.signal_finished.emit()

    class MainWindow(QMainWindow):
        def __init__(self, binaries: FFmpegBinaries):
            super().__init__()
            self._binaries = binaries
            self._worker: WorkerThread | None = None
            self.current_phase = PHASE_IDLE
            self._active_task: str | None = None
            self._root_dir: str | None = None

            self.setWindowTitle("Advanced Video Deduplicator")
            self.setMinimumSize(1100, 700)

            central = QWidget(self)
            self.setCentralWidget(central)

            self.path_edit = QLineEdit()
            self.path_edit.setPlaceholderText("Select a folder to scan...")
            self.browse_button = QPushButton("Browse...")
            self.start_button = QPushButton("Start Scan")
            self.continue_button = QPushButton("Continue")
            self.continue_button.setEnabled(False)
            self.continue_button.setVisible(False)
            self.stop_button = QPushButton("Stop/Cancel")
            self.stop_button.setEnabled(False)
            self.max_threads_spin = QSpinBox()
            self.max_threads_spin.setRange(1, 16)
            self.max_threads_spin.setValue(3)
            self.max_threads_spin.setToolTip("Limit concurrent FFmpeg workers to reduce CPU/Disk pressure.")
            self.skip_filters_checkbox = QCheckBox("Skip Size/Time Filters (Direct Phase 3)")
            self.skip_filters_checkbox.setChecked(False)

            input_row = QHBoxLayout()
            input_row.addWidget(QLabel("Folder:"))
            input_row.addWidget(self.path_edit, 1)
            input_row.addWidget(self.browse_button)

            control_row = QHBoxLayout()
            control_row.addWidget(self.start_button)
            control_row.addWidget(self.continue_button)
            control_row.addWidget(self.stop_button)
            control_row.addWidget(QLabel("Max Threads:"))
            control_row.addWidget(self.max_threads_spin)
            control_row.addWidget(self.skip_filters_checkbox)
            control_row.addStretch(1)

            self.progress_bar = QProgressBar()
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.stage_label = QLabel("Stage: Idle")

            self.log_box = QTextEdit()
            self.log_box.setReadOnly(True)
            self.log_box.setFont(QFont("Consolas", 10))

            self.results_table = QTableWidget(0, 4)
            self.results_table.setHorizontalHeaderLabels(
                ["Original File", "Duplicate File", "Similarity %", "Time Offset"]
            )
            self.results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
            self.results_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            self.results_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
            self.results_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
            self.results_table.setAlternatingRowColors(True)
            self.results_table.setSortingEnabled(False)
            self.results_table.setContextMenuPolicy(Qt.CustomContextMenu)
            self.results_table.customContextMenuRequested.connect(self._on_results_context_menu)
            self.results_table.cellDoubleClicked.connect(self._on_results_double_clicked)

            self._refresh_timer = QTimer(self)
            self._refresh_timer.setInterval(2000)
            self._refresh_timer.timeout.connect(self._refresh_table_state)
            self._refresh_timer.start()

            layout = QVBoxLayout()
            layout.addLayout(input_row)
            layout.addLayout(control_row)
            layout.addWidget(self.stage_label)
            layout.addWidget(self.progress_bar)
            layout.addWidget(QLabel("Logs:"))
            layout.addWidget(self.log_box, 2)
            layout.addWidget(QLabel("Results:"))
            layout.addWidget(self.results_table, 3)
            central.setLayout(layout)

            self.browse_button.clicked.connect(self._on_browse)
            self.start_button.clicked.connect(self._on_start)
            self.continue_button.clicked.connect(self._on_continue)
            self.stop_button.clicked.connect(self._on_stop)

            default_dir = Path(os.getcwd()).resolve()
            self.path_edit.setText(str(default_dir))

        def _append_log(self, message: str) -> None:
            self.log_box.append(message)
            self.log_box.moveCursor(QTextCursor.End)
            self.log_box.ensureCursorVisible()

        def _on_browse(self) -> None:
            chosen = QFileDialog.getExistingDirectory(self, "Select Folder", self.path_edit.text().strip() or os.getcwd())
            if chosen:
                self.path_edit.setText(chosen)

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

            self.results_table.setRowCount(0)
            self.log_box.clear()
            self.progress_bar.setValue(0)

            self._root_dir = root_dir
            if self.skip_filters_checkbox.isChecked():
                self.current_phase = PHASE_CONTENT
                self.continue_button.setEnabled(False)
                self.continue_button.setVisible(False)
                self._start_task("phase3_direct")
            else:
                self.current_phase = PHASE_SIZE
                self._start_task("phase1_size")

        def _on_continue(self) -> None:
            if self._worker and self._worker.isRunning():
                return
            if not self._root_dir:
                return
            if self.current_phase == PHASE_SIZE:
                self.current_phase = PHASE_TIME
                self._start_task("phase2_time")
            elif self.current_phase == PHASE_TIME:
                self._start_task("phase3_prepare")
            elif self.current_phase == PHASE_CONTENT:
                return

        def _start_task(self, task: str) -> None:
            self._active_task = task
            self.start_button.setEnabled(False)
            self.continue_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.progress_bar.setValue(0)
            self._set_stage("Working...")

            self._worker = WorkerThread(task, self._root_dir or "", self._binaries, self.max_threads_spin.value(), self)
            self._worker.signal_log.connect(self._append_log)
            self._worker.signal_progress.connect(self.progress_bar.setValue)
            self._worker.signal_stage.connect(self._set_stage)
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
            self.start_button.setEnabled(True)
            self.continue_button.setEnabled(False)
            self.continue_button.setVisible(False)
            self.stop_button.setEnabled(False)
            self.progress_bar.setValue(0)
            self._set_stage("Idle")

        def _set_stage(self, stage: str) -> None:
            stage_str = str(stage).strip() or "Working..."
            self.stage_label.setText(f"Stage: {stage_str}")

        def _on_task_done(self, task: str, summary: dict) -> None:
            cancelled = bool(summary.get("cancelled"))
            if cancelled:
                self._append_log(f"[INFO] {_timestamp()} Task cancelled.")
                self._reset_ui()
                return

            if task == "phase1_size":
                self.continue_button.setVisible(True)
                self.continue_button.setEnabled(True)
                self._append_log("Phase 1 Complete. Click Continue when ready.")
                if self._root_dir:
                    self._reveal_in_explorer(self._root_dir)
            elif task == "phase2_time":
                self.continue_button.setVisible(True)
                self.continue_button.setEnabled(True)
                self._append_log("Phase 2 Complete. Click Continue to proceed.")
                if self._root_dir:
                    self._reveal_in_explorer(self._root_dir)
            elif task == "phase3_prepare":
                reply = QMessageBox.question(
                    self,
                    "Proceed to Advanced Scan?",
                    "Phase 1 & 2 Complete. Do you want to proceed with the Advanced Content Scan (pHash)?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    self._append_log("[INFO] Advanced content scan skipped. Workflow complete.")
                    self._reset_ui()
                    return
                self.current_phase = PHASE_CONTENT
                self.results_table.setRowCount(0)
                self.progress_bar.setValue(0)
                self._start_task("phase3_content")
            elif task in {"phase3_content", "phase3_direct"}:
                processed = int(summary.get("processed") or 0)
                duplicates = int(summary.get("duplicates") or 0)
                if processed > 0 and duplicates == 0:
                    QMessageBox.information(
                        self,
                        "Scan Complete",
                        f"Scan Complete. No duplicates found among {processed} processed videos.",
                    )
                self._reset_ui()

        def _on_finished(self) -> None:
            if self.sender() is self._worker:
                self.stop_button.setEnabled(False)
                self._worker = None

        def _reveal_in_explorer(self, path: str) -> None:
            target = Path(path).resolve()
            if target.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent)))
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

        def _open_file_location(self, path: str) -> None:
            path_str = str(path).strip()
            if not path_str:
                return
            p = Path(path_str)
            if not p.exists():
                self._append_log(f"[WARN] {_timestamp()} File not found: {path_str}")
                QMessageBox.warning(self, "File not found", f"File does not exist:\n\n{path_str}")
                return
            if os.name == "nt":
                try:
                    subprocess.Popen(["explorer", f'/select,"{str(p)}"'])
                except Exception as exc:
                    self._append_log(f"[WARN] {_timestamp()} Failed to open Explorer: {exc}")
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))

        def _play_video(self, path: str) -> None:
            path_str = str(path).strip()
            if not path_str:
                return
            p = Path(path_str)
            if not p.exists():
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
            p = Path(path_str)
            if not p.exists():
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
            try:
                subprocess.Popen([exe, str(p)])
            except Exception as exc:
                self._append_log(f"[WARN] {_timestamp()} Failed to open MediaInfo: {exc}")

        def _delete_file(self, path: str) -> None:
            path_str = str(path).strip()
            if not path_str:
                return
            p = Path(path_str)
            if not p.exists():
                self._append_log(f"[WARN] {_timestamp()} File not found: {path_str}")
                QMessageBox.warning(self, "File not found", f"File does not exist:\n\n{path_str}")
                self._refresh_table_state()
                return

            msg = f"Are you sure you want to delete this file?\n\n{path_str}"
            try:
                from send2trash import send2trash as _send2trash  

                send2trash_available = True
            except Exception:
                _send2trash = None
                send2trash_available = False

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

            try:
                if _send2trash is not None:
                    _send2trash(str(p))
                else:
                    os.remove(str(p))
                self._append_log(f"[INFO] {_timestamp()} Deleted: {path_str}")
            except Exception as exc:
                self._append_log(f"[WARN] {_timestamp()} Delete failed: {exc}")
                QMessageBox.warning(self, "Delete failed", f"Could not delete:\n\n{path_str}\n\n{exc}")
            finally:
                self._refresh_table_state()

        def _get_raw_path(self, row: int, col: int) -> str:
            item = self.results_table.item(row, col)
            if item is None:
                return ""
            raw = item.data(Qt.UserRole)
            if isinstance(raw, str) and raw.strip():
                return raw
            return item.text().replace(" (Deleted)", "").strip()

        def _refresh_table_state(self) -> None:
            rows = self.results_table.rowCount()
            if rows <= 0:
                return

            conflict_brush = QBrush(QColor(255, 220, 220))
            resolved_brush = QBrush(QColor(220, 255, 220))

            for row in range(rows):
                path_a = self._get_raw_path(row, 0)
                path_b = self._get_raw_path(row, 1)

                exists_a = bool(path_a and Path(path_a).exists())
                exists_b = bool(path_b and Path(path_b).exists())

                item_a = self.results_table.item(row, 0)
                item_b = self.results_table.item(row, 1)

                if item_a is not None and path_a:
                    item_a.setData(Qt.UserRole, path_a)
                    item_a.setText(path_a + ("" if exists_a else " (Deleted)"))
                    font = item_a.font()
                    font.setStrikeOut(not exists_a)
                    item_a.setFont(font)

                if item_b is not None and path_b:
                    item_b.setData(Qt.UserRole, path_b)
                    item_b.setText(path_b + ("" if exists_b else " (Deleted)"))
                    font = item_b.font()
                    font.setStrikeOut(not exists_b)
                    item_b.setFont(font)

                row_brush = conflict_brush if (exists_a and exists_b) else resolved_brush
                for col in range(self.results_table.columnCount()):
                    item = self.results_table.item(row, col)
                    if item is not None:
                        item.setBackground(row_brush)

        def _on_results_double_clicked(self, row: int, col: int) -> None:
            if col not in (0, 1):
                return
            path = self._get_raw_path(row, col)
            if path:
                self._open_file_location(path)
                self._refresh_table_state()

        def _on_results_context_menu(self, pos) -> None:
            item = self.results_table.itemAt(pos)
            if item is None:
                return
            row = item.row()
            col = item.column()

            if col in (0, 1):
                path = self._get_raw_path(row, col)
            else:
                path = self._get_raw_path(row, 1) or self._get_raw_path(row, 0)

            if not path:
                return

            menu = QMenu(self)

            action_play = menu.addAction("Play Video")
            action_reveal = menu.addAction("Reveal in Explorer")
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
            elif chosen == action_mediainfo:
                self._open_mediainfo(path)
            elif chosen == action_delete:
                self._delete_file(path)

            self._refresh_table_state()

        def _add_result_row(self, payload: dict) -> None:
            original = str(payload.get("original", ""))
            duplicate = str(payload.get("duplicate", ""))
            similarity = float(payload.get("similarity") or 0.0)
            offset_s = float(payload.get("offset_s") or 0.0)

            row = self.results_table.rowCount()
            self.results_table.insertRow(row)

            item_original = QTableWidgetItem(original)
            item_original.setData(Qt.UserRole, original)
            item_duplicate = QTableWidgetItem(duplicate)
            item_duplicate.setData(Qt.UserRole, duplicate)
            self.results_table.setItem(row, 0, item_original)
            self.results_table.setItem(row, 1, item_duplicate)
            self.results_table.setItem(row, 2, QTableWidgetItem(f"{similarity:.2f}"))
            sign = "+" if offset_s >= 0 else "-"
            self.results_table.setItem(row, 3, QTableWidgetItem(f"{sign}{abs(offset_s):.1f}s"))
            self._refresh_table_state()

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
            "ffmpeg.exe and ffprobe.exe not found in the current directory or ./bin.\n\n"
            "Place portable FFmpeg binaries next to this script or in a ./bin folder.",
        )
        return 1

    return run_gui(binaries)

if __name__ == "__main__":
    raise SystemExit(main())
