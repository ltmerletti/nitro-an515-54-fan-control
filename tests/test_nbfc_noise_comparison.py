"""Hardware-free regressions for longer trials and independent audio recording."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_nbfc_guarded_trial import create_synthetic_ec_file, create_synthetic_sysfs_sensors
from tools.nbfc_guarded_trial import GuardedTrialSupervisor
from tools import nbfc_noise_comparison as comparison
from tools import monitor


class TestLongerTrial(unittest.TestCase):
    def test_three_minutes_accepted_with_unchanged_default(self):
        self.assertEqual(GuardedTrialSupervisor().duration_s, 30)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_synthetic_ec_file(root / "fake_ec.bin")
            create_synthetic_sysfs_sensors(root / "sys")
            supervisor = GuardedTrialSupervisor(duration_s=180, synthetic_root=root)
            try:
                supervisor.preflight_checks()
            finally:
                supervisor.cleanup()

    def test_duration_rejected_before_any_preflight_work(self):
        for duration in (0, -1, 181, True, 1.5):
            with self.subTest(duration=duration), patch("os.geteuid") as identity:
                with self.assertRaises(ValueError):
                    GuardedTrialSupervisor(duration_s=duration).preflight_checks()
                identity.assert_not_called()

    def test_shell_rejects_bad_duration_before_sudo_or_module_access(self):
        script = Path(__file__).resolve().parents[1] / "tools/run_nbfc_maximum_trial.sh"
        for duration in ("0", "181", "-1", "1.5", "030", "9999999999999999999999"):
            result = subprocess.run(["bash", str(script), "--duration", duration],
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 2)
            self.assertIn("from 1 to 180", result.stderr)


class TestNoiseComparison(unittest.TestCase):
    def test_microphone_warmup_is_excluded_from_measurement(self):
        warmup = b"\xff\x7f" * 32000
        steady = b"\x10\x00" * 16000
        completed = subprocess.CompletedProcess([], 0, stdout=warmup + steady, stderr=b"")
        with patch.object(monitor.subprocess, "run", return_value=completed) as capture:
            self.assertEqual(monitor.capture_mic("fixture", warmup_s=2), steady)
        self.assertIn("48000", capture.call_args.args[0])
        self.assertEqual(capture.call_args.kwargs["timeout"], 6)

    def test_bad_microphone_stops_before_any_child_starts(self):
        for audio in (None, b"", b"\0" * 32000, b"\xff\x7f" * 16000):
            with self.subTest(audio_length=None if audio is None else len(audio)), \
                    patch.object(comparison, "capture_mic", return_value=audio), \
                    patch.object(comparison.subprocess, "Popen") as launch:
                with self.assertRaises(RuntimeError):
                    comparison.NoiseComparison(180, "fixture").run(Path("/unused"))
                launch.assert_not_called()

    @patch.object(comparison, "check_microphone")
    @patch.object(comparison.subprocess, "run")
    @patch.object(comparison.subprocess, "Popen")
    def test_phase_order_duration_and_separate_processes(self, launch, gain, _mic):
        observer, trial = Mock(), Mock()
        trial.poll.return_value = 0
        trial.returncode = 0
        launch.side_effect = [observer, trial]
        gain.return_value.stdout = "capture settings"
        instance = comparison.NoiseComparison(180, "fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "observations.csv").write_text("mic_dbfs\n-31.2\n")
            with patch.object(instance, "wait_recording") as wait, \
                    patch.object(instance, "stop_recorder"), \
                    patch.object(comparison, "marker") as marker, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(instance.run(root), 0)
            self.assertEqual([call.args[0] for call in wait.call_args_list], [30, 60])
            self.assertEqual([call.args[0] for call in marker.call_args_list],
                             ["firmware_baseline_start", "maximum_wrapper_start",
                              "firmware_recovery_start", "comparison_complete"])
        self.assertIn("180", launch.call_args_list[1].args[0])
        for call in launch.call_args_list:
            self.assertTrue(call.kwargs["start_new_session"])

    @patch.object(comparison, "check_microphone")
    @patch.object(comparison.subprocess, "run")
    @patch.object(comparison.subprocess, "Popen")
    def test_recorder_failure_stops_trial_before_stopping_audio(self, launch, _gain, _mic):
        observer, trial = Mock(), Mock()
        trial.poll.return_value = None
        launch.side_effect = [observer, trial]
        instance = comparison.NoiseComparison(180, "fixture")
        order = []
        trial.terminate.side_effect = lambda: order.append("trial terminate")
        trial.wait.side_effect = lambda: order.append("trial reaped")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "observations.csv").write_text("mic_dbfs\n-31.2\n")
            with patch.object(instance, "wait_recording", side_effect=[None, RuntimeError("audio")]), \
                    patch.object(instance, "stop_recorder", side_effect=lambda: order.append("audio stop")), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "audio"):
                    instance.run(root)
        self.assertEqual(order, ["trial terminate", "trial reaped", "audio stop"])

    def test_interrupt_prevents_further_observation_waits(self):
        instance = comparison.NoiseComparison(180, "fixture")
        instance.request_stop(signal.SIGTERM, None)
        with self.assertRaises(InterruptedError):
            instance.wait_recording(30)
        self.assertEqual(instance.signal_number, signal.SIGTERM)

    def test_recorder_cleanup_targets_its_group_only(self):
        instance = comparison.NoiseComparison(180, "fixture")
        instance.recorder = Mock(pid=12345)
        instance.trial = Mock(pid=98765)
        with patch.object(comparison.os, "killpg") as kill:
            instance.stop_recorder()
        kill.assert_called_once_with(12345, signal.SIGTERM)
        instance.trial.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
