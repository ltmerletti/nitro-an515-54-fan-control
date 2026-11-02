# Maximum-only NBFC trial

The exact AN515-54 profile loaded successfully in the 45-second read-only trial.
A first 30-second maximum-speed trial on BIOS V1.33 passed on November 1 at
19:27 UTC. Both tachometers reached 6122 RPM, then fell after the saved register
values were restored and verified. Reduced-speed control is still unverified.
The helper requests 100% on both fans for 30 seconds by default, with an explicit
`--duration` accepting 1 through 180 seconds. It does not test lower speeds, run a stress workload,
install a service, or change boot configuration.

## What runs

A pinned nbfc-linux 0.5.4 build supplies the controller. Its EC backend is patched
to pass byte and word writes through one checked C writer. Alternative hardware
backends are disabled. Automatic module loading is removed. The Python supervisor
uses the same C guard implementation for restoration.

The active JSON policy lists every writable byte, including the high-byte neighbors
of word targets. It requires the observed baseline before any control write.
Register 0x03 is monitored and always forbidden for writes. Active target values
permit 100% only. Restoration has a separate internal path that permits only actual
captured baseline values. Descriptive labels do not establish register semantics.

The guard parses a bounded ASCII subset of JSON. It rejects unknown/duplicate keys,
missing sections, trailing content, invalid values, missing baselines and incomplete
restoration order. Both bytes of a word are checked before the write. A hardware
write itself can still fail or complete only partially.

The supervisor checks the binary, shared library, patch, guard sources, policy and
profile against the build manifest before hardware access. Both fans use the exact
CPU Package id 0 temperature file for this maximum-only test. Independent GPU
control and physical CPU/GPU fan assignments remain unverified.

The build uses private configuration, PID, socket and state paths under
`/home/luke/nbfc-evaluation/reviewed-runtime`. Host `/etc/nbfc` and `/var/lib/nbfc`
state are not used. Existing private state is backed up and restored, including
when it contains a lower saved speed.

## Cleanup

The Python process supervises a foreground NBFC child in a separate process group.
Signals request controlled cleanup. Child death, missing sensors and invalid
readings fail the trial. High valid temperatures already receive the maximum
speed request. No artificial workload is added.

Cleanup stops the child group before restoring saved bytes, independent of which
process made the writes. Configured handoff registers are restored first. If their
values cannot be verified, the guard leaves maximum targets in place rather than
writing zero targets while manual mode might remain active. Restoration attempts
and failures are logged; failures return a nonzero exit code.

The shell helper checks the baseline with kernel writes still disabled, enables
writes explicitly for this trial, then reloads ec_sys with write support disabled
again. It refuses to overlap the separate Acer Auto baseline recording.

Restoring bytes is not proof that the firmware resumed its normal curve. RPM and
sound observations are still needed. Upstream reset values and observed baseline
values have not yet been validated as reliable firmware handoff on this BIOS.
Neither the supervisor nor the shell can clean up after their own SIGKILL, a
kernel failure, or loss of power. Do not use kill -9 on the supervisor.

## Verification

Local hardware-free checks:

```sh
make -C tools/ec_guard test
python3.12 -m unittest discover -s tests -q
```

At preparation time, the local suite ran 353 tests, with five Linux integration tests skipped unless
explicitly enabled. The seven C test groups also pass.

On basement-server, five additional tests run the actual patched upstream child
against a regular 256-byte file. They verify a bounded maximum run, stale-state
isolation, child SIGKILL cleanup, supervisor SIGTERM cleanup, missing-temperature
cleanup, and a mismatched baseline causing zero writes. All five passed with
kernel EC write support still N.

```sh
cd /home/luke/nbfc-evaluation/reviewed
NITROFAN_GUARDED_MANIFEST=/home/luke/nbfc-evaluation/profile-tests-runtime/manifest.json \
  python3 -m unittest tests.test_nbfc_upstream_integration -v
```

The dedicated user-owned test build avoids modifying root-owned state from a
real hardware trial. See [profile verification](profiles.md#verification) for
its build command and the latest test results.

## Rebuild

The source checkout must point to upstream commit
`9da010d6a2e8351bdf6900d6c3643f1e742af936`. The build helper archives committed
files into a temporary build directory, preserving the original checkout.
The compiler and extracted Lua headers/libraries are already staged on the server.
No root privileges or package installation are needed for building.

```sh
python3 /home/luke/nbfc-evaluation/reviewed/tools/ec_guard/build_guarded_nbfc.py \
  --source-dir /home/luke/nbfc-evaluation/nbfc-linux-0.5.4 \
  --output-dir /home/luke/nbfc-evaluation/reviewed-runtime/bin \
  --ec-guard-dir /home/luke/nbfc-evaluation/reviewed/tools/ec_guard \
  --private-root /home/luke/nbfc-evaluation/reviewed-runtime
```

## Human-run hardware trial

Wait for the separate 30-minute Acer Auto recording to finish. Then on the server:

```sh
sudo bash /home/luke/nbfc-evaluation/reviewed/tools/run_nbfc_maximum_trial.sh > ~/nbfc-maximum.log 2>&1
```

Authentication is necessary to read the EC baseline, reload ec_sys and perform the
allowlisted test writes. The script lasts about 35 seconds after preflight.
It includes the write audit and temperature/RPM samples in the user-owned log.
Listen for both fans increasing, then settling after the test ends. A successful
exit proves only that the bounded process and byte restoration completed. Review
RPM behavior before declaring control or firmware handoff verified.

Phase 0 ultimately still requires maximum, 30% and firmware-auto behavior to work
and hold for five minutes each. This preliminary trial does not complete Phase 0.

## Three-minute maximum and noise comparison

The optional microphone comparison adds 30 seconds of firmware-controlled baseline
and 60 seconds of recovery after the guarded test and EC write protection finish.
For three minutes at maximum, run the deployed helper on basement-server:

```sh
sudo bash /home/luke/nbfc-evaluation/reviewed/tools/run_nbfc_maximum_trial.sh --duration 180 --mic > ~/nbfc-maximum-3min.log 2>&1
```

Allow about four minutes and 35 seconds after preflight. The log contains phase
timestamps, unchanged ALSA mixer settings, the EC audit, watchdog telemetry, and
CSV observations of temperatures, RPM and microphone RMS dBFS every four seconds.
The watchdog still checks temperature and RPM every second during maximum operation.
The observer runs in a separate process from fan supervision. Silent, clipped or
failed microphone preflight prevents the test. An observer process failure or
an interrupt stops the fan wrapper and waits for its restoration before stopping
audio capture. Later microphone sample failures leave blanks in the CSV.

The default microphone is `plughw:CARD=PCH,DEV=0`; `--mic-device` overrides it.
Raw audio stays in memory and is discarded. Only loudness measurements are saved.
Each capture discards its first two seconds, then measures one second. A passive
six-second check found a large startup transient in the first second, so including
that audio would obscure the actual fan noise.
These are relative dBFS readings, not calibrated sound-pressure decibels. ALSA gain
and microphone boost are recorded but not changed. This does not establish that
driver or firmware automatic gain control is disabled.

Keep microphone position, gain, surrounding noise and workload consistent.
No stress workload is started. This run can show sustained fan behavior and
temperature changes, but changing CPU/GPU activity can obscure the cooling effect.
Exact maximum timing comes from the watchdog's `active_trial` samples; wrapper
markers include preflight and the five-second restoration observation period.

Preparation was verified with 353 local tests (five opt-in Linux tests skipped)
and nine targeted tests on basement-server. Two live read-only samples returned
temperature, tachometer and non-clipping microphone values. The build manifest
still matches the existing guarded binary and policy. EC write support was N
after preparation. The subsequent hardware result is recorded below.

## First hardware result

The user ran the helper after the passive baseline had finished. The log records
36 permitted upstream writes and five baseline restoration writes. Every byte
touched was allowlisted; register 0x03 was never written. Both fans reached
6122 RPM by the second sample interval. About four seconds after release, the
tachometers read 2857 and 2419 RPM. NBFC was no longer running and kernel write
support was N when checked over SSH.

The CPU ranged from 47 to 93 C during maximum operation. This is evidence of
working fan control, not a controlled demonstration of better cooling.
Native warnings about missing zero-speed thresholds are expected for this
maximum-only profile. No denied writes, I/O errors, or tracebacks were present.

[Raw log](data/nbfc/maximum-2026-11-01.log) and
[analysis](data/nbfc/maximum-2026-11-01-analysis.json).

## Three-minute result

The second test completed on November 1, 2026. The maximum request ran from
19:38:27 to 19:41:27 UTC. Both tachometers reached 6122 RPM by the two-second
sample and remained there through the last of 181 watchdog samples. The observer
recorded 69 temperature/RPM/microphone samples, with no missing or clipped audio.

| Measurement | Firmware before (30s) | Maximum (180s) | Firmware recovery (60s) |
| --- | ---: | ---: | ---: |
| Mean CPU package temperature | 63.1 C | 55.2 C | 64.0 C |
| Mean GPU temperature | 44.0 C | 39.4 C | 38.9 C |
| Median fan1 RPM | 2400 | 6122 | 3571 |
| Median fan2 RPM | 2255 | 6122 | 3260 |
| Median microphone level | -68.28 dBFS | -49.77 dBFS | -64.00 dBFS |

The median microphone increase was 18.52 dB. This is a difference in the recorded
signal, not a calibrated room SPL measurement or a perceived-loudness multiplier.
Noise statistics exclude capture windows crossing a phase boundary. The baseline
contains eight sensor samples and seven wholly contained mic windows; maximum
contains 45 sensor samples and 44 wholly contained mic windows. Audio windows
occur after two seconds of warmup; timing is approximate because sensor reads and
GPU queries precede capture.

CPU temperature averaged 48.9 C in the final minute at maximum, and GPU temperature
averaged 37.1 C, with a minimum of 36 C. CPU temperature still reached 92 C during
the complete test. The workload and CPU/GPU power were not held constant or logged.
This demonstrates sustained control and a noise change; it does not establish a
repeatable cooling improvement of a particular size.

The audit contains 136 permitted upstream writes and five restoration writes.
Every changed byte matches the configured allowlist and permitted active values
or exact saved baseline. Register 0x03 was untouched. Saved bytes were verified;
both fans read 2542 RPM four seconds after release. They varied between roughly
2700 and 4000 RPM over the subsequent minute. NBFC was absent and kernel EC write
support was N when independently checked over SSH. Reduced-speed control and
five-minute reliability checks remain outstanding.

[Interactive chart](nbfc-maximum-3min-2026-11-01.html),
[analysis](data/nbfc/maximum-3min-2026-11-01-analysis.json),
[observer CSV](data/nbfc/maximum-3min-2026-11-01.csv), and
[complete log](data/nbfc/maximum-3min-2026-11-01.log).
