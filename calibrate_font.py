"""
字号系数标定 —— 实测「像素字号 → CJK 墨高/墨宽」的映射关系。

为什么不靠推理：CJK 字形在 QFontMetrics 里的 height() 含行距，
而实际墨迹（ink）高度又比 height() 小一截，两者关系因字体/DPI 而变。
必须实测出比例，再据此定 _fit_font 的系数，否则字号会系统性偏小
（实测旧系数 0.72 导致中文墨高只有原文的 54%）。

用法:
    .venv/Scripts/python.exe calibrate_font.py
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
from PyQt5.QtGui import QPainter, QColor, QFont, QImage, QFontMetrics
from PyQt5.QtWidgets import QApplication

SAMPLE = "显示亮度网络"


def ink_metrics(px_size, text=SAMPLE):
    """把 text 用指定像素字号画到透明画布上，量真实墨迹包围盒。"""
    f = QFont("Microsoft YaHei")
    f.setPixelSize(px_size)
    fm = QFontMetrics(f)
    w = max(40, fm.horizontalAdvance(text) + 20)
    h = max(20, fm.height() + 20)

    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setPen(QColor(0, 0, 0, 255))
    p.setFont(f)
    p.drawText(QRect(0, 0, w, h), Qt.AlignLeft | Qt.AlignVCenter, text)
    p.end()

    minx, miny, maxx, maxy = 10 ** 9, 10 ** 9, -1, -1
    for y in range(h):
        for x in range(w):
            if img.pixelColor(x, y).alpha() > 40:
                minx = min(minx, x); maxx = max(maxx, x)
                miny = min(miny, y); maxy = max(maxy, y)

    if maxx < 0:
        return None
    ink_h = maxy - miny + 1
    ink_w = maxx - minx + 1
    return {
        "px_size": px_size,
        "fm_height": fm.height(),
        "fm_ascent": fm.ascent(),
        "ink_h": ink_h,
        "ink_w": ink_w,
        "ink_per_fm": ink_h / float(fm.height()),
        "ink_per_px": ink_h / float(px_size),
    }


def main():
    a = QApplication(sys.argv)

    print("字体: Microsoft YaHei   样本: %r\n" % SAMPLE)
    print("%8s %10s %9s %9s %10s %10s" %
          ("像素字号", "fm.height", "墨高", "墨宽", "墨高/fm高", "墨高/字号"))

    rows = []
    for px in (8, 10, 12, 14, 16, 18, 20, 24, 28, 32, 40, 48, 56):
        m = ink_metrics(px)
        if not m:
            continue
        rows.append(m)
        print("%8d %10d %9d %9d %10.3f %10.3f"
              % (m["px_size"], m["fm_height"], m["ink_h"], m["ink_w"],
                 m["ink_per_fm"], m["ink_per_px"]))

    # 用中段样本算平均比例（小字号受抗锯齿影响大）
    mid = [r for r in rows if 16 <= r["px_size"] <= 48]
    avg_ink_per_px = sum(r["ink_per_px"] for r in mid) / len(mid)
    avg_ink_per_fm = sum(r["ink_per_fm"] for r in mid) / len(mid)

    print("\n中段(%d~%dpx)平均:" % (min(r["px_size"] for r in mid), max(r["px_size"] for r in mid)))
    print("  墨高 ≈ %.3f × 像素字号" % avg_ink_per_px)
    print("  墨高 ≈ %.3f × fm.height()" % avg_ink_per_fm)
    print("\n→ 想让「墨高 = 目标高度 h」时应取:")
    print("  像素字号 ≈ h / %.3f = %.3f × h" % (avg_ink_per_px, 1.0 / avg_ink_per_px))
    print("\n对照当前 _fit_font 系数 0.72 —— 应改为 %.3f" % (1.0 / avg_ink_per_px))
    return 0


if __name__ == "__main__":
    sys.exit(main())
