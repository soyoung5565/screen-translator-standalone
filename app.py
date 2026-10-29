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

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QRect, QPropertyAnimation, QObject, QAbstractNativeEventFilter
from PyQt5.QtGui import QPainter, QColor, QPen, QFont, QFontMetrics, QCursor, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QSlider, QComboBox,
    QVBoxLayout, QHBoxLayout, QSystemTrayIcon, QMenu, QAction,
    QGraphicsOpacityEffect,
)

import mss
from PIL import Image

import config as cfg_mod
import engine as engine_mod
from ui_components import SegmentedControl, StatusDot


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


def resource_path(name):
    """资源路径 —— 兼容 PyInstaller 单文件（解包到 sys._MEIPASS）。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


_ARROW_PATH = None


def combo_arrow_path():
    """
    QComboBox 下拉三角 —— Qt 样式表不能画三角形，只能给图片。
    运行时画一张灰色小三角存到临时目录，供 CSS url() 引用。
    """
    global _ARROW_PATH
    if _ARROW_PATH and os.path.exists(_ARROW_PATH):
        return _ARROW_PATH
    from PyQt5.QtGui import QPainterPath
    pm = QPixmap(10, 6)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#8b949e"))
    path = QPainterPath()
    path.moveTo(0, 0)
    path.lineTo(10, 0)
    path.lineTo(5, 6)
    path.closeSubpath()
    p.drawPath(path)
    p.end()
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or os.path.expanduser("~")
    out = os.path.join(tmp, "screen_trans_combo_arrow.png")
    pm.save(out, "PNG")
    _ARROW_PATH = out.replace("\\", "/")
    return _ARROW_PATH


def apply_dark_titlebar(hwnd):
    """把 Windows 标题栏染成暗色，与深色 UI 统一。"""
    try:
        dwm = ctypes.windll.dwmapi
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20  # Win10 20H1+
        val = ctypes.c_int(1)
        r = dwm.DwmSetWindowAttribute(ctypes.c_void_p(int(hwnd)),
                                      DWMWA_USE_IMMERSIVE_DARK_MODE,
                                      ctypes.byref(val), ctypes.sizeof(val))
        if r != 0:  # 老版本 Win10 用 19
            dwm.DwmSetWindowAttribute(ctypes.c_void_p(int(hwnd)), 19,
                                      ctypes.byref(val), ctypes.sizeof(val))
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
#  全局热键（RegisterHotKey）
# ═══════════════════════════════════════════════════════════════

WM_HOTKEY = 0x0312

HOTKEY_DEFS = [
    # (id, Ctrl+Alt+<key>, name, 说明) —— 用 Ctrl+Alt 前缀，避免和游戏的 Ctrl+S 冲突
    (1, ord("S"), "toggle", "开始/停止翻译"),
    (2, ord("H"), "hide", "隐藏/显示区域框"),
    (3, ord("Q"), "quit", "退出"),
]

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000


class HotkeyFilter(QObject, QAbstractNativeEventFilter):
    """
    全局热键 —— 面板没焦点时也能触发（打游戏时按键仍然有效）。

    实现：win32 RegisterHotKey 注册到本线程，WM_HOTKEY 从 Qt 的事件泵里
    拦出来，转成 Qt 信号。

    ⚠️ 必须同时继承 QAbstractNativeEventFilter，否则 installNativeEventFilter
    会报 "argument 1 has unexpected type"。
    """

    triggered = pyqtSignal(str)

    def __init__(self):
        QObject.__init__(self)
        QAbstractNativeEventFilter.__init__(self)
        self._registered = []

    def register_all(self):
        """注册全部热键；失败的单独返回，不阻塞其余热键。"""
        ok, failed = [], []
        for hid, vk, name, desc in HOTKEY_DEFS:
            r = ctypes.windll.user32.RegisterHotKey(
                None, hid, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, vk)
            if r:
                self._registered.append(hid)
                ok.append("%s（Ctrl+Alt+%s）" % (desc, chr(vk)))
            else:
                failed.append("%s（Ctrl+Alt+%s）" % (desc, chr(vk)))
        return ok, failed

    def unregister_all(self):
        for hid in self._registered:
            try:
                ctypes.windll.user32.UnregisterHotKey(None, hid)
            except Exception:
                pass
        self._registered = []

    def nativeEventFilter(self, event_type, message):
        """Qt5 签名。message 是 MSG*，取出 message 字段比对 WM_HOTKEY。"""
        try:
            if event_type == b"windows_generic_MSG" or event_type == "windows_generic_MSG":
                msg = ctypes.cast(int(message), ctypes.POINTER(MSG)).contents
                if msg.message == WM_HOTKEY:
                    for hid, vk, name, desc in HOTKEY_DEFS:
                        if msg.wParam == hid:
                            self.triggered.emit(name)
                            break
        except Exception:
            pass
        return False, 0


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


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

    __slots__ = ("text", "x", "y", "w", "h", "bg_lum")

    def __init__(self, text, x, y, w, h, bg_lum=None):
        self.text = text
        self.x = int(x)
        self.y = int(y)
        self.w = int(w)
        self.h = int(h)
        self.bg_lum = bg_lum   # 该处背景亮度 0-255；None = 未知（按浅底处理）

    def __repr__(self):
        return "TextBlock(%r, %d,%d %dx%d)" % (self.text[:20], self.x, self.y, self.w, self.h)


def boxes_to_blocks(boxes, region_x, region_y, region_w, region_h, img=None):
    """
    LLM 返回的 0-1000 归一化坐标 → 屏幕绝对坐标。

    注意：截图时可能做过缩放（max_dim），但归一化坐标与缩放无关，
    直接按 region 尺寸换算即可。

    给了 img（截图原图）时，顺带采样每块所在位置的背景亮度，
    覆盖层据此自动切换「深底→白字深罩 / 浅底→黑红字白罩」的配色。

    ⚠️ 模型给出的 h 偶尔严重偏小（实测出现过 h=3px / h=7px，而原文实际
    17~22px），w 也常偏窄（遮罩盖不住整句原文）。直接信它会让译文字号
    缩到看不清、遮罩也盖不住原文。这里对 h 做下限兜底，对 w 做加宽兜底。
    """
    # 先算同图中位数高度，作为偏小值的兜底基准（含所有正值，不只大值）
    hs = []
    for b in boxes:
        try:
            hh = b["h"] / 1000.0 * region_h
        except (KeyError, TypeError):
            continue
        if hh > 0:
            hs.append(hh)
    hs.sort()
    median_h = hs[len(hs) // 2] if hs else 0

    blocks = []
    for b in boxes:
        zh = b.get("zh", "").strip()
        if not zh:
            continue
        x = region_x + b["x"] / 1000.0 * region_w
        y = region_y + b["y"] / 1000.0 * region_h
        w = b["w"] / 1000.0 * region_w
        h = b["h"] / 1000.0 * region_h
        if w < 4 or h < 2:
            continue

        # h 兜底：低于中位数的 65% 或低于 11px 都视为模型量错，用中位数修正
        if median_h >= 11 and (h < 11 or h < median_h * 0.65):
            y = y + h / 2.0 - median_h / 2.0   # 以原框中心对齐
            h = median_h
        elif h < 11:
            h = 11

        # w 兜底：遮罩必须盖住原文整句。模型量窄了会让英文后半截露出来
        # （实测遮盖率只有 68~85%）。框太窄时按原文高度估算常见行宽。
        min_w = max(h * 3.0, 60.0)
        if w < min_w:
            w = min_w

        bg_lum = None
        if img is not None:
            # ⚠️ 截图可能被 to_jpeg_b64 缩放过；此处 img 是**原尺寸**截图。
            # 归一化坐标 → 原图像素必须按原图宽高换算，不能用 region_w/h
            # （两者通常相等，但裁剪/缩放后不等，早先写法会采错位置）。
            ix = int(b["x"] / 1000.0 * img.width)
            iy = int(b["y"] / 1000.0 * img.height)
            iw = max(1, int(w / float(region_w) * img.width))
            ih = max(1, int(h / float(region_h) * img.height))
            bg_lum = sample_bg_luminance(img, ix, iy, iw, ih)

        blocks.append(TextBlock(zh, x, y, w, h, bg_lum))
    return blocks


def sample_bg_luminance(img, x, y, w, h):
    """
    采样一块区域的**背景**亮度（0-255）。

    坑：不能取平均 —— 文字墨迹会把平均值拉偏；也不能取最亮 ——
    深底白字时最亮的是字本身。这里取中位数：框内占多数的像素就是背景。
    """
    try:
        from PIL import Image as _Image
        crop = img.crop((max(0, x), max(0, y),
                         min(img.width, x + max(1, w)), min(img.height, y + max(1, h))))
        crop = crop.convert("L").resize((min(16, crop.width), min(8, crop.height)))
        vals = sorted(crop.getdata())
        if not vals:
            return 128
        return vals[len(vals) // 2]
    except Exception:
        return 128


# ═══════════════════════════════════════════════════════════════
#  墨迹校正（模型坐标不稳的根治手段）
# ═══════════════════════════════════════════════════════════════

INK_TOL = 60          # 与背景亮度差超过这个值即视为「墨迹」
INK_MIN_ROW_PIX = 2   # 一行至少这么多墨迹像素才算文字行


def _same_row(a, b):
    """两个块的垂直中心是否落在同一行（用于邻居边界判断）。"""
    ah = max(6, a.h * 0.6)
    return abs((a.y + a.h / 2.0) - (b.y + b.h / 2.0)) < ah


def refine_blocks_with_ink(blocks, img, region_x, region_y, region_w, region_h,
                           tol=INK_TOL):
    """
    用**截图实测的墨迹包围盒**校正模型返回的框。

    为什么必须做：
      同一张图多次调用，模型返回的块数在 0~5 之间抖动，w 常偏窄
      （遮罩只盖住英文前半截，遮盖率 68~85%），h 有时给到 3px，
      y 也会整体偏上/偏下几个像素。这些都不是渲染 bug，是模型噪声 ——
      再优化渲染也治不了，只能拿像素反推真实框。

    ⚠️ 窗口尺寸**绝不能用当前块的 h 推**：h 就是最大的误差源。
       早先版本按 `±0.35×h` 开窗，h=8 时窗口只有 11px 高，
       真行 22px 只扫到顶部 6px，校正反而比原框更差（IoU 0.09→0.20）。
       现在统一用**同图中位数高度**做尺度基准。

    做法：
      1. 垂直窗口 = 模型 y 上方 0.7×median_h 到 y+h 下方 1.3×median_h；
      2. 水平窗口 = 模型 x 左侧 0.8×median_h 到右侧 max(3w, w+240)；
         再用同行邻居的实际宽度夹住，避免吃进相邻文字；
      3. 按行统计墨迹 → 连续行带 → 选「离模型上边缘最近 + 高度最接近中位数」的带；
      4. 行带给出真实 y/h，带内墨迹左右极值给出真实 x/w。

    兜底（任一命中就保留原框，宁可不改也不能改坏）：
      * 找不到墨迹；* 结果离谱地大（吃进图标/边框）；* 与原框完全不相交。
    """
    if not blocks or img is None:
        return blocks
    try:
        gray = img.convert("L")
        px = gray.load()
    except Exception:
        return blocks
    GW, GH = gray.size
    if GW <= 0 or GH <= 0:
        return blocks

    # ── 尺度基准：不能取自模型 h（h 就是最大误差源）──
    # 模型给的 h 系统性偏小（实测 6~8px，真实 17~22px），
    # 若拿它当窗口尺度，22px 的真行只会被扫到顶部 14px，高度永远校正。
    # 这里给一个绝对下限 12px，并用中位数做上限保护。
    hs = sorted(b.h for b in blocks if 3 <= b.h <= 200)
    median_h = hs[len(hs) // 2] if hs else 16
    unit = max(12, min(median_h, 48))

    out = []
    for b in blocks:
        lx, ly = b.x - region_x, b.y - region_y
        bg = b.bg_lum
        if bg is None:
            bg = sample_bg_luminance(img, lx, ly, max(4, b.w), max(4, b.h))

        # 垂直窗口与 h 解耦：向上 1.5×unit、向下 max(h,unit)+2.5×unit。
        # 这个余量能覆盖「模型 h 比真实小 2.5 倍」的最坏情况（实测就是）。
        x0 = int(lx - unit * 0.8)
        x1 = int(lx + max(b.w * 3.0, b.w + 240))
        y0 = int(ly - unit * 1.5)
        y1 = int(ly + max(b.h, unit) + unit * 2.5)

        # 同行邻居夹边界（用邻居的真实起点/终点，不用它的 w）
        for o in blocks:
            if o is b or not _same_row(b, o):
                continue
            olx = o.x - region_x
            if olx > lx + 2:
                x1 = min(x1, int(olx - 2))
            elif olx + o.w <= lx + 2:
                x0 = max(x0, int(olx + o.w + 2))

        x0, x1 = max(0, x0), min(GW, x1)
        y0, y1 = max(0, y0), min(GH, y1)
        if x1 - x0 < 4 or y1 - y0 < 4:
            out.append(b)
            continue

        # ── 逐行墨迹统计 ──
        solid = []
        for yy in range(y0, y1):
            c = 0
            for xx in range(x0, x1):
                if abs(px[xx, yy] - bg) >= tol:
                    c += 1
            if c >= INK_MIN_ROW_PIX:
                solid.append(yy)
        if not solid:
            out.append(b)
            continue

        # 连续行带：间隔 ≤2px 合并（抗抗锯齿、小写字母间隙）
        raw_runs = [[solid[0]]]
        for yy in solid[1:]:
            if yy - raw_runs[-1][-1] <= 2:
                raw_runs[-1].append(yy)
            else:
                raw_runs.append([yy])
        runs = [r for r in raw_runs if len(r) >= 3]
        if not runs:
            out.append(b)
            continue

        # 选带：离模型上边缘近 + 高度接近中位数
        best, best_score = None, None
        for r in runs:
            rh = r[-1] - r[0] + 1
            score = abs(r[0] - ly) / float(unit) + 0.6 * abs(rh - median_h) / float(unit)
            if best_score is None or score < best_score:
                best_score, best = score, r
        rows = best
        ny0, ny1 = rows[0], rows[-1]

        # ── 行带内墨迹的左右极值 ──
        nx0, nx1 = 10 ** 9, -1
        for yy in rows:
            for xx in range(x0, x1):
                if abs(px[xx, yy] - bg) >= tol:
                    if xx < nx0:
                        nx0 = xx
                    if xx > nx1:
                        nx1 = xx
        if nx1 < nx0:
            out.append(b)
            continue

        new_h = ny1 - ny0 + 1
        new_w = nx1 - nx0 + 1

        # 离谱兜底：吃进图标/整段版面
        # ⚠️ 上限同样不能用 median_h 推 —— median_h 由一批偏小的模型 h 组成
        # （实测 6~8px），真行 22px 就会被判「离谱」而放弃校正。
        # 用带 12px 下限的 unit 做基准。
        if new_h > max(16, unit * 3.0) or new_w > max(b.w * 4.0, b.w + 320):
            out.append(b)
            continue
        # 必须与原框相交（防串到别的行）
        if nx1 < lx or nx0 > lx + max(4, b.w):
            out.append(b)
            continue
        # 高度至少 3px，否则没有校正价值
        if new_h < 3:
            out.append(b)
            continue

        nb = TextBlock(b.text, region_x + nx0, region_y + ny0, new_w, new_h,
                       _bg_from_ring(img, nx0, ny0, new_w, new_h, bg))
        out.append(nb)
    return out


def _bg_from_ring(img, x, y, w, h, default):
    """
    从框**外侧一圈**采背景色，而不是框内。

    框内取了中位数仍可能被字本身带偏 —— 深底白字、粗体大标题时，
    框内白像素能占到近一半；实测「控制面板」这块 26px 白字，
    框内中位数算出 lum=88（真背景 36），离阈值 110 已经不远，
    再偏一点就会把深底判成浅底、贴出一块白斑。
    """
    try:
        gw, gh = img.size
        ring = 3
        x0, y0 = max(0, x - ring), max(0, y - ring)
        x1, y1 = min(gw, x + w + ring), min(gh, y + h + ring)
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            return default
        crop = img.crop((x0, y0, x1, y1)).convert("L")
        vals = []
        W_, H_ = crop.size
        px_ = crop.load()
        for yy in range(H_):
            for xx in range(W_):
                inside = (ring <= xx < W_ - ring) and (ring <= yy < H_ - ring)
                if not inside:
                    vals.append(px_[xx, yy])
        if not vals:
            return default
        vals.sort()
        return vals[len(vals) // 2]
    except Exception:
        return default




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
        self._interval = 3.0
        self._last_call = 0.0
        self.capture = None
        self._last_hash = None

    def configure(self, region, source, target, pixel_mode=True, interval=None):
        self._region = region
        self._source = source
        self._target = target
        self._pixel_mode = pixel_mode
        if interval is not None:
            self._interval = max(0.5, float(interval))
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
                    self._idle_wait()
                    continue

                # 节流：距上次请求不足「刷新间隔」就等着，避免动态画面疯狂烧额度
                since = time.time() - self._last_call
                if since < self._interval:
                    time.sleep(self._interval - since)
                    continue

                self._last_hash = hsh
                self._last_call = time.time()

                self.status_update.emit("翻译中…")
                self._process(img, x, y, w, h)
            except Exception as e:
                self.error_occurred.emit("%s: %s" % (type(e).__name__, e))
                time.sleep(2)
            self._idle_wait()

    def _idle_wait(self):
        """轮询节拍 —— 固定 0.35s 采样，保证及时抓到画面变化。"""
        time.sleep(0.35)

    def _process(self, img, ox, oy, ow, oh):
        b64 = to_jpeg_b64(img)

        if self._pixel_mode:
            boxes = self.translator.translate_image_boxes(b64, self._source, self._target)
            if boxes:
                # 传 img 进去，让每块带上背景亮度（覆盖层据此自适应配色）
                blocks = boxes_to_blocks(boxes, ox, oy, ow, oh, img=img)
                # 再用截图实测的墨迹框校正模型坐标 —— 模型 w/h 抖动是
                # 主要误差源（遮罩盖不满原文/字号忽大忽小），只能靠像素校正根治
                blocks = refine_blocks_with_ink(blocks, img, ox, oy, ow, oh)
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
        译文贴回原位。

        视觉策略（对应"原文被替换"的观感）：
          * 白底不透明遮罩（alpha 230）—— alpha 160 时原文英文会透出来，
            视觉上变成"中英叠字"，读起来很脏（实测被指"只盖住左半"）。
          * 遮罩宽度至少覆盖原文框，译文超长则向右延伸但不缩到看不清。
          * 深红字，字号按原文高度自适应（见 _fit_font）。

        注意：block 里存的是**屏幕绝对坐标**，而 QPainter 在 widget 上
        的坐标系原点是 widget 左上角 —— 必须减去 widget 原点，
        否则会叠加一层偏移（实际会画到两倍距离的位置）。
        """
        text = block.text

        # 绝对坐标 → widget 相对坐标
        ox = self.geometry().x()
        oy = self.geometry().y()

        pad = 2
        base_w = block.w + pad * 2
        base_h = block.h + pad * 2

        # 先按原框宽做宽度适配，量出实际需要多宽
        font = self._fit_font(block.h, text, max_w=max(8, base_w - 4))
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(text)

        x = block.x - ox - pad
        y = block.y - oy - pad
        w = max(base_w, tw + 6)
        h = base_h

        # 配色自适应：深色界面上贴白块极其刺眼（实测深色标题栏上是一块白斑），
        # 所以深底用「深色罩 + 白字」，浅底用「白罩 + 深红字」。
        dark_bg = block.bg_lum is not None and block.bg_lum < 110
        if dark_bg:
            p.fillRect(QRect(x, y, w, h), QColor(18, 20, 24, 235))
            p.setPen(QPen(QColor(120, 220, 150, 120), 1))
            p.drawRect(QRect(x, y, w, h))
            p.setPen(QColor(140, 240, 170))
        else:
            # alpha 230 ≈ 90% 遮盖 —— 太低会让原文透出形成中英叠字
            p.fillRect(QRect(x, y, w, h), QColor(255, 255, 255, 230))
            p.setPen(QPen(QColor(200, 0, 0, 90), 1))
            p.drawRect(QRect(x, y, w, h))
            p.setPen(QColor(150, 0, 0))

        p.setFont(font)
        p.drawText(QRect(x + 2, y, w - 4, h), Qt.AlignLeft | Qt.AlignVCenter, text)

    @staticmethod
    def _fit_font(orig_h, text=None, max_w=None):
        """
        按**原文高度**选字号（像素字号，不用 pointSize）。

        实测标定（见 calibrate_font.py，Microsoft YaHei）：
            墨高 ≈ 0.99 × 像素字号   →  像素字号 ≈ 1.01 × 目标高度
            墨高 ≈ 0.76 × fm.height()

        坑（都踩过）：
          * 旧实现 clamp 到 9pt，16px 高的原文溢出 150%、56px 标题只占 43%；
          * 之后改用 0.72×h 并结合 fm.height() 做校准，因为 fm.height()
            比墨高大 ~1.3 倍，校准循环把字号又压掉一档，中文墨高只剩原文 54%。
        正确做法：直接让墨高对齐原文高度，只用 fm.height() 做**上限**保护。
        """
        target = max(8, int(orig_h))

        def make(sz):
            f = QFont("Microsoft YaHei")
            f.setPixelSize(max(6, sz))
            return f

        # 墨高 ≈ 1.0×像素字号 → 目标高度直接用
        size = max(6, int(round(target * 1.01)))

        # 宽度校准：只缩 10% 作为缓冲，其余交给「向右延伸」。
        # 实测缩得更多会让长译文明显变小，而向右多出一小截远比字变小好读。
        font = make(size)
        if text and max_w:
            floor_sz = max(6, int(round(size * 0.90)))
            while size > floor_sz:
                fm = QFontMetrics(font)
                if fm.horizontalAdvance(text) <= max_w:
                    break
                size -= 1
                font = make(size)
        return font


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
        self.setWindowTitle("屏幕翻译器")
        # 普通窗口：任务栏有图标、Alt+Tab 能切、带标准标题栏/关闭按钮
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self._drag_pos = None

        self.capture_region = CaptureRegion()
        self.overlay = PixelOverlay()
        self.worker = TranslateWorker(translator)
        self.worker.blocks_ready.connect(self._on_blocks)
        self.worker.plain_ready.connect(self._on_plain)
        self.worker.status_update.connect(self._on_status)
        self.worker.error_occurred.connect(self._on_error)

        self._setup_ui()
        self._setup_tray()
        self._setup_hotkeys()
        self.capture_region.region_changed.connect(self._on_region_changed)
        self.capture_region.show()
        self.overlay.setGeometry(self.capture_region.geometry())
        self.overlay.show()

    # ── UI ──
    def _setup_ui(self):
        """UI 按 OpenDesign 设计稿实现（tech-utility 暗色仪表盘，400×268）。

        结构（自上而下）：
          状态带 40px   LED + 状态词 + LLM VISION 徽标 + 区域尺寸 + 清缓存 + 隐藏
          身体   212px  翻译方向 36 / 刷新间隔 32 / 贴字模式 32 / 模式说明 16
                         / 主按钮 36 / 底栏 28
        垂直预算 = 40 + 8 + 36 + 8 + 32 + 8 + 32 + 4 + 16 + 4 + 36 + 8 + 28 + 8 = 268
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ══ 状态带 40px ══
        strip = QWidget()
        strip.setFixedHeight(40)
        strip.setObjectName("statusStrip")
        strip_lay = QHBoxLayout(strip)
        strip_lay.setContentsMargins(16, 0, 16, 0)
        strip_lay.setSpacing(10)

        self.status_led = StatusDot()
        self.status_led.set_kind("idle")
        strip_lay.addWidget(self.status_led)

        self.status_title = QLabel("就绪")
        self.status_title.setObjectName("statusTitle")
        strip_lay.addWidget(self.status_title)

        badge = QLabel("LLM VISION")
        badge.setObjectName("visionBadge")
        strip_lay.addWidget(badge)

        strip_lay.addStretch(1)

        self.region_readout = QLabel("—")
        self.region_readout.setObjectName("regionReadout")
        strip_lay.addWidget(self.region_readout)

        self.btn_clear = QPushButton("⟳")
        self.btn_clear.setObjectName("iconBtn")
        self.btn_clear.setToolTip("清空翻译缓存")
        self.btn_clear.setFixedSize(22, 22)
        self.btn_clear.setFocusPolicy(Qt.NoFocus)
        self.btn_clear.clicked.connect(self._clear_cache)
        strip_lay.addWidget(self.btn_clear)

        self.btn_hide = QPushButton("隐藏")
        self.btn_hide.setObjectName("textLink")
        self.btn_hide.setFocusPolicy(Qt.NoFocus)
        self.btn_hide.setCursor(Qt.PointingHandCursor)
        self.btn_hide.clicked.connect(self._toggle_visibility)
        strip_lay.addWidget(self.btn_hide)

        root.addWidget(strip)

        # ══ 身体 212px ══
        body = QWidget()
        body.setObjectName("panelBody")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(16, 8, 16, 8)
        lay.setSpacing(0)

        # ── 翻译方向 36px（主选项，比另两行高 4px）──
        r1 = QHBoxLayout(); r1.setSpacing(12); r1.setContentsMargins(0, 0, 0, 0)
        l1 = QLabel("翻译方向"); l1.setFixedWidth(64)
        l1.setObjectName("rowLabel")
        self.lang_combo = QComboBox()
        self.lang_combo.setFixedHeight(36)
        self.lang_combo.setObjectName("langCombo")
        for key, label in LANG_PAIRS:
            self.lang_combo.addItem(label, key)
        self.lang_combo.setStyleSheet(self._combo_css())
        r1.addWidget(l1); r1.addWidget(self.lang_combo, 1)
        lay.addLayout(r1)
        lay.addSpacing(8)

        # ── 刷新间隔 32px ──
        r2 = QHBoxLayout(); r2.setSpacing(12); r2.setContentsMargins(0, 0, 0, 0)
        l2 = QLabel("刷新间隔"); l2.setFixedWidth(64)
        l2.setObjectName("rowLabel")
        self.interval_slider = QSlider(Qt.Horizontal)
        self.interval_slider.setFixedHeight(32)
        self.interval_slider.setRange(1, 15)
        self.interval_slider.setValue(3)
        self.interval_slider.setObjectName("intervalSlider")
        self.interval_slider.setStyleSheet(self._slider_css())
        self.interval_slider.valueChanged.connect(self._on_interval_changed)
        # 数值区：两行（数字 + 说明），宽 96 右对齐
        # 用固定高度 32 的容器 + 两个绝对定位感的行高，避免 QLabel 默认
        # 撑高把「3秒」和「最快 3 秒一次」叠在一起。
        val_box = QWidget(); val_box.setFixedWidth(96); val_box.setFixedHeight(32)
        val_lay = QVBoxLayout(val_box)
        val_lay.setContentsMargins(0, 0, 0, 0); val_lay.setSpacing(0)
        self.interval_label = QLabel("3<i>秒</i>")
        self.interval_label.setObjectName("intervalValue")
        self.interval_label.setFixedHeight(18)
        self.interval_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.interval_hint = QLabel("最快 3 秒一次")
        self.interval_hint.setObjectName("intervalHint")
        self.interval_hint.setFixedHeight(14)
        self.interval_hint.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        val_lay.addWidget(self.interval_label); val_lay.addWidget(self.interval_hint)
        r2.addWidget(l2); r2.addWidget(self.interval_slider, 1); r2.addWidget(val_box)
        lay.addLayout(r2)
        lay.addSpacing(8)

        # ── 贴字模式 32px（分段控件，替代下拉框）──
        r3 = QHBoxLayout(); r3.setSpacing(12); r3.setContentsMargins(0, 0, 0, 0)
        l3 = QLabel("贴字模式"); l3.setFixedWidth(64)
        l3.setObjectName("rowLabel")
        self.mode_seg = SegmentedControl([
            ("像素级 · 原位贴字", True),
            ("整图翻译", False),
        ])
        self.mode_seg.setFixedHeight(32)
        self.mode_seg.setStyleSheet(self._seg_css())
        r3.addWidget(l3); r3.addWidget(self.mode_seg, 1)
        lay.addLayout(r3)
        lay.addSpacing(4)

        # ── 模式说明 16px ──
        self.mode_hint = QLabel("译文按原文的像素尺寸原位覆盖，原排版不变。")
        self.mode_hint.setObjectName("modeHint")
        self.mode_hint.setFixedHeight(16)
        lay.addWidget(self.mode_hint)
        lay.addSpacing(4)

        # ── 主按钮 36px（通栏）──
        self.btn_start = QPushButton("▶  开始翻译")
        self.btn_start.setObjectName("primaryCta")
        self.btn_start.setFixedHeight(36)
        self.btn_start.setFocusPolicy(Qt.NoFocus)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setStyleSheet(self._cta_css(False))
        self.btn_start.clicked.connect(self._toggle)
        lay.addWidget(self.btn_start)
        lay.addSpacing(8)

        # ── 底栏 28px ──
        foot = QWidget(); foot.setFixedHeight(28)
        foot.setObjectName("panelFoot")
        foot_lay = QHBoxLayout(foot)
        foot_lay.setContentsMargins(0, 3, 0, 0)
        foot_lay.setSpacing(12)
        self.status_label = QLabel("Ctrl+Alt+S 开始/停止")
        self.status_label.setObjectName("footReadout")
        foot_lay.addWidget(self.status_label, 1)
        self.btn_retry = QPushButton("重试")
        self.btn_retry.setObjectName("retryBtn")
        self.btn_retry.setFixedHeight(20)
        self.btn_retry.setFocusPolicy(Qt.NoFocus)
        self.btn_retry.setCursor(Qt.PointingHandCursor)
        self.btn_retry.setVisible(False)
        self.btn_retry.clicked.connect(self._toggle)
        foot_lay.addWidget(self.btn_retry)
        self.btn_close = QPushButton("退出")
        self.btn_close.setObjectName("quitLink")
        self.btn_close.setFocusPolicy(Qt.NoFocus)
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.setToolTip("退出程序")
        self.btn_close.clicked.connect(self._cleanup_and_exit)
        foot_lay.addWidget(self.btn_close)
        lay.addWidget(foot)

        root.addWidget(body, 1)

        self.setFixedSize(400, 268)
        self.setStyleSheet(self._panel_css())
        # 居中显示，避免出现在屏幕外或被遮
        scr = QApplication.primaryScreen().geometry()
        self.move(scr.center().x() - 200, scr.center().y() - 134)
        self._refresh_region_readout()

    # ── 样式表（按设计稿 :root token 逐条映射）──
    @staticmethod
    def _panel_css():
        return (
            "ControlPanel{background:#161b22;border:1px solid #30363d;border-radius:8px;}"
            "QWidget{color:#e6edf3;font-family:'Microsoft YaHei';}"
            "QLabel{background:transparent;}"
            # 状态带
            "QWidget#statusStrip{background:#1c2333;border-bottom:1px solid #30363d;}"
            "QLabel#statusTitle{font-size:13px;font-weight:600;color:#e6edf3;}"
            "QLabel#visionBadge{font-size:9px;letter-spacing:0.08em;color:#8b949e;"
            "font-family:Consolas,monospace;border:1px solid #30363d;border-radius:9px;"
            "padding:2px 7px;}"
            "QLabel#regionReadout{font-size:10px;font-family:Consolas,monospace;color:#8b949e;}"
            "QPushButton#iconBtn{background:transparent;border:1px solid transparent;"
            "border-radius:4px;color:#8b949e;font-size:13px;padding:0;}"
            "QPushButton#iconBtn:hover{background:#252c38;border-color:#6e7681;}"
            "QPushButton#textLink{background:transparent;border:none;color:#8b949e;"
            "font-size:10px;padding:0;}"
            "QPushButton#textLink:hover{color:#e6edf3;}"
            # 身体
            "QWidget#panelBody{background:#161b22;}"
            "QLabel#rowLabel{font-size:12px;color:#8b949e;}"
            "QLabel#intervalValue{font-size:14px;font-family:Consolas,monospace;color:#e6edf3;}"
            "QLabel#intervalValue i{font-size:11px;font-style:normal;color:#8b949e;}"
            "QLabel#intervalHint{font-size:10px;color:#8b949e;}"
            "QLabel#modeHint{font-size:10px;color:#8b949e;}"
            # 底栏
            "QWidget#panelFoot{border-top:1px solid #30363d;}"
            "QLabel#footReadout{font-size:10px;font-family:Consolas,monospace;color:#8b949e;}"
            "QLabel#footReadout.err{color:#ff7b72;}"
            "QPushButton#retryBtn{background:#1c2333;border:1px solid #6e7681;"
            "border-radius:4px;color:#e6edf3;font-size:11px;padding:0 10px;}"
            "QPushButton#retryBtn:hover{background:#252c38;border-color:#8b949e;}"
            "QPushButton#quitLink{background:transparent;border:none;color:#6e7681;"
            "font-size:10px;padding:0;}"
            "QPushButton#quitLink:hover{color:#8b949e;}"
        )

    @staticmethod
    def _slider_css():
        return (
            "QSlider{background:transparent;}"
            "QSlider::groove:horizontal{background:#30363d;height:4px;border-radius:2px;}"
            "QSlider::sub-page:horizontal{background:#2ea043;border-radius:2px;}"
            "QSlider::handle:horizontal{background:#2ea043;border:2px solid #161b22;"
            "width:10px;height:10px;margin:-5px 0;border-radius:6px;}"
            "QSlider::handle:horizontal:hover{background:#3fb950;}"
        )

    @staticmethod
    def _seg_css():
        # 注意：不能用 `QWidget{...}` —— 它会命中所有子控件（含 QPushButton），
        # 把槽的底色和描边套到按钮上。必须用 objectName 精确限定容器。
        #
        # 槽的凹陷感靠三层对比撑起来：
        #   槽底 --surface-sunk(#12171f) 比面板底 --bg(#161b22) 更暗 → 挖进去
        #   槽描边 --border-ctl(#6e7681) 比其它发丝线亮 → 有边沿
        #   选中段 --surface-hi(#252c38) 比槽底亮 → 凸出来
        # 未选中段也要给底色，否则它会融进面板底色里，看不出「还有一个选项」。
        return (
            "QWidget#segControl{background:#0d1117;border:1px solid #6e7681;"
            "border-radius:6px;}"
            "QWidget#segControl QPushButton{background:#1c2333;"
            "border:1px solid transparent;border-radius:4px;color:#8b949e;"
            "font-size:11px;padding:0;}"
            "QWidget#segControl QPushButton:hover{background:#252c38;color:#e6edf3;}"
            "QWidget#segControl QPushButton:checked{background:#252c38;"
            "border-color:#8b949e;color:#e6edf3;font-weight:600;}"
        )

    @staticmethod
    def _cta_css(stop):
        if stop:
            base, hover, press = "#9e6a03", "#7d5303", "#6b4703"
        else:
            base, hover, press = "#1a7f37", "#1f883d", "#17692e"
        return (
            "QPushButton{background:%s;color:#fff;border:1px solid %s;border-radius:6px;"
            "font-size:13px;font-weight:600;}"
            "QPushButton:hover{background:%s;}"
            "QPushButton:pressed{background:%s;}"
            % (base, press, hover, press)
        )

    @staticmethod
    def _key_css():
        return ("color:#8b949e;font-size:11px;background:transparent;")

    @staticmethod
    def _status_css(kind="idle"):
        colors = {"idle": "#8b949e", "live": "#2ea043", "error": "#f85149"}
        return ("color:%s;font-size:10px;font-family:Consolas,monospace;"
                "border-top:1px solid #30363d;padding-top:8px;background:transparent;"
                % colors.get(kind, "#8b949e"))

    @staticmethod
    def _ghost_css():
        return ("QPushButton{background:#1c2333;color:#e6edf3;"
                "border:1px solid #30363d;border-radius:5px;"
                "font-size:11px;font-weight:600;padding:0 10px;}"
                "QPushButton:hover{background:#252c38;border-color:#484f58;}")

    def _setup_tray(self):
        """系统托盘：左键点 = 显示/隐藏面板，右键 = 菜单退出。"""
        icon = QIcon(resource_path("app.ico"))
        if icon.isNull() or not icon.availableSizes():
            pm = QPixmap(32, 32)
            pm.fill(QColor("#2ea043"))
            p = QPainter(pm)
            p.setPen(QColor("#ffffff"))
            p.setFont(QFont("Microsoft YaHei", 16, QFont.Bold))
            p.drawText(pm.rect(), Qt.AlignCenter, "译")
            p.end()
            icon = QIcon(pm)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("屏幕翻译器 — 独立版")
        menu = QMenu()
        menu.setStyleSheet(
            "QMenu{background:#161b22;color:#e6edf3;border:1px solid #30363d;"
            "border-radius:6px;padding:4px;font-size:11px;}"
            "QMenu::item{padding:6px 18px;border-radius:4px;}"
            "QMenu::item:selected{background:#1c2333;}"
            "QMenu::separator{height:1px;background:#30363d;margin:4px 6px;}")
        act_show = QAction("显示面板", self)
        act_show.triggered.connect(self._show_panel)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._cleanup_and_exit)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray)
        self.tray.show()

    def showEvent(self, event):
        super().showEvent(event)
        apply_dark_titlebar(self.winId())

    def _setup_hotkeys(self):
        """注册全局热键 —— 面板失去焦点（例如在打游戏）时依然能控制。"""
        self.hotkeys = HotkeyFilter()
        self.hotkeys.triggered.connect(self._on_hotkey)
        QApplication.instance().installNativeEventFilter(self.hotkeys)
        ok, failed = self.hotkeys.register_all()
        self._hotkey_report = (ok, failed)
        if failed:
            print("⚠ 以下全局热键被其他程序占用: %s" % "；".join(failed))

    def _on_hotkey(self, name):
        if name == "toggle":
            self._toggle()
        elif name == "hide":
            self._toggle_visibility()
        elif name == "quit":
            self._cleanup_and_exit()

    def _show_panel(self):
        self.showNormal(); self.raise_(); self.activateWindow()

    def _on_tray(self, reason):
        if reason == QSystemTrayIcon.Trigger:  # 左键
            if self.isVisible():
                self.hide()
            else:
                self._show_panel()

    def closeEvent(self, e):
        # 点 ✕ 不退出，只最小化到托盘；真要退走托盘菜单
        e.ignore()
        self.hide()
        self.tray.showMessage("屏幕翻译器", "已最小化到托盘，右键图标退出",
                              QSystemTrayIcon.Information, 1500)

    @staticmethod
    def _combo_css():
        arrow = combo_arrow_path()
        return (
            "QComboBox{background:#1c2333;color:#e6edf3;border:1px solid #30363d;"
            "border-radius:5px;padding:4px 8px;font-size:11px;"
            "font-family:Consolas,'Microsoft YaHei',monospace;}"
            "QComboBox:hover{border-color:#484f58;background:#252c38;}"
            "QComboBox:focus{border-color:#2ea043;}"
            "QComboBox:disabled{color:#6e7681;background:#161b22;}"
            "QComboBox::drop-down{border:none;width:18px;}"
            "QComboBox::down-arrow{image:url(%s);width:10px;height:6px;}"
            "QComboBox QAbstractItemView{background:#1c2333;color:#e6edf3;"
            "border:1px solid #30363d;border-radius:5px;outline:none;"
            "selection-background-color:#2ea043;selection-color:#ffffff;"
            "padding:3px;}"
        ) % arrow

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

    # ── 状态呈现（设计稿：LED + 状态词 + 底栏说明）──
    def _set_state(self, kind, title=None, foot=None, foot_error=False):
        """统一设置运行态：状态灯颜色 + 状态词 + 底栏读数。

        kind: 'idle' | 'live' | 'region' | 'error'
        """
        self.status_led.set_kind(kind)
        if title is not None:
            self.status_title.setText(title)
        if title is not None and kind == "error":
            self.status_title.setStyleSheet(
                "font-size:13px;font-weight:600;color:#ff7b72;background:transparent;")
        else:
            self.status_title.setStyleSheet(
                "font-size:13px;font-weight:600;color:#e6edf3;background:transparent;")
        if foot is not None:
            self.status_label.setText(foot)
        self.status_label.setStyleSheet(
            "font-size:10px;font-family:Consolas,monospace;color:%s;background:transparent;"
            % ("#ff7b72" if foot_error else "#8b949e"))
        self.btn_retry.setVisible(kind == "error")

    def _refresh_region_readout(self):
        """状态带右侧显示当前捕获区域尺寸（设计稿读数位）。"""
        try:
            geo = self.capture_region.geometry()
            self.region_readout.setText("%d×%d" % (geo.width(), geo.height()))
        except Exception:
            self.region_readout.setText("—")

    def _start(self):
        key = self.lang_combo.currentData()
        src, tgt = key.split("→")
        geo = self.capture_region.geometry()
        pixel = bool(self.mode_seg.currentData())

        self.worker.configure((geo.x(), geo.y(), geo.width(), geo.height()),
                              src, tgt, pixel, interval=self.interval_slider.value())
        self.worker.start()
        self.btn_start.setText("⏹  停止")
        self.btn_start.setStyleSheet(self._cta_css(True))
        self.lang_combo.setEnabled(False)
        self.mode_seg.setEnabled(False)
        self._set_state("live", "翻译中", "正在翻译…")
        self.overlay.set_status("正在翻译…")

    def _stop(self):
        self.worker.stop()
        self.worker.wait(5000)
        self.btn_start.setText("▶  开始翻译")
        self.btn_start.setStyleSheet(self._cta_css(False))
        self.lang_combo.setEnabled(True)
        self.mode_seg.setEnabled(True)
        self._set_state("idle", "就绪", "已停止")

    def _clear_cache(self):
        self.worker.translator.clear_cache()
        self._set_state(self.status_led.kind(), None, "缓存已清空")

    def _on_blocks(self, blocks):
        self.overlay.set_blocks(blocks)

    def _on_plain(self, text):
        self.overlay.set_plain(text)

    def _on_status(self, msg):
        # 状态带保持「翻译中」，底栏跑逐次读数（时间 + 译文块数）
        self.status_led.set_kind("live")
        self.status_title.setText("翻译中")
        self.status_title.setStyleSheet(
            "font-size:13px;font-weight:600;color:#e6edf3;background:transparent;")
        self.status_label.setText("%s · %s" % (time.strftime("%H:%M:%S"), msg))
        self.status_label.setStyleSheet(
            "font-size:10px;font-family:Consolas,monospace;color:#8b949e;background:transparent;")

    def _on_error(self, msg):
        self._set_state("error", "错误", msg, foot_error=True)
        self.overlay.set_status("⚠ " + msg)

    def _on_region_changed(self):
        geo = self.capture_region.geometry()
        self.overlay.setGeometry(geo)
        self._refresh_region_readout()
        if not self.worker.isRunning():
            self._set_state("region", "区域已选",
                            "区域位置 %d, %d · 拖动边框可调整" % (geo.x(), geo.y()))
        if self.worker.isRunning():
            key = self.lang_combo.currentData()
            src, tgt = key.split("→")
            self.worker.configure((geo.x(), geo.y(), geo.width(), geo.height()),
                                  src, tgt, bool(self.mode_seg.currentData()),
                                  interval=self.interval_slider.value())

    def _on_interval_changed(self, v):
        """滑块调整立即生效（运行中也生效），不必重启翻译。"""
        self.interval_label.setText("%d<i>秒</i>" % v)
        self.interval_hint.setText("最快 %d 秒一次" % v)
        if self.worker.isRunning():
            self.worker.configure(self.worker._region, self.worker._source,
                                  self.worker._target, self.worker._pixel_mode,
                                  interval=v)
            self.worker._last_hash = None

    def _toggle_visibility(self):
        if self.capture_region.isVisible():
            self.capture_region.hide()
            self.overlay.hide()
            self.btn_hide.setText("显示")
        else:
            self.capture_region.show()
            self.overlay.show()
            self.btn_hide.setText("隐藏")

    def _cleanup_and_exit(self):
        self._stop()
        try:
            self.hotkeys.unregister_all()
            QApplication.instance().removeNativeEventFilter(self.hotkeys)
        except Exception:
            pass
        try:
            self.worker.capture.close()
        except Exception:
            pass
        self.capture_region.close()
        self.overlay.close()
        self.tray.hide()
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


def selftest():
    """
    打包后自检 —— 让 exe **自己**验证完整链路。

    为什么需要它：源码测试全绿不代表 exe 里是同一份代码。PyInstaller 的
    归档是压缩过的，外面对 exe grep 字符串得不到可信结论；时间戳只能说明
    "打了包"，不能说明"打进的是新代码"。唯一可信的做法是让 exe 自己跑一遍。

    ⚠️ 打包后的 exe 是 console=False（runw.exe 引导器），**没有 stdout**。
       所以结果必须同时写进文件，否则窗口程序跑 --selftest 什么都看不到。
       文件落在 exe 同目录的 selftest.log。

    检查项（全部真实执行，不模拟）：
      1. 打包资源：app.ico 是否在解包目录里（托盘图标依赖它）
      2. 配置路径：HERE 是否为 exe 所在目录、能否读到 .env 里的 key
      3. 解析器：那段实测坏 JSON 能否还原 5 行（漏行 bug 的回归）
      4. 视觉链路：真发一次请求，能否返回 5/5 块
      5. 墨迹校正：坏坐标能否被拉回真值框
    """
    lines = []

    def say(msg=""):
        # 打包后的 exe 有两层输出坑（都实测踩过）：
        #   1. console=False → sys.stdout 可能是 None，print() 抛 AttributeError；
        #   2. stdout 被重定向时编码是 GBK（中文 Windows 默认），
        #      '✓' 这类字符抛 UnicodeEncodeError。
        # 两种都会在写文件**之前**崩掉，导致 selftest.log 根本不生成。
        lines.append(msg)
        try:
            if sys.stdout is not None:
                print(msg)
        except Exception:
            try:
                sys.stdout.write(msg.encode("utf-8", "replace").decode("utf-8", "replace") + "\n")
            except Exception:
                pass

    ok = True

    say("=== 1. 打包资源 ===")
    ico = resource_path("app.ico")
    have_ico = os.path.exists(ico)
    say("  app.ico: %s (%s)" % (ico, "存在 %d 字节" % os.path.getsize(ico) if have_ico else "缺失 → 托盘会退化"))
    ok = ok and have_ico

    say("\n=== 2. 配置路径 ===")
    say("  frozen=%s" % getattr(sys, "frozen", False))
    say("  HERE=%s" % cfg_mod.HERE)
    say("  .env 候选=%s" % cfg_mod._env_candidates())
    cfg = cfg_mod.load_config()
    say("  api_key 长度=%d, model=%s, style=%s"
        % (len(cfg["api_key"]), cfg["model"], cfg["api_style"]))
    if not cfg["api_key"]:
        say("  ✗ 读不到 key —— 打包后配置路径有问题")
        ok = False

    say("\n=== 3. 解析器（实测坏 JSON 回归）===")
    broken = ('[{"t":"Control Panel","x":39","y":52,"w":198,"h":34,"zh":"控制面板"},'
              '{"t":"Display brightness","x":39","y":289,"w":240,"h":32,"zh":"显示亮度"},'
              '{"t":"Network connection","x":39,"y":466,"w":249,"h":32,"zh":"网络连接"},'
              '{"t":"Automatic backup","x":39","y":647,"w":232,"h":32,"zh":"自动备份"},'
              '{"t":"Sign out of account","x":39","y":827,"w":246,"h":32,"zh":"退出账户"}]')
    boxes = engine_mod.parse_json_boxes(broken)
    say("  坏 JSON → %d/5 行 %s" % (len(boxes), "✓" if len(boxes) == 5 else "✗"))
    ok = ok and len(boxes) == 5

    say("\n=== 4. 视觉链路（真实 API）===")
    if not cfg["api_key"]:
        say("  跳过（无 key）")
        _write_selftest_log(lines, ok)
        return 2
    try:
        from PIL import Image, ImageDraw, ImageFont
        W, H = 760, 440
        img = Image.new("RGB", (W, H), (245, 246, 248))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 56], fill=(38, 42, 54))
        try:
            ft = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 26)
            fb = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 22)
        except Exception:
            ft = fb = ImageFont.load_default()
        d.text((30, 14), "Control Panel", font=ft, fill=(255, 255, 255))
        for i, t in enumerate(["Display brightness", "Network connection",
                               "Automatic backup", "Sign out of account"]):
            d.text((30, 122 + i * 80), t, font=fb, fill=(20, 20, 26))

        tr = engine_mod.LLMTranslator(cfg["api_key"], cfg["base_url"],
                                      cfg["model"], cfg["api_style"])
        b64 = to_jpeg_b64(img)
        got = tr.translate_image_boxes(b64, cfg["source_lang"], cfg["target_lang"])
        say("  识别 %d/5 行 %s" % (len(got), "✓" if len(got) == 5 else "⚠ 漏行"))
        for b in got:
            say("    %-22r → %r" % (b.get("t"), b.get("zh")))
        ok = ok and len(got) == 5

        say("\n=== 5. 墨迹校正 ===")
        blocks = boxes_to_blocks(got, 0, 0, W, H, img=img)
        ref = refine_blocks_with_ink(blocks, img, 0, 0, W, H)
        say("  %-12s %-22s %-22s" % ("译文", "校正前", "校正后"))
        for a, b in zip(blocks, ref):
            say("  %-12s %-22s %-22s"
                % (a.text, (a.x, a.y, a.w, a.h), (b.x, b.y, b.w, b.h)))
        hs = sorted(b.h for b in ref)
        med = hs[len(hs) // 2] if hs else 0
        tall = sum(1 for b in ref if b.h >= med * 0.8)
        say("  高度中位数=%d，达标块数=%d/%d" % (med, tall, len(ref)))
        ok = ok and tall >= max(1, int(len(ref) * 0.8))
    except Exception as e:
        say("  ✗ %s: %s" % (type(e).__name__, e))
        ok = False

    say("\n自检结论: %s" % ("✓ 全部通过" if ok else "✗ 有项目未通过"))
    _write_selftest_log(lines, ok)
    return 0 if ok else 1


def _write_selftest_log(lines, ok):
    """把自检结果落盘（console=False 的 exe 没有 stdout，只能靠文件）。"""
    try:
        path = os.path.join(cfg_mod.HERE, "selftest.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.write("\n")
        print("自检日志: %s" % path)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  启动
# ═══════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="屏幕翻译器（独立版）")
    ap.add_argument("--cli", metavar="TEXT", help="命令行翻译自测")
    ap.add_argument("--selftest", action="store_true",
                    help="打包后自检：资源/配置/解析器/视觉/墨迹校正")
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

    if args.selftest:
        return selftest()

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
    ok, failed = getattr(panel, "_hotkey_report", ([], []))
    print("屏幕翻译器已启动 (DPI: %s, 模型: %s)" % (dpi_mode, cfg["model"]))
    print("全局热键: %s" % ("、".join(ok) if ok else "无"))
    if failed:
        print("  以下热键被占用（可改用面板内 Ctrl+S/H/Q）: %s" % "、".join(failed))
    sys.exit(app.exec_())


if __name__ == "__main__":
    # 必须用 sys.exit(main()) 把退出码传出去：selftest() 失败返回 1、
    # 无 key 返回 2，直接调 main() 会让窗口程序的退出码恒为 0，
    # 自动化验收就会把「自检失败」误读成「通过」。
    sys.exit(main())
