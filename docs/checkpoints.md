# NitroFan checkpoints

## Discovery facts (November 1, 2026)

- **Machine**: Acer Nitro AN515-54, BIOS V1.33
- **OS**: Ubuntu 26.04.1, kernel 7.0.0-42-generic, Python 3.14.4
- **Project target**: Python 3.12+ (stdlib only)
- **Fan telemetry**: `acer_wmi` with `predator_v4=1` exposes hwmon device `acer`
  with `fan1_input`, `fan2_input` (RPM) and 3 unlabeled temperatures. No `pwm*` files.
  Fan1/fan2 CPU/GPU assignments unverified.
- **GPU**: Nvidia driver 595.99.02 running. GPU temp via `nvidia-smi`.
- **Microphone**: ALC255 analog (PCH), unprivileged capture working.
  Independent read-only logger verified (RPM/temps/noise every 5 sec).
- **EC status**: `ec_sys` loaded with `write_support=0` (parameter verified `N`)
  by the user. `/dev/ec` absent. The first complete EC dump is saved in
  [ec-baseline-2026-11-01.hex](ec-baseline-2026-11-01.hex). Sibling candidate
  control meanings remain partly unverified. A combined NBFC maximum-speed sequence
  has now worked; see the first writable trial below. Register 0x03 was not written.
- **Phase 0 control verification**: INCOMPLETE. Thirty-second and three-minute 100% tests worked and
  the fans slowed after saved-byte restoration. Reduced speeds and five-minute
  maximum/30%/firmware-auto checks remain outstanding.
- **Direction pivot (Upstream NBFC evaluation)**: To prioritize established, tested
  software and avoid bespoke control code, the project pivoted to evaluating upstream
  `nbfc-linux` 0.5.4 in read-only mode. Custom EC backend development was canceled,
  preserving existing telemetry, datasets, and mock architecture. See [nbfc-evaluation.md](nbfc-evaluation.md).

## Candidate EC register map (sibling, unverified)

| Purpose          | Register | Value/Notes        |
|------------------|----------|--------------------|
| Unlock           | 0x03     | Write 0x11         |
| CPU mode         | 0x22     | Auto 0x04, Manual 0x0C |
| GPU mode         | 0x21     | Auto 0x10, Manual 0x30 |
| CPU speed        | 0x37     | 0..100 percent     |
| GPU speed        | 0x3A     | 0..100 percent     |
| Sibling CPU readback claim | 0x13 | Percent claim superseded by measured fan1 RPM pair below |
| Sibling GPU readback claim | 0x15 | Percent claim superseded by measured fan2 RPM pair below |
| Write allowlist  | 0x03, 0x21, 0x22, 0x37, 0x3A |  |

Battery-bit behavior of the unlock register is unverified. No guessed masks
or arithmetic have been applied.

## Milestone 1 (complete)

Config loading (`nitrofan/config.py`), fan curve logic (`nitrofan/curve.py`),
and IPC protocol framing (`nitrofan/protocol.py`) with unit tests.

- TOML config via `tomllib`, validated with allowlist checks
- Linear interpolation with 4C hysteresis and 10pp/tick rate limiting
- JSON-lines protocol with strict validation, size limits, NaN rejection
- 151 total tests passing (130 nitrofan + 21 existing monitor tests)

## Milestone 2 (complete)

Mock daemon, backend interface, safety controller, and Unix domain socket IPC.
All substantive Checkpoint 2 review findings resolved and verified:

- Abstract `Backend` interface (`nitrofan/backends/base.py`)
- In-memory `MockBackend`, deterministic `MockSensorProvider`, and `SimulatedSensorProvider` (`nitrofan/backends/mock.py`) wiring thermal dynamics into periodic sampling
- Hardened single-instance advisory lock with `O_NOFOLLOW` and regular file checks, plus safe stale socket cleanup with bounded active listener checks (`nitrofan/lock.py`)
- Deterministic `Controller` with simulated safety checks (`nitrofan/controller.py`):
  - Emergency 100% fan speed on high temperatures with 10C hysteresis latch tracking all triggering sources
  - Deduplicated emergency writes respecting reassert interval
  - Sensor fault latching (3 consecutive missing/invalid CPU temperature readings switch requested_mode to auto)
  - Manual mode requests rejected while CPU sensor fault is active
  - Temperature sanitization normalizing NaN/Inf/bool to None
  - Safe handling of missing GPU temperatures (min_percent floor, no invented zero)
  - Speed floor enforcement across manual modes (fixed 0% rejected)
  - Canonical `percent` int or mapping format with legacy `percents` alias and contradictory payload rejection
  - Write deduplication and periodic reassertion with cache invalidation on `_fail_auto`
  - Separate monotonic and wall clocks; wall clock gap detection (> 3*tick_s) triggers safe backend close/open and mode reapply
  - Separation of periodic sampling from immediate command application (`apply_command`), preventing client command spam from skipping curve rate limits
  - Transitions from fixed/max to curves preserve applied speeds and lower once per tick
  - Centralized fail-safe fallback to auto mode on backend or sensor errors
- Unix domain socket server (`nitrofan/server.py`) with permissions 0660, bounded output buffers with `EVENT_WRITE`, per-frame size checks, pipelined/fragmented request support, and owned bound inode tracking on cleanup
- Daemon entry point (`nitrofan/daemon.py`) installing signal handlers before backend open, respecting startup stop, and enforcing explicit backend precedence
- Unsupported backends (ec, hwmon) rejected prior to socket creation or hardware access
- Note: `save` command remains unimplemented at Checkpoint 2 (persistence belongs to later milestone)
- 228 total tests passing (207 nitrofan unit/integration tests + 21 monitor tests)
- Verified on Python 3.12.16. A separate live mock-daemon smoke check exercised
  per-fan fixed requests, rejected zero, curve updates, max mode, subscriptions,
  periodic cooling, the save error reply, and SIGINT cleanup.
- Final review also corrected command failure replies, partial telemetry failure
  snapshots, curve response size limits, and repeat cleanup of file descriptors.

## Milestone 3: Real read-only telemetry (complete)

Real read-only telemetry integration for Linux sysfs hwmon and NVIDIA GPU.
No fan speed control is implemented or validated; Phase 0 hardware control remains
incomplete and candidate EC registers remain unverified.

Distinction from original EC-readback milestone:
This milestone reads live tachometer RPM (`fan1_input`, `fan2_input`) from the
`acer_wmi` driver (`predator_v4=1`) and thermal sensors (`coretemp`, `nvidia-smi`,
`acer_wmi`), rather than direct EC register access. It exposes zero PWM control
and performs zero sysfs or hardware writes.

Key components and safeguards:
- **Temperature provider (`nitrofan/sensors.py`)**:
  - Non-recursive sysfs scan for `coretemp` canonical CPU package temperature (`Package id 0` or `temp1_input`)
  - Targeted, awake-only NVIDIA GPU temperature via bounded `nvidia-smi --id=<PCI>` query only when PCI display function `power/runtime_status` is `active` (audio function `.1` ignored; suspended GPU never woken)
  - Neutral Acer temperatures (`acer_temp1`, `acer_temp2`, `acer_temp3`) from `acer_wmi` for thermal correlation
  - Any read or sensor failure returns `None`, never fake zeros
  - Dynamic path rediscovery on driver re-enumeration, removal, or resume
- **Read-only hwmon backend (`nitrofan/backends/hwmon.py`)**:
  - Discovers hwmon device named `acer` and fan inputs `fan1`/`fan2`
  - `read_rpm` returns nonnegative integer RPM or `None`
  - `read_percent` returns `None` (RPM is tachometer frequency, not duty cycle percent)
  - Explicitly rejects `set_percent` with `PermissionError`
  - Idempotent `open` and `close`; `set_auto` is a no-op; strictly zero hardware writes across entire lifecycle
- **Read-only controller safeguards (`nitrofan/controller.py`)**:
  - Exposes `read_only=true` and `can_control=false` in status alongside actual backend name (`backend="hwmon"`)
  - Atomically rejects manual mode requests (`fixed`, `max`, `curve`) and curve edits before state mutation
  - Forced manual `startup_mode` in config is rejected at startup
  - High-temperature emergency condition sets `emergency=true` in status while retaining firmware control (`mode="auto"`, reported percent `None`)
  - Emergency clear hysteresis tracks actual triggering thermal sources without permanently latching on fan tachometer keys (`fan1`/`fan2`)
  - Fail-safe fallback (`_fail_auto`) and wall clock resume perform zero hardware writes
- **Daemon CLI flags (`nitrofan/daemon.py`)**:
  - Supports `--backend hwmon --read-only` and `--sysfs-root PATH`
  - Refuses `hwmon` backend without `--read-only`
  - Refuses `ec` backend (always unsupported)
- 267 total tests passing (228 original + 39 new tests covering discovery, rediscovery, NVIDIA gating, zero writes, and fake sysfs live daemon integration)
- Review fixes verify device identity when a hwmon number is reused, handle
  oversized temperature values, and keep read-only emergency logs accurate.

### Verification on basement-server

On November 1, 2026 at 17:26 UTC, the read-only daemon was run as `luke`
against real sensors. All 267 tests also passed on the server's Python 3.14.4;
the same suite passed locally on Python 3.12.16.

- CPU package temperature, GPU temperature (45 C, GPU already active), both
  tachometers, and neutral Acer temperatures appeared in socket status.
- Subscription updates arrived every 2 seconds. Manual mode requests for
  max, fixed, and curve were rejected.
- The CPU briefly reached 93 C. Status reported `emergency=true`, `mode=auto`,
  `read_only=true`, and `can_control=false`; firmware retained control.
  A subsequent direct reading was 53 C with fans at 2479 and 2343 RPM.
- SIGTERM exited with status 0 and removed the test socket. The daemon was
  stopped after verification; no service or boot configuration was installed.
- GPU suspend behavior, emergency hysteresis, sensor loss, and resume were
  exercised with synthetic inputs. Actual GPU suspend and system suspend
  were not induced on the server.

The test copy is `/home/luke/nitrofan-readonly.LfnOz4`. The separate microphone
logger and its recordings were left intact. Detailed socket results are in
[hardware-telemetry-2026-11-01.json](hardware-telemetry-2026-11-01.json).

## Phase 0 EC discovery collector (collector implemented; hardware evidence pending)

Read-only EC discovery infrastructure implemented to collect raw EC
registers, real coretemp/NVIDIA GPU temperatures, and neutral `acer_wmi` tachometer RPM.
Phase 0 control verification remains **incomplete**; candidate register interpretations
remain unverified. Strictly zero hardware writes are performed.

Components:
- **`nitrofan/ec.py` (`ECReader`)**:
  - Opens `/dev/ec` or `/sys/kernel/debug/ec/ec0/io` (or an explicit path for synthetic testing)
  - Uses `os.open` with `O_RDONLY` and `O_CLOEXEC`
  - Atomic single-byte reads via `os.pread` at validated offsets `0..255`
  - Idempotent `open` and `close`; context manager support ensuring no leaked file descriptors
  - Rejects short reads and invalid offsets; never invents default or zero values
- **`tools/ec_discover.py`**:
  - Standalone executable runnable without `pip install` or manual `PYTHONPATH` manipulation
  - Gathers candidate register offsets (`unlock_register`, `mode_register`, `speed_register`,
    `read_register`) directly from `examples/config.toml` (or `--config`); no hardcoded sibling constants
  - Supports `--full-dump` to read all 256 bytes (0x00..0xFF) for byte-differential baselines
  - Correlates raw EC bytes with real temperatures from `SystemSensorProvider` and RPM from `HwmonBackend`
  - Emits JSON lines to stdout, flushed per sample; diagnostics and warnings go to stderr
  - Missing telemetry is reported as `None` with diagnostic explanations, never fake zero
  - Fatal exit on EC read errors (no partial snapshot emitted as valid evidence)
  - Options: `--samples` (default 1), `--interval` (default 2.0s), `--label` (`idle`, `load`, `return-idle`),
    `--ec-path`, `--config`, `--full-dump`, `--sysfs-root`
  - Handled cleanly on Ctrl+C (exit code 130)
- **Documentation (`docs/phase0-ec-discovery.md`)**:
  - Detailed recipe for safe `ec_sys` loading (`write_support=0`, verify `N`)
  - Human-run multi-phase observation workflow (idle, external load, return-idle)
  - Unprivileged stdout shell redirection avoiding root-owned output files
- 293 total unit tests passing (267 previous + 26 new tests in `tests/test_ec.py` and `tests/test_ec_discover.py`).
- Samples include their acquisition start time and `sample_duration_s`.
  RPM is read immediately after EC bytes, before querying GPU temperature.
  These are sequential observations, not an atomic hardware snapshot.
- Initial human-run read-only EC dump received. Raw bytes at `0x13..0x16`
  suggest two 16-bit little-endian values, 2521 and 2400. Their units and fan
  assignments remain hypotheses pending paired EC/RPM captures. See
  [ec-baseline-2026-11-01.md](ec-baseline-2026-11-01.md).
- Deployed to `/home/luke/nitrofan-phase0` on basement-server. All 293 tests
  passed both locally on Python 3.12.16 and on the server's Python 3.14.4.
  Direct script execution from the home directory was also checked using
  a saved EC fixture and live sensors. This fixture check is not a new EC
  hardware reading. The paired capture below was subsequently obtained using
  authenticated sudo.

### First paired capture received

The user completed a full read-only capture, collected on November 1, 2026
from 17:42:48 to 17:43:52 UTC. All 30 samples contain 256 register bytes and
paired temperature/tachometer telemetry.

- `0x13` low byte and `0x14` high byte track driver `fan1_input` as RPM.
  The decoded value matches exactly in 27/30 samples, with mean absolute
  difference 4.37 RPM and maximum difference 53 RPM.
- `0x15` low byte and `0x16` high byte track driver `fan2_input` as RPM.
  The decoded value matches exactly in 29/30 samples, with mean absolute
  difference 0.83 RPM and maximum difference 25 RPM.
- These are little-endian 16-bit RPM readbacks on this unit, rather than
  the sibling map's proposed single-byte percentages. CPU/GPU fan assignment
  remains unverified.
- Register `0x03` remained `0x21`. Candidate mode/target registers `0x21`,
  `0x22`, `0x37`, and `0x3a` remained zero. Control semantics are unverified.
- CPU temperature reached 95 C under normal server activity. No artificial
  workload was added, and further stress testing is deferred for now.

See the [capture analysis](ec-passive-baseline-2026-11-01.md),
[raw data](data/ec-passive-baseline-2026-11-01.jsonl), and
[interactive plot](ec-passive-baseline-2026-11-01.html).

## Revised build order

Build order prioritizes evaluating established upstream software and operational telemetry:

1. **Milestone 1 (complete)**: Config parsing, curve interpolation, protocol framing.
2. **Milestone 2 (complete)**: Mock daemon, controller safety architecture, socket IPC.
3. **Milestone 3 (complete)**: Real read-only telemetry (hwmon tachometer RPM, coretemp, awake-only NVIDIA GPU).
4. **Phase 0 EC discovery (complete)**:
   Collector implemented. Paired 16-bit word RPM readbacks verified at `0x13`/`0x14` and `0x15`/`0x16`.
5. **Upstream NBFC read-only evaluation (complete; PASSED on basement-server)**:
   Executed bounded read-only trial on November 1, 2026 (45s real EC polling). Verified 16-bit word
   RPM reads, zero write operations, clean socket status reporting, and clean SIGTERM exit.
   Kernel write_support remained 'N'. Confirmed critical operational limits: stock profile averages
   coretemp CPU sources rather than Package id 0, stock curves request 0% speed at low temperatures,
   CurrentSpeed normalizes against an assumed 6122 RPM rather than PWM duty cycle, and native service
   reads and rewrites global state at `/var/lib/nbfc/state.json`.
6. **Guarded maximum-only trial preparation (reviewed; synthetic verification passed)**:
   Patched upstream 0.5.4 build with a single checked writer, maximum-only active policy,
   separate saved-baseline restoration, private runtime state, and manifest verification.
   Local suite: 344 tests, including five opt-in Linux integration tests skipped locally.
   Seven C test groups pass. All five opt-in tests also pass on basement-server using the
   real patched upstream child and a fake 256-byte EC file. No real EC writes occurred.
   See [guarded-maximum-trial.md](guarded-maximum-trial.md).
7. **First writable maximum trial (passed; November 1, 19:27 UTC)**:
   Both tachometers rose from 2400/2272 RPM to 6122 RPM within about two seconds,
   stayed there through the 30-second request, then slowed after saved bytes were
   restored and verified. All write calls matched the active policy. Register 0x03
   was untouched. NBFC stopped and ec_sys returned to write_support=N.
   CPU still reached 93 C at full fan speed. Physical fan assignments, reduced
   speeds and five-minute stability/handoff checks remain unverified. See
   [the evidence](data/nbfc/maximum-2026-11-01-analysis.json).
8. **Initial multi-model profile catalogue (complete)**:
   Added configuration-driven profile catalogue (`profiles/catalog.json`, `profiles/schema.json`)
   and exact DMI identification (`nitrofan/models.py`). Pinned upstream 0.5.4 profiles for
   nine AN515 variants. Explicit `needs_profile` handling for AN515-55 and AN515-56 without aliasing.
   Only AN515-54 on verified BIOS V1.33 is eligible for guarded maximum trial writes.
   Developer read-only inspection tool (`tools/profile_check.py`) implemented and tested.
   Guarded trial preflight check wired through the shared selector.
   Parent review added strict capability/evidence validation, repository-confined
   paths and automatic selection of each entry's local artifacts. Real trials
   cannot substitute a synthetic DMI identity or another model's profile.
   Local verification: 401 tests, with five Linux tests skipped. On the server,
   63 targeted tests and all five fake-EC integration tests passed. The latter
   use a separate user-owned runtime. Real DMI and build attestation passed with
   hardware write support still N and no NBFC process remaining.
   Production control remains disabled across all models.
   Concrete remaining blockers documented in [docs/profiles.md](profiles.md).
9. **Basic NBFC backend and service (implemented; on-laptop validation partial)**:
   Firmware Auto, combined Manual 25..100% and Max through patched upstream NBFC.
   Independent guardian, durable session ownership, external recovery and guarded
   baseline restoration. Systemd startup/sleep hooks, group-gated socket access,
   installer and bounded hardware validation tool. Startup never restores a
   previous Manual target. No frontend or product CLI. See [the runbook](backend-service.md).
10. **On-laptop service validation (first attempt stopped safely)**:
    Installation completed after a paused apt process was resumed. The service
    reached 25% on both targets with spinning tachometers, then aborted on an
    independent guardian emergency indication. Firmware baseline and write
    protection were restored. The trigger was not retained by that build;
    updated diagnostics are staged for a repeat. Crash-only and staged
    suspend/resume validators are implemented and reviewed. The sleep observer
    records the handoff before sleep; kernel clocks and resume journal records
    establish sleep evidence. All 464 local tests passed with 12 opt-in skips;
    60 selected Linux tests passed, including seven native fake-EC checks.
    Physical crash and sleep/wake validation were pending at this stage. See [the record](data/nbfc/lifecycle-validation-2026-11-01.json).
11. **Installed service crash recovery (physical repeat passed; speed validation partial)**:
    The first crash check exposed native NBFC startup failure after SIGKILL.
    The guarded native engine now leaves PID/socket ownership to the guardian.
    After rebuilding and installing the fix, the physical repeat completed 25%,
    30%, Max, fresh-daemon crash recovery, a 20-second firmware Auto observation
    and shutdown handoff. The 60% observation was deferred before requesting it
    because guardian CPU reached 92 C after the 30% stage. Final cleanup verified
    baseline bytes and write_support=N. The service is stopped and boot startup
    remains disabled. Active and stopped sleep/wake checks are still pending.
    Local verification passed 482 tests with 14 opt-in skips; all 40 Linux
    restart-focused tests passed, including nine native fake-EC tests.
    See [the physical log](data/nbfc/backend-service-restart-fixed-2026-11-01.log).
12. **Blocked sleep and shutdown review (shutdown fix installed; cancellation follow-up staged)**:
    The first sleep attempt failed its pre-sleep controller shutdown dependency,
    so the laptop did not suspend. External recovery restored firmware control;
    the operator restored write protection and cancelled the test. A hardware-free
    Linux regression reproduced incorrect process-death detection when a zombie
    main thread still has live worker threads. Ownership now accounts for those
    threads. Cancellation retries recovery of failed services and clears stale
    restart intent after verified release. The user installed these fixes.
    Cancellation then failed while resetting an already inactive, unloaded unit.
    The follow-up resets only a failed unit and passed 48 focused tests on both
    local and Linux environments. This one-file correction needs installation.
    No further physical sleep test is planned without physical access or tested
    remote wake. See [the journal](data/nbfc/backend-service-suspend-blocked-2026-11-01.log).
13. **Application and additional models (deferred)**: add when needed.

The maximum-only helper also accepts an explicit duration up to 180 seconds.
`--duration 180 --mic` records 30 seconds before, three minutes at maximum, and
60 seconds after the guarded wrapper finishes. Temperature/RPM safety checks stay
at one-second intervals; the independent observer samples sensors and relative
microphone loudness every four seconds. Each mic capture excludes two seconds of
startup noise. Nine targeted Linux tests and 353 local tests (five skipped) pass.
Live read-only mic/temperature/RPM checks passed. The extended hardware run
subsequently passed at 19:38–19:41 UTC. Both fans held 6122 RPM, recorded mic level
rose 18.52 dB, and CPU/GPU temperatures trended down under an uncontrolled workload.
Baseline bytes were restored and verified, NBFC stopped, and write support is N.
See [the three-minute result](guarded-maximum-trial.md#three-minute-result).

## Longer Acer Auto baseline received

The separate passive recording completed from 18:43:04 to 19:13:04 UTC.
It contains 901 samples at two-second intervals, with no missing CPU readings.
CPU package temperature ranged from 47 to 95 C; fan 1 ranged from 2222 to
4000 RPM and fan 2 from 2255 to 3846 RPM. Source builds occurred during the
recording. Its workload was not matched to the later maximum-speed test, so
it cannot establish a temperature improvement caused by maximum fans.

[Baseline analysis](data/acer-auto-2026-11-01-analysis.json) and
[raw CSV](data/acer-auto-2026-11-01.csv). No audio was recorded.
