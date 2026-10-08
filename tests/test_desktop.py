import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from videocut import paths
from videocut.desktop import InstanceLock
from videocut.server import App
from videocut.preferences import Preferences


class DesktopTests(unittest.TestCase):
    def test_source_and_frozen_worker_dispatch(self):
        with patch.object(paths, 'FROZEN', False):
            self.assertEqual(paths.worker_command('separator', 'a', 'b')[1:], ['-m', 'videocut.separator', 'a', 'b'])
        with patch.object(paths, 'FROZEN', True):
            self.assertEqual(paths.worker_command('picker', 'video')[1:], ['--worker', 'picker', 'video'])
        with self.assertRaises(ValueError):
            paths.worker_command('untrusted')

    def test_frozen_data_is_outside_installation(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}, clear=True), patch.object(paths, 'FROZEN', True):
            self.assertEqual(paths.data_root(), Path(folder) / 'FenghuoClip')
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'VIDEOCUT_DATA_DIR': folder}), patch.object(paths, 'FROZEN', True):
            self.assertEqual(paths.data_root(), Path(folder).resolve())

    def test_only_one_desktop_uses_same_data(self):
        with tempfile.TemporaryDirectory() as folder:
            with InstanceLock(folder):
                with self.assertRaises(RuntimeError):
                    with InstanceLock(folder):
                        pass
            with InstanceLock(folder):
                pass

    def test_update_refuses_running_job_and_rolls_back_failed_install_gate(self):
        app = App()
        app.desktop = True
        app.request_exit = Mock()
        app.updater = Mock()
        app.active = 'rendering'
        with self.assertRaises(ValueError):
            app.begin_exit(update=True)
        app.updater.install.assert_not_called()
        self.assertFalse(app.closing)
        app.active = None
        app.updater.install.side_effect = OSError('read only destination')
        with self.assertRaises(OSError):
            app.begin_exit(update=True)
        self.assertFalse(app.closing)
        app.request_exit.assert_not_called()

    def test_successful_install_blocks_new_jobs_before_shutdown(self):
        app = App()
        app.desktop = True
        app.request_exit = Mock()
        app.updater = Mock()
        app.updater.install.return_value = {'state': 'installing'}
        with patch('videocut.server.threading.Timer') as timer:
            self.assertEqual(app.begin_exit(update=True)['state'], 'installing')
            timer.return_value.start.assert_called_once()
            self.assertTrue(app.closing)
            with self.assertRaises(ValueError):
                app.start('demo')

    def test_source_cannot_exit_through_desktop_api(self):
        with self.assertRaises(ValueError):
            App().begin_exit()

    def test_preferences_survive_restart_without_saving_media_or_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preferences.json'
            first = Preferences(path)
            first.save({'target': 60, 'encoder': 'nvenc', 'strength': .2})
            self.assertEqual(Preferences(path).get()['target'], 60)
            for invalid in ({'video': 'personal.mp4'}, {'target': float('nan')}, {'encoder': 'shell'}, {'strength': True}):
                with self.assertRaises(ValueError):
                    first.save(invalid)
            self.assertEqual(Preferences(path).get(), first.get())
            path.write_text('{invalid')
            self.assertEqual(Preferences(path).get(), {})


if __name__ == '__main__':
    unittest.main()
