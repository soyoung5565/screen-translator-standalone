# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件窗口程序，无控制台。"""

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    # datas=[] 会让 app.ico 不进包 —— 托盘图标走 resource_path("app.ico")，
    # 打包后 _MEIPASS 里找不到文件，托盘就退化成纯色方块「译」。
    # PyInstaller 只在 EXE 的 icon= 里嵌资源图，不会解包成文件。
    datas=[('app.ico', '.')],
    hiddenimports=[
        'PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets',
        'mss', 'PIL', 'requests',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 砍掉无用大包，减小体积
        'tkinter', 'matplotlib', 'numpy', 'scipy', 'pandas',
        'IPython', 'jupyter', 'notebook', 'pytest',
        'PyQt5.QtWebEngine', 'PyQt5.QtWebEngineCore', 'PyQt5.QtWebEngineWidgets',
        'PyQt5.QtMultimedia', 'PyQt5.QtBluetooth', 'PyQt5.QtNfc',
        'PyQt5.QtPositioning', 'PyQt5.QtQml', 'PyQt5.QtQuick',
        'PyQt5.QtSensors', 'PyQt5.QtSerialPort', 'PyQt5.QtSql',
        'PyQt5.QtTest', 'PyQt5.QtXml', 'PyQt5.QtDesigner',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='屏幕翻译器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 无控制台黑窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app.ico',
)
