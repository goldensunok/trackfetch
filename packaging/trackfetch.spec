# PyInstaller build description for Trackfetch.
#
# This produces a single folder containing Trackfetch.exe and everything it
# needs, including its own copy of Python. Nobody installing Trackfetch has to
# install Python, pip, or anything else.
#
# Build with:  pyinstaller packaging/trackfetch.spec --noconfirm --clean

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

PROJECT_ROOT = Path(SPECPATH).parent
IS_WINDOWS = sys.platform.startswith("win")

datas = [
    (str(PROJECT_ROOT / "trackfetch" / "storage" / "schema.sql"), "trackfetch/storage"),
    (str(PROJECT_ROOT / "trackfetch" / "resources"), "trackfetch/resources"),
]

hiddenimports = [
    # Providers are imported by name at runtime through the registry, so
    # PyInstaller cannot see them by following imports.
    *collect_submodules("trackfetch.providers"),
    # keyring finds its backend at runtime in the same way.
    "keyring.backends.Windows",
    "keyring.backends.macOS",
    "keyring.backends.SecretService",
    "keyring.backends.chainer",
    "keyring.backends.fail",
]

excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "pytest",
    "respx",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.QtBluetooth",
    "PySide6.QtPositioning",
]

a = Analysis(
    [str(PROJECT_ROOT / "trackfetch" / "__main__.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Trackfetch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # console=False means no black terminal window appears behind the program.
    console=False,
    disable_windowed_traceback=False,
    icon=str(PROJECT_ROOT / "trackfetch" / "resources" / "trackfetch.ico"),
    version=str(PROJECT_ROOT / "packaging" / "version_info.txt") if IS_WINDOWS else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Trackfetch",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Trackfetch.app",
        icon=str(PROJECT_ROOT / "trackfetch" / "resources" / "trackfetch.ico"),
        bundle_identifier="org.trackfetch.app",
        info_plist={
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
        },
    )
