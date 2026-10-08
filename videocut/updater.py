"""Opt-in, verified Windows updates from this project's GitHub Releases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from . import __version__
from .paths import DATA_ROOT, FROZEN, INSTALL_ROOT, RESOURCE_ROOT

REPOSITORY = "shelleyaeel-rgb/video-cut"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_ARCHIVE_SIZE = 4 * 1024**3
MAX_UNPACKED_SIZE = 12 * 1024**3
NETWORK_TIMEOUT = 30
_BUSY = {"checking", "downloading", "installing"}
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_STAGE_NAME = re.compile(r"\.FenghuoClip-update-[0-9a-f]{32}\Z")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z", re.I)


def parse_version(value: str) -> tuple[int, int, int]:
    """Only stable numeric releases are eligible for automatic installation."""
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError("版本号必须为稳定版数字格式，例如 0.2.0")
    return tuple(map(int, value.split(".")))


def _download_url(version: str, asset: str) -> str:
    return f"https://github.com/{REPOSITORY}/releases/download/v{version}/{asset}"


def _is_repo_api(url: str) -> bool:
    return bool(re.fullmatch(rf"https://api\.github\.com/repos/{re.escape(REPOSITORY)}/releases/"
                             r"(?:latest|assets/[1-9][0-9]*)", url))


def _asset_record(release: dict, name: str, version: str, maximum_size: int) -> dict:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ValueError("发布记录缺少安装包附件")
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    if len(matches) != 1:
        raise ValueError("发布记录缺少唯一的 " + name)
    asset = matches[0]
    ident, size = asset.get("id"), asset.get("size")
    if (type(ident) is not int or ident <= 0 or type(size) is not int or not 0 < size <= maximum_size
            or asset.get("url") != f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{ident}"
            or asset.get("browser_download_url") != _download_url(version, name)):
        raise ValueError("发布附件的来源、编号或大小不正确")
    return asset


class _GithubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.hostname not in {
                    "github.com", "api.github.com", "release-assets.githubusercontent.com",
                    "objects.githubusercontent.com",
                }):
            raise ValueError("更新下载重定向到了不受信任的地址")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            # Signed asset URLs carry their own authorization. Never copy a PAT
            # into redirected requests, even if GitHub changes its CDN hosts.
            redirected.remove_header("Authorization")
        return redirected


def _open_url(url: str):
    if not (_is_repo_api(url) or re.fullmatch(
        rf"https://github\.com/{re.escape(REPOSITORY)}/releases/download/"
        r"v[0-9]+\.[0-9]+\.[0-9]+/(?:update-windows\.json|FenghuoClip-[0-9]+\.[0-9]+\.[0-9]+-windows-x64\.zip)",
        url,
    )):
        raise ValueError("更新来源必须为项目官方 GitHub Releases")
    request = urllib.request.Request(url, headers={
        "User-Agent": f"FenghuoClip/{__version__}",
        "Accept": "application/vnd.github+json" if url == RELEASE_API else "application/octet-stream",
    })
    token = os.environ.get("VIDEOCUT_GITHUB_TOKEN", "").strip()
    if token and _is_repo_api(url):
        # unredirected headers are deliberately excluded from urllib redirects.
        request.add_unredirected_header("Authorization", "Bearer " + token)
    return urllib.request.build_opener(_GithubRedirects()).open(request, timeout=NETWORK_TIMEOUT)


def _read_json(url: str) -> dict:
    with _open_url(url) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("更新元数据过大")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("更新元数据格式不正确")
    return value


def _friendly_error(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 404:
            return "尚无可下载的正式版本，或当前无权访问仓库；私有仓库请配置 GitHub 更新凭据"
        if error.code in (403, 429):
            return "GitHub 暂时限制了访问，请稍后重试；也请确认仓库与发布版本可公开访问"
        return f"GitHub 请求失败（HTTP {error.code}），请稍后重试"
    if isinstance(error, (TimeoutError, urllib.error.URLError)):
        return "连接 GitHub 失败或网络请求超时，请检查网络后重试"
    if isinstance(error, json.JSONDecodeError):
        return "GitHub 发布的更新清单格式不正确，请稍后重试"
    if isinstance(error, PermissionError):
        return "无法写入更新文件，请关闭占用文件的程序，并确认应用目录具有写入权限"
    return str(error)


def validate_manifest(manifest: dict, version: str) -> dict:
    """Release metadata is deliberately small and has no arbitrary URLs/paths."""
    parse_version(version)
    if set(manifest) != {"version", "asset", "sha256", "size"}:
        raise ValueError("更新清单字段不正确")
    if manifest["version"] != version:
        raise ValueError("更新清单版本与发布标签不一致")
    if manifest["asset"] != f"FenghuoClip-{version}-windows-x64.zip":
        raise ValueError("更新安装包名称不正确")
    if not isinstance(manifest["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]):
        raise ValueError("更新清单缺少有效 SHA-256")
    if type(manifest["size"]) is not int or not 0 < manifest["size"] <= MAX_ARCHIVE_SIZE:
        raise ValueError("更新安装包大小不正确")
    return dict(manifest)


def validate_archive(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Reject Windows aliases, traversal, links and files outside the app bundle."""
    members = archive.infolist()
    if len(members) > 200000:
        raise ValueError("更新安装包文件数量异常")
    seen = set()
    total = 0
    has_exe = has_internal = False
    for entry in members:
        name = entry.filename
        # ZipInfo truncates at NUL; orig_filename preserves it for validation.
        if "\0" in entry.orig_filename or "\\" in name or not name or name.startswith("/"):
            raise ValueError("更新安装包包含不安全路径")
        parts = name.rstrip("/").split("/")
        if any(part in {"", ".", ".."} or part.endswith((".", " "))
               or any(c in part for c in ':<>"|?*') or any(ord(c) < 32 for c in part)
               or _RESERVED.fullmatch(part) for part in parts):
            raise ValueError("更新安装包包含不安全路径")
        if parts[0] != "FenghuoClip":
            raise ValueError("更新安装包必须只包含 FenghuoClip 目录")
        if len(parts) == 1 and not entry.is_dir():
            raise ValueError("更新安装包目录结构不正确")
        if len(parts) > 1 and parts[1] not in {"FenghuoClip.exe", "_internal"}:
            raise ValueError("更新安装包包含不允许覆盖的用户文件")
        if len(parts) > 2 and parts[1] != "_internal":
            raise ValueError("更新安装包目录结构不正确")
        mode = entry.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError("更新安装包不允许符号链接或特殊文件")
        if entry.flag_bits & 1:
            raise ValueError("更新安装包不允许加密文件")
        key = "/".join(parts).casefold()
        if key in seen:
            raise ValueError("更新安装包包含重复文件路径")
        seen.add(key)
        total += entry.file_size
        if total > MAX_UNPACKED_SIZE:
            raise ValueError("更新安装包解压大小超出限制")
        if name == "FenghuoClip/FenghuoClip.exe" and not entry.is_dir() and entry.file_size > 0:
            has_exe = True
        if len(parts) > 2 and parts[1] == "_internal" and not entry.is_dir():
            has_internal = True
    if not has_exe or not has_internal:
        raise ValueError("更新安装包缺少应用程序或运行资源")
    # No path may also act as a file and an ancestor directory.
    files = {e.filename.casefold() for e in members if not e.is_dir()}
    for entry in members:
        if any(str(parent).casefold() in files for parent in PurePosixPath(entry.filename).parents):
            raise ValueError("更新安装包存在文件与目录冲突")
    return members


def extract_archive(path: Path, stage_root: Path) -> Path:
    """Validate every entry before writing anything; stage_root must be new."""
    with zipfile.ZipFile(path) as archive:
        members = validate_archive(archive)
        stage_root.mkdir(parents=False, exist_ok=False)
        try:
            for entry in members:
                target = stage_root.joinpath(*entry.filename.rstrip("/").split("/"))
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, target.open("xb") as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
        except Exception:
            shutil.rmtree(stage_root)
            raise
    return stage_root / "FenghuoClip"


def mark_update_healthy(path: str | Path | None = None) -> bool:
    """Called only after the new desktop window and local server are ready."""
    if not path or not FROZEN:
        return False
    marker = Path(path).resolve()
    if (marker.name != "health-ok" or not _STAGE_NAME.fullmatch(marker.parent.name)
            or marker.parent.parent != INSTALL_ROOT.resolve().parent or not marker.parent.is_dir()):
        raise ValueError("更新健康标记路径无效")
    marker.write_text(__version__, encoding="utf-8")
    return True


class UpdateManager:
    def __init__(self, *, current_version=None, data_root=None, install_root=None,
                 resource_root=None, frozen=None):
        self.current_version = current_version or __version__
        parse_version(self.current_version)
        self.data_root = Path(data_root or DATA_ROOT).resolve()
        self.install_root = Path(install_root or INSTALL_ROOT).resolve()
        self.resource_root = Path(resource_root or RESOURCE_ROOT).resolve()
        self.supported = bool(FROZEN if frozen is None else frozen) and sys.platform == "win32"
        self._lock = threading.RLock()
        self._thread = None
        self._release = None
        self._asset_url = None
        self._stage = None
        self._state = {
            "state": "idle", "current_version": self.current_version,
            "version": None, "latest_version": None, "progress": 0,
            "message": "点击检查更新", "error": None, "supported": self.supported,
            "release_url": f"https://github.com/{REPOSITORY}/releases",
        }
        self._result_mtime = None

    def status(self) -> dict:
        with self._lock:
            # A rollback starts the previous app again; make its failure visible.
            result_path = self.data_root / "updates" / "result.json"
            try:
                modified = result_path.stat().st_mtime_ns
                if modified != self._result_mtime and self._state["state"] == "idle":
                    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
                    self._result_mtime = modified
                    if result.get("state") == "error":
                        self._state.update(state="error", error=result.get("message"),
                                           message=result.get("message", "上次更新失败，已保留原版本"))
                    elif result.get("version") == self.current_version:
                        self._state["message"] = "已成功更新至 " + self.current_version
            except (OSError, ValueError):
                pass
            return dict(self._state)

    def _set(self, **values):
        with self._lock:
            self._state.update(values)

    def _start(self, state: str, message: str, operation) -> dict:
        with self._lock:
            if self._state["state"] in _BUSY:
                raise ValueError("正在处理更新，请等待当前操作完成")
            self._set(state=state, message=message, error=None, progress=0)

            def run():
                try:
                    operation()
                except Exception as error:
                    self._set(state="error", message="更新失败，可重试", error=_friendly_error(error))

            self._thread = threading.Thread(target=run, name="fenghuo-update", daemon=True)
            self._thread.start()
            return dict(self._state)

    def check(self) -> dict:
        return self._start("checking", "正在检查 GitHub Releases…", self._check)

    def _check(self):
        self._release = None
        self._asset_url = None
        self._set(version=None, latest_version=None, release_url=f"https://github.com/{REPOSITORY}/releases")
        self._remove_stage()
        release = _read_json(RELEASE_API)
        tag = release.get("tag_name", "")
        if release.get("draft") or release.get("prerelease") or not isinstance(tag, str) or not tag.startswith("v"):
            raise ValueError("未找到可安装的正式版本")
        version = tag[1:]
        parse_version(version)
        self._set(version=version, latest_version=version,
                  release_url=f"https://github.com/{REPOSITORY}/releases/tag/v{version}")
        if parse_version(version) <= parse_version(self.current_version):
            self._release = None
            self._set(state="up_to_date", message="当前已是最新版本", progress=100)
            return
        manifest_asset = _asset_record(release, "update-windows.json", version, 1024 * 1024)
        private_access = bool(os.environ.get("VIDEOCUT_GITHUB_TOKEN", "").strip())
        manifest_url = manifest_asset["url"] if private_access else manifest_asset["browser_download_url"]
        manifest = validate_manifest(_read_json(manifest_url), version)
        package_asset = _asset_record(release, manifest["asset"], version, MAX_ARCHIVE_SIZE)
        if package_asset["size"] != manifest["size"]:
            raise ValueError("发布记录中的安装包与更新清单不一致")
        self._release = manifest
        self._asset_url = package_asset["url"] if private_access else package_asset["browser_download_url"]
        self._set(state="available", message="发现新版本 " + version, progress=0)

    def download(self) -> dict:
        with self._lock:
            if not self.supported:
                raise ValueError("源码运行模式仅支持检查版本，请使用 Windows 免安装版安装更新")
            if self._release is None:
                raise ValueError("请先检查并找到可用更新")
            if self._state["state"] == "ready":
                return dict(self._state)
            return self._start("downloading", "正在下载并校验更新…", self._download)

    prepare = download

    def _remove_stage(self):
        if self._stage is not None:
            stage = self._stage.resolve()
            if stage.parent != self.install_root.parent or not _STAGE_NAME.fullmatch(stage.name):
                raise ValueError("更新暂存目录无效")
            if stage.exists():
                shutil.rmtree(stage)
            self._stage = None

    def _download(self):
        manifest = dict(self._release)
        self._remove_stage()
        folder = self.data_root / "updates"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (uuid.uuid4().hex + ".zip.part")
        try:
            digest = hashlib.sha256()
            size = 0
            started = time.monotonic()
            with _open_url(self._asset_url) as source, path.open("xb") as target:
                while True:
                    if time.monotonic() - started > 3600:
                        raise TimeoutError("下载超过一小时，请检查网络后重试")
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > manifest["size"]:
                        raise ValueError("更新安装包超出清单声明的大小")
                    digest.update(chunk)
                    target.write(chunk)
                    self._set(progress=min(90, round(size * 90 / manifest["size"])))
            if size != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
                raise ValueError("更新安装包 SHA-256 或文件大小校验失败")
            if not (self.install_root / "FenghuoClip.exe").is_file():
                raise ValueError("当前安装目录缺少 FenghuoClip.exe")
            if any(item.name not in {"FenghuoClip.exe", "_internal"} for item in self.install_root.iterdir()):
                raise ValueError("安装目录含有额外文件，请先移出个人文件后重试更新")
            # User settings/exports must never be swallowed by the directory swap.
            if self.data_root == self.install_root or self.install_root in self.data_root.parents:
                raise ValueError("用户数据目录位于安装目录内，请先将数据迁移到独立目录")
            self._set(progress=92, message="正在准备更新文件…")
            self._stage = self.install_root.parent / (".FenghuoClip-update-" + uuid.uuid4().hex)
            extract_archive(path, self._stage)
            script = self.resource_root / "scripts" / "apply-update.ps1"
            shutil.copyfile(script, self._stage / "apply-update.ps1")
            self._set(state="ready", progress=100, message="更新已就绪，任务结束后可重启安装")
        except Exception:
            self._remove_stage()
            raise
        finally:
            path.unlink(missing_ok=True)

    def install(self) -> dict:
        """Caller holds its job mutex and must exit only after this succeeds."""
        with self._lock:
            if not self.supported or self._state["state"] != "ready" or self._stage is None:
                raise ValueError("更新尚未准备好")
            stage = self._stage.resolve()
            if (stage.parent != self.install_root.parent or not _STAGE_NAME.fullmatch(stage.name)
                    or not (stage / "FenghuoClip" / "FenghuoClip.exe").is_file()):
                self._set(state="error", error="更新暂存文件已丢失", message="请重新下载更新")
                raise ValueError("更新暂存文件已丢失，请重新下载")
            powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            command = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                       "-File", str(stage / "apply-update.ps1"), "-InstallDir", str(self.install_root),
                       "-StageRoot", str(stage), "-ParentProcessId", str(os.getpid()),
                       "-Version", self._release["version"], "-ResultPath", str(self.data_root / "updates" / "result.json")]
            try:
                (stage / "helper-ready").unlink(missing_ok=True)
                helper = subprocess.Popen(command, cwd=str(stage), close_fds=True,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                deadline = time.monotonic() + 10
                while not (stage / "helper-ready").is_file():
                    if helper.poll() is not None:
                        raise OSError("更新程序启动后提前退出")
                    if time.monotonic() >= deadline:
                        helper.terminate()
                        helper.wait(timeout=5)
                        raise OSError("更新程序未及时准备好")
                    time.sleep(0.05)
            except OSError as error:
                self._set(error=str(error), message="无法启动更新程序，可重试")
                raise ValueError("无法启动更新程序：" + str(error)) from error
            self._set(state="installing", message="正在重启安装更新…", error=None)
            return dict(self._state)
