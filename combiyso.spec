# -*- mode: python ; coding: utf-8 -*-
"""컴비서 PyInstaller 빌드 스펙 (onedir — 시작 빠르고 메모리 절약)."""
import os
import re
import sys

from PyInstaller.utils.hooks import collect_submodules

# 윈도우는 .ico, macOS 는 .icns 를 쓴다(없으면 아이콘 생략).
_ICON_WIN = os.path.join(SPECPATH, "assets", "combiyso.ico")
_ICON_MAC = os.path.join(SPECPATH, "assets", "combiyso.icns")
if sys.platform.startswith("win"):
    ICON = _ICON_WIN if os.path.exists(_ICON_WIN) else None
elif sys.platform == "darwin":
    ICON = _ICON_MAC if os.path.exists(_ICON_MAC) else None
else:
    ICON = None

_VER = "0.0.0"
try:
    _src = open(os.path.join(SPECPATH, "app", "config.py"), encoding="utf-8").read()
    _VER = re.search(r'VERSION\s*=\s*"([^"]+)"', _src).group(1)
except Exception:
    pass

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

# macOS: 더블클릭으로 실행되는 진짜 .app 번들(아이콘·Dock·응용 프로그램 목록)
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="컴비서.app",
        icon=ICON,
        bundle_identifier="com.kkang2da.combiyso",
        info_plist={
            "CFBundleName": "컴비서",
            "CFBundleDisplayName": "컴비서",
            "CFBundleGetInfoString": f"컴비서 {_VER}",
            "CFBundleShortVersionString": _VER,
            "CFBundleVersion": _VER,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.productivity",
        },
    )
