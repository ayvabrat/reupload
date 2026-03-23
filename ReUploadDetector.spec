# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: pyinstaller ReUploadDetector.spec
# Перед сборкой: cd web && npm run build

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# В exec(spec) нет __file__ — используем SPEC (путь к .spec от PyInstaller)
ROOT = Path(os.path.dirname(os.path.abspath(SPEC)))

added_datas = [
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "web" / "dist"), "web/dist"),
]

# uvicorn / starlette подгружают подмодули динамически
extra_hidden = (
    collect_submodules("uvicorn")
    + collect_submodules("starlette")
    + [
        "openpyxl.cell._writer",
        "web_dashboard",
        "core.database",
        "core.scheduler",
        "export.excel_export",
        "export.html_export",
    ]
)

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=added_datas,
    hiddenimports=extra_hidden,
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
    name="ReUploadDetector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
