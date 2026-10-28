"""生成 EXE 图标：绿底「译」字。"""
from PIL import Image, ImageDraw, ImageFont
import os

SIZES = [16, 24, 32, 48, 64, 128, 256]

def make(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 圆角方块，GitHub 深色底
    r = max(2, size // 5)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=(13, 17, 23, 255))
    # 细边框，中绿（在深色壁纸上仍有轮廓）
    d.rounded_rectangle([1, 1, size - 2, size - 2], radius=r,
                        outline=(46, 160, 67, 255), width=max(1, size // 32))
    # 字体：字号 0.58，留白约 20%
    fs = int(size * 0.58)
    font = None
    for path in [
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ]:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, fs)
                break
            except Exception:
                continue
    text = "译"
    if font:
        bb = d.textbbox((0, 0), text, font=font)
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        # 亮绿 #3fb950，与暗绿边框形成层次
        d.text(((size - w) / 2 - bb[0], (size - h) / 2 - bb[1]), text,
               font=font, fill=(63, 185, 80, 255))
    return img

imgs = [make(s) for s in SIZES]
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")
imgs[-1].save(out, format="ICO", sizes=[(s, s) for s in SIZES])
print("saved", out, os.path.getsize(out), "bytes")
