from __future__ import annotations

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
from typing import Any, Dict, Final, List, Mapping, Optional, Sequence, Tuple, Union

if sys.version_info < (3, 10):
    print(
        "ERROR: JPEG Batch Compressor requires Python 3.10 or newer.",
        file=sys.stderr,
    )
    raise SystemExit(2)

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

    def rich_escape(text: str) -> str:
        return text

try:
    from PIL import Image, ImageOps, __version__ as PILLOW_VERSION

    _pillow_version_parts = tuple(
        int(part) for part in re.findall(r"\d+", PILLOW_VERSION)[:3]
    )
    PILLOW_AVAILABLE: bool = _pillow_version_parts >= (10, 0)
    PILLOW_IMPORT_ERROR: Optional[str] = (
        None
        if PILLOW_AVAILABLE
        else f"Pillow 10.0+ is required; found {PILLOW_VERSION}"
    )
    if not PILLOW_AVAILABLE:
        Image = None
        ImageOps = None
except ImportError:
    PILLOW_AVAILABLE = False
    PILLOW_VERSION = "not installed"
    PILLOW_IMPORT_ERROR = "Pillow 10.0+ is not installed"
    Image = None
    ImageOps = None

SCRIPT_VERSION: Final[str] = "1.3.0"
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
FFMPEG_NAMES: Final[Tuple[str, ...]] = (
    ("ffmpeg.exe", "ffmpeg") if platform.system() == "Windows" else ("ffmpeg",)
)
FFPROBE_NAMES: Final[Tuple[str, ...]] = (
    ("ffprobe.exe", "ffprobe") if platform.system() == "Windows" else ("ffprobe",)
)
BINARY_FILENAMES_LOWER: Final[frozenset[str]] = frozenset(
    name.casefold() for name in (*FFMPEG_NAMES, *FFPROBE_NAMES)
)
DEFAULT_SUBPROCESS_TIMEOUT_SEC: Final[float] = 180.0
PROBE_TIMEOUT_SEC: Final[float] = 45.0
ENCODE_TIMEOUT_SEC: Final[float] = 240.0
FFMPEG_Q_BEST: Final[int] = 1
FFMPEG_Q_WORST: Final[int] = 28
QUALITY_LOSSLESS_PROXY: Final[int] = 100
QUALITY_BINARY_MIN: Final[int] = 35
QUALITY_BINARY_MAX: Final[int] = 97
BINARY_SEARCH_MAX_ITERS: Final[int] = 12
PNG_PALETTE_QUALITIES: Final[Tuple[int, ...]] = (90, 80, 70, 60, 50, 40, 30)
DOWNSCALE_FACTORS: Final[Tuple[float, ...]] = (0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6)
MIN_OUTPUT_DIMENSION: Final[int] = 640
DEFAULT_MAX_WORKERS: Final[int] = max(1, min(8, os.cpu_count() or 4))
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
        "banner_subtitle": "Zero-config · Local processing · Every published output is verified <{target_mb} MB ({target_bytes} bytes)",
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
        "res_copied": "Copied / sanitized",
        "res_failed": "Failed",
        "res_interrupted": "Run interrupted after completed work was preserved",
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
        "fail_size_limit": "Could not produce an output below the strict size limit with the selected strategy.",
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
        "strat_a_title": "Near-Lossless / Metadata-First",
        "strat_b_title": "High-Quality Lossy (90–95%)",
        "strat_c_title": "Binary-Search Target (<{target_mb} MB)",
        "strat_d_title": "Aggressive Adaptive (max retention)",
        "strat_e_title": "Copy Already-Compliant Only",
        "strat_a_desc": "Try the codec's highest-fidelity setting first, then a mild lossy fallback when needed. For PNG, this means optimized lossless followed by a 256-color palette attempt suited to line art and low-color images; photographs may show visible banding. Resolution is preserved.",
        "strat_b_desc": "Start oversize JPEG/WEBP images at quality 90–95, then search lower qualities if needed. PNG uses optimized lossless plus an ordered palette ladder. Resolution is preserved.",
        "strat_c_desc": "Search JPEG/WEBP quality per image for the highest-quality result below the preferred target. PNG uses codec-aware lossless and palette variants instead of a nominal quality search.",
        "strat_d_desc": "Full codec-aware pipeline: high-fidelity attempt, quality or palette ladder, then target search where applicable. Resolution is preserved because interactive downscaling is disabled.",
        "strat_e_desc": "Only copy files already under {target_mb} MB into the output folder. Oversize images are skipped (listed in the report). Useful for dry-run style triage.",
        "msg_already_copied": "already under limit — copied or sanitized",
        "msg_already_not_copied": "already under limit (not copied)",
        "msg_copy_only_skip": "over limit; copy-only strategy skips compression",
        "msg_dry_run": "dry-run: no output written",
        "msg_compressed": "compressed to {size} (q={q}, scale={scale:.2f})",
        "msg_size_fail": "could not get under {limit} (best={best})",
        "msg_unable": "unable to reach <{limit}; best effort was {best} at q={q} scale={scale:.2f}",
    },
    "vi": {
        "lang_prompt": "Select language / Chọn ngôn ngữ",
        "lang_en": "English",
        "lang_vi": "Tiếng Việt",
        "banner_subtitle": "Zero-config · Xử lý local · Mọi output được xuất đều được kiểm tra <{target_mb} MB ({target_bytes} bytes)",
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
        "res_copied": "Đã copy / làm sạch metadata",
        "res_failed": "Thất bại",
        "res_interrupted": "Run đã bị ngắt; các kết quả hoàn tất đã được giữ lại",
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
        "fail_size_limit": "Không tạo được output dưới giới hạn dung lượng nghiêm ngặt bằng strategy đã chọn.",
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
        "strat_a_title": "Near-Lossless / Metadata-First",
        "strat_b_title": "High-Quality Lossy (90–95%)",
        "strat_c_title": "Binary-Search Target (<{target_mb} MB)",
        "strat_d_title": "Aggressive Adaptive (giữ chất lượng tối đa)",
        "strat_e_title": "Chỉ copy file đã đạt chuẩn",
        "strat_a_desc": "Thử mức fidelity cao nhất của codec trước, rồi fallback lossy nhẹ khi cần. Với PNG: optimized lossless, sau đó thử palette 256 màu phù hợp line art và ảnh ít màu; ảnh chụp có thể bị banding rõ. Giữ nguyên resolution.",
        "strat_b_desc": "Bắt đầu JPEG/WEBP vượt size ở quality 90–95, rồi tìm quality thấp hơn nếu cần. PNG dùng optimized lossless và palette ladder có thứ tự. Giữ nguyên resolution.",
        "strat_c_desc": "Tìm quality JPEG/WEBP cao nhất dưới preferred target. PNG dùng lossless và palette variant đúng theo codec thay vì search quality danh nghĩa.",
        "strat_d_desc": "Pipeline theo codec: thử fidelity cao, quality hoặc palette ladder, rồi target search khi phù hợp. Giữ nguyên resolution vì giao diện interactive tắt downscale.",
        "strat_e_desc": "Chỉ copy các file đã dưới {target_mb} MB vào output. Image vượt size bị skip (ghi trong report). Hữu ích khi triage kiểu dry-run.",
        "msg_already_copied": "đã dưới limit — đã copy hoặc làm sạch metadata",
        "msg_already_not_copied": "đã dưới limit (không copy)",
        "msg_copy_only_skip": "vượt limit; strategy copy-only bỏ qua nén",
        "msg_dry_run": "dry-run: không ghi output",
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


class UserAbortError(CompressorError):
    pass


class BatchInterruptedError(UserAbortError):

    def __init__(
        self,
        message: str,
        *,
        results: Sequence["ImageJobResult"],
    ) -> None:
        super().__init__(message)
        self.results = (
            results if isinstance(results, list) else list(results)
        )


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
    DRY_RUN = auto()
    COPIED = auto()
    SANITIZED = auto()
    COMPRESSED = auto()
    FAILED = auto()
    SIZE_LIMIT_FAILED = auto()


class EncodeBackend(Enum):
    NONE = "none"
    COPY = "copy"
    FFMPEG = "ffmpeg"
    PILLOW = "pillow"


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
    suffix = path.suffix.lower()
    native_codec: Optional[ImageCodec]
    if suffix in JPEG_EXTENSIONS:
        native_codec = ImageCodec.JPG
    elif suffix == ".png":
        native_codec = ImageCodec.PNG
    elif suffix == ".webp":
        native_codec = ImageCodec.WEBP
    else:
        native_codec = None
    return native_codec == target


def quality_to_png_level(quality: int) -> int:
    return 9


def quality_to_png_colors(quality: int) -> Optional[int]:
    q = clamp(int(quality), 1, 100)
    if q >= 95:
        return None
    if q >= 85:
        return 256
    if q >= 75:
        return 192
    if q >= 65:
        return 128
    if q >= 55:
        return 96
    if q >= 45:
        return 64
    if q >= 35:
        return 48
    return 32


def quality_to_webp_params(quality: int) -> Tuple[int, bool]:
    q = clamp(int(quality), 1, 100)
    if q >= 98:
        return 100, True
    return q, False


@dataclass(frozen=True, slots=True)
class SizePolicy:
    strict_max_bytes: int = DEFAULT_MAX_BYTES
    preferred_target_bytes: int = EFFECTIVE_TARGET_BYTES

    def __post_init__(self) -> None:
        if self.strict_max_bytes <= 1:
            raise ValueError("strict_max_bytes must be greater than one")
        if not 0 < self.preferred_target_bytes < self.strict_max_bytes:
            raise ValueError("preferred_target_bytes must be below strict_max_bytes")

    def is_acceptable(self, size_bytes: int) -> bool:
        return 0 < size_bytes < self.strict_max_bytes

    def is_preferred(self, size_bytes: int) -> bool:
        return 0 < size_bytes < self.preferred_target_bytes

    def reduction_needed_pct(self, size_bytes: int) -> float:
        if size_bytes <= 0 or self.is_acceptable(size_bytes):
            return 0.0
        return max(0.0, (1.0 - self.preferred_target_bytes / size_bytes) * 100.0)


DEFAULT_SIZE_POLICY: Final[SizePolicy] = SizePolicy()


class CancellationToken:

    def __init__(self) -> None:
        self._event = threading.Event()
        self._publication_lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        with self._publication_lock:
            self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise UserAbortError("operation cancelled")

    def run_if_active(self, operation: Any) -> Any:
        with self._publication_lock:
            self.raise_if_cancelled()
            return operation()


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
        if factor >= 1.0 or self.width <= 0 or self.height <= 0:
            return self
        source_min = min(self.width, self.height)
        minimum_factor = min(1.0, MIN_OUTPUT_DIMENSION / float(source_min))
        effective_factor = max(0.0, factor, minimum_factor)
        if self.width <= self.height:
            w = self._even_dimension(self.width * effective_factor, self.width)
            ratio = w / self.width
            h = self._even_dimension(self.height * ratio, self.height)
        else:
            h = self._even_dimension(self.height * effective_factor, self.height)
            ratio = h / self.height
            w = self._even_dimension(self.width * ratio, self.width)
        return ImageDimensions(width=w, height=h)

    @staticmethod
    def _even_dimension(value: float, maximum: int) -> int:
        rounded = max(2, int(round(value / 2.0)) * 2)
        max_even = maximum if maximum % 2 == 0 else maximum - 1
        if max_even < 2:
            return max(1, maximum)
        return min(max_even, rounded)

    def scale_from(self, source: "ImageDimensions") -> float:
        if source.width <= 0 or source.height <= 0:
            return 1.0
        return min(self.width / source.width, self.height / source.height)

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
    has_icc_profile: bool = False
    has_alpha: bool = False
    exif_orientation: int = 1
    probe_error: Optional[str] = None
    format_name: Optional[str] = None
    duration: Optional[float] = None

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    def is_over_limit(self, policy: SizePolicy = DEFAULT_SIZE_POLICY) -> bool:
        return not policy.is_acceptable(self.size_bytes)

    def reduction_needed_pct(self, policy: SizePolicy = DEFAULT_SIZE_POLICY) -> float:
        return policy.reduction_needed_pct(self.size_bytes)

    def compression_potential_for(
        self, policy: SizePolicy = DEFAULT_SIZE_POLICY
    ) -> str:
        if not self.is_readable:
            return "unreadable"
        if not self.is_over_limit(policy):
            return "already_ok"
        needed = self.reduction_needed_pct(policy)
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

    @property
    def compression_potential(self) -> str:
        return self.compression_potential_for()


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
    variant: Optional[str] = None

    def is_acceptable(self, policy: SizePolicy = DEFAULT_SIZE_POLICY) -> bool:
        return self.success and policy.is_acceptable(self.output_bytes)

    def is_preferred(self, policy: SizePolicy = DEFAULT_SIZE_POLICY) -> bool:
        return self.success and policy.is_preferred(self.output_bytes)


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
    variant: Optional[str] = None
    work_dir: Optional[Path] = None

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


@dataclass(slots=True)
class PreflightSummary:
    root: Path
    images: List[ImageProbeResult]
    size_policy: SizePolicy
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
    size_policy: SizePolicy = field(default_factory=SizePolicy)
    strategy: CompressionStrategy = CompressionStrategy.AGGRESSIVE_ADAPTIVE
    max_workers: int = DEFAULT_MAX_WORKERS
    allow_downscale: bool = False
    copy_under_limit: bool = True
    overwrite_output: bool = False
    keep_temp_on_failure: bool = False
    dry_run: bool = False
    include_convertibles: bool = True
    progressive_jpeg: bool = True
    strip_metadata: bool = True
    preserve_icc_profile: bool = True
    output_format: OutputFormatChoice = OutputFormatChoice.JPG
    cancellation: CancellationToken = field(default_factory=CancellationToken)

    @property
    def max_bytes(self) -> int:
        return self.size_policy.strict_max_bytes

    @property
    def effective_target_bytes(self) -> int:
        return self.size_policy.preferred_target_bytes


@dataclass(slots=True)
class BatchReport:
    config: RuntimeConfig
    preflight: PreflightSummary
    results: List[ImageJobResult]
    started_at: datetime
    finished_at: datetime
    total_elapsed_sec: float
    interrupted: bool = False

    @property
    def success_count(self) -> int:
        return sum(
            (
                1
                for r in self.results
                if r.status
                in (
                    ImageStatus.COMPRESSED,
                    ImageStatus.COPIED,
                    ImageStatus.SANITIZED,
                    ImageStatus.SKIPPED_UNDER_LIMIT,
                    ImageStatus.DRY_RUN,
                )
                and (
                    r.output_path is not None
                    or r.status
                    in (
                        ImageStatus.SKIPPED_UNDER_LIMIT,
                        ImageStatus.DRY_RUN,
                    )
                )
            )
        )

    @property
    def compressed_count(self) -> int:
        return sum((1 for r in self.results if r.status == ImageStatus.COMPRESSED))

    @property
    def copied_count(self) -> int:
        return sum(
            (
                1
                for r in self.results
                if r.status in (ImageStatus.COPIED, ImageStatus.SANITIZED)
            )
        )

    @property
    def failed_count(self) -> int:
        return sum(
            (
                1
                for r in self.results
                if r.status
                in (
                    ImageStatus.FAILED,
                    ImageStatus.SIZE_LIMIT_FAILED,
                    ImageStatus.SKIPPED_CORRUPT,
                    ImageStatus.SKIPPED_UNSUPPORTED,
                )
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
            "interrupted": self.interrupted,
            "strategy": self.config.strategy.name,
            "output_format": self.config.output_format.name,
            "max_bytes": self.config.max_bytes,
            "preferred_target_bytes": self.config.effective_target_bytes,
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
                    "work_dir": str(r.work_dir) if r.work_dir else None,
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
                    "variant": r.variant,
                    "message": r.message,
                    "elapsed_sec": round(r.elapsed_sec, 3),
                    "error": r.error_detail,
                    "attempts": len(r.attempts),
                }
                for r in self.results
            ],
        }


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


WINDOWS_RESERVED_BASENAMES: Final[frozenset[str]] = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)


def safe_filename(name: str, *, max_length: int = 180) -> str:
    if max_length < 1:
        raise ValueError("max_length must be positive")
    cleaned = re.sub('[<>:"/\\\\|?*\\x00-\\x1f]', "_", name).strip(" .")
    cleaned = cleaned or "unnamed"
    if cleaned.split(".", 1)[0].upper() in WINDOWS_RESERVED_BASENAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_length:
        suffix = Path(cleaned).suffix
        if 0 < len(suffix) < max_length:
            stem_limit = max_length - len(suffix)
            stem = cleaned[: -len(suffix)][:stem_limit].rstrip(" .")
            cleaned = f"{stem or 'unnamed'}{suffix}"
        else:
            cleaned = cleaned[:max_length].rstrip(" .") or "unnamed"
    return cleaned


def is_within_directory(path: Path, directory: Path) -> bool:
    try:
        path = path.resolve()
        directory = directory.resolve()
        return directory == path or directory in path.parents
    except OSError:
        return False


def atomic_replace(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(src), str(dest))


def atomic_publish(src: Path, dest: Path, *, overwrite: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite:
        fd: Optional[int] = None
        try:
            fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        finally:
            if fd is not None:
                os.close(fd)
        try:
            os.replace(str(src), str(dest))
        except Exception:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return
    atomic_replace(src, dest)


def fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    fd: Optional[int] = None
    try:
        fd = os.open(directory, os.O_RDONLY)
        os.fsync(fd)
    except OSError:
        pass
    finally:
        if fd is not None:
            os.close(fd)


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def unique_temp_path(directory: Path, suffix: str = ".jpg", *, label: str = "tmp") -> Path:
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    return directory / f"{safe_filename(label, max_length=40)}_{uuid.uuid4().hex}{suffix}"


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
            markup=False,
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
        proc: Optional[subprocess.Popen[str]] = None
        try:
            proc = subprocess.Popen(
                [str(binary), "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            stdout, stderr = proc.communicate(timeout=15)
            first = (stdout or stderr or "").splitlines()
            return first[0].strip() if first else "unknown"
        except subprocess.TimeoutExpired:
            if proc is not None:
                try:
                    proc.kill()
                    proc.communicate()
                except (OSError, subprocess.SubprocessError):
                    pass
            return "unknown"
        except KeyboardInterrupt:
            if proc is not None:
                try:
                    proc.kill()
                    proc.communicate()
                except (OSError, subprocess.SubprocessError):
                    pass
            raise
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
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled


class SubprocessRunner:

    def __init__(
        self,
        logger: logging.Logger,
        *,
        default_timeout: float = DEFAULT_SUBPROCESS_TIMEOUT_SEC,
        cancellation: Optional[CancellationToken] = None,
    ) -> None:
        self.logger = logger
        self.default_timeout = default_timeout
        self.cancellation = cancellation or CancellationToken()
        self._lock = threading.Lock()
        self._active: set[subprocess.Popen[str]] = set()
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
        self.cancellation.raise_if_cancelled()
        self.logger.debug("[%s] exec: %s", label, " ".join(argv_list))
        t0 = time.perf_counter()
        popen_kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True
        with self._lock:
            self.cancellation.raise_if_cancelled()
            try:
                proc = subprocess.Popen(
                    argv_list,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(cwd) if cwd else None,
                    env=merged_env,
                    **popen_kwargs,
                )
            except FileNotFoundError as exc:
                raise BinaryNotFoundError(
                    f"Executable not found: {argv_list[0]}",
                    cause=exc,
                ) from exc
            except OSError as exc:
                raise CompressorError(
                    f"Failed to spawn process: {exc}",
                    cause=exc,
                ) from exc
            self._active.add(proc)
            self.call_count += 1
        timed_out = False
        cancelled = False
        stdout = ""
        stderr = ""
        deadline = t0 + max(0.0, timeout)
        try:
            while True:
                try:
                    stdout, stderr = proc.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if self.cancellation.cancelled:
                        cancelled = True
                        self._stop_process(proc)
                        stdout, stderr = proc.communicate()
                        break
                    if time.perf_counter() >= deadline:
                        timed_out = True
                        self._stop_process(proc)
                        stdout, stderr = proc.communicate()
                        break
            if self.cancellation.cancelled:
                cancelled = True
        finally:
            with self._lock:
                self._active.discard(proc)

        elapsed = time.perf_counter() - t0
        if timed_out:
            stderr = f"Timed out after {timeout}s\n{stderr}".strip()
            self.logger.error("[%s] TIMEOUT after %.1fs: %s", label, timeout, argv_list[0])
        elif cancelled:
            stderr = f"Cancelled\n{stderr}".strip()
        result = CommandResult(
            argv=argv_list,
            returncode=proc.returncode if proc.returncode is not None else -9,
            stdout=stdout or "",
            stderr=stderr or "",
            elapsed_sec=elapsed,
            timed_out=timed_out,
            cancelled=cancelled,
        )
        if not result.ok:
            self.logger.debug(
                "[%s] failed rc=%s in %.2fs stderr=%s",
                label,
                result.returncode,
                elapsed,
                result.stderr[:500],
            )
            if check:
                raise EncodeError(
                    f"{label} failed (rc={result.returncode}): {(result.stderr or result.stdout)[:800]}"
                )
        else:
            self.logger.debug("[%s] ok in %.2fs", label, elapsed)
        return result

    @staticmethod
    def _stop_process(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ValueError):
            try:
                proc.terminate()
            except OSError:
                return
        try:
            proc.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5.0,
                )
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                return
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass

    def cancel_all(self) -> None:
        self.cancellation.cancel()
        with self._lock:
            active = list(self._active)
        for proc in active:
            try:
                self._stop_process(proc)
            except OSError:
                pass


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
            result = self._probe_ffprobe(path, size)
            if PILLOW_AVAILABLE:
                try:
                    pillow_result = self._probe_pillow(path, size)
                    result.dimensions = pillow_result.dimensions
                    result.has_metadata_hint = (
                        result.has_metadata_hint or pillow_result.has_metadata_hint
                    )
                    result.has_icc_profile = pillow_result.has_icc_profile
                    result.has_alpha = (
                        result.has_alpha or pillow_result.has_alpha
                    )
                    result.exif_orientation = pillow_result.exif_orientation
                    result.pixel_format = (
                        self._prefer_pillow_pixel_format(
                            result.pixel_format,
                            pillow_result.pixel_format,
                        )
                    )
                    if (
                        result.bit_depth is None
                        and pillow_result.bit_depth is not None
                    ):
                        result.bit_depth = pillow_result.bit_depth
                except UserAbortError:
                    raise
                except Exception as exc:
                    self.logger.debug("Pillow metadata probe failed for %s: %s", path.name, exc)
            return result
        except UserAbortError:
            raise
        except (ProbeError, CompressorError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self.runner.cancellation.raise_if_cancelled()
            self.logger.debug("ffprobe failed for %s: %s — trying Pillow", path.name, exc)
            if PILLOW_AVAILABLE:
                try:
                    return self._probe_pillow(path, size)
                except UserAbortError:
                    raise
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
        if bit_depth is None:
            bit_depth = self._pixel_format_bit_depth(pix_fmt)
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
            has_alpha=self._pixel_format_has_alpha(pix_fmt),
            format_name=fmt.get("format_name"),
            duration=float(fmt["duration"]) if fmt.get("duration") else None,
        )

    def _probe_pillow(self, path: Path, size: int) -> ImageProbeResult:
        assert PILLOW_AVAILABLE and Image is not None
        try:
            self.runner.cancellation.raise_if_cancelled()
            with Image.open(path) as img:
                img.verify()
            self.runner.cancellation.raise_if_cancelled()
            with Image.open(path) as img:
                fmt = (img.format or "").upper()
                mode = img.mode
                info = dict(img.info or {})
                try:
                    orientation = int(img.getexif().get(274, 1) or 1)
                except (AttributeError, TypeError, ValueError):
                    orientation = 1
                oriented = ImageOps.exif_transpose(img) if ImageOps is not None else img
                width, height = oriented.size
            self.runner.cancellation.raise_if_cancelled()
        except UserAbortError:
            raise
        except Exception as exc:
            raise ProbeError(
                f"Pillow cannot open {path.name}: {exc}",
                cause=exc,
            ) from exc
        return ImageProbeResult(
            path=path,
            size_bytes=size,
            dimensions=ImageDimensions(width, height),
            codec_name=fmt.lower() if fmt else None,
            pixel_format=mode,
            color_space=None,
            bit_depth=self._pillow_bit_depth(mode),
            is_readable=True,
            is_jpeg=fmt in {"JPEG", "MPO"} or path.suffix.lower() in JPEG_EXTENSIONS,
            has_metadata_hint=bool(info.get("exif") or info.get("icc_profile")),
            has_icc_profile=bool(info.get("icc_profile")),
            has_alpha=(
                "A" in mode
                or (
                    mode == "P"
                    and "transparency" in info
                )
            ),
            exif_orientation=orientation,
            format_name=fmt.lower() if fmt else None,
        )

    @staticmethod
    def _pillow_bit_depth(mode: str) -> Optional[int]:
        normalized = (mode or "").upper()
        if normalized.startswith("I;16"):
            return 16
        if normalized in {"I", "F"}:
            return 32
        if normalized:
            return 8
        return None

    @staticmethod
    def _pixel_format_bit_depth(
        pixel_format: Optional[str],
    ) -> Optional[int]:
        value = (pixel_format or "").casefold()
        if not value:
            return None
        packed_match = re.fullmatch(
            r"(rgb|bgr|rgba|bgra|argb|abgr)(24|32|48|64)(?:le|be)?",
            value,
        )
        if packed_match:
            packed_bits = int(packed_match.group(2))
            channels = 4 if len(packed_match.group(1)) == 4 else 3
            if packed_bits % channels == 0:
                return packed_bits // channels
        planar_match = re.search(
            r"(?:pf?|grayf?|ya)(9|10|12|14|16|32)(?:le|be)?$",
            value,
        )
        if planar_match:
            return int(planar_match.group(1))
        return 8

    @staticmethod
    def _pixel_format_has_alpha(
        pixel_format: Optional[str],
    ) -> bool:
        value = (pixel_format or "").casefold()
        return (
            value == "pal8"
            or value.startswith(
                ("rgba", "bgra", "argb", "abgr", "ayuv", "ya")
            )
            or "yuva" in value
            or "vuya" in value
            or "gbrap" in value
        )

    @staticmethod
    def _prefer_pillow_pixel_format(
        ffprobe_pixel_format: Optional[str],
        pillow_pixel_format: Optional[str],
    ) -> Optional[str]:
        ffmpeg_value = (ffprobe_pixel_format or "").casefold()
        pillow_value = (pillow_pixel_format or "").upper()
        if any(
            marker in ffmpeg_value
            for marker in ("420", "422", "444", "rgb", "bgr", "gbr")
        ):
            return ffprobe_pixel_format
        return pillow_pixel_format or ffprobe_pixel_format


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
        preserve_icc_profile: bool = True,
        progressive: bool = True,
        source_pixel_format: Optional[str] = None,
        source_orientation: int = 1,
        source_has_icc_profile: Optional[bool] = None,
        source_bit_depth: Optional[int] = None,
        source_has_alpha: bool = False,
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
        preserve_icc_profile: bool = True,
        progressive: bool = True,
        source_pixel_format: Optional[str] = None,
        source_orientation: int = 1,
        source_has_icc_profile: Optional[bool] = None,
        source_bit_depth: Optional[int] = None,
        source_has_alpha: bool = False,
    ) -> CompressionAttempt:
        del preserve_icc_profile, progressive, source_orientation
        del source_has_icc_profile, source_bit_depth, source_has_alpha
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.runner.cancellation.raise_if_cancelled()
        t0 = time.perf_counter()
        q_v = quality_to_ffmpeg_q(quality)
        vf_parts: List[str] = []
        out_dims = target_dimensions
        if target_dimensions is not None:
            vf_parts.append(
                f"scale={target_dimensions.width}:{target_dimensions.height}:flags=lanczos"
            )
        elif scale_factor < 0.999:
            vf_parts.append(
                f"scale=trunc(iw*{scale_factor}/2)*2:trunc(ih*{scale_factor}/2)*2:flags=lanczos"
            )

        base = self._base_args(source, vf_parts=vf_parts, strip_metadata=strip_metadata)
        codec_args = self._codec_args(
            codec,
            quality=quality,
            q_v=q_v,
            huffman=True,
            source_pixel_format=source_pixel_format,
        )
        argv = [*base, *codec_args, str(destination)]
        result = self.runner.run(argv, timeout=self.timeout, label=f"ffmpeg-enc:{source.name}")
        if result.cancelled:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise UserAbortError("operation cancelled")
        if (
            codec == ImageCodec.JPG
            and not result.ok
            and self._is_huffman_compatibility_error(result.stderr)
        ):
            retry_argv = [
                *base,
                *self._codec_args(
                    codec,
                    quality=quality,
                    q_v=q_v,
                    huffman=False,
                    source_pixel_format=source_pixel_format,
                ),
                str(destination),
            ]
            result = self.runner.run(
                retry_argv, timeout=self.timeout, label=f"ffmpeg-enc-retry:{source.name}"
            )
            if result.cancelled:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
                raise UserAbortError("operation cancelled")
        elapsed = time.perf_counter() - t0
        out_size = file_size(destination) if result.ok and destination.is_file() else -1
        success = result.ok and out_size > 0
        if not success:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        return CompressionAttempt(
            backend=EncodeBackend.FFMPEG,
            quality=quality,
            ffmpeg_q=q_v if codec == ImageCodec.JPG else None,
            scale_factor=scale_factor,
            output_bytes=max(0, out_size),
            elapsed_sec=elapsed,
            success=success,
            error=None if success else (result.stderr or result.stdout or "encode_failed")[:600],
            dimensions=out_dims,
            output_path=destination if success else None,
        )

    def _base_args(
        self, source: Path, *, vf_parts: Sequence[str], strip_metadata: bool
    ) -> List[str]:
        argv = [
            str(self.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-autorotate",
            "-i",
            str(source),
            "-frames:v",
            "1",
        ]
        if vf_parts:
            argv.extend(["-vf", ",".join(vf_parts)])
        if strip_metadata:
            argv.extend(["-map_metadata", "-1"])
        return argv

    @staticmethod
    def _codec_args(
        codec: ImageCodec,
        *,
        quality: int,
        q_v: int,
        huffman: bool,
        source_pixel_format: Optional[str] = None,
    ) -> List[str]:
        if codec == ImageCodec.JPG:
            pixel_format = FFmpegEncoder._jpeg_pixel_format(
                source_pixel_format
            )
            argv = [
                "-c:v",
                "mjpeg",
                "-q:v",
                str(q_v),
                "-pix_fmt",
                pixel_format,
                "-color_range",
                "pc",
            ]
            if huffman:
                argv.extend(["-huffman", "optimal"])
            return argv
        if codec == ImageCodec.PNG:
            return ["-c:v", "png", "-compression_level", "9"]
        webp_q, lossless = quality_to_webp_params(quality)
        return [
            "-c:v",
            "libwebp",
            "-quality",
            str(webp_q),
            "-lossless",
            "1" if lossless else "0",
            "-compression_level",
            "6",
        ]

    @staticmethod
    def _jpeg_pixel_format(source_pixel_format: Optional[str]) -> str:
        pixel_format = (source_pixel_format or "").casefold()
        high_chroma_markers = (
            "444",
            "rgb",
            "bgr",
            "gbr",
            "cmyk",
        )
        if any(marker in pixel_format for marker in high_chroma_markers):
            return "yuvj444p"
        if "422" in pixel_format:
            return "yuvj422p"
        return "yuvj420p"

    @staticmethod
    def _is_huffman_compatibility_error(stderr: str) -> bool:
        text = (stderr or "").lower()
        return "huffman" in text and any(
            marker in text for marker in ("option", "not found", "unrecognized", "invalid")
        )


class PillowEncoder(Encoder):
    name = "pillow"

    def __init__(
        self,
        logger: logging.Logger,
        *,
        cancellation: Optional[CancellationToken] = None,
    ) -> None:
        if not PILLOW_AVAILABLE:
            raise CompressorError("Pillow is not installed")
        self.logger = logger
        self.cancellation = cancellation or CancellationToken()

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
        preserve_icc_profile: bool = True,
        progressive: bool = True,
        source_pixel_format: Optional[str] = None,
        source_orientation: int = 1,
        source_has_icc_profile: Optional[bool] = None,
        source_bit_depth: Optional[int] = None,
        source_has_alpha: bool = False,
    ) -> CompressionAttempt:
        del source_orientation, source_has_icc_profile
        del source_bit_depth, source_has_alpha
        assert Image is not None
        t0 = time.perf_counter()
        quality = clamp(int(quality), 1, 100)
        out_dims: Optional[ImageDimensions] = None
        try:
            self.cancellation.raise_if_cancelled()
            with Image.open(source) as img:
                source_info = dict(img.info or {})
                icc_profile = source_info.get("icc_profile")
                source_exif = source_info.get("exif")
                if ImageOps is not None:
                    img = ImageOps.exif_transpose(img)
                img = self._prepare_mode(img, codec)
                self.cancellation.raise_if_cancelled()
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
                if codec == ImageCodec.PNG:
                    colors = quality_to_png_colors(quality)
                    if colors is not None:
                        if self._is_high_bit_depth_mode(img.mode):
                            raise EncodeError(
                                "Palette PNG conversion would reduce source bit depth"
                            )
                        img = self._quantize_png(img, colors)
                save_kwargs = self._save_kwargs(
                    codec,
                    quality,
                    progressive,
                    strip_metadata,
                    source_exif,
                    source_pixel_format=source_pixel_format,
                )
                if preserve_icc_profile and icc_profile:
                    save_kwargs["icc_profile"] = icc_profile
                self.cancellation.raise_if_cancelled()
                img.save(destination, **save_kwargs)
                self.cancellation.raise_if_cancelled()
        except UserAbortError:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except Exception as exc:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
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
    def _is_high_bit_depth_mode(mode: str) -> bool:
        normalized = (mode or "").upper()
        return normalized.startswith("I;16") or normalized in {"I", "F"}

    @staticmethod
    def _quantize_png(img: Any, colors: int) -> Any:
        if img.mode == "P" and len(img.getcolors(maxcolors=256) or []) <= colors:
            return img.copy()
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            return rgba.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
        return img.convert("RGB").quantize(colors=colors, method=Image.Quantize.MEDIANCUT)

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

        if codec == ImageCodec.PNG:
            if img.mode == "P":
                return img.copy()
            if img.mode == "LA":
                return img.convert("RGBA")
            if img.mode in ("RGBA", "RGB", "L", "1", "I", "F"):
                return img
            if img.mode.startswith("I;16"):
                return img
            if "A" in img.mode:
                return img.convert("RGBA")
            return img.convert("RGB")

        if img.mode == "P":
            return img.copy()
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
        source_exif: Any,
        *,
        source_pixel_format: Optional[str] = None,
    ) -> Dict[str, Any]:
        if codec == ImageCodec.JPG:
            kwargs: Dict[str, Any] = {
                "format": "JPEG",
                "quality": quality,
                "optimize": True,
                "progressive": progressive,
                "subsampling": PillowEncoder._jpeg_subsampling(
                    source_pixel_format
                ),
            }
        elif codec == ImageCodec.PNG:
            kwargs = {
                "format": "PNG",
                "optimize": True,
                "compress_level": quality_to_png_level(quality),
            }
        else:
            webp_q, lossless = quality_to_webp_params(quality)
            kwargs = {
                "format": "WEBP",
                "quality": webp_q,
                "method": 6,
                "lossless": lossless,
            }
        if (
            not strip_metadata
            and source_exif
            and codec in (
                ImageCodec.JPG,
                ImageCodec.PNG,
                ImageCodec.WEBP,
            )
        ):
            cleaned_exif = PillowEncoder._exif_without_orientation(
                source_exif
            )
            if cleaned_exif:
                kwargs["exif"] = cleaned_exif
        return kwargs

    @staticmethod
    def _jpeg_subsampling(
        source_pixel_format: Optional[str],
    ) -> int:
        pixel_format = (source_pixel_format or "").casefold()
        if any(
            marker in pixel_format
            for marker in ("444", "rgb", "bgr", "gbr", "cmyk")
        ):
            return 0
        if "422" in pixel_format:
            return 1
        return 2

    @staticmethod
    def _exif_without_orientation(source_exif: Any) -> Optional[bytes]:
        if not PILLOW_AVAILABLE or Image is None:
            return None
        try:
            exif = Image.Exif()
            exif.load(source_exif)
            if 274 in exif:
                del exif[274]
            return exif.tobytes()
        except Exception:
            return None


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
        preserve_icc_profile: bool = True,
        progressive: bool = True,
        source_pixel_format: Optional[str] = None,
        source_orientation: int = 1,
        source_has_icc_profile: Optional[bool] = None,
        source_bit_depth: Optional[int] = None,
        source_has_alpha: bool = False,
    ) -> CompressionAttempt:
        order: List[Encoder] = []
        if (
            source_has_icc_profile is None
            and preserve_icc_profile
        ):
            source_has_icc_profile = self._source_has_icc(source)
        requires_pillow = (
            not strip_metadata
            or source_orientation not in (0, 1)
            or (
                preserve_icc_profile
                and PILLOW_AVAILABLE
                and source_has_icc_profile
            )
            or (codec == ImageCodec.JPG and source_has_alpha)
        )
        palette_png = (
            codec == ImageCodec.PNG
            and quality_to_png_colors(quality) is not None
        )
        pillow_required_without_pillow = (
            self.pillow_encoder is None
            and (
                not strip_metadata
                or source_orientation not in (0, 1)
                or (
                    preserve_icc_profile
                    and bool(source_has_icc_profile)
                )
                or (codec == ImageCodec.JPG and source_has_alpha)
            )
        )
        if pillow_required_without_pillow:
            requirements: List[str] = []
            if not strip_metadata:
                requirements.append("preserve image metadata")
            if source_orientation not in (0, 1):
                requirements.append("normalize EXIF orientation safely")
            if preserve_icc_profile and source_has_icc_profile:
                requirements.append("preserve the ICC profile")
            if codec == ImageCodec.JPG and source_has_alpha:
                requirements.append("flatten transparency onto white")
            return CompressionAttempt(
                backend=EncodeBackend.NONE,
                quality=quality,
                ffmpeg_q=None,
                scale_factor=scale_factor,
                output_bytes=0,
                elapsed_sec=0.0,
                success=False,
                error=(
                    "Pillow 10.0+ is required to "
                    + ", ".join(requirements)
                ),
                dimensions=target_dimensions,
                output_path=None,
            )
        if palette_png and self.pillow_encoder is None:
            return CompressionAttempt(
                backend=EncodeBackend.NONE,
                quality=quality,
                ffmpeg_q=None,
                scale_factor=scale_factor,
                output_bytes=0,
                elapsed_sec=0.0,
                success=False,
                error="Pillow 10.0+ is required for palette PNG output",
                dimensions=target_dimensions,
                output_path=None,
            )
        high_bit_depth_png = (
            codec == ImageCodec.PNG
            and (source_bit_depth or 0) > 8
        )
        if (
            high_bit_depth_png
            and preserve_icc_profile
            and source_has_icc_profile
        ):
            return CompressionAttempt(
                backend=EncodeBackend.NONE,
                quality=quality,
                ffmpeg_q=None,
                scale_factor=scale_factor,
                output_bytes=0,
                elapsed_sec=0.0,
                success=False,
                error=(
                    "Cannot safely preserve both a >8-bit PNG and "
                    "its ICC profile with the available encoders"
                ),
                dimensions=target_dimensions,
                output_path=None,
            )
        if high_bit_depth_png:
            order = [self.ffmpeg_encoder]
        else:
            pillow_first = codec == ImageCodec.PNG or requires_pillow
            if self.pillow_encoder and (requires_pillow or palette_png):
                order = [self.pillow_encoder]
            elif self.pillow_encoder and (
                self.prefer == EncodeBackend.PILLOW or pillow_first
            ):
                order = [self.pillow_encoder, self.ffmpeg_encoder]
            else:
                order = [self.ffmpeg_encoder]
                if self.pillow_encoder:
                    order.append(self.pillow_encoder)

        last: Optional[CompressionAttempt] = None
        for enc in order:
            tmp = unique_temp_path(destination.parent, destination.suffix, label=enc.name)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

            try:
                attempt = enc.encode_image(
                    source,
                    tmp,
                    codec=codec,
                    quality=quality,
                    scale_factor=scale_factor,
                    target_dimensions=target_dimensions,
                    strip_metadata=strip_metadata,
                    preserve_icc_profile=preserve_icc_profile,
                    progressive=progressive,
                    source_pixel_format=source_pixel_format,
                    source_orientation=source_orientation,
                    source_has_icc_profile=source_has_icc_profile,
                    source_bit_depth=source_bit_depth,
                    source_has_alpha=source_has_alpha,
                )
            except UserAbortError:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            last = attempt
            if attempt.success and attempt.output_bytes > 0:
                try:
                    atomic_replace(tmp, destination)
                except OSError as exc:
                    attempt.success = False
                    attempt.error = f"atomic_replace_failed: {exc}"
                    attempt.output_path = None
                    last = attempt
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
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

    @staticmethod
    def _source_has_icc(source: Path) -> bool:
        if not PILLOW_AVAILABLE or Image is None:
            return False
        try:
            with Image.open(source) as img:
                return bool(img.info.get("icc_profile"))
        except UserAbortError:
            raise
        except Exception:
            return False


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
        source_probe: Optional[ImageProbeResult] = None,
    ) -> CompressionAttempt:
        codec = self._codec_for(source)
        encode_kwargs: Dict[str, Any] = {
            "codec": codec,
            "quality": quality,
            "scale_factor": scale_factor,
            "target_dimensions": target_dimensions,
            "strip_metadata": self.config.strip_metadata,
            "preserve_icc_profile": self.config.preserve_icc_profile,
            "progressive": self.config.progressive_jpeg,
        }
        if source_probe is not None:
            encode_kwargs.update(
                source_pixel_format=source_probe.pixel_format,
                source_orientation=source_probe.exif_orientation,
                source_has_icc_profile=source_probe.has_icc_profile,
                source_bit_depth=source_probe.bit_depth,
                source_has_alpha=source_probe.has_alpha,
            )
        return self.encoder.encode_image(
            source,
            destination,
            **encode_kwargs,
        )

    def process_image(
        self, probe: ImageProbeResult, final_dest: Path, work_dir: Path
    ) -> ImageJobResult:
        work_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        source = probe.path
        result = ImageJobResult(
            source=source,
            status=ImageStatus.PENDING,
            original_bytes=probe.size_bytes,
            original_dimensions=probe.dimensions,
            strategy_used=self.config.strategy,
        )
        try:
            if not probe.is_readable:
                result.status = ImageStatus.SKIPPED_CORRUPT
                result.message = probe.probe_error or "unreadable"
                result.error_detail = probe.probe_error
                return result

            target_codec = self._codec_for(source)
            same_format = codecs_match_for_copy(source, target_codec)

            if self.config.size_policy.is_acceptable(probe.size_bytes):
                if same_format:
                    handled = self._handle_under_limit(
                        probe,
                        final_dest,
                        work_dir,
                        result,
                        t0,
                    )
                    if handled is not None:
                        return handled
                if self.config.strategy == CompressionStrategy.COPY_ONLY_UNDER_LIMIT:
                    result.status = ImageStatus.SKIPPED_UNDER_LIMIT
                    result.message = "format conversion disabled by copy-only strategy"
                    return result

            if self.config.dry_run:
                result.status = ImageStatus.DRY_RUN
                result.message = t("msg_dry_run")
                return result

            if self.config.strategy == CompressionStrategy.COPY_ONLY_UNDER_LIMIT:
                result.status = ImageStatus.SIZE_LIMIT_FAILED
                result.message = t("msg_copy_only_skip")
                result.error_detail = t("msg_copy_only_skip")
                return result

            self.config.cancellation.raise_if_cancelled()
            if self._codec_for(source) == ImageCodec.PNG:
                self._strategy_png(probe, final_dest, work_dir, result)
                return result

            if (
                self.config.size_policy.is_preferred(probe.size_bytes)
                and not same_format
            ):
                converted = self._try_format_convert_only(
                    probe,
                    final_dest,
                    work_dir,
                    result,
                )
                if converted:
                    return result

            if self.config.strategy == CompressionStrategy.LOSSLESS_FIRST:
                self._strategy_lossless_first(probe, final_dest, work_dir, result)
            elif self.config.strategy == CompressionStrategy.HIGH_QUALITY_LOSSY:
                self._strategy_high_quality(probe, final_dest, work_dir, result)
            elif self.config.strategy == CompressionStrategy.BINARY_SEARCH:
                self._strategy_binary_search(probe, final_dest, work_dir, result)
            else:
                self._strategy_aggressive_adaptive(probe, final_dest, work_dir, result)
            return result
        except UserAbortError:
            raise
        except Exception as exc:
            self.logger.exception("Unhandled error processing %s", source.name)
            result.status = ImageStatus.FAILED
            result.message = "unhandled_exception"
            result.error_detail = f"{type(exc).__name__}: {exc}"
            return result
        finally:
            self._cleanup_uncommitted_attempts(result)
            result.elapsed_sec = time.perf_counter() - t0

    def _try_format_convert_only(
        self,
        probe: ImageProbeResult,
        final_dest: Path,
        work_dir: Path,
        result: ImageJobResult,
    ) -> bool:
        tmp = unique_temp_path(work_dir, suffix=self._temp_suffix(probe.path))
        attempt = self._encode(
            probe.path,
            tmp,
            quality=QUALITY_LOSSLESS_PROXY,
            scale_factor=1.0,
            source_probe=probe,
        )
        result.attempts.append(attempt)
        if attempt.success and attempt.is_preferred(self.config.size_policy):
            self._commit_best(attempt, final_dest, result, quality=QUALITY_LOSSLESS_PROXY)
            return result.status == ImageStatus.COMPRESSED
        self._discard_attempt(attempt)
        return False

    def _handle_under_limit(
        self,
        probe: ImageProbeResult,
        final_dest: Path,
        work_dir: Path,
        result: ImageJobResult,
        t0: float,
    ) -> Optional[ImageJobResult]:
        if not self.config.copy_under_limit:
            result.status = ImageStatus.SKIPPED_UNDER_LIMIT
            result.message = t("msg_already_not_copied")
            result.elapsed_sec = time.perf_counter() - t0
            return result
        if self.config.dry_run:
            result.status = ImageStatus.DRY_RUN
            result.message = t("msg_dry_run")
            result.elapsed_sec = time.perf_counter() - t0
            return result
        stage: Optional[Path] = None
        try:
            self.config.cancellation.raise_if_cancelled()
            if self.config.strip_metadata:
                tmp = unique_temp_path(work_dir, self._temp_suffix(probe.path), label="sanitize")
                attempt = self._encode(
                    probe.path,
                    tmp,
                    quality=QUALITY_LOSSLESS_PROXY,
                    scale_factor=1.0,
                    source_probe=probe,
                )
                result.attempts.append(attempt)
                if not attempt.success:
                    result.status = ImageStatus.FAILED
                    result.message = "metadata_sanitize_failed"
                    result.error_detail = attempt.error
                    self._discard_attempt(attempt)
                    result.elapsed_sec = time.perf_counter() - t0
                    return result
                if not self.config.size_policy.is_acceptable(attempt.output_bytes):
                    self._discard_attempt(attempt)
                    return None
                self._commit_best(attempt, final_dest, result)
                if result.status == ImageStatus.COMPRESSED:
                    result.status = ImageStatus.SANITIZED
                    result.message = t("msg_already_copied")
            else:
                stage = unique_temp_path(final_dest.parent, final_dest.suffix, label="copy")
                shutil.copy2(probe.path, stage)
                self.config.cancellation.run_if_active(
                    lambda: atomic_publish(
                        stage,
                        final_dest,
                        overwrite=self.config.overwrite_output,
                    )
                )
                stage = None
                fsync_directory(final_dest.parent)
                result.output_path = final_dest
                result.output_bytes = file_size(final_dest)
                if not self.config.size_policy.is_acceptable(
                    result.output_bytes
                ):
                    try:
                        final_dest.unlink(missing_ok=True)
                    except OSError:
                        pass
                    result.output_path = None
                    raise WorkspaceError(
                        "published copy violates the strict size limit"
                    )
                result.output_dimensions = probe.dimensions
                result.backend = EncodeBackend.COPY
                result.status = ImageStatus.COPIED
                result.message = t("msg_already_copied")
        except UserAbortError:
            raise
        except Exception as exc:
            result.status = ImageStatus.FAILED
            result.message = (
                "metadata_sanitize_failed"
                if self.config.strip_metadata
                else "copy_failed"
            )
            result.error_detail = f"{type(exc).__name__}: {exc}"
        finally:
            if stage is not None:
                try:
                    stage.unlink(missing_ok=True)
                except OSError:
                    pass
        result.elapsed_sec = time.perf_counter() - t0
        return result

    def _strategy_png(
        self, probe: ImageProbeResult, final_dest: Path, work_dir: Path, result: ImageJobResult
    ) -> None:
        qualities: Sequence[int] = (QUALITY_LOSSLESS_PROXY,)
        palette_supported = (
            self.encoder.pillow_encoder is not None
            and not self._probe_is_high_bit_depth(probe)
        )
        if (
            palette_supported
            and self.config.strategy == CompressionStrategy.LOSSLESS_FIRST
        ):
            qualities = (
                QUALITY_LOSSLESS_PROXY,
                PNG_PALETTE_QUALITIES[0],
            )
        elif (
            palette_supported
            and self.config.strategy
            != CompressionStrategy.COPY_ONLY_UNDER_LIMIT
        ):
            qualities = (QUALITY_LOSSLESS_PROXY, *PNG_PALETTE_QUALITIES)
        best: Optional[CompressionAttempt] = None
        for quality in qualities:
            self.config.cancellation.raise_if_cancelled()
            tmp = unique_temp_path(work_dir, ".png", label="png")
            attempt = self._encode(
                probe.path,
                tmp,
                quality=quality,
                scale_factor=1.0,
                source_probe=probe,
            )
            attempt.variant = (
                "lossless"
                if quality == QUALITY_LOSSLESS_PROXY
                else f"palette-{quality_to_png_colors(quality)}"
            )
            result.attempts.append(attempt)
            if not attempt.success:
                self._discard_attempt(attempt)
                continue
            selected = self._better_attempt(best, attempt)
            if selected is attempt:
                self._discard_attempt(best)
                best = attempt
            else:
                self._discard_attempt(attempt)
            if best and best.is_preferred(self.config.size_policy):
                break
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(best, final_dest, result)
            return
        self._fail_or_commit_closest(best, final_dest, result)

    @staticmethod
    def _probe_is_high_bit_depth(probe: ImageProbeResult) -> bool:
        mode = (probe.pixel_format or "").upper()
        return (
            (probe.bit_depth or 0) > 8
            or mode.startswith("I;16")
            or mode in {"I", "F"}
        )

    def _strategy_lossless_first(
        self, probe: ImageProbeResult, final_dest: Path, work_dir: Path, result: ImageJobResult
    ) -> None:
        best = self._phase_near_lossless(probe, work_dir, result)
        if best and best.is_preferred(self.config.size_policy):
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
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(best, final_dest, result)
            return
        if self.config.allow_downscale:
            best = self._phase_downscale_search(probe, work_dir, result, previous_best=best)
            if best and best.is_preferred(self.config.size_policy):
                self._commit_best(best, final_dest, result)
                return
        self._fail_or_commit_closest(best, final_dest, result)

    def _strategy_high_quality(
        self, probe: ImageProbeResult, final_dest: Path, work_dir: Path, result: ImageJobResult
    ) -> None:
        best = self._phase_quality_ladder(
            probe, work_dir, result, qualities=(95, 93, 92, 90), scale_factor=1.0
        )
        if best and best.is_preferred(self.config.size_policy):
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
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(best, final_dest, result)
            return
        if self.config.allow_downscale:
            best = self._phase_downscale_search(probe, work_dir, result, previous_best=best)
            if best and best.is_preferred(self.config.size_policy):
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
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(best, final_dest, result)
            return
        if self.config.allow_downscale:
            best = self._phase_downscale_search(probe, work_dir, result, previous_best=best)
            if best and best.is_preferred(self.config.size_policy):
                self._commit_best(best, final_dest, result)
                return
        self._fail_or_commit_closest(best, final_dest, result)

    def _strategy_aggressive_adaptive(
        self, probe: ImageProbeResult, final_dest: Path, work_dir: Path, result: ImageJobResult
    ) -> None:
        best = self._phase_near_lossless(probe, work_dir, result)
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(best, final_dest, result, quality=QUALITY_LOSSLESS_PROXY)
            return
        best = self._phase_quality_ladder(
            probe, work_dir, result, qualities=(95, 92, 90), scale_factor=1.0, previous_best=best
        )
        if best and best.is_preferred(self.config.size_policy):
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
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(best, final_dest, result)
            return
        if self.config.allow_downscale:
            best = self._phase_downscale_search(probe, work_dir, result, previous_best=best)
            if best and best.is_preferred(self.config.size_policy):
                self._commit_best(best, final_dest, result)
                return
        self._fail_or_commit_closest(best, final_dest, result)

    def _phase_near_lossless(
        self, probe: ImageProbeResult, work_dir: Path, result: ImageJobResult
    ) -> Optional[CompressionAttempt]:
        self.config.cancellation.raise_if_cancelled()
        already_attempted = any(
            attempt.quality == QUALITY_LOSSLESS_PROXY
            and attempt.scale_factor == 1.0
            for attempt in result.attempts
        )
        if already_attempted:
            return None
        tmp = unique_temp_path(work_dir, suffix=self._temp_suffix(probe.path))
        attempt = self._encode(
            probe.path,
            tmp,
            quality=QUALITY_LOSSLESS_PROXY,
            scale_factor=1.0,
            source_probe=probe,
        )
        result.attempts.append(attempt)
        if attempt.success:
            return attempt
        self._discard_attempt(attempt)
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
        for q in dict.fromkeys(qualities):
            self.config.cancellation.raise_if_cancelled()
            tmp = unique_temp_path(work_dir, suffix=self._temp_suffix(probe.path))
            attempt = self._encode(
                probe.path,
                tmp,
                quality=q,
                scale_factor=scale_factor,
                source_probe=probe,
            )
            result.attempts.append(attempt)
            if not attempt.success:
                self._discard_attempt(attempt)
                continue
            selected = self._better_attempt(best, attempt)
            if selected is attempt:
                self._discard_attempt(best)
                best = attempt
            else:
                self._discard_attempt(attempt)
            if best and best.is_preferred(self.config.size_policy):
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
        best = previous_best
        iterations = 0
        while lo <= hi and iterations < BINARY_SEARCH_MAX_ITERS:
            self.config.cancellation.raise_if_cancelled()
            iterations += 1
            mid = (lo + hi) // 2
            tmp = unique_temp_path(work_dir, suffix=self._temp_suffix(probe.path))
            attempt = self._encode(
                probe.path,
                tmp,
                quality=mid,
                scale_factor=scale_factor,
                target_dimensions=target_dims,
                source_probe=probe,
            )
            result.attempts.append(attempt)
            if not attempt.success:
                self._discard_attempt(attempt)
                self.config.cancellation.raise_if_cancelled()
                retry_tmp = unique_temp_path(
                    work_dir,
                    suffix=self._temp_suffix(probe.path),
                    label="retry",
                )
                attempt = self._encode(
                    probe.path,
                    retry_tmp,
                    quality=mid,
                    scale_factor=scale_factor,
                    target_dimensions=target_dims,
                    source_probe=probe,
                )
                result.attempts.append(attempt)
                if not attempt.success:
                    self._discard_attempt(attempt)
                    hi = mid - 1
                    continue
            selected = self._better_attempt(best, attempt)
            if selected is attempt:
                self._discard_attempt(best)
                best = attempt
            else:
                self._discard_attempt(attempt)
            if attempt.is_preferred(self.config.size_policy):
                lo = mid + 1
            else:
                hi = mid - 1
        return best

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
            self.config.cancellation.raise_if_cancelled()
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
                scale_factor=target.scale_from(probe.dimensions),
                target_dims=target,
                previous_best=None,
            )
            if candidate is not None:
                if candidate.dimensions is None:
                    candidate.dimensions = target
                candidate.scale_factor = target.scale_from(probe.dimensions)
                selected = self._better_attempt(best, candidate)
                if selected is candidate:
                    self._discard_attempt(best)
                    best = candidate
                else:
                    self._discard_attempt(candidate)
                if best and best.is_preferred(self.config.size_policy):
                    return best
        return best

    def _better_attempt(
        self, current: Optional[CompressionAttempt], new: CompressionAttempt
    ) -> CompressionAttempt:
        if current is None or not current.success:
            return new if new.success else current or new
        if not new.success:
            return current
        policy = self.config.size_policy
        cur_preferred = current.is_preferred(policy)
        new_preferred = new.is_preferred(policy)
        if cur_preferred != new_preferred:
            return new if new_preferred else current
        cur_acceptable = current.is_acceptable(policy)
        new_acceptable = new.is_acceptable(policy)
        if cur_acceptable != new_acceptable:
            return new if new_acceptable else current
        if cur_acceptable and new_acceptable:
            if new.scale_factor != current.scale_factor:
                return new if new.scale_factor > current.scale_factor else current
            return self._prefer_quality(current, new)
        return new if new.output_bytes < current.output_bytes else current

    @staticmethod
    def _prefer_quality(
        current: CompressionAttempt,
        new: CompressionAttempt,
    ) -> CompressionAttempt:
        nq, cq = (new.quality or 0, current.quality or 0)
        if nq != cq:
            return new if nq > cq else current
        return new if new.output_bytes > current.output_bytes else current

    @staticmethod
    def _discard_attempt(attempt: Optional[CompressionAttempt]) -> None:
        if attempt is None or attempt.output_path is None:
            return
        try:
            if attempt.output_path.exists():
                attempt.output_path.unlink()
        except OSError:
            pass
        attempt.output_path = None

    @classmethod
    def _cleanup_uncommitted_attempts(
        cls,
        result: ImageJobResult,
    ) -> None:
        for attempt in result.attempts:
            cls._discard_attempt(attempt)

    def _commit_best(
        self,
        best: CompressionAttempt,
        final_dest: Path,
        result: ImageJobResult,
        *,
        quality: Optional[int] = None,
    ) -> None:
        del quality
        final_dest.parent.mkdir(parents=True, exist_ok=True)
        if not (
            best.success
            and best.output_path is not None
            and best.output_path.is_file()
            and best.output_bytes > 0
        ):
            result.status = ImageStatus.FAILED
            result.message = "selected_candidate_missing"
            result.error_detail = best.error
            return
        live_size = file_size(best.output_path)
        if live_size > 0:
            best.output_bytes = live_size
        if not self.config.size_policy.is_acceptable(best.output_bytes):
            result.status = ImageStatus.SIZE_LIMIT_FAILED
            result.output_bytes = best.output_bytes
            result.quality_used = best.quality
            result.ffmpeg_q_used = best.ffmpeg_q
            result.scale_factor = best.scale_factor
            result.backend = best.backend
            result.variant = best.variant
            result.message = t(
                "msg_size_fail",
                limit=human_bytes(self.config.max_bytes, binary=False),
                best=human_bytes(best.output_bytes),
            )
            self._discard_attempt(best)
            return
        try:
            self.config.cancellation.run_if_active(
                lambda: atomic_publish(
                    best.output_path,
                    final_dest,
                    overwrite=self.config.overwrite_output,
                )
            )
            best.output_path = None
            fsync_directory(final_dest.parent)
        except UserAbortError:
            raise
        except Exception as exc:
            result.status = ImageStatus.FAILED
            result.message = "commit_failed"
            result.error_detail = f"{type(exc).__name__}: {exc}"
            self._discard_attempt(best)
            return
        result.output_path = final_dest
        result.output_bytes = file_size(final_dest)
        if not self.config.size_policy.is_acceptable(result.output_bytes):
            result.status = ImageStatus.FAILED
            result.message = "published_output_validation_failed"
            result.error_detail = (
                "published output is missing or violates the strict size limit"
            )
            try:
                final_dest.unlink(missing_ok=True)
            except OSError:
                pass
            result.output_path = None
            return
        result.output_dimensions = best.dimensions or result.original_dimensions
        if result.output_dimensions and result.original_dimensions:
            best.scale_factor = result.output_dimensions.scale_from(result.original_dimensions)
        result.quality_used = best.quality
        result.ffmpeg_q_used = best.ffmpeg_q
        result.scale_factor = best.scale_factor
        result.backend = best.backend
        result.variant = best.variant
        result.status = ImageStatus.COMPRESSED
        result.message = t(
            "msg_compressed",
            size=human_bytes(result.output_bytes),
            q=result.variant or result.quality_used,
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
        if best.is_acceptable(self.config.size_policy):
            self._commit_best(best, final_dest, result)
            return
        result.status = ImageStatus.SIZE_LIMIT_FAILED
        result.output_bytes = best.output_bytes
        result.quality_used = best.quality
        result.ffmpeg_q_used = best.ffmpeg_q
        result.scale_factor = best.scale_factor
        result.backend = best.backend
        result.variant = best.variant
        self._discard_attempt(best)
        result.message = t(
            "msg_unable",
            limit=human_bytes(self.config.max_bytes, binary=False),
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
        size_policy: SizePolicy = DEFAULT_SIZE_POLICY,
        cancellation: Optional[CancellationToken] = None,
    ) -> None:
        self.root = root.resolve()
        self.prober = prober
        self.logger = logger
        self.include_convertibles = include_convertibles
        self.output_dir = output_dir.resolve() if output_dir else None
        self.size_policy = size_policy
        self.cancellation = cancellation or CancellationToken()

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
            if entry.name.casefold() in BINARY_FILENAMES_LOWER:
                continue
            if suffix in JPEG_EXTENSIONS:
                found.append(entry)
            elif self.include_convertibles and suffix in CONVERTIBLE_EXTENSIONS:
                found.append(entry)
        return found

    def scan(self, *, max_workers: int = 4) -> PreflightSummary:
        t0 = time.perf_counter()
        self.cancellation.raise_if_cancelled()
        files = self.discover_files()
        self.logger.info("Discovered %d candidate image(s) in %s", len(files), self.root)
        images: List[ImageProbeResult] = []
        workers = max(1, min(max_workers, len(files) or 1))
        pool: Optional[ThreadPoolExecutor] = None
        try:
            if files:
                pool = ThreadPoolExecutor(max_workers=workers)
                future_map = {pool.submit(self.prober.probe, f): f for f in files}
                for fut in as_completed(future_map):
                    self.cancellation.raise_if_cancelled()
                    path = future_map[fut]
                    try:
                        images.append(fut.result())
                    except Exception as exc:
                        if isinstance(exc, UserAbortError):
                            raise exc
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
        except (KeyboardInterrupt, UserAbortError):
            self.cancellation.cancel()
            cancel_all = getattr(self.prober.runner, "cancel_all", None)
            if callable(cancel_all):
                cancel_all()
            if pool is not None:
                while True:
                    try:
                        pool.shutdown(wait=True, cancel_futures=True)
                        break
                    except KeyboardInterrupt:
                        self.cancellation.cancel()
                        if callable(cancel_all):
                            cancel_all()
                pool = None
            raise
        finally:
            if pool is not None:
                shutdown_interrupted = False
                while True:
                    try:
                        pool.shutdown(
                            wait=True,
                            cancel_futures=self.cancellation.cancelled,
                        )
                        break
                    except KeyboardInterrupt:
                        shutdown_interrupted = True
                        self.cancellation.cancel()
                        cancel_all = getattr(
                            self.prober.runner,
                            "cancel_all",
                            None,
                        )
                        if callable(cancel_all):
                            cancel_all()
                if shutdown_interrupted:
                    raise UserAbortError("operation cancelled")
        images.sort(key=lambda i: (i.path.name.casefold(), i.path.name))
        return self._summarise(images, time.perf_counter() - t0)

    def _summarise(self, images: List[ImageProbeResult], elapsed: float) -> PreflightSummary:
        jpeg_count = sum((1 for i in images if i.path.suffix.lower() in JPEG_EXTENSIONS))
        convertible_count = len(images) - jpeg_count
        under = [i for i in images if i.is_readable and not i.is_over_limit(self.size_policy)]
        over = [i for i in images if i.is_readable and i.is_over_limit(self.size_policy)]
        corrupt = [i for i in images if not i.is_readable]
        total_bytes = sum((i.size_bytes for i in images))
        over_bytes = sum((i.size_bytes for i in over))
        savings_low = 0
        savings_high = 0
        for img in over:
            est_high_q = estimate_output_bytes(img.size_bytes, 93)
            est_low_q = estimate_output_bytes(img.size_bytes, 55)
            max_save = max(0, img.size_bytes - self.size_policy.preferred_target_bytes)
            savings_low += min(max_save, max(0, img.size_bytes - est_high_q))
            savings_high += min(max_save, max(0, img.size_bytes - est_low_q))
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
        pot_hist = Counter(
            (i.compression_potential_for(self.size_policy) for i in images)
        )
        return PreflightSummary(
            root=self.root,
            images=images,
            size_policy=self.size_policy,
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
        configure_stdio_utf8()
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
        self.rule(rich_escape(t("lang_prompt")))
        options = {"1": "en", "2": "vi", "en": "en", "vi": "vi", "e": "en", "v": "vi"}
        if self.console is not None:
            table = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
            table.add_column("k", style="accent")
            table.add_column("v")
            table.add_row("1", Text(t("lang_en")))
            table.add_row("2", Text(t("lang_vi")))
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
            banner_text = Text(title, style="title")
            banner_text.append("\n")
            banner_text.append(subtitle, style="muted")
            self.console.print(
                Panel(
                    Align.center(banner_text),
                    border_style="bright_cyan",
                    box=box.DOUBLE if hasattr(box, "DOUBLE") else box.ROUNDED,
                )
            )
        else:
            print(f"\n*** {title} ***\n{subtitle}\n")

    def show_environment(
        self,
        root: Path,
        ffmpeg: Path,
        ffprobe: Path,
        ffmpeg_ver: str,
        ffprobe_ver: str,
        size_policy: SizePolicy = DEFAULT_SIZE_POLICY,
    ) -> None:
        self.rule(rich_escape(t("rule_environment")))
        rows = [
            (t("env_workdir"), str(root)),
            (t("env_ffmpeg"), f"{ffmpeg}  ({ffmpeg_ver[:60]})"),
            (t("env_ffprobe"), f"{ffprobe}  ({ffprobe_ver[:60]})"),
            (
                t("env_pillow"),
                (
                    f"{t('avail_yes')} ({PILLOW_VERSION})"
                    if PILLOW_AVAILABLE
                    else PILLOW_IMPORT_ERROR or t("avail_pillow_no")
                ),
            ),
            (t("env_rich"), t("avail_yes") if RICH_AVAILABLE else t("avail_rich_no")),
            (t("env_python"), sys.version.split()[0]),
            (t("env_platform"), f"{platform.system()} {platform.release()}"),
            (t("env_cpu"), str(os.cpu_count() or "?")),
            (
                t("env_size_limit"),
                t(
                    "size_limit_val",
                    size=human_bytes(
                        size_policy.strict_max_bytes,
                        binary=False,
                    ),
                ),
            ),
            (
                t("env_effective"),
                human_bytes(
                    size_policy.preferred_target_bytes,
                    binary=False,
                ),
            ),
        ]
        if self.console is not None:
            table = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
            table.add_column("Key", style="accent")
            table.add_column("Value")
            for k, v in rows:
                table.add_row(k, Text(v))
            self.console.print(table)
        else:
            for k, v in rows:
                print(f"  {k:20s}: {v}")

    def show_preflight(self, summary: PreflightSummary) -> None:
        self.rule(rich_escape(t("rule_preflight")))
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
                over_limit = img.is_over_limit(summary.size_policy)
                size_style = (
                    "size_ok"
                    if not over_limit and img.is_readable
                    else "size_bad" if img.is_readable else "warning"
                )
                status = (
                    "OK"
                    if img.is_readable and not over_limit
                    else "OVER" if img.is_readable else "BAD"
                )
                dims = str(img.dimensions) if img.dimensions else "—"
                need = (
                    f"{img.reduction_needed_pct(summary.size_policy):.0f}%"
                    if over_limit
                    else "—"
                )
                detail.add_row(
                    str(idx),
                    Text(img.path.name),
                    Text(human_bytes(img.size_bytes), style=size_style),
                    dims,
                    Text(img.codec_name or "—"),
                    status,
                    need,
                    img.compression_potential_for(summary.size_policy),
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
                flag = "OK " if not img.is_over_limit(summary.size_policy) else "OVER"
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
        self.rule(rich_escape(t("rule_strategy")))
        recommended = self.recommend_strategy(summary)
        strategies = list(CompressionStrategy)
        if self.console is not None:
            table = Table(title=t("strat_available"), box=box.ROUNDED, show_lines=True)
            table.add_column(t("col_opt"), style="accent", justify="center")
            table.add_column(t("col_name"), style="title")
            table.add_column(t("col_desc"))
            for s in strategies:
                name = Text(s.title)
                if s is recommended:
                    name.append("  ")
                    name.append(t("recommended_tag"), style="success")
                table.add_row(
                    s.value,
                    name,
                    Text(s.description),
                )
            self.console.print(table)
            self.print(
                f"\n[muted]{rich_escape(t('recommended_line', opt=recommended.value, title=recommended.title))}[/muted]"
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
                self.print(
                    f"[error]{rich_escape(t('invalid_selection'))}[/error]"
                )
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
        self.rule(rich_escape(t("rule_format")))
        options = list(OutputFormatChoice)
        recommended = OutputFormatChoice.JPG
        if self.console is not None:
            table = Table(title=t("format_available"), box=box.ROUNDED, show_lines=True)
            table.add_column(t("col_opt"), style="accent", justify="center")
            table.add_column(t("col_name"), style="title")
            table.add_column(t("col_desc"))
            for opt in options:
                name = Text(opt.title)
                if opt is recommended:
                    name.append("  ")
                    name.append(t("recommended_tag"), style="success")
                table.add_row(
                    opt.value,
                    name,
                    Text(opt.description),
                )
            self.console.print(table)
            self.print(
                f"\n[muted]{rich_escape(t('format_recommended', opt=recommended.value, title=recommended.title))}[/muted]"
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
                self.print(
                    f"[error]{rich_escape(t('invalid_format'))}[/error]"
                )
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
            title=f"[accent]{rich_escape(strategy.title)}[/accent]",
            n=f"[warning]{over_count}[/warning]",
        )
        if self.console is not None:
            return Confirm.ask(rich_msg, default=True, console=self.console)
        print(plain)
        raw = input(t("continue_yn")).strip().lower()
        return raw in ("", "y", "yes")

    def show_batch_report(self, report: BatchReport) -> None:
        self.rule(rich_escape(t("rule_complete")))
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
            table.add_row(t("res_output"), Text(str(report.config.output_dir)))
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
                    ImageStatus.SANITIZED: "size_ok",
                    ImageStatus.DRY_RUN: "muted",
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
                    Text(r.source.name),
                    Text(r.status.name, style=status_style),
                    human_bytes(r.original_bytes),
                    out_s,
                    saved_s,
                    str(r.quality_used) if r.quality_used is not None else "—",
                    f"{r.scale_factor:.2f}" if r.scale_factor != 1.0 else "1.00",
                    Text(note[:60]),
                )
            self.console.print(detail)

            if failed_rows:
                self.rule(rich_escape(t("fail_section_title")))
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
                        Text(r.source.name),
                        r.status.name,
                        Text(friendly_failure_reason(r)),
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


class OutputReservation:

    def __init__(
        self,
        path: Path,
        marker: Optional[Path],
        token: Optional[str] = None,
    ) -> None:
        self.path = path
        self.marker = marker
        self.token = token

    def release(self) -> None:
        if self.marker is None:
            return
        if self.token:
            try:
                payload = json.loads(
                    self.marker.read_text(encoding="utf-8")
                )
                if payload.get("token") != self.token:
                    self.marker = None
                    return
            except (OSError, json.JSONDecodeError, TypeError):
                self.marker = None
                return
        try:
            self.marker.unlink(missing_ok=True)
        except OSError:
            pass
        self.marker = None


class OutputPlanner:

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config

    def plan(
        self, images: Sequence[ImageProbeResult]
    ) -> Dict[Path, OutputReservation]:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        reserved: set[str] = set()
        plan: Dict[Path, OutputReservation] = {}
        ordered = sorted(
            images,
            key=lambda image: (
                image.path.name.casefold(),
                image.path.name,
            ),
        )
        try:
            for image in ordered:
                stem = safe_filename(image.path.stem)
                ext = resolve_output_codec(
                    image.path,
                    self.config.output_format,
                ).extension
                index = 1
                while True:
                    candidate = (
                        f"{stem}{ext}"
                        if index == 1
                        else f"{stem}_{index}{ext}"
                    )
                    folded = candidate.casefold()
                    index += 1
                    if folded in reserved:
                        continue
                    path = self.config.output_dir / candidate
                    marker: Optional[Path] = None
                    token: Optional[str] = None
                    if not self.config.overwrite_output:
                        claimed = self._claim(path)
                        if claimed is None:
                            continue
                        if isinstance(claimed, tuple):
                            marker, token = claimed
                        else:
                            marker = claimed
                    reserved.add(folded)
                    plan[image.path] = OutputReservation(
                        path,
                        marker,
                        token,
                    )
                    break
        except Exception:
            for reservation in plan.values():
                reservation.release()
            raise
        return plan

    @staticmethod
    def _claim(
        path: Path,
    ) -> Optional[Union[Path, Tuple[Path, str]]]:
        marker_key = uuid.uuid5(
            uuid.NAMESPACE_URL,
            path.name.casefold(),
        ).hex
        marker = path.with_name(
            f".jpeg-compressor-{marker_key}.reserve"
        )
        token = uuid.uuid4().hex
        try:
            fd = os.open(
                marker,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            if OutputPlanner._remove_stale_marker(marker):
                return OutputPlanner._claim(path)
            return None
        try:
            try:
                payload = json.dumps(
                    {
                        "destination": path.name,
                        "pid": os.getpid(),
                        "token": token,
                        "created_utc": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    }
                ).encode("utf-8")
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        try:
            destination_exists = any(
                entry != marker
                and entry.name.casefold() == path.name.casefold()
                for entry in path.parent.iterdir()
            )
        except OSError:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        if destination_exists:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        return marker, token

    @staticmethod
    def _remove_stale_marker(marker: Path) -> bool:
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            pid = int(payload.get("pid", 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        if pid <= 0 or OutputPlanner._pid_is_running(pid):
            return False
        try:
            marker.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    @staticmethod
    def _pid_is_running(pid: int) -> bool:
        if pid == os.getpid():
            return True
        if os.name == "nt":
            try:
                import ctypes

                process_query_limited_information = 0x1000
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                handle = kernel32.OpenProcess(
                    process_query_limited_information,
                    False,
                    pid,
                )
                if not handle:
                    return ctypes.get_last_error() == 5
                kernel32.CloseHandle(handle)
                return True
            except (AttributeError, OSError):
                return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True


class BatchProcessor:

    def __init__(
        self, config: RuntimeConfig, engine: StrategyEngine, logger: logging.Logger, cli: CLI
    ) -> None:
        self.config = config
        self.engine = engine
        self.logger = logger
        self.cli = cli

    def run(self, images: Sequence[ImageProbeResult]) -> List[ImageJobResult]:
        self.config.cancellation.raise_if_cancelled()
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        results: List[ImageJobResult] = []
        total = len(images)
        if total == 0:
            return results
        destinations: Dict[Path, OutputReservation] = {}
        run_root = (
            self.config.output_dir
            / TEMP_WORKDIR_NAME
            / f"run_{uuid.uuid4().hex}"
        )
        try:
            destinations = OutputPlanner(self.config).plan(images)
            run_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            for reservation in destinations.values():
                reservation.release()
            self._cleanup_dir(run_root)
            try:
                run_root.parent.rmdir()
            except OSError:
                pass
            raise

        def job(probe: ImageProbeResult) -> ImageJobResult:
            reservation = destinations[probe.path]
            work_dir: Optional[Path] = None
            result: Optional[ImageJobResult] = None
            try:
                self.config.cancellation.raise_if_cancelled()
                work_dir = run_root / (
                    f"{safe_filename(probe.path.stem)}_"
                    f"{uuid.uuid4().hex[:10]}"
                )
                work_dir.mkdir(parents=True, exist_ok=True)
                result = self.engine.process_image(
                    probe,
                    reservation.path,
                    work_dir,
                )
                return result
            finally:
                reservation.release()
                if work_dir is not None:
                    keep = (
                        self.config.keep_temp_on_failure
                        and result is not None
                        and result.status
                        in (
                            ImageStatus.FAILED,
                            ImageStatus.SIZE_LIMIT_FAILED,
                        )
                    )
                    if keep:
                        result.work_dir = work_dir
                        self.logger.info(
                            "Retained failed-job workspace: %s",
                            work_dir,
                        )
                    else:
                        self._cleanup_dir(work_dir)

        workers = max(1, min(self.config.max_workers, total))
        self.logger.info(
            "Processing %d image(s) with %d thread worker(s), strategy=%s",
            total,
            workers,
            self.config.strategy.name,
        )
        executor = ThreadPoolExecutor(max_workers=workers)
        future_map: Dict[
            Future[ImageJobResult],
            ImageProbeResult,
        ] = {}
        try:
            with self.cli.progress() as progress:
                task_id = progress.add_task(t("compressing"), total=total)
                future_map = {
                    executor.submit(job, probe): probe for probe in images
                }
                for fut in as_completed(future_map):
                    probe = future_map[fut]
                    try:
                        res = fut.result()
                    except UserAbortError:
                        raise
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
                        ImageStatus.SANITIZED: "SN",
                        ImageStatus.DRY_RUN: "DR",
                        ImageStatus.SIZE_LIMIT_FAILED: "!!",
                        ImageStatus.FAILED: "XX",
                        ImageStatus.SKIPPED_CORRUPT: "??",
                        ImageStatus.SKIPPED_UNDER_LIMIT: "--",
                    }.get(res.status, "??")
                    safe_name = (
                        rich_escape(probe.path.name[:40]) if RICH_AVAILABLE else probe.path.name[:40]
                    )
                    progress.update(
                        task_id,
                        advance=1,
                        description=(
                            f"{status_icon} {safe_name} "
                            f"{human_bytes(res.original_bytes)}→{human_bytes(res.output_bytes)}"
                        ),
                    )
        except (KeyboardInterrupt, UserAbortError) as exc:
            self.config.cancellation.cancel()
            cancel_all = getattr(
                self.engine.encoder.ffmpeg_encoder.runner,
                "cancel_all",
                None,
            )
            if callable(cancel_all):
                cancel_all()
            for future in future_map:
                future.cancel()
            raise BatchInterruptedError(
                "operation cancelled",
                results=results,
            ) from exc
        except BatchInterruptedError:
            raise
        finally:
            shutdown_interrupted = False
            shutdown_complete = False
            try:
                while True:
                    try:
                        executor.shutdown(
                            wait=True,
                            cancel_futures=self.config.cancellation.cancelled,
                        )
                        shutdown_complete = True
                        break
                    except KeyboardInterrupt:
                        shutdown_interrupted = True
                        self.config.cancellation.cancel()
                        cancel_all = getattr(
                            self.engine.encoder.ffmpeg_encoder.runner,
                            "cancel_all",
                            None,
                        )
                        if callable(cancel_all):
                            cancel_all()
                        for future in future_map:
                            future.cancel()
            finally:
                for reservation in destinations.values():
                    reservation.release()
                if shutdown_complete:
                    try:
                        run_root.rmdir()
                    except OSError:
                        if not self.config.keep_temp_on_failure:
                            self._cleanup_dir(run_root)
                    parent = run_root.parent
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
            if self.config.cancellation.cancelled:
                self._collect_completed_results(
                    future_map,
                    results,
                )
            if shutdown_interrupted:
                raise BatchInterruptedError(
                    "operation cancelled",
                    results=results,
                )
        order = {img.path: i for i, img in enumerate(images)}
        results.sort(key=lambda r: order.get(r.source, 10**9))
        return results

    @staticmethod
    def _collect_completed_results(
        future_map: Mapping[
            Future[ImageJobResult],
            ImageProbeResult,
        ],
        results: List[ImageJobResult],
    ) -> None:
        seen = {result.source for result in results}
        for future in future_map:
            if future.cancelled() or not future.done():
                continue
            try:
                completed = future.result()
            except BaseException as exc:
                if isinstance(exc, (SystemExit, GeneratorExit)):
                    raise
                continue
            if completed.source not in seen:
                results.append(completed)
                seen.add(completed.source)

    def _cleanup_dir(self, path: Path) -> None:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            self.logger.warning("Could not clean temporary directory %s: %s", path, exc)


class ReportWriter:

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def write_json(
        self,
        report: BatchReport,
        path: Path,
        *,
        cancellation: Optional[CancellationToken] = None,
    ) -> None:
        stage: Optional[Path] = None
        try:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            path.parent.mkdir(parents=True, exist_ok=True)
            stage = unique_temp_path(path.parent, ".json", label="report")
            with stage.open("w", encoding="utf-8") as fh:
                json.dump(report.to_dict(), fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            if cancellation is None:
                atomic_replace(stage, path)
            else:
                cancellation.run_if_active(
                    lambda: atomic_replace(stage, path)
                )
            stage = None
            fsync_directory(path.parent)
            self.logger.info("Wrote JSON report → %s", path)
        except UserAbortError:
            raise
        except OSError as exc:
            self.logger.error("Failed to write report %s: %s", path, exc)
        finally:
            if stage is not None:
                try:
                    stage.unlink(missing_ok=True)
                except OSError:
                    pass


class Application:

    def __init__(self) -> None:
        configure_stdio_utf8()
        self.cli = CLI()
        self.root = self._resolve_root()
        self.logger = setup_logging(level=DEFAULT_LOG_LEVEL)

    @staticmethod
    def _script_dir() -> Path:
        try:
            return Path(__file__).resolve().parent
        except NameError:
            return Path.cwd().resolve()

    @staticmethod
    def _resolve_root() -> Path:
        script_dir = Application._script_dir()
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

    def run(self) -> int:
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        cancellation = CancellationToken()
        try:
            self.cli.select_language()
            self.cli.banner()
        except KeyboardInterrupt:
            cancellation.cancel()
            raise UserAbortError("operation cancelled")
        try:
            locator = BinaryLocator(search_dirs=[self.root, self._script_dir()])
            ffmpeg, ffprobe = locator.resolve()
        except KeyboardInterrupt:
            cancellation.cancel()
            raise UserAbortError("operation cancelled")
        except BinaryNotFoundError as exc:
            self.cli.print(f"[error]{rich_escape(str(exc))}[/error]")
            return 2
        size_policy = SizePolicy()
        self.cli.show_environment(
            self.root,
            ffmpeg,
            ffprobe,
            locator.ffmpeg_version,
            locator.ffprobe_version,
            size_policy,
        )
        output_dir = self.root / DEFAULT_OUTPUT_DIRNAME
        log_path = output_dir / REPORT_LOG_NAME
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.cli.print(
                f"[error]{rich_escape(t('err_output_dir', exc=exc))}[/error]"
            )
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
        runner = SubprocessRunner(self.logger, cancellation=cancellation)
        prober = ImageProber(ffprobe, runner, self.logger)
        scanner = PreflightScanner(
            self.root,
            prober,
            self.logger,
            include_convertibles=True,
            output_dir=output_dir,
            size_policy=size_policy,
            cancellation=cancellation,
        )
        self.cli.print(
            f"\n[info]{rich_escape(t('preflight_running'))}[/info]"
        )
        try:
            summary = scanner.scan(max_workers=DEFAULT_MAX_WORKERS)
        except KeyboardInterrupt:
            cancellation.cancel()
            runner.cancel_all()
            raise UserAbortError("operation cancelled")
        except WorkspaceError as exc:
            self.cli.print(f"[error]{rich_escape(str(exc))}[/error]")
            return 4
        if summary.total_files_scanned == 0:
            self.cli.print(
                f"[warning]{rich_escape(t('no_images', exts=', '.join(sorted(SUPPORTED_EXTENSIONS))))}[/warning]"
            )
            return 0
        try:
            self.cli.show_preflight(summary)
            strategy = self.cli.select_strategy(summary)
            self.logger.info("User selected strategy: %s", strategy.name)
            output_format = self.cli.select_output_format()
            self.logger.info("User selected output format: %s", output_format.name)
            if not self.cli.confirm_start(strategy, summary.over_limit_count):
                self.cli.print(
                    f"[warning]{rich_escape(t('aborted_user'))}[/warning]"
                )
                return 0
            max_workers = DEFAULT_MAX_WORKERS
            if RICH_AVAILABLE and self.cli.console is not None:
                try:
                    max_workers = IntPrompt.ask(
                        t("parallel_workers"), default=DEFAULT_MAX_WORKERS, console=self.cli.console
                    )
                    max_workers = clamp(int(max_workers), 1, 32)
                except (ValueError, TypeError):
                    max_workers = DEFAULT_MAX_WORKERS
            else:
                raw = input(f"{t('parallel_workers')} [{DEFAULT_MAX_WORKERS}]: ").strip()
                if raw.isdigit():
                    max_workers = clamp(int(raw), 1, 32)
        except KeyboardInterrupt:
            cancellation.cancel()
            runner.cancel_all()
            raise UserAbortError("operation cancelled")
        config = RuntimeConfig(
            root_dir=self.root,
            output_dir=output_dir,
            size_policy=size_policy,
            strategy=strategy,
            max_workers=max_workers,
            allow_downscale=False,
            copy_under_limit=True,
            overwrite_output=False,
            strip_metadata=True,
            preserve_icc_profile=True,
            progressive_jpeg=True,
            include_convertibles=True,
            output_format=output_format,
            cancellation=cancellation,
        )
        ffmpeg_enc = FFmpegEncoder(ffmpeg, runner, self.logger)
        pillow_enc: Optional[PillowEncoder] = None
        if PILLOW_AVAILABLE:
            try:
                pillow_enc = PillowEncoder(
                    self.logger,
                    cancellation=cancellation,
                )
            except CompressorError:
                pillow_enc = None
        dual = DualEncoder(ffmpeg_enc, pillow_enc, self.logger)
        engine = StrategyEngine(dual, self.logger, config)
        processor = BatchProcessor(config, engine, self.logger, self.cli)
        self.cli.rule(rich_escape(t("rule_processing")))
        interrupted = False
        try:
            results = processor.run(summary.images)
        except BatchInterruptedError as exc:
            interrupted = True
            results = exc.results
            source_order = {
                image.path: index
                for index, image in enumerate(summary.images)
            }
            results.sort(
                key=lambda result: source_order.get(
                    result.source,
                    10**9,
                )
            )
        finished = datetime.now(timezone.utc)
        elapsed = time.perf_counter() - t0
        report = BatchReport(
            config=config,
            preflight=summary,
            results=results,
            started_at=started,
            finished_at=finished,
            total_elapsed_sec=elapsed,
            interrupted=interrupted,
        )
        writer = ReportWriter(self.logger)
        writer.write_json(
            report,
            output_dir / REPORT_JSON_NAME,
            cancellation=None if interrupted else cancellation,
        )
        self.cli.show_batch_report(report)
        if interrupted:
            self.cli.print(
                f"\n[warning]{rich_escape(t('res_interrupted'))}[/warning]"
            )
            raise UserAbortError("operation cancelled")
        if report.failed_count == 0:
            self.cli.print(
                f"\n[success]{rich_escape(t('all_success'))}[/success]"
            )
            return 0
        size_fails = sum(
            1
            for r in results
            if r.status == ImageStatus.SIZE_LIMIT_FAILED
        )
        hard_fails = sum(
            1
            for r in results
            if r.status
            in (
                ImageStatus.FAILED,
                ImageStatus.SKIPPED_CORRUPT,
                ImageStatus.SKIPPED_UNSUPPORTED,
            )
        )
        if hard_fails:
            self.cli.print(
                f"\n[error]{rich_escape(t('done_hard_fail', hard=hard_fails, size=size_fails))}[/error]"
            )
            return 1
        self.cli.print(
            f"\n[warning]{rich_escape(t('done_size_fail', n=size_fails))}[/warning]"
        )
        return 5


def _pause_if_windows_double_click(*, interrupted: bool = False) -> None:
    if sys.platform != "win32" or interrupted:
        return
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            print()
            input(t("press_enter"))
    except EOFError:
        pass


def main() -> int:
    exit_code = 1
    try:
        app = Application()
        exit_code = app.run()
    except UserAbortError:
        exit_code = 130
        print(f"\n{t('stopped_by_user')}")
    except (KeyboardInterrupt, EOFError):
        exit_code = 130
        print(f"\n{t('stopped_by_user')}")
    except BinaryNotFoundError as exc:
        exit_code = 2
        print(t("error_prefix", exc=exc), file=sys.stderr)
    except CompressorError as exc:
        exit_code = 1
        print(t("error_prefix", exc=exc), file=sys.stderr)
        traceback.print_exc()
    except Exception:
        exit_code = 1
        traceback.print_exc()
    finally:
        no_pause = os.environ.get(
            "JPEG_COMPRESSOR_NO_PAUSE",
            "",
        ).strip().lower() in {"1", "true", "yes"}
        if not no_pause:
            _pause_if_windows_double_click(interrupted=exit_code == 130)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
