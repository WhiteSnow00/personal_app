from __future__ import annotations

import argparse
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
import uuid
import warnings
import zlib
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from statistics import median
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
    from PIL import Image, ImageOps, features as PillowFeatures, __version__ as PILLOW_VERSION

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
        PillowFeatures = None
except ImportError:
    PILLOW_AVAILABLE = False
    PILLOW_VERSION = "not installed"
    PILLOW_IMPORT_ERROR = "Pillow 10.0+ is not installed"
    Image = None
    ImageOps = None
    PillowFeatures = None

SCRIPT_VERSION: Final[str] = "1.7.0"
SCRIPT_NAME: Final[str] = "JPEG Batch Compressor"
TARGET_SIZE_MB: Final[float] = 4.95
DEFAULT_MAX_BYTES: Final[int] = int(TARGET_SIZE_MB * 1_000_000)
DEFAULT_OUTPUT_DIRNAME: Final[str] = "compressed_output"
TEMP_WORKDIR_NAME: Final[str] = ".tmp_compress_work"
UNCONFIRMED_PROCESS_MARKER: Final[str] = ".process-unconfirmed"
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
BINARY_REFINEMENT_RADIUS: Final[int] = 2
BINARY_FAILURE_OFFSETS: Final[Tuple[int, ...]] = (-1, 1, -2, 2)
BINARY_FAILURE_ATTEMPT_BUDGET: Final[int] = 4
PNG_PALETTE_COLOR_TARGETS: Final[Tuple[int, ...]] = (
    256,
    192,
    128,
    96,
    64,
    48,
    32,
    16,
)
PNG_PALETTE_MIN_COLORS: Final[int] = 2
PNG_COMPRESS_LEVEL: Final[int] = 9
MIN_AUTOMATIC_PAGE_SIDE: Final[int] = 640
UNIFORM_CANVAS_REDUCTION_FACTOR: Final[float] = 0.95
DEFAULT_MAX_PIXELS: Final[int] = 200_000_000
FFMPEG_PROCESS_THREADS: Final[int] = 1
WEBP_SEARCH_METHOD: Final[int] = 4
WEBP_FINAL_METHOD: Final[int] = 6
STALE_MARKER_AGE_SEC: Final[float] = 24 * 60 * 60
SUBPROCESS_COLLECT_TIMEOUT_SEC: Final[float] = 2.0
WINDOWS_PATH_BUDGET: Final[int] = 240
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
        "already_under": "Already below limit",
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
        "plain_savings": "Est. savings: ~{lo:.1f}–{hi:.1f} MB",
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
        "continue_yn": "Continue? [Y/n]:",
        "resize_prompt": "Choose chapter page resizing",
        "resize_all": "Resize and pad all pages to the common canvas",
        "resize_outliers": "Resize only geometry outliers and native failures",
        "resize_none": "Do not resize or pad pages",
        "page_size_auto": "Automatic page canvas: {canvas}",
        "page_size_provided": "Provided page canvas: {canvas}",
        "uniform_reduce": "Reducing the common page canvas uniformly to {canvas}",
        "outlier_resize_failed": "Some outlier or retry pages still failed at the common canvas.",
        "resize_requires_pillow": "Pillow 10.0+ is required for page resizing and padding.",
        "resize_switch_all": "Switch to resize-all mode with uniform canvas reduction?",
        "canvas_unavailable": "A stable automatic page canvas could not be derived. Provide --page-size WxH.",
        "res_resize_mode": "Resize mode",
        "res_page_canvas": "Page canvas",
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
        "col_notes": "Details",
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
        "stopped_by_user": "Program stopped by user.",
        "error_prefix": "ERROR: {exc}",
        "strat_a_title": "Maximum Fidelity / Metadata-First",
        "strat_b_title": "High-Quality Lossy (90–95%)",
        "strat_c_title": "Binary-Search Target",
        "strat_d_title": "Aggressive Adaptive (max retention)",
        "strat_e_title": "Copy Already-Compliant Only",
        "strat_a_desc": "Try the codec's highest-fidelity setting first, then a mild lossy fallback when needed. PNG uses optimized lossless followed by a deep palette search suited to line art and low-color images.",
        "strat_b_desc": "Start oversized JPEG/WEBP images at high quality, then search lower qualities when needed. PNG uses optimized lossless plus a deep palette search.",
        "strat_c_desc": "Search JPEG/WEBP quality per image for the highest-quality result below the preferred target. PNG uses codec-aware lossless and palette variants instead of a nominal quality search.",
        "strat_d_desc": "Predictive codec-aware pipeline that skips unnecessary high-fidelity attempts when substantial reduction is required.",
        "strat_e_desc": "Only copy files already under {target_mb} MB into the output folder. Oversize images are skipped (listed in the report). Useful for dry-run style triage.",
        "msg_already_copied": "already under limit — copied or sanitized",
        "msg_already_not_copied": "already under limit (not copied)",
        "msg_copy_only_skip": "over limit; copy-only strategy skips compression",
        "msg_dry_run": "dry-run: no output written",
        "msg_compressed": "compressed to {size} (q={q}, scale={scale:.2f})",
        "msg_size_fail": "could not get under {limit} (best={best})",
        "msg_unable": "unable to reach <{limit}; best effort was {best} at q={q} scale={scale:.2f}",
        "status_pending": "Pending",
        "status_skipped_under": "Skipped",
        "status_skipped_unsupported": "Unsupported",
        "status_skipped_corrupt": "Unreadable",
        "status_dry_run": "Dry run",
        "status_copied": "Copied",
        "status_sanitized": "Sanitized",
        "status_compressed": "Compressed",
        "status_failed": "Failed",
        "status_size_failed": "Size limit",
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
        "already_under": "Đã dưới giới hạn",
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
        "plain_savings": "Ước tiết kiệm: ~{lo:.1f}–{hi:.1f} MB",
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
        "continue_yn": "Tiếp tục? [Y/n]:",
        "resize_prompt": "Chọn cách đổi kích thước trang",
        "resize_all": "Đổi và đệm tất cả trang theo khung chung",
        "resize_outliers": "Chỉ đổi trang lệch chuẩn và trang lỗi ở kích thước gốc",
        "resize_none": "Không đổi kích thước hoặc đệm trang",
        "page_size_auto": "Khung trang tự động: {canvas}",
        "page_size_provided": "Khung trang đã chọn: {canvas}",
        "uniform_reduce": "Giảm đồng đều khung trang chung xuống {canvas}",
        "outlier_resize_failed": "Một số trang lệch chuẩn hoặc thử lại vẫn lỗi ở khung chung.",
        "resize_requires_pillow": "Cần Pillow 10.0+ để đổi kích thước và đệm trang.",
        "resize_switch_all": "Chuyển sang đổi tất cả trang và giảm khung đồng đều?",
        "canvas_unavailable": "Không xác định được khung trang tự động ổn định. Hãy dùng --page-size WxH.",
        "res_resize_mode": "Chế độ đổi kích thước",
        "res_page_canvas": "Khung trang",
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
        "col_notes": "Chi tiết",
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
        "stopped_by_user": "Chương trình đã dừng.",
        "error_prefix": "ERROR: {exc}",
        "strat_a_title": "Maximum Fidelity / Metadata-First",
        "strat_b_title": "High-Quality Lossy (90–95%)",
        "strat_c_title": "Binary-Search Target",
        "strat_d_title": "Aggressive Adaptive (giữ chất lượng tối đa)",
        "strat_e_title": "Chỉ copy file đã đạt chuẩn",
        "strat_a_desc": "Thử mức fidelity cao nhất trước, rồi dùng lossy nhẹ khi cần. PNG dùng lossless tối ưu và tìm palette sâu cho line art hoặc ảnh ít màu.",
        "strat_b_desc": "Bắt đầu JPEG/WEBP vượt dung lượng ở quality cao, rồi tìm mức thấp hơn khi cần. PNG dùng lossless tối ưu và tìm palette sâu.",
        "strat_c_desc": "Tìm quality JPEG/WEBP cao nhất dưới preferred target. PNG dùng lossless và palette variant đúng theo codec thay vì search quality danh nghĩa.",
        "strat_d_desc": "Pipeline dự đoán theo codec, bỏ qua thử nghiệm fidelity cao không cần thiết khi phải giảm nhiều.",
        "strat_e_desc": "Chỉ copy các file đã dưới {target_mb} MB vào output. Image vượt size bị skip (ghi trong report). Hữu ích khi triage kiểu dry-run.",
        "msg_already_copied": "đã dưới limit — đã copy hoặc làm sạch metadata",
        "msg_already_not_copied": "đã dưới limit (không copy)",
        "msg_copy_only_skip": "vượt limit; strategy copy-only bỏ qua nén",
        "msg_dry_run": "dry-run: không ghi output",
        "msg_compressed": "đã nén còn {size} (q={q}, scale={scale:.2f})",
        "msg_size_fail": "không xuống dưới {limit} (best={best})",
        "msg_unable": "không đạt <{limit}; best effort {best} tại q={q} scale={scale:.2f}",
        "status_pending": "Đang chờ",
        "status_skipped_under": "Đã bỏ qua",
        "status_skipped_unsupported": "Không hỗ trợ",
        "status_skipped_corrupt": "Không đọc được",
        "status_dry_run": "Dry run",
        "status_copied": "Đã sao chép",
        "status_sanitized": "Đã làm sạch",
        "status_compressed": "Đã nén",
        "status_failed": "Thất bại",
        "status_size_failed": "Vượt dung lượng",
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


def status_label(status: "ImageStatus") -> str:
    return t(
        {
            ImageStatus.PENDING: "status_pending",
            ImageStatus.SKIPPED_UNDER_LIMIT: "status_skipped_under",
            ImageStatus.SKIPPED_UNSUPPORTED: "status_skipped_unsupported",
            ImageStatus.SKIPPED_CORRUPT: "status_skipped_corrupt",
            ImageStatus.DRY_RUN: "status_dry_run",
            ImageStatus.COPIED: "status_copied",
            ImageStatus.SANITIZED: "status_sanitized",
            ImageStatus.COMPRESSED: "status_compressed",
            ImageStatus.FAILED: "status_failed",
            ImageStatus.SIZE_LIMIT_FAILED: "status_size_failed",
        }[status]
    )


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
    __slots__ = ()


class ProbeError(CompressorError):
    __slots__ = ()


class ImageSafetyError(ProbeError):
    __slots__ = ()


class EncodeError(CompressorError):
    __slots__ = ()


class ProcessTerminationError(CompressorError):

    def __init__(
        self,
        message: str,
        *,
        result: Optional["ImageJobResult"] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.result = result


class UserAbortError(CompressorError):
    __slots__ = ()


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
    __slots__ = ()


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


class WebPVariant(Enum):
    LOSSY = "lossy"
    LOSSLESS = "lossless"


class ResizeMode(Enum):
    NONE = "none"
    ALL = "all"
    OUTLIERS = "outliers"


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


def png_palette_quality(colors: int) -> int:
    return clamp(int(round(clamp(colors, 2, 256) / 256 * 94)), 1, 94)


def quality_to_webp_params(
    quality: int,
    variant: WebPVariant = WebPVariant.LOSSY,
) -> Tuple[int, bool]:
    q = clamp(int(quality), 1, 100)
    return (100, True) if variant == WebPVariant.LOSSLESS else (q, False)


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

    def reduced(self, factor: float) -> "ImageDimensions":
        if not 0.0 < factor < 1.0:
            raise ValueError("factor must be between zero and one")
        return ImageDimensions(
            even_dimension(self.width * factor),
            even_dimension(self.height * factor),
        )

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
    icc_profile_sha256: Optional[str] = None
    has_alpha: bool = False
    exif_orientation: int = 1
    probe_error: Optional[str] = None
    format_name: Optional[str] = None
    duration: Optional[float] = None
    processing_path: Optional[Path] = None
    processing_dimensions: Optional[ImageDimensions] = None
    resized: bool = False
    padded: bool = False
    canvas_scale: float = 1.0

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
    webp_method: Optional[int] = None
    png_colors: Optional[int] = None
    transient_failure: bool = False
    verification: Optional["OutputVerification"] = None

    def is_acceptable(self, policy: SizePolicy = DEFAULT_SIZE_POLICY) -> bool:
        return self.success and policy.is_acceptable(self.output_bytes)

    def is_preferred(self, policy: SizePolicy = DEFAULT_SIZE_POLICY) -> bool:
        return self.success and policy.is_preferred(self.output_bytes)


@dataclass(frozen=True, slots=True)
class OutputVerification:
    valid: bool
    codec: Optional[ImageCodec]
    dimensions: Optional[ImageDimensions]
    has_alpha: Optional[bool]
    has_icc_profile: Optional[bool]
    size_bytes: int
    decoder: str
    error: Optional[str] = None
    icc_profile_sha256: Optional[str] = None
    structurally_valid: Optional[bool] = None
    size_acceptable: Optional[bool] = None

    def __post_init__(self) -> None:
        if self.structurally_valid is None:
            object.__setattr__(self, "structurally_valid", self.valid)
        if self.size_acceptable is None:
            object.__setattr__(self, "size_acceptable", self.valid)


@dataclass(slots=True)
class ImageJobResult:
    source: Path
    status: ImageStatus
    output_path: Optional[Path] = None
    original_bytes: int = 0
    output_bytes: int = 0
    original_dimensions: Optional[ImageDimensions] = None
    output_dimensions: Optional[ImageDimensions] = None
    target_codec: Optional[ImageCodec] = None
    source_has_alpha: bool = False
    source_has_icc_profile: bool = False
    source_icc_profile_sha256: Optional[str] = None
    backend: EncodeBackend = EncodeBackend.NONE
    quality_used: Optional[int] = None
    ffmpeg_q_used: Optional[int] = None
    scale_factor: float = 1.0
    attempts: List[CompressionAttempt] = field(default_factory=list)
    message: str = ""
    elapsed_sec: float = 0.0
    error_detail: Optional[str] = None
    variant: Optional[str] = None
    webp_method: Optional[int] = None
    png_colors: Optional[int] = None
    verification: Optional[OutputVerification] = None
    resized: bool = False
    padded: bool = False
    work_dir: Optional[Path] = None
    attempt_cache: Dict[Tuple[Any, ...], CompressionAttempt] = field(
        default_factory=dict,
        repr=False,
    )
    transient_retries: Dict[Tuple[Any, ...], int] = field(
        default_factory=dict,
        repr=False,
    )
    attempted_keys: set[Tuple[Any, ...]] = field(
        default_factory=set,
        repr=False,
    )

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


@dataclass(slots=True)
class RuntimeConfig:
    root_dir: Path
    output_dir: Path
    size_policy: SizePolicy = field(default_factory=SizePolicy)
    strategy: CompressionStrategy = CompressionStrategy.AGGRESSIVE_ADAPTIVE
    max_workers: int = DEFAULT_MAX_WORKERS
    max_pixels: int = DEFAULT_MAX_PIXELS
    resize_mode: ResizeMode = ResizeMode.NONE
    page_canvas_source: Optional[str] = None
    page_canvas: Optional[ImageDimensions] = None
    initial_page_canvas: Optional[ImageDimensions] = None
    minimum_page_side: int = MIN_AUTOMATIC_PAGE_SIDE
    uniform_reduction_applied: bool = False
    allow_upscale: bool = False
    canvas_paths: frozenset[Path] = field(default_factory=frozenset)
    reprocess_existing: bool = False
    recursive: bool = False
    copy_under_limit: bool = True
    overwrite_output: bool = False
    keep_temp_on_failure: bool = False
    dry_run: bool = False
    include_convertibles: bool = True
    progressive_jpeg: bool = True
    strip_metadata: bool = True
    preserve_icc_profile: bool = True
    output_format: OutputFormatChoice = OutputFormatChoice.JPG
    runtime_metadata: Dict[str, Any] = field(default_factory=dict)
    cancellation: CancellationToken = field(default_factory=CancellationToken)

    @property
    def max_bytes(self) -> int:
        return self.size_policy.strict_max_bytes

    @property
    def effective_target_bytes(self) -> int:
        return self.size_policy.preferred_target_bytes

    def __post_init__(self) -> None:
        if self.max_pixels <= 0:
            raise ValueError("max_pixels must be positive")
        if self.minimum_page_side <= 0:
            raise ValueError("minimum page side must be positive")
        if self.resize_mode != ResizeMode.NONE and self.page_canvas is None:
            raise ValueError("resize mode requires a page canvas")
        if self.page_canvas is not None:
            if self.page_canvas.width <= 0 or self.page_canvas.height <= 0:
                raise ValueError("page canvas dimensions must be positive")
            if self.page_canvas.width % 2 or self.page_canvas.height % 2:
                raise ValueError("page canvas dimensions must be even")


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
            "runtime": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "pillow": PILLOW_VERSION,
                "rich": RICH_AVAILABLE,
                "max_pixels": self.config.max_pixels,
                "recursive": self.config.recursive,
                "resize_mode": self.config.resize_mode.value,
                "page_canvas_source": self.config.page_canvas_source,
                "initial_page_canvas": (
                    {
                        "width": self.config.initial_page_canvas.width,
                        "height": self.config.initial_page_canvas.height,
                    }
                    if self.config.initial_page_canvas
                    else None
                ),
                "final_page_canvas": (
                    {
                        "width": self.config.page_canvas.width,
                        "height": self.config.page_canvas.height,
                    }
                    if self.config.page_canvas
                    else None
                ),
                "uniform_reduction_applied": self.config.uniform_reduction_applied,
                "allow_upscale": self.config.allow_upscale,
                "ffmpeg_process_threads": FFMPEG_PROCESS_THREADS,
                "progressive_jpeg": self.config.progressive_jpeg,
                "strip_metadata": self.config.strip_metadata,
                "preserve_icc_profile": self.config.preserve_icc_profile,
                **self.config.runtime_metadata,
            },
            "preflight": {
                "total_files_scanned": self.preflight.total_files_scanned,
                "jpeg_count": self.preflight.jpeg_count,
                "convertible_count": self.preflight.convertible_count,
                "under_limit_count": self.preflight.under_limit_count,
                "over_limit_count": self.preflight.over_limit_count,
                "corrupt_count": self.preflight.corrupt_count,
                "total_bytes": self.preflight.total_bytes,
                "over_limit_bytes": self.preflight.over_limit_bytes,
                "scan_elapsed_sec": round(self.preflight.scan_elapsed_sec, 3),
                "dimension_stats": self.preflight.dimension_stats,
                "potential_histogram": self.preflight.potential_histogram,
            },
            "summary": {
                "total_jobs": len(self.results),
                "compressed": self.compressed_count,
                "copied": self.copied_count,
                "failed": self.failed_count,
                "total_saved_bytes": self.total_saved_bytes,
                "total_saved_mb": round(self.total_saved_bytes / 1_000_000, 3),
            },
            "results": [
                {
                    "source": str(r.source),
                    "work_dir": str(r.work_dir) if r.work_dir else None,
                    "source_icc_profile_sha256": r.source_icc_profile_sha256,
                    "status": r.status.name,
                    "status_label": status_label(r.status),
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
                    "webp_method": r.webp_method,
                    "png_colors": r.png_colors,
                    "message": r.message,
                    "elapsed_sec": round(r.elapsed_sec, 3),
                    "error": r.error_detail,
                    "resized": r.resized,
                    "padded": r.padded,
                    "original_dimensions": (
                        {
                            "width": r.original_dimensions.width,
                            "height": r.original_dimensions.height,
                        }
                        if r.original_dimensions
                        else None
                    ),
                    "output_dimensions": (
                        {
                            "width": r.output_dimensions.width,
                            "height": r.output_dimensions.height,
                        }
                        if r.output_dimensions
                        else None
                    ),
                    "verification": (
                        {
                            "valid": r.verification.valid,
                            "codec": (
                                r.verification.codec.value
                                if r.verification.codec
                                else None
                            ),
                            "dimensions": (
                                {
                                    "width": r.verification.dimensions.width,
                                    "height": r.verification.dimensions.height,
                                }
                                if r.verification.dimensions
                                else None
                            ),
                            "has_alpha": r.verification.has_alpha,
                            "has_icc_profile": r.verification.has_icc_profile,
                            "icc_profile_sha256": r.verification.icc_profile_sha256,
                            "structurally_valid": r.verification.structurally_valid,
                            "size_acceptable": r.verification.size_acceptable,
                            "size_bytes": r.verification.size_bytes,
                            "decoder": r.verification.decoder,
                            "error": r.verification.error,
                        }
                        if r.verification
                        else None
                    ),
                    "attempts": len(r.attempts),
                }
                for r in self.results
            ],
        }


def human_bytes(num: Union[int, float], *, binary: bool = False) -> str:
    if num is None:
        return "n/a"
    n = float(num)
    if n < 0:
        return f"-{human_bytes(-n, binary=binary)}"
    unit_step = 1024.0 if binary else 1000.0
    units = (
        ("B", "KiB", "MiB", "GiB", "TiB")
        if binary
        else ("B", "KB", "MB", "GB", "TB")
    )
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


def even_dimension(value: Union[int, float]) -> int:
    rounded = max(2, int(round(float(value))))
    return rounded if rounded % 2 == 0 else rounded + 1


def parse_page_size(value: str) -> ImageDimensions:
    match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", value)
    if match is None:
        raise argparse.ArgumentTypeError("page size must use WxH, for example 1600x2400")
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("page size dimensions must be positive")
    return ImageDimensions(even_dimension(width), even_dimension(height))


@dataclass(frozen=True, slots=True)
class PageCanvasPlan:
    canvas: ImageDimensions
    source: str
    outliers: frozenset[Path]


class PageCanvasPlanner:

    @staticmethod
    def create(
        images: Sequence[ImageProbeResult],
        supplied: Optional[ImageDimensions] = None,
    ) -> PageCanvasPlan:
        readable = [
            image
            for image in images
            if image.is_readable and image.dimensions is not None
        ]
        if not readable:
            raise CompressorError("no readable image dimensions are available for page sizing")
        if supplied is not None:
            canvas = ImageDimensions(
                even_dimension(supplied.width),
                even_dimension(supplied.height),
            )
            return PageCanvasPlan(
                canvas,
                "user-provided",
                PageCanvasPlanner._find_outliers(readable, canvas, exact=True),
            )
        counts = Counter(image.dimensions for image in readable)
        modal_dimensions, modal_count = counts.most_common(1)[0]
        use_modal = modal_count * 2 >= len(readable)
        if use_modal:
            canvas = ImageDimensions(
                even_dimension(modal_dimensions.width),
                even_dimension(modal_dimensions.height),
            )
        else:
            canvas = ImageDimensions(
                even_dimension(median(image.dimensions.width for image in readable)),
                even_dimension(median(image.dimensions.height for image in readable)),
            )
        if min(canvas.width, canvas.height) < MIN_AUTOMATIC_PAGE_SIDE:
            raise CompressorError(
                f"automatic page canvas {canvas} has a side below {MIN_AUTOMATIC_PAGE_SIDE} pixels"
            )
        return PageCanvasPlan(
            canvas,
            "automatic",
            PageCanvasPlanner._find_outliers(readable, canvas, exact=use_modal),
        )

    @staticmethod
    def _find_outliers(
        images: Sequence[ImageProbeResult],
        canvas: ImageDimensions,
        *,
        exact: bool,
    ) -> frozenset[Path]:
        outliers: set[Path] = set()
        canvas_ratio = canvas.aspect_ratio
        for image in images:
            dimensions = image.dimensions
            if dimensions is None:
                continue
            if exact:
                is_outlier = dimensions != canvas
            else:
                width_delta = abs(dimensions.width - canvas.width) / canvas.width
                height_delta = abs(dimensions.height - canvas.height) / canvas.height
                ratio_delta = abs(dimensions.aspect_ratio - canvas_ratio) / canvas_ratio
                orientation_differs = (dimensions.width > dimensions.height) != (
                    canvas.width > canvas.height
                )
                is_outlier = (
                    orientation_differs
                    or width_delta > 0.08
                    or height_delta > 0.08
                    or ratio_delta > 0.04
                )
            if is_outlier:
                outliers.add(image.path)
        return frozenset(outliers)

    @staticmethod
    def reduce_uniformly(
        canvas: ImageDimensions,
        minimum_side: int,
    ) -> Optional[ImageDimensions]:
        shortest = min(canvas.width, canvas.height)
        if shortest <= minimum_side:
            return None
        factor = max(
            UNIFORM_CANVAS_REDUCTION_FACTOR,
            minimum_side / float(shortest),
        )
        reduced = canvas.reduced(factor)
        if min(reduced.width, reduced.height) < minimum_side:
            scale = minimum_side / float(min(reduced.width, reduced.height))
            reduced = ImageDimensions(
                even_dimension(reduced.width * scale),
                even_dimension(reduced.height * scale),
            )
        return None if reduced == canvas else reduced


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


def ensure_path_budget(path: Path, *, label: str = "Path") -> None:
    if os.name != "nt":
        return
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    if len(str(resolved)) > WINDOWS_PATH_BUDGET:
        raise WorkspaceError(
            f"{label} exceeds the Windows path budget: {resolved}"
        )


def atomic_replace(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(src), str(dest))


def atomic_publish(src: Path, dest: Path, *, overwrite: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite:
        fd: Optional[int] = None
        claimed = False
        try:
            fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            claimed = True
            os.close(fd)
            fd = None
            os.replace(str(src), str(dest))
        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if claimed:
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
            try:
                os.close(fd)
            except OSError:
                pass


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


@dataclass(frozen=True, slots=True)
class JPEGSanitizeResult:
    success: bool
    changed: bool
    preserved_icc: bool
    error: Optional[str] = None


class JPEGMetadataSanitizer:
    _STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})
    _PRESERVED_APP = frozenset({0xE0, 0xEE})
    _ICC_SIGNATURE = b"ICC_PROFILE\x00"

    @classmethod
    def sanitize(
        cls,
        source: Path,
        destination: Path,
        *,
        orientation: int = 1,
        preserve_icc: bool = True,
    ) -> JPEGSanitizeResult:
        if orientation not in (0, 1):
            return JPEGSanitizeResult(
                success=False,
                changed=False,
                preserved_icc=False,
                error="non_default_orientation",
            )
        try:
            data = source.read_bytes()
            output, changed, preserved_icc = cls.sanitize_bytes(
                data,
                preserve_icc=preserve_icc,
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(output)
            return JPEGSanitizeResult(
                success=True,
                changed=changed,
                preserved_icc=preserved_icc,
            )
        except (OSError, ValueError) as exc:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            return JPEGSanitizeResult(
                success=False,
                changed=False,
                preserved_icc=False,
                error=str(exc),
            )

    @classmethod
    def sanitize_bytes(
        cls,
        data: bytes,
        *,
        preserve_icc: bool = True,
    ) -> Tuple[bytes, bool, bool]:
        if len(data) < 4 or data[:2] != b"\xff\xd8":
            raise ValueError("invalid_jpeg_soi")
        output = bytearray(data[:2])
        offset = 2
        changed = False
        preserved_icc = False
        saw_scan = False
        while offset < len(data):
            marker_start = offset
            marker, offset = cls._read_marker(data, offset)
            if marker == 0xD9:
                if not saw_scan:
                    raise ValueError("jpeg_missing_scan")
                output.extend(data[marker_start:offset])
                if offset != len(data):
                    raise ValueError("jpeg_trailing_data")
                return bytes(output), changed, preserved_icc
            if marker in cls._STANDALONE:
                output.extend(data[marker_start:offset])
                continue
            segment_end, payload = cls._read_segment(data, offset)
            if marker == 0xDA:
                saw_scan = True
                output.extend(data[marker_start:segment_end])
                scan_end = cls._scan_end(data, segment_end)
                output.extend(data[segment_end:scan_end])
                offset = scan_end
                continue
            keep = marker < 0xE0 or marker in cls._PRESERVED_APP
            if marker == 0xE2 and payload.startswith(cls._ICC_SIGNATURE):
                keep = preserve_icc
                preserved_icc = preserve_icc
            if keep:
                output.extend(data[marker_start:segment_end])
            else:
                changed = True
            offset = segment_end
        raise ValueError("jpeg_missing_eoi")

    @staticmethod
    def _read_marker(data: bytes, offset: int) -> Tuple[int, int]:
        if offset >= len(data) or data[offset] != 0xFF:
            raise ValueError("invalid_jpeg_marker")
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            raise ValueError("truncated_jpeg_marker")
        marker = data[offset]
        if marker == 0x00:
            raise ValueError("unexpected_jpeg_stuffing")
        return marker, offset + 1

    @staticmethod
    def _read_segment(data: bytes, offset: int) -> Tuple[int, bytes]:
        if offset + 2 > len(data):
            raise ValueError("truncated_jpeg_segment")
        segment_length = int.from_bytes(data[offset : offset + 2], "big")
        segment_end = offset + segment_length
        if segment_length < 2 or segment_end > len(data):
            raise ValueError("invalid_jpeg_segment_length")
        return segment_end, data[offset + 2 : segment_end]

    @staticmethod
    def _scan_end(data: bytes, offset: int) -> int:
        while offset < len(data):
            marker = data.find(b"\xff", offset)
            if marker < 0:
                raise ValueError("truncated_jpeg_scan")
            if marker + 1 >= len(data):
                raise ValueError("truncated_jpeg_scan")
            code_offset = marker + 1
            while code_offset < len(data) and data[code_offset] == 0xFF:
                code_offset += 1
            if code_offset >= len(data):
                raise ValueError("truncated_jpeg_scan")
            code = data[code_offset]
            if code == 0x00 or 0xD0 <= code <= 0xD7:
                offset = code_offset + 1
                continue
            return marker
        raise ValueError("truncated_jpeg_scan")


def _container_icc_profile_state(
    path: Path,
) -> Tuple[bool, Optional[bytes]]:
    present = False
    try:
        with path.open("rb") as handle:
            signature = handle.read(12)
            if signature.startswith(b"\xff\xd8"):
                chunks: Dict[int, bytes] = {}
                chunk_count: Optional[int] = None
                handle.seek(2)
                while True:
                    prefix = handle.read(1)
                    if prefix != b"\xff":
                        return present, None
                    marker_byte = handle.read(1)
                    while marker_byte == b"\xff":
                        marker_byte = handle.read(1)
                    if not marker_byte:
                        return present, None
                    marker = marker_byte[0]
                    if marker in (0xDA, 0xD9):
                        break
                    if marker in JPEGMetadataSanitizer._STANDALONE:
                        continue
                    length_bytes = handle.read(2)
                    if len(length_bytes) != 2:
                        return present, None
                    length = int.from_bytes(length_bytes, "big")
                    if length < 2:
                        return present, None
                    payload = handle.read(length - 2)
                    if len(payload) != length - 2:
                        return present, None
                    if marker != 0xE2 or not payload.startswith(
                        JPEGMetadataSanitizer._ICC_SIGNATURE
                    ):
                        continue
                    present = True
                    header_size = len(JPEGMetadataSanitizer._ICC_SIGNATURE) + 2
                    if len(payload) < header_size:
                        return True, None
                    sequence = payload[len(JPEGMetadataSanitizer._ICC_SIGNATURE)]
                    count = payload[len(JPEGMetadataSanitizer._ICC_SIGNATURE) + 1]
                    if sequence <= 0 or count <= 0 or sequence > count:
                        return True, None
                    if chunk_count is not None and chunk_count != count:
                        return True, None
                    chunk_count = count
                    if sequence in chunks:
                        return True, None
                    chunks[sequence] = payload[header_size:]
                if not present:
                    return False, None
                if chunk_count is None or set(chunks) != set(
                    range(1, chunk_count + 1)
                ):
                    return True, None
                profile = b"".join(
                    chunks[index] for index in range(1, chunk_count + 1)
                )
                return True, profile or None
            if signature[:8] == b"\x89PNG\r\n\x1a\n":
                handle.seek(8)
                while True:
                    header = handle.read(8)
                    if len(header) != 8:
                        return present, None
                    length = int.from_bytes(header[:4], "big")
                    chunk = header[4:]
                    payload = handle.read(length)
                    crc = handle.read(4)
                    if len(payload) != length or len(crc) != 4:
                        return present, None
                    expected_crc = zlib.crc32(chunk + payload) & 0xFFFFFFFF
                    if int.from_bytes(crc, "big") != expected_crc:
                        if chunk == b"iCCP":
                            return True, None
                        return present, None
                    if chunk == b"iCCP":
                        present = True
                        separator = payload.find(b"\x00")
                        if separator < 1 or separator + 2 > len(payload):
                            return True, None
                        if payload[separator + 1] != 0:
                            return True, None
                        try:
                            profile = zlib.decompress(
                                payload[separator + 2 :]
                            )
                        except zlib.error:
                            return True, None
                        return True, profile or None
                    if chunk == b"IEND":
                        return False, None
            if signature[:4] == b"RIFF" and signature[8:12] == b"WEBP":
                riff_size = int.from_bytes(signature[4:8], "little")
                remaining = riff_size - 4
                if remaining < 0:
                    return False, None
                while remaining > 0:
                    if remaining < 8:
                        return present, None
                    header = handle.read(8)
                    if len(header) != 8:
                        return present, None
                    remaining -= 8
                    chunk = header[:4]
                    length = int.from_bytes(header[4:], "little")
                    padded_length = length + (length & 1)
                    if padded_length > remaining:
                        return present, None
                    payload = handle.read(length)
                    if len(payload) != length:
                        return present, None
                    if length & 1 and len(handle.read(1)) != 1:
                        return present, None
                    remaining -= padded_length
                    if chunk == b"ICCP":
                        return True, payload or None
                return present, None
    except OSError:
        return False, None
    return False, None


def container_icc_profile(path: Path) -> Optional[bytes]:
    return _container_icc_profile_state(path)[1]


def icc_profile_sha256(profile: Optional[bytes]) -> Optional[str]:
    return hashlib.sha256(profile).hexdigest() if profile else None


def container_icc_profile_sha256(path: Path) -> Optional[str]:
    return icc_profile_sha256(container_icc_profile(path))


def container_has_icc_profile(path: Path) -> bool:
    return _container_icc_profile_state(path)[0]


def unique_temp_path(directory: Path, suffix: str = ".jpg", *, label: str = "tmp") -> Path:
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    name = (
        f"{safe_filename(label, max_length=24)}_"
        f"{uuid.uuid4().hex[:16]}{suffix}"
    )
    path = directory / name
    ensure_path_budget(path, label="Temporary path")
    return path


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
    original_bytes: int,
    quality: int,
    *,
    scale_factor: float = 1.0,
    strip_metadata: bool = True,
    codec: ImageCodec = ImageCodec.JPG,
) -> int:
    q = clamp(quality, 1, 100) / 100.0
    scale_area = max(0.05, scale_factor**2)
    if codec == ImageCodec.PNG:
        if quality >= QUALITY_LOSSLESS_PROXY:
            codec_factor = 0.97
        else:
            codec_factor = 0.35 + 0.65 * q
        meta_factor = 0.97 if strip_metadata else 1.0
    elif codec == ImageCodec.WEBP:
        codec_factor = 0.18 + 0.82 * q**1.25
        meta_factor = 0.985 if strip_metadata else 1.0
    else:
        codec_factor = 0.22 + 0.78 * q**1.35
        meta_factor = 0.985 if strip_metadata else 1.0
    est = int(original_bytes * codec_factor * scale_area * meta_factor)
    return max(1024, est)


_PILLOW_CONFIGURED_MAX_PIXELS: Optional[int] = None


def configure_pillow_pixel_limit(max_pixels: int) -> None:
    if max_pixels <= 0:
        raise ValueError("max_pixels must be positive")
    if not PILLOW_AVAILABLE or Image is None:
        return
    global _PILLOW_CONFIGURED_MAX_PIXELS
    if _PILLOW_CONFIGURED_MAX_PIXELS is None:
        Image.MAX_IMAGE_PIXELS = max_pixels
        _PILLOW_CONFIGURED_MAX_PIXELS = max_pixels
    elif _PILLOW_CONFIGURED_MAX_PIXELS != max_pixels:
        raise CompressorError("Pillow pixel limit was already configured")


def pillow_open(path: Path, max_pixels: int) -> Any:
    if not PILLOW_AVAILABLE or Image is None:
        raise CompressorError("Pillow is not available")
    if _PILLOW_CONFIGURED_MAX_PIXELS != max_pixels:
        configure_pillow_pixel_limit(max_pixels)
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        return Image.open(path)


def enforce_image_pixel_limit(
    image: Any,
    max_pixels: int,
    message: str,
) -> None:
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > max_pixels:
        raise ImageSafetyError(message)


class CanvasPreparer:

    def __init__(self, config: RuntimeConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger

    def prepare(
        self,
        probe: ImageProbeResult,
        work_dir: Path,
    ) -> ImageProbeResult:
        if not probe.is_readable:
            return probe
        canvas = self.config.page_canvas
        if canvas is None:
            return probe
        should_prepare = self.config.resize_mode == ResizeMode.ALL or (
            self.config.resize_mode == ResizeMode.OUTLIERS
            and probe.path in self.config.canvas_paths
        )
        if not should_prepare:
            return probe
        if not PILLOW_AVAILABLE or Image is None:
            raise EncodeError(t("resize_requires_pillow"))
        self.config.cancellation.raise_if_cancelled()
        destination = unique_temp_path(work_dir, ".png", label="canvas")
        target_codec = resolve_output_codec(probe.path, self.config.output_format)
        with pillow_open(probe.path, self.config.max_pixels) as source_image:
            enforce_image_pixel_limit(
                source_image,
                self.config.max_pixels,
                "source image exceeds pixel limit",
            )
            source_info = dict(source_image.info or {})
            source_image.load()
            oriented = (
                ImageOps.exif_transpose(source_image)
                if ImageOps is not None
                else source_image.copy()
            )
            source_dimensions = ImageDimensions(oriented.width, oriented.height)
            fit_scale = min(
                canvas.width / oriented.width,
                canvas.height / oriented.height,
            )
            if not self.config.allow_upscale:
                fit_scale = min(1.0, fit_scale)
            fitted_width = max(1, min(canvas.width, int(round(oriented.width * fit_scale))))
            fitted_height = max(1, min(canvas.height, int(round(oriented.height * fit_scale))))
            fitted_size = (fitted_width, fitted_height)
            transformation_required = fitted_size != oriented.size or fitted_size != (
                canvas.width,
                canvas.height,
            )
            if transformation_required and (probe.bit_depth or 0) > 8:
                raise EncodeError("high-bit-depth images cannot be safely resized or padded")
            prepared = PillowEncoder._prepare_mode(oriented, target_codec)
            if fitted_size != prepared.size:
                prepared = prepared.resize(
                    fitted_size,
                    resample=Image.Resampling.LANCZOS,
                )
            padded = fitted_size != (canvas.width, canvas.height)
            if padded:
                if target_codec == ImageCodec.JPG:
                    background = Image.new("RGB", (canvas.width, canvas.height), (255, 255, 255))
                elif probe.has_alpha:
                    prepared = prepared.convert("RGBA")
                    background = Image.new("RGBA", (canvas.width, canvas.height), (0, 0, 0, 0))
                else:
                    background = Image.new(prepared.mode, (canvas.width, canvas.height), 255)
                offset = (
                    (canvas.width - fitted_width) // 2,
                    (canvas.height - fitted_height) // 2,
                )
                if target_codec == ImageCodec.JPG:
                    background.paste(prepared, offset)
                elif probe.has_alpha and "A" in prepared.mode:
                    background.paste(prepared, offset)
                else:
                    background.paste(prepared, offset)
                prepared = background
            save_kwargs: Dict[str, Any] = {
                "format": "PNG",
                "compress_level": 1,
            }
            if self.config.preserve_icc_profile and probe.has_icc_profile:
                icc_profile = source_info.get("icc_profile") or container_icc_profile(probe.path)
                if not isinstance(icc_profile, bytes):
                    raise EncodeError("ICC profile identity is unavailable during resize")
                if icc_profile_sha256(icc_profile) != probe.icc_profile_sha256:
                    raise EncodeError("ICC profile identity changed during resize preparation")
                save_kwargs["icc_profile"] = icc_profile
            if not self.config.strip_metadata:
                source_exif = source_info.get("exif")
                if source_exif:
                    cleaned_exif = PillowEncoder._exif_without_orientation(source_exif)
                    if cleaned_exif:
                        save_kwargs["exif"] = cleaned_exif
            self.config.cancellation.raise_if_cancelled()
            prepared.save(destination, **save_kwargs)
            prepared_mode = prepared.mode
        if not destination.is_file() or file_size(destination) <= 0:
            raise EncodeError("page canvas preparation failed")
        resized = fitted_size != (source_dimensions.width, source_dimensions.height)
        self.logger.info(
            "Prepared page canvas %s for %s",
            canvas,
            probe.path.name,
        )
        return replace(
            probe,
            processing_path=destination,
            processing_dimensions=canvas,
            resized=resized,
            padded=padded,
            canvas_scale=fit_scale,
            pixel_format=prepared_mode,
            exif_orientation=1,
            has_alpha=probe.has_alpha and target_codec.preserves_alpha,
        )


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
            fh.setLevel(level)
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
    def probe_capabilities(
        ffmpeg: Path,
        runner: "SubprocessRunner",
    ) -> FFmpegCapabilities:
        result = runner.run(
            [str(ffmpeg), "-hide_banner", "-encoders"],
            timeout=15,
            label="ffmpeg-capabilities",
        )
        text = (result.stdout + "\n" + result.stderr).casefold()
        mjpeg = bool(re.search(r"\bmjpeg\b", text))
        png = bool(re.search(r"\bpng\b", text))
        webp = bool(re.search(r"\blibwebp\b", text))
        help_result = runner.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-h",
                "encoder=mjpeg",
            ],
            timeout=15,
            label="mjpeg-capabilities",
        )
        huffman_text = (
            help_result.stdout + "\n" + help_result.stderr
        ).casefold()
        return FFmpegCapabilities(
            mjpeg=mjpeg,
            png=png,
            webp=webp,
            optimal_huffman="huffman" in huffman_text,
        )

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
                    proc.communicate(timeout=SUBPROCESS_COLLECT_TIMEOUT_SEC)
                except (OSError, subprocess.SubprocessError):
                    pass
            return "unknown"
        except KeyboardInterrupt:
            if proc is not None:
                try:
                    proc.kill()
                    proc.communicate(timeout=SUBPROCESS_COLLECT_TIMEOUT_SEC)
                except (OSError, subprocess.SubprocessError):
                    pass
            raise
        except (OSError, subprocess.SubprocessError):
            return "unknown"


@dataclass(frozen=True, slots=True)
class FFmpegCapabilities:
    mjpeg: bool
    png: bool
    webp: bool
    optimal_huffman: bool

    def to_dict(self) -> Dict[str, bool]:
        return {
            "mjpeg": self.mjpeg,
            "png": self.png,
            "webp": self.webp,
            "optimal_huffman": self.optimal_huffman,
        }


def available_output_codecs(
    capabilities: FFmpegCapabilities,
) -> frozenset[ImageCodec]:
    codecs: set[ImageCodec] = set()
    pillow_codecs = pillow_output_codecs()
    if capabilities.mjpeg or ImageCodec.JPG in pillow_codecs:
        codecs.add(ImageCodec.JPG)
    if capabilities.png or ImageCodec.PNG in pillow_codecs:
        codecs.add(ImageCodec.PNG)
    if capabilities.webp or ImageCodec.WEBP in pillow_codecs:
        codecs.add(ImageCodec.WEBP)
    return frozenset(codecs)


def pillow_output_codecs() -> frozenset[ImageCodec]:
    if not PILLOW_AVAILABLE or Image is None:
        return frozenset()
    formats = set(Image.registered_extensions().values())
    codecs: set[ImageCodec] = set()
    if "JPEG" in formats:
        codecs.add(ImageCodec.JPG)
    if "PNG" in formats:
        codecs.add(ImageCodec.PNG)
    if (
        "WEBP" in formats
        and PillowFeatures is not None
        and PillowFeatures.check("webp")
    ):
        codecs.add(ImageCodec.WEBP)
    return frozenset(codecs)


def output_choice_available(
    choice: OutputFormatChoice,
    summary: PreflightSummary,
    codecs: frozenset[ImageCodec],
) -> bool:
    if choice == OutputFormatChoice.JPG:
        return ImageCodec.JPG in codecs and (
            ImageCodec.JPG in pillow_output_codecs()
            or not any(image.has_alpha for image in summary.images if image.is_readable)
        )
    if choice == OutputFormatChoice.PNG:
        return ImageCodec.PNG in codecs
    if choice == OutputFormatChoice.WEBP:
        return ImageCodec.WEBP in codecs
    required = {
        resolve_output_codec(image.path, choice)
        for image in summary.images
        if image.is_readable
    }
    return required.issubset(codecs)


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
                        if not self._stop_process(proc):
                            raise ProcessTerminationError(
                                f"Could not confirm process termination: pid={proc.pid}"
                            )
                        stdout, stderr = self._collect_output(proc)
                        break
                    if time.perf_counter() >= deadline:
                        timed_out = True
                        if not self._stop_process(proc):
                            raise ProcessTerminationError(
                                f"Could not confirm process termination: pid={proc.pid}"
                            )
                        stdout, stderr = self._collect_output(proc)
                        break
            if self.cancellation.cancelled:
                cancelled = True
        except BaseException as exc:
            if isinstance(exc, ProcessTerminationError):
                raise
            if proc.poll() is None and not self._stop_process(proc):
                raise ProcessTerminationError(
                    f"Could not confirm process termination after "
                    f"{type(exc).__name__}: pid={proc.pid}"
                ) from exc
            raise
        finally:
            if proc.poll() is not None:
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
            if check:
                raise EncodeError(
                    f"{label} failed (rc={result.returncode}): {(result.stderr or result.stdout)[:800]}"
                )
        return result

    @classmethod
    def _collect_output(
        cls,
        proc: subprocess.Popen[str],
    ) -> Tuple[str, str]:
        try:
            return proc.communicate(timeout=SUBPROCESS_COLLECT_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            if not cls._stop_process(proc):
                raise ProcessTerminationError(
                    f"Could not confirm process termination: pid={proc.pid}"
                )
            try:
                return proc.communicate(
                    timeout=SUBPROCESS_COLLECT_TIMEOUT_SEC
                )
            except subprocess.TimeoutExpired as exc:
                raise ProcessTerminationError(
                    f"Process output pipes remained open after termination: "
                    f"pid={proc.pid}"
                ) from exc

    @staticmethod
    def _stop_process(proc: subprocess.Popen[str]) -> bool:
        process_exited = proc.poll() is not None
        if not process_exited:
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(proc.pid, signal.SIGTERM)
            except (OSError, ValueError):
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
            process_exited = proc.poll() is not None
        if process_exited:
            return True
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
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        return proc.poll() is not None

    def cancel_all(self) -> None:
        self.cancellation.cancel()
        with self._lock:
            active = list(self._active)
        for proc in active:
            if not self._stop_process(proc):
                self.logger.error(
                    "Could not confirm process termination: pid=%s",
                    proc.pid,
                )


class ImageProber:

    def __init__(
        self,
        ffprobe: Path,
        runner: SubprocessRunner,
        *,
        max_pixels: int = DEFAULT_MAX_PIXELS,
    ) -> None:
        if max_pixels <= 0:
            raise ValueError("max_pixels must be positive")
        self.ffprobe = ffprobe
        self.runner = runner
        self.max_pixels = max_pixels

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
                    result.has_icc_profile = (
                        result.has_icc_profile
                        or pillow_result.has_icc_profile
                    )
                    result.icc_profile_sha256 = (
                        pillow_result.icc_profile_sha256
                        or result.icc_profile_sha256
                    )
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
                except (UserAbortError, ImageSafetyError):
                    raise
                except ProbeError as exc:
                    return ImageProbeResult(
                        path=path,
                        size_bytes=size,
                        dimensions=None,
                        codec_name=result.codec_name,
                        pixel_format=result.pixel_format,
                        color_space=result.color_space,
                        bit_depth=result.bit_depth,
                        is_readable=False,
                        is_jpeg=result.is_jpeg,
                        has_metadata_hint=result.has_metadata_hint,
                        has_icc_profile=(
                            result.has_icc_profile
                            or container_has_icc_profile(path)
                        ),
                        icc_profile_sha256=(
                            result.icc_profile_sha256
                            or container_icc_profile_sha256(path)
                        ),
                        has_alpha=result.has_alpha,
                        probe_error=str(exc),
                        format_name=result.format_name,
                        duration=result.duration,
                    )
            container_icc_present, container_icc = (
                _container_icc_profile_state(path)
            )
            result.has_icc_profile = (
                result.has_icc_profile or container_icc_present
            )
            result.icc_profile_sha256 = (
                result.icc_profile_sha256
                or icc_profile_sha256(container_icc)
            )
            return result
        except (UserAbortError, ProcessTerminationError):
            raise
        except ImageSafetyError as exc:
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
                has_icc_profile=container_has_icc_profile(path),
                icc_profile_sha256=container_icc_profile_sha256(path),
                probe_error=str(exc),
            )
        except (ProbeError, CompressorError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self.runner.cancellation.raise_if_cancelled()
            if PILLOW_AVAILABLE:
                try:
                    return self._probe_pillow(path, size)
                except UserAbortError:
                    raise
                except ImageSafetyError as pillow_exc:
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
                        has_icc_profile=container_has_icc_profile(path),
                        icc_profile_sha256=container_icc_profile_sha256(path),
                        probe_error=str(pillow_exc),
                    )
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
        self._enforce_pixel_limit(dims, path)
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
        if not PILLOW_AVAILABLE or Image is None:
            raise ProbeError("Pillow is not available")
        try:
            self.runner.cancellation.raise_if_cancelled()
            with pillow_open(path, self.max_pixels) as img:
                enforce_image_pixel_limit(
                    img,
                    self.max_pixels,
                    f"Image exceeds safe pixel limits: {path.name}",
                )
                img.verify()
            self.runner.cancellation.raise_if_cancelled()
            with pillow_open(path, self.max_pixels) as img:
                enforce_image_pixel_limit(
                    img,
                    self.max_pixels,
                    f"Image exceeds safe pixel limits: {path.name}",
                )
                fmt = (img.format or "").upper()
                mode = img.mode
                info = dict(img.info or {})
                try:
                    orientation = int(img.getexif().get(274, 1) or 1)
                except (AttributeError, TypeError, ValueError):
                    orientation = 1
                width, height = img.size
                if orientation in (5, 6, 7, 8):
                    width, height = height, width
            dimensions = ImageDimensions(width, height)
            self._enforce_pixel_limit(dimensions, path)
            self.runner.cancellation.raise_if_cancelled()
        except UserAbortError:
            raise
        except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise ImageSafetyError(
                f"Image exceeds safe pixel limits: {path.name}",
                cause=exc,
            ) from exc
        except Exception as exc:
            raise ProbeError(
                f"Pillow cannot open {path.name}: {exc}",
                cause=exc,
            ) from exc
        icc_profile = info.get("icc_profile")
        container_icc_present, container_icc = (
            _container_icc_profile_state(path)
        )
        return ImageProbeResult(
            path=path,
            size_bytes=size,
            dimensions=dimensions,
            codec_name=fmt.lower() if fmt else None,
            pixel_format=mode,
            color_space=None,
            bit_depth=self._pillow_bit_depth(mode),
            is_readable=True,
            is_jpeg=fmt in {"JPEG", "MPO"} or path.suffix.lower() in JPEG_EXTENSIONS,
            has_metadata_hint=bool(info.get("exif") or icc_profile),
            has_icc_profile=bool(
                icc_profile or container_icc_present
            ),
            icc_profile_sha256=icc_profile_sha256(
                icc_profile or container_icc
            ),
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

    def _enforce_pixel_limit(
        self,
        dimensions: ImageDimensions,
        path: Path,
    ) -> None:
        pixels = dimensions.width * dimensions.height
        if pixels > self.max_pixels:
            raise ImageSafetyError(
                f"Image has {pixels:,} pixels; limit is {self.max_pixels:,}: {path.name}"
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
        if any(
            marker in ffmpeg_value
            for marker in ("420", "422", "444", "rgb", "bgr", "gbr")
        ):
            return ffprobe_pixel_format
        return pillow_pixel_format or ffprobe_pixel_format


class OutputVerifier:

    def __init__(
        self,
        ffmpeg: Path,
        ffprobe: Path,
        runner: SubprocessRunner,
        *,
        max_pixels: int = DEFAULT_MAX_PIXELS,
    ) -> None:
        if max_pixels <= 0:
            raise ValueError("max_pixels must be positive")
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.runner = runner
        self.max_pixels = max_pixels

    def verify(
        self,
        path: Path,
        *,
        expected_codec: ImageCodec,
        expected_dimensions: Optional[ImageDimensions],
        require_alpha: bool,
        require_icc: bool,
        size_policy: SizePolicy,
        expected_icc_sha256: Optional[str] = None,
        enforce_size: bool = True,
    ) -> OutputVerification:
        size = file_size(path)
        size_acceptable = size_policy.is_acceptable(size)
        if PILLOW_AVAILABLE and Image is not None:
            pillow_verification = self._verify_pillow(path, size)
            if pillow_verification.valid:
                verified = pillow_verification
            else:
                ffmpeg_verification = self._verify_ffmpeg(path, size)
                if ffmpeg_verification.valid:
                    verified = ffmpeg_verification
                else:
                    verified = OutputVerification(
                        False,
                        ffmpeg_verification.codec,
                        ffmpeg_verification.dimensions,
                        ffmpeg_verification.has_alpha,
                        ffmpeg_verification.has_icc_profile,
                        size,
                        "pillow+ffmpeg",
                        (
                            f"pillow: {pillow_verification.error}; "
                            f"ffmpeg: {ffmpeg_verification.error}"
                        )[:600],
                        ffmpeg_verification.icc_profile_sha256,
                    )
        else:
            verified = self._verify_ffmpeg(path, size)
        if not verified.valid:
            return OutputVerification(
                False,
                verified.codec,
                verified.dimensions,
                verified.has_alpha,
                verified.has_icc_profile,
                size,
                verified.decoder,
                verified.error,
                verified.icc_profile_sha256,
                False,
                size_acceptable,
            )
        if verified.codec != expected_codec:
            return self._failure(
                verified,
                "unexpected_codec",
                size_acceptable=size_acceptable,
            )
        if expected_dimensions and verified.dimensions != expected_dimensions:
            return self._failure(
                verified,
                "unexpected_dimensions",
                size_acceptable=size_acceptable,
            )
        if require_alpha and verified.has_alpha is not True:
            return self._failure(
                verified,
                "alpha_not_preserved",
                size_acceptable=size_acceptable,
            )
        if expected_codec == ImageCodec.JPG and verified.has_alpha is True:
            return self._failure(
                verified,
                "jpeg_output_has_alpha",
                size_acceptable=size_acceptable,
            )
        if require_icc and expected_icc_sha256 is None:
            return self._failure(
                verified,
                "icc_identity_unavailable",
                size_acceptable=size_acceptable,
            )
        if require_icc and verified.has_icc_profile is not True:
            return self._failure(
                verified,
                "icc_not_preserved",
                size_acceptable=size_acceptable,
            )
        if require_icc and verified.icc_profile_sha256 is None:
            return self._failure(
                verified,
                "icc_identity_unavailable",
                size_acceptable=size_acceptable,
            )
        if (
            require_icc
            and verified.icc_profile_sha256 != expected_icc_sha256
        ):
            return self._failure(
                verified,
                "icc_profile_mismatch",
                size_acceptable=size_acceptable,
            )
        return OutputVerification(
            valid=not enforce_size or size_acceptable,
            codec=verified.codec,
            dimensions=verified.dimensions,
            has_alpha=verified.has_alpha,
            has_icc_profile=verified.has_icc_profile,
            size_bytes=verified.size_bytes,
            decoder=verified.decoder,
            error=(
                None
                if not enforce_size or size_acceptable
                else "strict_size_limit_failed"
            ),
            icc_profile_sha256=verified.icc_profile_sha256,
            structurally_valid=True,
            size_acceptable=size_acceptable,
        )

    @staticmethod
    def _failure(
        verified: OutputVerification,
        error: str,
        *,
        size_acceptable: Optional[bool] = None,
    ) -> OutputVerification:
        return OutputVerification(
            False,
            verified.codec,
            verified.dimensions,
            verified.has_alpha,
            verified.has_icc_profile,
            verified.size_bytes,
            verified.decoder,
            error,
            verified.icc_profile_sha256,
            False,
            (
                verified.size_acceptable
                if size_acceptable is None
                else size_acceptable
            ),
        )

    def _verify_pillow(
        self,
        path: Path,
        size: int,
    ) -> OutputVerification:
        if not PILLOW_AVAILABLE or Image is None:
            return OutputVerification(
                False,
                None,
                None,
                None,
                None,
                size,
                "pillow",
                "Pillow is not available",
            )
        try:
            self.runner.cancellation.raise_if_cancelled()
            with pillow_open(path, self.max_pixels) as image:
                enforce_image_pixel_limit(
                    image,
                    self.max_pixels,
                    "verified output exceeds pixel limit",
                )
                image.load()
                fmt = (image.format or "").upper()
                width, height = image.size
                orientation = int(image.getexif().get(274, 1) or 1)
                if orientation in (5, 6, 7, 8):
                    width, height = height, width
                dimensions = ImageDimensions(width, height)
                has_alpha = (
                    "A" in image.mode
                    or (
                        image.mode == "P"
                        and "transparency" in image.info
                    )
                )
                icc_profile = image.info.get("icc_profile")
            codec = {
                "JPEG": ImageCodec.JPG,
                "MPO": ImageCodec.JPG,
                "PNG": ImageCodec.PNG,
                "WEBP": ImageCodec.WEBP,
            }.get(fmt)
            container_icc_present, container_icc = (
                _container_icc_profile_state(path)
            )
            output_icc = icc_profile or container_icc
            return OutputVerification(
                True,
                codec,
                dimensions,
                has_alpha,
                bool(icc_profile or container_icc_present),
                size,
                "pillow",
                icc_profile_sha256=icc_profile_sha256(output_icc),
                structurally_valid=True,
            )
        except UserAbortError:
            raise
        except Exception as exc:
            return OutputVerification(
                False,
                None,
                None,
                None,
                None,
                size,
                "pillow",
                f"{type(exc).__name__}: {exc}",
            )

    def _verify_ffmpeg(
        self,
        path: Path,
        size: int,
    ) -> OutputVerification:
        decode = self.runner.run(
            [
                str(self.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-threads",
                str(FFMPEG_PROCESS_THREADS),
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            timeout=PROBE_TIMEOUT_SEC,
            label=f"verify:{path.name}",
        )
        if not decode.ok:
            return OutputVerification(
                False,
                None,
                None,
                None,
                container_has_icc_profile(path),
                size,
                "ffmpeg",
                (decode.stderr or decode.stdout or "decode_failed")[:600],
            )
        probe = self.runner.run(
            [
                str(self.ffprobe),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height,pix_fmt",
                "-of",
                "json",
                str(path),
            ],
            timeout=PROBE_TIMEOUT_SEC,
            label=f"verify-probe:{path.name}",
        )
        if not probe.ok:
            return self._failure(
                OutputVerification(
                    True,
                    None,
                    None,
                    None,
                    container_has_icc_profile(path),
                    size,
                    "ffmpeg",
                ),
                "verification_probe_failed",
            )
        try:
            stream = (json.loads(probe.stdout).get("streams") or [])[0]
            codec = {
                "mjpeg": ImageCodec.JPG,
                "jpeg": ImageCodec.JPG,
                "png": ImageCodec.PNG,
                "webp": ImageCodec.WEBP,
            }.get(str(stream.get("codec_name") or "").casefold())
            dimensions = ImageDimensions(
                int(stream["width"]),
                int(stream["height"]),
            )
            if dimensions.width * dimensions.height > self.max_pixels:
                raise ImageSafetyError("verified output exceeds pixel limit")
            has_alpha = ImageProber._pixel_format_has_alpha(
                stream.get("pix_fmt")
            )
        except (
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            ImageSafetyError,
        ) as exc:
            return self._failure(
                OutputVerification(
                    True,
                    None,
                    None,
                    None,
                    container_has_icc_profile(path),
                    size,
                    "ffmpeg",
                ),
                f"invalid_verification_probe: {exc}",
            )
        container_icc_present, container_icc = (
            _container_icc_profile_state(path)
        )
        return OutputVerification(
            True,
            codec,
            dimensions,
            has_alpha,
            container_icc_present,
            size,
            "ffmpeg",
            icc_profile_sha256=icc_profile_sha256(container_icc),
            structurally_valid=True,
        )


class FFmpegEncoder:
    name = "ffmpeg"

    def __init__(
        self,
        ffmpeg: Path,
        runner: SubprocessRunner,
        *,
        timeout: float = ENCODE_TIMEOUT_SEC,
        capabilities: Optional[FFmpegCapabilities] = None,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.runner = runner
        self.timeout = timeout
        self.capabilities = capabilities

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
        source_has_icc_profile: Optional[bool] = None,
        source_has_alpha: bool = False,
        webp_variant: WebPVariant = WebPVariant.LOSSY,
        webp_method: int = WEBP_SEARCH_METHOD,
        png_colors: Optional[int] = None,
    ) -> CompressionAttempt:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.runner.cancellation.raise_if_cancelled()
        if png_colors is not None:
            return CompressionAttempt(
                backend=EncodeBackend.FFMPEG,
                quality=quality,
                ffmpeg_q=None,
                scale_factor=scale_factor,
                output_bytes=0,
                elapsed_sec=0.0,
                success=False,
                error="FFmpeg palette PNG encoding is unavailable",
                dimensions=target_dimensions,
                png_colors=png_colors,
            )
        if preserve_icc_profile and source_has_icc_profile:
            return CompressionAttempt(
                backend=EncodeBackend.FFMPEG,
                quality=quality,
                ffmpeg_q=None,
                scale_factor=scale_factor,
                output_bytes=0,
                elapsed_sec=0.0,
                success=False,
                error="FFmpeg cannot safely preserve ICC profile identity",
                dimensions=target_dimensions,
            )
        if codec == ImageCodec.JPG and source_has_alpha:
            return CompressionAttempt(
                backend=EncodeBackend.FFMPEG,
                quality=quality,
                ffmpeg_q=None,
                scale_factor=scale_factor,
                output_bytes=0,
                elapsed_sec=0.0,
                success=False,
                error="Pillow is required to flatten transparency onto white",
                dimensions=target_dimensions,
            )
        if not self._supports(codec):
            return CompressionAttempt(
                backend=EncodeBackend.FFMPEG,
                quality=quality,
                ffmpeg_q=(
                    quality_to_ffmpeg_q(quality)
                    if codec == ImageCodec.JPG
                    else None
                ),
                scale_factor=scale_factor,
                output_bytes=0,
                elapsed_sec=0.0,
                success=False,
                error=f"FFmpeg encoder is unavailable for {codec.value}",
                dimensions=target_dimensions,
            )
        t0 = time.perf_counter()
        q_v = quality_to_ffmpeg_q(quality)
        out_dims = target_dimensions
        base = self._base_args(source, strip_metadata=strip_metadata)
        codec_args = self._codec_args(
            codec,
            quality=quality,
            q_v=q_v,
            huffman=(
                self.capabilities.optimal_huffman
                if self.capabilities is not None
                else True
            ),
            source_pixel_format=source_pixel_format,
            webp_variant=webp_variant,
            webp_method=webp_method,
        )
        argv = [
            *base,
            *codec_args,
            "-threads",
            str(FFMPEG_PROCESS_THREADS),
            str(destination),
        ]
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
                    webp_variant=webp_variant,
                    webp_method=webp_method,
                ),
                "-threads",
                str(FFMPEG_PROCESS_THREADS),
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
            variant=(
                f"{webp_variant.value}-method-{webp_method}"
                if codec == ImageCodec.WEBP
                else "baseline-ffmpeg-fallback"
                if codec == ImageCodec.JPG and progressive
                else "baseline"
                if codec == ImageCodec.JPG
                else None
            ),
            webp_method=webp_method if codec == ImageCodec.WEBP else None,
            transient_failure=(
                not success
                and (
                    result.timed_out
                    or any(
                        marker in (result.stderr or "").casefold()
                        for marker in (
                            "resource temporarily unavailable",
                            "i/o error",
                            "input/output error",
                        )
                    )
                )
            ),
        )

    def _supports(self, codec: ImageCodec) -> bool:
        if self.capabilities is None:
            return True
        return {
            ImageCodec.JPG: self.capabilities.mjpeg,
            ImageCodec.PNG: self.capabilities.png,
            ImageCodec.WEBP: self.capabilities.webp,
        }[codec]

    def _base_args(
        self, source: Path, *, strip_metadata: bool
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
        webp_variant: WebPVariant = WebPVariant.LOSSY,
        webp_method: int = WEBP_SEARCH_METHOD,
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
        webp_q, lossless = quality_to_webp_params(
            quality,
            webp_variant,
        )
        return [
            "-c:v",
            "libwebp",
            "-quality",
            str(webp_q),
            "-lossless",
            "1" if lossless else "0",
            "-compression_level",
            str(webp_method),
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


class PillowEncoder:
    name = "pillow"

    def __init__(
        self,
        *,
        cancellation: Optional[CancellationToken] = None,
        max_pixels: int = DEFAULT_MAX_PIXELS,
    ) -> None:
        if not PILLOW_AVAILABLE:
            raise CompressorError("Pillow is not installed")
        if max_pixels <= 0:
            raise ValueError("max_pixels must be positive")
        self.cancellation = cancellation or CancellationToken()
        self.max_pixels = max_pixels
        self._source_cache = threading.local()

    @contextmanager
    def _source_image(self, source: Path) -> Any:
        stat_result = source.stat()
        key = (str(source.resolve()), stat_result.st_size, stat_result.st_mtime_ns)
        cached_key = getattr(self._source_cache, "key", None)
        cached_image = getattr(self._source_cache, "image", None)
        if cached_key != key or cached_image is None:
            if cached_image is not None:
                cached_image.close()
            loaded = pillow_open(source, self.max_pixels)
            try:
                enforce_image_pixel_limit(
                    loaded,
                    self.max_pixels,
                    "source image exceeds pixel limit",
                )
                loaded.load()
            except BaseException:
                loaded.close()
                raise
            self._source_cache.key = key
            self._source_cache.image = loaded
            cached_image = loaded
        image_copy = cached_image.copy()
        try:
            yield image_copy, dict(cached_image.info or {})
        finally:
            image_copy.close()

    def release_source(self, source: Path) -> None:
        cached_key = getattr(self._source_cache, "key", None)
        cached_image = getattr(self._source_cache, "image", None)
        if cached_key is None or cached_key[0] != str(source.resolve()):
            return
        if cached_image is not None:
            cached_image.close()
        self._source_cache.key = None
        self._source_cache.image = None

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
        webp_variant: WebPVariant = WebPVariant.LOSSY,
        webp_method: int = WEBP_SEARCH_METHOD,
        png_colors: Optional[int] = None,
    ) -> CompressionAttempt:
        if Image is None:
            raise CompressorError("Pillow is not available")
        if source_orientation not in range(0, 9):
            raise EncodeError("invalid EXIF orientation")
        if preserve_icc_profile and source_has_icc_profile is None:
            raise EncodeError("ICC preservation state is unknown")
        t0 = time.perf_counter()
        quality = clamp(int(quality), 1, 100)
        out_dims: Optional[ImageDimensions] = None
        try:
            self.cancellation.raise_if_cancelled()
            with self._source_image(source) as source_data:
                source_image, source_info = source_data
                icc_profile = source_info.get("icc_profile")
                source_exif = source_info.get("exif")
                img = (
                    ImageOps.exif_transpose(source_image)
                    if ImageOps is not None
                    else source_image
                )
                img = self._prepare_mode(img, codec)
                if (
                    source_has_alpha
                    and codec.preserves_alpha
                    and "A" not in img.mode
                    and not (img.mode == "P" and "transparency" in img.info)
                ):
                    raise EncodeError("source alpha channel could not be preserved")
                self.cancellation.raise_if_cancelled()
                out_dims = ImageDimensions(img.width, img.height)
                if target_dimensions is not None and out_dims != target_dimensions:
                    raise EncodeError("prepared image dimensions do not match the expected canvas")

                destination.parent.mkdir(parents=True, exist_ok=True)
                if codec == ImageCodec.PNG and png_colors is not None:
                    if self._is_high_bit_depth_mode(img.mode) or (source_bit_depth or 0) > 8:
                        raise EncodeError(
                            "Palette PNG conversion would reduce source bit depth"
                        )
                    img = self._quantize_png(img, png_colors)
                save_kwargs = self._save_kwargs(
                    codec,
                    quality,
                    progressive,
                    strip_metadata,
                    source_exif,
                    source_pixel_format=source_pixel_format,
                    webp_variant=webp_variant,
                    webp_method=webp_method,
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
                webp_method=webp_method if codec == ImageCodec.WEBP else None,
                png_colors=png_colors,
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
            variant=(
                f"{webp_variant.value}-method-{webp_method}"
                if codec == ImageCodec.WEBP
                else "progressive"
                if codec == ImageCodec.JPG and progressive
                else "baseline"
                if codec == ImageCodec.JPG
                else None
            ),
            webp_method=webp_method if codec == ImageCodec.WEBP else None,
            png_colors=png_colors,
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
        webp_variant: WebPVariant = WebPVariant.LOSSY,
        webp_method: int = WEBP_SEARCH_METHOD,
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
                "compress_level": PNG_COMPRESS_LEVEL,
            }
        else:
            webp_q, lossless = quality_to_webp_params(
                quality,
                webp_variant,
            )
            kwargs = {
                "format": "WEBP",
                "quality": webp_q,
                "method": webp_method,
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
        *,
        prefer: EncodeBackend = EncodeBackend.FFMPEG,
    ) -> None:
        self.ffmpeg_encoder = ffmpeg_encoder
        self.pillow_encoder = pillow_encoder
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
        webp_variant: WebPVariant = WebPVariant.LOSSY,
        webp_method: int = WEBP_SEARCH_METHOD,
        png_colors: Optional[int] = None,
    ) -> CompressionAttempt:
        order: List[Union[FFmpegEncoder, PillowEncoder]] = []
        if (
            source_has_icc_profile is None
            and preserve_icc_profile
        ):
            source_has_icc_profile = self._source_has_icc(source)
        strict_pillow_required = (
            not strip_metadata
            or (
                preserve_icc_profile
                and source_has_icc_profile
            )
            or (codec == ImageCodec.JPG and source_has_alpha)
        )
        prefer_pillow = strict_pillow_required or (
            progressive and codec == ImageCodec.JPG
        ) or source_orientation not in (0, 1)
        palette_png = codec == ImageCodec.PNG and png_colors is not None
        pillow_required_without_pillow = (
            self.pillow_encoder is None
            and (
                not strip_metadata
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
                png_colors=png_colors,
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
                png_colors=png_colors,
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
            pillow_first = codec == ImageCodec.PNG or prefer_pillow
            if self.pillow_encoder and (strict_pillow_required or palette_png):
                order = [self.pillow_encoder]
            elif self.pillow_encoder and progressive and codec == ImageCodec.JPG:
                order = [self.pillow_encoder, self.ffmpeg_encoder]
            elif self.pillow_encoder and (
                self.prefer == EncodeBackend.PILLOW or pillow_first
            ):
                order = [self.pillow_encoder, self.ffmpeg_encoder]
            else:
                order = [self.ffmpeg_encoder]
                if self.pillow_encoder:
                    order.append(self.pillow_encoder)

        last: Optional[CompressionAttempt] = None
        transient_failure = False
        for enc in order:
            tmp = unique_temp_path(destination.parent, destination.suffix, label=enc.name)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

            try:
                encode_kwargs: Dict[str, Any] = {
                    "codec": codec,
                    "quality": quality,
                    "scale_factor": scale_factor,
                    "target_dimensions": target_dimensions,
                    "strip_metadata": strip_metadata,
                    "preserve_icc_profile": preserve_icc_profile,
                    "progressive": progressive,
                    "source_pixel_format": source_pixel_format,
                    "source_has_icc_profile": source_has_icc_profile,
                    "source_has_alpha": source_has_alpha,
                    "webp_variant": webp_variant,
                    "webp_method": webp_method,
                    "png_colors": png_colors,
                }
                if isinstance(enc, PillowEncoder):
                    encode_kwargs["source_orientation"] = source_orientation
                    encode_kwargs["source_bit_depth"] = source_bit_depth
                attempt = enc.encode_image(source, tmp, **encode_kwargs)
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
                    return attempt
                attempt.output_bytes = file_size(destination)
                attempt.output_path = destination
                return attempt

            transient_failure = (
                transient_failure or attempt.transient_failure
            )
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        if last is None:
            raise EncodeError("no compatible encoder is available")
        last.transient_failure = transient_failure
        return last

    @staticmethod
    def _source_has_icc(source: Path) -> bool:
        return container_has_icc_profile(source)

    def release_source(self, source: Path) -> None:
        if self.pillow_encoder is not None:
            self.pillow_encoder.release_source(source)


class StrategyEngine:

    def __init__(
        self,
        encoder: DualEncoder,
        logger: logging.Logger,
        config: RuntimeConfig,
        verifier: OutputVerifier,
    ) -> None:
        self.encoder = encoder
        self.logger = logger
        self.config = config
        self.verifier = verifier

    def _codec_for(self, path: Path) -> ImageCodec:
        return resolve_output_codec(path, self.config.output_format)

    def _temp_suffix(self, path: Path) -> str:
        return self._codec_for(path).extension

    def _encode(
        self,
        probe: ImageProbeResult,
        destination: Path,
        *,
        quality: int,
        scale_factor: float = 1.0,
        target_dimensions: Optional[ImageDimensions] = None,
        webp_variant: WebPVariant = WebPVariant.LOSSY,
        webp_method: int = WEBP_SEARCH_METHOD,
        png_colors: Optional[int] = None,
    ) -> CompressionAttempt:
        source = probe.processing_path or probe.path
        codec = self._codec_for(probe.path)
        encode_kwargs: Dict[str, Any] = {
            "codec": codec,
            "quality": quality,
            "scale_factor": scale_factor,
            "target_dimensions": target_dimensions,
            "strip_metadata": self.config.strip_metadata,
            "preserve_icc_profile": self.config.preserve_icc_profile,
            "progressive": self.config.progressive_jpeg,
            "webp_variant": webp_variant,
            "webp_method": webp_method,
            "png_colors": png_colors,
        }
        encode_kwargs.update(
            source_pixel_format=probe.pixel_format,
            source_orientation=probe.exif_orientation,
            source_has_icc_profile=probe.has_icc_profile,
            source_bit_depth=probe.bit_depth,
            source_has_alpha=probe.has_alpha,
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
            target_codec=self._codec_for(source),
            source_has_alpha=probe.has_alpha,
            source_has_icc_profile=probe.has_icc_profile,
            source_icc_profile_sha256=probe.icc_profile_sha256,
            resized=probe.resized,
            padded=probe.padded,
            scale_factor=probe.canvas_scale,
        )
        process_termination_unconfirmed = False
        try:
            if not probe.is_readable:
                result.status = ImageStatus.SKIPPED_CORRUPT
                result.message = probe.probe_error or "unreadable"
                result.error_detail = probe.probe_error
                return result

            target_codec = self._codec_for(source)
            same_format = codecs_match_for_copy(source, target_codec)

            if (
                probe.processing_path is None
                and self.config.size_policy.is_acceptable(probe.size_bytes)
            ):
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
        except ProcessTerminationError:
            process_termination_unconfirmed = True
            raise
        except Exception as exc:
            self.logger.exception("Unhandled error processing %s", source.name)
            result.status = ImageStatus.FAILED
            result.message = "unhandled_exception"
            result.error_detail = f"{type(exc).__name__}: {exc}"
            return result
        finally:
            self.encoder.release_source(probe.processing_path or probe.path)
            if not process_termination_unconfirmed:
                self._cleanup_uncommitted_attempts(result)
            result.elapsed_sec = time.perf_counter() - t0

    def _try_format_convert_only(
        self,
        probe: ImageProbeResult,
        final_dest: Path,
        work_dir: Path,
        result: ImageJobResult,
    ) -> bool:
        attempt = self._attempt_candidate(
            probe,
            work_dir,
            result,
            quality=QUALITY_LOSSLESS_PROXY,
            scale_factor=1.0,
            label="format-convert",
        )
        if attempt and attempt.is_preferred(self.config.size_policy):
            self._commit_best(probe, work_dir, attempt, final_dest, result)
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
            stage = unique_temp_path(
                work_dir,
                self._temp_suffix(probe.path),
                label="sanitize" if self.config.strip_metadata else "copy",
            )
            sanitized = False
            fallback_reason: Optional[str] = None
            metadata_retained = False
            if self.config.strip_metadata and probe.is_jpeg:
                sanitized_result = JPEGMetadataSanitizer.sanitize(
                    probe.path,
                    stage,
                    orientation=probe.exif_orientation,
                    preserve_icc=self.config.preserve_icc_profile,
                )
                icc_preserved = (
                    not self.config.preserve_icc_profile
                    or not probe.has_icc_profile
                    or sanitized_result.preserved_icc
                )
                sanitized = (
                    sanitized_result.success
                    and sanitized_result.changed
                    and icc_preserved
                )
                if not sanitized_result.success or not icc_preserved:
                    fallback_reason = (
                        sanitized_result.error
                        or "ICC profile could not be safely preserved during sanitization"
                    )
                    metadata_retained = True
                    shutil.copy2(probe.path, stage)
            else:
                metadata_retained = (
                    self.config.strip_metadata and not probe.is_jpeg
                )
                shutil.copy2(probe.path, stage)
            attempt = CompressionAttempt(
                backend=EncodeBackend.COPY,
                quality=None,
                ffmpeg_q=None,
                scale_factor=1.0,
                output_bytes=file_size(stage),
                elapsed_sec=0.0,
                success=stage.is_file() and file_size(stage) > 0,
                dimensions=probe.dimensions,
                output_path=stage,
                variant=(
                    "marker-sanitized"
                    if sanitized
                    else "exact-copy-metadata-retained"
                    if metadata_retained
                    else "exact-copy"
                ),
                error=fallback_reason,
            )
            result.attempts.append(attempt)
            if not attempt.success:
                result.status = ImageStatus.FAILED
                result.message = "copy_failed"
                result.error_detail = attempt.error
                self._discard_attempt(attempt)
                return result
            if not self.config.size_policy.is_acceptable(attempt.output_bytes):
                self._discard_attempt(attempt)
                return None
            self._commit_best(probe, work_dir, attempt, final_dest, result)
            stage = None
            if result.status == ImageStatus.COMPRESSED:
                result.backend = EncodeBackend.COPY
                result.quality_used = None
                result.ffmpeg_q_used = None
                result.variant = attempt.variant
                if sanitized:
                    result.status = ImageStatus.SANITIZED
                    result.message = "metadata removed without image re-encoding"
                else:
                    result.status = ImageStatus.COPIED
                    result.message = (
                        "exact copy; metadata retained because safe sanitization was not possible"
                        if fallback_reason
                        else "exact copy; metadata retained"
                        if metadata_retained
                        else t("msg_already_copied")
                    )
                    result.error_detail = (
                        fallback_reason
                        or (
                            "metadata stripping is best effort for exact PNG/WEBP copies"
                            if metadata_retained
                            else None
                        )
                    )
        except (UserAbortError, ProcessTerminationError):
            raise
        except Exception as exc:
            result.status = ImageStatus.FAILED
            result.message = "metadata_sanitize_failed" if self.config.strip_metadata else "copy_failed"
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
        palette_supported = (
            self.encoder.pillow_encoder is not None
            and not self._probe_is_high_bit_depth(probe)
        )
        best = self._attempt_candidate(
            probe,
            work_dir,
            result,
            quality=QUALITY_LOSSLESS_PROXY,
            label="png-lossless",
        )
        if best is not None:
            best.variant = "lossless"
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        if palette_supported:
            best = self._phase_png_palette_search(
                probe,
                work_dir,
                result,
                previous_best=best,
            )
        self._fail_or_commit_closest(probe, work_dir, best, final_dest, result)

    def _phase_png_palette_search(
        self,
        probe: ImageProbeResult,
        work_dir: Path,
        result: ImageJobResult,
        *,
        previous_best: Optional[CompressionAttempt] = None,
    ) -> Optional[CompressionAttempt]:
        best = previous_best
        tried: set[int] = set()
        for colors in PNG_PALETTE_COLOR_TARGETS:
            tried.add(colors)
            attempt = self._attempt_candidate(
                probe,
                work_dir,
                result,
                quality=png_palette_quality(colors),
                png_colors=colors,
                label="png-palette",
            )
            if attempt is None:
                continue
            attempt.variant = f"palette-{colors}"
            best = self._retain_better(best, attempt)
            if best.is_preferred(self.config.size_policy):
                break
        lo = PNG_PALETTE_MIN_COLORS
        hi = 256
        iterations = 0
        while lo <= hi and iterations < BINARY_SEARCH_MAX_ITERS:
            self.config.cancellation.raise_if_cancelled()
            iterations += 1
            colors = (lo + hi) // 2
            if colors in tried:
                available = [
                    value
                    for value in range(lo, hi + 1)
                    if value not in tried
                ]
                if not available:
                    break
                colors = available[len(available) // 2]
            tried.add(colors)
            attempt = self._attempt_candidate(
                probe,
                work_dir,
                result,
                quality=png_palette_quality(colors),
                png_colors=colors,
                label="png-palette-search",
            )
            if attempt is None:
                hi = colors - 1
                continue
            attempt.variant = f"palette-{colors}"
            best = self._retain_better(best, attempt)
            if attempt.is_preferred(self.config.size_policy):
                lo = colors + 1
            else:
                hi = colors - 1
        boundary = best.png_colors if best and best.png_colors is not None else None
        if boundary is not None:
            for colors in range(max(2, boundary - 3), min(256, boundary + 3) + 1):
                if colors in tried:
                    continue
                attempt = self._attempt_candidate(
                    probe,
                    work_dir,
                    result,
                    quality=png_palette_quality(colors),
                    png_colors=colors,
                    label="png-palette-refine",
                )
                if attempt is None:
                    continue
                attempt.variant = f"palette-{colors}"
                best = self._retain_better(best, attempt)
        return best

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
        best = self._phase_maximum_fidelity(probe, work_dir, result)
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
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
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        q_lo, q_hi = self._predictive_quality_bounds(probe)
        best = self._phase_binary_search(
            probe,
            work_dir,
            result,
            q_lo=q_lo,
            q_hi=min(84, q_hi),
            scale_factor=1.0,
            previous_best=best,
        )
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        if best and best.is_acceptable(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        self._fail_or_commit_closest(probe, work_dir, best, final_dest, result)

    def _strategy_high_quality(
        self, probe: ImageProbeResult, final_dest: Path, work_dir: Path, result: ImageJobResult
    ) -> None:
        q_lo, q_hi = self._predictive_quality_bounds(probe)
        ladder = tuple(q for q in (95, 93, 92, 90, 88, 85) if q <= q_hi)
        best = self._phase_quality_ladder(
            probe,
            work_dir,
            result,
            qualities=ladder or (q_hi,),
            scale_factor=1.0,
        )
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        best = self._phase_binary_search(
            probe,
            work_dir,
            result,
            q_lo=q_lo,
            q_hi=min(89, q_hi),
            scale_factor=1.0,
            previous_best=best,
        )
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        if best and best.is_acceptable(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        self._fail_or_commit_closest(probe, work_dir, best, final_dest, result)

    def _strategy_binary_search(
        self, probe: ImageProbeResult, final_dest: Path, work_dir: Path, result: ImageJobResult
    ) -> None:
        q_lo, q_hi = self._predictive_quality_bounds(probe)
        best = self._phase_binary_search(
            probe,
            work_dir,
            result,
            q_lo=q_lo,
            q_hi=q_hi,
            scale_factor=1.0,
        )
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        if best and best.is_acceptable(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        self._fail_or_commit_closest(probe, work_dir, best, final_dest, result)

    def _strategy_aggressive_adaptive(
        self, probe: ImageProbeResult, final_dest: Path, work_dir: Path, result: ImageJobResult
    ) -> None:
        reduction_needed = probe.reduction_needed_pct(self.config.size_policy)
        best = (
            self._phase_maximum_fidelity(probe, work_dir, result)
            if reduction_needed < 20.0
            else None
        )
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        q_lo, q_hi = self._predictive_quality_bounds(probe)
        ladder = tuple(q for q in (95, 92, 90, 86, 82, 78) if q <= q_hi)
        best = self._phase_quality_ladder(
            probe,
            work_dir,
            result,
            qualities=ladder or (q_hi,),
            scale_factor=1.0,
            previous_best=best,
        )
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        best = self._phase_binary_search(
            probe,
            work_dir,
            result,
            q_lo=q_lo,
            q_hi=min(89, q_hi),
            scale_factor=1.0,
            previous_best=best,
        )
        if best and best.is_preferred(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        if best and best.is_acceptable(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        self._fail_or_commit_closest(probe, work_dir, best, final_dest, result)

    def _predictive_quality_bounds(
        self,
        probe: ImageProbeResult,
    ) -> Tuple[int, int]:
        reduction_needed = probe.reduction_needed_pct(self.config.size_policy)
        if reduction_needed >= 55.0:
            return (20, 74)
        if reduction_needed >= 40.0:
            return (24, 82)
        if reduction_needed >= 25.0:
            return (30, 88)
        if reduction_needed >= 10.0:
            return (36, 93)
        return (QUALITY_BINARY_MIN, QUALITY_BINARY_MAX)

    def _attempt_key(
        self,
        probe: ImageProbeResult,
        *,
        quality: int,
        scale_factor: float,
        target_dimensions: Optional[ImageDimensions],
        webp_variant: WebPVariant,
        webp_method: int,
        png_colors: Optional[int],
    ) -> Tuple[Any, ...]:
        dimensions = target_dimensions or probe.processing_dimensions or probe.dimensions
        codec = self._codec_for(probe.path)
        backend = self._preferred_backend_for_attempt(
            probe,
            codec,
            png_colors,
        )
        if codec == ImageCodec.PNG:
            effective_quality: Any = (
                ("palette", png_colors)
                if png_colors is not None
                else ("lossless", PNG_COMPRESS_LEVEL)
            )
        elif codec == ImageCodec.WEBP and webp_variant == WebPVariant.LOSSLESS:
            effective_quality = ("lossless", webp_method)
        else:
            effective_quality = ("request", quality)
        return self._attempt_key_parts(
            probe,
            backend=backend,
            effective_quality=effective_quality,
            scale_factor=scale_factor,
            dimensions=dimensions,
            webp_variant=webp_variant,
            webp_method=webp_method,
        )

    def _attempt_key_parts(
        self,
        probe: ImageProbeResult,
        *,
        backend: EncodeBackend,
        effective_quality: Any,
        scale_factor: float,
        dimensions: Optional[ImageDimensions],
        webp_variant: WebPVariant,
        webp_method: int,
    ) -> Tuple[Any, ...]:
        jpeg_mode = (
            "progressive"
            if self._codec_for(probe.path) == ImageCodec.JPG
            and backend == EncodeBackend.PILLOW
            and self.config.progressive_jpeg
            else "baseline"
        )
        return (
            self._codec_for(probe.path),
            webp_variant,
            webp_method,
            backend,
            effective_quality,
            dimensions.width if dimensions else None,
            dimensions.height if dimensions else None,
            round(scale_factor, 8),
            self.config.strip_metadata,
            self.config.preserve_icc_profile,
            jpeg_mode,
            probe.has_alpha,
            probe.has_icc_profile,
            probe.resized,
            probe.padded,
            self.config.allow_upscale,
        )

    def _successful_attempt_alias(
        self,
        probe: ImageProbeResult,
        attempt: CompressionAttempt,
        *,
        scale_factor: float,
        target_dimensions: Optional[ImageDimensions],
        webp_variant: WebPVariant,
        webp_method: int,
    ) -> Optional[Tuple[Any, ...]]:
        if (
            self._codec_for(probe.path) != ImageCodec.JPG
            or not attempt.success
            or attempt.output_path is None
            or attempt.backend != EncodeBackend.FFMPEG
            or attempt.ffmpeg_q is None
        ):
            return None
        return self._attempt_key_parts(
            probe,
            backend=EncodeBackend.FFMPEG,
            effective_quality=("ffmpeg-q", attempt.ffmpeg_q),
            scale_factor=scale_factor,
            dimensions=target_dimensions or probe.processing_dimensions or probe.dimensions,
            webp_variant=webp_variant,
            webp_method=webp_method,
        )

    def _predicted_attempt_alias(
        self,
        probe: ImageProbeResult,
        *,
        quality: int,
        scale_factor: float,
        target_dimensions: Optional[ImageDimensions],
        webp_variant: WebPVariant,
        webp_method: int,
    ) -> Optional[Tuple[Any, ...]]:
        codec = self._codec_for(probe.path)
        backend = self._preferred_backend_for_attempt(
            probe,
            codec,
            None,
        )
        if codec != ImageCodec.JPG or backend != EncodeBackend.FFMPEG:
            return None
        return self._attempt_key_parts(
            probe,
            backend=EncodeBackend.FFMPEG,
            effective_quality=("ffmpeg-q", quality_to_ffmpeg_q(quality)),
            scale_factor=scale_factor,
            dimensions=target_dimensions or probe.processing_dimensions or probe.dimensions,
            webp_variant=webp_variant,
            webp_method=webp_method,
        )

    def _preferred_backend_for_attempt(
        self,
        probe: ImageProbeResult,
        codec: ImageCodec,
        png_colors: Optional[int],
    ) -> EncodeBackend:
        pillow = getattr(self.encoder, "pillow_encoder", None)
        if pillow is None:
            return EncodeBackend.FFMPEG
        high_bit_depth_png = (
            codec == ImageCodec.PNG
            and self._probe_is_high_bit_depth(probe)
        )
        if high_bit_depth_png:
            return EncodeBackend.FFMPEG
        requires_pillow = (
            not self.config.strip_metadata
            or probe.exif_orientation not in (0, 1)
            or (
                self.config.progressive_jpeg
                and codec == ImageCodec.JPG
            )
            or (
                self.config.preserve_icc_profile
                and PILLOW_AVAILABLE
                and probe.has_icc_profile
            )
            or (codec == ImageCodec.JPG and probe.has_alpha)
        )
        palette_png = codec == ImageCodec.PNG and png_colors is not None
        prefer = getattr(
            self.encoder,
            "prefer",
            EncodeBackend.FFMPEG,
        )
        if requires_pillow or palette_png:
            return EncodeBackend.PILLOW
        if codec == ImageCodec.PNG or prefer == EncodeBackend.PILLOW:
            return EncodeBackend.PILLOW
        return EncodeBackend.FFMPEG

    def _attempt_candidate(
        self,
        probe: ImageProbeResult,
        work_dir: Path,
        result: ImageJobResult,
        *,
        quality: int,
        scale_factor: float = 1.0,
        target_dimensions: Optional[ImageDimensions] = None,
        webp_variant: WebPVariant = WebPVariant.LOSSY,
        webp_method: int = WEBP_SEARCH_METHOD,
        png_colors: Optional[int] = None,
        label: str = "candidate",
    ) -> Optional[CompressionAttempt]:
        self.config.cancellation.raise_if_cancelled()
        effective_dimensions = (
            target_dimensions
            or probe.processing_dimensions
            or probe.dimensions
        )
        effective_scale = probe.canvas_scale if probe.processing_path else scale_factor
        key = self._attempt_key(
            probe,
            quality=quality,
            scale_factor=effective_scale,
            target_dimensions=effective_dimensions,
            webp_variant=webp_variant,
            webp_method=webp_method,
            png_colors=png_colors,
        )
        cached = result.attempt_cache.get(key)
        if key in result.attempted_keys:
            if self._cached_attempt_available(cached):
                return cached
            return None
        alias = self._predicted_attempt_alias(
            probe,
            quality=quality,
            scale_factor=effective_scale,
            target_dimensions=effective_dimensions,
            webp_variant=webp_variant,
            webp_method=webp_method,
        )
        if alias is not None:
            cached = result.attempt_cache.get(alias)
            if self._cached_attempt_available(cached):
                result.attempted_keys.add(key)
                result.attempt_cache[key] = cached
                return cached
        result.attempted_keys.add(key)
        while True:
            tmp = unique_temp_path(
                work_dir,
                suffix=self._temp_suffix(probe.path),
                label=label,
            )
            attempt = self._encode(
                probe,
                tmp,
                quality=quality,
                scale_factor=effective_scale,
                target_dimensions=effective_dimensions,
                webp_variant=webp_variant,
                webp_method=webp_method,
                png_colors=png_colors,
            )
            result.attempts.append(attempt)
            result.attempt_cache[key] = attempt
            if attempt.success:
                if not self._verify_attempt(
                    probe,
                    attempt,
                    result,
                    target_dimensions=effective_dimensions,
                ):
                    result.attempt_cache[key] = attempt
                    self._discard_attempt(attempt)
                    return None
                success_alias = self._successful_attempt_alias(
                    probe,
                    attempt,
                    scale_factor=effective_scale,
                    target_dimensions=effective_dimensions,
                    webp_variant=webp_variant,
                    webp_method=webp_method,
                )
                if success_alias is not None:
                    result.attempt_cache[success_alias] = attempt
                return attempt
            self._discard_attempt(attempt)
            if (
                attempt.transient_failure
                and result.transient_retries.get(key, 0) < 1
            ):
                result.transient_retries[key] = 1
                continue
            return None

    @staticmethod
    def _cached_attempt_available(
        attempt: Optional[CompressionAttempt],
    ) -> bool:
        return bool(
            attempt is not None
            and attempt.success
            and attempt.verification is not None
            and attempt.verification.structurally_valid
            and attempt.output_path is not None
            and attempt.output_path.is_file()
        )

    def _verify_attempt(
        self,
        probe: ImageProbeResult,
        attempt: CompressionAttempt,
        result: ImageJobResult,
        *,
        target_dimensions: Optional[ImageDimensions],
    ) -> bool:
        if attempt.output_path is None:
            return False
        expected_codec = result.target_codec or self._codec_for(probe.path)
        expected_dimensions = target_dimensions or probe.dimensions
        verification = self.verifier.verify(
            attempt.output_path,
            expected_codec=expected_codec,
            expected_dimensions=expected_dimensions,
            require_alpha=(
                probe.has_alpha and expected_codec.preserves_alpha
            ),
            require_icc=(
                self.config.preserve_icc_profile
                and probe.has_icc_profile
            ),
            expected_icc_sha256=probe.icc_profile_sha256,
            size_policy=self.config.size_policy,
            enforce_size=False,
        )
        attempt.verification = verification
        attempt.dimensions = verification.dimensions or expected_dimensions
        attempt.output_bytes = verification.size_bytes
        if verification.structurally_valid:
            return True
        attempt.success = False
        attempt.error = verification.error or "output_verification_failed"
        return False

    def _phase_maximum_fidelity(
        self, probe: ImageProbeResult, work_dir: Path, result: ImageJobResult
    ) -> Optional[CompressionAttempt]:
        codec = self._codec_for(probe.path)
        variant = (
            WebPVariant.LOSSLESS
            if codec == ImageCodec.WEBP
            else WebPVariant.LOSSY
        )
        return self._attempt_candidate(
            probe,
            work_dir,
            result,
            quality=QUALITY_LOSSLESS_PROXY,
            scale_factor=1.0,
            webp_variant=variant,
            label="maximum-fidelity",
        )

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
            attempt = self._attempt_candidate(
                probe,
                work_dir,
                result,
                quality=q,
                scale_factor=scale_factor,
                webp_variant=WebPVariant.LOSSY,
                label="quality",
            )
            if attempt is None:
                continue
            best = self._retain_better(best, attempt)
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
        failed_recovery_attempts = 0
        tried: set[int] = set()
        boundary_quality: Optional[int] = None
        acceptable_boundary: Optional[int] = (
            best.quality
            if best is not None
            and best.quality is not None
            and best.is_acceptable(self.config.size_policy)
            else None
        )
        while lo <= hi and iterations < BINARY_SEARCH_MAX_ITERS:
            self.config.cancellation.raise_if_cancelled()
            iterations += 1
            mid = (lo + hi) // 2
            if mid in tried:
                break
            candidate_qualities = [mid]
            candidate_qualities.extend(
                quality
                for offset in BINARY_FAILURE_OFFSETS
                if lo <= (quality := mid + offset) <= hi
                and quality not in tried
            )
            attempt: Optional[CompressionAttempt] = None
            used_quality: Optional[int] = None
            for index, quality in enumerate(candidate_qualities):
                if index > 0:
                    if (
                        failed_recovery_attempts
                        >= BINARY_FAILURE_ATTEMPT_BUDGET
                    ):
                        break
                    failed_recovery_attempts += 1
                tried.add(quality)
                attempt = self._attempt_candidate(
                    probe,
                    work_dir,
                    result,
                    quality=quality,
                    scale_factor=scale_factor,
                    target_dimensions=target_dims,
                    webp_variant=WebPVariant.LOSSY,
                    label="binary" if index == 0 else "binary-recovery",
                )
                if attempt is not None:
                    used_quality = quality
                    break
            if attempt is None or used_quality is None:
                break
            preferred = attempt.is_preferred(self.config.size_policy)
            if attempt.is_acceptable(self.config.size_policy):
                acceptable_boundary = max(
                    acceptable_boundary or used_quality,
                    used_quality,
                )
            best = self._retain_better(best, attempt)
            if preferred:
                boundary_quality = used_quality
                lo = used_quality + 1
            else:
                hi = used_quality - 1
        refinement_boundary = boundary_quality or acceptable_boundary
        if refinement_boundary is not None:
            refinement = range(
                max(clamp(q_lo, 1, 100), refinement_boundary - BINARY_REFINEMENT_RADIUS),
                min(clamp(q_hi, 1, 100), refinement_boundary + BINARY_REFINEMENT_RADIUS) + 1,
            )
            for quality in refinement:
                if quality in tried:
                    continue
                tried.add(quality)
                attempt = self._attempt_candidate(
                    probe,
                    work_dir,
                    result,
                    quality=quality,
                    scale_factor=scale_factor,
                    target_dimensions=target_dims,
                    webp_variant=WebPVariant.LOSSY,
                    label="refine",
                )
                if attempt is None:
                    continue
                best = self._retain_better(best, attempt)
        return best

    def _retain_better(
        self,
        current: Optional[CompressionAttempt],
        new: CompressionAttempt,
    ) -> CompressionAttempt:
        selected = self._better_attempt(current, new)
        if selected is new:
            if current is not new:
                self._discard_attempt(current)
            return new
        if current is not new:
            self._discard_attempt(new)
        return selected

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
        return new if new.output_bytes < current.output_bytes else current

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

    def _verify_output(
        self,
        path: Path,
        best: CompressionAttempt,
        result: ImageJobResult,
    ) -> OutputVerification:
        expected_codec = result.target_codec or self._codec_for(result.source)
        return self.verifier.verify(
            path,
            expected_codec=expected_codec,
            expected_dimensions=best.dimensions or result.original_dimensions,
            require_alpha=(
                result.source_has_alpha and expected_codec.preserves_alpha
            ),
            require_icc=(
                self.config.preserve_icc_profile
                and result.source_has_icc_profile
            ),
            expected_icc_sha256=result.source_icc_profile_sha256,
            size_policy=self.config.size_policy,
        )

    def _record_verification_failure(
        self,
        best: CompressionAttempt,
        result: ImageJobResult,
        verification: OutputVerification,
    ) -> None:
        result.verification = verification
        best.output_bytes = verification.size_bytes
        if (
            verification.structurally_valid
            and verification.size_acceptable is False
        ):
            result.status = ImageStatus.SIZE_LIMIT_FAILED
            result.output_bytes = verification.size_bytes
            result.quality_used = best.quality
            result.ffmpeg_q_used = best.ffmpeg_q
            result.scale_factor = best.scale_factor
            result.backend = best.backend
            result.variant = best.variant
            result.webp_method = best.webp_method
            result.png_colors = best.png_colors
            result.message = t(
                "msg_size_fail",
                limit=human_bytes(self.config.max_bytes, binary=False),
                best=human_bytes(verification.size_bytes, binary=False),
            )
        else:
            result.status = ImageStatus.FAILED
            result.message = "output_verification_failed"
        result.error_detail = verification.error

    def _verify_candidate(
        self,
        best: CompressionAttempt,
        result: ImageJobResult,
    ) -> bool:
        if best.output_path is None:
            return False
        expected_dimensions = best.dimensions or result.original_dimensions
        verification = self._verify_output(best.output_path, best, result)
        result.verification = verification
        best.output_bytes = verification.size_bytes
        if verification.valid:
            best.dimensions = verification.dimensions or expected_dimensions
            return True
        self._record_verification_failure(best, result, verification)
        self._discard_attempt(best)
        return False

    def _commit_best(
        self,
        probe: ImageProbeResult,
        work_dir: Path,
        best: CompressionAttempt,
        final_dest: Path,
        result: ImageJobResult,
    ) -> None:
        best = self._finalize_webp_candidate(probe, work_dir, best, result)
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
        try:
            candidate_verified = self._verify_candidate(best, result)
        except ProcessTerminationError:
            raise
        except BaseException:
            self._discard_attempt(best)
            raise
        if not candidate_verified:
            return
        published = False

        def publish_and_verify() -> OutputVerification:
            nonlocal published
            atomic_publish(
                best.output_path,
                final_dest,
                overwrite=(
                    self.config.overwrite_output
                    or self.config.reprocess_existing
                ),
            )
            published = True
            best.output_path = None
            try:
                fsync_directory(final_dest.parent)
            except OSError:
                pass
            return self._verify_output(final_dest, best, result)

        def discard_publication() -> None:
            if published:
                try:
                    final_dest.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                self._discard_attempt(best)
            result.output_path = None

        try:
            verification = self.config.cancellation.run_if_active(
                publish_and_verify
            )
        except (UserAbortError, ProcessTerminationError):
            discard_publication()
            raise
        except BaseException as exc:
            discard_publication()
            if not isinstance(exc, Exception):
                raise
            result.status = ImageStatus.FAILED
            result.message = "commit_failed"
            result.error_detail = f"{type(exc).__name__}: {exc}"
            return
        result.output_path = final_dest
        result.verification = verification
        result.output_bytes = verification.size_bytes
        if not verification.valid:
            self._record_verification_failure(best, result, verification)
            try:
                final_dest.unlink(missing_ok=True)
            except OSError:
                pass
            result.output_path = None
            return
        best.dimensions = (
            verification.dimensions
            or best.dimensions
            or result.original_dimensions
        )
        result.output_dimensions = best.dimensions
        result.quality_used = best.quality
        result.ffmpeg_q_used = best.ffmpeg_q
        result.scale_factor = best.scale_factor
        result.backend = best.backend
        result.variant = best.variant
        result.webp_method = best.webp_method
        result.png_colors = best.png_colors
        result.status = ImageStatus.COMPRESSED
        result.message = t(
            "msg_compressed",
            size=human_bytes(result.output_bytes),
            q=result.variant or result.quality_used,
            scale=result.scale_factor,
        )

    def _finalize_webp_candidate(
        self,
        probe: ImageProbeResult,
        work_dir: Path,
        best: CompressionAttempt,
        result: ImageJobResult,
    ) -> CompressionAttempt:
        if (
            self._codec_for(probe.path) != ImageCodec.WEBP
            or best.backend == EncodeBackend.COPY
            or best.webp_method == WEBP_FINAL_METHOD
        ):
            return best
        variant = (
            WebPVariant.LOSSLESS
            if best.variant and best.variant.startswith(WebPVariant.LOSSLESS.value)
            else WebPVariant.LOSSY
        )
        final_attempt = self._attempt_candidate(
            probe,
            work_dir,
            result,
            quality=best.quality or QUALITY_LOSSLESS_PROXY,
            webp_variant=variant,
            webp_method=WEBP_FINAL_METHOD,
            label="webp-final",
        )
        if final_attempt and final_attempt.is_acceptable(self.config.size_policy):
            self._discard_attempt(best)
            return final_attempt
        self._discard_attempt(final_attempt)
        return best

    def _fail_or_commit_closest(
        self,
        probe: ImageProbeResult,
        work_dir: Path,
        best: Optional[CompressionAttempt],
        final_dest: Path,
        result: ImageJobResult,
    ) -> None:
        if best is None or not best.success:
            result.status = ImageStatus.FAILED
            result.message = "all_encode_attempts_failed"
            if result.attempts:
                result.error_detail = result.attempts[-1].error
            return
        if best.is_acceptable(self.config.size_policy):
            self._commit_best(probe, work_dir, best, final_dest, result)
            return
        result.status = ImageStatus.SIZE_LIMIT_FAILED
        result.output_bytes = best.output_bytes
        result.quality_used = best.quality
        result.ffmpeg_q_used = best.ffmpeg_q
        result.scale_factor = best.scale_factor
        result.backend = best.backend
        result.variant = best.variant
        result.webp_method = best.webp_method
        result.png_colors = best.png_colors
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

    def discover_files(self, *, recursive: bool = False) -> List[Path]:
        found: List[Path] = []
        try:
            if recursive:
                entries: List[Path] = []
                for current, directories, filenames in os.walk(
                    self.root,
                    followlinks=False,
                ):
                    current_path = Path(current)
                    directories[:] = sorted(
                        (
                            name
                            for name in directories
                            if not (current_path / name).is_symlink()
                            and name != TEMP_WORKDIR_NAME
                            and not (
                                self.output_dir
                                and is_within_directory(
                                    current_path / name,
                                    self.output_dir,
                                )
                            )
                        ),
                        key=str.casefold,
                    )
                    entries.extend(
                        current_path / name
                        for name in sorted(
                            filenames,
                            key=str.casefold,
                        )
                    )
            else:
                entries = sorted(
                    self.root.iterdir(),
                    key=lambda path: path.name.casefold(),
                )
        except OSError as exc:
            raise WorkspaceError(
                f"Cannot list directory {self.root}: {exc}",
                cause=exc,
            ) from exc
        for entry in entries:
            if not entry.is_file() or entry.is_symlink():
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
        return sorted(
            found,
            key=lambda path: (
                str(path.relative_to(self.root)).casefold(),
                str(path.relative_to(self.root)),
            ),
        )

    def scan(
        self,
        *,
        max_workers: int = 4,
        recursive: bool = False,
    ) -> PreflightSummary:
        t0 = time.perf_counter()
        self.cancellation.raise_if_cancelled()
        files = self.discover_files(recursive=recursive)
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
                        if isinstance(
                            exc,
                            (UserAbortError, ProcessTerminationError),
                        ):
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
        except (
            KeyboardInterrupt,
            UserAbortError,
            ProcessTerminationError,
        ):
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
            codec = detect_source_codec(img.path)
            est_high_q = estimate_output_bytes(
                img.size_bytes,
                93,
                codec=codec,
            )
            est_low_q = estimate_output_bytes(
                img.size_bytes,
                55,
                codec=codec,
            )
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
            potential_savings_low_mb=savings_low / 1_000_000,
            potential_savings_high_mb=savings_high / 1_000_000,
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

    def banner(self, size_policy: SizePolicy = DEFAULT_SIZE_POLICY) -> None:
        title = f"{SCRIPT_NAME} v{SCRIPT_VERSION}"
        subtitle = t(
            "banner_subtitle",
            target_mb=f"{size_policy.strict_max_bytes / 1_000_000:g}",
            target_bytes=f"{size_policy.strict_max_bytes:,}",
        )
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
            overview.add_row(t("total_size"), human_bytes(summary.total_bytes, binary=False))
            overview.add_row(t("over_limit_mass"), human_bytes(summary.over_limit_bytes, binary=False))
            overview.add_row(
                t("est_savings_gentle"), f"~{summary.potential_savings_low_mb:.1f} MB"
            )
            overview.add_row(t("est_savings_aggr"), f"~{summary.potential_savings_high_mb:.1f} MB")
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
            print(f"  {t('plain_total', s=human_bytes(summary.total_bytes, binary=False))}")
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

    def recommend_output_format(
        self,
        summary: PreflightSummary,
    ) -> OutputFormatChoice:
        if any(image.has_alpha for image in summary.images if image.is_readable):
            return OutputFormatChoice.KEEP_ORIGINAL
        return OutputFormatChoice.JPG

    def select_output_format(
        self,
        summary: Optional[PreflightSummary] = None,
        available_choices: Optional[Sequence[OutputFormatChoice]] = None,
    ) -> OutputFormatChoice:
        self.rule(rich_escape(t("rule_format")))
        options = list(available_choices or OutputFormatChoice)
        if not options:
            raise CompressorError("no output format is available")
        recommended = (
            self.recommend_output_format(summary)
            if summary is not None
            else OutputFormatChoice.JPG
        )
        if recommended not in options:
            recommended = (
                OutputFormatChoice.KEEP_ORIGINAL
                if OutputFormatChoice.KEEP_ORIGINAL in options
                else options[0]
            )
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

    def select_resize_mode(self) -> ResizeMode:
        options = (
            ("1", ResizeMode.ALL, t("resize_all")),
            ("2", ResizeMode.OUTLIERS, t("resize_outliers")),
            ("3", ResizeMode.NONE, t("resize_none")),
        )
        if self.console is not None:
            table = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
            table.add_column("k", style="accent")
            table.add_column("v")
            for key, _, label in options:
                table.add_row(key, label)
            self.console.print(table)
            selected = Prompt.ask(
                t("resize_prompt"),
                choices=[key for key, _, _ in options],
                default="3",
                console=self.console,
            )
        else:
            print(f"\n{t('resize_prompt')}:")
            for key, _, label in options:
                print(f"  {key}) {label}")
            selected = input("[3]: ").strip() or "3"
        return next(mode for key, mode, _ in options if key == selected)

    def confirm_switch_to_all(self) -> bool:
        if self.console is not None:
            return Confirm.ask(
                t("resize_switch_all"),
                default=False,
                console=self.console,
            )
        print(t("resize_switch_all"))
        return input("[y/N]: ").strip().casefold() in ("y", "yes")

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
            table.add_row(t("res_resize_mode"), report.config.resize_mode.value)
            table.add_row(
                t("res_page_canvas"),
                str(report.config.page_canvas) if report.config.page_canvas else "—",
            )
            table.add_row(t("res_elapsed"), human_duration(report.total_elapsed_sec))
            table.add_row(t("res_compressed"), str(report.compressed_count))
            table.add_row(t("res_copied"), str(report.copied_count))
            table.add_row(t("res_failed"), str(report.failed_count))
            table.add_row(t("res_saved"), human_bytes(report.total_saved_bytes, binary=False))
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
                    Text(status_label(r.status), style=status_style),
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
                        status_label(r.status),
                        Text(friendly_failure_reason(r)),
                    )
                self.console.print(fail_table)
        else:
            print(f"  {t('res_strategy')}: {report.config.strategy.title}")
            print(f"  {t('res_format')}: {report.config.output_format.title}")
            print(f"  {t('res_resize_mode')}: {report.config.resize_mode.value}")
            print(
                f"  {t('res_page_canvas')}: "
                f"{report.config.page_canvas if report.config.page_canvas else '—'}"
            )
            print(f"  {t('res_elapsed')}: {human_duration(report.total_elapsed_sec)}")
            print(f"  {t('res_compressed')}: {report.compressed_count}")
            print(f"  {t('res_copied')}: {report.copied_count}")
            print(f"  {t('res_failed')}: {report.failed_count}")
            print(f"  {t('res_saved')}: {human_bytes(report.total_saved_bytes, binary=False)}")
            print(f"  {t('res_output')}: {report.config.output_dir}")
            for r in report.results:
                if r.status in fail_statuses:
                    note = friendly_failure_reason(r)
                else:
                    note = r.message
                print(
                    f"  - {r.source.name}: {status_label(r.status)} "
                    f"{human_bytes(r.original_bytes)} → {human_bytes(r.output_bytes)} | {note}"
                )
            if failed_rows:
                print()
                print(f"  === {t('fail_section_title')} ===")
                for r in failed_rows:
                    print(f"  ! {r.source.name}")
                    print(f"      {status_label(r.status)}: {friendly_failure_reason(r)}")

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
        if len(args) != 3:
            raise ValueError("invalid context manager exit")

    def add_task(self, description: str, total: Optional[float] = None) -> int:
        if total is not None and total < 0:
            raise ValueError("progress total cannot be negative")
        print(f"  >> {description}")
        return 0

    def update(self, task_id: int, **kwargs: Any) -> None:
        if task_id != 0:
            raise ValueError("unknown progress task")
        description = kwargs.get("description")
        if description:
            print(f"  .. {description}")


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
                self._relative_sort_key(image.path).casefold(),
                self._relative_sort_key(image.path),
            ),
        )
        try:
            for image in ordered:
                relative_parent = Path()
                if self.config.recursive:
                    try:
                        relative_parent = image.path.relative_to(
                            self.config.root_dir
                        ).parent
                    except ValueError:
                        relative_parent = Path()
                parent = self.config.output_dir / relative_parent
                parent.mkdir(parents=True, exist_ok=True)
                ext = resolve_output_codec(
                    image.path,
                    self.config.output_format,
                ).extension
                stem = self._budgeted_stem(
                    image.path.stem,
                    parent,
                    ext,
                )
                index = 1
                while True:
                    candidate = (
                        f"{stem}{ext}"
                        if index == 1
                        else f"{stem}_{index}{ext}"
                    )
                    folded = str(relative_parent / candidate).casefold()
                    index += 1
                    if folded in reserved:
                        continue
                    path = parent / candidate
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

    def _relative_sort_key(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.config.root_dir))
        except ValueError:
            return str(path)

    @staticmethod
    def _budgeted_stem(
        stem: str,
        parent: Path,
        extension: str,
    ) -> str:
        cleaned = safe_filename(stem)
        if os.name != "nt":
            return cleaned
        try:
            parent_length = len(str(parent.resolve()))
        except OSError:
            parent_length = len(str(parent.absolute()))
        reservation_name_length = len(
            ".jpeg-compressor-.reserve"
        ) + 32
        if (
            parent_length
            + 1
            + reservation_name_length
            > WINDOWS_PATH_BUDGET
        ):
            raise WorkspaceError(
                f"Output directory path is too long: {parent}"
            )
        available = (
            WINDOWS_PATH_BUDGET
            - parent_length
            - len(extension)
            - 12
        )
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:10]
        if available < len(digest) + 2:
            raise WorkspaceError(
                f"Output directory path is too long: {parent}"
            )
        if len(cleaned) <= available:
            return cleaned
        prefix = cleaned[: max(1, available - len(digest) - 1)].rstrip(" .")
        return f"{prefix or 'image'}_{digest}"

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
            try:
                age = time.time() - marker.stat().st_mtime
            except OSError:
                return False
            if age < STALE_MARKER_AGE_SEC:
                return False
            try:
                marker.unlink()
                return True
            except FileNotFoundError:
                return True
            except OSError:
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
        self.canvas_preparer = CanvasPreparer(config, logger)
        self.destination_paths: Dict[Path, Path] = {}

    def run(
        self,
        images: Sequence[ImageProbeResult],
        *,
        reuse_destinations: bool = False,
    ) -> List[ImageJobResult]:
        self.config.cancellation.raise_if_cancelled()
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        results: List[ImageJobResult] = []
        total = len(images)
        if total == 0:
            return results
        destinations: Dict[Path, OutputReservation] = {}
        retain_run_root = threading.Event()
        run_root = (
            self.config.output_dir
            / TEMP_WORKDIR_NAME
            / f"run_{uuid.uuid4().hex[:16]}"
        )
        try:
            ensure_path_budget(
                run_root / ".owner.json",
                label="Temporary run path",
            )
            self._reclaim_stale_runs(run_root.parent)
            if reuse_destinations:
                missing = [image.path for image in images if image.path not in self.destination_paths]
                if missing:
                    raise WorkspaceError("reprocessing destination plan is incomplete")
                destinations = {
                    image.path: OutputReservation(
                        self.destination_paths[image.path],
                        None,
                        None,
                    )
                    for image in images
                }
            else:
                destinations = OutputPlanner(self.config).plan(images)
                self.destination_paths.update(
                    {
                        source: reservation.path
                        for source, reservation in destinations.items()
                    }
                )
            run_root.mkdir(parents=True, exist_ok=True)
            (run_root / ".owner.json").write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "created_utc": datetime.now(
                            timezone.utc
                        ).isoformat(),
                        "cleanup_safe": False,
                    }
                ),
                encoding="utf-8",
            )
        except Exception:
            for reservation in destinations.values():
                reservation.release()
            self._cleanup_dir(run_root)
            try:
                run_root.parent.rmdir()
            except OSError:
                pass
            raise

        def mark_cleanup_safe() -> None:
            owner = run_root / ".owner.json"
            stage = run_root / ".owner.cleanup-safe"
            stage.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "created_utc": datetime.now(
                            timezone.utc
                        ).isoformat(),
                        "cleanup_safe": True,
                    }
                ),
                encoding="utf-8",
            )
            atomic_replace(stage, owner)

        def job(probe: ImageProbeResult) -> ImageJobResult:
            reservation = destinations[probe.path]
            work_dir: Optional[Path] = None
            result: Optional[ImageJobResult] = None
            try:
                self.config.cancellation.raise_if_cancelled()
                work_dir = self._work_dir_for(run_root, probe.path)
                work_dir.mkdir(parents=True, exist_ok=True)
                processing_probe = self.canvas_preparer.prepare(probe, work_dir)
                result = self.engine.process_image(
                    processing_probe,
                    reservation.path,
                    work_dir,
                )
                return result
            except ProcessTerminationError as exc:
                self.config.cancellation.cancel()
                retain_run_root.set()
                marker_error: Optional[OSError] = None
                try:
                    (run_root / UNCONFIRMED_PROCESS_MARKER).touch(
                        exist_ok=True
                    )
                except OSError as error:
                    marker_error = error
                    self.logger.error(
                        "Could not mark retained process workspace %s: %s",
                        run_root,
                        error,
                    )
                result = ImageJobResult(
                    source=probe.path,
                    status=ImageStatus.FAILED,
                    original_bytes=probe.size_bytes,
                    original_dimensions=probe.dimensions,
                    message="process_termination_unconfirmed",
                    error_detail=(
                        f"{exc}; marker_error={marker_error}"
                        if marker_error is not None
                        else str(exc)
                    ),
                    work_dir=work_dir,
                )
                exc.result = result
                raise
            except UserAbortError:
                raise
            except CompressorError as exc:
                result = ImageJobResult(
                    source=probe.path,
                    status=ImageStatus.FAILED,
                    original_bytes=probe.size_bytes,
                    original_dimensions=probe.dimensions,
                    message="canvas_preparation_failed",
                    error_detail=str(exc),
                    resized=probe.path in self.config.canvas_paths,
                    padded=probe.path in self.config.canvas_paths,
                )
                return result
            finally:
                reservation.release()
                if work_dir is not None:
                    keep = retain_run_root.is_set() or (
                        self.config.keep_temp_on_failure
                        and result is not None
                        and result.status
                        in (
                            ImageStatus.FAILED,
                            ImageStatus.SIZE_LIMIT_FAILED,
                        )
                    )
                    if keep:
                        if result is not None:
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
                    except ProcessTerminationError as exc:
                        if exc.result is not None:
                            results.append(exc.result)
                        raise
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
        except (
            KeyboardInterrupt,
            UserAbortError,
            ProcessTerminationError,
        ) as exc:
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
                (
                    "process termination could not be confirmed"
                    if isinstance(exc, ProcessTerminationError)
                    else "operation cancelled"
                ),
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
                    self._collect_completed_results(
                        future_map,
                        results,
                    )
                if shutdown_complete and not retain_run_root.is_set():
                    retained_failures = any(
                        result.work_dir is not None
                        for result in results
                    )
                    if retained_failures:
                        try:
                            mark_cleanup_safe()
                        except OSError as exc:
                            self.logger.warning(
                                "Could not mark retained workspace for stale cleanup %s: %s",
                                run_root,
                                exc,
                            )
                    else:
                        self._cleanup_dir(run_root)
                    parent = run_root.parent
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
            if shutdown_interrupted:
                raise BatchInterruptedError(
                    "operation cancelled",
                    results=results,
                )
        order = {img.path: i for i, img in enumerate(images)}
        results.sort(key=lambda r: order.get(r.source, 10**9))
        return results

    @staticmethod
    def _work_dir_for(run_root: Path, source: Path) -> Path:
        token = uuid.uuid4().hex[:10]
        stem = safe_filename(source.stem, max_length=80)
        if os.name == "nt":
            try:
                parent_length = len(str(run_root.resolve()))
            except OSError:
                parent_length = len(str(run_root.absolute()))
            workspace_tail = len(token) + 2
            candidate_tail = 1 + 24 + 1 + 16 + len(".webp")
            available = (
                WINDOWS_PATH_BUDGET
                - parent_length
                - workspace_tail
                - candidate_tail
            )
            if available < 12:
                raise WorkspaceError(
                    f"Temporary run directory path is too long: {run_root}"
                )
            if len(stem) > available:
                digest = hashlib.sha256(
                    stem.encode("utf-8")
                ).hexdigest()[:10]
                prefix = stem[: max(1, available - len(digest) - 1)].rstrip(
                    " ."
                )
                stem = f"{prefix or 'image'}_{digest}"
        path = run_root / f"{stem}_{token}"
        ensure_path_budget(path, label="Image workspace path")
        return path

    def _reclaim_stale_runs(self, parent: Path) -> None:
        if not parent.is_dir():
            return
        try:
            runs = list(parent.glob("run_*"))
        except OSError:
            return
        for run in runs:
            if (run / UNCONFIRMED_PROCESS_MARKER).exists():
                continue
            owner = run / ".owner.json"
            try:
                payload = json.loads(owner.read_text(encoding="utf-8"))
                pid = int(payload.get("pid", 0))
                cleanup_safe = payload.get("cleanup_safe") is True
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if (
                pid <= 0
                or not cleanup_safe
                or OutputPlanner._pid_is_running(pid)
            ):
                continue
            self._cleanup_dir(run)

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
            except ProcessTerminationError as exc:
                completed = exc.result
                if completed is None:
                    continue
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
        return self._run(interactive=True)

    def run_headless(
        self,
        namespace: argparse.Namespace,
    ) -> int:
        self.root = namespace.input.resolve()
        return self._run(interactive=False, namespace=namespace)

    @staticmethod
    def _merge_results(
        results: Sequence[ImageJobResult],
        replacements: Sequence[ImageJobResult],
        images: Sequence[ImageProbeResult],
    ) -> List[ImageJobResult]:
        replacement_map = {result.source: result for result in replacements}
        merged = [replacement_map.get(result.source, result) for result in results]
        order = {image.path: index for index, image in enumerate(images)}
        merged.sort(key=lambda result: order.get(result.source, 10**9))
        return merged

    @staticmethod
    def _remove_published_results(
        results: Sequence[ImageJobResult],
        output_dir: Path,
    ) -> None:
        for result in results:
            path = result.output_path
            if path is None or not is_within_directory(path, output_dir):
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise WorkspaceError(f"cannot remove prior run output {path}: {exc}") from exc
            result.output_path = None

    def _run_all_resize_mode(
        self,
        processor: BatchProcessor,
        images: Sequence[ImageProbeResult],
        config: RuntimeConfig,
        *,
        reuse_destinations: bool,
    ) -> List[ImageJobResult]:
        results = processor.run(images, reuse_destinations=reuse_destinations)
        while any(result.status == ImageStatus.SIZE_LIMIT_FAILED for result in results):
            if config.page_canvas is None:
                break
            reduced = PageCanvasPlanner.reduce_uniformly(
                config.page_canvas,
                config.minimum_page_side,
            )
            if reduced is None:
                break
            self._remove_published_results(results, config.output_dir)
            config.page_canvas = reduced
            config.uniform_reduction_applied = True
            config.reprocess_existing = True
            self.logger.info("Uniform page canvas reduction to %s", reduced)
            self.cli.print(
                f"[warning]{rich_escape(t('uniform_reduce', canvas=reduced))}[/warning]"
            )
            results = processor.run(images, reuse_destinations=True)
        return results

    def _run(
        self,
        *,
        interactive: bool,
        namespace: Optional[argparse.Namespace] = None,
    ) -> int:
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        cancellation = CancellationToken()
        try:
            if interactive:
                self.cli.select_language()
            else:
                if namespace is None:
                    raise CompressorError("headless arguments are missing")
                set_language(namespace.language)
            size_policy = (
                SizePolicy(
                    strict_max_bytes=namespace.max_bytes,
                    preferred_target_bytes=namespace.preferred_bytes,
                )
                if namespace is not None
                else SizePolicy()
            )
            self.cli.banner(size_policy)
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

        self.cli.show_environment(
            self.root,
            ffmpeg,
            ffprobe,
            locator.ffmpeg_version,
            locator.ffprobe_version,
            size_policy,
        )
        output_dir = (
            namespace.output.resolve()
            if namespace is not None
            else self.root / DEFAULT_OUTPUT_DIRNAME
        )
        log_path = output_dir / REPORT_LOG_NAME
        report_path = output_dir / REPORT_JSON_NAME
        try:
            ensure_path_budget(log_path, label="Log path")
            ensure_path_budget(report_path, label="Report path")
            ensure_path_budget(
                output_dir / f"report_{'0' * 16}.json",
                label="Report staging path",
            )
            ensure_path_budget(
                output_dir
                / TEMP_WORKDIR_NAME
                / f"run_{'0' * 16}"
                / ".owner.json",
                label="Temporary run path",
            )
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
        capabilities = BinaryLocator.probe_capabilities(ffmpeg, runner)
        max_pixels = (
            namespace.max_pixels
            if namespace is not None
            else DEFAULT_MAX_PIXELS
        )
        configure_pillow_pixel_limit(max_pixels)
        prober = ImageProber(
            ffprobe,
            runner,
            max_pixels=max_pixels,
        )
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
            summary = scanner.scan(
                max_workers=(
                    namespace.workers
                    if namespace is not None
                    else DEFAULT_MAX_WORKERS
                ),
                recursive=(
                    namespace.recursive
                    if namespace is not None
                    else False
                ),
            )
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
        available_codecs = available_output_codecs(capabilities)
        available_choices = [
            choice
            for choice in OutputFormatChoice
            if output_choice_available(choice, summary, available_codecs)
        ]
        if not available_choices:
            self.cli.print(
                f"[error]{rich_escape(t('unavailable_format', format='JPG/PNG/WEBP'))}[/error]"
            )
            return 2
        try:
            self.cli.show_preflight(summary)
            if interactive:
                strategy = self.cli.select_strategy(summary)
                self.logger.info("User selected strategy: %s", strategy.name)
                output_format = self.cli.select_output_format(
                    summary,
                    available_choices,
                )
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
                            t("parallel_workers"),
                            default=DEFAULT_MAX_WORKERS,
                            console=self.cli.console,
                        )
                        max_workers = clamp(int(max_workers), 1, 32)
                    except (ValueError, TypeError):
                        max_workers = DEFAULT_MAX_WORKERS
                else:
                    raw = input(
                        f"{t('parallel_workers')} [{DEFAULT_MAX_WORKERS}]: "
                    ).strip()
                    if raw.isdigit():
                        max_workers = clamp(int(raw), 1, 32)
            else:
                if namespace is None:
                    raise CompressorError("headless arguments are missing")
                strategy = CompressionStrategy[namespace.strategy]
                output_format = OutputFormatChoice[namespace.output_format]
                max_workers = namespace.workers
        except KeyboardInterrupt:
            cancellation.cancel()
            runner.cancel_all()
            raise UserAbortError("operation cancelled")
        if not output_choice_available(output_format, summary, available_codecs):
            self.cli.print(
                f"[error]{rich_escape(t('unavailable_format', format=output_format.name))}[/error]"
            )
            return 2
        page_plan: Optional[PageCanvasPlan] = None
        resize_mode = ResizeMode.NONE
        if interactive:
            try:
                page_plan = PageCanvasPlanner.create(summary.images)
            except CompressorError:
                self.cli.print(
                    f"[warning]{rich_escape(t('canvas_unavailable'))}[/warning]"
                )
            if page_plan and (page_plan.outliers or summary.over_limit_count):
                resize_mode = self.cli.select_resize_mode()
        else:
            if namespace is None:
                raise CompressorError("headless arguments are missing")
            resize_mode = ResizeMode(namespace.resize_mode)
            if resize_mode != ResizeMode.NONE:
                try:
                    page_plan = PageCanvasPlanner.create(
                        summary.images,
                        namespace.page_size,
                    )
                except CompressorError as exc:
                    self.cli.print(f"[error]{rich_escape(str(exc))}[/error]")
                    return 2
        if resize_mode != ResizeMode.NONE:
            if page_plan is None:
                self.cli.print(
                    f"[error]{rich_escape(t('canvas_unavailable'))}[/error]"
                )
                return 2
            if ImageCodec.PNG not in pillow_output_codecs():
                self.cli.print(
                    f"[error]{rich_escape(t('resize_requires_pillow'))}[/error]"
                )
                return 2
            canvas_message_key = (
                "page_size_provided"
                if page_plan.source == "user-provided"
                else "page_size_auto"
            )
            self.cli.print(
                f"[info]{rich_escape(t(canvas_message_key, canvas=page_plan.canvas))}[/info]"
            )
        if strategy == CompressionStrategy.COPY_ONLY_UNDER_LIMIT and resize_mode != ResizeMode.NONE:
            self.cli.print(
                f"[error]{rich_escape('COPY_ONLY_UNDER_LIMIT is incompatible with page resizing')}[/error]"
            )
            return 2
        readable_paths = frozenset(
            image.path for image in summary.images if image.is_readable
        )
        canvas_paths = (
            readable_paths
            if resize_mode == ResizeMode.ALL
            else page_plan.outliers
            if resize_mode == ResizeMode.OUTLIERS and page_plan is not None
            else frozenset()
        )
        minimum_page_side = (
            min(MIN_AUTOMATIC_PAGE_SIDE, min(page_plan.canvas.width, page_plan.canvas.height))
            if page_plan and page_plan.source == "user-provided"
            else MIN_AUTOMATIC_PAGE_SIDE
        )
        config = RuntimeConfig(
            root_dir=self.root,
            output_dir=output_dir,
            size_policy=size_policy,
            strategy=strategy,
            max_workers=max_workers,
            max_pixels=max_pixels,
            resize_mode=resize_mode,
            page_canvas_source=page_plan.source if page_plan else None,
            page_canvas=page_plan.canvas if page_plan else None,
            initial_page_canvas=page_plan.canvas if page_plan else None,
            minimum_page_side=minimum_page_side,
            allow_upscale=bool(namespace is not None and namespace.allow_upscale),
            canvas_paths=canvas_paths,
            recursive=bool(namespace is not None and namespace.recursive),
            copy_under_limit=True,
            overwrite_output=bool(
                namespace is not None and namespace.overwrite
            ),
            dry_run=bool(namespace is not None and namespace.dry_run),
            strip_metadata=(
                not namespace.keep_metadata
                if namespace is not None
                else True
            ),
            preserve_icc_profile=(
                not namespace.discard_icc
                if namespace is not None
                else True
            ),
            progressive_jpeg=(
                not namespace.baseline_jpeg
                if namespace is not None
                else True
            ),
            include_convertibles=True,
            output_format=output_format,
            runtime_metadata={
                "ffmpeg_version": locator.ffmpeg_version,
                "ffprobe_version": locator.ffprobe_version,
                "ffmpeg_capabilities": capabilities.to_dict(),
            },
            cancellation=cancellation,
        )
        if config.progressive_jpeg and ImageCodec.JPG not in pillow_output_codecs() and (
            output_format == OutputFormatChoice.JPG
            or (
                output_format == OutputFormatChoice.KEEP_ORIGINAL
                and any(image.is_jpeg for image in summary.images if image.is_readable)
            )
        ):
            self.cli.print(
                f"[warning]{rich_escape(t('baseline_jpeg_fallback'))}[/warning]"
            )
        ffmpeg_enc = FFmpegEncoder(
            ffmpeg,
            runner,
            capabilities=capabilities,
        )
        pillow_enc: Optional[PillowEncoder] = None
        if PILLOW_AVAILABLE:
            try:
                pillow_enc = PillowEncoder(
                    cancellation=cancellation,
                    max_pixels=config.max_pixels,
                )
            except CompressorError:
                pillow_enc = None
        dual = DualEncoder(ffmpeg_enc, pillow_enc)
        verifier = OutputVerifier(
            ffmpeg,
            ffprobe,
            runner,
            max_pixels=config.max_pixels,
        )
        engine = StrategyEngine(
            dual,
            self.logger,
            config,
            verifier=verifier,
        )
        processor = BatchProcessor(config, engine, self.logger, self.cli)
        self.cli.rule(rich_escape(t("rule_processing")))
        interrupted = False
        try:
            if config.resize_mode == ResizeMode.ALL:
                results = self._run_all_resize_mode(
                    processor,
                    summary.images,
                    config,
                    reuse_destinations=False,
                )
            else:
                results = processor.run(summary.images)
            if config.resize_mode == ResizeMode.OUTLIERS:
                native_failures = {
                    result.source
                    for result in results
                    if result.status == ImageStatus.SIZE_LIMIT_FAILED
                    and result.source not in config.canvas_paths
                }
                if native_failures:
                    config.canvas_paths = frozenset(
                        set(config.canvas_paths) | native_failures
                    )
                    config.reprocess_existing = True
                    retry_images = [
                        image
                        for image in summary.images
                        if image.path in native_failures
                    ]
                    replacements = processor.run(
                        retry_images,
                        reuse_destinations=True,
                    )
                    results = self._merge_results(
                        results,
                        replacements,
                        summary.images,
                    )
                affected_failures = [
                    result
                    for result in results
                    if result.source in config.canvas_paths
                    and result.status
                    in (ImageStatus.SIZE_LIMIT_FAILED, ImageStatus.FAILED)
                ]
                if affected_failures:
                    self.cli.print(
                        f"[warning]{rich_escape(t('outlier_resize_failed'))}[/warning]"
                    )
                    if interactive and self.cli.confirm_switch_to_all():
                        self._remove_published_results(results, config.output_dir)
                        config.resize_mode = ResizeMode.ALL
                        config.page_canvas = config.initial_page_canvas
                        config.canvas_paths = readable_paths
                        config.uniform_reduction_applied = False
                        config.reprocess_existing = True
                        results = self._run_all_resize_mode(
                            processor,
                            summary.images,
                            config,
                            reuse_destinations=True,
                        )
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
            report_path,
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


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jpeg_compressor")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--language", choices=("en", "vi"), default="en")
    parser.add_argument(
        "--strategy",
        choices=tuple(strategy.name for strategy in CompressionStrategy),
        default=CompressionStrategy.AGGRESSIVE_ADAPTIVE.name,
    )
    parser.add_argument(
        "--output-format",
        choices=tuple(choice.name for choice in OutputFormatChoice),
        default=OutputFormatChoice.KEEP_ORIGINAL.name,
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument(
        "--preferred-bytes",
        type=int,
        default=EFFECTIVE_TARGET_BYTES,
    )
    parser.add_argument("--max-pixels", type=int, default=DEFAULT_MAX_PIXELS)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument(
        "--resize-mode",
        choices=tuple(mode.value for mode in ResizeMode),
        default=ResizeMode.NONE.value,
    )
    parser.add_argument("--page-size", type=parse_page_size)
    parser.add_argument("--allow-upscale", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--keep-metadata", action="store_true")
    parser.add_argument("--discard-icc", action="store_true")
    parser.add_argument("--baseline-jpeg", action="store_true")
    return parser


def parse_headless_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = build_argument_parser()
    namespace = parser.parse_args(list(argv))
    namespace.workers = clamp(namespace.workers, 1, 32)
    if not namespace.input.is_dir():
        parser.error(f"input directory does not exist: {namespace.input}")
    if namespace.max_pixels <= 0:
        parser.error("--max-pixels must be positive")
    if namespace.resize_mode == ResizeMode.NONE.value and namespace.page_size is not None:
        parser.error("--page-size requires --resize-mode all or outliers")
    if namespace.resize_mode == ResizeMode.NONE.value and namespace.allow_upscale:
        parser.error("--allow-upscale requires --resize-mode all or outliers")
    if (
        namespace.strategy == CompressionStrategy.COPY_ONLY_UNDER_LIMIT.name
        and namespace.resize_mode != ResizeMode.NONE.value
    ):
        parser.error("COPY_ONLY_UNDER_LIMIT cannot be used with page resizing")
    try:
        SizePolicy(
            strict_max_bytes=namespace.max_bytes,
            preferred_target_bytes=namespace.preferred_bytes,
        )
    except ValueError as exc:
        parser.error(str(exc))
    return namespace


def _pause_if_windows_double_click(*, interrupted: bool = False) -> None:
    if sys.platform != "win32" or interrupted:
        return
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            print()
            input(t("press_enter"))
    except EOFError:
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    exit_code = 1
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        app = Application()
        exit_code = (
            app.run_headless(parse_headless_args(arguments))
            if arguments
            else app.run()
        )
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
        logging.getLogger("jpeg_compressor").error("%s", exc)
        print(t("error_prefix", exc=exc), file=sys.stderr)
    except Exception as exc:
        exit_code = 1
        logging.getLogger("jpeg_compressor").exception("Unexpected failure")
        print(t("error_prefix", exc=exc), file=sys.stderr)
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
