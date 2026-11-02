"""Max cooling accepts hot readings and always verifies release before disarming."""

from contextlib import redirect_stdout
import io
import subprocess
import unittest
from unittest.mock import patch

from tools import cool_down
from tools import validate_backend
from tests.test_suspend_resume import FakeRunner


class CoolingRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.status['temps'] = {'cpu': 92, 'acer_temp1': 92}
        self.temperatures = [92, 85, 69]
        self.requested_max = False

    def request(self, command):
        if command['cmd'] == 'set_mode':
            self.requested_max = command['mode'] == 'max'
            self.status['mode'] = self.status['requested_mode'] = command['mode']
            return {'ok': True}
        if len(self.temperatures) > 1:
            value = self.temperatures.pop(0)
        else:
            value = self.temperatures[0]
        self.status['temps'] = {'cpu': value, 'acer_temp1': value}
        return self.status


class TestCooldown(unittest.TestCase):
    def setUp(self):
        self.runner = CoolingRunner()
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_hot_start_then_sustained_cooling_releases_auto(self):
        self.assertTrue(cool_down.cool_down(self.runner))
        self.assertTrue(self.runner.requested_max)
        self.assertGreaterEqual(self.runner.clock - 1000, 34)
        self.assertFalse(self.runner.active)
        self.assertEqual(self.runner.files[validate_backend.WRITE_SUPPORT], 'N')

    def test_timeout_restores_auto(self):
        self.runner.temperatures = [92]
        with patch.object(cool_down, 'MAX_SECONDS', 6):
            self.assertFalse(cool_down.cool_down(self.runner))
        self.assertFalse(self.runner.active)

    def test_stopped_fan_aborts_and_releases(self):
        self.runner.status['fans']['fan1']['rpm'] = 0
        with self.assertRaisesRegex(RuntimeError, 'tachometer'):
            cool_down.cool_down(self.runner)
        self.assertFalse(self.runner.active)

    def test_failed_release_keeps_stop_timer_armed(self):
        self.runner.fail_stop = True
        with self.assertRaises(subprocess.CalledProcessError):
            cool_down.cool_down(self.runner)
        self.assertNotIn(['systemctl', 'stop', f'{cool_down.TIMER}.timer'], self.runner.commands)

    def test_running_service_is_preserved_and_refused(self):
        self.runner.active = True
        with self.assertRaisesRegex(RuntimeError, 'cleanly stopped'):
            cool_down.cool_down(self.runner)
        self.assertTrue(self.runner.active)
        self.assertFalse(self.runner.requested_max)


if __name__ == '__main__':
    unittest.main()
