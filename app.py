"""
屏幕翻译器 — 独立版（不依赖 Hermes）

用法:
    python app.py                      # 启动 GUI
    python app.py --cli "Hello world"  # 命令行自测翻译
    python app.py --set-key sk-xxx     # 写入 API Key

原理:
    截屏 → 送给 LLM（视觉）→ LLM 返回译文 + 位置坐标
        → 把译文按坐标原位贴回屏幕（像素级覆盖，半透明，原文透出）

无 OCR 依赖 —— 文字识别和定位全部由多模态大模型完成。
"""

import argparse
import base64
import ctypes
import hashlib
import io
import os
import sys
import time

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QRect
from PyQt5.QtGui import QPainter, QColor, QPen, QFont, QFontMetrics, QCursor
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QSlider, QComboBox,
    QVBoxLayout, QHBoxLayout,
)

import mss
from PIL import Image

import config as cfg_mod
import engine as engine_mod


# ═══════════════════════════════════════════════════════════════
#  DPI 感知（必须最先执行，否则截图/坐标全部错位）
# ═══════════════════════════════════════════════════════════════

def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


# ═══════════════════════════════════════════════════════════════
#  让译文层不被截进屏幕
# ═══════════════════════════════════════════════════════════════

WDA_EXCLUDEFROMCAPTURE = 0x00000011
WDA_MONITOR = 0x00000001


def exclude_from_capture(hwnd):
    """
    把窗口标记为「不参与屏幕捕获」。

    这是解决「自反馈幻觉」的关键：
    译文贴上去之后，下一次截图会把译文也拍进去，模型就会把译文
    当成原文再翻一遍（实测出现过幽灵行）。
    排除后，截图里永远只有原始界面，模型不会误认。

    需要 Windows 10 2004+。失败就退回提示词方案（仍能工作）。
    """
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(
            ctypes.c_void_p(int(hwnd)), WDA_EXCLUDEFROMCAPTURE)
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
#  屏幕捕获
# ═══════════════════════════════════════════════════════════════

class ScreenCapture:
    """mss 截图。DPI 感知必须在进程启动时设置好，否则坐标错位。"""

    def __init__(self):
        self._sct = mss.mss()

    def grab(self, x, y, w, h):
        mon = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}
        raw = self._sct.grab(mon)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    def close(self):
        try:
            self._sct.close()
        except Exception:
            pass


def to_jpeg_b64(img, max_dim=1400, quality=78):
    """缩放 + JPEG 编码。坐标归一化到 0-1000，所以缩放不影响定位精度。"""
    if max(img.size) > max_dim:
        ratio = max_dim / float(max(img.size))
        img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


# ═══════════════════════════════════════════════════════════════
#  翻译结果块
# ═══════════════════════════════════════════════════════════════

class TextBlock:
    """屏幕绝对坐标的文字块。"""

    __slots__ = ("text", "x", "y", "w", "h")

    def __init__(self, text, x, y, w, h):
        self.text = text
        self.x = int(x)
        self.y = int(y)
        self.w = int(w)
        self.h = int(h)

    def __repr__(self):
        return "TextBlock(%r, %d,%d %dx%d)" % (self.text[:20], self.x, self.y, self.w, self.h)


def boxes_to_blocks(boxes, region_x, region_y, region_w, region_h):
    """
    LLM 返回的 0-1000 归一化坐标 → 屏幕绝对坐标。

    注意：截图时可能做过缩放（max_dim），但归一化坐标与缩放无关，
    直接按 region 尺寸换算即可。
    """
    blocks = []
    for b in boxes:
        zh = b.get("zh", "").strip()
        if not zh:
            continue
        x = region_x + b["x"] / 1000.0 * region_w
        y = region_y + b["y"] / 1000.0 * region_h
        w = b["w"] / 1000.0 * region_w
        h = b["h"] / 1000.0 * region_h
        if w < 4 or h < 4:
            continue
        blocks.append(TextBlock(zh, x, y, w, h))
    return blocks


# ═══════════════════════════════════════════════════════════════
#  翻译线程
# ═══════════════════════════════════════════════════════════════

class TranslateWorker(QThread):
    """后台：捕获 → LLM 视觉翻译 → 发回带坐标的译文块。"""

    blocks_ready = pyqtSignal(list)     # [TextBlock]
    plain_ready = pyqtSignal(str)       # 整图翻译的纯文本
    status_update = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, translator, parent=None):
        super().__init__(parent)
        self.translator = translator
        self._running = False
        self._region = (0, 0, 400, 300)
        self._source = "auto"
        self._target = "zh"
        self._pixel_mode = True
        self.capture = None
        self._last_hash = None

    def configure(self, region, source, target, pixel_mode=True):
        self._region = region
        self._source = source
        self._target = target
        self._pixel_mode = pixel_mode
        self._last_hash = None   # 强制重翻

    def stop(self):
        self._running = False

    def run(self):
        self._running = True
        try:
            self.capture = ScreenCapture()
        except Exception as e:
            self.error_occurred.emit("截屏失败：%s" % e)
            return

        while self._running:
            try:
                x, y, w, h = self._region
                if w < 40 or h < 25:
                    time.sleep(0.5)
                    continue

                img = self.capture.grab(x, y, w, h)

                # 变化检测：内容没变就不重复请求 API
                hsh = hashlib.md5(img.tobytes()).hexdigest()
                if hsh == self._last_hash:
                    time.sleep(0.6)
                    continue
                self._last_hash = hsh

                self.status_update.emit("翻译中…")
                self._process(img, x, y, w, h)
            except Exception as e:
                self.error_occurred.emit("%s: %s" % (type(e).__name__, e))
                time.sleep(2)
            time.sleep(0.4)

    def _process(self, img, ox, oy, ow, oh):
        b64 = to_jpeg_b64(img)

        if self._pixel_mode:
            boxes = self.translator.translate_image_boxes(b64, self._source, self._target)
            if boxes:
                blocks = boxes_to_blocks(boxes, ox, oy, ow, oh)
                self.blocks_ready.emit(blocks)
                self.status_update.emit("像素级翻译 · %d 块" % len(blocks))
                return
            # 没拿到结构化结果 → 退回整图模式
            self.status_update.emit("未定位到文字，转整图模式…")

        text = self.translator.translate_image_lines(b64, self._source, self._target)
        self.plain_ready.emit("\n".join(text))
        self.status_update.emit("整图翻译 · %d 行" % len(text))


# ═══════════════════════════════════════════════════════════════
#  捕获区域
# ═══════════════════════════════════════════════════════════════

HANDLE_SIZE = 12
HANDLE_MARGIN = 6


class CaptureRegion(QWidget):
    region_changed = pyqtSignal()

    (DIR_NONE, DIR_TOP, DIR_BOTTOM, DIR_LEFT, DIR_RIGHT,
     DIR_TOP_LEFT, DIR_TOP_RIGHT, DIR_BOTTOM_LEFT, DIR_BOTTOM_RIGHT) = range(9)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setGeometry(200, 200, 640, 420)

        self._resize_dir = self.DIR_NONE
        self._drag_start = None
        self._geo_start = None
        self._min_size = 120

        self._handles = {}
        for d in [self.DIR_TOP, self.DIR_BOTTOM, self.DIR_LEFT, self.DIR_RIGHT,
                  self.DIR_TOP_LEFT, self.DIR_TOP_RIGHT,
                  self.DIR_BOTTOM_LEFT, self.DIR_BOTTOM_RIGHT]:
            h = QWidget(self)
            h.setObjectName("resize_handle")
            h.setFixedSize(HANDLE_SIZE, HANDLE_SIZE)
            h.setCursor(self._handle_cursor(d))
            h.installEventFilter(self)
            self._handles[d] = h
        self._update_handle_positions()

    def _handle_cursor(self, d):
        return {
            self.DIR_TOP: Qt.SizeVerCursor, self.DIR_BOTTOM: Qt.SizeVerCursor,
            self.DIR_LEFT: Qt.SizeHorCursor, self.DIR_RIGHT: Qt.SizeHorCursor,
            self.DIR_TOP_LEFT: Qt.SizeFDiagCursor, self.DIR_BOTTOM_RIGHT: Qt.SizeFDiagCursor,
            self.DIR_TOP_RIGHT: Qt.SizeBDiagCursor, self.DIR_BOTTOM_LEFT: Qt.SizeBDiagCursor,
        }.get(d, Qt.ArrowCursor)

    def _update_handle_positions(self):
        w, h, s, m = self.width(), self.height(), HANDLE_SIZE, HANDLE_MARGIN
        pos = {
            self.DIR_TOP: (w // 2 - s // 2, -m),
            self.DIR_BOTTOM: (w // 2 - s // 2, h - s + m),
            self.DIR_LEFT: (-m, h // 2 - s // 2),
            self.DIR_RIGHT: (w - s + m, h // 2 - s // 2),
            self.DIR_TOP_LEFT: (-m, -m), self.DIR_TOP_RIGHT: (w - s + m, -m),
            self.DIR_BOTTOM_LEFT: (-m, h - s + m), self.DIR_BOTTOM_RIGHT: (w - s + m, h - s + m),
        }
        for d, (hx, hy) in pos.items():
            self._handles[d].move(hx, hy)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_handle_positions()

    def eventFilter(self, obj, event):
        from PyQt5.QtCore import QEvent
        direction = None
        for d, handle in self._handles.items():
            if obj is handle:
                direction = d
                break
        if direction is not None:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._resize_dir = direction
                self._drag_start = QCursor.pos()
                self._geo_start = self.geometry()
                return True
            if event.type() == QEvent.MouseButtonRelease:
                self._resize_dir = self.DIR_NONE
                self.region_changed.emit()
                return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._resize_dir = self.DIR_NONE
            self._drag_start = e.globalPos()
            self._geo_start = self.geometry()

    def mouseMoveEvent(self, e):
        if self._drag_start is None:
            return
        delta = e.globalPos() - self._drag_start
        geo = self._geo_start
        if self._resize_dir == self.DIR_NONE:
            self.move(geo.x() + delta.x(), geo.y() + delta.y())
        else:
            self._apply_resize(delta, geo)

    def mouseReleaseEvent(self, e):
        self._resize_dir = self.DIR_NONE
        self._drag_start = None
        self.region_changed.emit()

    def _apply_resize(self, delta, geo):
        x, y, w, h = geo.x(), geo.y(), geo.width(), geo.height()
        d, mn = self._resize_dir, self._min_size
        if d in (self.DIR_TOP, self.DIR_TOP_LEFT, self.DIR_TOP_RIGHT):
            nh = max(mn, h - delta.y()); y = y + h - nh; h = nh
        if d in (self.DIR_BOTTOM, self.DIR_BOTTOM_LEFT, self.DIR_BOTTOM_RIGHT):
            h = max(mn, h + delta.y())
        if d in (self.DIR_LEFT, self.DIR_TOP_LEFT, self.DIR_BOTTOM_LEFT):
            nw = max(mn, w - delta.x()); x = x + w - nw; w = nw
        if d in (self.DIR_RIGHT, self.DIR_TOP_RIGHT, self.DIR_BOTTOM_RIGHT):
            w = max(mn, w + delta.x())
        self.setGeometry(x, y, w, h)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(30, 30, 30, 90))
        p.setPen(QPen(QColor(0, 200, 255, 220), 2))
        p.drawRect(self.rect().adjusted(1, 1, -1, -1))
        p.setPen(QPen(QColor(0, 200, 255, 255), 3))
        cl = 20
        for (cx, cy, dx1, dy1, dx2, dy2) in [
            (0, 0, 1, 0, 0, 1), (self.width(), 0, -1, 0, 0, 1),
            (0, self.height(), 1, 0, 0, -1), (self.width(), self.height(), -1, 0, 0, -1)]:
            p.drawLine(cx, cy, cx + dx1 * cl, cy + dy1 * cl)
            p.drawLine(cx, cy, cx + dx2 * cl, cy + dy2 * cl)
        p.setPen(QColor(0, 200, 255, 200))
        p.setFont(QFont("Consolas", 9))
        p.drawText(self.rect().adjusted(10, 10, -10, -10),
                   Qt.AlignTop | Qt.AlignLeft | Qt.TextSingleLine,
                   "翻译区域  %d×%d" % (self.width(), self.height()))
        p.setPen(QColor(200, 200, 200, 140))
        p.setFont(QFont("Microsoft YaHei", 8))
        p.drawText(self.rect().adjusted(10, 28, -10, -10),
                   Qt.AlignTop | Qt.AlignLeft | Qt.TextSingleLine,
                   "拖动移动 · 边缘手柄缩放 · Ctrl+H 隐藏/显示")


# ═══════════════════════════════════════════════════════════════
#  像素级覆盖层
# ═══════════════════════════════════════════════════════════════

class PixelOverlay(QWidget):
    """
    译文贴在原文原位。
    半透明底 + 红字，原文能透出来 —— 方便肉眼校对，不遮挡上下文。
    """

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)   # 不挡鼠标
        self._blocks = []
        self._plain = ""
        self._status = ""
        self.hide()

    def showEvent(self, event):
        super().showEvent(event)
        # 让译文层不出现在截图里 —— 防止模型把译文当原文再翻一遍
        exclude_from_capture(self.winId())

    def set_blocks(self, blocks):
        self._blocks = blocks
        self._plain = ""
        self.update()

    def set_plain(self, text):
        self._plain = text
        self._blocks = []
        self.update()

    def set_status(self, text):
        self._status = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        self.paint_with(p)
        p.end()

    def paint_with(self, p):
        """实际绘制逻辑 —— 与 paintEvent 分离，便于离线渲染验证。"""
        if not self._blocks and not self._plain and not self._status:
            return
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)

        if self._blocks:
            for block in self._blocks:
                self._draw_block(p, block)
        elif self._plain:
            p.fillRect(self.rect(), QColor(20, 20, 20, 170))
            p.setPen(QColor(255, 255, 255, 235))
            p.setFont(QFont("Microsoft YaHei", 12))
            p.drawText(self.rect().adjusted(12, 12, -12, -12), Qt.TextWordWrap, self._plain)
        elif self._status:
            p.fillRect(self.rect(), QColor(20, 20, 20, 140))
            p.setPen(QColor(255, 200, 0, 220))
            p.setFont(QFont("Microsoft YaHei", 11))
            p.drawText(self.rect().adjusted(12, 12, -12, -12),
                       Qt.AlignCenter | Qt.TextWordWrap, self._status)

    def _draw_block(self, p, block):
        """
        半透明底色 + 红字。
        底色 alpha 160 —— 原文能隐约透出，不至于完全看不见。
        框只比原文大 2px，避免压到相邻内容。

        注意：block 里存的是**屏幕绝对坐标**，而 QPainter 在 widget 上
        的坐标系原点是 widget 左上角 —— 必须减去 widget 原点，
        否则会叠加一层偏移（实际会画到两倍距离的位置）。
        """
        text = block.text
        font = self._fit_font(block.h, text)
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(text)
        th = fm.height()

        # 绝对坐标 → widget 相对坐标
        ox = self.geometry().x()
        oy = self.geometry().y()

        pad = 2
        x = block.x - ox - pad
        y = block.y - oy - pad
        w = max(block.w + pad * 2, tw + pad * 2)
        h = max(block.h + pad * 2, th + pad)

        p.fillRect(QRect(x, y, w, h), QColor(255, 255, 255, 160))
        p.setPen(QPen(QColor(200, 0, 0, 210), 1))
        p.drawRect(QRect(x, y, w, h))

        p.setPen(QColor(190, 0, 0))
        p.setFont(font)
        p.drawText(QRect(x + 2, y, w - 4, h), Qt.AlignLeft | Qt.AlignVCenter, text)

    @staticmethod
    def _fit_font(orig_h, text):
        """按原文字高度选字号，上限 9pt，保证塞得进原位。"""
        px = max(8, int(orig_h * 0.8))
        pt = max(6, min(9, int(round(px * 0.75))))
        return QFont("Microsoft YaHei", pt)


# ═══════════════════════════════════════════════════════════════
#  控制面板
# ═══════════════════════════════════════════════════════════════

LANG_PAIRS = [
    ("auto→zh", "自动→中文"),
    ("en→zh", "英文→中文"),
    ("zh→en", "中文→英文"),
    ("ja→zh", "日文→中文"),
    ("ko→zh", "韩文→中文"),
    ("fr→zh", "法文→中文"),
    ("de→zh", "德文→中文"),
    ("es→zh", "西文→中文"),
    ("ru→zh", "俄文→中文"),
    ("en→ja", "英文→日文"),
    ("en→ko", "英文→韩文"),
]


class ControlPanel(QWidget):
    def __init__(self, translator):
        super().__init__()
        self.setWindowTitle("屏幕翻译器 — 独立版")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._drag_pos = None

        self.capture_region = CaptureRegion()
        self.overlay = PixelOverlay()
        self.worker = TranslateWorker(translator)
        self.worker.blocks_ready.connect(self._on_blocks)
        self.worker.plain_ready.connect(self._on_plain)
        self.worker.status_update.connect(self._on_status)
        self.worker.error_occurred.connect(self._on_error)

        self._setup_ui()
        self.capture_region.region_changed.connect(self._on_region_changed)
        self.capture_region.show()
        self.overlay.show()

    # ── UI ──
    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(9)

        title = QLabel("🌐 屏幕翻译器 <span style='color:#666;font-size:11px'>独立版 · LLM 视觉</span>")
        title.setStyleSheet("color:#00c8ff;font-size:15px;font-weight:bold;background:transparent;")
        lay.addWidget(title)

        r1 = QHBoxLayout()
        l1 = QLabel("翻译方向:")
        l1.setStyleSheet("color:#ccc;background:transparent;")
        self.lang_combo = QComboBox()
        for key, label in LANG_PAIRS:
            self.lang_combo.addItem(label, key)
        self.lang_combo.setStyleSheet(self._combo_css())
        r1.addWidget(l1); r1.addWidget(self.lang_combo); r1.addStretch()
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        l2 = QLabel("刷新间隔:")
        l2.setStyleSheet("color:#ccc;background:transparent;")
        self.interval_slider = QSlider(Qt.Horizontal)
        self.interval_slider.setRange(1, 15)
        self.interval_slider.setValue(3)
        self.interval_slider.setStyleSheet("""
            QSlider::groove:horizontal{background:#444;height:6px;border-radius:3px;}
            QSlider::handle:horizontal{background:#00c8ff;width:16px;height:16px;margin:-5px 0;border-radius:8px;}
        """)
        self.interval_label = QLabel("3 秒")
        self.interval_label.setStyleSheet("color:#aaa;min-width:35px;background:transparent;")
        self.interval_slider.valueChanged.connect(
            lambda v: self.interval_label.setText("%d 秒" % v))
        r2.addWidget(l2); r2.addWidget(self.interval_slider); r2.addWidget(self.interval_label)
        lay.addLayout(r2)

        r3 = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("像素级（原位贴字）", True)
        self.mode_combo.addItem("整图翻译", False)
        self.mode_combo.setStyleSheet(self._combo_css())
        r3.addWidget(self.mode_combo); r3.addStretch()
        lay.addLayout(r3)

        r4 = QHBoxLayout()
        self.btn_start = QPushButton("▶ 开始翻译")
        self.btn_start.setStyleSheet(self._btn_css("#006633", "#008844"))
        self.btn_start.clicked.connect(self._toggle)

        self.btn_hide = QPushButton("👁 隐藏")
        self.btn_hide.setStyleSheet(self._btn_css("#444", "#555"))
        self.btn_hide.clicked.connect(self._toggle_visibility)

        self.btn_clear = QPushButton("⟳ 缓存")
        self.btn_clear.setStyleSheet(self._btn_css("#444", "#555"))
        self.btn_clear.clicked.connect(self._clear_cache)

        self.btn_close = QPushButton("✕")
        self.btn_close.setStyleSheet(self._btn_css("#661111", "#882222"))
        self.btn_close.setFixedWidth(34)
        self.btn_close.clicked.connect(self._cleanup_and_exit)

        r4.addWidget(self.btn_start); r4.addWidget(self.btn_hide)
        r4.addWidget(self.btn_clear); r4.addWidget(self.btn_close)
        lay.addLayout(r4)

        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color:#888;font-size:11px;background:transparent;")
        lay.addWidget(self.status_label)

        self.setFixedSize(400, 232)
        self.setStyleSheet("""
            QWidget{background:#1a1a1a;border:1px solid #333;border-radius:10px;}
            QLabel{background:transparent;}
        """)
        scr = QApplication.primaryScreen().geometry()
        self.move(scr.width() - 440, 60)

    @staticmethod
    def _combo_css():
        return ("QComboBox{background:#2a2a2a;color:#fff;border:1px solid #555;"
                "padding:4px 8px;border-radius:4px;min-width:130px;}"
                "QComboBox::drop-down{border:none;}"
                "QComboBox QAbstractItemView{background:#2a2a2a;color:#fff;"
                "selection-background-color:#0066aa;}")

    @staticmethod
    def _btn_css(bg, hover):
        return ("QPushButton{background:%s;color:#fff;border:none;padding:8px 12px;"
                "border-radius:6px;font-size:12px;font-weight:bold;}"
                "QPushButton:hover{background:%s;}" % (bg, hover))

    # ── 事件 ──
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = e.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() & Qt.LeftButton:
            self.move(e.globalPos() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_H and e.modifiers() & Qt.ControlModifier:
            self._toggle_visibility()
        elif e.key() == Qt.Key_S and e.modifiers() & Qt.ControlModifier:
            self._toggle()
        elif e.key() == Qt.Key_Q and e.modifiers() & Qt.ControlModifier:
            self._cleanup_and_exit()

    # ── 翻译控制 ──
    def _toggle(self):
        if self.worker.isRunning():
            self._stop()
        else:
            self._start()

    def _start(self):
        key = self.lang_combo.currentData()
        src, tgt = key.split("→")
        geo = self.capture_region.geometry()
        pixel = bool(self.mode_combo.currentData())

        self.worker.configure((geo.x(), geo.y(), geo.width(), geo.height()),
                              src, tgt, pixel)
        self.worker.start()
        self.btn_start.setText("⏹ 停止")
        self.btn_start.setStyleSheet(self._btn_css("#663300", "#884400"))
        self.lang_combo.setEnabled(False)
        self.mode_combo.setEnabled(False)
        self.status_label.setText("翻译中…")
        self.overlay.set_status("正在翻译…")

    def _stop(self):
        self.worker.stop()
        self.worker.wait(5000)
        self.btn_start.setText("▶ 开始翻译")
        self.btn_start.setStyleSheet(self._btn_css("#006633", "#008844"))
        self.lang_combo.setEnabled(True)
        self.mode_combo.setEnabled(True)
        self.status_label.setText("已停止")

    def _clear_cache(self):
        self.worker.translator.clear_cache()
        self.status_label.setText("缓存已清空")

    def _on_blocks(self, blocks):
        self.overlay.set_blocks(blocks)

    def _on_plain(self, text):
        self.overlay.set_plain(text)

    def _on_status(self, msg):
        self.status_label.setText("%s · %s" % (time.strftime("%H:%M:%S"), msg))

    def _on_error(self, msg):
        self.status_label.setText("⚠ " + msg)
        self.overlay.set_status("⚠ " + msg)

    def _on_region_changed(self):
        geo = self.capture_region.geometry()
        self.overlay.setGeometry(geo)
        if self.worker.isRunning():
            key = self.lang_combo.currentData()
            src, tgt = key.split("→")
            self.worker.configure((geo.x(), geo.y(), geo.width(), geo.height()),
                                  src, tgt, bool(self.mode_combo.currentData()))

    def _toggle_visibility(self):
        if self.capture_region.isVisible():
            self.capture_region.hide()
            self.overlay.hide()
            self.btn_hide.setText("👁 显示")
        else:
            self.capture_region.show()
            self.overlay.show()
            self.btn_hide.setText("👁 隐藏")

    def _cleanup_and_exit(self):
        self._stop()
        try:
            self.worker.capture.close()
        except Exception:
            pass
        self.capture_region.close()
        self.overlay.close()
        QApplication.quit()


# ═══════════════════════════════════════════════════════════════
#  CLI 自测
# ═══════════════════════════════════════════════════════════════

def cli_test(text):
    cfg = cfg_mod.load_config()
    if not cfg["api_key"]:
        print("✗ 未配置 API Key。")
        print("  方式一：环境变量 SCREEN_TRANS_API_KEY")
        print("  方式二：%s 里写 SCREEN_TRANS_API_KEY=xxx" % cfg_mod.ENV_PATH)
        print("  方式三：%s 里填 api_key" % cfg_mod.CONFIG_PATH)
        return 2
    t = engine_mod.LLMTranslator(cfg["api_key"], cfg["base_url"], cfg["model"], "anthropic")
    out = t.translate_text([text], cfg["source_lang"], cfg["target_lang"])
    print("原文:", text)
    print("译文:", out[0] if out else "(空)")
    return 0


# ═══════════════════════════════════════════════════════════════
#  启动
# ═══════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="屏幕翻译器（独立版）")
    ap.add_argument("--cli", metavar="TEXT", help="命令行翻译自测")
    ap.add_argument("--set-key", metavar="KEY", help="把 API Key 写进本目录 .env")
    ap.add_argument("--base-url", metavar="URL", help="设置 API Base URL")
    ap.add_argument("--model", metavar="MODEL", help="设置模型名")
    ap.add_argument("--style", choices=["anthropic", "openai"], default="anthropic",
                    help="API 协议风格")
    args = ap.parse_args()

    if args.set_key:
        with open(cfg_mod.ENV_PATH, "w", encoding="utf-8") as f:
            f.write("SCREEN_TRANS_API_KEY=%s\n" % args.set_key.strip())
            if args.base_url:
                f.write("SCREEN_TRANS_BASE_URL=%s\n" % args.base_url.strip())
            if args.model:
                f.write("SCREEN_TRANS_MODEL=%s\n" % args.model.strip())
            f.write("SCREEN_TRANS_STYLE=%s\n" % args.style)
        print("✓ 已写入 %s" % cfg_mod.ENV_PATH)
        print("  现在可以运行: python app.py")
        return 0

    if args.cli:
        return cli_test(args.cli)

    dpi_mode = enable_dpi_awareness()
    cfg = cfg_mod.load_config()

    if not cfg["api_key"]:
        print("⚠ 未找到 API Key —— 工具仍可启动，但翻译会失败。")
        print("  设置方法：python app.py --set-key <你的KEY>")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet("""
        QToolTip{background:#333;color:#fff;border:1px solid #555;padding:4px;border-radius:3px;}
    """)

    style = cfg.get("api_style", "anthropic")
    translator = engine_mod.LLMTranslator(
        cfg["api_key"], cfg["base_url"], cfg["model"], style)

    panel = ControlPanel(translator)
    panel.show()
    print("屏幕翻译器已启动 (DPI: %s, 模型: %s)" % (dpi_mode, cfg["model"]))
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
