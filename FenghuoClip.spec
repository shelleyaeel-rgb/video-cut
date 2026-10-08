# -*- mode: python ; coding: utf-8 -*-
"""Windows x64, one-directory application; writable data stays outside this tree."""
from pathlib import Path
import json
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = Path(SPECPATH)
config = json.loads((root / 'scripts' / 'windows-dependencies.json').read_text(encoding='utf-8'))['ffmpeg']
ffmpeg = root / '.tools' / 'ffmpeg' / config['directory']
datas = [
    (str(root / 'web'), 'web'),
    (str(root / '.tools' / 'audio' / 'Kim_Vocal_2.onnx'), '.tools/audio'),
    (str(root / 'scripts' / 'apply-update.ps1'), 'scripts'),
    (str(root / 'build' / 'licenses'), 'licenses'),
]
for relative in config['files']:
    datas.append((str(ffmpeg / relative), str(Path('.tools/ffmpeg') / config['directory'] / Path(relative).parent)))
for package in ('numpy', 'opencv-python-headless', 'pywebview', 'pythonnet', 'clr-loader'):
    datas += copy_metadata(package)
webview_datas, webview_binaries, webview_imports = collect_all('webview', filter_submodules=lambda name: not any(value in name for value in ('android', 'cocoa', 'gtk', 'qt', '__pyinstaller')))
datas += webview_datas
# Dynamic application imports and worker entry points are used by the launcher.
hiddenimports = collect_submodules('videocut') + webview_imports + [
    'webview.platforms.winforms', 'webview.platforms.edgechromium',
    'clr', 'pythonnet', 'clr_loader', 'tkinter', 'tkinter.filedialog',
]
a = Analysis(
    [str(root / 'scripts' / 'desktop_entry.py')],
    pathex=[str(root)], binaries=webview_binaries, datas=datas,
    hiddenimports=hiddenimports, hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'pytest', 'IPython', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name='FenghuoClip',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, contents_directory='_internal',
    version=str(root / 'build' / 'windows-version.txt'),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='FenghuoClip')
