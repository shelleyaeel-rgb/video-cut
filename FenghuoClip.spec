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
    (str(root / 'native' / 'capture_host.py'), 'native'),
    (str(root / '.tools' / 'audio' / 'Kim_Vocal_2.onnx'), '.tools/audio'),
    (str(root / 'scripts' / 'apply-update.ps1'), 'scripts'),
    (str(root / 'build' / 'licenses'), 'licenses'),
]
capture_root = root / '.tools' / 'obs'
if not (capture_root / 'capture-runtime.json').is_file():
    raise RuntimeError('Run scripts/download_capture.py before building the recording edition.')
# Native capture has a private Python/DLL directory, independent of PyInstaller.
for capture_file in capture_root.rglob('*'):
    relative = capture_file.relative_to(capture_root)
    if capture_file.is_file() and relative.parts[0] != 'capture-config' and '__pycache__' not in relative.parts:
        datas.append((str(capture_file), str(Path('.tools/obs') / relative.parent)))
for relative in config['files']:
    datas.append((str(ffmpeg / relative), str(Path('.tools/ffmpeg') / config['directory'] / Path(relative).parent)))
for package in ('numpy', 'opencv-python', 'pywebview', 'pythonnet', 'clr-loader', 'rapidocr-onnxruntime', 'onnxruntime'):
    datas += copy_metadata(package)
webview_datas, webview_binaries, webview_imports = collect_all('webview', filter_submodules=lambda name: not any(value in name for value in ('android', 'cocoa', 'gtk', 'qt', '__pyinstaller')))
datas += webview_datas
ocr_datas, ocr_binaries, ocr_imports = collect_all('rapidocr_onnxruntime')
datas += ocr_datas
# Dynamic application imports and worker entry points are used by the launcher.
hiddenimports = collect_submodules('videocut') + webview_imports + ocr_imports + [
    'webview.platforms.winforms', 'webview.platforms.edgechromium',
    'clr', 'pythonnet', 'clr_loader', 'tkinter', 'tkinter.filedialog',
]
a = Analysis(
    [str(root / 'scripts' / 'desktop_entry.py')],
    pathex=[str(root)], binaries=webview_binaries + ocr_binaries, datas=datas,
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
