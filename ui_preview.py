"""
UI 离屏渲染预览 —— 不显示任何真实窗口，供不打断用户操作时核对界面。

关键做法：
  * 用**原生 windows 平台插件**（offscreen 插件在这个环境里没有字体，
    渲染出来只有色块没有文字，不能用于核对）。
  * 把 QWidget.show / QSystemTrayIcon.show 临时换成空函数，
    所以 ControlPanel 构造里那些 show() 都不会真的把窗口弹到屏幕上。
  * 用 widget.grab() 直接渲染成 PNG —— grab 走 render()，不需要窗口可见。

用法:
    .venv/Scripts/python.exe ui_preview.py [输出目录]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from PyQt5.QtCore import Qt, QRect
from PyQt5.QtGui import QPainter, QColor, QFont, QPixmap, QImage, QFontDatabase
from PyQt5.QtWidgets import QApplication, QWidget, QSystemTrayIcon

import config as cfg_mod
import engine as engine_mod
import app as app_mod

OUT = sys.argv[1] if len(sys.argv) > 1 else "D:/hermes-data/cache/scratch/ui"
os.makedirs(OUT, exist_ok=True)

# ── 把所有会真正显示窗口的调用换成空函数 ──
QWidget.show = lambda self: None
QWidget.showNormal = lambda self: None
QWidget.showFullScreen = lambda self: None
QWidget.showMaximized = lambda self: None
QWidget.raise_ = lambda self: None
QWidget.activateWindow = lambda self: None
QWidget.close = lambda self: True
QSystemTrayIcon.show = lambda self: None
QSystemTrayIcon.showMessage = lambda *a, **k: None


def save_widget(w, path, pad=0, bg="#010409"):
    """widget.grab() → PNG；pad 给面板加一圈背景，看得出圆角边界。"""
    img = w.grab()
    if pad:
        canvas = QPixmap(img.width() + pad * 2, img.height() + pad * 2)
        canvas.fill(QColor(bg))
        p = QPainter(canvas)
        p.drawPixmap(pad, pad, img)
        p.end()
        canvas.save(path, "PNG")
    else:
        img.save(path, "PNG")
    print("  → %s  (%dx%d)" % (path, img.width(), img.height()))


def count_px(qimg, pred):
    n = 0
    for y in range(qimg.height()):
        for x in range(qimg.width()):
            c = qimg.pixelColor(x, y)
            if pred(c):
                n += 1
    return n


def main():
    a = QApplication(sys.argv)

    fams = QFontDatabase().families()
    print("字体数: %d  (Microsoft YaHei 存在: %s)"
          % (len(fams), "Microsoft YaHei" in fams))

    cfg = cfg_mod.load_config()
    tr = engine_mod.LLMTranslator(
        cfg["api_key"] or "dummy", cfg["base_url"], cfg["model"], "anthropic")

    print("[1] 控制面板 — 就绪态")
    panel = app_mod.ControlPanel(tr)
    a.processEvents()
    p1 = os.path.join(OUT, "panel_idle.png")
    save_widget(panel, p1, pad=16)
    light = count_px(QImage(p1), lambda c: c.red() > 150 and c.green() > 150 and c.blue() > 150)
    print("    客户区: %dx%d, 亮像素: %d" % (panel.width(), panel.height(), light))

    print("[2] 控制面板 — 翻译中态")
    panel.btn_start.setText("⏹  停止")
    panel.btn_start.setStyleSheet(panel._cta_css(True))
    panel.lang_combo.setEnabled(False)
    panel.mode_seg.setEnabled(False)
    panel.interval_slider.setValue(5)
    a.processEvents()
    panel._set_state("live", "翻译中", "14:32:07 · 像素级翻译 · 7 块")
    a.processEvents()
    save_widget(panel, os.path.join(OUT, "panel_live.png"), pad=16)

    print("[3] 控制面板 — 错误态")
    panel.lang_combo.setEnabled(True)
    panel.mode_seg.setEnabled(True)
    panel.btn_start.setText("▶  开始翻译")
    panel.btn_start.setStyleSheet(panel._cta_css(False))
    panel._set_state("error", "鉴权失败(401)",
                     "API Key 无效或已过期，检查 .env 后重试", foot_error=True)
    a.processEvents()
    save_widget(panel, os.path.join(OUT, "panel_error.png"), pad=16)

    print("[3b] 控制面板 — 区域已选态")
    panel._set_state("region", "区域已选", "区域位置 1920, 1080 · 拖动边框可调整")
    a.processEvents()
    save_widget(panel, os.path.join(OUT, "panel_region.png"), pad=16)

    print("[4] 覆盖层 — 像素级贴字样例（垫一层模拟原文）")
    ov = app_mod.PixelOverlay()
    ov.resize(760, 440)
    ov.set_blocks([
        app_mod.TextBlock("设置", 30, 62, 60, 30),
        app_mod.TextBlock("显示亮度", 30, 142, 120, 28),
        app_mod.TextBlock("网络连接", 30, 222, 120, 28),
        app_mod.TextBlock("自动备份", 30, 302, 90, 28),
        app_mod.TextBlock("退出账号登录", 30, 382, 150, 28),
    ])
    a.processEvents()

    shot = QImage(ov.width(), ov.height(), QImage.Format_ARGB32)
    shot.fill(QColor("#f5f6f8"))
    pp = QPainter(shot)
    pp.fillRect(QRect(0, 0, ov.width(), 56), QColor("#262a36"))
    pp.setPen(QColor("#ffffff"))
    pp.setFont(QFont("Segoe UI", 18, QFont.Bold))
    pp.drawText(QRect(30, 14, 400, 30), Qt.AlignLeft | Qt.AlignVCenter, "Control Panel")
    pp.setPen(QColor("#14141a"))
    pp.setFont(QFont("Segoe UI", 14))
    for t, y in [("Display brightness", 140), ("Network connection", 220),
                 ("Automatic backup", 300), ("Sign out of account", 380)]:
        pp.drawText(QRect(30, y, 500, 30), Qt.AlignLeft | Qt.AlignVCenter, t)
    pp.end()

    pp = QPainter(shot)
    pp.drawPixmap(0, 0, ov.grab())
    pp.end()
    p4 = os.path.join(OUT, "overlay_demo.png")
    shot.save(p4, "PNG")
    red = count_px(shot, lambda c: c.red() > 110 and c.green() < 100 and c.blue() < 100)
    print("  → %s  (暗红贴字像素: %d)" % (p4, red))

    print("[5] 托盘图标")
    icon = panel.tray.icon()
    pm = icon.pixmap(64, 64)
    p5 = os.path.join(OUT, "tray_icon.png")
    pm.save(p5, "PNG")
    print("  → %s (%dx%d, sizes=%s)"
          % (p5, pm.width(), pm.height(),
             [(s.width(), s.height()) for s in icon.availableSizes()]))

    print("\n输出目录: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
