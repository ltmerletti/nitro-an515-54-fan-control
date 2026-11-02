#!/usr/bin/env python3
"""Record relative mic loudness around a bounded, guarded NBFC maximum trial."""

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

try:
    from .monitor import capture_mic, rms_dbfs
except ImportError:
    from monitor import capture_mic, rms_dbfs


BASELINE_SECONDS = 30
RECOVERY_SECONDS = 60
SAMPLE_INTERVAL = 4
MIC_WARMUP_SECONDS = 2
TOOLS_DIRECTORY = Path(__file__).resolve().parent


def marker(phase):
    print(json.dumps({"event": phase,
                      "timestamp": datetime.now(timezone.utc).isoformat(),
                      "monotonic_s": time.monotonic()}), flush=True)


def check_microphone(device):
    audio = capture_mic(device, warmup_s=MIC_WARMUP_SECONDS)
    if audio is None or len(audio) != 32000:
        raise RuntimeError("Microphone capture failed; no fan test was started")
    loudness, clipping = rms_dbfs(audio)
    if loudness is None or clipping:
        raise RuntimeError("Microphone is silent or clipping; no fan test was started")
    print(json.dumps({"microphone": device, "preflight_dbfs": loudness,
                      "units": "dBFS, relative and uncalibrated",
                      "discarded_startup_s": MIC_WARMUP_SECONDS,
                      "audio_saved": False}), flush=True)


def audio_samples_available(path):
    with path.open(newline="") as source:
        return any(row.get("mic_dbfs") for row in csv.DictReader(source))


class NoiseComparison:
    def __init__(self, duration, device):
        self.duration = duration
        self.device = device
        self.stop_requested = False
        self.signal_number = 0
        self.recorder = None
        self.trial = None

    def request_stop(self, signum, _frame):
        self.signal_number = signum
        self.stop_requested = True

    def wait_recording(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stop_requested:
                raise InterruptedError("Comparison interrupted")
            if self.recorder.poll() is not None:
                raise RuntimeError("Observation process stopped unexpectedly")
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))

    def stop_trial(self):
        if self.trial is not None and self.trial.poll() is None:
            # Signal the shell only. Its trap waits for supervised EC restoration.
            self.trial.terminate()
            self.trial.wait()

    def stop_recorder(self):
        if self.recorder is not None:
            try:
                # This process group contains only the observer and arecord.
                os.killpg(self.recorder.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.recorder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.recorder.pid, signal.SIGKILL)
                self.recorder.wait(timeout=2)

    def run(self, directory):
        check_microphone(self.device)
        gain = subprocess.run(["amixer", "-c", "PCH", "scontents"],
                              capture_output=True, text=True, timeout=5)
        print("=== ALSA settings (unchanged) ===", flush=True)
        print(gain.stdout or gain.stderr, flush=True)
        if self.stop_requested:
            raise InterruptedError("Comparison interrupted during microphone preflight")
        recording = directory / "observations.csv"
        result = 1
        try:
            self.recorder = subprocess.Popen(
                [sys.executable, str(TOOLS_DIRECTORY / "monitor.py"),
                 "--mic", "--mic-device", self.device,
                 "--mic-warmup", str(MIC_WARMUP_SECONDS),
                 "--interval", str(SAMPLE_INTERVAL), "--csv", str(recording)],
                stdin=subprocess.DEVNULL, start_new_session=True)
            marker("firmware_baseline_start")
            self.wait_recording(BASELINE_SECONDS)
            if not audio_samples_available(recording):
                raise RuntimeError("No microphone samples in baseline; refusing fan writes")
            if self.stop_requested:
                raise InterruptedError("Comparison interrupted before fan trial")
            marker("maximum_wrapper_start")
            self.trial = subprocess.Popen(
                ["bash", str(TOOLS_DIRECTORY / "run_nbfc_maximum_trial.sh"),
                 "--duration", str(self.duration)],
                stdin=subprocess.DEVNULL, start_new_session=True)
            while self.trial.poll() is None:
                self.wait_recording(0.2)
            if self.trial.returncode:
                raise RuntimeError(f"Guarded fan trial failed with exit {self.trial.returncode}")
            marker("firmware_recovery_start")
            self.wait_recording(RECOVERY_SECONDS)
            marker("comparison_complete")
            result = 0
        finally:
            # Fan handoff and write protection finish before the audio process stops.
            self.stop_trial()
            self.stop_recorder()
            if recording.exists():
                print("=== Temperature, RPM and microphone observations (CSV) ===", flush=True)
                print(recording.read_text(), flush=True)
        return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=180)
    parser.add_argument("--mic-device", default="plughw:CARD=PCH,DEV=0")
    options = parser.parse_args(argv)
    if not 1 <= options.duration <= 180:
        parser.error("--duration must be from 1 to 180 seconds")
    return options


def main():
    options = parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run this comparison with sudo via run_nbfc_maximum_trial.sh --mic")
    comparison = NoiseComparison(options.duration, options.mic_device)
    previous = {signum: signal.signal(signum, comparison.request_stop)
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    result = 1
    try:
        with open("/run/lock/nitrofan_noise_comparison.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with tempfile.TemporaryDirectory(prefix="nitrofan-noise-") as directory:
                result = comparison.run(Path(directory))
    except Exception as error:
        print(f"Comparison stopped: {error}", file=sys.stderr, flush=True)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 128 + comparison.signal_number if comparison.signal_number else result


if __name__ == "__main__":
    raise SystemExit(main())
