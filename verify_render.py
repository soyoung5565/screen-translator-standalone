"""
程序化验证 GUI 渲染（不依赖视觉模型）。

检查面板截图里：
  - 是否有深色背景（非纯黑）
  - 是否有白/浅色文字像素
  - 是否有强调色（GitHub Dark 绿 #2ea043）像素
  - 像素分布是否合理（不是全黑或全空）

用法:
    python verify_render.py                # 用默认截图
    python verify_render.py <png路径>      # 指定截图

注意（实测坑）:
  1. 截图必须是**真实运行的面板**。`ui_preview.py` 用 widget.grab() 渲染的图
     没有窗口边框/标题栏，亮度分布和真实窗口不同 —— 本脚本只对真实窗口截图有意义。
  2. 抓窗口必须在 **DPI 感知**的进程里做（先 SetProcessDpiAwareness(2)），
     否则 GetWindowRect 返回被系统虚拟化的坐标，抓到的图错位/缺块。
     更稳的是 PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)。
  3. 本机 UI 已从青色 #00c8ff 改为 GitHub Dark 绿 #2ea043，判定色必须跟着改，
     否则「青色像素太少」会一直误报。
"""
import os
import sys
from collections import Counter

from PIL import Image

DEFAULT = "D:/hermes-data/cache/scratch/ui/exe_panel_full.png"


def main(argv):
    path = argv[1] if len(argv) > 1 else DEFAULT
    if not os.path.exists(path):
        print("✗ 截图不存在:", path)
        print("  先真实启动 exe，再用 PrintWindow 抓窗口（见 docs/进展记录.md）")
        return 2
    PATH = path
    img = Image.open(PATH).convert("RGB")
    w, h = img.size
    px = img.load()
    print("面板尺寸:", (w, h))

    total = w * h
    buckets = Counter()
    cyan = whiteish = accent = 0
    bright = 0

    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            lum = (r * 299 + g * 587 + b * 114) / 1000.0
            if lum < 50:
                buckets["很暗(<50)"] += 1
            elif lum < 120:
                buckets["暗(50-120)"] += 1
            elif lum < 200:
                buckets["中(120-200)"] += 1
            else:
                buckets["亮(>200)"] += 1
                bright += 1
            if g > 130 and b > 140 and r < 90:
                cyan += 1
            # GitHub Dark 绿 #2ea043 ≈ (46,160,67)：绿显著高于红蓝
            if g > 110 and g > r + 60 and g > b + 55:
                accent += 1
            if lum > 190 and abs(r - g) < 45 and abs(g - b) < 45:
                whiteish += 1

    print("总像素:", total)
    print("亮度分布:")
    for k in ["很暗(<50)", "暗(50-120)", "中(120-200)", "亮(>200)"]:
        print("  %-14s %6d  %5.1f%%" % (k, buckets[k], 100.0 * buckets[k] / total))
    print("强调绿像素(#2ea043 系): %d" % accent)
    print("浅色文字像素: %d" % whiteish)

    dark_ratio = (buckets["很暗(<50)"] + buckets["暗(50-120)"]) / float(total)
    print("深色占比: %.1f%%" % (100 * dark_ratio))

    ok = True
    if dark_ratio < 0.5:
        print("✗ 深色占比太低 —— 面板可能没渲染，或抓到的是桌面")
        ok = False
    if whiteish < 300:
        print("✗ 文字像素太少 —— UI 元素可能没画出来")
        ok = False
    if accent < 50:
        print("✗ 强调绿像素太少 —— 主按钮/滑块/徽章没生效")
        ok = False
    if bright > total * 0.6:
        print("✗ 亮像素过多 —— 大概率拍到了桌面而非面板")
        ok = False

    print("\n结论:", "✓ 面板渲染正常（深色底 + 文字 + 绿色强调齐全）" if ok else "✗ 渲染有问题")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
