import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_windows import (
    extract_verified_ffmpeg, read_version, validate_bundle_layout, write_release_archive,
)


class PackagingTests(unittest.TestCase):
    def test_release_archive_and_manifest_are_updater_compatible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / 'FenghuoClip'
            (bundle / '_internal' / 'web').mkdir(parents=True)
            (bundle / 'FenghuoClip.exe').write_bytes(b'fixture application')
            (bundle / '_internal' / 'web' / 'index.html').write_text('ui', encoding='utf-8')
            archive, manifest_path = write_release_archive(bundle, root / 'releases', '1.2.3')
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest, {
                'version': '1.2.3', 'asset': archive.name,
                'size': archive.stat().st_size,
                'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
            })
            with zipfile.ZipFile(archive) as zipped:
                self.assertCountEqual(zipped.namelist(), [
                    'FenghuoClip/FenghuoClip.exe', 'FenghuoClip/_internal/web/index.html',
                ])

    def test_bundle_rejects_user_data_and_unexpected_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '_internal').mkdir()
            (root / 'FenghuoClip.exe').write_bytes(b'exe')
            for relative in ('data/profile.json', '_internal/outputs/video.mp4', 'extra.exe'):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'private')
                with self.assertRaises(ValueError):
                    validate_bundle_layout(root)
                path.unlink()
                if path.parent != root:
                    path.parent.rmdir()

    def test_external_files_require_pinned_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'ffmpeg.zip'
            with zipfile.ZipFile(archive, 'w') as zipped:
                zipped.writestr('ffmpeg/bin/ffmpeg.exe', b'wrong executable')
                zipped.writestr('../escape.txt', b'not selected')
            config = {'directory': 'ffmpeg', 'files': {'bin/ffmpeg.exe': '0' * 64}}
            with self.assertRaises(ValueError):
                extract_verified_ffmpeg(archive, root / 'tools', config)
            self.assertFalse((root / 'tools' / 'ffmpeg' / 'bin' / 'ffmpeg.exe').exists())
            self.assertFalse((root / 'escape.txt').exists())
            config['files']['bin/ffmpeg.exe'] = hashlib.sha256(b'wrong executable').hexdigest()
            extract_verified_ffmpeg(archive, root / 'tools', config)
            self.assertEqual((root / 'tools' / 'ffmpeg' / 'bin' / 'ffmpeg.exe').read_bytes(), b'wrong executable')

    def test_version_is_literal_and_rejects_non_release_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'videocut').mkdir()
            version_file = root / 'videocut' / '__init__.py'
            version_file.write_text('__version__ = "0.2.0"', encoding='utf-8')
            self.assertEqual(read_version(root), '0.2.0')
            for value in ('0.2.0-rc1', '../x', '01.2.3'):
                version_file.write_text('__version__ = ' + repr(value), encoding='utf-8')
                with self.assertRaises(ValueError):
                    read_version(root)


if __name__ == '__main__':
    unittest.main()
