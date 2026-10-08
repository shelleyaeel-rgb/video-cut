import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.request
import urllib.error
import uuid
import zipfile

from videocut import updater


def bundle(extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("FenghuoClip/FenghuoClip.exe", b"test app")
        archive.writestr("FenghuoClip/_internal/library.dll", b"test runtime")
        for name, content in extra or []:
            archive.writestr(name, content)
    return buffer.getvalue()


def metadata(raw, version="0.3.0"):
    return {"version": version, "asset": f"FenghuoClip-{version}-windows-x64.zip",
            "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}


def release(manifest):
    return {"tag_name": "v" + manifest["version"], "draft": False, "prerelease": False,
            "assets": [{"name": manifest["asset"], "size": manifest["size"], "id": 1002,
                        "url": f"https://api.github.com/repos/{updater.REPOSITORY}/releases/assets/1002",
                        "browser_download_url": updater._download_url(manifest["version"], manifest["asset"])},
                       {"name": "update-windows.json", "size": len(json.dumps(manifest).encode()), "id": 1001,
                        "url": f"https://api.github.com/repos/{updater.REPOSITORY}/releases/assets/1001",
                        "browser_download_url": updater._download_url(manifest["version"], "update-windows.json")}]}


class UpdateValidationTests(unittest.TestCase):
    def test_numeric_versions_do_not_compare_lexically(self):
        self.assertGreater(updater.parse_version("0.10.0"), updater.parse_version("0.9.9"))
        for value in ["v1.2.3", "1.2", "1.2.3-beta", "01.2.3", "1.2.3/evil", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                updater.parse_version(value)

    def test_manifest_rejects_untrusted_name_hash_size_version_and_extra_fields(self):
        valid = metadata(bundle())
        self.assertEqual(updater.validate_manifest(valid, "0.3.0"), valid)
        for field, value in [("asset", "../evil.zip"), ("version", "0.4.0"),
                             ("sha256", "bad"), ("size", True), ("size", 0),
                             ("size", updater.MAX_ARCHIVE_SIZE + 1), ("url", "https://evil.test")]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                updater.validate_manifest({**valid, field: value}, "0.3.0")

    def test_urls_and_redirects_are_restricted_to_github(self):
        for url in ["http://github.com/a.zip", "https://evil.test", "https://github.com/other/repo/releases/latest"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                updater._open_url(url)
        handler = updater._GithubRedirects()
        request = urllib.request.Request(updater.RELEASE_API)
        for url in ["http://github.com/x", "https://evil.test/x", "https://github.com@evil.test/x",
                    "https://github.com:8443/x", "file:///C:/file"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, "Found", {}, url)

    def test_token_is_sent_only_to_fixed_repository_api_and_never_to_redirects(self):
        with patch.dict(os.environ, {"VIDEOCUT_GITHUB_TOKEN": "test-only-token"}), patch.object(updater.urllib.request, "build_opener") as factory:
            updater._open_url(updater.RELEASE_API)
            request = factory.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer test-only-token")
            updater._open_url(updater._download_url("0.3.0", "update-windows.json"))
            self.assertIsNone(factory.return_value.open.call_args.args[0].get_header("Authorization"))
            for url in ["https://api.github.com/repos/other/private/releases/latest",
                        "https://api.github.com/user", updater.RELEASE_API + "?redirect=1"]:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    updater._open_url(url)
        handler = updater._GithubRedirects()
        for target in ["https://release-assets.githubusercontent.com/asset?signature=test", "https://objects.githubusercontent.com/asset",
                       "https://github.com/asset", "https://api.github.com/repos/other/repo/releases/latest"]:
            source = urllib.request.Request(updater.RELEASE_API, headers={"Authorization": "Bearer test-only-token"})
            source.add_unredirected_header("Authorization", "Bearer test-only-token")
            redirected = handler.redirect_request(source, None, 302, "Found", {}, target)
            self.assertIsNone(redirected.get_header("Authorization"), target)

    def test_archive_rejects_traversal_aliases_links_and_personal_files(self):
        bad_paths = ["../escape", "/absolute", "FenghuoClip/../escape", "FenghuoClip\\escape",
                     "FenghuoClip/_internal/.. /escape", "FenghuoClip/_internal/C:/escape",
                     "FenghuoClip/_internal/file:stream", "FenghuoClip/_internal/CON.txt",
                     "FenghuoClip/_internal/trailing.", "FenghuoClip/_internal//file",
                     "FenghuoClip/data/settings.json", "FenghuoClip/outputs/video.mp4",
                     "OtherApp/file", "FenghuoClip/_internal/LIBRARY.dll",
                     "FenghuoClip/FenghuoClip.exe/child", "FenghuoClip/_internal/library.dll/child"]
        for name in bad_paths:
            with self.subTest(name=name), zipfile.ZipFile(io.BytesIO(bundle([(name, b"bad")]))) as archive:
                with self.assertRaises(ValueError):
                    updater.validate_archive(archive)
        linked = zipfile.ZipInfo("FenghuoClip/_internal/link")
        linked.create_system = 3
        linked.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(io.BytesIO(bundle([(linked, b"target")]))) as archive:
            with self.assertRaises(ValueError):
                updater.validate_archive(archive)

    def test_archive_requires_both_executable_and_internal_resources(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("FenghuoClip/FenghuoClip.exe", b"exe")
        with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
            with self.assertRaises(ValueError):
                updater.validate_archive(archive)

    def test_invalid_archive_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "bad.zip"
            source.write_bytes(bundle([("../escaped.txt", b"bad")]))
            with self.assertRaises(ValueError):
                updater.extract_archive(source, root / "stage")
            self.assertFalse((root / "stage").exists())
            self.assertFalse((root / "escaped.txt").exists())


class UpdateManagerTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"VIDEOCUT_GITHUB_TOKEN": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.install = self.root / "App"
        self.install.mkdir()
        (self.install / "FenghuoClip.exe").write_bytes(b"previous")
        (self.install / "_internal").mkdir()
        self.manager = updater.UpdateManager(current_version="0.2.0", data_root=self.root / "data",
                                             install_root=self.install,
                                             resource_root=Path(__file__).resolve().parents[1], frozen=True)
        self.manager.supported = True
        self.manager._state["supported"] = True
        self.raw = bundle()
        self.manifest = metadata(self.raw)

    def tearDown(self):
        if self.manager._thread:
            self.manager._thread.join(5)
        self.temp.cleanup()

    def wait(self):
        self.manager._thread.join(5)
        self.assertFalse(self.manager._thread.is_alive())
        return self.manager.status()

    def found(self):
        with patch.object(updater, "_read_json", side_effect=[release(self.manifest), self.manifest]):
            self.manager.check()
            status = self.wait()
        self.assertEqual(status["state"], "available")

    def prepared(self):
        self.found()
        with patch.object(updater, "_open_url", return_value=io.BytesIO(self.raw)):
            self.manager.download()
            status = self.wait()
        self.assertEqual(status["state"], "ready", status)
        return self.manager._stage

    def test_check_is_opt_in_and_same_or_older_versions_need_no_manifest(self):
        self.assertEqual(self.manager.status()["state"], "idle")
        self.assertIsNone(self.manager._thread)
        for version in ["0.2.0", "0.1.0"]:
            with patch.object(updater, "_read_json", return_value={"tag_name": "v" + version}) as read:
                self.manager.check()
                self.assertEqual(self.wait()["state"], "up_to_date")
                read.assert_called_once_with(updater.RELEASE_API)

    def test_check_failure_can_be_retried(self):
        with patch.object(updater, "_read_json", side_effect=TimeoutError("network timeout")):
            self.manager.check()
            status = self.wait()
        self.assertEqual(status["state"], "error")
        self.assertIn("网络", status["error"])
        self.found()
        self.assertIsNone(self.manager.status()["error"])

    def test_github_access_errors_explain_unpublished_or_private_releases(self):
        self.found()
        for code, message in [(404, "正式版本"), (403, "限制"), (429, "限制")]:
            error = urllib.error.HTTPError(updater.RELEASE_API, code, "Failed", {}, None)
            with patch.object(updater, "_read_json", side_effect=error):
                self.manager.check()
                status = self.wait()
            self.assertEqual(status["state"], "error")
            self.assertIn(message, status["error"])
            self.assertIsNone(status["version"])
            self.assertIsNone(status["latest_version"])

    def test_private_release_uses_asset_api_with_exact_ids(self):
        with patch.dict(os.environ, {"VIDEOCUT_GITHUB_TOKEN": "test-only-token"}):
            with patch.object(updater, "_read_json", side_effect=[release(self.manifest), self.manifest]) as read:
                self.manager.check()
                self.assertEqual(self.wait()["state"], "available")
                self.assertEqual(read.call_args_list[1].args[0], f"https://api.github.com/repos/{updater.REPOSITORY}/releases/assets/1001")
            with patch.object(updater, "_open_url", return_value=io.BytesIO(self.raw)) as download:
                self.manager.download()
                self.assertEqual(self.wait()["state"], "ready")
                download.assert_called_once_with(f"https://api.github.com/repos/{updater.REPOSITORY}/releases/assets/1002")

    def test_asset_id_and_api_url_must_match_the_fixed_repository(self):
        for change in [{"id": True}, {"id": 0}, {"url": "https://api.github.com/repos/evil/repo/releases/assets/1002"},
                       {"url": f"https://api.github.com/repos/{updater.REPOSITORY}/releases/assets/9999"}]:
            wrong = release(self.manifest)
            wrong["assets"][0].update(change)
            with self.subTest(change=change), patch.object(updater, "_read_json", side_effect=[wrong, self.manifest]):
                self.manager.check()
                self.assertEqual(self.wait()["state"], "error")

    def test_mismatch_and_prerelease_are_rejected(self):
        wrong = release(self.manifest)
        wrong["assets"][0]["browser_download_url"] = "https://evil.test/a.zip"
        with patch.object(updater, "_read_json", side_effect=[wrong, self.manifest]):
            self.manager.check()
            self.assertEqual(self.wait()["state"], "error")
        with patch.object(updater, "_read_json", return_value={"tag_name": "v0.3.0", "prerelease": True}):
            self.manager.check()
            self.assertEqual(self.wait()["state"], "error")

    def test_busy_operations_are_rejected_while_check_runs(self):
        event = threading.Event()

        def delayed(url):
            event.wait(5)
            return {"tag_name": "v0.2.0"}

        try:
            with patch.object(updater, "_read_json", side_effect=delayed):
                self.assertEqual(self.manager.check()["state"], "checking")
                with self.assertRaises(ValueError):
                    self.manager.check()
                with self.assertRaises(ValueError):
                    self.manager.install()
                event.set()
                self.assertEqual(self.wait()["state"], "up_to_date")
        finally:
            event.set()

    def test_verified_download_preserves_original_and_user_files(self):
        data = self.root / "data"
        data.mkdir()
        (data / "settings.json").write_text("keep me")
        stage = self.prepared()
        self.assertEqual((stage / "FenghuoClip/FenghuoClip.exe").read_bytes(), b"test app")
        self.assertTrue((stage / "apply-update.ps1").is_file())
        self.assertEqual((self.install / "FenghuoClip.exe").read_bytes(), b"previous")
        self.assertEqual((data / "settings.json").read_text(), "keep me")
        self.assertEqual(list((data / "updates").glob("*.part")), [])

    def test_hash_and_size_failure_clean_up_and_allow_retry(self):
        self.found()
        for bad in [self.raw[:-1], self.raw + b"extra", b"x" * len(self.raw)]:
            with patch.object(updater, "_open_url", return_value=io.BytesIO(bad)):
                self.manager.download()
                self.assertEqual(self.wait()["state"], "error")
            self.assertIsNone(self.manager._stage)
            self.assertEqual(list((self.root / "data/updates").glob("*.part")), [])
        with patch.object(updater, "_open_url", return_value=io.BytesIO(self.raw)):
            self.manager.download()
            self.assertEqual(self.wait()["state"], "ready")

    def test_valid_hash_does_not_allow_zip_traversal(self):
        self.raw = bundle([("../escaped", b"bad")])
        self.manifest = metadata(self.raw)
        self.found()
        with patch.object(updater, "_open_url", return_value=io.BytesIO(self.raw)):
            self.manager.download()
            self.assertEqual(self.wait()["state"], "error")
        self.assertEqual(list(self.root.glob(".FenghuoClip-update-*")), [])
        self.assertFalse((self.root / "escaped").exists())

    def test_personal_files_in_install_directory_block_swap(self):
        (self.install / "my-video.mp4").write_bytes(b"keep")
        self.found()
        with patch.object(updater, "_open_url", return_value=io.BytesIO(self.raw)):
            self.manager.download()
            self.assertEqual(self.wait()["state"], "error")
        self.assertEqual((self.install / "my-video.mp4").read_bytes(), b"keep")

    def test_source_mode_cannot_download_or_install(self):
        self.manager.supported = False
        self.found()
        with self.assertRaises(ValueError):
            self.manager.download()
        with self.assertRaises(ValueError):
            self.manager.install()

    def test_install_waits_for_helper_ready_and_hands_off_without_swapping(self):
        stage = self.prepared()

        def spawn(*args, **kwargs):
            (stage / "helper-ready").write_text("ready")
            return Mock(poll=Mock(return_value=None))

        with patch.object(updater.subprocess, "Popen", side_effect=spawn) as popen:
            self.assertEqual(self.manager.install()["state"], "installing")
            self.assertIn(str(stage / "apply-update.ps1"), popen.call_args.args[0])
            self.assertIn(str(os.getpid()), popen.call_args.args[0])
        with self.assertRaises(ValueError):
            self.manager.check()
        self.assertEqual((self.install / "FenghuoClip.exe").read_bytes(), b"previous")

    def test_failed_helper_keeps_application_ready_for_retry(self):
        self.prepared()
        with patch.object(updater.subprocess, "Popen", return_value=Mock(poll=Mock(return_value=1))):
            with self.assertRaises(ValueError):
                self.manager.install()
        self.assertEqual(self.manager.status()["state"], "ready")
        self.assertTrue((self.install / "FenghuoClip.exe").is_file())

    def test_missing_prepared_files_allow_redownload(self):
        stage = self.prepared()
        (stage / "FenghuoClip/FenghuoClip.exe").unlink()
        with self.assertRaises(ValueError):
            self.manager.install()
        self.assertEqual(self.manager.status()["state"], "error")
        with patch.object(updater, "_open_url", return_value=io.BytesIO(self.raw)):
            self.manager.download()
            self.assertEqual(self.wait()["state"], "ready")

    def test_health_marker_only_accepts_owned_sibling_stage(self):
        stage = self.prepared()
        with patch.object(updater, "FROZEN", True), patch.object(updater, "INSTALL_ROOT", self.install):
            self.assertTrue(updater.mark_update_healthy(stage / "health-ok"))
            for wrong in [self.root / "health-ok", stage / "other-file", self.root / ".FenghuoClip-update-invalid/health-ok"]:
                with self.subTest(path=wrong), self.assertRaises(ValueError):
                    updater.mark_update_healthy(wrong)

    @unittest.skipUnless(os.name == "nt", "PowerShell validation runs on Windows")
    def test_powershell_validates_paths_without_modifying_any_installation(self):
        stage = self.prepared()
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        script = Path(__file__).resolve().parents[1] / "scripts/apply-update.ps1"
        command = [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                   "-InstallDir", str(self.install), "-StageRoot", str(stage), "-ParentProcessId", str(os.getpid()),
                   "-Version", "0.3.0", "-ResultPath", str(self.root / "data/updates/result.json"), "-ValidateOnly"]
        result = subprocess.run(command, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertIn(b"no files changed", result.stdout)
        self.assertEqual((self.install / "FenghuoClip.exe").read_bytes(), b"previous")
        self.assertFalse((stage / "helper-ready").exists())


@unittest.skipUnless(os.name == "nt", "Isolated executable replacement tests require Windows")
class WindowsReplacementTests(unittest.TestCase):
    """Only swap throwaway C# fixtures; never touch the real app installation."""

    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="fenghuo-update-fixture-")
        cls.build_root = Path(cls.build.name)
        compiler = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if not compiler.is_file():
            cls.build.cleanup()
            raise unittest.SkipTest("The Windows .NET Framework fixture compiler is unavailable")
        source = cls.build_root / "Fixture.cs"
        source.write_text('''using System;
using System.IO;
class Fixture {
    static void Main(string[] args) {
        string resources = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "_internal");
        if (File.Exists(Path.Combine(resources, "fail.txt"))) return;
        File.WriteAllText(Path.Combine(resources, "started.txt"), "started");
        if (args.Length == 2 && args[0] == "--update-health")
            File.WriteAllText(args[1], File.ReadAllText(Path.Combine(resources, "version.txt")).Trim());
    }
}''', encoding="utf-8")
        cls.fixture = cls.build_root / "FenghuoClip.exe"
        result = subprocess.run([str(compiler), "/nologo", "/target:winexe", "/out:" + str(cls.fixture), str(source)],
                                capture_output=True, timeout=30)
        if result.returncode:
            cls.build.cleanup()
            raise RuntimeError(result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace"))

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fenghuo updater e2e ")
        self.root = Path(self.temp.name)
        self.install = self.root / "Application with spaces"
        self.stage = self.root / (".FenghuoClip-update-" + uuid.uuid4().hex)
        self.data = self.root / "User Data"
        self.data.mkdir()
        (self.data / "settings.json").write_text('{"keep":"templates and settings"}')
        (self.data / "export.mp4").write_bytes(b"user video")
        self.make_app(self.install, "0.2.0")
        self.make_app(self.stage / "FenghuoClip", "0.3.0")

    def tearDown(self):
        self.temp.cleanup()

    def make_app(self, destination, version):
        internal = destination / "_internal"
        internal.mkdir(parents=True)
        shutil.copyfile(self.fixture, destination / "FenghuoClip.exe")
        (internal / "version.txt").write_text(version)

    def run_helper(self):
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        script = Path(__file__).resolve().parents[1] / "scripts/apply-update.ps1"
        command = [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                   "-InstallDir", str(self.install), "-StageRoot", str(self.stage), "-ParentProcessId", "2147483647",
                   "-Version", "0.3.0", "-ResultPath", str(self.data / "updates/result.json")]
        result = subprocess.run(command, capture_output=True, timeout=30)
        self.assertEqual((self.data / "settings.json").read_text(), '{"keep":"templates and settings"}')
        self.assertEqual((self.data / "export.mp4").read_bytes(), b"user video")
        return result

    def test_real_directory_replacement_and_successful_health_handshake(self):
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual((self.install / "_internal/version.txt").read_text(), "0.3.0")
        self.assertEqual((self.stage / "health-ok").read_text(), "0.3.0")
        self.assertFalse((self.stage / "previous").exists())
        record = json.loads((self.data / "updates/result.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(record["state"], "success")

    def test_failed_new_executable_restores_and_restarts_previous_version(self):
        (self.stage / "FenghuoClip/_internal/fail.txt").write_text("simulate startup failure")
        result = self.run_helper()
        self.assertEqual(result.returncode, 1, result.stderr.decode(errors="replace"))
        self.assertEqual((self.install / "_internal/version.txt").read_text(), "0.2.0")
        self.assertFalse((self.stage / "previous").exists())
        self.assertFalse((self.stage / "failed").exists())
        deadline = time.monotonic() + 5
        while not (self.install / "_internal/started.txt").is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue((self.install / "_internal/started.txt").is_file(), "Previous app was not restarted")
        record = json.loads((self.data / "updates/result.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(record["state"], "error")
        self.assertIn("Previous version retained", record["message"])

    def test_files_added_after_download_prevent_replacement(self):
        (self.install / "personal-notes.txt").write_text("keep this file")
        result = self.run_helper()
        self.assertEqual(result.returncode, 1, result.stderr.decode(errors="replace"))
        self.assertEqual((self.install / "personal-notes.txt").read_text(), "keep this file")
        self.assertEqual((self.install / "_internal/version.txt").read_text(), "0.2.0")


if __name__ == "__main__":
    unittest.main()
