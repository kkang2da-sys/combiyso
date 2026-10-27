# -*- mode: python ; coding: utf-8 -*-
"""컴비서 PyInstaller 빌드 스펙 (onedir — 시작 빠르고 메모리 절약)."""
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

# 윈도우는 .ico 를 쓰고, macOS/Linux 는 아이콘을 생략한다(.ico 는 macOS 에서 거부됨).
_ICON_WIN = os.path.join(SPECPATH, "assets", "combiyso.ico")
ICON = _ICON_WIN if sys.platform.startswith("win") and os.path.exists(_ICON_WIN) else None

try:
    _pypdf = collect_submodules("pypdf")      # 선택 의존성(PDF 텍스트 추출)
except Exception:
    _pypdf = []

hiddenimports = (
    collect_submodules("uvicorn")
    + _pypdf
    + [
        "app", "app.server", "app.agent", "app.llm", "app.tools",
        "app.config", "app.sessions", "app.websearch", "app.backup",
        "app.mcp", "app.context", "app.approval",
        "app.skills", "app.commands", "app.agents", "app.permissions",
        "app.runs", "app.net", "app.vcs", "app.pdftext", "app.attach",
        "app.perf", "app.sysres", "app.diag", "app.discover",
        "pypdf",
        "anyio", "h11", "click", "sniffio", "idna", "certifi",
        "charset_normalizer", "urllib3",
    ]
)

a = Analysis(
    ["run.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=[
        ("app/ui/index.html", "ui"),
        ("assets/combiyso.ico", "assets"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "PIL", "numpy", "pandas", "matplotlib", "scipy",
        "PySide6", "PyQt5", "PyQt6", "IPython", "notebook", "pytest",
        "setuptools", "pip", "wheel",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="combiyso",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON if (ICON and os.path.exists(ICON)) else None,
)

# onedir: 실행 시 임시폴더로 압축을 풀지 않아 시작이 빠르고 메모리도 덜 쓴다.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="combiyso",
)
