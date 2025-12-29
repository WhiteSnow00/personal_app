import sys
import os
import subprocess
import re
import zipfile
import shutil
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Set
from datetime import datetime
from collections import defaultdict
from threading import Lock
from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import Qt

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

IMG_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".heic",
    ".heif",
    ".avif",
}

QUALITY_PRESETS = {
    "jpg": {"q": 2},
    "jpeg": {"q": 2},
    "png": {"compression": 1},
    "heic": {"crf": 18},
    "heif": {"crf": 18},
    "webp": {"lossless": False, "q": 95},
}

DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
MAX_LOG_LINES = 1000
MAX_PATH_LENGTH = 250


class Styles:
    GROUP_BOX = """
        QGroupBox {
            font-weight: bold;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            margin-top: 10px;
            padding-top: 10px;
            background: white;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 10px 0 10px;
            color: #333;
        }
    """

    HEADER = """
        QWidget {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #667eea, stop:1 #764ba2);
            border-radius: 10px;
            padding: 15px;
            margin-bottom: 10px;
        }
    """

    BTN_PRIMARY = """
        QPushButton {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #4fc3f7, stop:1 #29b6f6);
            color: white;
            padding: 12px 30px;
            font-size: 14px;
            font-weight: bold;
            border-radius: 8px;
            border: none;
        }
        QPushButton:hover:enabled {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #29b6f6, stop:1 #0288d1);
        }
        QPushButton:disabled {
            background: #cccccc;
            color: #666666;
        }
    """

    BTN_DANGER = """
        QPushButton {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #ef5350, stop:1 #e53935);
            color: white;
            padding: 12px 30px;
            font-size: 14px;
            font-weight: bold;
            border-radius: 8px;
            border: none;
        }
        QPushButton:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #e53935, stop:1 #c62828);
        }
    """

    BTN_SUCCESS = """
        QPushButton {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #66bb6a, stop:1 #43a047);
            color: white;
            padding: 12px 30px;
            font-size: 14px;
            font-weight: bold;
            border-radius: 8px;
            border: none;
        }
        QPushButton:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #43a047, stop:1 #2e7d32);
        }
    """

    BTN_WARNING = """
        QPushButton {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #ff9a00, stop:1 #ff6200);
            color: white;
            padding: 10px 20px;
            font-size: 13px;
            font-weight: bold;
            border-radius: 6px;
            border: none;
        }
        QPushButton:hover {
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #ffaa00, stop:1 #ff7200);
        }
    """

    BTN_WHITE = """
        QPushButton {
            background-color: white;
            color: #667eea;
            font-size: 14px;
            font-weight: bold;
            padding: 12px 24px;
            border-radius: 8px;
            border: none;
        }
        QPushButton:hover {
            background-color: #f0f0f0;
        }
        QPushButton:pressed {
            background-color: #e0e0e0;
        }
    """

    PROGRESS_BAR = """
        QProgressBar {
            height: 28px;
            background-color: #e0e0e0;
            border-radius: 14px;
            text-align: center;
            font-weight: bold;
            color: white;
        }
        QProgressBar::chunk {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #667eea, stop:1 #764ba2);
            border-radius: 14px;
        }
    """

    LOG_TEXT = """
        QTextEdit {
            background-color: #2d2d30;
            color: #d4d4d4;
            font-family: 'Consolas', 'Courier New', monospace;
            font-size: 12px;
            border: 2px solid #3e3e42;
            border-radius: 8px;
            padding: 10px;
        }
    """

    FOLDER_LABEL = """
        color: white;
        font-size: 14px;
        padding: 10px;
        background: rgba(255, 255, 255, 0.1);
        border-radius: 5px;
    """

    TASK_LABEL = """
        padding: 15px;
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #f8f9fa, stop:1 #e9ecef);
        border-radius: 8px;
        color: #495057;
        font-size: 13px;
    """


def format_size(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def validate_path(path: Path) -> Tuple[bool, str]:
    try:
        if not path.exists():
            return False, "File không tồn tại"
        if not path.is_file():
            return False, "Không phải file"
        path_str = str(path.resolve())
        if sys.platform == "win32" and len(path_str) > MAX_PATH_LENGTH:
            return False, f"Đường dẫn quá dài ({len(path_str)} > {MAX_PATH_LENGTH})"
        return True, ""
    except OSError as e:
        return False, str(e)


class ImageProcessor:
    def __init__(self):
        script_dir = Path(__file__).parent
        self.ffmpeg_path = script_dir / "ffmpeg.exe"
        if not self.ffmpeg_path.exists():
            raise FileNotFoundError(
                f"Không tìm thấy ffmpeg.exe trong {script_dir}\n"
                f"Vui lòng copy ffmpeg.exe vào cùng thư mục với file Python"
            )

    def run_cmd(self, cmd: List[str]) -> Tuple[int, str, str]:
        if cmd[0] == "ffmpeg":
            cmd[0] = str(self.ffmpeg_path)
        p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        out, err = p.communicate()
        return p.returncode, out.strip(), err.strip()

    def get_image_info(self, path: Path) -> Dict:
        info = {"path": str(path), "size": path.stat().st_size}

        if HAS_PILLOW:
            try:
                with Image.open(path) as img:
                    info["width"], info["height"] = img.size
                    info["has_alpha"] = img.mode in ("RGBA", "LA", "PA") or (
                        img.mode == "P" and "transparency" in img.info
                    )
                return info
            except Exception:
                pass

        code, _, err = self.run_cmd(["ffmpeg", "-i", str(path), "-hide_banner"])
        if err:
            resolution_match = re.search(r"(\d+)x(\d+)", err)
            if resolution_match:
                info["width"] = int(resolution_match.group(1))
                info["height"] = int(resolution_match.group(2))
            pix_fmt_match = re.search(
                r"\s+(yuva\w*|rgba|bgra|argb|ya\d+|gray\w*a)", err.lower()
            )
            info["has_alpha"] = bool(pix_fmt_match)
            if path.suffix.lower() in [".png", ".webp"]:
                info["has_alpha"] = True
        return info

    def build_ffmpeg_cmd(self, src: Path, dst: Path, fmt: str, info: Dict) -> List[str]:
        preset = QUALITY_PRESETS.get(fmt.lower(), {})
        has_alpha = info.get("has_alpha", False)
        supports_alpha = fmt.lower() in {"png", "webp", "tiff"}

        if has_alpha and not supports_alpha:
            width = info.get("width", DEFAULT_WIDTH)
            height = info.get("height", DEFAULT_HEIGHT)
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c=white:s={width}x{height}:d=1",
                "-i", str(src),
                "-filter_complex", "[0:v][1:v]overlay=shortest=1,format=yuv420p",
            ]
        else:
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]

        format_params = {
            "jpg": lambda: ["-q:v", str(preset.get("q", 2))],
            "jpeg": lambda: ["-q:v", str(preset.get("q", 2))],
            "png": lambda: [
                "-compression_level", str(preset.get("compression", 1)),
                "-pred", "mixed",
            ],
            "heic": lambda: [
                "-c:v", "libx265", "-preset", "medium",
                "-x265-params", f"crf={preset.get('crf', 18)}",
                "-tag:v", "hvc1",
            ],
            "heif": lambda: [
                "-c:v", "libx265", "-preset", "medium",
                "-x265-params", f"crf={preset.get('crf', 18)}",
                "-tag:v", "hvc1",
            ],
            "webp": lambda: ["-lossless", "0", "-quality", str(preset.get("q", 95))],
        }

        params = format_params.get(fmt.lower(), lambda: [])()
        cmd.extend(params)
        cmd.append(str(dst))
        return cmd


@dataclass
class ConvertOptions:
    out_formats: Set[str] = field(default_factory=set)


@dataclass
class ConversionStats:
    total_files: int = 0
    processed: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    total_size_before: int = 0
    total_size_after: int = 0
    format_counts: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    start_time: datetime = field(default_factory=datetime.now)


class ScanWorker(QtCore.QThread):
    finished = QtCore.pyqtSignal(list)
    progress = QtCore.pyqtSignal(str)
    error = QtCore.pyqtSignal(str)

    def __init__(self, src_dir: Path):
        super().__init__()
        self.src_dir = src_dir
        self._stopped = False
        self._lock = Lock()

    def stop(self):
        with self._lock:
            self._stopped = True

    def is_stopped(self) -> bool:
        with self._lock:
            return self._stopped

    def run(self):
        try:
            files = []
            for p in self.src_dir.iterdir():
                if self.is_stopped():
                    break
                if p.is_file() and p.suffix.lower() in IMG_EXTS:
                    files.append(p)
                    self.progress.emit(f"Tìm thấy: {p.name}")
            self.finished.emit(files)
        except Exception as e:
            self.error.emit(str(e))
            self.finished.emit([])


class ConvertWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, int, str)
    finished = QtCore.pyqtSignal(dict)
    error_msg = QtCore.pyqtSignal(str)
    stats_updated = QtCore.pyqtSignal(dict)
    task_update = QtCore.pyqtSignal(str)
    error_details = QtCore.pyqtSignal(list)

    def __init__(self, src_root: Path, files: List[Path], opts: ConvertOptions):
        super().__init__()
        self.src_root = src_root
        self.files = files
        self.opts = opts
        self._stopped = False
        self._lock = Lock()
        self.stats = ConversionStats(total_files=len(files))

    def stop(self):
        with self._lock:
            self._stopped = True

    def is_stopped(self) -> bool:
        with self._lock:
            return self._stopped

    def run(self):
        try:
            processor = ImageProcessor()
            self._process_files(processor)
        except FileNotFoundError as e:
            self.error_msg.emit(f"❌ Lỗi: {str(e)}")
        except Exception as e:
            self.error_msg.emit(f"❌ Lỗi không xác định: {str(e)}")
        finally:
            if self.stats.errors:
                self.error_details.emit(self.stats.errors)
            self.finished.emit(self._get_stats_dict())

    def _process_files(self, processor: ImageProcessor):
        output_dirs = {
            fmt: self.src_root / fmt.lower() for fmt in self.opts.out_formats
        }
        for out_dir in output_dirs.values():
            out_dir.mkdir(parents=True, exist_ok=True)

        for idx, file in enumerate(self.files):
            if self.is_stopped():
                break

            self.stats.processed += 1
            rel_path = (
                file.relative_to(self.src_root)
                if file.is_relative_to(self.src_root)
                else Path(file.name)
            )
            self.task_update.emit(f"Đang xử lý: {rel_path.name}")
            self.progress.emit(
                idx, len(self.files), f"[{idx+1}/{len(self.files)}] {rel_path.name}"
            )

            try:
                is_valid, error_reason = validate_path(file)
                if not is_valid:
                    self.stats.skipped += 1
                    self.stats.errors.append(f"{file.name}: Bỏ qua - {error_reason}")
                    continue

                info = processor.get_image_info(file)
                self.stats.total_size_before += info.get("size", 0)

                for fmt in self.opts.out_formats:
                    out_dir = output_dirs[fmt]
                    out_path = out_dir / rel_path.parent
                    out_path.mkdir(parents=True, exist_ok=True)
                    out_file = out_path / f"{file.stem}.{fmt}"

                    cmd = processor.build_ffmpeg_cmd(file, out_file, fmt, info)
                    code, out, err = processor.run_cmd(cmd)

                    if code == 0:
                        self.stats.success += 1
                        if out_file.exists():
                            self.stats.total_size_after += out_file.stat().st_size
                        self.progress.emit(
                            idx, len(self.files), f"✓ Hoàn thành: {file.name} → {fmt}"
                        )
                    else:
                        self.stats.failed += 1
                        error_detail = err if err else out if out else "Unknown error"
                        self.stats.errors.append(f"{file.name} → {fmt}: {error_detail}")
                        self.error_msg.emit(
                            f"✗ Lỗi: {file.name} → {fmt}: {error_detail}"
                        )
            except Exception as e:
                self.stats.failed += 1
                self.stats.errors.append(f"{file.name}: {str(e)}")
                self.error_msg.emit(f"✗ Ngoại lệ: {file.name}: {str(e)}")

            self.stats_updated.emit(self._get_stats_dict())

    def _get_stats_dict(self) -> dict:
        return {
            "total": self.stats.total_files,
            "processed": self.stats.processed,
            "success": self.stats.success,
            "failed": self.stats.failed,
            "skipped": self.stats.skipped,
            "size_before": self.stats.total_size_before,
            "size_after": self.stats.total_size_after,
            "duration": (datetime.now() - self.stats.start_time).total_seconds(),
        }


class ZipWorker(QtCore.QThread):
    progress = QtCore.pyqtSignal(int, int, str)
    finished = QtCore.pyqtSignal(dict)
    error = QtCore.pyqtSignal(str)

    def __init__(self, files: List[Path], src_dir: Path, zip_path: Path):
        super().__init__()
        self.files = files
        self.src_dir = src_dir
        self.zip_path = zip_path
        self._stopped = False
        self._lock = Lock()

    def stop(self):
        with self._lock:
            self._stopped = True

    def is_stopped(self) -> bool:
        with self._lock:
            return self._stopped

    def run(self):
        files_zipped = []
        deleted_count = 0
        failed_deletions = []

        try:
            with zipfile.ZipFile(self.zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for i, file in enumerate(self.files):
                    if self.is_stopped():
                        self._cleanup_zip()
                        self.finished.emit({
                            "success": False,
                            "cancelled": True,
                            "message": "Đã hủy trong quá trình nén"
                        })
                        return

                    rel_path = (
                        file.relative_to(self.src_dir)
                        if file.is_relative_to(self.src_dir)
                        else file.name
                    )
                    zipf.write(file, rel_path)
                    files_zipped.append(file)
                    self.progress.emit(
                        i + 1, len(self.files) + 1, f"Đang nén: {file.name}"
                    )

            self.progress.emit(
                len(self.files), len(self.files) + 1, "Đang xóa ảnh gốc..."
            )

            for file in files_zipped:
                if self.is_stopped():
                    self.finished.emit({
                        "success": True,
                        "cancelled_during_delete": True,
                        "zip_path": str(self.zip_path),
                        "zip_size": self.zip_path.stat().st_size if self.zip_path.exists() else 0,
                        "files_zipped": len(files_zipped),
                        "files_deleted": deleted_count,
                        "files_remaining": len(files_zipped) - deleted_count,
                        "failed_deletions": failed_deletions,
                        "message": f"Đã hủy khi xóa. ZIP đầy đủ, còn {len(files_zipped) - deleted_count} file chưa xóa"
                    })
                    return

                try:
                    if file.exists():
                        os.remove(file)
                        deleted_count += 1
                except Exception as e:
                    failed_deletions.append((file.name, str(e)))

            zip_size = self.zip_path.stat().st_size if self.zip_path.exists() else 0

            self.finished.emit({
                "success": True,
                "zip_path": str(self.zip_path),
                "zip_size": zip_size,
                "files_zipped": len(files_zipped),
                "files_deleted": deleted_count,
                "failed_deletions": failed_deletions,
            })

        except Exception as e:
            self._cleanup_zip()
            self.error.emit(str(e))
            self.finished.emit({"success": False, "error": str(e)})

    def _cleanup_zip(self):
        if self.zip_path.exists():
            try:
                os.remove(self.zip_path)
            except:
                pass


class StatsWidget(QtWidgets.QGroupBox):
    def __init__(self):
        super().__init__("📊 Thống kê đầu vào")
        self.setStyleSheet(Styles.GROUP_BOX)
        self.setup_ui()

    def setup_ui(self):
        layout = QtWidgets.QGridLayout(self)
        layout.setSpacing(10)
        self.labels = {}
        stats_info = [
            ("total", "Tổng số ảnh:"),
            ("formats", "Định dạng chính:"),
            ("distribution", "Phân bố:"),
            ("size", "Dung lượng gốc:"),
            ("output_size", "Dung lượng xuất:"),
            ("saved", "Tiết kiệm:"),
            ("success", "Thành công:"),
            ("failed", "Thất bại:"),
            ("progress", "Tiến độ:"),
        ]
        for i, (key, text) in enumerate(stats_info):
            label = QtWidgets.QLabel(text)
            label.setStyleSheet("color: #666; font-weight: 500;")
            value = QtWidgets.QLabel("—")
            value.setStyleSheet("color: #333; font-weight: bold;")
            layout.addWidget(label, i, 0)
            layout.addWidget(value, i, 1)
            self.labels[key] = value

    def update_scan_stats(self, files: List[Path]):
        if not files:
            self.labels["total"].setText("0")
            self.labels["formats"].setText("—")
            self.labels["distribution"].setText("—")
            self.labels["size"].setText("—")
            return

        format_counts = defaultdict(int)
        total_size = 0
        for f in files:
            ext = f.suffix.lower().lstrip(".")
            format_counts[ext] += 1
            try:
                total_size += f.stat().st_size
            except OSError:
                pass

        main_format = (
            max(format_counts.items(), key=lambda x: x[1])[0] if format_counts else ""
        )
        distribution = ", ".join(
            f"{k}: {v}"
            for k, v in sorted(format_counts.items(), key=lambda x: (-x[1], x[0]))
        )

        self.labels["total"].setText(str(len(files)))
        self.labels["formats"].setText(main_format.upper())
        self.labels["distribution"].setText(distribution)
        self.labels["size"].setText(format_size(total_size))

    def update_conversion_stats(self, stats: dict):
        self.labels["success"].setText(f"{stats.get('success', 0)}")
        self.labels["failed"].setText(f"{stats.get('failed', 0)}")
        self.labels["progress"].setText(
            f"{stats.get('processed', 0)}/{stats.get('total', 0)}"
        )
        if stats.get("size_after", 0) > 0:
            self.labels["output_size"].setText(format_size(stats["size_after"]))
            saved = stats.get("size_before", 0) - stats.get("size_after", 0)
            if saved > 0:
                percent = (saved / stats.get("size_before", 1)) * 100
                self.labels["saved"].setText(f"{format_size(saved)} ({percent:.1f}%)")
            else:
                self.labels["saved"].setText("—")


class TaskWidget(QtWidgets.QGroupBox):
    def __init__(self):
        super().__init__("⚡ Tác vụ hiện tại")
        self.setStyleSheet(Styles.GROUP_BOX)
        self.setup_ui()

    def setup_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        self.task_label = QtWidgets.QLabel("Chờ lệnh...")
        self.task_label.setStyleSheet(Styles.TASK_LABEL)
        self.task_label.setWordWrap(True)
        self.task_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.task_label)

    def update_task(self, text: str):
        self.task_label.setText(text)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Trình Chuyển Đổi Định Dạng Ảnh")
        self.setMinimumSize(1000, 750)
        self.src_dir = None
        self.files = []
        self.worker = None
        self.scan_worker = None
        self.zip_worker = None
        self.error_list = []
        self.is_processing = False
        self.converted_successfully = False
        self.setStyleSheet("QMainWindow { background-color: #f5f5f5; }")
        self.setup_ui()

    def setup_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)

        header_widget = QtWidgets.QWidget()
        header_widget.setStyleSheet(Styles.HEADER)
        header_layout = QtWidgets.QHBoxLayout(header_widget)

        self.btn_pick = QtWidgets.QPushButton("📁 CHỌN THƯ MỤC")
        self.btn_pick.setStyleSheet(Styles.BTN_WHITE)

        self.lbl_folder = QtWidgets.QLabel("Chưa chọn thư mục")
        self.lbl_folder.setStyleSheet(Styles.FOLDER_LABEL)

        header_layout.addWidget(self.btn_pick)
        header_layout.addWidget(self.lbl_folder, 1)
        main_layout.addWidget(header_widget)

        content_layout = QtWidgets.QHBoxLayout()
        left_panel = QtWidgets.QVBoxLayout()

        format_group = QtWidgets.QGroupBox("🎯 Định dạng xuất")
        format_group.setStyleSheet(Styles.GROUP_BOX)
        format_layout = QtWidgets.QVBoxLayout(format_group)

        self.format_checks = {}
        format_icons = {"JPG": "🏆", "PNG": "🔷", "WEBP": "🌐", "HEIC": "🌈"}
        format_adv = {
            "JPG": "Phổ biến, nhẹ",
            "PNG": "Nặng hơn, giữ chất lượng tốt",
            "WEBP": "Định dạng ảnh của Web",
            "HEIC": "Hình ảnh hiệu suất cao",
        }
        formats = ["JPG", "PNG", "WEBP", "HEIC"]

        for fmt in formats:
            icon = format_icons.get(fmt, "🖼️")
            adv = format_adv.get(fmt, "")
            cb = QtWidgets.QCheckBox(f"{icon} {fmt} — {adv}")
            cb.setStyleSheet("QCheckBox { padding: 8px; font-size: 13px; color: #333; }")
            cb.stateChanged.connect(self.on_format_changed)
            format_layout.addWidget(cb)
            self.format_checks[fmt.lower()] = cb

        left_panel.addWidget(format_group)

        self.output_group = QtWidgets.QGroupBox("🎉 Kết quả xuất")
        self.output_group.setStyleSheet(Styles.GROUP_BOX)
        output_layout = QtWidgets.QFormLayout(self.output_group)
        output_layout.setSpacing(12)

        self.output_labels = {}
        output_info = [
            ("status", "Trạng thái:"),
            ("output_dir", "Thư mục xuất:"),
            ("output_size", "Dung lượng:"),
            ("files_count", "Số file:"),
            ("time_taken", "Thời gian:"),
            ("compression", "Nén:"),
        ]

        for key, text in output_info:
            title_label = QtWidgets.QLabel(text)
            title_label.setStyleSheet("color: #666; font-weight: 500;")
            value_label = QtWidgets.QLabel("—")
            value_label.setWordWrap(True)
            value_label.setStyleSheet("color: #333; font-weight: bold;")
            output_layout.addRow(title_label, value_label)
            self.output_labels[key] = value_label

        self.output_labels["status"].setText("Chưa chuyển đổi")
        left_panel.addWidget(self.output_group)
        left_panel.addStretch()

        right_panel = QtWidgets.QVBoxLayout()
        self.stats_widget = StatsWidget()
        right_panel.addWidget(self.stats_widget)

        self.task_widget = TaskWidget()
        right_panel.addWidget(self.task_widget)
        right_panel.addStretch()

        content_layout.addLayout(left_panel, 1)
        content_layout.addLayout(right_panel, 2)
        main_layout.addLayout(content_layout)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setStyleSheet(Styles.PROGRESS_BAR)
        main_layout.addWidget(self.progress)

        self.log = QtWidgets.QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet(Styles.LOG_TEXT)
        main_layout.addWidget(self.log, 1)

        button_layout = QtWidgets.QHBoxLayout()

        self.btn_errors = QtWidgets.QPushButton("📋 Xem chi tiết lỗi")
        self.btn_errors.setStyleSheet(Styles.BTN_WARNING)
        self.btn_errors.setVisible(False)

        self.btn_zip = QtWidgets.QPushButton("🗜️ NÉN ZIP")
        self.btn_zip.setStyleSheet(Styles.BTN_SUCCESS)
        self.btn_zip.setVisible(False)

        button_layout.addWidget(self.btn_errors)
        button_layout.addStretch()
        button_layout.addWidget(self.btn_zip)

        self.btn_start = QtWidgets.QPushButton("▶ BẮT ĐẦU CHUYỂN ĐỔI")
        self.btn_start.setStyleSheet(Styles.BTN_PRIMARY)
        self.btn_start.setEnabled(False)

        self.btn_stop = QtWidgets.QPushButton("❌ THOÁT")
        self.btn_stop.setStyleSheet(Styles.BTN_DANGER)

        button_layout.addWidget(self.btn_start)
        button_layout.addWidget(self.btn_stop)
        main_layout.addLayout(button_layout)

        self.btn_pick.clicked.connect(self.pick_folder)
        self.btn_start.clicked.connect(self.start_convert)
        self.btn_stop.clicked.connect(self.stop_or_exit)
        self.btn_errors.clicked.connect(self.show_error_details)
        self.btn_zip.clicked.connect(self.create_zip)

        self.check_ffmpeg_availability()

    def check_ffmpeg_availability(self):
        script_dir = Path(__file__).parent
        ffmpeg_path = script_dir / "ffmpeg.exe"
        if not ffmpeg_path.exists():
            self.append_log("❌ Không tìm thấy ffmpeg.exe")
            self.append_log(f"📁 Vui lòng copy ffmpeg.exe vào: {script_dir}")
            self.btn_pick.setEnabled(False)
        else:
            self.append_log("✅ Đã tìm thấy ffmpeg.exe")
            if HAS_PILLOW:
                self.append_log("✅ Pillow đã được cài đặt (tăng tốc đọc metadata)")
            else:
                self.append_log("⚠️ Pillow chưa cài (pip install Pillow để tăng tốc)")
            self.append_log("📂 Hãy chọn thư mục chứa ảnh để bắt đầu")

    def append_log(self, msg: str):
        self.log.append(msg)
        doc = self.log.document()
        if doc.blockCount() > MAX_LOG_LINES:
            cursor = QtGui.QTextCursor(doc)
            cursor.movePosition(QtGui.QTextCursor.Start)
            cursor.movePosition(
                QtGui.QTextCursor.Down,
                QtGui.QTextCursor.KeepAnchor,
                doc.blockCount() - MAX_LOG_LINES
            )
            cursor.removeSelectedText()
        self.log.ensureCursorVisible()

    def on_format_changed(self):
        any_checked = any(cb.isChecked() for cb in self.format_checks.values())
        self.btn_start.setEnabled(
            any_checked and self.src_dir is not None and len(self.files) > 0
        )

    def pick_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Chọn thư mục chứa ảnh"
        )
        if not folder:
            return
        self.reset_ui()
        self.src_dir = Path(folder)
        self.lbl_folder.setText(str(self.src_dir))
        self.scan_folder()

    def reset_ui(self):
        self.progress.setValue(0)
        self.stats_widget.update_scan_stats([])
        self.stats_widget.update_conversion_stats({
            "success": 0,
            "failed": 0,
            "processed": 0,
            "total": 0,
            "size_before": 0,
            "size_after": 0,
        })
        self.stats_widget.labels["output_size"].setText("—")
        self.stats_widget.labels["saved"].setText("—")
        self.output_labels["status"].setText("Chưa chuyển đổi")
        self.output_labels["output_dir"].setText("—")
        self.output_labels["output_size"].setText("—")
        self.output_labels["files_count"].setText("—")
        self.output_labels["time_taken"].setText("—")
        self.output_labels["compression"].setText("—")
        self.task_widget.update_task("Chờ lệnh...")
        self.btn_errors.setVisible(False)
        self.btn_zip.setVisible(False)
        self.error_list = []
        self.converted_successfully = False

    def scan_folder(self):
        if not self.src_dir:
            return

        self.log.clear()
        self.append_log(f"📂 Đang quét thư mục: {self.src_dir}")
        self.task_widget.update_task("Đang quét thư mục...")
        self.btn_pick.setEnabled(False)

        self.scan_worker = ScanWorker(self.src_dir)
        self.scan_worker.finished.connect(self.on_scan_finished)
        self.scan_worker.progress.connect(lambda msg: self.task_widget.update_task(msg))
        self.scan_worker.error.connect(lambda err: self.append_log(f"❌ Lỗi: {err}"))
        self.scan_worker.start()

    def on_scan_finished(self, files: List[Path]):
        self.btn_pick.setEnabled(True)
        self.files = files
        self.task_widget.update_task("Chờ lệnh...")

        if not self.files:
            self.append_log("⚠️ Không tìm thấy ảnh trong thư mục này")
            return

        self.stats_widget.update_scan_stats(self.files)
        self.append_log(f"✅ Tìm thấy {len(self.files)} ảnh")

        format_counts = defaultdict(int)
        for f in self.files:
            ext = f.suffix.lower().lstrip(".")
            format_counts[ext] += 1

        self.append_log("\n📊 Chi tiết định dạng:")
        for ext, count in sorted(format_counts.items(), key=lambda x: -x[1]):
            percent = (count / len(self.files)) * 100
            self.append_log(f"  • {ext.upper()}: {count} file ({percent:.1f}%)")

        self.on_format_changed()

    def start_convert(self):
        if not self.src_dir or not self.files:
            QtWidgets.QMessageBox.warning(
                self, "Cảnh báo", "Vui lòng chọn thư mục chứa ảnh"
            )
            return

        selected_formats = {
            fmt for fmt, cb in self.format_checks.items() if cb.isChecked()
        }
        if not selected_formats:
            QtWidgets.QMessageBox.warning(
                self, "Cảnh báo", "Vui lòng chọn ít nhất một định dạng xuất"
            )
            return

        input_formats = defaultdict(int)
        for f in self.files:
            ext = f.suffix.lower().lstrip(".")
            if ext == "jpeg":
                ext = "jpg"
            input_formats[ext] += 1

        main_format = (
            max(input_formats.items(), key=lambda x: x[1])[0] if input_formats else None
        )
        total_files = len(self.files)

        same_format_conversions = []
        for fmt in selected_formats:
            fmt_lower = fmt.lower()
            if fmt_lower == "jpg" or fmt_lower == "jpeg":
                jpg_count = input_formats.get("jpg", 0) + input_formats.get("jpeg", 0)
                if jpg_count > 0:
                    same_format_conversions.append((fmt.upper(), jpg_count))
            elif input_formats.get(fmt_lower, 0) > 0:
                same_format_conversions.append((fmt.upper(), input_formats[fmt_lower]))

        should_warn = False
        for fmt, count in same_format_conversions:
            fmt_lower = fmt.lower()
            is_main = (fmt_lower == main_format) or (
                fmt_lower == "jpg" and main_format in ["jpg", "jpeg"]
            )
            is_majority = (count / total_files) > 0.5
            if is_main or is_majority:
                should_warn = True
                break

        if should_warn and same_format_conversions:
            warning_msg = "⚠️ Phát hiện chuyển đổi cùng định dạng chính:\n\n"
            for fmt, count in same_format_conversions:
                percent = (count / total_files) * 100
                warning_msg += (
                    f"• {fmt} → {fmt}: {count}/{total_files} file ({percent:.0f}%)\n"
                )
            warning_msg += (
                "\nĐiều này có thể không cần thiết và làm giảm chất lượng ảnh.\n"
                "Bạn có muốn tiếp tục không?"
            )

            reply = QtWidgets.QMessageBox.question(
                self,
                "Cảnh báo chuyển đổi cùng định dạng",
                warning_msg,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply == QtWidgets.QMessageBox.No:
                self.append_log("🚫 Người dùng đã hủy chuyển đổi")
                return

        existing_dirs = []
        for fmt in selected_formats:
            output_dir = self.src_dir / fmt.lower()
            if output_dir.exists() and any(output_dir.iterdir()):
                existing_dirs.append(fmt.upper())

        if existing_dirs:
            reply = QtWidgets.QMessageBox.question(
                self,
                "Thư mục đã tồn tại",
                f"Các thư mục sau đã tồn tại và có file bên trong:\n"
                f"{', '.join(existing_dirs)}\n\nBạn có muốn ghi đè không?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply == QtWidgets.QMessageBox.No:
                return

            for fmt in selected_formats:
                output_dir = self.src_dir / fmt.lower()
                if output_dir.exists():
                    try:
                        shutil.rmtree(output_dir)
                        self.append_log(f"🗑️ Đã xóa thư mục cũ: {output_dir.name}")
                    except Exception as e:
                        self.append_log(
                            f"❌ Không thể xóa thư mục {output_dir.name}: {str(e)}"
                        )
                        return

        opts = ConvertOptions(out_formats=selected_formats)

        self.append_log("\n" + "=" * 50)
        self.append_log("🚀 BẮT ĐẦU CHUYỂN ĐỔI")
        self.append_log(f"📁 Thư mục: {self.src_dir}")
        self.append_log(f"📷 Số lượng: {len(self.files)} ảnh")
        self.append_log(f"📤 Định dạng xuất: {', '.join(selected_formats).upper()}")
        self.append_log("=" * 50 + "\n")

        self.progress.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_pick.setEnabled(False)
        self.btn_errors.setVisible(False)
        self.btn_zip.setVisible(False)
        self.error_list = []
        self.is_processing = True
        self.btn_stop.setText("⏹ DỪNG")
        self.btn_stop.setEnabled(True)

        self.output_labels["status"].setText("🔄 Đang chuyển đổi...")
        self.output_labels["output_dir"].setText(
            f"{', '.join(selected_formats).lower()}/"
        )

        self.worker = ConvertWorker(self.src_dir, self.files, opts)
        self.worker.progress.connect(self.on_progress)
        self.worker.error_msg.connect(self.on_error)
        self.worker.finished.connect(self.on_finished)
        self.worker.error_details.connect(self.on_error_details)
        self.worker.stats_updated.connect(self.stats_widget.update_conversion_stats)
        self.worker.task_update.connect(self.task_widget.update_task)
        self.worker.start()

    def stop_or_exit(self):
        if self.is_processing:
            if self.worker and self.worker.isRunning():
                self.worker.stop()
                self.append_log("\n⏹️ Đang dừng...")
            if self.zip_worker and self.zip_worker.isRunning():
                self.zip_worker.stop()
                self.append_log("\n⏹️ Đang dừng nén...")
        else:
            reply = QtWidgets.QMessageBox.question(
                self,
                "Xác nhận thoát",
                "Bạn có chắc chắn muốn thoát ứng dụng?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if reply == QtWidgets.QMessageBox.Yes:
                QtWidgets.QApplication.quit()

    @QtCore.pyqtSlot(int, int, str)
    def on_progress(self, done: int, total: int, msg: str):
        percent = int((done / max(1, total)) * 100)
        self.progress.setValue(percent)
        self.append_log(msg)

    @QtCore.pyqtSlot(str)
    def on_error(self, msg: str):
        self.append_log(f"<span style='color: red;'>{msg}</span>")

    @QtCore.pyqtSlot(list)
    def on_error_details(self, errors: list):
        self.error_list = errors
        if errors:
            self.btn_errors.setVisible(True)

    def show_error_details(self):
        if not self.error_list:
            return

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Chi tiết lỗi")
        dialog.setMinimumSize(600, 400)

        layout = QtWidgets.QVBoxLayout(dialog)
        text_edit = QtWidgets.QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setPlainText("\n".join(self.error_list))
        layout.addWidget(text_edit)

        close_btn = QtWidgets.QPushButton("Đóng")
        close_btn.clicked.connect(dialog.close)
        layout.addWidget(close_btn)

        dialog.exec_()

    @QtCore.pyqtSlot(dict)
    def on_finished(self, stats: dict):
        self.is_processing = False
        self.btn_start.setEnabled(True)
        self.btn_pick.setEnabled(True)
        self.btn_stop.setText("❌ THOÁT")
        self.btn_stop.setEnabled(True)
        self.progress.setValue(100)
        self.task_widget.update_task("Hoàn thành!")

        if stats["success"] > 0:
            self.output_labels["status"].setText("✅ Hoàn thành")
            self.output_labels["output_size"].setText(
                format_size(stats.get("size_after", 0))
            )
            self.output_labels["files_count"].setText(str(stats["success"]))
            self.output_labels["time_taken"].setText(
                f"{stats.get('duration', 0):.1f} giây"
            )
            self.converted_successfully = True
            self.btn_zip.setVisible(True)

            if stats.get("size_before", 0) > 0:
                compression = (
                    (stats["size_before"] - stats.get("size_after", 0))
                    / stats["size_before"]
                ) * 100
                self.output_labels["compression"].setText(f"{compression:.1f}%")
            else:
                self.output_labels["compression"].setText("—")
        else:
            self.output_labels["status"].setText("❌ Thất bại")
            self.converted_successfully = False

        self.append_log("\n" + "=" * 50)
        self.append_log("✅ HOÀN THÀNH CHUYỂN ĐỔI")
        self.append_log("📊 Kết quả:")
        self.append_log(f"  • Thành công: {stats['success']} tác vụ")
        self.append_log(f"  • Thất bại: {stats['failed']} tác vụ")
        self.append_log(f"  • Bỏ qua: {stats['skipped']} tác vụ")

        if stats.get("size_before", 0) > 0:
            self.append_log(
                f"  • Dung lượng gốc: {format_size(stats['size_before'])}"
            )
        if stats.get("size_after", 0) > 0:
            self.append_log(
                f"  • Dung lượng xuất: {format_size(stats['size_after'])}"
            )

        if stats["failed"] > 0:
            self.append_log(
                f"\n⚠️ Có {stats['failed']} lỗi. Click nút '📋 Xem log lỗi' để xem chi tiết."
            )

        size_saved = stats["size_before"] - stats["size_after"]
        if size_saved > 0:
            percent_saved = (size_saved / stats["size_before"]) * 100
            self.append_log(
                f"  • Tiết kiệm: {format_size(size_saved)} ({percent_saved:.1f}%)"
            )

        duration = stats["duration"]
        self.append_log(f"  • Thời gian: {duration:.1f} giây")
        self.append_log("=" * 50)

    def create_zip(self):
        if not self.src_dir or not self.files:
            QtWidgets.QMessageBox.warning(self, "Cảnh báo", "Không có file để nén")
            return

        reply = QtWidgets.QMessageBox.warning(
            self,
            "⚠️ Xác nhận nén và xóa",
            f"CẢNH BÁO: Thao tác này KHÔNG THỂ HOÀN TÁC!\n\n"
            f"1. Nén {len(self.files)} ảnh thành file ZIP\n"
            f"2. XÓA VĨNH VIỄN tất cả ảnh gốc sau khi nén\n\n"
            f"Nếu dừng giữa chừng:\n"
            f"• Dừng khi đang nén → ZIP bị xóa, ảnh gốc còn nguyên\n"
            f"• Dừng khi đang xóa → ZIP đầy đủ, một số ảnh đã xóa\n\n"
            f"Bạn có chắc chắn muốn tiếp tục?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply == QtWidgets.QMessageBox.No:
            return

        parent_dir = self.src_dir.parent
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = self.src_dir / f"{parent_dir.name}.zip"

        counter = 1
        while zip_path.exists():
            zip_path = self.src_dir / f"{self.src_dir.name}_images_{timestamp}_{counter}.zip"
            counter += 1

        self.is_processing = True
        self.btn_stop.setText("⏹ DỪNG")
        self.btn_start.setEnabled(False)
        self.btn_pick.setEnabled(False)
        self.btn_zip.setEnabled(False)
        self.progress.setValue(0)
        self.task_widget.update_task("Đang nén ZIP...")

        self.zip_worker = ZipWorker(self.files, self.src_dir, zip_path)
        self.zip_worker.progress.connect(self.on_zip_progress)
        self.zip_worker.finished.connect(self.on_zip_finished)
        self.zip_worker.error.connect(lambda e: self.append_log(f"❌ Lỗi: {e}"))
        self.zip_worker.start()

    @QtCore.pyqtSlot(int, int, str)
    def on_zip_progress(self, done: int, total: int, msg: str):
        percent = int((done / max(1, total)) * 100)
        self.progress.setValue(percent)
        self.task_widget.update_task(msg)

    @QtCore.pyqtSlot(dict)
    def on_zip_finished(self, result: dict):
        self.is_processing = False
        self.btn_stop.setText("❌ THOÁT")
        self.btn_start.setEnabled(True)
        self.btn_pick.setEnabled(True)
        self.progress.setValue(100)
        self.task_widget.update_task("Hoàn thành!")

        if result.get("cancelled"):
            self.btn_zip.setEnabled(True)
            self.append_log(f"❌ {result.get('message', 'Đã hủy')}")
            QtWidgets.QMessageBox.information(
                self, "Đã hủy", result.get("message", "Đã hủy thao tác")
            )
            return

        if result.get("cancelled_during_delete"):
            self.files = [f for f in self.files if f.exists()]
            self.btn_zip.setVisible(False)
            self.stats_widget.update_scan_stats(self.files)

            message = result.get("message", "")
            self.append_log(f"⚠️ {message}")
            QtWidgets.QMessageBox.warning(
                self,
                "Dừng khi đang xóa",
                f"{message}\n\nFile ZIP vẫn chứa đầy đủ ảnh."
            )
            return

        if not result.get("success", False):
            self.btn_zip.setEnabled(True)
            QtWidgets.QMessageBox.critical(
                self, "Lỗi", f"Không thể hoàn thành: {result.get('error', 'Unknown')}"
            )
            return

        self.files = [f for f in self.files if f.exists()]
        self.btn_zip.setVisible(False)
        self.converted_successfully = False

        zip_path = Path(result["zip_path"])
        zip_size = result["zip_size"]
        files_zipped = result["files_zipped"]
        files_deleted = result["files_deleted"]
        failed_deletions = result.get("failed_deletions", [])

        message = (
            f"✅ Hoàn thành!\n\n"
            f"📦 File ZIP: {zip_path.name}\n"
            f"💾 Dung lượng: {format_size(zip_size)}\n"
            f"🗜️ Đã nén: {files_zipped} ảnh\n"
            f"🗑️ Đã xóa: {files_deleted} ảnh gốc"
        )
        if failed_deletions:
            message += f"\n⚠️ Không thể xóa: {len(failed_deletions)} file"

        QtWidgets.QMessageBox.information(self, "Nén và xóa hoàn tất", message)

        self.append_log("\n" + "=" * 50)
        self.append_log("✅ NÉN VÀ XÓA HOÀN TẤT")
        self.append_log(f"📦 File ZIP: {zip_path}")
        self.append_log(f"💾 Dung lượng: {format_size(zip_size)}")
        self.append_log(f"🗜️ Đã nén: {files_zipped} ảnh")
        self.append_log(f"🗑️ Đã xóa: {files_deleted} ảnh gốc")

        if failed_deletions:
            self.append_log(f"\n⚠️ Không thể xóa {len(failed_deletions)} file:")
            for name, error in failed_deletions[:5]:
                self.append_log(f"  • {name}: {error}")
            if len(failed_deletions) > 5:
                self.append_log(f"  ... và {len(failed_deletions) - 5} file khác")

        self.append_log("=" * 50)

        if not self.files:
            self.stats_widget.update_scan_stats([])
        else:
            self.stats_widget.update_scan_stats(self.files)


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()