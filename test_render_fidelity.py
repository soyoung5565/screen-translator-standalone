"""
渲染保真度test —— 走**真实** PixelOverlay 绘制路径，不弹窗口。

为什么需要这个文件：
  旧的 test_e2e.py 用 PIL 手绘贴字框（alpha=160、11pt 固定），根本没经过
  PixelOverlay.paint_with，所以它出的"效果图"不能证明实际渲染质量
  （实测那份图确实是中英叠字）。

测三件事：
  1. 遮盖率 —— 原英文墨迹被译文遮罩抹掉了多少（越高越好）
  2. 垂直对齐 —— 译文墨迹中心 vs 原英文墨迹中心（越小越好）
  3. 字号占比 —— 译文墨高 / 原英文墨高（接近 1 最好）

用法:
    .venv/Scripts/python.exe test_render_fidelity.py
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
from PyQt5.QtWidgets import QApplication, QWidget

import app as app_mod

# 与 test_e2e 相同的模拟界面，但这里用 Qt 画，走真实覆盖层
W, H = 760, 440
LINES = [
    ("Control Panel", 30, 14, 26),
    ("Display brightness", 30, 122, 22),
    ("Network connection", 30, 202, 22),
    ("Automatic backup", 30, 282, 22),
    ("Sign out of account", 30, 362, 22),
]

OUT = "D:/hermes-data/cache/scratch/ui"


def render_background():
    """模拟一张"英文界面"截图。"""
    img = QImage(W, H, QImage.Format_ARGB32)
    img.fill(QColor("#f5f6f8"))
    p = QPainter(img)
    p.fillRect(QRect(0, 0, W, 56), QColor("#262a36"))
    for text, x, y, px in LINES:
        f = QFont("Segoe UI")
        f.setPixelSize(px)
        p.setFont(f)
        p.setPen(QColor("#ffffff") if y < 60 else QColor("#14141a"))
        p.drawText(QRect(x, y, 600, 34), Qt.AlignLeft | Qt.AlignTop, text)
    p.end()
    return img


def ink_bbox(img, x0, y0, x1, y1, pred):
    minx, miny, maxx, maxy = 10 ** 9, 10 ** 9, -1, -1
    for y in range(max(0, y0), min(img.height(), y1)):
        for x in range(max(0, x0), min(img.width(), x1)):
            if pred(img.pixelColor(x, y)):
                minx = min(minx, x); maxx = max(maxx, x)
                miny = min(miny, y); maxy = max(maxy, y)
    if maxx < 0:
        return None
    return (minx, miny, maxx, maxy)


def make_en_ink(bg):
    """
    构造一个"英文墨迹"判定器：对**给定背景**判断某像素原本是不是英文。

    为什么不能写死阈值：标题栏是深底白字（白=墨迹），正文是浅底黑字
    （黑=墨迹）。早先统一只认"暗色中性"，把白字标题全漏掉，遮盖率
    凭空掉到 52%。这里按背景亮度自动翻转：背景暗 → 找亮的；背景亮 → 找暗的。
    """
    bg_lum_cache = {}

    def pred_at(x, y):
        return None

    def is_ink(c, bg_lum):
        lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
        # 中性色（R≈G≈B），排除红色译文
        if (c.red() - c.green()) >= 40 or (c.red() - c.blue()) >= 40:
            return False
        if bg_lum < 100:          # 深色底 → 白字是墨迹
            return lum > 150
        return lum < 130          # 浅色底 → 暗字是墨迹

    return is_ink


def zh_ink(c):
    """浅底译文文字：深红 #960000。"""
    return c.red() < 200 and c.green() < 70 and c.blue() < 70


def zh_ink_dark(c):
    """深底译文文字：浅绿 #8cf0aa（深底自动切换的配色）。"""
    return c.green() > 180 and c.red() > 90 and c.red() < 200 and c.blue() < 200


def count_ink(img, x, y, w, h, pred, bg_lum):
    n = 0
    for yy in range(y, min(y + h, img.height())):
        for xx in range(x, min(x + w, img.width())):
            if pred(img.pixelColor(xx, yy), bg_lum):
                n += 1
    return n


def block_bg_lum(bg, x, y, w, h):
    """
    估计这个块附近的**背景**亮度。

    ⚠️ 用 90 分位取到的是"白字本身"而不是背景，导致深色标题栏被误判成
    浅底、白字被当成背景 → 遮盖率算出 -584%。背景是框内占多数的颜色，
    所以取中位数才对。
    """
    vals = []
    for yy in range(max(0, y), min(y + h, bg.height()), 2):
        for xx in range(max(0, x), min(x + w, bg.width()), 2):
            c = bg.pixelColor(xx, yy)
            vals.append(0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue())
    if not vals:
        return 255
    vals.sort()
    return vals[len(vals) // 2]


def main():
    a = QApplication(sys.argv)
    os.makedirs(OUT, exist_ok=True)

    bg = render_background()
    bg_path = os.path.join(OUT, "fidelity_before.png")
    bg.save(bg_path, "PNG")

    # ── 用真实 LLM 的坐标（缓存里已有同一张图的解析结果时直接复用）──
    import config as cfg_mod
    import engine as engine_mod
    from app import to_jpeg_b64

    cfg = cfg_mod.load_config()
    tr = engine_mod.LLMTranslator(cfg["api_key"], cfg["base_url"],
                                  cfg["model"], cfg["anthropic"] if False else "anthropic")

    import base64, io
    buf = io.BytesIO()
    # QImage → JPEG b64
    tmp = os.path.join(OUT, "_fidelity_input.jpg")
    bg.save(tmp, "JPEG", 78)
    with open(tmp, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    from PIL import Image as PILImage
    pil_bg = PILImage.open(tmp).convert("RGB")

    print("=== 请求 LLM 视觉坐标 ===")
    boxes = tr.translate_image_boxes(b64, "auto", "zh")
    for b in boxes:
        print("  %-24r → %-12r (%.0f,%.0f %.0fx%.0f)"
              % (b.get("t"), b.get("zh"), b["x"], b["y"], b["w"], b["h"]))

    if len(boxes) < len(LINES):
        print("⚠ LLM 只识别到 %d/%d 行（模型漏行，属已知限制）" % (len(boxes), len(LINES)))

    # 除了测 LLM 实返坐标，再补一组"真值坐标"，用来单独评估渲染质量
    # —— 否则模型漏行会让渲染质量测不全。
    gt_boxes = []
    for text, x, y, px in LINES:
        f = QFont("Segoe UI")
        f.setPixelSize(px)
        fm = QFontMetrics(f)
        tw = fm.horizontalAdvance(text)
        # ⚠️ 框高要用**真实墨高**，不能用 fm.height()。
        #    fm.height() 含行距，比墨高大 ~1.3 倍；LLM 返回的 h 实测接近墨高。
        #    早先用 fm.height() 当 h，等于把"原文"人为放大，字号被算成 1.4 倍。
        glyph_h = _ink_height_of(text, px)
        gt_boxes.append({"t": text, "zh": GT_ZH[text],
                         "x": x / float(W) * 1000.0,
                         "y": _ink_top_of(text, x, y, px) / float(H) * 1000.0,
                         "w": tw / float(W) * 1000.0,
                         "h": glyph_h / float(H) * 1000.0})

    # 真值路径也要带上背景亮度 —— 否则深色标题栏那块会走"白罩"分支，
    # 测出来的不是真实运行时的行为。
    gt_blocks = app_mod.boxes_to_blocks(gt_boxes, 0, 0, W, H, img=pil_bg)
    for b in gt_blocks:
        print("  真值块 %-14r bg_lum=%s" % (b.text, b.bg_lum))

    # 三组对比：
    #   1. LLM 实返坐标（不校正）—— 现状基线
    #   2. LLM 实返坐标 + 墨迹校正 —— 生产路径的真实效果
    #   3. 真值坐标 —— 单独评估渲染质量（不受模型噪声影响）
    rc = run_case(a, bg, "LLM 实返坐标（未校正）", boxes, "fidelity_after_llm.png", pil_bg)
    rcf = run_case(a, bg, "LLM 实返坐标 + 墨迹校正", boxes,
                   "fidelity_after_llm_refined.png", pil_bg, refine=True)
    rg = run_case(a, bg, "真值坐标（单测渲染）", gt_boxes, "fidelity_after_gt.png", pil_bg)

    # 4. 确定性回归：把真值坐标按模型的典型劣化劣化一遍，
    #    再看墨迹校正能不能在**真实渲染路径**里救回来。
    #    这一步不依赖模型，任何时候跑结果都一样 —— 是真正的回归测试。
    bad = []
    for g in gt_boxes:
        bad.append({
            "t": g["t"], "zh": g["zh"],
            # 模型实测倾向：w 偏窄 ~45%、h 偏小 ~60%、y 偏移几像素
            "x": g["x"] + 1.0,
            "y": max(0.0, g["y"] - 2.0),
            "w": g["w"] * 0.55,
            "h": max(1.0, g["h"] * 0.40),
        })
    rb_before = run_case(a, bg, "模拟模型坏坐标（未校正）", bad,
                         "fidelity_bad_raw.png", pil_bg)
    rb_after = run_case(a, bg, "模拟模型坏坐标 + 墨迹校正", bad,
                        "fidelity_bad_refined.png", pil_bg, refine=True)
    if rb_after != 0:
        print("✗ 确定性回归失败：墨迹校正没能救回劣化坐标")
    return 0 if (rg == 0 and rcf == 0 and rb_after == 0) else 1


GT_ZH = {
    "Control Panel": "控制面板",
    "Display brightness": "显示亮度",
    "Network connection": "网络连接",
    "Automatic backup": "自动备份",
    "Sign out of account": "退出账户",
}


def _ink_box_of(text, px, region_x, region_y, region_w, region_h):
    """用同一字体画一遍，量真实墨迹包围盒（相对画布）。"""
    f = QFont("Segoe UI")
    f.setPixelSize(px)
    fm = QFontMetrics(f)
    cw = max(60, fm.horizontalAdvance(text) + 20)
    ch = max(30, fm.height() + 20)
    img = QImage(cw, ch, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setPen(QColor(0, 0, 0, 255))
    p.setFont(f)
    p.drawText(QRect(0, 0, cw, ch), Qt.AlignLeft | Qt.AlignTop, text)
    p.end()
    minx, miny, maxx, maxy = 10 ** 9, 10 ** 9, -1, -1
    for yy in range(ch):
        for xx in range(cw):
            if img.pixelColor(xx, yy).alpha() > 40:
                minx = min(minx, xx); maxx = max(maxx, xx)
                miny = min(miny, yy); maxy = max(maxy, yy)
    if maxx < 0:
        return (0, 0, 1, 1)
    return (minx, miny, maxx - minx + 1, maxy - miny + 1)


def _ink_height_of(text, px):
    return _ink_box_of(text, px, 0, 0, 0, 0)[3]


def _ink_top_of(text, x, y, px):
    """墨迹相对 drawText(y) 的偏移，用于把框精确摆到原字上。"""
    return y + _ink_box_of(text, px, 0, 0, 0, 0)[1]


def run_case(a, bg, label, boxes, out_name, pil_bg=None, refine=False):
    W, H = bg.width(), bg.height()
    print("\n" + "=" * 62)
    print("[%s] %d 块" % (label, len(boxes)))
    if not boxes:
        print("✗ 没有可渲染的块")
        return 1

    ov = app_mod.PixelOverlay()
    ov.resize(W, H)
    ov.move(0, 0)
    blocks = app_mod.boxes_to_blocks(boxes, 0, 0, W, H, img=pil_bg)
    if refine and pil_bg is not None:
        # 走生产同一条路径：boxes_to_blocks → refine_blocks_with_ink
        blocks = app_mod.refine_blocks_with_ink(blocks, pil_bg, 0, 0, W, H)
    ov.set_blocks(blocks)
    a.processEvents()

    out = QImage(W, H, QImage.Format_ARGB32)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.drawImage(0, 0, bg)
    ov.paint_with(p)
    p.end()

    out_path = os.path.join(OUT, out_name)
    out.save(out_path, "PNG")
    print("效果图(真实 Qt 渲染): %s" % out_path)

    print("%-14s %6s %8s %8s %8s %8s" %
          ("译文", "原高", "遮盖率", "垂直Δ", "字高比", "结论"))
    ok = True
    is_ink = make_en_ink(bg)
    for b in blocks:
        x, y, w, h = b.x, b.y, b.w, b.h
        # 窗口：只在**本块框内 + 少量容差**统计，不能扩太远，
        # 否则会把相邻行的英文算进来（早先 +60 导致遮盖率虚低到 9~28%）。
        bx0, by0 = max(0, x - 3), max(0, y - 4)
        bx1, by1 = min(W, x + w + 6), min(H, y + h + 4)
        bg_lum = block_bg_lum(bg, bx0, by0, bx1 - bx0, by1 - by0)

        # 深色底的英文是「白字」，遮罩也是深色 —— 用同样的 lum>150 判定，
        # 遮罩后的深色不会被误判成白字。但深色遮罩本身 (18,20,24) 会被判成
        # 「暗色墨迹」，所以深底块必须改用「亮墨迹」判定，且不做"暗色"统计。
        if bg_lum < 110:
            def is_ink_dark(c, _bl=bg_lum):
                lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
                # 必须三通道都接近（中性白/灰）。只看 R-G 会让绿色译文
                # (140,240,170) 混进"白色英文"里，遮盖率凭空掉一半。
                hi, lo = max(c.red(), c.green(), c.blue()), min(c.red(), c.green(), c.blue())
                if hi - lo >= 40:
                    return False
                return lum > 150
            ink_pred = is_ink_dark
        else:
            def is_ink_light(c, _bl=bg_lum):
                lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
                hi, lo = max(c.red(), c.green(), c.blue()), min(c.red(), c.green(), c.blue())
                if hi - lo >= 40:
                    return False
                return lum < 130
            ink_pred = is_ink_light

        en_before = ink_bbox(bg, bx0, by0, bx1, by1, lambda c: ink_pred(c, bg_lum))
        if en_before is None:
            print("%-14s  跳过（未定位到英文）" % b.text)
            continue
        en_x0, en_y0, en_x1, en_y1 = en_before
        en_h = en_y1 - en_y0 + 1

        en_before_n = count_ink(bg, bx0, by0, bx1 - bx0, by1 - by0, ink_pred, bg_lum)
        en_after_n = count_ink(out, bx0, by0, bx1 - bx0, by1 - by0, ink_pred, bg_lum)
        cover = 100.0 * (1.0 - en_after_n / max(1, en_before_n))

        zh_b = ink_bbox(out, bx0, by0, bx1, by1, zh_ink if bg_lum >= 110 else zh_ink_dark)
        if zh_b is None:
            print("%-14s  ✗ 没有译文墨迹（遮罩可能没画上）" % b.text)
            ok = False
            continue
        zh_h = zh_b[3] - zh_b[1] + 1
        dz = ((zh_b[1] + zh_b[3]) / 2.0) - ((en_y0 + en_y1) / 2.0)
        hratio = zh_h / float(en_h)

        verdict = []
        if cover < 85:
            verdict.append("遮盖不足")
        if abs(dz) > 4:
            verdict.append("垂直偏移")
        if not (0.55 <= hratio <= 1.40):
            verdict.append("字号失配")
        good = not verdict
        if not good:
            ok = False
        print("%-14s %6d %7.1f%% %+7.1fpx %7.2f  %s"
              % (b.text, en_h, cover, dz, hratio,
                 "✓" if good else "✗ " + "/".join(verdict)))

    print("小结: %s" % ("✓ 达标" if ok else "✗ 有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
