"""
覆盖层贴字精度测量 —— 不用肉眼，直接用像素统计量化。

测什么：
  1. 译文墨迹的包围盒 vs 原始文字框，判断字号是否被压小、是否上下偏移
  2. 白底遮罩的覆盖率（能不能盖住原英文）
  3. 长译文（超出原框）的溢出行为

用法:
    .venv/Scripts/python.exe test_overlay_metrics.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtCore import Qt, QRect
from PyQt5.QtGui import QPainter, QColor, QFont, QImage, QFontMetrics
from PyQt5.QtWidgets import QApplication

import app as app_mod


def render_block(block, canvas_w=800, canvas_h=600):
    """把单个 block 画到白底上，返回 QImage。"""
    ov = app_mod.PixelOverlay()
    ov.resize(canvas_w, canvas_h)
    ov.set_blocks([block])
    # 覆盖层用绝对坐标减 widget 原点，所以把 widget 原点摆到 (0,0)
    ov.move(0, 0)

    img = QImage(canvas_w, canvas_h, QImage.Format_ARGB32)
    img.fill(QColor("#ffffff"))

    # 先画"原文"：深灰英文，模拟真实界面
    p = QPainter(img)
    p.setPen(QColor("#333333"))
    p.setFont(QFont("Segoe UI", max(7, int(block.h * 0.55))))
    p.drawText(QRect(block.x, block.y, block.w, block.h),
               Qt.AlignLeft | Qt.AlignVCenter, "Sample English text")
    p.end()

    # 再叠译文层
    p = QPainter(img)
    ov.paint_with(p)
    p.end()
    return img


def ink_bbox(img, x0, y0, x1, y1, pred):
    """在给定窗口里找满足 pred 的像素包围盒。"""
    minx, miny, maxx, maxy = 10 ** 9, 10 ** 9, -1, -1
    for y in range(max(0, y0), min(img.height(), y1)):
        for x in range(max(0, x0), min(img.width(), x1)):
            c = img.pixelColor(x, y)
            if pred(c):
                minx = min(minx, x); maxx = max(maxx, x)
                miny = min(miny, y); maxy = max(maxy, y)
    if maxx < 0:
        return None
    return (minx, miny, maxx, maxy)


def text_ink(c):
    """
    译文**文字**墨迹（深红 #960000）。红色描边是 (200,0,0,90) 叠在白底上
    约 (249,232,232)，不会命中这个判定 —— 早先版本把描边也算进墨迹，
    导致高度永远"超标"，测不出真实字号问题。
    """
    return c.red() < 200 and c.green() < 70 and c.blue() < 70


def light_fill(c):
    """白底遮罩（纯白 + 半透白叠在深色上都偏亮）。"""
    return c.red() > 235 and c.green() > 235 and c.blue() > 235


def case(name, text, x, y, w, h):
    b = app_mod.TextBlock(text, x, y, w, h)
    img = render_block(b)
    path = "D:/hermes-data/cache/scratch/ui/metric_%s.png" % name
    img.save(path, "PNG")

    pad = 30
    window = (x - pad, y - pad, x + w + pad * 3, y + h + pad)
    red = ink_bbox(img, *window, pred=text_ink)

    print("\n[%s] 译文=%r 原框=(%d,%d) %dx%d" % (name, text, x, y, w, h))
    if red is None:
        print("  ✗ 没有检测到译文墨迹！")
        return None
    rx0, ry0, rx1, ry1 = red
    rw, rh = rx1 - rx0 + 1, ry1 - ry0 + 1
    print("  墨迹包围盒: (%d,%d) %dx%d" % (rx0, ry0, rw, rh))
    print("  高度占原框: %.1f%%  (理想 70~100%%)" % (100.0 * rh / h))
    print("  垂直偏移(墨迹中心 - 原框中心): %+.1f px" % ((ry0 + ry1) / 2.0 - (y + h / 2.0)))
    print("  水平偏移(墨迹左边 - 原框左边): %+d px" % (rx0 - x))
    print("  宽度占原框: %.1f%%" % (100.0 * rw / w))
    print("  图片: %s" % path)
    return dict(name=name, rh=rh, h=h, rw=rw, w=w,
                dy=(ry0 + ry1) / 2.0 - (y + h / 2.0),
                dx=rx0 - x, path=path)


def main():
    a = QApplication(sys.argv)
    print("字号策略自检 _fit_font:")
    for h in (16, 20, 23, 30, 40, 56):
        f = app_mod.PixelOverlay._fit_font(h, "设置")
        fm = QFontMetrics(f)
        print("  原高 %2dpx → 字号 %2dpt, 实际墨高 %2dpx (占 %.0f%%), 宽 '设置' %dpx"
              % (h, f.pointSize(), fm.height(), 100.0 * fm.height() / h,
                 fm.horizontalAdvance("设置")))

    results = []
    results.append(case("short", "设置", 100, 100, 120, 23))
    results.append(case("mid", "显示亮度", 100, 200, 180, 23))
    results.append(case("long", "管理你的个人资料以同步到云端", 100, 300, 180, 23))
    results.append(case("big", "控制面板", 100, 400, 220, 56))
    results.append(case("tiny", "退出", 100, 500, 60, 16))

    print("\n" + "=" * 60)
    ok = True
    # 判定区间必须对齐「block.h 是什么」：
    #   本测试里 block.h 直接就是目标墨高（真值），所以译文墨高应≈100%。
    #   早先写 55~85% 是拿 QFontMetrics.height()（含行距，比墨高大 ~1.3 倍）
    #   当基准留下的历史残留 —— 修好字号标定后它反而把正确渲染判成错误。
    # 下限放到 80% 容忍 antialias 边缘像素，上限 115% 容忍下伸部（如「置」）。
    for r in results:
        if r is None:
            ok = False
            continue
        ratio = 100.0 * r["rh"] / r["h"]
        flag = "" if 80 <= ratio <= 115 else "  ✗"
        if not (80 <= ratio <= 115):
            ok = False
        if abs(r["dy"]) > 4:
            print("✗ %s 垂直偏移 %.1fpx > 4px" % (r["name"], r["dy"]))
            ok = False
        print("%-6s 墨高占框 %5.1f%%  垂直偏移 %+.1fpx%s" % (r["name"], ratio, r["dy"], flag))
    print("结论: %s" % ("✓ 贴字尺寸与位置达标" if ok else "✗ 需要修正"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
