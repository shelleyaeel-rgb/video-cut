"""Fetch the pinned, checksum-verified portable OBS runtime (no machine install)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "32.2.2"
URL = f"https://github.com/obsproject/obs-studio/releases/download/{VERSION}/OBS-Studio-{VERSION}-Windows-x64.zip"
SHA256 = "4d6e40e3ab155f56b30de517380566a206d74b63cdf5ad49aa596924768f97e1"
DEST = ROOT / ".tools" / "obs"
PYTHON_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
PYTHON_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
VC_URL = "https://download.visualstudio.microsoft.com/download/pr/bd1c8d9d-ba95-4eee-bc6e-df1fcc876373/CC0FF0EB1DC3F5188AE6300FAEF32BF5BEEBA4BDD6E8E445A9184072096B713B/VC_redist.x64.exe"
VC_SHA256 = "cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b"
VC_VERSION = "14.44.35211.0"
COPYING_URL = "https://raw.githubusercontent.com/obsproject/obs-studio/32.2.2/COPYING"
COPYING_SHA256 = "8177f97513213526df2cf6184d8ff986c675afb514d4e68a404010521b880643"


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url, target, sha256):
    if target.is_file() and file_hash(target) == sha256:
        return target
    partial = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "FenghuoClip-Capture-Setup"})
    with urllib.request.urlopen(request, timeout=90) as response, partial.open("wb") as output:
        shutil.copyfileobj(response, output, 1024 * 1024)
    if file_hash(partial) != sha256:
        raise RuntimeError("Capture dependency SHA-256 mismatch: " + target.name)
    partial.replace(target)
    return target


def extract_zip(archive, destination):
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            resolved = (destination / member.filename).resolve()
            if not resolved.is_relative_to(destination.resolve()):
                raise RuntimeError("Unsafe archive member")
        source.extractall(destination)


def install_local_vc(archive, destination, cache):
    """Extract verified Microsoft Burn cabinets; never execute the installer."""
    unpack = cache / "vc-14.44.35211"
    unpack.mkdir(parents=True, exist_ok=True)
    data = archive.read_bytes()
    cabinets, position = [], 0
    while True:
        position = data.find(b"MSCF", position)
        if position < 0:
            break
        if position + 36 <= len(data):
            size = struct.unpack_from("<I", data, position + 8)[0]
            if 36 <= size <= len(data) - position and data[position + 4:position + 8] == b"\0" * 4:
                cabinet = unpack / f"container-{len(cabinets)}.cab"
                cabinet.write_bytes(data[position:position + size])
                cabinets.append(cabinet)
        position += 4
    if len(cabinets) != 2:
        raise RuntimeError("Unexpected Microsoft redistributable container layout")
    manifest_dir, payload_dir, dll_dir = (unpack / name for name in ("manifest", "payload", "dlls"))
    for folder in (manifest_dir, payload_dir, dll_dir):
        folder.mkdir(exist_ok=True)

    def expand(source, target, pattern="*"):
        subprocess.run(["expand.exe", str(source), "-F:" + pattern, str(target)],
                       check=True, stdout=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    expand(cabinets[0], manifest_dir)
    expand(cabinets[1], payload_dir)
    root = ET.parse(manifest_dir / "0").getroot()
    ns = {"b": "http://schemas.microsoft.com/wix/2008/Burn"}
    selected = next((item for item in root.findall("b:Payload", ns)
                     if item.get("FilePath", "").lower().endswith(r"vcruntimeminimum_amd64\cab1.cab")), None)
    if selected is None:
        raise RuntimeError("Microsoft runtime payload was not found")
    payload = payload_dir / selected.attrib["SourcePath"]
    if payload.parent != payload_dir:
        raise RuntimeError("Invalid runtime payload path")
    expand(payload, dll_dir)
    count = 0
    for dll in dll_dir.glob("*.dll_amd64"):
        shutil.copy2(dll, destination / dll.name.removesuffix("_amd64"))
        count += 1
    if count < 5:
        raise RuntimeError("Microsoft runtime extraction did not produce the required DLLs")
    license_item = next((item for item in root.findall("b:UX/b:Payload", ns)
                         if item.get("FilePath") == "license.rtf"), None)
    if license_item is not None:
        shutil.copy2(manifest_dir / license_item.attrib["SourcePath"], destination / "VC-REDIST-LICENSE.rtf")


def ensure(destination=DEST, cache=None):
    """Idempotently return the verified private runtime manifest, installing if needed."""
    if os.name != "nt":
        raise RuntimeError("This capture runtime requires 64-bit Windows.")
    destination = Path(destination).resolve()
    cache = Path(cache or ROOT / "work" / "downloads").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    download(COPYING_URL, destination / "OBS-COPYING.txt", COPYING_SHA256)
    manifest_path = destination / "capture-runtime.json"
    try:
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        if saved.get("version") == VERSION and saved.get("vc_sha256") == VC_SHA256 and saved.get("files"):
            if all((destination / relative).is_file() and file_hash(destination / relative) == digest
                   for relative, digest in saved["files"].items()):
                if saved.get("obs_license_sha256") != COPYING_SHA256:
                    saved.update(obs_license_url=COPYING_URL, obs_license_sha256=COPYING_SHA256)
                    saved["files"]["OBS-COPYING.txt"] = COPYING_SHA256
                    manifest_path.write_text(json.dumps(saved, indent=2), encoding="utf-8")
                return saved
    except (OSError, ValueError):
        pass
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"OBS-Studio-{VERSION}-Windows-x64.zip"
    download(URL, archive, SHA256)
    destination.mkdir(parents=True, exist_ok=True)
    extract_zip(archive, destination)
    # OBS's native muxer resolves its helper next to the executable. A private
    # stdlib-only Python runtime lives there too, independent of system Python.
    python_archive = cache / "python-3.12.10-embed-amd64.zip"
    download(PYTHON_URL, python_archive, PYTHON_SHA256)
    extract_zip(python_archive, destination / "bin" / "64bit")
    vc_archive = download(VC_URL, cache / "vc_redist.x64.exe", VC_SHA256)
    install_local_vc(vc_archive, destination / "bin" / "64bit", cache)
    manifest = {"version": VERSION, "url": URL, "sha256": SHA256,
                "license": "GPL-2.0-or-later", "runtime": "libobs",
                "python_url": PYTHON_URL, "python_sha256": PYTHON_SHA256,
                "vc_url": VC_URL, "vc_sha256": VC_SHA256, "vc_version": VC_VERSION,
                "obs_license_url": COPYING_URL, "obs_license_sha256": COPYING_SHA256}
    manifest["files"] = {path.relative_to(destination).as_posix(): file_hash(path)
                         for folder in (destination / "bin" / "64bit", destination / "obs-plugins" / "64bit")
                         for path in folder.iterdir() if path.is_file() and path.suffix.lower() in (".dll", ".exe", ".pyd", ".zip", "._pth")}
    manifest["files"]["OBS-COPYING.txt"] = COPYING_SHA256
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main():
    manifest = ensure()
    print(json.dumps({key: value for key, value in manifest.items() if key != "files"}), flush=True)
    print(f"Installed portable runtime: {DEST}", flush=True)


if __name__ == "__main__":
    main()
