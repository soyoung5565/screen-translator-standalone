"""
全局热键逻辑测试 —— 不开窗口、不发送真实键盘事件。

做法：注册 RegisterHotKey（证明占用/注册成功），并用自造的 MSG 结构
直接喂给 nativeEventFilter，验证 WM_HOTKEY 的解析与分发正确。

用法:
    .venv/Scripts/python.exe test_hotkeys.py
"""

import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtCore import QCoreApplication, QTimer
from PyQt5.QtWidgets import QApplication

import app as app_mod


def main():
    a = QApplication(sys.argv)
    f = app_mod.HotkeyFilter()

    got = []
    f.triggered.connect(lambda name: got.append(name))

    ok, failed = f.register_all()
    print("注册成功: %s" % ("、".join(ok) if ok else "无"))
    if failed:
        print("注册失败(被占用): %s" % "、".join(failed))

    # ── 自造 WM_HOTKEY 消息，逐个 id 验证分发 ──
    print("\n模拟 WM_HOTKEY 分发:")
    for hid, vk, name, desc in app_mod.HOTKEY_DEFS:
        msg = app_mod.MSG()
        msg.hwnd = None
        msg.message = app_mod.WM_HOTKEY
        msg.wParam = hid
        msg.lParam = 0
        addr = ctypes.addressof(msg)
        handled, result = f.nativeEventFilter(b"windows_generic_MSG", addr)
        print("  id=%d (%s) → 触发 %r, filter返回值=(%s, %s)"
              % (hid, desc, got[-1] if got else None, handled, result))

    # ── 验证非 WM_HOTKEY 消息不会误触发 ──
    before = len(got)
    msg2 = app_mod.MSG()
    msg2.message = 0x0100  # WM_KEYDOWN
    msg2.wParam = 1
    f.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg2))
    print("\n普通按键消息(WM_KEYDOWN) 误触发数: %d" % (len(got) - before))

    # ── 再注册一次应失败（证明第一次真的占用了）──
    dup = ctypes.windll.user32.RegisterHotKey(
        None, 99, app_mod.MOD_CONTROL | app_mod.MOD_ALT, ord("S"))
    print("重复注册 Ctrl+Alt+S 结果: %s (0=已被自己占用 ✓)" % dup)

    f.unregister_all()

    expected = [d[2] for d in app_mod.HOTKEY_DEFS]
    ok_all = (got == expected) and (len(got) == len(expected)) and dup == 0
    print("\n结论: %s" % ("✓ 热键注册与分发全部正确" if ok_all else "✗ 存在异常"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
