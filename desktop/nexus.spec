# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the NEXUS desktop client.

Build with ``scripts/build-desktop.ps1`` rather than calling PyInstaller directly: the script puts
the intermediate files and the output on a drive you choose (the build is large - Qt alone is
hundreds of megabytes) and runs the self-check afterwards.

Two decisions worth knowing:

* **Excluded Qt modules.** PySide6 ships WebEngine, Quick/QML, 3D, Charts, Multimedia and more.
  This application uses only QtCore/QtGui/QtWidgets, and bundling the rest multiplies the size for
  code that never loads. The exclusions are the heavy optional ones only; anything the application
  touches is left in place, and the self-check proves the result still starts.
* **onedir, not onefile.** A onefile build unpacks the whole of Qt into a temporary directory on
  every launch: slow to start and awkward to debug. onedir starts immediately and lets you see
  what was bundled.
"""

import os

# Console output is off for a normal release build. Set NEXUS_BUILD_CONSOLE=1 to build a console
# version, which is what you want when diagnosing a start-up failure.
CONSOLE = os.environ.get("NEXUS_BUILD_CONSOLE", "0") == "1"

EXCLUDES = [
    # Heavy Qt modules this application never imports.
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtTest",
    # Standard-library modules that would only add weight.
    "tkinter",
    "unittest",
    "pydoc",
    "doctest",
    "pdb",
]

a = Analysis(  # noqa: F821 - provided by PyInstaller
    ["run_desktop.py"],
    pathex=[os.path.abspath(SPECPATH)],  # noqa: F821
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NEXUS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="NEXUS",
)
