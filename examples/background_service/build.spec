# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project = Path(SPECPATH).resolve()
a = Analysis(
    [str(project / "src/heartbeat_plugin/__main__.py")],
    pathex=[str(project / "src")],
    binaries=[],
    datas=[],
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
    name="HeartbeatPlugin",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
