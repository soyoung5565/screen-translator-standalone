"""
程序化验证 GUI 渲染（不依赖视觉模型）。

检查面板截图里：
  - 是否有深色背景（非纯黑）
  - 是否有青色/白色文字像素
  - 像素分布是否合理（不是全黑或全空）
"""
import os
from collections import Counter

from PIL import Image

PATH = "D:/hermes-data/cache/scratch/gui_panel2.png"


def main():
    if not os.path.exists(PATH):
        print("✗ 截图不存在:", PATH)
        return 1
    img = Image.open(PATH).convert("RGB")
    w, h = img.size
    px = img.load()
    print("面板尺寸:", (w, h))

    total = w * h
    buckets = Counter()
    cyan = whiteish = 0
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
            if lum > 190 and abs(r - g) < 45 and abs(g - b) < 45:
                whiteish += 1

    print("总像素:", total)
    print("亮度分布:")
    for k in ["很暗(<50)", "暗(50-120)", "中(120-200)", "亮(>200)"]:
        print("  %-14s %6d  %5.1f%%" % (k, buckets[k], 100.0 * buckets[k] / total))
    print("青色像素(#00c8ff 系): %d" % cyan)
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
    if cyan < 50:
        print("✗ 青色像素太少 —— 标题/强调色没生效")
        ok = False
    if bright > total * 0.6:
        print("✗ 亮像素过多 —— 大概率拍到了桌面而非面板")
        ok = False

    print("\n结论:", "✓ 面板渲染正常（深色底 + 文字 + 青色强调齐全）" if ok else "✗ 渲染有问题")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
