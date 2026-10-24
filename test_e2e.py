"""
端到端测试：合成「屏幕截图」→ LLM 视觉翻译（带坐标）→ 贴回原位。

不依赖 OCR —— 文字识别与定位全部由多模态模型完成。

验证：
  1. LLM 返回的 JSON 能否被正确解析
  2. 坐标是否落在合理范围（0-1000，且与实际文字位置吻合）
  3. 归一化坐标 → 屏幕绝对坐标的换算是否正确
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw, ImageFont

import config as cfg_mod
import engine as engine_mod
from app import boxes_to_blocks

W, H = 760, 440
LINES = [
    ("Control Panel", 30, 42),
    ("Display brightness", 30, 122),
    ("Network connection", 30, 202),
    ("Automatic backup", 30, 282),
    ("Sign out of account", 30, 362),
]


def make_screenshot():
    img = Image.new("RGB", (W, H), (245, 246, 248))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 56], fill=(38, 42, 54))
    try:
        f_title = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 26)
        f_item = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 22)
    except Exception:
        f_title = f_item = ImageFont.load_default()
    d.text((30, 14), "Control Panel", fill="white", font=f_title)
    for text, x, y in LINES[1:]:
        d.text((x, y), text, fill=(20, 20, 25), font=f_item)
    path = "D:/hermes-data/cache/scratch/llm_screenshot.png"
    img.save(path)
    return img, path


def main():
    img, path = make_screenshot()
    print("合成截图:", path, img.size)

    cfg = cfg_mod.load_config()
    tr = engine_mod.LLMTranslator(cfg["api_key"], cfg["base_url"], cfg["model"], "anthropic")

    import base64, io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=78)
    b64 = base64.b64encode(buf.getvalue()).decode()

    print("\n=== 请求 LLM 视觉翻译（带坐标） ===")
    boxes = tr.translate_image_boxes(b64, "auto", "zh")

    print("解析到 %d 个文字块:" % len(boxes))
    for b in boxes:
        print("  原文=%-24r 译文=%-14r  x=%-6.1f y=%-6.1f w=%-6.1f h=%-6.1f"
              % (b.get("t"), b.get("zh"), b["x"], b["y"], b["w"], b["h"]))

    if not boxes:
        print("✗ 没拿到结构化结果")
        return 1

    # 校验坐标范围
    bad = [b for b in boxes if not (0 <= b["x"] <= 1000 and 0 <= b["y"] <= 1000
                                   and 0 < b["w"] <= 1000 and 0 < b["h"] <= 1000)]
    print("\n坐标越界条数:", len(bad))
    if bad:
        for b in bad:
            print("  ✗", b)

    # 换算成屏幕绝对坐标（区域从 100,100 开始）
    RX, RY = 100, 100
    blocks = boxes_to_blocks(boxes, RX, RY, W, H)
    print("\n=== 归一化 → 屏幕绝对坐标 ===")
    for b in blocks:
        print("  %-14r → 绝对 (%d,%d) %dx%d" % (b.text, b.x, b.y, b.w, b.h))

    # 渲染效果图
    out = img.copy()
    dd = ImageDraw.Draw(out, "RGBA")
    try:
        f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 11)
    except Exception:
        f = ImageFont.load_default()
    for b in blocks:
        dd.rectangle([b.x - RX, b.y - RY, b.x - RX + b.w, b.y - RY + b.h],
                     fill=(255, 255, 255, 160))
        dd.text((b.x - RX, b.y - RY), b.text, fill=(190, 0, 0), font=f)
    op = "D:/hermes-data/cache/scratch/llm_overlay.png"
    out.save(op)
    print("\n效果图:", op)

    ok = len(boxes) >= 4 and not bad
    print("\n结论:", "✓ LLM 坐标输出可用" if ok else "✗ 需要调提示词")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
