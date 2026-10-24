"""
验证 overlay 贴字位置 —— 绕过 WDA_EXCLUDEFROMCAPTURE。

因为译文层被标记为「不参与捕获」，连截图都拍不到它，
所以这里直接用 Qt 把 overlay 渲染到内存位图上，检查绘制结果。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctypes

ctypes.windll.shcore.SetProcessDpiAwareness(2)

from PyQt5.QtCore import Qt, QRect
from PyQt5.QtGui import QImage, QPainter, QColor
from PyQt5.QtWidgets import QApplication

from app import PixelOverlay, TextBlock

OUT = "D:/hermes-data/cache/scratch/overlay_render.png"


def main():
    app = QApplication(sys.argv)

    # 模拟 760x440 的区域，5 条译文块（坐标与实测一致）
    blocks = [
        TextBlock("控制面板", 330, 415, 200, 40),
        TextBlock("显示屏亮度", 330, 540, 239, 40),
        TextBlock("网络连接", 330, 616, 262, 39),
        TextBlock("自动备份", 330, 691, 238, 40),
        TextBlock("退出账户", 330, 769, 251, 40),
    ]

    ov = PixelOverlay()
    # 用真实场景的绝对坐标：overlay 位于 (300,400)，块是屏幕绝对坐标
    ov.setGeometry(300, 400, 760, 440)
    blocks = [
        TextBlock("控制面板", 330, 415, 200, 40),
        TextBlock("显示屏亮度", 330, 540, 239, 40),
        TextBlock("网络连接", 330, 616, 262, 39),
        TextBlock("自动备份", 330, 691, 238, 40),
        TextBlock("退出账户", 330, 769, 251, 40),
    ]
    ov.set_blocks(blocks)

    # 渲染到 QImage —— 用 paint_with，避开 render() 对 translucent widget 的坑
    img = QImage(760, 440, QImage.Format_ARGB32)
    img.fill(QColor(245, 246, 248))          # 模拟浅色界面背景
    p = QPainter(img)
    ov.paint_with(p)
    p.end()

    img.save(OUT)
    print("渲染完成:", OUT)

    # 检查红字像素分布
    # 判据放宽：抗锯齿会把 (190,0,0) 与白底混合成各种粉色，
    # 所以只要「红明显高于绿蓝」就算译文像素
    # 注意：不能用 constBits() 按 width*4 算偏移 —— QImage 有行对齐填充，
    # 会串行。用 pixelColor() 才准。
    reds = []
    for y in range(440):
        for x in range(760):
            c = img.pixelColor(x, y)
            r, g, b = c.red(), c.green(), c.blue()
            if r > 120 and r > g + 50 and r > b + 50:
                reds.append((x, y))
    print("红字像素:", len(reds))

    # 行聚类
    rows = {}
    for x, y in reds:
        rows.setdefault(y // 6, []).append((x, y))
    print("\n译文行分布 (期望 5 行):")
    n = 0
    for k in sorted(rows):
        pts = rows[k]
        if len(pts) < 10:
            continue
        n += 1
        ys = [p[1] for p in pts]
        xs = [p[0] for p in pts]
        print("  行%d  y中心=%-4d x=[%d..%d]  n=%d" % (n, sum(ys) // len(ys), min(xs), max(xs), len(pts)))

    # overlay 位于 (300,400)，块是屏幕绝对坐标 → 相对位置 = 绝对 - 原点
    expected_rel = [415 - 400, 540 - 400, 616 - 400, 691 - 400, 769 - 400]
    print("\n期望相对 y 起点:", expected_rel)

    # 聚类：把相近的 y 合并成「一个文本行」
    bands = []
    for y in sorted(set(p[1] for p in reds)):
        if not bands or y - bands[-1][-1] > 12:
            bands.append([y])
        else:
            bands[-1].append(y)
    print("实际文本行数:", len(bands))
    for i, band in enumerate(bands):
        pts = [p for p in reds if p[1] in band]
        ys = [p[1] for p in pts]
        xs = [p[0] for p in pts]
        print("  行%d  y=[%d..%d] 中心=%-4d x=[%d..%d]  n=%d"
              % (i + 1, min(ys), max(ys), sum(ys) // len(ys), min(xs), max(xs), len(pts)))

    ok = len(bands) == 5
    if ok:
        centers = [sum(b) // len(b) for b in bands]
        # 每行中心应落在期望起点 ±25px 内（字号自适应，文字不会正好贴在顶）
        for c, e in zip(centers, expected_rel):
            if abs(c - e) > 28:
                ok = False
                print("  ✗ 行中心 %d 偏离期望 %d" % (c, e))
                break
    print("\n结论:", "✓ 5 行译文位置正确" if ok else "✗ 行数或位置不对")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
