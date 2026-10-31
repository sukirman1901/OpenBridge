import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.restart import restart


class SafeRestart(unittest.TestCase):
    def test_timeout_does_not_signal_or_replace_running_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'bridge.pid').write_text('123')
            with patch('deploy.restart.owned_process', return_value=True), \
                    patch('deploy.restart.running_jobs', return_value=1), \
                    patch('deploy.restart.os.kill') as kill, \
                    patch('deploy.restart.subprocess.Popen') as spawn:
                with self.assertRaises(TimeoutError):
                    restart(home, timeout=0)
                kill.assert_not_called()
                spawn.assert_not_called()
            self.assertFalse((home / 'bridge.drain').exists())

    def test_does_not_remove_someone_elses_drain_request(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'bridge.drain').write_text('other operator')
            with self.assertRaises(FileExistsError):
                restart(home)
            self.assertEqual((home / 'bridge.drain').read_text(), 'other operator')
