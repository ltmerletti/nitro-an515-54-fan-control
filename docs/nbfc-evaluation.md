# Upstream NBFC evaluation for Acer Nitro AN515-54

## Overview and pivot context

To minimize bespoke code on `basement-server` (Acer Nitro AN515-54, BIOS V1.33, Ubuntu 26.04.1),
the project prioritized evaluating the established, upstream NoteBook FanControl (`nbfc-linux`)
software before continuing any custom EC write backend development.

Existing NitroFan telemetry collectors, passive discovery datasets, unit test suites,
and mock architecture are preserved in the repository. Custom EC backend development
was suspended and partial files left intact. The read-only trial passed; a guarded maximum-only trial is now prepared.

## Package and dependency provenance

All evaluation components on `basement-server` were staged in `/home/luke/nbfc-evaluation`
as user-owned files without system-wide package installation, apt changes, or systemd services.

- **Upstream release**: `nbfc-linux` 0.5.4 (git tag `0.5.4`, commit `9da010d6a2e8351bdf6900d6c3643f1e742af936`).
  - Source release: [https://github.com/nbfc-linux/nbfc-linux/releases/tag/0.5.4](https://github.com/nbfc-linux/nbfc-linux/releases/tag/0.5.4)
  - Upstream package: `ubuntu-resolute-nbfc-linux_0.5.4_amd64.deb`
  - SHA256: `a9948da08735df3ff154638690d2941d77fdc28347bdb2ef517e0e6675910ccb`
  - Extracted to: `/home/luke/nbfc-evaluation/package`
  - Exact model profile: `/home/luke/nbfc-evaluation/package/usr/share/nbfc/configs/Acer Nitro AN515-54.json`
- **Dynamic dependency**: `liblua5.4-0`
  - Package: `liblua5.4-0_5.4.8-1build1_amd64.deb`
  - SHA256: `ff589ea3f181b89dd76b0240f79c59ac2f13f24e2053f624f774f596c7ef7897`
  - Extracted to: `/home/luke/nbfc-evaluation/dependencies/usr/lib/x86_64-linux-gnu`
  - Runtime environment: `LD_LIBRARY_PATH=/home/luke/nbfc-evaluation/dependencies/usr/lib/x86_64-linux-gnu`
  - With this path set, `ldd` reports all dynamic dependencies satisfied for both `nbfc_service` and `nbfc`.
- Manifest copies are tracked in `docs/research/nbfc-evaluation/` for review.

## Upstream source review and safeguards

Review of upstream 0.5.4 source code confirmed key architectural safeguards:

1. **Read-only execution**: In `src/service.c`, hardware register writes, initialization
   applies, fan speed flushes, and EC reset routines are all strictly guarded by
   `if (! options.read_only)`. In `--read-only` mode, the service performs reads and temperature
   filter calculations only.
2. **EC access backend**: Upstream `src/ec_sys_linux.c` opens `/sys/kernel/debug/ec/ec0/io`
   using `O_RDWR` even in read-only mode, but the kernel module `ec_sys` is loaded with
   `write_support=0` (verified `N`). The Linux kernel prevents any write operations.
   The helper script explicitly passes `--embedded-controller ec_sys` to avoid fallback to `/dev/port`.
3. **Execution model**: Upstream requires root (`geteuid() == 0`) to run `nbfc_service`.
   Foreground execution without `--fork` allows bounded execution via coreutils `timeout`.
4. **Service configuration**: Upstream supports absolute paths for `SelectedConfigId` in
   the service configuration file. `TargetFanSpeeds` is omitted because upstream rewrites
   and migrates this field to state files.

## Runtime paths and system state

While evaluation binaries and configurations reside in `/home/luke/nbfc-evaluation`,
compiled upstream paths write state to the host filesystem:

- `/etc/nbfc`: created for system configuration
- `/var/lib/nbfc`: created for model fingerprint and runtime state (`state.json`, `current_model.txt`)
- `/run/nbfc_service.pid`: process ID file, managed via advisory file locking
- `/run/nbfc_service.socket`: local Unix domain socket for client queries

These files are standard upstream paths and were absent prior to this trial. The helper
does not remove arbitrary files and ensures no competing NBFC process is active.

## What read-only evaluation proves vs active control

Successful completion of the bounded read-only trial can establish:
- Successful startup and profile parsing on this unit; this does not validate control behavior on BIOS V1.33.
- Successful polling of the EC via `ec_sys` debugfs node without errors.
- Verification of 16-bit word RPM read registers (`0x13`/`0x14` and `0x15`/`0x16`) tracked in debug logs.
- Validation that the daemon starts, serves status over its socket, and exits cleanly on SIGTERM.

Read-only evaluation **does not** prove:
- Fan speed control or overrides.
- Register write behavior (`0x37`/`0x38` CPU speed, `0x3a`/`0x3b` GPU speed).
- Manual control mode registers (`0x22` CPU mode, `0x21` GPU mode, `0x10` CoolBoost).
- Verified assignment of fan 1 and fan 2 to CPU and GPU physical heatsinks.

## Later staged verification (future work)

The short maximum-speed trial passed. Further testing remains bounded and staged:
1. Short fixed-speed trial at maximum fan (100%) to confirm register responsiveness.
2. Short fixed-speed trial at 30% to verify speed reduction.
3. Verification of automatic firmware handoff upon process exit.

The current hardware-test command and its limits are documented in
[guarded-maximum-trial.md](guarded-maximum-trial.md). No boot service is enabled.

## Read-only trial execution results (November 1, 2026)

The prepared read-only trial was executed on `basement-server` by the user with authenticated sudo:
Log file: `docs/data/nbfc/read-only-2026-11-01.log` (SHA256: `3c2c502cb7ed730119a599d52c2d4182db5a8bfb97db1e9805d11307f6aea257`).
Analysis: `docs/data/nbfc/read-only-2026-11-01-analysis.json`.

Verified outcomes:
- Dummy backend ran for 3 seconds and passed.
- Real EC polling ran for 45 seconds using `--embedded-controller ec_sys` and reached timeout deadline cleanly.
- Zero EC write calls were recorded across the run (`write_calls_in_debug_log: 0`).
- Kernel write protection remained intact (`write_support=N` verified before, during, and after).
- Zero residual processes remained active after clean SIGTERM shutdown (`active_processes_after: 0`).
- Little-endian 16-bit RPM read words verified:
  - Register 0x13/0x14 (Fan 1): read values from 2884 to 4000 RPM across 16 polling samples.
  - Register 0x15/0x16 (Fan 2): read values from 2727 to 2400 RPM across 16 polling samples.

Operational limits established:
1. **Average CPU core temperature**: NBFC defaults to averaging coretemp core inputs instead of reading canonical CPU `Package id 0`.
2. **Stock curve requests zero fan speed**: The stock temperature curve commands 0.0% fan speed below 45 C, which conflicts with our 25% minimum and maximum-only first trial.
3. **CurrentSpeed calculation**: Upstream computes `CurrentSpeed` as `(RPM / 6122) * 100`, which is normalized tachometer speed rather than PWM duty cycle.
4. **AutoMode semantics**: In NBFC, `AutoMode` indicates software-driven curve interpolation, not firmware handoff.
5. **State file interference**: Upstream reads and writes global state at `/var/lib/nbfc/state.json`, which can persist and restore stale target fan speeds on startup.

## Next phase: guarded maximum-only control evaluation

To evaluate active control safely without bespoke controller logic, a guarded maximum-only architecture was prepared:
- A build-time patch applied to upstream `src/ec_sys_linux.c`.
- A C guard (`tools/ec_guard/ec_guard.c`) routing every write via a single checked function with explicit allowlist policy.
- A maximum-only model profile (`tools/ec_guard/profiles/Acer_Nitro_AN515-54_guarded_max.json`) commanding 100% fan speed across all temperature thresholds.
- Pre-write snapshotting of all touched registers and automatic restoration back to exact original bytes.
- A Python supervisor (`tools/nbfc_guarded_trial.py`) tracking canonical Package id 0 temperature, both tachometers, and isolating host state.

Full specification and hardware-free test instructions are documented in [guarded-maximum-trial.md](guarded-maximum-trial.md).

Parent verification also passed five end-to-end Linux tests of the patched upstream
child against a regular fake EC file. The reviewed build uses private runtime paths
and leaves host NBFC state untouched. Kernel write support was N after fake testing. The human-run
maximum trial subsequently passed, and write support returned to N. See the linked trial instructions for the exact
command and its limits.

## First writable result

At 19:27 UTC on November 1, the guarded upstream profile drove both fans to
6122 RPM during a 30-second maximum request. Saved bytes were restored and
verified; fans slowed afterward. Register 0x03 was untouched. No NBFC process
remained, and write support was N on follow-up inspection. This supports using
upstream NBFC for control on BIOS V1.33, with the remaining lower-speed and
long-duration checks still pending.

The CPU still reached 93 C at maximum speed, so changing the curve alone is
not proven to eliminate its temperature spikes. Read the
[trial analysis](data/nbfc/maximum-2026-11-01-analysis.json) for measured results.
