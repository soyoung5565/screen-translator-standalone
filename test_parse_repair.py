"""
解析器回归测试 —— 用**实测抓到的真实坏输出**验证容错。

为什么要这个文件：
  「模型漏行」曾被误判为模型能力上限（同一张图返回 0~5 块）。
  抓原始响应才看清：模型每次都返回了全部 5 行，只是 JSON 写坏了 ——
    "x":39","y":52   ← 数字后面多一个引号
  整段不是合法 JSON，标准解析失败，逐对象正则兜底又很脆，于是只剩 0~3 块。
  这是解析器缺陷，不是模型缺陷。任何修复都必须用这段真实样本做回归。

用法:
    .venv/Scripts/python.exe test_parse_repair.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine


# ── 实测抓到的原始响应（Minimax-M3，2026-10-28，760x440 英文界面）──
REAL_BROKEN = (
    '[{"t":"Control Panel","x":39","y":52,"w":198,"h":34,"zh":"控制面板"},'
    '{"t":"Display brightness","x":39","y":289,"w":240,"h":32,"zh":"显示亮度"},'
    '{"t":"Network connection","x":39","y":466,"w":249,"h":32,"zh":"网络连接"},'
    '{"t":"Automatic backup","x":39","y":647,"w":232,"h":32,"zh":"自动备份"},'
    '{"t":"Sign out of account","x":39","y":827,"w":246,"h":32,"zh":"退出账户"}]'
)

CASES = [
    ("实测坏输出（数字后多引号）", REAL_BROKEN, 5),
    ("合法 JSON", '[{"t":"A","x":10,"y":20,"w":30,"h":8,"zh":"甲"}]', 1),
    ("markdown 包裹",
     '```json\n[{"t":"A","x":10,"y":20,"w":30,"h":8,"zh":"甲"}]\n```', 1),
    ("前后有废话",
     '好的，结果如下：\n[{"t":"A","x":10,"y":20,"w":30,"h":8,"zh":"甲"}]\n以上。', 1),
    # 重复键：模型把 h 写成第二个 w —— 应保留第一个 w 作宽度、第二个作高度
    ("重复键 w/w（h 误写成 w）",
     '[{"t":"A","x":10,"y":20,"w":30,"w":8,"zh":"甲"}]', 1),
    # 数值写成字符串：下游 float() 要能转
    ("数值是字符串",
     '[{"t":"A","x":"10","y":"20","w":"30","h":"8","zh":"甲"}]', 1),
    ("空数组", "[]", 0),
    ("模型回了一句废话", "图片里没有可翻译的文字。", 0),
    ("真正的空图", "", 0),
]


def check(name, raw, expect_n, verbose=False):
    got = engine.parse_json_boxes(raw)
    ok = len(got) == expect_n
    # 除了条数，还要校验字段没被修补搞坏
    if ok and expect_n > 0:
        for b in got:
            for k in ("x", "y", "w", "h"):
                if not isinstance(b[k], float):
                    ok = False
            if not b["zh"]:
                ok = False
    print("%-28s 期望 %d 块 → 实得 %d 块  %s" % (name, expect_n, len(got), "✓" if ok else "✗"))
    if verbose or not ok:
        for b in got:
            print("     ", b)
    return ok


def main():
    print("解析器容错回归（用例来自实测）:")
    print("=" * 66)
    allok = True
    for name, raw, n in CASES:
        if not check(name, raw, n, verbose=(n == 5)):
            allok = False

    print("\n" + "=" * 66)
    print("关键断言：实测坏输出必须解析出**全部 5 行**，且坐标正确")
    got = engine.parse_json_boxes(REAL_BROKEN)
    exp = [
        ("Control Panel", 39, 52, 198, 34, "控制面板"),
        ("Display brightness", 39, 289, 240, 32, "显示亮度"),
        ("Network connection", 39, 466, 249, 32, "网络连接"),
        ("Automatic backup", 39, 647, 232, 32, "自动备份"),
        ("Sign out of account", 39, 827, 246, 32, "退出账户"),
    ]
    key_ok = True
    if len(got) != len(exp):
        print("✗ 条数不对：%d ≠ %d" % (len(got), len(exp)))
        key_ok = False
    else:
        for b, (t, x, y, w, h, zh) in zip(got, exp):
            match = (b["t"] == t and b["x"] == x and b["y"] == y
                     and b["w"] == w and b["h"] == h and b["zh"] == zh)
            if not match:
                print("✗ %r 字段不符：%s" % (t, b))
                key_ok = False
        if key_ok:
            print("✓ 5 行全部还原，t/x/y/w/h/zh 逐字段一致")
    if not key_ok:
        allok = False

    print("\n结论: %s" % ("✓ 解析器容错达标" if allok else "✗ 需要修正"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
