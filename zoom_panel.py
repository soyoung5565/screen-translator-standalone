"""把面板渲染图放大 2×，逐区域检查像素级细节。

用法:
    .venv/Scripts/python.exe zoom_panel.py <png> <out> [x y w h]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QImage, QPainter, QColor
from PyQt5.QtWidgets import QApplication


def main():
    src = sys.argv[1]
    out = sys.argv[2]
    a = QApplication.instance() or QApplication([])
    img = QImage(src)
    if img.isNull():
        raise SystemExit("cannot load " + src)
    if len(sys.argv) >= 7:
        x, y, w, h = (int(v) for v in sys.argv[3:7])
        img = img.copy(QRect(x, y, w, h))
    scale = 3
    big = QImage(img.width() * scale, img.height() * scale, QImage.Format_ARGB32)
    big.fill(QColor("#000000"))
    p = QPainter(big)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.drawImage(0, 0, img.scaled(img.width() * scale, img.height() * scale,
                                Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
    p.end()
    big.save(out, "PNG")
    print("saved", out, big.width(), "x", big.height())
    return 0


if __name__ == "__main__":
    sys.exit(main())
