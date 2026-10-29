"""诊断分段控件的 QSS 渲染 —— 为什么凹陷槽没出来。

用法:
    .venv/Scripts/python.exe diag_seg.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from collections import Counter

from PyQt5.QtWidgets import QApplication, QWidget, QSystemTrayIcon

QWidget.show = lambda self: None
QSystemTrayIcon.show = lambda self: None
QSystemTrayIcon.showMessage = lambda *a, **k: None

from ui_components import SegmentedControl

CSS = (
    "QWidget#segControl{background:#12171f;border:1px solid #6e7681;border-radius:6px;}"
    "QWidget#segControl QPushButton{background:transparent;border:1px solid transparent;"
    "border-radius:4px;color:#8b949e;font-size:11px;padding:0;}"
    "QWidget#segControl QPushButton:checked{background:#252c38;border-color:#6e7681;"
    "color:#e6edf3;}"
)


def probe(css, label):
    a = QApplication.instance() or QApplication([])
    seg = SegmentedControl([("像素级 · 原位贴字", True), ("整图翻译", False)])
    seg.resize(290, 32)
    seg.setStyleSheet(css)
    a.processEvents()
    img = seg.grab().toImage()

    def px(x, y):
        c = img.pixelColor(x, y)
        return (c.red(), c.green(), c.blue())

    cnt = Counter()
    for y in range(img.height()):
        for x in range(img.width()):
            cnt[img.pixelColor(x, y).name()] += 1
    print("\n=== %s ===" % label)
    print("  size           :", img.width(), img.height())
    print("  (2,2)  外边    :", px(2, 2))
    print("  (4,16) 左描边  :", px(4, 16))
    print("  (60,16) 段1中  :", px(60, 16))
    print("  (146,16) 段间隙:", px(146, 16))
    print("  (215,16) 段2中 :", px(215, 16))
    print("  (287,16) 右描边:", px(287, 16))
    print("  top colors     :", cnt.most_common(6))
    return img


def main():
    probe(CSS, "方案 A：QWidget#segControl 前缀（当前实现）")

    css_b = (
        "QWidget#segControl{background:#12171f;border:1px solid #6e7681;border-radius:6px;}"
        "QPushButton{background:transparent;border:1px solid transparent;"
        "border-radius:4px;color:#8b949e;font-size:11px;padding:0;}"
        "QPushButton:checked{background:#252c38;border-color:#6e7681;color:#e6edf3;}"
    )
    probe(css_b, "方案 B：容器用 #id，按钮用裸 QPushButton")

    css_c = (
        "QWidget#segControl{background:#12171f;border:1px solid #6e7681;border-radius:6px;}"
        "QPushButton{background:transparent;border:1px solid transparent;"
        "border-radius:4px;color:#8b949e;font-size:11px;padding:0;}"
        "QPushButton:checked{background:#252c38;border-color:#6e7681;color:#e6edf3;}"
    )
    probe(css_c, "方案 C：与 B 相同（对照）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
