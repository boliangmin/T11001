# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import tkinter as tk


try:
    tcl_probe = tk.Tcl()
    tcl_library = Path(tcl_probe.eval("info library"))
    tk_library = tcl_library.parent / f"tk{tk.TkVersion}"
    if not (tk_library / "tk.tcl").is_file():
        raise FileNotFoundError(tk_library / "tk.tcl")
except (OSError, tk.TclError) as exc:
    raise RuntimeError(
        "PyInstaller requires a usable Tcl/Tk runtime; "
        "do not build trade_view.exe in a restricted sandbox."
    ) from exc


a = Analysis(
    ["stock_price_ui.py"],
    pathex=[],
    binaries=[],
    datas=[("trade_view.ico", ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="trade_view",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["trade_view.ico"],
)
