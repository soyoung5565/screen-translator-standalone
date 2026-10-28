"""
墨迹校正单测 —— 验证 refine_blocks_with_ink 能把模型给的烂框拉回真实文字框。

为什么要这个文件：
  上面的 test_render_fidelity.py 只能测「模型当次返回的坐标」好不好，
  而模型同一张图每次返回 0~5 块不等、w 偏窄、h 可能给 3px。那份结果无法回归。
  墨迹校正的价值在于：**故意喂错框**，看它能不能靠像素把框找回来。
  这样不依赖模型，测试是确定性的。

用法:
    .venv/Scripts/python.exe test_ink_refine.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw, ImageFont

import app as app_mod


W, H = 720, 420
BG_LIGHT = (245, 246, 248)
BG_DARK = (32, 36, 48)
FG_LIGHT = (20, 20, 26)      # 浅底上的深字
FG_DARK = (240, 240, 245)    # 深底上的白字

ROWS = [
    # (文本, x, y, 像素字号, 背景, 前景, 真值扫描的 y 范围)
    # y 范围必须避开色块边界 —— 否则标题栏下沿（深/浅交界）会被算成墨迹，
    # 真值框直接膨胀到整个窗口（实测把 410x50 当成了一行 26px 的字）。
    ("Control Panel",       30,  18, 26, BG_DARK,  FG_DARK,  (12, 64)),
    ("Display brightness",  30, 110, 22, BG_LIGHT, FG_LIGHT, (104, 150)),
    ("Network connection",  30, 180, 22, BG_LIGHT, FG_LIGHT, (174, 220)),
    ("Sign out",            30, 250, 22, BG_LIGHT, FG_LIGHT, (244, 290)),
]


def load_font(size):
    """找一个可用的 TrueType 字体；找不到就退回默认位图字体（仍能测几何）。"""
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        for base in (r"C:\Windows\Fonts", "/usr/share/fonts/truetype/dejavu"):
            p = os.path.join(base, name)
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    pass
    return ImageFont.load_default()


def build_image():
    img = Image.new("RGB", (W, H), BG_LIGHT)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 70], fill=BG_DARK)
    for text, x, y, px, bg, fg, (sy0, sy1) in ROWS:
        d.text((x, y), text, font=load_font(px), fill=fg)
    return img


def true_boxes(img):
    """
    用像素统计量出每行的真实墨迹框（这就是"标准答案"）。
    与 refine_blocks_with_ink 内部算法同源，但独立走一遍全图扫描，
    不共享代码路径 —— 否则测的是"自己等于自己"。
    """
    gray = img.convert("L")
    px = gray.load()
    out = []
    for text, x, y, _px, bg, _fg, (sy0, sy1) in ROWS:
        bg_l = int(0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2])
        y0, y1 = max(0, sy0), min(H, sy1)
        xs, ys = [], []
        for yy in range(y0, y1):
            for xx in range(max(0, x - 10), min(W, x + 400)):
                if abs(px[xx, yy] - bg_l) >= 60:
                    xs.append(xx)
                    ys.append(yy)
        if not xs:
            out.append(None)
            continue
        out.append((min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1))
    return out


def make_bad_blocks(truth):
    """
    构造「模型风格的坏框」—— 全部基于真实框做已知劣化：
      1. h 压到 40%（模型常给偏小）
      2. w 砍到 55%（遮罩盖不满）
      3. y 上移 4px（垂直漂移）
      4. x 右移 3px
    """
    bad = []
    for i, t in enumerate(truth):
        if t is None:
            continue
        tx, ty, tw, th = t
        bad.append(app_mod.TextBlock(
            ROWS[i][0],
            tx + 3, ty - 4,
            int(tw * 0.55), max(3, int(th * 0.40)),
            None))
    return bad


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    union = aw * ah + bw * bh - inter
    return inter / float(union)


def main():
    img = build_image()
    outdir = "D:/hermes-data/cache/scratch/ui"
    os.makedirs(outdir, exist_ok=True)
    img.save(os.path.join(outdir, "ink_src.png"))

    truth = true_boxes(img)
    print("真值墨迹框（像素实测）:")
    for i, t in enumerate(truth):
        print("  %-20s %s" % (ROWS[i][0], t))

    bad = make_bad_blocks(truth)
    print("\n模型风格坏框（h×0.4, w×0.55, y-4, x+3）:")
    for b in bad:
        print("  %-20s (%d,%d) %dx%d" % (b.text, b.x, b.y, b.w, b.h))

    fixed = app_mod.refine_blocks_with_ink(bad, img, 0, 0, W, H)
    print("\n校正后:")
    for b in fixed:
        print("  %-20s (%d,%d) %dx%d bg=%s" % (b.text, b.x, b.y, b.w, b.h, b.bg_lum))

    print("\n" + "=" * 70)
    print("%-20s %8s %8s %8s" % ("行", "校正前IoU", "校正后IoU", "结论"))
    ok = True
    for i, t in enumerate(truth):
        if t is None:
            print("  ✗ 真值没测到 %s" % ROWS[i][0])
            ok = False
            continue
        before = None
        for b in bad:
            if b.text == ROWS[i][0]:
                before = (b.x, b.y, b.w, b.h)
        after = None
        for b in fixed:
            if b.text == ROWS[i][0]:
                after = (b.x, b.y, b.w, b.h)
        if before is None or after is None:
            print("  ✗ 缺块 %s" % ROWS[i][0])
            ok = False
            continue
        i0, i1 = iou(before, t), iou(after, t)
        good = i1 >= 0.85 and i1 > i0
        if not good:
            ok = False
        print("%-20s %8.3f %8.3f  %s" % (ROWS[i][0], i0, i1, "✓" if good else "✗"))

    # 边界情况：纯色图（无墨迹）必须原样返回，不能崩
    blank = Image.new("RGB", (300, 200), (128, 128, 128))
    b0 = [app_mod.TextBlock("none", 20, 20, 100, 20, None)]
    r0 = app_mod.refine_blocks_with_ink(b0, blank, 0, 0, 300, 200)
    same = len(r0) == 1 and r0[0].x == 20 and r0[0].w == 100
    print("\n纯色图兜底（应保持原框）: %s" % ("✓" if same else "✗"))
    if not same:
        ok = False

    # 边界情况：img=None 必须原样返回
    r1 = app_mod.refine_blocks_with_ink(b0, None, 0, 0, 300, 200)
    print("img=None 兜底: %s" % ("✓" if r1 is b0 else "✗"))
    if r1 is not b0:
        ok = False

    print("\n结论: %s" % ("✓ 墨迹校正有效" if ok else "✗ 需要修正"))

    # ── 密集行距对抗测试 ──
    # 搜索窗向下延伸 2.5×unit，行距只有 1.2×字高时必须仍吸附到**自己那一行**，
    # 否则密集列表（设置项/表格）会串行贴错位置。
    print("\n" + "=" * 70)
    print("密集行距（行距 26px，字高 22px）:")
    dimg = Image.new("RGB", (520, 300), BG_LIGHT)
    dd = ImageDraw.Draw(dimg)
    dtexts = ["Setting alpha", "Setting bravo", "Setting charlie", "Setting delta"]
    dfont = load_font(22)
    for i, tt in enumerate(dtexts):
        dd.text((40, 20 + i * 26), tt, font=dfont, fill=FG_LIGHT)

    # 真值：逐行**单独渲染**再量墨迹 —— 密集行距下用固定 ±12px 窗口扫描
    # 会把上一行的下伸部算进来（实测真值 45..68，实际行 51..68），
    # 那种"标准答案"本身就是错的，不能拿来判校正是否串行。
    dtruth = []
    dfont = load_font(22)
    for i, tt in enumerate(dtexts):
        solo = Image.new("RGB", (520, 80), BG_LIGHT)
        ImageDraw.Draw(solo).text((40, 20), tt, font=dfont, fill=FG_LIGHT)
        sgray = solo.convert("L")
        spx = sgray.load()
        xs, ys = [], []
        for yy in range(80):
            for xx in range(520):
                if abs(spx[xx, yy] - 246) >= 60:
                    xs.append(xx); ys.append(yy)
        # 挪到第 i 行的位置
        dtruth.append((min(xs), min(ys) + i * 26,
                       max(xs) - min(xs) + 1, max(ys) - min(ys) + 1))

    # 模型风格坏框：h 压到 8px，y 上移 3px
    dbad = [app_mod.TextBlock(dtexts[i], dtruth[i][0] + 2, dtruth[i][1] - 3,
                              int(dtruth[i][2] * 0.6), 8, None)
            for i in range(len(dtexts))]
    dfixed = app_mod.refine_blocks_with_ink(dbad, dimg, 0, 0, 520, 300)

    dok = True
    for i, t in enumerate(dtruth):
        after = None
        for b in dfixed:
            if b.text == dtexts[i]:
                after = (b.x, b.y, b.w, b.h)
        i1 = iou(after, t) if after else 0.0
        # 密集行距下允许 y 轻微偏差（±3px），IoU 门槛放宽到 0.75
        good = i1 >= 0.75
        if not good:
            dok = False
        print("  %-18s 真值%s 校正后%s IoU=%.3f %s"
              % (dtexts[i], t, after, i1, "✓" if good else "✗ 串行/漏校"))
    if not dok:
        ok = False
        print("  ✗ 密集行距下发生串行或漏校")

    print("\n最终结论: %s" % ("✓ 全部通过" if ok else "✗ 需要修正"))

    # ── 同行双块对抗测试 ──
    # 同一行左侧标签 + 右侧数值。模型常把左侧块的 w 量到右侧去，
    # 若邻居夹边界失效，左侧遮罩会盖住右侧数值 —— 用户会以为翻译错了。
    print("\n" + "=" * 70)
    print("同行双块（左侧标签 + 右侧数值）:")
    pimg = Image.new("RGB", (600, 120), BG_LIGHT)
    pd = ImageDraw.Draw(pimg)
    pfont = load_font(22)
    pd.text((40, 40), "Volume", font=pfont, fill=FG_LIGHT)
    pd.text((420, 40), "72%", font=pfont, fill=FG_LIGHT)
    # 右侧数值另画成蓝色（模拟按钮/数值），确认遮罩不越界
    pd.text((500, 40), "OK", font=pfont, fill=(20, 90, 200))

    pgray = pimg.convert("L")
    ppx = pgray.load()

    def pbox(x0, x1):
        xs, ys = [], []
        for yy in range(30, 110):
            for xx in range(x0, x1):
                if abs(ppx[xx, yy] - 246) >= 60:
                    xs.append(xx); ys.append(yy)
        return (min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)

    plabel_t = pbox(30, 300)
    pvalue_t = pbox(400, 600)

    # 左侧标签的 w 被模型量到了 640（越过右侧全部内容），h 压到 9
    pbad = [
        app_mod.TextBlock("音量", plabel_t[0] + 1, plabel_t[1] - 2,
                          pvalue_t[0] + pvalue_t[2] - plabel_t[0], 9, None),
        app_mod.TextBlock("72% OK", pvalue_t[0] + 1, pvalue_t[1] - 2,
                          int(pvalue_t[2] * 0.6), 9, None),
    ]
    pfixed = app_mod.refine_blocks_with_ink(pbad, pimg, 0, 0, 600, 120)

    pok = True
    for txt, t in (("音量", plabel_t), ("72% OK", pvalue_t)):
        after = None
        for b in pfixed:
            if b.text == txt:
                after = (b.x, b.y, b.w, b.h)
        if after is None:
            print("  ✗ 缺块 %s" % txt)
            pok = False
            continue
        i1 = iou(after, t)
        # 关键判据：宽度不能越界吃进邻居
        overflow = after[0] + after[2] > t[0] + t[2] + 6
        good = i1 >= 0.70 and not overflow
        if not good:
            pok = False
        print("  %-10s 真值%s 校正后%s IoU=%.3f 越界=%s %s"
              % (txt, t, after, i1, overflow, "✓" if good else "✗"))
    if not pok:
        ok = False
        print("  ✗ 同行邻居夹边界失效")

    print("\n最终结论: %s" % ("✓ 全部通过" if ok else "✗ 需要修正"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
