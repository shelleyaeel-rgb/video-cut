"""Install pinned wheels and a verified FFmpeg build inside this project."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DOWNLOADS = ROOT / 'work' / 'downloads'

def read_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'LocalVideoCut-setup'}), timeout=40) as response:
        return json.load(response)

def download(url, dest, expected):
    if dest.is_file():
        with dest.open('rb') as f:
            if hashlib.file_digest(f, 'sha256').hexdigest() == expected.lower():
                return expected.lower()
    temporary = dest.with_suffix(dest.suffix+'.part')
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'LocalVideoCut-setup'}), timeout=60) as response, temporary.open('wb') as output:
        shutil.copyfileobj(response, output, length=256*1024)
    with temporary.open('rb') as f:
        actual = hashlib.file_digest(f, 'sha256').hexdigest()
    if actual != expected.lower():
        raise RuntimeError('SHA-256 mismatch: '+dest.name)
    temporary.replace(dest)
    return actual

def packages(mirror=None):
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.utils import parse_wheel_filename
    tags = set(sys_tags())
    wheels = []
    if mirror and not mirror.startswith('https://'):
        raise ValueError('The package mirror must use HTTPS.')
    for requirement in (ROOT/'requirements.txt').read_text().splitlines():
        if not requirement.strip() or requirement.startswith('#'):
            continue
        name, version = requirement.split('==')
        try:
            if importlib.metadata.version(name) == version:
                print('Already installed '+requirement, flush=True)
                continue
        except importlib.metadata.PackageNotFoundError:
            pass
        metadata = read_json(f'https://pypi.org/pypi/{name}/{version}/json')
        matches = [f for f in metadata['urls'] if f['filename'].endswith('.whl') and parse_wheel_filename(f['filename'])[3] & tags]
        if not matches:
            raise RuntimeError('No compatible wheel: '+requirement)
        item = matches[0]
        target = DOWNLOADS/item['filename']
        url = mirror.rstrip('/') + urlparse(item['url']).path if mirror else item['url']
        print('Downloading '+item['filename'], flush=True)
        download(url, target, item['digests']['sha256'])
        wheels.append(str(target))
    if wheels:
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index', '--disable-pip-version-check', *wheels], check=True)

def ffmpeg():
    if list((ROOT/'.tools'/'ffmpeg').glob('**/bin/ffprobe.exe')) and list((ROOT/'.tools'/'ffmpeg').glob('**/bin/ffmpeg.exe')):
        return
    url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
    with urllib.request.urlopen(url+'.sha256', timeout=30) as response:
        checksum = response.read().decode()
    match = re.search(r'\b[a-fA-F0-9]{64}\b', checksum)
    if not match:
        raise RuntimeError('Invalid official FFmpeg checksum')
    target = DOWNLOADS/'ffmpeg-release-essentials.zip'
    print('Downloading verified FFmpeg essentials build', flush=True)
    actual = download(url, target, match.group(0))
    destination = ROOT/'.tools'/'ffmpeg'
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target) as archive:
        for member in archive.infolist():
            if not (destination/member.filename).resolve().is_relative_to(destination.resolve()):
                raise RuntimeError('Invalid archive path')
        archive.extractall(destination)
    (ROOT/'.tools'/'ffmpeg-download.json').write_text(json.dumps({'url': url, 'sha256': actual}, indent=2), encoding='utf-8')

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--mirror', help='Optional HTTPS PyPI wheel mirror; official PyPI hashes remain required')
    parser.add_argument('--ffmpeg-only', action='store_true')
    parser.add_argument('--packages-only', action='store_true')
    args=parser.parse_args()
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    if not args.ffmpeg_only:
        packages(args.mirror)
    if not args.packages_only:
        ffmpeg()
