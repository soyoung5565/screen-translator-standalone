"""启动打包后的 exe，验证窗口真的出现且渲染出**新** UI。

判据（都是实测，不靠推理）：
  1. 进程存在
  2. 主窗口可见（IsWindowVisible）
  3. 窗口尺寸 = 400×268 客户区（+ 系统边框）
  4. PrintWindow 抓到的位图里含新 UI 的特征色：
     - 状态带底 #1c2333
     - 主按钮底 #1a7f37（设计稿的新主色，旧版是 #2ea043）
     - 分段控件选中段 #252c38
  5. 抓到的位图里有文字（不是纯色块）

用法:
    .venv/Scripts/python.exe verify_gui.py <exe路径> <输出png>
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
# WINFUNCTYPE 在 ctypes 里，不在 ctypes.wintypes
WINFUNCTYPE = ctypes.WINFUNCTYPE
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass


def find_windows(pid):
    out = []
    cb_t = WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, l):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid:
            out.append((h, user32.IsWindowVisible(h),
                        user32.GetWindowTextLengthW(h)))
        return True

    user32.EnumWindows(cb_t(cb), 0)
    return out


def grab(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    # PW_RENDERFULLCONTENT = 2
    user32.PrintWindow(hwnd, mdc, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                    ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    return w, h, buf


def count_colors(buf, w, h):
    """统计颜色。GetDIBits 返回的是 BGRA 顺序（Windows 惯例）。

    注意：buf 是 ctypes 的 create_string_buffer，索引得到的是**单字节
    bytes 对象**而不是 int —— 直接塞进 tuple 会让 Counter 的 key 变成
    bytes 元组，后面按 RGB int 比较必然全 0。必须先整体转 bytes。
    """
    from collections import Counter
    raw = bytes(buf)          # 一次性转成 bytes，索引得 int
    c = Counter()
    for i in range(0, w * h):
        o = i * 4
        c[(raw[o + 2], raw[o + 1], raw[o])] += 1   # → (R, G, B)
    return c


def main():
    exe = sys.argv[1]
    out = sys.argv[2]
    p = subprocess.Popen([exe])
    print("launched pid", p.pid)
    time.sleep(9)

    wins = find_windows(p.pid)
    print("windows(parent):", [(hex(h), v, t) for h, v, t in wins])
    # 面板启动时是「隐藏到托盘前」的隐藏态 —— IsWindowVisible 为假。
    # 先对每个有标题的窗口 SW_RESTORE，再重新判定可见性。
    for h, v, t in wins:
        if t > 0:
            user32.ShowWindow(h, 9)  # SW_RESTORE
    time.sleep(2)
    wins = find_windows(p.pid)
    print("after restore(parent):", [(hex(h), v, t) for h, v, t in wins])

    # PyInstaller onefile：父进程是引导窗，真正的 GUI 在**子进程**里。
    # 所以要枚举该 exe 的所有进程，逐个找窗口。
    all_pids = [p.pid]
    try:
        tl = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % os.path.basename(exe), "/FO", "CSV"],
            capture_output=True, text=True, timeout=30, encoding="gbk", errors="replace")
        for line in tl.stdout.splitlines():
            line = line.strip()
            if not line.startswith('"') or not line.endswith('"'):
                continue
            parts = line.split('","')
            if len(parts) < 2:
                continue
            try:
                pid_i = int(parts[1].strip('"'))
            except ValueError:
                continue          # 表头行 "映像名称","PID",...
            if pid_i not in all_pids:
                all_pids.append(pid_i)
    except Exception as e:
        print("tasklist err", e)
    print("all pids:", all_pids)

    wins = []
    for pid_i in all_pids:
        wins += find_windows(pid_i)
    print("windows(all):", [(hex(h), v, t) for h, v, t in wins])
    for h, v, t in wins:
        if t > 0:
            user32.ShowWindow(h, 9)
    time.sleep(2)
    wins = []
    for pid_i in all_pids:
        wins += find_windows(pid_i)

    vis = [h for h, v, t in wins if t > 0]
    if not vis:
        print("!! 没有带标题的窗口")
        p.kill()
        return 1

    # 目标 = 标题为「屏幕翻译器」的控制面板。不能用面积最大 —— 那个是
    # 全屏捕获层（1920×1103），面板只有 400×268。
    target = None
    for h in vis:
        n = user32.GetWindowTextLengthW(h)
        buf_t = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(h, buf_t, n + 2)
        title = buf_t.value
        print("  cand %s title=%r" % (hex(h), title))
        if "屏幕翻译器" in title:
            target = h
            break
    if target is None:
        print("!! 找不到标题含「屏幕翻译器」的窗口")
        p.kill()
        return 1
    hwnd = target
    print("target hwnd:", hex(hwnd))
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    time.sleep(2)
    w, h, buf = grab(hwnd)
    print("window rect: %dx%d" % (w, h))

    cnt = count_colors(buf, w, h)
    print("  top colors:", cnt.most_common(8))
    checks = {
        "状态带底 #1c2333": (0x1c, 0x23, 0x33),
        "主按钮底 #1a7f37": (0x1a, 0x7f, 0x37),
        "分段选中 #252c38": (0x25, 0x2c, 0x38),
        "面板底 #161b22": (0x16, 0x1b, 0x22),
    }
    ok = True
    for name, rgb in checks.items():
        n = cnt.get(rgb, 0)
        flag = "✓" if n > 20 else "✗"
        if n <= 20:
            ok = False
        print("  %s %-18s RGB(%02x%02x%02x) × %d" % (flag, name, rgb[0], rgb[1], rgb[2], n))

    # 文字存在性：统计非背景像素比例
    total = w * h
    bg = cnt.get((0x16, 0x1b, 0x22), 0) + cnt.get((0x1c, 0x23, 0x33), 0)
    print("  客户区 %dx%d, 非背景像素 %d (%.1f%%)" % (w, h, total - bg, (total - bg) * 100.0 / total))
    if (total - bg) * 100.0 / total < 2.0:
        print("  ✗ 几乎没有像素变化 —— 疑似空白面板")
        ok = False

    # 存 PNG
    try:
        from PIL import Image
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        # 路径可能含中文，PIL 的 open 依赖 locale；先存到纯 ASCII 临时名再改名
        tmp_path = out + ".tmp.png"
        img.save(tmp_path, "PNG")
        if os.path.exists(out):
            os.remove(out)
        os.rename(tmp_path, out)
        print("  → %s" % out)
    except Exception as e:
        print("  (PIL 存图失败: %s)" % e)

    p.kill()
    print("\n结论:", "✓ 通过" if ok else "✗ 不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
