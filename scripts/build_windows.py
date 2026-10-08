"""Build a verified Windows portable bundle and its updater manifest.

Run with 64-bit Python 3.12 after installing requirements-build.txt.
No user data, working videos, or exports are added to the application archive.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = 'FenghuoClip'
RUNTIME_PACKAGES = (
    'numpy', 'opencv-python-headless', 'pywebview', 'pythonnet', 'clr-loader',
    'cffi', 'pycparser', 'proxy-tools', 'bottle', 'typing-extensions',
)


def read_version(root=ROOT):
    tree = ast.parse((root / 'videocut' / '__init__.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
                return value
    raise ValueError('videocut.__version__ must be a stable numeric major.minor.patch version.')


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def ffmpeg_configuration():
    return json.loads((ROOT / 'scripts' / 'windows-dependencies.json').read_text(encoding='utf-8'))['ffmpeg']


def extract_verified_ffmpeg(archive, destination, configuration):
    """Only extract explicitly pinned files, preventing traversal and extra payloads."""
    with zipfile.ZipFile(archive) as bundle:
        for relative, expected in configuration['files'].items():
            member = f"{configuration['directory']}/{relative}"
            if PurePosixPath(member).is_absolute() or '..' in PurePosixPath(member).parts or '\\' in member:
                raise ValueError('Unsafe FFmpeg archive path')
            payload = bundle.read(member)
            if hashlib.sha256(payload).hexdigest() != expected:
                raise ValueError(f'FFmpeg file checksum mismatch: {relative}')
            target = destination / member
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)


def ensure_resources():
    sys.path.insert(0, str(ROOT))
    # Reuse the checksum-verifying downloader; no machine-wide tools are installed.
    from scripts.download_tools import download
    from videocut.audio_model import MODEL_BYTES, MODEL_NAME, MODEL_SHA256, MODEL_URL
    config = ffmpeg_configuration()
    ffmpeg_root = ROOT / '.tools' / 'ffmpeg' / config['directory']
    downloads = ROOT / 'work' / 'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    if not all((ffmpeg_root / name).is_file() and sha256(ffmpeg_root / name) == digest for name, digest in config['files'].items()):
        archive = downloads / f"ffmpeg-{config['version']}-verified.zip"
        # A cached official archive can be reused, but is never trusted by name alone.
        existing = downloads / 'ffmpeg-release-essentials.zip'
        if existing.is_file() and sha256(existing) == config['sha256']:
            archive = existing
        else:
            download(config['url'], archive, config['sha256'])
        extract_verified_ffmpeg(archive, ROOT / '.tools' / 'ffmpeg', config)
    for name, digest in config['files'].items():
        if sha256(ffmpeg_root / name) != digest:
            raise ValueError(f'FFmpeg checksum mismatch: {name}')
    model = ROOT / '.tools' / 'audio' / MODEL_NAME
    model.parent.mkdir(parents=True, exist_ok=True)
    download(MODEL_URL, model, MODEL_SHA256)
    if model.stat().st_size != MODEL_BYTES:
        raise ValueError('Unexpected voice model size')
    return ffmpeg_root, model


def collect_license_files(destination):
    """Keep upstream license files and package identity next to shipped components."""
    destination.mkdir(parents=True, exist_ok=True)
    inventory = []
    # cffi's optional compilation path pulls in setuptools/packaging; preserve
    # their vendored notices even though the shipped app never compiles code.
    for name in (*RUNTIME_PACKAGES, 'setuptools', 'packaging', 'pyinstaller'):
        distribution = importlib.metadata.distribution(name)
        folder = destination / name
        folder.mkdir(exist_ok=True)
        copied = []
        for item in distribution.files or ():
            # Include METADATA for provenance even when a wheel omits its license file.
            if item.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE')) or item.name == 'METADATA':
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    relative = Path(*[part for part in item.parts if part not in ('.', '..')])
                    target = folder / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied.append(str(target.relative_to(destination)).replace('\\', '/'))
        inventory.append({'package': name, 'version': distribution.version, 'license_files': copied})
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if not python_license.is_file():
        raise FileNotFoundError(f'CPython license not found: {python_license}')
    shutil.copy2(python_license, destination / 'Python-LICENSE.txt')
    inventory.append({'package': 'CPython', 'version': platform.python_version(), 'license_files': ['Python-LICENSE.txt']})
    (destination / 'runtime-packages.json').write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    shutil.copy2(ROOT / 'scripts' / 'THIRD-PARTY-NOTICES.txt', destination / 'THIRD-PARTY-NOTICES.txt')
    shutil.copy2(ROOT / 'scripts' / 'windows-dependencies.json', destination / 'windows-dependencies.json')
    shutil.copytree(ROOT / 'scripts' / 'upstream-licenses', destination / 'upstream-licenses', dirs_exist_ok=True)


def validate_bundle_layout(bundle):
    bundle = Path(bundle)
    if not (bundle / f'{APP_NAME}.exe').is_file() or not (bundle / '_internal').is_dir():
        raise ValueError('Portable bundle must contain FenghuoClip.exe and _internal/.')
    allowed = {f'{APP_NAME}.exe', '_internal'}
    for path in bundle.rglob('*'):
        if path.is_symlink():
            raise ValueError(f'Symlink not permitted in portable bundle: {path.name}')
        relative = path.relative_to(bundle)
        if relative.parts[0] not in allowed:
            raise ValueError(f'Unexpected portable bundle file: {relative}')
        if any(part in ('data', 'work', 'outputs', '.venv', '.git') for part in relative.parts[:2]):
            raise ValueError(f'User/development data cannot be shipped: {relative}')


def write_release_archive(bundle, output, version):
    validate_bundle_layout(bundle)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'{APP_NAME}-{version}-windows-x64.zip'
    temporary = archive.with_suffix('.zip.part')
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for path in sorted(Path(bundle).rglob('*')):
            if path.is_file():
                zipped.write(path, f'{APP_NAME}/{path.relative_to(bundle).as_posix()}')
    temporary.replace(archive)
    manifest = {'version': version, 'asset': archive.name, 'sha256': sha256(archive), 'size': archive.stat().st_size}
    manifest_path = output / 'update-windows.json'
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return archive, manifest_path


def frozen_smoke_test(bundle, result):
    result = Path(result).resolve()
    with tempfile.TemporaryDirectory(prefix='fenghuo-build-check-') as temporary:
        environment = dict(os.environ, VIDEOCUT_DATA_DIR=temporary)
        fresh_report = Path(temporary) / 'self-test.json'
        completed = subprocess.run([str((bundle / f'{APP_NAME}.exe').resolve()), '--self-test', '--result', str(fresh_report)], env=environment, timeout=180)
        if fresh_report.is_file():
            shutil.copy2(fresh_report, result)
        if completed.returncode:
            raise RuntimeError(f'Frozen application self-test failed ({completed.returncode}); inspect {result}')
        if not fresh_report.is_file():
            raise RuntimeError('Frozen application did not produce a self-test report')
        report = json.loads(fresh_report.read_text(encoding='utf-8'))
        if not report.get('ok'):
            raise RuntimeError(f'Frozen application self-test did not pass: {report}')
    print(f'Frozen self-test passed: {result}', flush=True)


def write_windows_version_info(version, destination):
    numbers = ', '.join((*version.split('.'), '0'))
    content = f'''VSVersionInfo(
    ffi=FixedFileInfo(filevers=({numbers}), prodvers=({numbers}), mask=0x3f,
                     flags=0, OS=0x40004, fileType=0x1, subtype=0, date=(0, 0)),
    kids=[StringFileInfo([StringTable('080404B0', [
        StringStruct('FileDescription', '烽火剪辑'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'FenghuoClip'),
        StringStruct('OriginalFilename', 'FenghuoClip.exe'),
        StringStruct('ProductName', '烽火剪辑'),
        StringStruct('ProductVersion', '{version}'),
    ])]), VarFileInfo([VarStruct('Translation', [2052, 1200])])]
)
'''
    destination.write_text(content, encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true', help='Download and verify pinned external resources without building.')
    parser.add_argument('--check-tag', help='Fail unless the supplied release tag equals v{application version}.')
    args = parser.parse_args(argv)
    version = read_version()
    if args.check_tag and args.check_tag != 'v' + version:
        parser.error(f'Release tag must be v{version}; received {args.check_tag}.')
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64') or sys.maxsize <= 2**32:
        parser.error('The desktop bundle must be built on 64-bit Windows.')
    if sys.version_info[:2] != (3, 12):
        parser.error('Use CPython 3.12 for this release configuration.')
    ensure_resources()
    if args.prepare_only:
        print('FFmpeg and voice model checksums verified.', flush=True)
        return 0
    licenses = ROOT / 'build' / 'licenses'
    collect_license_files(licenses)
    write_windows_version_info(version, ROOT / 'build' / 'windows-version.txt')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', str(ROOT / 'FenghuoClip.spec')], cwd=ROOT, check=True)
    bundle = ROOT / 'dist' / APP_NAME
    report = ROOT / 'dist' / 'self-test.json'
    frozen_smoke_test(bundle, report)
    archive, manifest = write_release_archive(bundle, ROOT / 'dist', version)
    print(json.dumps({'bundle': str(bundle), 'archive': str(archive), 'manifest': str(manifest), 'report': str(report)}, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
