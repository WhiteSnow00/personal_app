from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, Final, List, Mapping, Optional, Sequence, Tuple, TypeVar, Union

try:
    from rich import box
    from rich.align import Align
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.markup import escape as rich_escape
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.prompt import Confirm, IntPrompt, Prompt
    from rich.table import Table
    from rich.text import Text
    from rich.theme import Theme

    RICH_AVAILABLE: bool = True
except ImportError:
    RICH_AVAILABLE = False

try:
    from PIL import Image, ImageFile, UnidentifiedImageError

    ImageFile.LOAD_TRUNCATED_IMAGES = True
    PILLOW_AVAILABLE: bool = True
except ImportError:
    PILLOW_AVAILABLE = False
    Image = None
    UnidentifiedImageError = Exception

SCRIPT_VERSION: Final[str] = "1.1.0"
SCRIPT_NAME: Final[str] = "JPEG Batch Compressor"
TARGET_SIZE_MB: Final[float] = 4.95
DEFAULT_MAX_BYTES: Final[int] = int(TARGET_SIZE_MB * 1_000_000)
DEFAULT_OUTPUT_DIRNAME: Final[str] = "compressed_output"
TEMP_WORKDIR_NAME: Final[str] = ".tmp_compress_work"
JPEG_EXTENSIONS: Final[frozenset[str]] = frozenset({".jpg", ".jpeg", ".jpe", ".jfif"})
CONVERTIBLE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".png", ".webp", ".bmp", ".tif", ".tiff"}
)
SUPPORTED_EXTENSIONS: Final[frozenset[str]] = JPEG_EXTENSIONS | CONVERTIBLE_EXTENSIONS
EXCLUDED_BASENAMES: Final[frozenset[str]] = frozenset(
    {"ffmpeg", "ffprobe", "ffplay", "jpeg_compressor", "requirements", "readme", "license"}
)
FFMPEG_NAMES: Final[Tuple[str, ...]] = (
    ("ffmpeg.exe", "ffmpeg") if platform.system() == "Windows" else ("ffmpeg",)
)
FFPROBE_NAMES: Final[Tuple[str, ...]] = (
    ("ffprobe.exe", "ffprobe") if platform.system() == "Windows" else ("ffprobe",)
)
DEFAULT_SUBPROCESS_TIMEOUT_SEC: Final[float] = 180.0
PROBE_TIMEOUT_SEC: Final[float] = 45.0
ENCODE_TIMEOUT_SEC: Final[float] = 240.0
FFMPEG_Q_BEST: Final[int] = 1
FFMPEG_Q_WORST: Final[int] = 28
FFMPEG_Q_HIGH_QUALITY_START: Final[int] = 2
FFMPEG_Q_MEDIUM: Final[int] = 5
QUALITY_LOSSLESS_PROXY: Final[int] = 100
QUALITY_HIGH_MIN: Final[int] = 90
QUALITY_HIGH_MAX: Final[int] = 95
QUALITY_BINARY_MIN: Final[int] = 35
QUALITY_BINARY_MAX: Final[int] = 97
BINARY_SEARCH_MAX_ITERS: Final[int] = 12
QUALITY_LADDER: Final[Tuple[int, ...]] = (95, 92, 90, 88, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40)
DOWNSCALE_FACTORS: Final[Tuple[float, ...]] = (0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6)
MIN_OUTPUT_DIMENSION: Final[int] = 640
DEFAULT_MAX_WORKERS: Final[int] = max(1, min(8, os.cpu_count() or 4))
WORKER_KIND_AUTO: Final[str] = "auto"
LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATE_FORMAT: Final[str] = "%H:%M:%S"
DEFAULT_LOG_LEVEL: Final[int] = logging.INFO
SIZE_SAFETY_MARGIN_BYTES: Final[int] = 10_000
EFFECTIVE_TARGET_BYTES: Final[int] = max(1, DEFAULT_MAX_BYTES - SIZE_SAFETY_MARGIN_BYTES)
REPORT_JSON_NAME: Final[str] = "compression_report.json"
REPORT_LOG_NAME: Final[str] = "compression_run.log"
RICH_THEME: Final[Any] = (
    Theme(
        {
            "info": "cyan",
            "warning": "yellow",
            "error": "bold red",
            "success": "bold green",
            "muted": "dim",
            "title": "bold bright_white",
            "accent": "bold magenta",
            "size_ok": "green",
            "size_warn": "yellow",
            "size_bad": "red",
            "header": "bold bright_cyan",
        }
    )
    if RICH_AVAILABLE
    else None
)
UI_TEXT: Final[Dict[str, Dict[str, str]]] = {
    "en": {
        "lang_prompt": "Select language / Chọn ngôn ngữ",
        "lang_en": "English",
        "lang_vi": "Vietnamese (Tiếng Việt)",
        "banner_subtitle": "Zero-config · Local ffmpeg · Strict <{target_mb} MB ({target_bytes} bytes) guarantee pipeline",
        "rule_environment": "Environment",
        "rule_preflight": "Pre-flight Analysis",
        "rule_strategy": "Strategy Selection",
        "rule_format": "Output Format",
        "rule_processing": "Processing",
        "rule_complete": "Run Complete",
        "format_available": "Available Output Formats",
        "select_format": "Select output format",
        "format_1_title": "JPG",
        "format_2_title": "PNG",
        "format_3_title": "WEBP",
        "format_4_title": "Keep Original Format",
        "format_1_desc": "Convert all images to JPG (RGB). Alpha channel is flattened onto white.",
        "format_2_desc": "Convert all images to PNG. Alpha channel is preserved when present.",
        "format_3_desc": "Convert all images to WEBP. Alpha channel is preserved when present.",
        "format_4_desc": "Keep each file's original format (JPG / PNG / WEBP). Other types map to PNG.",
        "format_recommended": "Recommended: {opt} — {title}",
        "invalid_format": "Invalid selection. Enter 1, 2, 3, or 4.",
        "res_format": "Output format",
        "env_workdir": "Working directory",
        "env_ffmpeg": "ffmpeg",
        "env_ffprobe": "ffprobe",
        "env_pillow": "Pillow",
        "env_rich": "Rich",
        "env_python": "Python",
        "env_platform": "Platform",
        "env_cpu": "CPU count",
        "env_size_limit": "Size limit",
        "env_effective": "Effective target",
        "avail_yes": "available",
        "avail_pillow_no": "not installed (optional)",
        "avail_rich_no": "not installed (plain mode)",
        "size_limit_val": "< {size} (strict)",
        "scan_overview": "Scan Overview",
        "metric": "Metric",
        "value": "Value",
        "files_discovered": "Files discovered",
        "jpeg": "JPEG",
        "convertible": "Convertible (PNG/etc.)",
        "already_under": "Already < {target_mb} MB",
        "need_compression": "Need compression",
        "unreadable_corrupt": "Unreadable / corrupt",
        "total_size": "Total size",
        "over_limit_mass": "Over-limit mass",
        "est_savings_gentle": "Est. savings (gentle)",
        "est_savings_aggr": "Est. savings (aggressive)",
        "scan_time": "Scan time",
        "dim_range": "Dimension range",
        "avg_mp": "Average megapixels",
        "comp_potential": "Compression Potential",
        "bucket": "Bucket",
        "count": "Count",
        "per_file_inv": "Per-file Inventory",
        "col_file": "File",
        "col_size": "Size",
        "col_dims": "Dimensions",
        "col_codec": "Codec",
        "col_status": "Status",
        "col_need": "Need ↓",
        "col_potential": "Potential",
        "showing_first": "Showing first {n} of {total} files",
        "plain_files": "Files: {n}",
        "plain_jpeg_conv": "JPEG: {j}  Convertible: {c}",
        "plain_under_over": "Under limit: {u}  Over: {o}",
        "plain_corrupt": "Corrupt: {n}",
        "plain_total": "Total size: {s}",
        "plain_savings": "Est. savings: ~{lo:.1f}–{hi:.1f} MiB",
        "strat_available": "Available Strategies",
        "col_opt": "Opt",
        "col_name": "Name",
        "col_desc": "Description",
        "recommended_tag": "← recommended",
        "recommended_line": "Recommended: {opt} — {title}",
        "select_strategy": "Select strategy",
        "invalid_selection": "Invalid selection.",
        "invalid_selection_abc": "Invalid selection. Enter A, B, C, D, or E.",
        "recommended_paren": " (recommended)",
        "confirm_proceed": "Proceed with {title} on {n} oversize file(s)?",
        "continue_yn": "Continue? [Y/n]: ",
        "results_summary": "Results Summary",
        "res_strategy": "Strategy",
        "res_elapsed": "Elapsed",
        "res_compressed": "Compressed",
        "res_copied": "Copied (already OK)",
        "res_failed": "Failed",
        "res_saved": "Total saved",
        "res_output": "Output folder",
        "per_file_results": "Per-file Results",
        "col_in": "In",
        "col_out": "Out",
        "col_saved": "Saved",
        "col_q": "Q",
        "col_scale": "Scale",
        "col_notes": "Notes",
        "fail_section_title": "Failed Files — Action Required",
        "fail_col_reason": "Why it failed",
        "fail_size_limit": "Cannot compress below {target_mb}MB without reducing image resolution.",
        "fail_corrupt": "File is corrupted or unreadable.",
        "fail_generic": "Conversion process failed.",
        "fail_unsupported": "This file format is not supported.",
        "fail_none_detail": "No additional detail available.",
        "compressing": "Compressing images",
        "parallel_workers": "Parallel workers",
        "preflight_running": "Running pre-flight scan…",
        "no_images": "No supported images found in this folder ({exts}).",
        "aborted_user": "Aborted by user.",
        "all_success": "All jobs finished successfully.",
        "done_hard_fail": "Completed with {hard} hard failure(s) and {size} size-limit failure(s).",
        "done_size_fail": "Completed with {n} size-limit failure(s). See report for details.",
        "err_output_dir": "Cannot create output directory: {exc}",
        "press_enter": "Press Enter to close…",
        "aborted": "Aborted.",
        "interrupted": "Interrupted.",
        "stopped_by_user": "Program stopped by user.",
        "error_prefix": "ERROR: {exc}",
        "strat_a_title": "Lossless / Metadata-First",
        "strat_b_title": "High-Quality Lossy (90–95%)",
        "strat_c_title": "Binary-Search Target (<{target_mb} MB)",
        "strat_d_title": "Aggressive Adaptive (max retention)",
        "strat_e_title": "Copy Already-Compliant Only",
        "strat_a_desc": "Strip metadata and re-mux / re-encode at near-lossless quality. Falls back to mild lossy only when the file still exceeds the limit. Best visual fidelity; may leave a few stubborn files slightly large if downscale is disabled.",
        "strat_b_desc": "Encode every oversize image at quality 90–95 with progressive JPEG and Huffman optimisation. Fast, predictable, excellent quality for print-to-web pipelines.",
        "strat_c_desc": "Per-image binary search over quality to land just under {target_mb} MB while maximising retained quality. Most size-efficient single-pass approach; slightly more CPU per file.",
        "strat_d_desc": "Full pipeline: lossless attempt → quality ladder → binary search → gentle downscale if still over limit. Guarantees <{target_mb} MB for virtually every readable image. Recommended production default.",
        "strat_e_desc": "Only copy files already under {target_mb} MB into the output folder. Oversize images are skipped (listed in the report). Useful for dry-run style triage.",
        "msg_already_copied": "already under limit — copied",
        "msg_already_not_copied": "already under limit (not copied)",
        "msg_copy_only_skip": "over limit; copy-only strategy skips compression",
        "msg_dry_run": "dry-run: would compress",
        "msg_compressed": "compressed to {size} (q={q}, scale={scale:.2f})",
        "msg_size_fail": "could not get under {limit} (best={best})",
        "msg_unable": "unable to reach <{limit}; best effort was {best} at q={q} scale={scale:.2f}",
    },
    "vi": {
        "lang_prompt": "Select language / Chọn ngôn ngữ",
        "lang_en": "English",
        "lang_vi": "Tiếng Việt",
        "banner_subtitle": "Zero-config · ffmpeg local · Pipeline đảm bảo nghiêm ngặt <{target_mb} MB ({target_bytes} bytes)",
        "rule_environment": "Môi trường",
        "rule_preflight": "Phân tích Pre-flight",
        "rule_strategy": "Chọn Strategy",
        "rule_format": "Output Format",
        "rule_processing": "Đang xử lý",
        "rule_complete": "Hoàn tất",
        "format_available": "Các Output Format khả dụng",
        "select_format": "Chọn output format",
        "format_1_title": "JPG",
        "format_2_title": "PNG",
        "format_3_title": "WEBP",
        "format_4_title": "Keep Original Format",
        "format_1_desc": "Chuyển tất cả image sang JPG (RGB). Alpha channel được flatten lên nền trắng.",
        "format_2_desc": "Chuyển tất cả image sang PNG. Giữ alpha channel nếu có.",
        "format_3_desc": "Chuyển tất cả image sang WEBP. Giữ alpha channel nếu có.",
        "format_4_desc": "Giữ original format của từng file (JPG / PNG / WEBP). Định dạng khác map sang PNG.",
        "format_recommended": "Khuyến nghị: {opt} — {title}",
        "invalid_format": "Lựa chọn không hợp lệ. Nhập 1, 2, 3 hoặc 4.",
        "res_format": "Output format",
        "env_workdir": "Thư mục làm việc",
        "env_ffmpeg": "ffmpeg",
        "env_ffprobe": "ffprobe",
        "env_pillow": "Pillow",
        "env_rich": "Rich",
        "env_python": "Python",
        "env_platform": "Platform",
        "env_cpu": "Số CPU",
        "env_size_limit": "Giới hạn dung lượng",
        "env_effective": "Mục tiêu hiệu dụng",
        "avail_yes": "có sẵn",
        "avail_pillow_no": "chưa cài (tuỳ chọn)",
        "avail_rich_no": "chưa cài (chế độ plain)",
        "size_limit_val": "< {size} (strict)",
        "scan_overview": "Tổng quan quét",
        "metric": "Chỉ số",
        "value": "Giá trị",
        "files_discovered": "File tìm thấy",
        "jpeg": "JPEG",
        "convertible": "Convertible (PNG/etc.)",
        "already_under": "Đã < {target_mb} MB",
        "need_compression": "Cần nén",
        "unreadable_corrupt": "Không đọc được / corrupt",
        "total_size": "Tổng dung lượng",
        "over_limit_mass": "Khối lượng vượt limit",
        "est_savings_gentle": "Ước tiết kiệm (gentle)",
        "est_savings_aggr": "Ước tiết kiệm (aggressive)",
        "scan_time": "Thời gian scan",
        "dim_range": "Khoảng dimensions",
        "avg_mp": "Megapixels trung bình",
        "comp_potential": "Tiềm năng nén",
        "bucket": "Nhóm",
        "count": "Số lượng",
        "per_file_inv": "Danh sách từng file",
        "col_file": "File",
        "col_size": "Size",
        "col_dims": "Dimensions",
        "col_codec": "Codec",
        "col_status": "Status",
        "col_need": "Cần ↓",
        "col_potential": "Potential",
        "showing_first": "Hiển thị {n}/{total} file đầu",
        "plain_files": "Files: {n}",
        "plain_jpeg_conv": "JPEG: {j}  Convertible: {c}",
        "plain_under_over": "Dưới limit: {u}  Vượt: {o}",
        "plain_corrupt": "Corrupt: {n}",
        "plain_total": "Tổng size: {s}",
        "plain_savings": "Ước tiết kiệm: ~{lo:.1f}–{hi:.1f} MiB",
        "strat_available": "Các Strategy khả dụng",
        "col_opt": "Opt",
        "col_name": "Tên",
        "col_desc": "Mô tả",
        "recommended_tag": "← khuyến nghị",
        "recommended_line": "Khuyến nghị: {opt} — {title}",
        "select_strategy": "Chọn strategy",
        "invalid_selection": "Lựa chọn không hợp lệ.",
        "invalid_selection_abc": "Lựa chọn không hợp lệ. Nhập A, B, C, D hoặc E.",
        "recommended_paren": " (khuyến nghị)",
        "confirm_proceed": "Tiếp tục với {title} trên {n} file vượt size?",
        "continue_yn": "Tiếp tục? [Y/n]: ",
        "results_summary": "Tóm tắt kết quả",
        "res_strategy": "Strategy",
        "res_elapsed": "Thời gian",
        "res_compressed": "Đã nén",
        "res_copied": "Đã copy (đã đạt)",
        "res_failed": "Thất bại",
        "res_saved": "Tổng tiết kiệm",
        "res_output": "Thư mục output",
        "per_file_results": "Kết quả từng file",
        "col_in": "In",
        "col_out": "Out",
        "col_saved": "Saved",
        "col_q": "Q",
        "col_scale": "Scale",
        "col_notes": "Ghi chú",
        "fail_section_title": "File thất bại — Cần xử lý",
        "fail_col_reason": "Lý do thất bại",
        "fail_size_limit": "Không thể nén dưới {target_mb}MB nếu không giảm độ phân giải ảnh.",
        "fail_corrupt": "File bị hỏng hoặc phần mềm không thể đọc.",
        "fail_generic": "Quá trình chuyển đổi bị lỗi.",
        "fail_unsupported": "Format file này không được hỗ trợ.",
        "fail_none_detail": "Không có thông tin chi tiết thêm.",
        "compressing": "Đang nén images",
        "parallel_workers": "Số worker song song",
        "preflight_running": "Đang chạy pre-flight scan…",
        "no_images": "Không tìm thấy image được hỗ trợ trong thư mục này ({exts}).",
        "aborted_user": "Người dùng đã huỷ.",
        "all_success": "Tất cả job hoàn tất thành công.",
        "done_hard_fail": "Hoàn tất với {hard} hard failure(s) và {size} size-limit failure(s).",
        "done_size_fail": "Hoàn tất với {n} size-limit failure(s). Xem report để biết chi tiết.",
        "err_output_dir": "Không tạo được thư mục output: {exc}",
        "press_enter": "Nhấn Enter để đóng…",
        "aborted": "Đã huỷ.",
        "interrupted": "Đã ngắt.",
        "stopped_by_user": "Chương trình đã dừng.",
        "error_prefix": "ERROR: {exc}",
        "strat_a_title": "Lossless / Metadata-First",
        "strat_b_title": "High-Quality Lossy (90–95%)",
        "strat_c_title": "Binary-Search Target (<{target_mb} MB)",
        "strat_d_title": "Aggressive Adaptive (giữ chất lượng tối đa)",
        "strat_e_title": "Chỉ copy file đã đạt chuẩn",
        "strat_a_desc": "Gỡ metadata và re-encode gần lossless. Chỉ fallback sang lossy nhẹ khi file vẫn vượt limit. Giữ fidelity tốt nhất; vài file cứng đầu có thể vẫn lớn nếu tắt downscale.",
        "strat_b_desc": "Encode mọi image vượt size ở quality 90–95 với progressive JPEG và Huffman optimisation. Nhanh, ổn định, chất lượng tốt cho pipeline print-to-web.",
        "strat_c_desc": "Binary search quality theo từng image để nằm sát dưới {target_mb} MB, tối đa hoá quality giữ lại. Hiệu quả size nhất trong single-pass; tốn CPU hơn một chút.",
        "strat_d_desc": "Pipeline đầy đủ: lossless → quality ladder → binary search → downscale nhẹ nếu vẫn over. Đảm bảo <{target_mb} MB cho hầu hết image đọc được. Mặc định production khuyến nghị.",
        "strat_e_desc": "Chỉ copy các file đã dưới {target_mb} MB vào output. Image vượt size bị skip (ghi trong report). Hữu ích khi triage kiểu dry-run.",
        "msg_already_copied": "đã dưới limit — đã copy",
        "msg_already_not_copied": "đã dưới limit (không copy)",
        "msg_copy_only_skip": "vượt limit; strategy copy-only bỏ qua nén",
        "msg_dry_run": "dry-run: sẽ nén",
        "msg_compressed": "đã nén còn {size} (q={q}, scale={scale:.2f})",
        "msg_size_fail": "không xuống dưới {limit} (best={best})",
        "msg_unable": "không đạt <{limit}; best effort {best} tại q={q} scale={scale:.2f}",
    },
}
ACTIVE_LANG: str = "en"


def t(key: str, **kwargs: Any) -> str:
    bundle = UI_TEXT.get(ACTIVE_LANG) or UI_TEXT["en"]
    template = bundle.get(key) or UI_TEXT["en"].get(key, key)
    merged: Dict[str, Any] = {
        "target_mb": f"{TARGET_SIZE_MB:g}",
        "target_bytes": f"{DEFAULT_MAX_BYTES:,}",
    }
    merged.update(kwargs)
    try:
        return template.format(**merged)
    except (KeyError, ValueError, IndexError):
        return template


def set_language(lang: str) -> None:
    global ACTIVE_LANG
    ACTIVE_LANG = lang if lang in UI_TEXT else "en"


def friendly_failure_reason(result: "ImageJobResult") -> str:
    if result.status == ImageStatus.SIZE_LIMIT_FAILED:
        base = t("fail_size_limit")
        if result.output_bytes > 0:
            return f"{base} ({human_bytes(result.output_bytes)})"
        return base
    if result.status == ImageStatus.SKIPPED_CORRUPT:
        return t("fail_corrupt")
    if result.status == ImageStatus.SKIPPED_UNSUPPORTED:
        return t("fail_unsupported")
    if result.status == ImageStatus.FAILED:
        return t("fail_generic")
    if result.message:
        return result.message
    return t("fail_none_detail")


class CompressorError(Exception):

    def __init__(self, message: str, *, cause: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __str__(self) -> str:
        if self.cause is not None:
            return f"{self.message} (caused by: {self.cause!r})"
        return self.message


class BinaryNotFoundError(CompressorError):
    pass


class ProbeError(CompressorError):
    pass


class EncodeError(CompressorError):
    pass


class CorruptImageError(CompressorError):
    pass


class UnsupportedFormatError(CompressorError):
    pass


class SizeLimitError(CompressorError):
    pass


class UserAbortError(CompressorError):
    pass


class WorkspaceError(CompressorError):
    pass


class CompressionStrategy(Enum):
    LOSSLESS_FIRST = "A"
    HIGH_QUALITY_LOSSY = "B"
    BINARY_SEARCH = "C"
    AGGRESSIVE_ADAPTIVE = "D"
    COPY_ONLY_UNDER_LIMIT = "E"

    @property
    def title(self) -> str:
        return t(
            {
                CompressionStrategy.LOSSLESS_FIRST: "strat_a_title",
                CompressionStrategy.HIGH_QUALITY_LOSSY: "strat_b_title",
                CompressionStrategy.BINARY_SEARCH: "strat_c_title",
                CompressionStrategy.AGGRESSIVE_ADAPTIVE: "strat_d_title",
                CompressionStrategy.COPY_ONLY_UNDER_LIMIT: "strat_e_title",
            }[self]
        )

    @property
    def description(self) -> str:
        return t(
            {
                CompressionStrategy.LOSSLESS_FIRST: "strat_a_desc",
                CompressionStrategy.HIGH_QUALITY_LOSSY: "strat_b_desc",
                CompressionStrategy.BINARY_SEARCH: "strat_c_desc",
                CompressionStrategy.AGGRESSIVE_ADAPTIVE: "strat_d_desc",
                CompressionStrategy.COPY_ONLY_UNDER_LIMIT: "strat_e_desc",
            }[self]
        )


class ImageStatus(Enum):
    PENDING = auto()
    SKIPPED_UNDER_LIMIT = auto()
    SKIPPED_UNSUPPORTED = auto()
    SKIPPED_CORRUPT = auto()
    COPIED = auto()
    COMPRESSED = auto()
    FAILED = auto()
    SIZE_LIMIT_FAILED = auto()


class EncodeBackend(Enum):
    NONE = "none"
    COPY = "copy"
    FFMPEG = "ffmpeg"
    PILLOW = "pillow"


class WorkerKind(Enum):
    THREAD = "thread"
    PROCESS = "process"


class OutputFormatChoice(Enum):
    JPG = "1"
    PNG = "2"
    WEBP = "3"
    KEEP_ORIGINAL = "4"

    @property
    def title(self) -> str:
        return t(
            {
                OutputFormatChoice.JPG: "format_1_title",
                OutputFormatChoice.PNG: "format_2_title",
                OutputFormatChoice.WEBP: "format_3_title",
                OutputFormatChoice.KEEP_ORIGINAL: "format_4_title",
            }[self]
        )

    @property
    def description(self) -> str:
        return t(
            {
                OutputFormatChoice.JPG: "format_1_desc",
                OutputFormatChoice.PNG: "format_2_desc",
                OutputFormatChoice.WEBP: "format_3_desc",
                OutputFormatChoice.KEEP_ORIGINAL: "format_4_desc",
            }[self]
        )


class ImageCodec(Enum):
    JPG = "jpg"
    PNG = "png"
    WEBP = "webp"

    @property
    def extension(self) -> str:
        return f".{self.value}"

    @property
    def preserves_alpha(self) -> bool:
        return self in (ImageCodec.PNG, ImageCodec.WEBP)


def detect_source_codec(path: Path) -> ImageCodec:
    suffix = path.suffix.lower()
    if suffix in JPEG_EXTENSIONS:
        return ImageCodec.JPG
    if suffix == ".png":
        return ImageCodec.PNG
    if suffix == ".webp":
        return ImageCodec.WEBP
    return ImageCodec.PNG


def resolve_output_codec(path: Path, choice: OutputFormatChoice) -> ImageCodec:
    if choice == OutputFormatChoice.JPG:
        return ImageCodec.JPG
    if choice == OutputFormatChoice.PNG:
        return ImageCodec.PNG
    if choice == OutputFormatChoice.WEBP:
        return ImageCodec.WEBP
    return detect_source_codec(path)


def codecs_match_for_copy(path: Path, target: ImageCodec) -> bool:
    return detect_source_codec(path) == target


def quality_to_png_level(quality: int) -> int:
    q = clamp(int(quality), 1, 100)
    return clamp(int(round((100 - q) / 100.0 * 9.0)), 0, 9)


def quality_to_webp_params(quality: int) -> Tuple[int, bool]:
    q = clamp(int(quality), 1, 100)
    if q >= 98:
        return 100, True
    return q, False


@dataclass(frozen=True, slots=True)
class ImageDimensions:
    width: int
    height: int

    @property
    def megapixels(self) -> float:
        if self.width <= 0 or self.height <= 0:
            return 0.0
        return self.width * self.height / 1000000.0

    @property
    def aspect_ratio(self) -> float:
        if self.height == 0:
            return 0.0
        return self.width / self.height

    def scaled(self, factor: float) -> "ImageDimensions":
        if factor >= 1.0:
            return self
        w = max(MIN_OUTPUT_DIMENSION, int(self.width * factor) // 2 * 2)
        h = max(MIN_OUTPUT_DIMENSION, int(self.height * factor) // 2 * 2)
        w = min(w, self.width) if self.width >= MIN_OUTPUT_DIMENSION else self.width
        h = min(h, self.height) if self.height >= MIN_OUTPUT_DIMENSION else self.height
        w = max(2, w)
        h = max(2, h)
        return ImageDimensions(width=w, height=h)

    def __str__(self) -> str:
        return f"{self.width}×{self.height}"


@dataclass(slots=True)
class ImageProbeResult:
    path: Path
    size_bytes: int
    dimensions: Optional[ImageDimensions]
    codec_name: Optional[str]
    pixel_format: Optional[str]
    color_space: Optional[str]
    bit_depth: Optional[int]
    is_readable: bool
    is_jpeg: bool
    has_metadata_hint: bool
    probe_error: Optional[str] = None
    format_name: Optional[str] = None
    duration: Optional[float] = None

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    @property
    def over_limit(self) -> bool:
        return self.size_bytes >= DEFAULT_MAX_BYTES

    @property
    def bytes_over_limit(self) -> int:
        return max(0, self.size_bytes - DEFAULT_MAX_BYTES + 1)

    @property
    def estimated_reduction_needed_pct(self) -> float:
        if self.size_bytes <= 0:
            return 0.0
        if not self.over_limit:
            return 0.0
        target = EFFECTIVE_TARGET_BYTES
        return max(0.0, (1.0 - target / self.size_bytes) * 100.0)

    @property
    def compression_potential(self) -> str:
        if not self.is_readable:
            return "unreadable"
        if not self.over_limit:
            return "already_ok"
        needed = self.estimated_reduction_needed_pct
        mp = self.dimensions.megapixels if self.dimensions else 0.0
        bpp = 0.0
        if self.dimensions and self.dimensions.width > 0:
            pixels = self.dimensions.width * self.dimensions.height
            bpp = self.size_bytes * 8 / pixels if pixels else 0.0
        if needed < 10 and bpp > 4:
            return "easy"
        if needed < 25:
            return "moderate"
        if needed < 45 or bpp > 3:
            return "challenging"
        return "difficult"


@dataclass(slots=True)
class CompressionAttempt:
    backend: EncodeBackend
    quality: Optional[int]
    ffmpeg_q: Optional[int]
    scale_factor: float
    output_bytes: int
    elapsed_sec: float
    success: bool
    error: Optional[str] = None
    dimensions: Optional[ImageDimensions] = None
    output_path: Optional[Path] = None

    @property
    def under_limit(self) -> bool:
        return self.success and self.output_bytes < DEFAULT_MAX_BYTES


@dataclass(slots=True)
class ImageJobResult:
    source: Path
    status: ImageStatus
    output_path: Optional[Path] = None
    original_bytes: int = 0
    output_bytes: int = 0
    original_dimensions: Optional[ImageDimensions] = None
    output_dimensions: Optional[ImageDimensions] = None
    strategy_used: Optional[CompressionStrategy] = None
    backend: EncodeBackend = EncodeBackend.NONE
    quality_used: Optional[int] = None
    ffmpeg_q_used: Optional[int] = None
    scale_factor: float = 1.0
    attempts: List[CompressionAttempt] = field(default_factory=list)
    message: str = ""
    elapsed_sec: float = 0.0
    error_detail: Optional[str] = None

    @property
    def saved_bytes(self) -> int:
        if self.output_bytes <= 0:
            return 0
        return max(0, self.original_bytes - self.output_bytes)

    @property
    def saved_pct(self) -> float:
        if self.original_bytes <= 0:
            return 0.0
        return self.saved_bytes / self.original_bytes * 100.0

    @property
    def under_limit(self) -> bool:
        return self.output_bytes > 0 and self.output_bytes < DEFAULT_MAX_BYTES


@dataclass(slots=True)
class PreflightSummary:
    root: Path
    images: List[ImageProbeResult]
    total_files_scanned: int
    jpeg_count: int
    convertible_count: int
    under_limit_count: int
    over_limit_count: int
    corrupt_count: int
    total_bytes: int
    over_limit_bytes: int
    potential_savings_low_mb: float
    potential_savings_high_mb: float
    scan_elapsed_sec: float
    dimension_stats: Dict[str, Any] = field(default_factory=dict)
    potential_histogram: Dict[str, int] = field(default_factory=dict)

    @property
    def processable(self) -> List[ImageProbeResult]:
        return [i for i in self.images if i.is_readable]

    @property
    def total_mb(self) -> float:
        return self.total_bytes / (1024 * 1024)


@dataclass(slots=True)
class RuntimeConfig:
    root_dir: Path
    output_dir: Path
    max_bytes: int = DEFAULT_MAX_BYTES
    effective_target_bytes: int = EFFECTIVE_TARGET_BYTES
    strategy: CompressionStrategy = CompressionStrategy.AGGRESSIVE_ADAPTIVE
    max_workers: int = DEFAULT_MAX_WORKERS
    worker_kind: WorkerKind = WorkerKind.THREAD
    allow_downscale: bool = True
    copy_under_limit: bool = True
    overwrite_output: bool = True
    keep_temp_on_failure: bool = False
    subprocess_timeout: float = DEFAULT_SUBPROCESS_TIMEOUT_SEC
    log_file: Optional[Path] = None
    dry_run: bool = False
    include_convertibles: bool = True
    progressive_jpeg: bool = True
    strip_metadata: bool = True
    output_format: OutputFormatChoice = OutputFormatChoice.JPG
    ffmpeg_path: Optional[Path] = None
    ffprobe_path: Optional[Path] = None


@dataclass(slots=True)
class BatchReport:
    config: RuntimeConfig
    preflight: PreflightSummary
    results: List[ImageJobResult]
    started_at: datetime
    finished_at: datetime
    total_elapsed_sec: float

    @property
    def success_count(self) -> int:
        return sum(
            (
                1
                for r in self.results
                if r.status
                in (ImageStatus.COMPRESSED, ImageStatus.COPIED, ImageStatus.SKIPPED_UNDER_LIMIT)
                and (r.output_path is not None or r.status == ImageStatus.SKIPPED_UNDER_LIMIT)
            )
        )

    @property
    def compressed_count(self) -> int:
        return sum((1 for r in self.results if r.status == ImageStatus.COMPRESSED))

    @property
    def copied_count(self) -> int:
        return sum((1 for r in self.results if r.status == ImageStatus.COPIED))

    @property
    def failed_count(self) -> int:
        return sum(
            (
                1
                for r in self.results
                if r.status
                in (ImageStatus.FAILED, ImageStatus.SIZE_LIMIT_FAILED, ImageStatus.SKIPPED_CORRUPT)
            )
        )

    @property
    def total_saved_bytes(self) -> int:
        return sum((r.saved_bytes for r in self.results if r.output_path))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "script_version": SCRIPT_VERSION,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "total_elapsed_sec": round(self.total_elapsed_sec, 3),
            "strategy": self.config.strategy.name,
            "output_format": self.config.output_format.name,
            "max_bytes": self.config.max_bytes,
            "root_dir": str(self.config.root_dir),
            "output_dir": str(self.config.output_dir),
            "language": ACTIVE_LANG,
            "summary": {
                "total_jobs": len(self.results),
                "compressed": self.compressed_count,
                "copied": self.copied_count,
                "failed": self.failed_count,
                "total_saved_bytes": self.total_saved_bytes,
                "total_saved_mb": round(self.total_saved_bytes / (1024 * 1024), 3),
            },
            "results": [
                {
                    "source": str(r.source),
                    "status": r.status.name,
                    "output": str(r.output_path) if r.output_path else None,
                    "original_bytes": r.original_bytes,
                    "output_bytes": r.output_bytes,
                    "saved_bytes": r.saved_bytes,
                    "saved_pct": round(r.saved_pct, 2),
                    "quality": r.quality_used,
                    "ffmpeg_q": r.ffmpeg_q_used,
                    "scale_factor": r.scale_factor,
                    "backend": r.backend.value,
                    "message": r.message,
                    "elapsed_sec": round(r.elapsed_sec, 3),
                    "error": r.error_detail,
                    "attempts": len(r.attempts),
                }
                for r in self.results
            ],
        }


T = TypeVar("T")


def human_bytes(num: Union[int, float], *, binary: bool = True) -> str:
    if num is None:
        return "n/a"
    n = float(num)
    if n < 0:
        return f"-{human_bytes(-n, binary=binary)}"
    unit_step = 1024.0 if binary else 1000.0
    units = ("B", "KiB", "MiB", "GiB", "TiB") if binary else ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if abs(n) < unit_step or unit == units[-1]:
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.2f} {unit}"
        n /= unit_step
    return f"{n:.2f} {units[-1]}"


def human_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def safe_filename(name: str) -> str:
    cleaned = re.sub('[<>:"/\\\\|?*\\x00-\\x1f]', "_", name)
    cleaned = cleaned.strip(" .")
    return cleaned or "unnamed"


def is_within_directory(path: Path, directory: Path) -> bool:
    try:
        path = path.resolve()
        directory = directory.resolve()
        return directory == path or directory in path.parents
    except OSError:
        return False


def atomic_replace(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        try:
            dest.unlink()
        except OSError:
            pass
    os.replace(str(src), str(dest))


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def compute_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def unique_temp_path(directory: Path, suffix: str = ".jpg") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    return directory / f"tmp_{uuid.uuid4().hex}{suffix}"


def quality_to_ffmpeg_q(quality: int) -> int:
    q = clamp(int(quality), 1, 100)
    if q >= 95:
        return 2
    if q >= 90:
        return 3
    if q >= 85:
        return 4
    if q >= 80:
        return 5
    if q >= 75:
        return 6
    if q >= 70:
        return 7
    if q >= 65:
        return 8
    if q >= 60:
        return 10
    if q >= 55:
        return 12
    if q >= 50:
        return 14
    if q >= 45:
        return 16
    if q >= 40:
        return 18
    if q >= 35:
        return 20
    return clamp(int(round(2 + (100 - q) * 0.28)), FFMPEG_Q_BEST, FFMPEG_Q_WORST)


def ffmpeg_q_to_quality(q_v: int) -> int:
    q_v = clamp(int(q_v), FFMPEG_Q_BEST, 31)
    table = {
        1: 98,
        2: 95,
        3: 92,
        4: 88,
        5: 84,
        6: 80,
        7: 76,
        8: 72,
        9: 68,
        10: 64,
        12: 58,
        14: 52,
        16: 48,
        18: 44,
        20: 40,
        22: 36,
        24: 32,
        26: 28,
        28: 24,
        31: 20,
    }
    if q_v in table:
        return table[q_v]
    keys = sorted(table.keys())
    for i in range(len(keys) - 1):
        a, b = (keys[i], keys[i + 1])
        if a <= q_v <= b:
            t = (q_v - a) / (b - a)
            return int(round(table[a] + t * (table[b] - table[a])))
    return clamp(100 - int(q_v * 3), 1, 100)


def estimate_output_bytes(
    original_bytes: int, quality: int, *, scale_factor: float = 1.0, strip_metadata: bool = True
) -> int:
    q = clamp(quality, 1, 100) / 100.0
    quality_factor = 0.22 + 0.78 * q**1.35
    scale_area = max(0.05, scale_factor**2)
    meta_factor = 0.985 if strip_metadata else 1.0
    est = int(original_bytes * quality_factor * scale_area * meta_factor)
    return max(1024, est)


def configure_stdio_utf8() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


class _PlainFormatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        return super().format(record)


def setup_logging(
    *,
    level: int = DEFAULT_LOG_LEVEL,
    log_file: Optional[Path] = None,
    console: Optional[Any] = None,
) -> logging.Logger:
    logger = logging.getLogger("jpeg_compressor")
    logger.handlers.clear()
    logger.setLevel(level)
    logger.propagate = False
    if RICH_AVAILABLE and console is not None:
        handler: logging.Handler = RichHandler(
            console=console,
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            markup=True,
            log_time_format=LOG_DATE_FORMAT,
        )
        handler.setLevel(level)
        logger.addHandler(handler)
    else:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(level)
        stream_handler.setFormatter(_PlainFormatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
        logger.addHandler(stream_handler)
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
            logger.addHandler(fh)
        except OSError as exc:
            logger.warning("Could not open log file %s: %s", log_file, exc)
    return logger


class BinaryLocator:

    def __init__(self, search_dirs: Sequence[Path]) -> None:
        self.search_dirs = [Path(d).resolve() for d in search_dirs]
        self._ffmpeg: Optional[Path] = None
        self._ffprobe: Optional[Path] = None
        self._ffmpeg_version: Optional[str] = None
        self._ffprobe_version: Optional[str] = None

    def resolve(self) -> Tuple[Path, Path]:
        self._ffmpeg = self._find(FFMPEG_NAMES, label="ffmpeg")
        self._ffprobe = self._find(FFPROBE_NAMES, label="ffprobe")
        self._ffmpeg_version = self._read_version(self._ffmpeg)
        self._ffprobe_version = self._read_version(self._ffprobe)
        return (self._ffmpeg, self._ffprobe)

    @property
    def ffmpeg(self) -> Path:
        if self._ffmpeg is None:
            raise BinaryNotFoundError("ffmpeg has not been resolved yet")
        return self._ffmpeg

    @property
    def ffprobe(self) -> Path:
        if self._ffprobe is None:
            raise BinaryNotFoundError("ffprobe has not been resolved yet")
        return self._ffprobe

    @property
    def ffmpeg_version(self) -> str:
        return self._ffmpeg_version or "unknown"

    @property
    def ffprobe_version(self) -> str:
        return self._ffprobe_version or "unknown"

    def _find(self, names: Sequence[str], *, label: str) -> Path:
        for directory in self.search_dirs:
            for name in names:
                candidate = directory / name
                if candidate.is_file():
                    return self._ensure_executable(candidate)
        for name in names:
            found = shutil.which(name)
            if found:
                return Path(found).resolve()
        searched = ", ".join((str(d) for d in self.search_dirs))
        raise BinaryNotFoundError(
            f"Could not find {label} executable. Searched: [{searched}] and PATH. Place {names[0]} in the same folder as this script."
        )

    @staticmethod
    def _ensure_executable(path: Path) -> Path:
        path = path.resolve()
        if not path.is_file():
            raise BinaryNotFoundError(f"Not a file: {path}")
        if os.name != "nt":
            mode = path.stat().st_mode
            if not mode & stat.S_IXUSR:
                try:
                    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                except OSError:
                    pass
        return path

    @staticmethod
    def _read_version(binary: Path) -> str:
        try:
            proc = subprocess.run(
                [str(binary), "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
            first = (proc.stdout or proc.stderr or "").splitlines()
            return first[0].strip() if first else "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unknown"


@dataclass(slots=True)
class CommandResult:
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_sec: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and (not self.timed_out)


class SubprocessRunner:

    def __init__(
        self, logger: logging.Logger, *, default_timeout: float = DEFAULT_SUBPROCESS_TIMEOUT_SEC
    ) -> None:
        self.logger = logger
        self.default_timeout = default_timeout
        self._lock = threading.Lock()
        self.call_count = 0

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: Optional[float] = None,
        cwd: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
        check: bool = False,
        label: str = "cmd",
    ) -> CommandResult:
        timeout = self.default_timeout if timeout is None else timeout
        argv_list = [str(a) for a in argv]
        merged_env = os.environ.copy()
        if env:
            merged_env.update(dict(env))
        merged_env.setdefault("AV_LOG_FORCE_NOCOLOR", "1")
        self.logger.debug("[%s] exec: %s", label, " ".join(argv_list))
        t0 = time.perf_counter()
        timed_out = False
        try:
            proc = subprocess.run(
                argv_list,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=str(cwd) if cwd else None,
                env=merged_env,
                check=False,
            )
            rc = proc.returncode
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            rc = -9
            stdout = exc.stdout or "" if isinstance(exc.stdout, str) else ""
            stderr = f"Timed out after {timeout}s"
            self.logger.error("[%s] TIMEOUT after %.1fs: %s", label, timeout, argv_list[0])
        except FileNotFoundError as exc:
            raise BinaryNotFoundError(f"Executable not found: {argv_list[0]}", cause=exc) from exc
        except OSError as exc:
            raise CompressorError(f"Failed to spawn process: {exc}", cause=exc) from exc
        elapsed = time.perf_counter() - t0
        with self._lock:
            self.call_count += 1
        result = CommandResult(
            argv=argv_list,
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
            elapsed_sec=elapsed,
            timed_out=timed_out,
        )
        if not result.ok:
            self.logger.debug(
                "[%s] failed rc=%s in %.2fs stderr=%s", label, rc, elapsed, (stderr or "")[:500]
            )
            if check:
                raise EncodeError(f"{label} failed (rc={rc}): {(stderr or stdout)[:800]}")
        else:
            self.logger.debug("[%s] ok in %.2fs", label, elapsed)
        return result


class ImageProber:

    def __init__(self, ffprobe: Path, runner: SubprocessRunner, logger: logging.Logger) -> None:
        self.ffprobe = ffprobe
        self.runner = runner
        self.logger = logger

    def probe(self, path: Path) -> ImageProbeResult:
        path = Path(path)
        size = file_size(path)
        if size < 0:
            return ImageProbeResult(
                path=path,
                size_bytes=0,
                dimensions=None,
                codec_name=None,
                pixel_format=None,
                color_space=None,
                bit_depth=None,
                is_readable=False,
                is_jpeg=False,
                has_metadata_hint=False,
                probe_error="stat_failed",
            )
        if size == 0:
            return ImageProbeResult(
                path=path,
                size_bytes=0,
                dimensions=None,
                codec_name=None,
                pixel_format=None,
                color_space=None,
                bit_depth=None,
                is_readable=False,
                is_jpeg=False,
                has_metadata_hint=False,
                probe_error="empty_file",
            )
        try:
            return self._probe_ffprobe(path, size)
        except (ProbeError, CompressorError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self.logger.debug("ffprobe failed for %s: %s — trying Pillow", path.name, exc)
            if PILLOW_AVAILABLE:
                try:
                    return self._probe_pillow(path, size)
                except Exception as pillow_exc:
                    return ImageProbeResult(
                        path=path,
                        size_bytes=size,
                        dimensions=None,
                        codec_name=None,
                        pixel_format=None,
                        color_space=None,
                        bit_depth=None,
                        is_readable=False,
                        is_jpeg=path.suffix.lower() in JPEG_EXTENSIONS,
                        has_metadata_hint=False,
                        probe_error=f"probe_failed: {exc}; pillow: {pillow_exc}",
                    )
            return ImageProbeResult(
                path=path,
                size_bytes=size,
                dimensions=None,
                codec_name=None,
                pixel_format=None,
                color_space=None,
                bit_depth=None,
                is_readable=False,
                is_jpeg=path.suffix.lower() in JPEG_EXTENSIONS,
                has_metadata_hint=False,
                probe_error=str(exc),
            )

    def _probe_ffprobe(self, path: Path, size: int) -> ImageProbeResult:
        argv = [
            str(self.ffprobe),
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        result = self.runner.run(argv, timeout=PROBE_TIMEOUT_SEC, label=f"ffprobe:{path.name}")
        if not result.ok:
            raise ProbeError(
                f"ffprobe failed for {path.name}: {result.stderr[:400] or result.stdout[:400]}"
            )
        data = json.loads(result.stdout)
        streams = data.get("streams") or []
        video = next(
            (s for s in streams if s.get("codec_type") in (None, "video") or "width" in s),
            streams[0] if streams else {},
        )
        fmt = data.get("format") or {}
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        dims = ImageDimensions(width, height) if width > 0 and height > 0 else None
        codec = video.get("codec_name")
        pix_fmt = video.get("pix_fmt")
        color_space = video.get("color_space") or video.get("color_primaries")
        bits = video.get("bits_per_raw_sample")
        bit_depth = int(bits) if bits not in (None, "N/A", "") else None
        tags = fmt.get("tags") or video.get("tags") or {}
        has_meta = bool(tags)
        codec_l = (codec or "").lower()
        is_jpeg = codec_l in {"mjpeg", "jpeg", "jpg"} or path.suffix.lower() in JPEG_EXTENSIONS
        is_readable = dims is not None and dims.width > 0
        if not is_readable:
            raise ProbeError(f"No valid video/image stream in {path.name}")
        return ImageProbeResult(
            path=path,
            size_bytes=size,
            dimensions=dims,
            codec_name=codec,
            pixel_format=pix_fmt,
            color_space=color_space,
            bit_depth=bit_depth,
            is_readable=True,
            is_jpeg=is_jpeg,
            has_metadata_hint=has_meta,
            format_name=fmt.get("format_name"),
            duration=float(fmt["duration"]) if fmt.get("duration") else None,
        )

    def _probe_pillow(self, path: Path, size: int) -> ImageProbeResult:
        assert PILLOW_AVAILABLE and Image is not None
        try:
            with Image.open(path) as img:
                img.verify()
            with Image.open(path) as img:
                width, height = img.size
                fmt = (img.format or "").upper()
                mode = img.mode
                info = img.info or {}
        except Exception as exc:
            raise CorruptImageError(f"Pillow cannot open {path.name}: {exc}", cause=exc) from exc
        return ImageProbeResult(
            path=path,
            size_bytes=size,
            dimensions=ImageDimensions(width, height),
            codec_name=fmt.lower() if fmt else None,
            pixel_format=mode,
            color_space=None,
            bit_depth=None,
            is_readable=True,
            is_jpeg=fmt in {"JPEG", "MPO"} or path.suffix.lower() in JPEG_EXTENSIONS,
            has_metadata_hint=bool(info.get("exif") or info.get("icc_profile")),
            format_name=fmt.lower() if fmt else None,
        )


class Encoder(ABC):
    name: str

    @abstractmethod
    def encode_image(
        self,
        source: Path,
        destination: Path,
        *,
        codec: ImageCodec,
        quality: int,
        scale_factor: float = 1.0,
        target_dimensions: Optional[ImageDimensions] = None,
        strip_metadata: bool = True,
        progressive: bool = True,
    ) -> CompressionAttempt:
        pass


class FFmpegEncoder(Encoder):
    name = "ffmpeg"

    def __init__(
        self,
        ffmpeg: Path,
        runner: SubprocessRunner,
        logger: logging.Logger,
        *,
        timeout: float = ENCODE_TIMEOUT_SEC,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.runner = runner
        self.logger = logger
        self.timeout = timeout

    def encode_image(
        self,
        source: Path,
        destination: Path,
        *,
        codec: ImageCodec,
        quality: int,
        scale_factor: float = 1.0,
        target_dimensions: Optional[ImageDimensions] = None,
        strip_metadata: bool = True,
        progressive: bool = True,
    ) -> CompressionAttempt:
        destination.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        q_v = quality_to_ffmpeg_q(quality)
        vf_parts: List[str] = []
        out_dims: Optional[ImageDimensions] = target_dimensions

        if target_dimensions is not None:
            vf_parts.append(
                f"scale={target_dimensions.width}:{target_dimensions.height}:flags=lanczos"
            )
        elif scale_factor < 0.999:
            vf_parts.append(
                f"scale=trunc(iw*{scale_factor}/2)*2:trunc(ih*{scale_factor}/2)*2:flags=lanczos"
            )

        argv: List[str] = [
            str(self.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-frames:v",
            "1",
        ]
        if vf_parts:
            argv.extend(["-vf", ",".join(vf_parts)])
        if strip_metadata:
            argv.extend(["-map_metadata", "-1"])

        if codec == ImageCodec.JPG:
            argv.extend(["-c:v", "mjpeg", "-q:v", str(q_v), "-color_range", "pc"])
            argv.extend(["-huffman", "optimal"])
        elif codec == ImageCodec.PNG:
            png_level = quality_to_png_level(quality)
            argv.extend(["-c:v", "png", "-compression_level", str(png_level)])
        else:
            webp_q, lossless = quality_to_webp_params(quality)
            argv.extend(["-c:v", "libwebp", "-quality", str(webp_q)])
            if lossless:
                argv.extend(["-lossless", "1"])
            else:
                argv.extend(["-lossless", "0"])
            argv.extend(["-compression_level", "6"])

        argv.append(str(destination))
        result = self.runner.run(argv, timeout=self.timeout, label=f"ffmpeg-enc:{source.name}")
        elapsed = time.perf_counter() - t0

        if not result.ok or not destination.is_file():
            if codec == ImageCodec.JPG and (
                "huffman" in (result.stderr or "").lower() or result.returncode != 0
            ):
                return self._encode_jpg_without_huffman(
                    source,
                    destination,
                    quality=quality,
                    q_v=q_v,
                    vf_parts=vf_parts,
                    strip_metadata=strip_metadata,
                    scale_factor=scale_factor,
                    out_dims=out_dims,
                    t0=t0,
                    previous_error=result.stderr,
                )
            return CompressionAttempt(
                backend=EncodeBackend.FFMPEG,
                quality=quality,
                ffmpeg_q=q_v if codec == ImageCodec.JPG else None,
                scale_factor=scale_factor,
                output_bytes=0,
                elapsed_sec=elapsed,
                success=False,
                error=(result.stderr or result.stdout or "encode_failed")[:600],
                dimensions=out_dims,
                output_path=None,
            )

        out_size = file_size(destination)
        return CompressionAttempt(
            backend=EncodeBackend.FFMPEG,
            quality=quality,
            ffmpeg_q=q_v if codec == ImageCodec.JPG else None,
            scale_factor=scale_factor,
            output_bytes=max(0, out_size),
            elapsed_sec=elapsed,
            success=out_size > 0,
            error=None if out_size > 0 else "zero_byte_output",
            dimensions=out_dims,
            output_path=destination if out_size > 0 else None,
        )

    def _encode_jpg_without_huffman(
        self,
        source: Path,
        destination: Path,
        *,
        quality: int,
        q_v: int,
        vf_parts: List[str],
        strip_metadata: bool,
        scale_factor: float,
        out_dims: Optional[ImageDimensions],
        t0: float,
        previous_error: str,
    ) -> CompressionAttempt:
        argv: List[str] = [
            str(self.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-frames:v",
            "1",
        ]
        if vf_parts:
            argv.extend(["-vf", ",".join(vf_parts)])
        if strip_metadata:
            argv.extend(["-map_metadata", "-1"])
        argv.extend(["-c:v", "mjpeg", "-q:v", str(q_v), str(destination)])
        result = self.runner.run(
            argv, timeout=self.timeout, label=f"ffmpeg-enc-retry:{source.name}"
        )
        elapsed = time.perf_counter() - t0
        if not result.ok or not destination.is_file():
            return CompressionAttempt(
                backend=EncodeBackend.FFMPEG,
                quality=quality,
                ffmpeg_q=q_v,
                scale_factor=scale_factor,
                output_bytes=0,
                elapsed_sec=elapsed,
                success=False,
                error=(result.stderr or previous_error or "encode_failed")[:600],
                dimensions=out_dims,
                output_path=None,
            )
        out_size = file_size(destination)
        return CompressionAttempt(
            backend=EncodeBackend.FFMPEG,
            quality=quality,
            ffmpeg_q=q_v,
            scale_factor=scale_factor,
            output_bytes=max(0, out_size),
            elapsed_sec=elapsed,
            success=out_size > 0,
            error=None if out_size > 0 else "zero_byte_output",
            dimensions=out_dims,
            output_path=destination if out_size > 0 else None,
        )


class PillowEncoder(Encoder):
    name = "pillow"

    def __init__(self, logger: logging.Logger) -> None:
        if not PILLOW_AVAILABLE:
            raise CompressorError("Pillow is not installed")
        self.logger = logger

    def encode_image(
        self,
        source: Path,
        destination: Path,
        *,
        codec: ImageCodec,
        quality: int,
        scale_factor: float = 1.0,
        target_dimensions: Optional[ImageDimensions] = None,
        strip_metadata: bool = True,
        progressive: bool = True,
    ) -> CompressionAttempt:
        assert Image is not None
        t0 = time.perf_counter()
        quality = clamp(int(quality), 1, 100)
        out_dims: Optional[ImageDimensions] = None
        try:
            with Image.open(source) as img:
                img = self._prepare_mode(img, codec)
                if target_dimensions is not None:
                    img = img.resize(
                        (target_dimensions.width, target_dimensions.height),
                        resample=Image.Resampling.LANCZOS,
                    )
                    out_dims = target_dimensions
                elif scale_factor < 0.999:
                    new_w = max(2, int(img.width * scale_factor) // 2 * 2)
                    new_h = max(2, int(img.height * scale_factor) // 2 * 2)
                    img = img.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)
                    out_dims = ImageDimensions(new_w, new_h)
                else:
                    out_dims = ImageDimensions(img.width, img.height)

                destination.parent.mkdir(parents=True, exist_ok=True)
                save_kwargs = self._save_kwargs(codec, quality, progressive, strip_metadata, img)
                img.save(destination, **save_kwargs)
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            return CompressionAttempt(
                backend=EncodeBackend.PILLOW,
                quality=quality,
                ffmpeg_q=None,
                scale_factor=scale_factor,
                output_bytes=0,
                elapsed_sec=elapsed,
                success=False,
                error=str(exc)[:600],
                dimensions=out_dims,
                output_path=None,
            )

        elapsed = time.perf_counter() - t0
        out_size = file_size(destination)
        return CompressionAttempt(
            backend=EncodeBackend.PILLOW,
            quality=quality,
            ffmpeg_q=None,
            scale_factor=scale_factor,
            output_bytes=max(0, out_size),
            elapsed_sec=elapsed,
            success=out_size > 0,
            error=None if out_size > 0 else "zero_byte_output",
            dimensions=out_dims,
            output_path=destination if out_size > 0 else None,
        )

    @staticmethod
    def _prepare_mode(img: Any, codec: ImageCodec) -> Any:
        if codec == ImageCodec.JPG:
            if img.mode in ("RGBA", "LA"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                alpha = img.split()[-1]
                rgb = img.convert("RGBA")
                background.paste(rgb, mask=alpha)
                return background
            if img.mode == "P":
                if "transparency" in img.info:
                    img = img.convert("RGBA")
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[-1])
                    return background
                return img.convert("RGB")
            if img.mode != "RGB":
                return img.convert("RGB")
            return img

        if img.mode == "P":
            return img.convert("RGBA") if "transparency" in img.info else img.convert("RGBA")
        if img.mode == "LA":
            return img.convert("RGBA")
        if img.mode in ("RGBA", "RGB", "L"):
            return img
        if "A" in img.mode:
            return img.convert("RGBA")
        return img.convert("RGB")

    @staticmethod
    def _save_kwargs(
        codec: ImageCodec,
        quality: int,
        progressive: bool,
        strip_metadata: bool,
        img: Any,
    ) -> Dict[str, Any]:
        if codec == ImageCodec.JPG:
            kwargs: Dict[str, Any] = {
                "format": "JPEG",
                "quality": quality,
                "optimize": True,
                "progressive": progressive,
            }
            if not strip_metadata:
                exif = img.info.get("exif")
                if exif:
                    kwargs["exif"] = exif
            return kwargs

        if codec == ImageCodec.PNG:
            return {
                "format": "PNG",
                "optimize": True,
                "compress_level": quality_to_png_level(quality),
            }

        webp_q, lossless = quality_to_webp_params(quality)
        kwargs = {
            "format": "WEBP",
            "quality": webp_q,
            "method": 6,
            "lossless": lossless,
        }
        return kwargs


class DualEncoder:

    def __init__(
        self,
        ffmpeg_encoder: FFmpegEncoder,
        pillow_encoder: Optional[PillowEncoder],
        logger: logging.Logger,
        *,
        prefer: EncodeBackend = EncodeBackend.FFMPEG,
    ) -> None:
        self.ffmpeg_encoder = ffmpeg_encoder
        self.pillow_encoder = pillow_encoder
        self.logger = logger
        self.prefer = prefer

    def encode_image(
        self,
        source: Path,
        destination: Path,
        *,
        codec: ImageCodec,
        quality: int,
        scale_factor: float = 1.0,
        target_dimensions: Optional[ImageDimensions] = None,
        strip_metadata: bool = True,
        progressive: bool = True,
    ) -> CompressionAttempt:
        order: List[Encoder] = []
        if self.prefer == EncodeBackend.PILLOW and self.pillow_encoder:
            order = [self.pillow_encoder, self.ffmpeg_encoder]
        else:
            order = [self.ffmpeg_encoder]
            if self.pillow_encoder:
                order.append(self.pillow_encoder)

        last: Optional[CompressionAttempt] = None
        for enc in order:
            tmp = destination.with_suffix(destination.suffix + f".{enc.name}.part")
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

            attempt = enc.encode_image(
                source,
                tmp,
                codec=codec,
                quality=quality,
                scale_factor=scale_factor,
                target_dimensions=target_dimensions,
                strip_metadata=strip_metadata,
                progressive=progressive,
            )
            last = attempt
            if attempt.success and attempt.output_bytes > 0:
                try:
                    atomic_replace(tmp, destination)
                except OSError as exc:
                    attempt.success = False
                    attempt.error = f"atomic_replace_failed: {exc}"
                    attempt.output_path = None
                    last = attempt
                    continue
                attempt.output_bytes = file_size(destination)
                attempt.output_path = destination
                return attempt

            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            self.logger.debug("Encoder %s failed for %s: %s", enc.name, source.name, attempt.error)

        assert last is not None
        return last


class StrategyEngine:

    def __init__(self, encoder: DualEncoder, logger: logging.Logger, config: RuntimeConfig) -> None:
        self.encoder = encoder
        self.logger = logger
        self.config = config

    def _codec_for(self, path: Path) -> ImageCodec:
        return resolve_output_codec(path, self.config.output_format)

    def _temp_suffix(self, path: Path) -> str:
        return self._codec_for(path).extension

    def _encode(
        self,
        source: Path,
        destination: Path,
        *,
        quality: int,
        scale_factor: float = 1.0,
        target_dimensions: Optional[ImageDimensions] = None,
    ) -> CompressionAttempt:
        codec = self._codec_for(source)
        return self.encoder.encode_image(
            source,
            destination,
            codec=codec,
            quality=quality,
            scale_factor=scale_factor,
            target_dimensions=target_dimensions,
            strip_metadata=self.config.strip_metadata,
            progressive=self.config.progressive_jpeg,
        )

    def process_image(
        self, probe: ImageProbeResult, final_dest: Path, work_dir: Path
    ) -> ImageJobResult:
        t0 = time.perf_counter()
        source = probe.path
        result = ImageJobResult(
            source=source,
            status=ImageStatus.PENDING,
            original_bytes=probe.size_bytes,
            original_dimensions=probe.dimensions,
            strategy_used=self.config.strategy,
        )
        if not probe.is_readable:
            result.status = ImageStatus.SKIPPED_CORRUPT
            result.message = probe.probe_error or "unreadable"
            result.error_detail = probe.probe_error
            result.elapsed_sec = time.perf_counter() - t0
            return result

        target_codec = self._codec_for(source)
        same_format = codecs_match_for_copy(source, target_codec)

        if probe.size_bytes < self.config.max_bytes and same_format:
            return self._handle_under_limit(probe, final_dest, result, t0)

        if self.config.strategy == CompressionStrategy.COPY_ONLY_UNDER_LIMIT:
            if probe.size_bytes < self.config.max_bytes and same_format:
                return self._handle_under_limit(probe, final_dest, result, t0)
            result.status = ImageStatus.SKIPPED_UNDER_LIMIT
            result.message = t("msg_copy_only_skip")
            result.elapsed_sec = time.perf_counter() - t0
            return result

        if self.config.dry_run:
            result.status = ImageStatus.PENDING
            result.message = t("msg_dry_run")
            result.elapsed_sec = time.perf_counter() - t0
            return result

        try:
            if probe.size_bytes < self.config.max_bytes and not same_format:
                converted = self._try_format_convert_only(probe, final_dest, work_dir, result)
                if converted:
                    result.elapsed_sec = time.perf_counter() - t0
                    return result

            if self.config.strategy == CompressionStrategy.LOSSLESS_FIRST:
                self._strategy_lossless_first(probe, final_dest, work_dir, result)
            elif self.config.strategy == CompressionStrategy.HIGH_QUALITY_LOSSY:
                self._strategy_high_quality(probe, final_dest, work_dir, result)
            elif self.config.strategy == CompressionStrategy.BINARY_SEARCH:
                self._strategy_binary_search(probe, final_dest, work_dir, result)
            else:
                self._strategy_aggressive_adaptive(probe, final_dest, work_dir, result)
        except Exception as exc:
            self.logger.exception("Unhandled error processing %s", source.name)
            result.status = ImageStatus.FAILED
            result.message = "unhandled_exception"
            result.error_detail = f"{type(exc).__name__}: {exc}"
            result.elapsed_sec = time.perf_counter() - t0
            return result

        result.elapsed_sec = time.perf_counter() - t0
        return result

    def _try_format_convert_only(
        self,
        probe: ImageProbeResult,
        final_dest: Path,
        work_dir: Path,
        result: ImageJobResult,
    ) -> bool:
        tmp = unique_temp_path(work_dir, suffix=self._temp_suffix(probe.path))
        attempt = self._encode(probe.path, tmp, quality=QUALITY_LOSSLESS_PROXY, scale_factor=1.0)
        result.attempts.append(attempt)
        if attempt.success and attempt.under_limit:
            self._commit_best(attempt, final_dest, result, quality=QUALITY_LOSSLESS_PROXY)
            return result.status == ImageStatus.COMPRESSED
        return False

    def _handle_under_limit(
        self, probe: ImageProbeResult, final_dest: Path, result: ImageJobResult, t0: float
    ) -> ImageJobResult:
        if not self.config.copy_under_limit:
            result.status = ImageStatus.SKIPPED_UNDER_LIMIT
            result.message = t("msg_already_not_copied")
            result.elapsed_sec = time.perf_counter() - t0
            return result
        try:
            final_dest.parent.mkdir(parents=True, exist_ok=True)
            if not self.config.dry_run:
                shutil.copy2(probe.path, final_dest)
            result.output_path = final_dest
            result.output_bytes = probe.size_bytes
            result.output_dimensions = probe.dimensions
            result.backend = EncodeBackend.COPY
            result.status = ImageStatus.COPIED
            result.message = t("msg_already_copied")
        except OSError as exc:
            result.status = ImageStatus.FAILED
            result.message = "copy_failed"
            result.error_detail = str(exc)
        result.elapsed_sec = time.perf_counter() - t0
        return result

    def _strategy_lossless_first(
        self, probe: ImageProbeResult, final_dest: Path, work_dir: Path, result: ImageJobResult
    ) -> None:
        best = self._phase_near_lossless(probe, work_dir, result)
        if best and best.under_limit:
            self._commit_best(best, final_dest, result, quality=QUALITY_LOSSLESS_PROXY)
            return
        best = self._phase_quality_ladder(
            probe,
            work_dir,
            result,
            qualities=(95, 93, 90, 88, 85),
            scale_factor=1.0,
            previous_best=best,
        )
        if best and best.under_limit:
            self._commit_best(best, final_dest, result)
            return
        if self.config.allow_downscale:
            best = self._phase_downscale_search(probe, work_dir, result, previous_best=best)
            if best and best.under_limit:
                self._commit_best(best, final_dest, result)
                return
        self._fail_or_commit_closest(best, final_dest, result)

    def _strategy_high_quality(
        self, probe: ImageProbeResult, final_dest: Path, work_dir: Path, result: ImageJobResult
    ) -> None:
        best = self._phase_quality_ladder(
            probe, work_dir, result, qualities=(95, 93, 92, 90), scale_factor=1.0
        )
        if best and best.under_limit:
            self._commit_best(best, final_dest, result)
            return
        best = self._phase_binary_search(
            probe,
            work_dir,
            result,
            q_lo=QUALITY_BINARY_MIN,
            q_hi=89,
            scale_factor=1.0,
            previous_best=best,
        )
        if best and best.under_limit:
            self._commit_best(best, final_dest, result)
            return
        if self.config.allow_downscale:
            best = self._phase_downscale_search(probe, work_dir, result, previous_best=best)
            if best and best.under_limit:
                self._commit_best(best, final_dest, result)
                return
        self._fail_or_commit_closest(best, final_dest, result)

    def _strategy_binary_search(
        self, probe: ImageProbeResult, final_dest: Path, work_dir: Path, result: ImageJobResult
    ) -> None:
        best = self._phase_binary_search(
            probe,
            work_dir,
            result,
            q_lo=QUALITY_BINARY_MIN,
            q_hi=QUALITY_BINARY_MAX,
            scale_factor=1.0,
        )
        if best and best.under_limit:
            self._commit_best(best, final_dest, result)
            return
        if self.config.allow_downscale:
            best = self._phase_downscale_search(probe, work_dir, result, previous_best=best)
            if best and best.under_limit:
                self._commit_best(best, final_dest, result)
                return
        self._fail_or_commit_closest(best, final_dest, result)

    def _strategy_aggressive_adaptive(
        self, probe: ImageProbeResult, final_dest: Path, work_dir: Path, result: ImageJobResult
    ) -> None:
        best = self._phase_near_lossless(probe, work_dir, result)
        if best and best.under_limit:
            self._commit_best(best, final_dest, result, quality=QUALITY_LOSSLESS_PROXY)
            return
        best = self._phase_quality_ladder(
            probe, work_dir, result, qualities=(95, 92, 90), scale_factor=1.0, previous_best=best
        )
        if best and best.under_limit:
            self._commit_best(best, final_dest, result)
            return
        best = self._phase_binary_search(
            probe,
            work_dir,
            result,
            q_lo=QUALITY_BINARY_MIN,
            q_hi=89,
            scale_factor=1.0,
            previous_best=best,
        )
        if best and best.under_limit:
            self._commit_best(best, final_dest, result)
            return
        if self.config.allow_downscale:
            best = self._phase_downscale_search(probe, work_dir, result, previous_best=best)
            if best and best.under_limit:
                self._commit_best(best, final_dest, result)
                return
        self._fail_or_commit_closest(best, final_dest, result)

    def _phase_near_lossless(
        self, probe: ImageProbeResult, work_dir: Path, result: ImageJobResult
    ) -> Optional[CompressionAttempt]:
        tmp = unique_temp_path(work_dir, suffix=self._temp_suffix(probe.path))
        attempt = self._encode(
            probe.path,
            tmp,
            quality=QUALITY_LOSSLESS_PROXY,
            scale_factor=1.0,
        )
        result.attempts.append(attempt)
        if attempt.success:
            return attempt
        return None

    def _phase_quality_ladder(
        self,
        probe: ImageProbeResult,
        work_dir: Path,
        result: ImageJobResult,
        *,
        qualities: Sequence[int],
        scale_factor: float,
        previous_best: Optional[CompressionAttempt] = None,
    ) -> Optional[CompressionAttempt]:
        best = previous_best
        for q in qualities:
            tmp = unique_temp_path(work_dir, suffix=self._temp_suffix(probe.path))
            attempt = self._encode(
                probe.path,
                tmp,
                quality=q,
                scale_factor=scale_factor,
            )
            result.attempts.append(attempt)
            if not attempt.success:
                continue
            best = self._better_attempt(best, attempt)
            if attempt.under_limit:
                return best
        return best

    def _phase_binary_search(
        self,
        probe: ImageProbeResult,
        work_dir: Path,
        result: ImageJobResult,
        *,
        q_lo: int,
        q_hi: int,
        scale_factor: float,
        previous_best: Optional[CompressionAttempt] = None,
        target_dims: Optional[ImageDimensions] = None,
    ) -> Optional[CompressionAttempt]:
        lo = clamp(q_lo, 1, 100)
        hi = clamp(q_hi, 1, 100)
        if lo > hi:
            lo, hi = (hi, lo)
        best_under: Optional[CompressionAttempt] = (
            previous_best if previous_best and previous_best.under_limit else None
        )
        best_any: Optional[CompressionAttempt] = previous_best
        iterations = 0
        while lo <= hi and iterations < BINARY_SEARCH_MAX_ITERS:
            iterations += 1
            mid = (lo + hi) // 2
            tmp = unique_temp_path(work_dir, suffix=self._temp_suffix(probe.path))
            attempt = self._encode(
                probe.path,
                tmp,
                quality=mid,
                scale_factor=scale_factor,
                target_dimensions=target_dims,
            )
            result.attempts.append(attempt)
            if not attempt.success:
                hi = mid - 1
                continue
            best_any = self._better_attempt(best_any, attempt)
            if attempt.output_bytes < self.config.max_bytes:
                best_under = (
                    attempt
                    if best_under is None
                    or (attempt.quality or 0) > (best_under.quality or 0)
                    or (
                        (attempt.quality or 0) == (best_under.quality or 0)
                        and attempt.output_bytes < best_under.output_bytes
                    )
                    else best_under
                )
                lo = mid + 1
            else:
                hi = mid - 1
        if best_under is not None:
            return best_under
        return best_any

    def _phase_downscale_search(
        self,
        probe: ImageProbeResult,
        work_dir: Path,
        result: ImageJobResult,
        *,
        previous_best: Optional[CompressionAttempt] = None,
    ) -> Optional[CompressionAttempt]:
        if probe.dimensions is None:
            return previous_best
        best = previous_best
        for factor in DOWNSCALE_FACTORS:
            target = probe.dimensions.scaled(factor)
            if (
                target.width == probe.dimensions.width
                and target.height == probe.dimensions.height
                and (factor < 1.0)
            ):
                continue
            self.logger.debug("Downscale %.0f%% → %s for %s", factor * 100, target, probe.path.name)
            candidate = self._phase_binary_search(
                probe,
                work_dir,
                result,
                q_lo=QUALITY_BINARY_MIN,
                q_hi=QUALITY_BINARY_MAX,
                scale_factor=factor,
                target_dims=target,
                previous_best=None,
            )
            if candidate is not None:
                if candidate.dimensions is None:
                    candidate.dimensions = target
                candidate.scale_factor = factor
                best = self._better_attempt(best, candidate)
                if candidate.under_limit:
                    return candidate
        return best

    @staticmethod
    def _better_attempt(
        current: Optional[CompressionAttempt], new: CompressionAttempt
    ) -> CompressionAttempt:
        if current is None or not current.success:
            return new if new.success else current or new
        if not new.success:
            return current
        cur_under = current.under_limit
        new_under = new.under_limit
        if new_under and (not cur_under):
            return new
        if cur_under and (not new_under):
            return current
        if new_under and cur_under:
            nq, cq = (new.quality or 0, current.quality or 0)
            if nq != cq:
                return new if nq > cq else current
            return new if new.output_bytes < current.output_bytes else current
        return new if new.output_bytes < current.output_bytes else current

    def _commit_best(
        self,
        best: CompressionAttempt,
        final_dest: Path,
        result: ImageJobResult,
        *,
        quality: Optional[int] = None,
    ) -> None:
        final_dest.parent.mkdir(parents=True, exist_ok=True)
        if (
            best.success
            and best.output_path is not None
            and best.output_path.is_file()
            and (best.output_bytes > 0)
        ):
            live_size = file_size(best.output_path)
            if live_size > 0:
                best.output_bytes = live_size
            if best.output_bytes >= self.config.max_bytes:
                result.status = ImageStatus.SIZE_LIMIT_FAILED
                result.output_bytes = best.output_bytes
                result.quality_used = best.quality
                result.ffmpeg_q_used = best.ffmpeg_q
                result.scale_factor = best.scale_factor
                result.backend = best.backend
                result.message = t(
                    "msg_size_fail",
                    limit=human_bytes(self.config.max_bytes),
                    best=human_bytes(best.output_bytes),
                )
                return
            try:
                atomic_replace(best.output_path, final_dest)
            except OSError as exc:
                result.status = ImageStatus.FAILED
                result.message = "commit_failed"
                result.error_detail = str(exc)
                return
            result.output_path = final_dest
            result.output_bytes = file_size(final_dest)
            result.output_dimensions = best.dimensions or result.original_dimensions
            result.quality_used = best.quality
            result.ffmpeg_q_used = best.ffmpeg_q
            result.scale_factor = best.scale_factor
            result.backend = best.backend
            result.status = ImageStatus.COMPRESSED
            result.message = t(
                "msg_compressed",
                size=human_bytes(result.output_bytes),
                q=result.quality_used,
                scale=result.scale_factor,
            )
            return
        q = best.quality if best.quality is not None else 85
        if quality is not None:
            q = quality
        part = final_dest.with_suffix(final_dest.suffix + ".finalpart")
        attempt = self._encode(
            result.source,
            part,
            quality=q,
            scale_factor=best.scale_factor,
            target_dimensions=best.dimensions if best.scale_factor < 0.999 else None,
        )
        result.attempts.append(attempt)
        if not attempt.success or attempt.output_bytes <= 0:
            result.status = ImageStatus.FAILED
            result.message = "final_encode_failed"
            result.error_detail = attempt.error
            try:
                if part.exists():
                    part.unlink()
            except OSError:
                pass
            return
        if attempt.output_bytes >= self.config.max_bytes:
            q2 = clamp((attempt.quality or q) - 3, 1, 100)
            attempt2 = self._encode(
                result.source,
                part,
                quality=q2,
                scale_factor=best.scale_factor,
                target_dimensions=best.dimensions if best.scale_factor < 0.999 else None,
            )
            result.attempts.append(attempt2)
            attempt = attempt2
            if attempt.output_bytes >= self.config.max_bytes or not attempt.success:
                try:
                    if part.exists():
                        part.unlink()
                except OSError:
                    pass
                result.status = ImageStatus.SIZE_LIMIT_FAILED
                result.message = t(
                    "msg_size_fail",
                    limit=human_bytes(self.config.max_bytes),
                    best=human_bytes(attempt.output_bytes),
                )
                result.output_bytes = attempt.output_bytes
                result.quality_used = attempt.quality
                result.ffmpeg_q_used = attempt.ffmpeg_q
                result.scale_factor = attempt.scale_factor
                result.backend = attempt.backend
                return
        try:
            src_part = attempt.output_path if attempt.output_path else part
            atomic_replace(src_part, final_dest)
        except OSError as exc:
            result.status = ImageStatus.FAILED
            result.message = "commit_failed"
            result.error_detail = str(exc)
            return
        result.output_path = final_dest
        result.output_bytes = file_size(final_dest)
        result.output_dimensions = attempt.dimensions or result.original_dimensions
        result.quality_used = attempt.quality
        result.ffmpeg_q_used = attempt.ffmpeg_q
        result.scale_factor = attempt.scale_factor
        result.backend = attempt.backend
        result.status = ImageStatus.COMPRESSED
        result.message = t(
            "msg_compressed",
            size=human_bytes(result.output_bytes),
            q=result.quality_used,
            scale=result.scale_factor,
        )

    def _fail_or_commit_closest(
        self, best: Optional[CompressionAttempt], final_dest: Path, result: ImageJobResult
    ) -> None:
        if best is None or not best.success:
            result.status = ImageStatus.FAILED
            result.message = "all_encode_attempts_failed"
            if result.attempts:
                result.error_detail = result.attempts[-1].error
            return
        if best.under_limit:
            self._commit_best(best, final_dest, result)
            return
        result.status = ImageStatus.SIZE_LIMIT_FAILED
        result.output_bytes = best.output_bytes
        result.quality_used = best.quality
        result.ffmpeg_q_used = best.ffmpeg_q
        result.scale_factor = best.scale_factor
        result.backend = best.backend
        result.message = t(
            "msg_unable",
            limit=human_bytes(self.config.max_bytes),
            best=human_bytes(best.output_bytes),
            q=best.quality,
            scale=best.scale_factor,
        )


class PreflightScanner:

    def __init__(
        self,
        root: Path,
        prober: ImageProber,
        logger: logging.Logger,
        *,
        include_convertibles: bool = True,
        output_dir: Optional[Path] = None,
    ) -> None:
        self.root = root.resolve()
        self.prober = prober
        self.logger = logger
        self.include_convertibles = include_convertibles
        self.output_dir = output_dir.resolve() if output_dir else None

    def discover_files(self) -> List[Path]:
        found: List[Path] = []
        try:
            entries = sorted(self.root.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            raise WorkspaceError(f"Cannot list directory {self.root}: {exc}", cause=exc) from exc
        for entry in entries:
            if not entry.is_file():
                continue
            if self.output_dir and is_within_directory(entry, self.output_dir):
                continue
            suffix = entry.suffix.lower()
            stem_lower = entry.stem.lower()
            if stem_lower in EXCLUDED_BASENAMES:
                continue
            if entry.name.lower() in {n.lower() for n in (*FFMPEG_NAMES, *FFPROBE_NAMES)}:
                continue
            if suffix in JPEG_EXTENSIONS:
                found.append(entry)
            elif self.include_convertibles and suffix in CONVERTIBLE_EXTENSIONS:
                found.append(entry)
        return found

    def scan(self, *, max_workers: int = 4) -> PreflightSummary:
        t0 = time.perf_counter()
        files = self.discover_files()
        self.logger.info("Discovered %d candidate image(s) in %s", len(files), self.root)
        images: List[ImageProbeResult] = []
        workers = max(1, min(max_workers, len(files) or 1))
        if files:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {pool.submit(self.prober.probe, f): f for f in files}
                for fut in as_completed(future_map):
                    path = future_map[fut]
                    try:
                        images.append(fut.result())
                    except Exception as exc:
                        self.logger.warning("Probe crash on %s: %s", path.name, exc)
                        images.append(
                            ImageProbeResult(
                                path=path,
                                size_bytes=file_size(path),
                                dimensions=None,
                                codec_name=None,
                                pixel_format=None,
                                color_space=None,
                                bit_depth=None,
                                is_readable=False,
                                is_jpeg=path.suffix.lower() in JPEG_EXTENSIONS,
                                has_metadata_hint=False,
                                probe_error=str(exc),
                            )
                        )
        images.sort(key=lambda i: i.path.name.lower())
        return self._summarise(images, time.perf_counter() - t0)

    def _summarise(self, images: List[ImageProbeResult], elapsed: float) -> PreflightSummary:
        jpeg_count = sum((1 for i in images if i.path.suffix.lower() in JPEG_EXTENSIONS))
        convertible_count = len(images) - jpeg_count
        under = [i for i in images if i.is_readable and (not i.over_limit)]
        over = [i for i in images if i.is_readable and i.over_limit]
        corrupt = [i for i in images if not i.is_readable]
        total_bytes = sum((i.size_bytes for i in images))
        over_bytes = sum((i.size_bytes for i in over))
        savings_low = 0
        savings_high = 0
        for img in over:
            est_high_q = estimate_output_bytes(img.size_bytes, 93)
            est_low_q = estimate_output_bytes(img.size_bytes, 55)
            max_save = max(0, img.size_bytes - EFFECTIVE_TARGET_BYTES)
            savings_low += min(max_save, max(0, img.size_bytes - est_high_q))
            savings_high += min(max_save, max(0, img.size_bytes - est_low_q))
        for img in under:
            pass
        widths = [i.dimensions.width for i in images if i.dimensions]
        heights = [i.dimensions.height for i in images if i.dimensions]
        dim_stats: Dict[str, Any] = {}
        if widths:
            dim_stats = {
                "min_width": min(widths),
                "max_width": max(widths),
                "min_height": min(heights),
                "max_height": max(heights),
                "avg_width": int(sum(widths) / len(widths)),
                "avg_height": int(sum(heights) / len(heights)),
                "avg_mp": round(
                    sum((i.dimensions.megapixels for i in images if i.dimensions))
                    / max(1, len(widths)),
                    2,
                ),
            }
        pot_hist = Counter((i.compression_potential for i in images))
        return PreflightSummary(
            root=self.root,
            images=images,
            total_files_scanned=len(images),
            jpeg_count=jpeg_count,
            convertible_count=convertible_count,
            under_limit_count=len(under),
            over_limit_count=len(over),
            corrupt_count=len(corrupt),
            total_bytes=total_bytes,
            over_limit_bytes=over_bytes,
            potential_savings_low_mb=savings_low / (1024 * 1024),
            potential_savings_high_mb=savings_high / (1024 * 1024),
            scan_elapsed_sec=elapsed,
            dimension_stats=dim_stats,
            potential_histogram=dict(pot_hist),
        )


class CLI:

    def __init__(self) -> None:
        if RICH_AVAILABLE:
            self.console = Console(theme=RICH_THEME, highlight=False)
        else:
            self.console = None

    def print(self, message: str = "", *, style: Optional[str] = None) -> None:
        if self.console is not None:
            self.console.print(message, style=style)
        else:
            print(re.sub("\\[/?[^\\]]+\\]", "", message))

    def rule(self, title: str = "") -> None:
        if self.console is not None:
            self.console.rule(title, style="header")
        else:
            bar = "=" * 72
            print(f"\n{bar}\n{title}\n{bar}")

    def select_language(self) -> None:
        self.rule(t("lang_prompt"))
        options = {"1": "en", "2": "vi", "en": "en", "vi": "vi", "e": "en", "v": "vi"}
        if self.console is not None:
            table = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
            table.add_column("k", style="accent")
            table.add_column("v")
            table.add_row("1", t("lang_en"))
            table.add_row("2", t("lang_vi"))
            self.console.print(table)
            while True:
                raw = Prompt.ask(
                    t("lang_prompt"), choices=["1", "2"], default="1", console=self.console
                )
                choice = options.get(raw.strip().lower())
                if choice:
                    set_language(choice)
                    return
        else:
            print(f"  1) {t('lang_en')}")
            print(f"  2) {t('lang_vi')}")
            while True:
                raw = input(f"{t('lang_prompt')} [1]: ").strip().lower() or "1"
                choice = options.get(raw)
                if choice:
                    set_language(choice)
                    return
                print(t("invalid_selection"))

    def banner(self) -> None:
        title = f"{SCRIPT_NAME} v{SCRIPT_VERSION}"
        subtitle = t("banner_subtitle")
        if self.console is not None:
            self.console.print(
                Panel(
                    Align.center(
                        Text.from_markup(f"[title]{title}[/title]\n[muted]{subtitle}[/muted]")
                    ),
                    border_style="bright_cyan",
                    box=box.DOUBLE if hasattr(box, "DOUBLE") else box.ROUNDED,
                )
            )
        else:
            print(f"\n*** {title} ***\n{subtitle}\n")

    def show_environment(
        self, root: Path, ffmpeg: Path, ffprobe: Path, ffmpeg_ver: str, ffprobe_ver: str
    ) -> None:
        self.rule(t("rule_environment"))
        rows = [
            (t("env_workdir"), str(root)),
            (t("env_ffmpeg"), f"{ffmpeg}  ({ffmpeg_ver[:60]})"),
            (t("env_ffprobe"), f"{ffprobe}  ({ffprobe_ver[:60]})"),
            (t("env_pillow"), t("avail_yes") if PILLOW_AVAILABLE else t("avail_pillow_no")),
            (t("env_rich"), t("avail_yes") if RICH_AVAILABLE else t("avail_rich_no")),
            (t("env_python"), sys.version.split()[0]),
            (t("env_platform"), f"{platform.system()} {platform.release()}"),
            (t("env_cpu"), str(os.cpu_count() or "?")),
            (t("env_size_limit"), t("size_limit_val", size=human_bytes(DEFAULT_MAX_BYTES))),
            (t("env_effective"), human_bytes(EFFECTIVE_TARGET_BYTES)),
        ]
        if self.console is not None:
            table = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
            table.add_column("Key", style="accent")
            table.add_column("Value")
            for k, v in rows:
                table.add_row(k, v)
            self.console.print(table)
        else:
            for k, v in rows:
                print(f"  {k:20s}: {v}")

    def show_preflight(self, summary: PreflightSummary) -> None:
        self.rule(t("rule_preflight"))
        if self.console is not None:
            overview = Table(title=t("scan_overview"), box=box.ROUNDED, show_lines=False)
            overview.add_column(t("metric"), style="header")
            overview.add_column(t("value"), justify="right")
            overview.add_row(t("files_discovered"), str(summary.total_files_scanned))
            overview.add_row(t("jpeg"), str(summary.jpeg_count))
            overview.add_row(t("convertible"), str(summary.convertible_count))
            overview.add_row(t("already_under"), f"[size_ok]{summary.under_limit_count}[/size_ok]")
            overview.add_row(
                t("need_compression"), f"[size_bad]{summary.over_limit_count}[/size_bad]"
            )
            overview.add_row(t("unreadable_corrupt"), f"[warning]{summary.corrupt_count}[/warning]")
            overview.add_row(t("total_size"), human_bytes(summary.total_bytes))
            overview.add_row(t("over_limit_mass"), human_bytes(summary.over_limit_bytes))
            overview.add_row(
                t("est_savings_gentle"), f"~{summary.potential_savings_low_mb:.1f} MiB"
            )
            overview.add_row(t("est_savings_aggr"), f"~{summary.potential_savings_high_mb:.1f} MiB")
            overview.add_row(t("scan_time"), human_duration(summary.scan_elapsed_sec))
            if summary.dimension_stats:
                ds = summary.dimension_stats
                overview.add_row(
                    t("dim_range"),
                    f"{ds.get('min_width')}×{ds.get('min_height')} … {ds.get('max_width')}×{ds.get('max_height')}",
                )
                overview.add_row(t("avg_mp"), str(ds.get("avg_mp")))
            self.console.print(overview)
            if summary.potential_histogram:
                hist = Table(title=t("comp_potential"), box=box.SIMPLE)
                hist.add_column(t("bucket"))
                hist.add_column(t("count"), justify="right")
                for key in (
                    "already_ok",
                    "easy",
                    "moderate",
                    "challenging",
                    "difficult",
                    "unreadable",
                ):
                    if key in summary.potential_histogram:
                        hist.add_row(key, str(summary.potential_histogram[key]))
                self.console.print(hist)
            detail = Table(title=t("per_file_inv"), box=box.MINIMAL_DOUBLE_HEAD, show_lines=False)
            detail.add_column("#", justify="right", style="muted")
            detail.add_column(t("col_file"), overflow="fold")
            detail.add_column(t("col_size"), justify="right")
            detail.add_column(t("col_dims"), justify="center")
            detail.add_column(t("col_codec"))
            detail.add_column(t("col_status"))
            detail.add_column(t("col_need"), justify="right")
            detail.add_column(t("col_potential"))
            max_rows = 200
            for idx, img in enumerate(summary.images[:max_rows], start=1):
                size_style = (
                    "size_ok"
                    if not img.over_limit and img.is_readable
                    else "size_bad" if img.is_readable else "warning"
                )
                status = (
                    "OK"
                    if img.is_readable and (not img.over_limit)
                    else "OVER" if img.is_readable else "BAD"
                )
                dims = str(img.dimensions) if img.dimensions else "—"
                need = f"{img.estimated_reduction_needed_pct:.0f}%" if img.over_limit else "—"
                detail.add_row(
                    str(idx),
                    rich_escape(img.path.name),
                    f"[{size_style}]{human_bytes(img.size_bytes)}[/{size_style}]",
                    dims,
                    img.codec_name or "—",
                    status,
                    need,
                    img.compression_potential,
                )
            if len(summary.images) > max_rows:
                detail.caption = t("showing_first", n=max_rows, total=len(summary.images))
            self.console.print(detail)
        else:
            print(f"  {t('plain_files', n=summary.total_files_scanned)}")
            print(f"  {t('plain_jpeg_conv', j=summary.jpeg_count, c=summary.convertible_count)}")
            print(
                f"  {t('plain_under_over', u=summary.under_limit_count, o=summary.over_limit_count)}"
            )
            print(f"  {t('plain_corrupt', n=summary.corrupt_count)}")
            print(f"  {t('plain_total', s=human_bytes(summary.total_bytes))}")
            print(
                f"  {t('plain_savings', lo=summary.potential_savings_low_mb, hi=summary.potential_savings_high_mb)}"
            )
            print()
            for img in summary.images:
                flag = "OK " if not img.over_limit else "OVER"
                if not img.is_readable:
                    flag = "BAD"
                dims = str(img.dimensions) if img.dimensions else "?"
                print(f"  [{flag}] {img.path.name:40s} {human_bytes(img.size_bytes):>10s}  {dims}")

    def recommend_strategy(self, summary: PreflightSummary) -> CompressionStrategy:
        if summary.over_limit_count == 0:
            return CompressionStrategy.COPY_ONLY_UNDER_LIMIT
        difficult = summary.potential_histogram.get(
            "difficult", 0
        ) + summary.potential_histogram.get("challenging", 0)
        if difficult >= max(1, summary.over_limit_count // 3):
            return CompressionStrategy.AGGRESSIVE_ADAPTIVE
        if summary.over_limit_count <= 5:
            return CompressionStrategy.BINARY_SEARCH
        avg_over = (
            summary.over_limit_bytes / summary.over_limit_count if summary.over_limit_count else 0
        )
        if avg_over < 7 * 1024 * 1024:
            return CompressionStrategy.HIGH_QUALITY_LOSSY
        return CompressionStrategy.AGGRESSIVE_ADAPTIVE

    def select_strategy(self, summary: PreflightSummary) -> CompressionStrategy:
        self.rule(t("rule_strategy"))
        recommended = self.recommend_strategy(summary)
        strategies = list(CompressionStrategy)
        if self.console is not None:
            table = Table(title=t("strat_available"), box=box.ROUNDED, show_lines=True)
            table.add_column(t("col_opt"), style="accent", justify="center")
            table.add_column(t("col_name"), style="title")
            table.add_column(t("col_desc"))
            for s in strategies:
                name = s.title
                if s is recommended:
                    name = f"{name}  [success]{t('recommended_tag')}[/success]"
                table.add_row(s.value, name, s.description)
            self.console.print(table)
            self.print(
                f"\n[muted]{t('recommended_line', opt=recommended.value, title=recommended.title)}[/muted]"
            )
            while True:
                choice = (
                    Prompt.ask(
                        t("select_strategy"),
                        choices=[s.value for s in strategies],
                        default=recommended.value,
                        console=self.console,
                    )
                    .strip()
                    .upper()
                )
                for s in strategies:
                    if s.value == choice:
                        return s
                self.print(f"[error]{t('invalid_selection')}[/error]")
        else:
            print(f"\n{t('strat_available')}:")
            for s in strategies:
                rec = t("recommended_paren") if s is recommended else ""
                print(f"  {s.value}) {s.title}{rec}")
                print(f"      {s.description}\n")
            while True:
                raw = input(f"{t('select_strategy')} [{recommended.value}]: ").strip().upper()
                if not raw:
                    return recommended
                for s in strategies:
                    if s.value == raw:
                        return s
                print(t("invalid_selection_abc"))

    def select_output_format(self) -> OutputFormatChoice:
        self.rule(t("rule_format"))
        options = list(OutputFormatChoice)
        recommended = OutputFormatChoice.JPG
        if self.console is not None:
            table = Table(title=t("format_available"), box=box.ROUNDED, show_lines=True)
            table.add_column(t("col_opt"), style="accent", justify="center")
            table.add_column(t("col_name"), style="title")
            table.add_column(t("col_desc"))
            for opt in options:
                name = opt.title
                if opt is recommended:
                    name = f"{name}  [success]{t('recommended_tag')}[/success]"
                table.add_row(opt.value, name, opt.description)
            self.console.print(table)
            self.print(
                f"\n[muted]{t('format_recommended', opt=recommended.value, title=recommended.title)}[/muted]"
            )
            while True:
                choice = Prompt.ask(
                    t("select_format"),
                    choices=[o.value for o in options],
                    default=recommended.value,
                    console=self.console,
                ).strip()
                for opt in options:
                    if opt.value == choice:
                        return opt
                self.print(f"[error]{t('invalid_format')}[/error]")
        else:
            print(f"\n{t('format_available')}:")
            for opt in options:
                rec = t("recommended_paren") if opt is recommended else ""
                print(f"  {opt.value}) {opt.title}{rec}")
                print(f"      {opt.description}\n")
            while True:
                raw = (
                    input(f"{t('select_format')} [{recommended.value}]: ").strip()
                    or recommended.value
                )
                for opt in options:
                    if opt.value == raw:
                        return opt
                print(t("invalid_format"))

    def confirm_start(self, strategy: CompressionStrategy, over_count: int) -> bool:
        plain = t("confirm_proceed", title=strategy.title, n=over_count)
        rich_msg = t(
            "confirm_proceed",
            title=f"[accent]{strategy.title}[/accent]",
            n=f"[warning]{over_count}[/warning]",
        )
        if self.console is not None:
            return Confirm.ask(rich_msg, default=True, console=self.console)
        print(plain)
        raw = input(t("continue_yn")).strip().lower()
        return raw in ("", "y", "yes")

    def show_batch_report(self, report: BatchReport) -> None:
        self.rule(t("rule_complete"))
        fail_statuses = (
            ImageStatus.FAILED,
            ImageStatus.SIZE_LIMIT_FAILED,
            ImageStatus.SKIPPED_CORRUPT,
            ImageStatus.SKIPPED_UNSUPPORTED,
        )
        failed_rows = [r for r in report.results if r.status in fail_statuses]

        if self.console is not None:
            table = Table(title=t("results_summary"), box=box.ROUNDED)
            table.add_column(t("metric"), style="header")
            table.add_column(t("value"), justify="right")
            table.add_row(t("res_strategy"), report.config.strategy.title)
            table.add_row(t("res_format"), report.config.output_format.title)
            table.add_row(t("res_elapsed"), human_duration(report.total_elapsed_sec))
            table.add_row(t("res_compressed"), str(report.compressed_count))
            table.add_row(t("res_copied"), str(report.copied_count))
            table.add_row(t("res_failed"), str(report.failed_count))
            table.add_row(t("res_saved"), human_bytes(report.total_saved_bytes))
            table.add_row(t("res_output"), str(report.config.output_dir))
            self.console.print(table)

            detail = Table(title=t("per_file_results"), box=box.MINIMAL_DOUBLE_HEAD)
            detail.add_column(t("col_file"), overflow="fold")
            detail.add_column(t("col_status"))
            detail.add_column(t("col_in"), justify="right")
            detail.add_column(t("col_out"), justify="right")
            detail.add_column(t("col_saved"), justify="right")
            detail.add_column(t("col_q"), justify="right")
            detail.add_column(t("col_scale"), justify="right")
            detail.add_column(t("col_notes"), overflow="fold")
            for r in report.results:
                status_style = {
                    ImageStatus.COMPRESSED: "success",
                    ImageStatus.COPIED: "size_ok",
                    ImageStatus.SKIPPED_UNDER_LIMIT: "muted",
                    ImageStatus.FAILED: "error",
                    ImageStatus.SIZE_LIMIT_FAILED: "error",
                    ImageStatus.SKIPPED_CORRUPT: "warning",
                    ImageStatus.SKIPPED_UNSUPPORTED: "warning",
                    ImageStatus.PENDING: "muted",
                }.get(r.status, "")
                out_s = human_bytes(r.output_bytes) if r.output_bytes else "—"
                saved_s = (
                    f"{human_bytes(r.saved_bytes)} ({r.saved_pct:.0f}%)" if r.saved_bytes else "—"
                )
                if r.status in fail_statuses:
                    note = friendly_failure_reason(r)
                else:
                    note = r.message or "—"
                detail.add_row(
                    rich_escape(r.source.name),
                    (
                        f"[{status_style}]{r.status.name}[/{status_style}]"
                        if status_style
                        else r.status.name
                    ),
                    human_bytes(r.original_bytes),
                    out_s,
                    saved_s,
                    str(r.quality_used) if r.quality_used is not None else "—",
                    f"{r.scale_factor:.2f}" if r.scale_factor != 1.0 else "1.00",
                    rich_escape(note[:60]),
                )
            self.console.print(detail)

            if failed_rows:
                self.rule(t("fail_section_title"))
                fail_table = Table(
                    title=t("fail_section_title"),
                    box=box.HEAVY_HEAD if hasattr(box, "HEAVY_HEAD") else box.ROUNDED,
                    show_lines=True,
                    border_style="bold red",
                )
                fail_table.add_column(t("col_file"), overflow="fold", style="bold")
                fail_table.add_column(t("col_status"), style="error")
                fail_table.add_column(t("fail_col_reason"), overflow="fold")
                for r in failed_rows:
                    fail_table.add_row(
                        rich_escape(r.source.name),
                        r.status.name,
                        rich_escape(friendly_failure_reason(r)),
                    )
                self.console.print(fail_table)
        else:
            print(f"  {t('res_strategy')}: {report.config.strategy.title}")
            print(f"  {t('res_format')}: {report.config.output_format.title}")
            print(f"  {t('res_elapsed')}: {human_duration(report.total_elapsed_sec)}")
            print(f"  {t('res_compressed')}: {report.compressed_count}")
            print(f"  {t('res_copied')}: {report.copied_count}")
            print(f"  {t('res_failed')}: {report.failed_count}")
            print(f"  {t('res_saved')}: {human_bytes(report.total_saved_bytes)}")
            print(f"  {t('res_output')}: {report.config.output_dir}")
            for r in report.results:
                if r.status in fail_statuses:
                    note = friendly_failure_reason(r)
                else:
                    note = r.message
                print(
                    f"  - {r.source.name}: {r.status.name} "
                    f"{human_bytes(r.original_bytes)} → {human_bytes(r.output_bytes)} | {note}"
                )
            if failed_rows:
                print()
                print(f"  === {t('fail_section_title')} ===")
                for r in failed_rows:
                    print(f"  ! {r.source.name}")
                    print(f"      {r.status.name}: {friendly_failure_reason(r)}")

    def progress(self) -> Any:
        if self.console is not None:
            return Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=self.console,
                transient=False,
            )
        return _NullProgress()


class _NullProgress:

    def __enter__(self) -> "_NullProgress":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def add_task(self, description: str, total: Optional[float] = None) -> int:
        print(f"  >> {description}")
        return 0

    def update(self, task_id: int, **kwargs: Any) -> None:
        advance = kwargs.get("advance")
        description = kwargs.get("description")
        if description:
            print(f"  .. {description}")
        elif advance:
            pass

    def advance(self, task_id: int, advance: float = 1) -> None:
        pass


class BatchProcessor:

    def __init__(
        self, config: RuntimeConfig, engine: StrategyEngine, logger: logging.Logger, cli: CLI
    ) -> None:
        self.config = config
        self.engine = engine
        self.logger = logger
        self.cli = cli

    def run(self, images: Sequence[ImageProbeResult]) -> List[ImageJobResult]:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        work_root = self.config.output_dir / TEMP_WORKDIR_NAME
        work_root.mkdir(parents=True, exist_ok=True)
        results: List[ImageJobResult] = []
        total = len(images)
        if total == 0:
            return results

        def job(probe: ImageProbeResult) -> ImageJobResult:
            out_name = self._output_name(probe.path)
            final_dest = self.config.output_dir / out_name
            work_dir = work_root / safe_filename(probe.path.stem)
            work_dir.mkdir(parents=True, exist_ok=True)
            try:
                return self.engine.process_image(probe, final_dest, work_dir)
            finally:
                if not self.config.keep_temp_on_failure:
                    shutil.rmtree(work_dir, ignore_errors=True)

        workers = max(1, min(self.config.max_workers, total))
        self.logger.info(
            "Processing %d image(s) with %d %s worker(s), strategy=%s",
            total,
            workers,
            self.config.worker_kind.value,
            self.config.strategy.name,
        )
        with self.cli.progress() as progress:
            task_id = progress.add_task(t("compressing"), total=total)
            executor: concurrent.futures.Executor
            if self.config.worker_kind == WorkerKind.PROCESS:
                executor = ThreadPoolExecutor(max_workers=workers)
            else:
                executor = ThreadPoolExecutor(max_workers=workers)
            try:
                future_map: Dict[Future[ImageJobResult], ImageProbeResult] = {}
                for probe in images:
                    fut = executor.submit(job, probe)
                    future_map[fut] = probe
                for fut in as_completed(future_map):
                    probe = future_map[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:
                        self.logger.error("Worker crashed on %s: %s", probe.path.name, exc)
                        res = ImageJobResult(
                            source=probe.path,
                            status=ImageStatus.FAILED,
                            original_bytes=probe.size_bytes,
                            original_dimensions=probe.dimensions,
                            message="worker_crash",
                            error_detail=str(exc),
                        )
                    results.append(res)
                    status_icon = {
                        ImageStatus.COMPRESSED: "OK",
                        ImageStatus.COPIED: "CP",
                        ImageStatus.SIZE_LIMIT_FAILED: "!!",
                        ImageStatus.FAILED: "XX",
                        ImageStatus.SKIPPED_CORRUPT: "??",
                        ImageStatus.SKIPPED_UNDER_LIMIT: "--",
                    }.get(res.status, "??")
                    progress.update(
                        task_id,
                        advance=1,
                        description=f"[{status_icon}] {probe.path.name[:40]} {human_bytes(res.original_bytes)}→{human_bytes(res.output_bytes)}",
                    )
            finally:
                executor.shutdown(wait=True, cancel_futures=False)
        order = {img.path: i for i, img in enumerate(images)}
        results.sort(key=lambda r: order.get(r.source, 10**9))
        if not self.config.keep_temp_on_failure:
            shutil.rmtree(work_root, ignore_errors=True)
        return results

    def _output_name(self, source: Path) -> str:
        stem = safe_filename(source.stem)
        codec = resolve_output_codec(source, self.config.output_format)
        return f"{stem}{codec.extension}"


class ReportWriter:

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def write_json(self, report: BatchReport, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                json.dump(report.to_dict(), fh, indent=2, ensure_ascii=False)
            self.logger.info("Wrote JSON report → %s", path)
        except OSError as exc:
            self.logger.error("Failed to write report %s: %s", path, exc)


class Application:

    def __init__(self) -> None:
        configure_stdio_utf8()
        self.cli = CLI()
        self.root = self._resolve_root()
        self.logger = setup_logging(level=DEFAULT_LOG_LEVEL)
        self._shutdown_requested = False
        self._install_signal_handlers()

    @staticmethod
    def _resolve_root() -> Path:
        try:
            script_dir = Path(__file__).resolve().parent
        except NameError:
            script_dir = Path.cwd().resolve()
        cwd = Path.cwd().resolve()

        def score(d: Path) -> int:
            s = 0
            try:
                for p in d.iterdir():
                    if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                        s += 2
                    if p.name.lower() in {n.lower() for n in FFMPEG_NAMES}:
                        s += 5
            except OSError:
                return -1
            return s

        if score(cwd) >= score(script_dir):
            return cwd
        return script_dir

    def _install_signal_handlers(self) -> None:

        def _handler(signum: int, frame: Any) -> None:
            self._shutdown_requested = True
            self.logger.warning("Interrupt received — finishing in-flight work…")

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass

    def run(self) -> int:
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        self.cli.select_language()
        self.cli.banner()
        try:
            locator = BinaryLocator(search_dirs=[self.root, Path(__file__).resolve().parent])
            ffmpeg, ffprobe = locator.resolve()
        except BinaryNotFoundError as exc:
            self.cli.print(f"[error]{exc}[/error]")
            return 2
        self.cli.show_environment(
            self.root, ffmpeg, ffprobe, locator.ffmpeg_version, locator.ffprobe_version
        )
        output_dir = self.root / DEFAULT_OUTPUT_DIRNAME
        log_path = output_dir / REPORT_LOG_NAME
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.cli.print(f"[error]{t('err_output_dir', exc=exc)}[/error]")
            return 3
        self.logger = setup_logging(
            level=DEFAULT_LOG_LEVEL,
            log_file=log_path,
            console=self.cli.console if RICH_AVAILABLE else None,
        )
        self.logger.info("=== %s v%s starting ===", SCRIPT_NAME, SCRIPT_VERSION)
        self.logger.info("Root: %s", self.root)
        self.logger.info("ffmpeg: %s", ffmpeg)
        self.logger.info("ffprobe: %s", ffprobe)
        self.logger.info("Language: %s", ACTIVE_LANG)
        runner = SubprocessRunner(self.logger)
        prober = ImageProber(ffprobe, runner, self.logger)
        scanner = PreflightScanner(
            self.root, prober, self.logger, include_convertibles=True, output_dir=output_dir
        )
        self.cli.print(f"\n[info]{t('preflight_running')}[/info]")
        try:
            summary = scanner.scan(max_workers=DEFAULT_MAX_WORKERS)
        except WorkspaceError as exc:
            self.cli.print(f"[error]{exc}[/error]")
            return 4
        if summary.total_files_scanned == 0:
            self.cli.print(
                f"[warning]{t('no_images', exts=', '.join(sorted(SUPPORTED_EXTENSIONS)))}[/warning]"
            )
            return 0
        self.cli.show_preflight(summary)
        strategy = self.cli.select_strategy(summary)
        self.logger.info("User selected strategy: %s", strategy.name)
        output_format = self.cli.select_output_format()
        self.logger.info("User selected output format: %s", output_format.name)
        if not self.cli.confirm_start(strategy, summary.over_limit_count):
            self.cli.print(f"[warning]{t('aborted_user')}[/warning]")
            return 0
        max_workers = DEFAULT_MAX_WORKERS
        if RICH_AVAILABLE and self.cli.console is not None:
            try:
                max_workers = IntPrompt.ask(
                    t("parallel_workers"), default=DEFAULT_MAX_WORKERS, console=self.cli.console
                )
                max_workers = clamp(int(max_workers), 1, 32)
            except Exception:
                max_workers = DEFAULT_MAX_WORKERS
        else:
            raw = input(f"{t('parallel_workers')} [{DEFAULT_MAX_WORKERS}]: ").strip()
            if raw.isdigit():
                max_workers = clamp(int(raw), 1, 32)
        config = RuntimeConfig(
            root_dir=self.root,
            output_dir=output_dir,
            max_bytes=DEFAULT_MAX_BYTES,
            effective_target_bytes=EFFECTIVE_TARGET_BYTES,
            strategy=strategy,
            max_workers=max_workers,
            worker_kind=WorkerKind.THREAD,
            allow_downscale=False,
            copy_under_limit=True,
            overwrite_output=True,
            strip_metadata=True,
            progressive_jpeg=True,
            include_convertibles=True,
            output_format=output_format,
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            log_file=log_path,
        )
        ffmpeg_enc = FFmpegEncoder(ffmpeg, runner, self.logger)
        pillow_enc: Optional[PillowEncoder] = None
        if PILLOW_AVAILABLE:
            try:
                pillow_enc = PillowEncoder(self.logger)
            except CompressorError:
                pillow_enc = None
        dual = DualEncoder(ffmpeg_enc, pillow_enc, self.logger)
        engine = StrategyEngine(dual, self.logger, config)
        processor = BatchProcessor(config, engine, self.logger, self.cli)
        self.cli.rule(t("rule_processing"))
        results = processor.run(summary.images)
        finished = datetime.now(timezone.utc)
        elapsed = time.perf_counter() - t0
        report = BatchReport(
            config=config,
            preflight=summary,
            results=results,
            started_at=started,
            finished_at=finished,
            total_elapsed_sec=elapsed,
        )
        writer = ReportWriter(self.logger)
        writer.write_json(report, output_dir / REPORT_JSON_NAME)
        self.cli.show_batch_report(report)
        if report.failed_count == 0:
            self.cli.print(f"\n[success]{t('all_success')}[/success]")
            return 0
        size_fails = sum((1 for r in results if r.status == ImageStatus.SIZE_LIMIT_FAILED))
        hard_fails = sum((1 for r in results if r.status == ImageStatus.FAILED))
        if hard_fails:
            self.cli.print(
                f"\n[error]{t('done_hard_fail', hard=hard_fails, size=size_fails)}[/error]"
            )
            return 1
        self.cli.print(f"\n[warning]{t('done_size_fail', n=size_fails)}[/warning]")
        return 5


def _pause_if_windows_double_click() -> None:
    if sys.platform != "win32":
        return
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            print()
            input(t("press_enter"))
    except EOFError:
        pass


def main() -> int:
    try:
        app = Application()
        return app.run()
    except UserAbortError:
        print(t("aborted"))
        return 0
    except (KeyboardInterrupt, EOFError):
        print(f"\n{t('stopped_by_user')}")
        return 130
    except BinaryNotFoundError as exc:
        print(t("error_prefix", exc=exc), file=sys.stderr)
        return 2
    except CompressorError as exc:
        print(t("error_prefix", exc=exc), file=sys.stderr)
        traceback.print_exc()
        return 1
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        if os.environ.get("JPEG_COMPRESSOR_NO_PAUSE", "").strip() not in {"1", "true", "yes"}:
            _pause_if_windows_double_click()


if __name__ == "__main__":
    sys.exit(main())
