"""
GUI 端到端验证 + 幻觉回归测试。

流程（模拟真实使用的两轮迭代）：
  第1轮：截屏 → LLM 翻译 → 贴到 overlay
  第2轮：再次截屏（overlay 已贴了译文）→ LLM 再翻译
         验证第2轮不会把第1轮的译文当成原文（幽灵行）

关键：overlay 用了 SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)，
截图里应该看不到译文。
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ctypes.windll.shcore.SetProcessDpiAwareness(2)

from PyQt5.QtCore import Qt, QRect
from PyQt5.QtGui import QPainter, QColor, QFont
from PyQt5.QtWidgets import QApplication, QWidget

import mss
from PIL import Image

import config as cfg_mod
import engine as engine_mod
from app import CaptureRegion, PixelOverlay, boxes_to_blocks, to_jpeg_b64

MOCK_X, MOCK_Y = 300, 400
MOCK_W, MOCK_H = 760, 440


class MockScreen(QWidget):
    ITEMS = [
        ("Control Panel", 30, 40),
        ("Display brightness", 30, 140),
        ("Network connection", 30, 220),
        ("Automatic backup", 30, 300),
        ("Sign out of account", 30, 380),
    ]

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setGeometry(MOCK_X, MOCK_Y, MOCK_W, MOCK_H)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(245, 246, 248))
        p.fillRect(QRect(0, 0, MOCK_W, 56), QColor(38, 42, 54))
        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont("Segoe UI", 18, QFont.Bold))
        p.drawText(QRect(30, 14, 400, 30), Qt.AlignLeft | Qt.AlignVCenter, "Control Panel")
        p.setPen(QColor(20, 20, 25))
        p.setFont(QFont("Segoe UI", 14))
        for text, x, y in self.ITEMS[1:]:
            p.drawText(QRect(x, y, 500, 30), Qt.AlignLeft | Qt.AlignVCenter, text)


def grab_region():
    with mss.mss() as s:
        im = s.grab({"left": MOCK_X, "top": MOCK_Y,
                     "width": MOCK_W, "height": MOCK_H})
        return Image.frombytes("RGB", im.size, im.bgra, "raw", "BGRX")


def main():
    app = QApplication(sys.argv)
    mock = MockScreen()
    mock.show()

    region = CaptureRegion()
    region.setGeometry(MOCK_X, MOCK_Y, MOCK_W, MOCK_H)
    region.show()

    overlay = PixelOverlay()
    overlay.setGeometry(region.geometry())
    overlay.show()
    app.processEvents()
    time.sleep(1.2)
    app.processEvents()

    cfg = cfg_mod.load_config()
    tr = engine_mod.LLMTranslator(cfg["api_key"], cfg["base_url"], cfg["model"], "anthropic")

    # ── 第 1 轮 ──
    print("=== 第 1 轮：截屏 → LLM → 贴字 ===")
    img1 = grab_region()
    img1.save("D:/hermes-data/cache/scratch/iter1_before.png")
    boxes1 = tr.translate_image_boxes(to_jpeg_b64(img1), "auto", "zh")
    print("第1轮 %d 块:" % len(boxes1))
    for b in boxes1:
        print("   %-22r → %-12r (%.0f,%.0f %.0fx%.0f)"
              % (b.get("t"), b.get("zh"), b["x"], b["y"], b["w"], b["h"]))
    blocks1 = boxes_to_blocks(boxes1, MOCK_X, MOCK_Y, MOCK_W, MOCK_H)
    overlay.set_blocks(blocks1)
    app.processEvents()
    time.sleep(1.0)
    app.processEvents()

    # 验证：overlay 贴字后，截屏里应该看不到译文
    img2 = grab_region()
    img2.save("D:/hermes-data/cache/scratch/iter2_before.png")

    diff = 0
    p1, p2 = img1.load(), img2.load()
    for y in range(0, img1.size[1], 3):
        for x in range(0, img1.size[0], 3):
            if sum(p1[x, y]) != sum(p2[x, y]):
                diff += 1
    total = len(range(0, img1.size[1], 3)) * len(range(0, img1.size[0], 3))
    print("\n两轮截图差异像素: %d / %d (%.1f%%)" % (diff, total, 100.0 * diff / total))
    print("→ 译文层已从截图排除" if diff / total < 0.02 else "→ 译文层仍在截图里（自反馈风险）")

    # ── 第 2 轮 ──
    print("\n=== 第 2 轮：再截一次 → LLM 再翻（幻觉检查）===")
    boxes2 = tr.translate_image_boxes(to_jpeg_b64(img2), "auto", "zh")
    print("第2轮 %d 块:" % len(boxes2))
    for b in boxes2:
        print("   %-22r → %-12r (%.0f,%.0f %.0fx%.0f)"
              % (b.get("t"), b.get("zh"), b["x"], b["y"], b["w"], b["h"]))

    # 幻觉检测：原文里不该出现纯中文
    zh_only = [b for b in boxes2
               if b.get("t") and not any(c.isascii() and c.isalpha() for c in b["t"])]
    print("\n第2轮中'原文不是拉丁字母'的可疑条目: %d" % len(zh_only))
    for b in zh_only:
        print("   ✗ 幽灵:", b)

    blocks2 = boxes_to_blocks(boxes2, MOCK_X, MOCK_Y, MOCK_W, MOCK_H)
    overlay.set_blocks(blocks2)
    app.processEvents()
    time.sleep(0.8)
    app.processEvents()

    with mss.mss() as s:
        full = s.grab(s.monitors[0])
        fimg = Image.frombytes("RGB", full.size, full.bgra, "raw", "BGRX")
        crop = fimg.crop((MOCK_X - 20, MOCK_Y - 20,
                          MOCK_X + MOCK_W + 20, MOCK_Y + MOCK_H + 20))
        crop.save("D:/hermes-data/cache/scratch/gui_final.png")
    print("\n最终效果图: gui_final.png")

    ok = len(boxes1) >= 4 and len(boxes2) >= 4 and len(zh_only) == 0
    print("结论:", "✓ 两轮一致，无幻觉" if ok else "✗ 仍有问题")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
