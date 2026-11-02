# Basic NBFC backend

The active target is Acer Nitro AN515-54 with BIOS V1.33. Other models remain
catalogue entries for later work. Adding a model uses a profile and register
policy; it does not require another fan controller. The existing -54 policy is
not a compatibility promise for the -55 or -56.

## Modes

- Auto releases NBFC and restores the saved firmware-control bytes. Acer owns
  the fan curve. This is the startup mode after every service start or restart.
- Manual sets both fans to the same integer percentage, subject to the minimum
  and emergency override. Zero and values below 25% are rejected.
- Max requests 100% on both fans.

The backend uses neutral fan1/fan2 identifiers. The physical CPU/GPU assignments
have not been established. Percentages are requested targets, not a measurement
of electrical duty cycle. RPM comes from the Acer tachometers.

NBFC's software Auto and custom curves are outside this backend's scope. The
existing curve implementation remains available for mock development.

## Process ownership and recovery

The socket daemon handles requests and telemetry. A separate guardian owns the
native guarded NBFC process and restores firmware control when the daemon dies.
Systemd runs an additional recovery command after service termination, including
forced termination of the guardian. Recovery uses an active-session record and
the same restricted C writer. It must finish before EC write support is disabled.

No user-space process can perform cleanup while the kernel is hung or the
machine has lost power. Startup never reloads a previous Manual request.

The installed service runs as root because the EC debugfs interface requires it.
The socket is limited to root and members of the nitrofan group. Members can
change fan modes, so group membership is a local control permission. Clients do
not receive direct EC access. The service uses a private NBFC state directory,
not the host's `/var/lib/nbfc/state.json`.

The sleep hook stops the service before sleep and starts it in Auto after wake.
It remembers whether the service was running, so waking does not start a service
that was already stopped. A failed handoff fails the required sleep hook.

## Installation

Installation builds the pinned upstream commit with the reviewed guard and
copies only runtime code, profiles and administrative tools into `/opt/nitrofan`.
It preserves an existing `/etc/nitrofan/config.toml`. It does not start or enable
the service, and it does not enable EC writes. Stop an existing service before
upgrading; finish any outstanding recovery before replacing guard artifacts.

On basement-server, from the staged checkout:

```sh
sudo python3 tools/install_backend.py \
  --source-dir /home/luke/nbfc-evaluation/nbfc-linux-0.5.4 \
  --install-dependencies --client-user luke
```

The dependency option installs the system compiler, Lua development library,
Git, patch and procps through apt. Installed native binaries use system libraries,
not the earlier evaluation libraries in Luke's home directory. Existing
maximum-trial builds and reports remain separate.

Add `--validate` to run the bounded check immediately after installation, using
the same sudo authentication. This still leaves the service stopped afterward.

## Bounded hardware check

On 2026-11-02 the full suite passed locally (504 run, 16 skipped) and on
basement-server (504 run, 15 opt-in skipped), and the 10 native fake-EC tests
passed on Linux. The daemon integration tests now ignore an installed
`/etc/nitrofan/config.toml`; previously they failed only on the installed host.
Every installed runtime file, unit file and native build digest matched the repo
except the pending cancellation script. See
[the Linux suite log](data/nbfc/final-check-linux-suite-2026-11-02.log).
The earlier local run passed 487 tests, with 16 Linux or opt-in tests skipped.
All 66 shutdown-focused tests passed on Linux, including ten native fake-EC tests
and a real synthetic process with a zombie main thread and live worker thread.
The earlier 40 restart-focused tests also passed, including nine native fake-EC tests.
The revised validator's earlier 58-test Linux run also passed.
The C guard's nine test groups passed in the earlier
backend review; the C guard is unchanged. Independent Linux checks using
the compiled upstream child passed target transitions, parent/guardian/child
death recovery, thermal override, sensor loss and socket-daemon shutdown against
a regular EC fixture. See [the verification record](data/nbfc/backend-service-2026-11-01-verification.json).

The first user-authenticated installation completed, but its validation stopped
during the 25% stage when the independent guardian reported an emergency. The
latest main-daemon CPU sample was 51 C; the older guard did not record its own
triggering sample, so the cause is unresolved. Cleanup verified firmware baseline
bytes and write protection. The reviewed update retains guardian trigger readings
and reports them in the journal and status under `safety`. Do not bypass the
temperature guard to get a passing test. See [the attempt and current checks](data/nbfc/lifecycle-validation-2026-11-01.json).

The reviewed update was then installed and rechecked. The guard captured CPU
and Acer temperature readings of 92 C in firmware Auto before Manual was
requested, followed by an Acer reading of 94 C. A separate read-only `sensors`
check also reported CPU package and core readings of 92 C with NitroFan stopped.
The recheck aborted and restored baseline/write protection. Both boot services
remain disabled. A subsequent cooldown held both fans at 6122 RPM and reached
its cool target, but the old full validator then stopped at 83 C during its
first low-speed stage. The revised validator below separates thermal deferrals
from control failures. The 90 C backend emergency override is unchanged.

The first physical crash-only run held Max successfully and external recovery
restored the firmware baseline after SIGKILL. Systemd restarted the daemon, but
the native engine then failed initialization with status 3, so this was not a
passing recovery test. The old native startup path used an exclusive PID file
and Unix socket; SIGKILL can leave those artifacts behind. Earlier synthetic
mode skipped both paths and therefore missed this difference.

The reviewed native startup fix removes that duplicate PID/socket handling in
all guarded runs. The guardian retains its exclusive lock, process identity,
client socket and baseline restoration. Forking is refused. No EC write policy
or fan-temperature threshold changes. A regression kills guardian and native
processes, recovers firmware, and starts fresh control with stale native artifacts
present. The recovery waiter now also rejects a daemon with errors, missing fan
readings or an emergency flag. Native failure details are copied to the journal.
The user rebuilt and installed this fix, then repeated the physical check.
The latest run completed 25%, 30%, Max, SIGKILL crash recovery, a 20-second
firmware Auto observation and shutdown handoff. Recovery changed the daemon PID
from 137351 to 137769, restored the baseline bytes, and returned healthy Auto
status without errors or an emergency flag. The installed native binary SHA-256
is `3bc8676381dcdd3eaa2cf549da55694dbbf46313dfc44d81f3e6205f88b209e3`.

The result remains partial. Before requesting 60%, the validator observed guardian
CPU at 92 C after the 30% stage and deferred that observation. Max and crash
recovery continued successfully. Final cleanup restored and verified baseline
bytes and `write_support=N`; the service is stopped and boot startup disabled.
See [the physical log](data/nbfc/backend-service-restart-fixed-2026-11-01.log).

After installation, run:

```sh
sudo python3 /opt/nitrofan/tools/validate_backend.py > ~/nitrofan-backend-check.jsonl 2> ~/nitrofan-backend-check.log
```

This checks the 25% floor, 30%, 60% and Max, kills the socket daemon at Max to check recovery,
then observes firmware Auto. Each observation lasts 20 seconds. The validator records the pre-crash
daemon PID, sends SIGKILL, and verifies that systemd restarts a fresh daemon instance with a new PID.
It confirms that the fresh daemon serves firmware Auto and that EC registers match baseline bytes before
accepting recovery.

To run only the crash and recovery check without stepping through every lower target speed:

```sh
sudo python3 /opt/nitrofan/tools/validate_backend.py --crash-only > ~/nitrofan-crash-check.jsonl 2> ~/nitrofan-crash-check.log
```

The full check takes about 100 seconds (5 stages of 20 seconds each). The crash-only check takes about
40 seconds. Both record CPU temperature, actual RPM, controller state and raw control-register readback.
Missing/invalid CPU readings, stopped fans and controller errors abort the run.
At 80 C or a temperature override, low-speed stages switch to Max and remain
pending while Max and crash recovery proceed. The independent 90 C guard remains
active. Max observations allow warm readings, but abort at the model's 100 C
CPU critical limit. They still require spinning tachometers, actual 100 target
bytes and a persistent Max request.

If the post-crash Auto observation warms up, it is shortened and marked pending.
The crash check still requires a fresh daemon in Auto with matching baseline
bytes, and final service shutdown must verify firmware handoff/write protection.
Only completed stages appear in `completed`. A `partial` result and exit code 2
mean some observations are pending; a `passed` result applies to the selected
scope, either the full check or `--crash-only`.

Until these script updates are installed, run the reviewed staged version:

```sh
sudo python3 /home/luke/nbfc-evaluation/backend-stage/tools/validate_backend.py --crash-only > ~/nitrofan-crash-only.log 2>&1
```

The check stops the service and verifies saved bytes and `write_support=N` at
the end. A separate systemd timer bounds the run if the terminal or validator
dies. The stop timer remains armed until cleanup verification succeeds. Boot startup
remains disabled. Review the JSON `passed` event and journal before enabling everyday use.

## Suspend and resume validation

The validator exercises the installed sleep hook and adds a temporary required
observer unit. That observer runs after firmware handoff and before system sleep,
recording baseline EC bytes, write protection and the hook's restart intent.
It blocks the sleep transaction if those checks fail. Merely running the tool
or its help cannot put the laptop to sleep. Only the explicit `suspend` command does.

Both scenarios require an initially stopped service and finish stopped. The
15-minute stop timer survives loss of the SSH connection. Preparation and
verification use separate commands, so no SSH process needs to survive sleep.
Boot startup and existing sleep-hook enablement are never changed.

### Active service scenario

The active scenario validates that the sleep hook safely stops a running service before suspend,
hands control back to firmware Auto, enforces `write_support=N`, and starts a fresh daemon in Auto
after wake.

1. Prepare the validation checkpoint:

```sh
sudo python3 /opt/nitrofan/tools/validate_suspend_resume.py prepare --scenario active --target-percent 60
```

This starts the service, checks 60% on both fans and healthy telemetry, and arms
the timer. A private, atomically saved checkpoint at
`/var/lib/nitrofan-validation/suspend.json` records the initial daemon PID and
systemd invocation ID. A temporary dependency under
`/run/systemd/system/sleep.target.requires/` pulls in the observer and actual
sleep hook. Only one validation session can be pending at a time.

2. Put the machine to sleep:

```sh
sudo python3 /opt/nitrofan/tools/validate_suspend_resume.py suspend
```

An SSH disconnection is expected. The operator can instead use the desktop's
suspend action or `systemctl suspend`. Lid and power-button behavior depends on
the system settings; those actions can be configured to do something else.

3. Wake the machine physically:

Leave the laptop asleep for at least five seconds, then wake it using its power
button or keyboard if supported. The sleep hook should restart the service in
Auto and clear its restart marker. Someone must be nearby; remote wake is not
assumed. Allow about 5-10 minutes for each complete manual check.

4. Reconnect SSH and verify recovery:

```sh
sudo python3 /opt/nitrofan/tools/validate_suspend_resume.py verify
```

Verification requires both a current-boot resume journal record since
preparation and at least two seconds of additional kernel suspend time.
`CLOCK_BOOTTIME` includes suspended time; `CLOCK_MONOTONIC` does not. A hook log,
old sleep timestamp or reboot cannot establish a pass. Missing sleep evidence
returns exit code 2, keeps the timer/checkpoint, and allows another verification
attempt or cancellation.

It then checks the saved pre-sleep handoff, a fresh systemd invocation in Auto,
the cleared restart marker, CPU temperature below 80 C, positive RPM from both
fans and baseline EC bytes. Cleanup stops control, verifies baseline bytes and
`write_support=N`, removes the temporary observer dependency and disarms the
timer. A failed cleanup keeps its checkpoint and timer for recovery.

### Stopped service scenario

The stopped scenario validates that waking the machine does not start `nitrofan.service` if it was
stopped before sleep.

1. Prepare:

```sh
sudo python3 /opt/nitrofan/tools/validate_suspend_resume.py prepare --scenario stopped
```

2. Suspend:

```sh
sudo python3 /opt/nitrofan/tools/validate_suspend_resume.py suspend
```

3. Wake physically and verify:

```sh
sudo python3 /opt/nitrofan/tools/validate_suspend_resume.py verify
```

This verifies that the service remains inactive, write protection remains `write_support=N`, and baseline
registers are intact.

### Results and cancellation

The last completed result, including handoff evidence, is saved privately at
`/var/lib/nitrofan-validation/last-suspend-result.json`. The pre-sleep observer
also logs to `journalctl -u nitrofan-suspend-check -b`. Read the result with sudo.
These files and journals survive terminal loss. Starting another test while a
checkpoint exists is refused.

If you decide not to sleep, or a sleep attempt is rejected, cancel safely:

```sh
sudo python3 /opt/nitrofan/tools/validate_suspend_resume.py cancel
```

Cancellation stops the service and verifies restoration before removing the
checkpoint. If recovery fails, retain the checkpoint and journal for diagnosis.
The tool deliberately does not schedule automatic wake or require an SSH
connection to remain alive.

### Blocked sleep attempt

The first active-service sleep attempt on 2026-11-01 did not reach suspension.
The guardian timed out waiting for its native child to exit. External recovery
restored the firmware baseline, but the stop hook still saw an NBFC process and
refused to unload the EC driver. The failed sleep dependency blocked suspension.
The operator then restored write protection, reset the failed main unit, and
cancelled the test. Read-only inspection confirmed an inactive main service,
no NBFC process, `write_support=N` and no validation timer.
See [the journal](data/nbfc/backend-service-suspend-blocked-2026-11-01.log).

Review reproduced a Linux process-tracking bug with a synthetic threaded child.
A zombie main thread can coexist with live worker threads. Ownership detection
now checks those threads before declaring the native process stopped, preserving
SIGKILL escalation and preventing early restoration. This reproduces a shutdown
failure mechanism; the physical child thread state was not captured during the
original failure.

Cancellation now retries guarded recovery after stopping an already failed unit,
then clears the unit failure and verifies baseline/write protection. It removes
leftover restart intent only after verification. Repeated cancellation also works
when an earlier attempt already removed the checkpoint. Missing transient timer
units no longer print an expected error. The user installed these fixes successfully.
All 66 focused Linux tests passed, including shutdown during an emergency override.
The full local run passed 487 tests with 16 skipped. See
[the synthetic test log](data/nbfc/backend-stop-fix-synthetic-2026-11-01.log).

The first cancellation after installation encountered another administrative
error. It unconditionally ran `reset-failed` against an already inactive unit,
which systemd can unload from memory. `reset-failed` does not load the unit and
returned "Unit nitrofan.service not loaded". Read-only checks confirmed the unit
file was present, the service was inactive, no NBFC process remained, and EC
writes were disabled. The stale restart marker was retained because cancellation
stopped at the error.

The follow-up correction queries the state and resets only a failed unit. All
48 focused cancellation/lifecycle tests passed locally and on Linux. The file is
staged at `/home/luke/nbfc-evaluation/backend-stop-check`. Copy this one script
into the existing installation, then cancel again to retire the leftover marker
after checked cleanup:

```sh
sudo install -o root -g root -m 755 \
  /home/luke/nbfc-evaluation/backend-stop-check/tools/validate_suspend_resume.py \
  /opt/nitrofan/tools/validate_suspend_resume.py &&
sudo python3 /opt/nitrofan/tools/validate_suspend_resume.py cancel
```

This leaves the service stopped and does not request suspension. A physical
shutdown check with the installed fix is still needed; the synthetic regressions do
not establish that result.

Do not repeat suspend over SSH without someone ready to wake the laptop or a
previously tested remote wake method. The safety stop timer does not wake the
machine. Physical sleep/wake validation is deferred while physical access is
unavailable.

### Final remote check (2026-11-02)

The user installed the cancellation follow-up. Cancellation verified the baseline
bytes and `write_support=N`, then removed the leftover restart marker. The full
validator then returned partial (exit 2). Max, SIGKILL crash recovery (daemon
PID 397293 to 397576) and shutdown handoff passed. About 3 seconds into the 25%
stage, the guardian's CPU reading jumped from 49 C to 93 C. The 90 C override
forced Max within about a second, and the low-speed and Auto stages were
deferred. Cleanup verified the baseline and write protection.

The heat came from the host's workload, not from fan control. With NitroFan
stopped and firmware Auto in charge, the CPU package swung from 51 C to 96 C
within 20 seconds. A `uvicorn` process in the `mhwa_wn_finder-web` container was
bursting to a full core. Even at 6122 RPM the CPU held above 90 C. The 25% and
30% stages passed on 2026-11-01. See
[the physical log](data/nbfc/final-check-physical-2026-11-02.jsonl).

### Not tested

The backend is complete. Physical runs confirmed 25%, 30%, Max, crash recovery
and firmware handoff. These were not tested and are optional:

- A full 20-second observation at 60%.
- Suspend and resume with physical wake. The server is configured not to sleep.

## Startup and client access

After the hardware check passes:

```sh
sudo systemctl enable nitrofan-sleep.service
sudo systemctl enable --now nitrofan.service
```

Log in again after installation to pick up nitrofan group membership. Existing
SSH sessions retain their previous group list. There is no application or panel
widget yet. A future client can use `/run/nitrofan/nitrofan.sock` with these JSON
requests, one per line:

```json
{"cmd":"status"}
{"cmd":"set_mode","mode":"auto"}
{"cmd":"set_mode","mode":"fixed","percent":60}
{"cmd":"set_mode","mode":"max"}
```

`sudo systemctl stop nitrofan` releases control and restores EC write protection.
Use `journalctl -u nitrofan -b` to inspect failures. If restoration fails, retain
the session record and journal for recovery rather than removing the record or
reinstalling over it.

## Why Git reports so many insertions

Only the initial README was tracked before this work. The insertion count covers
the accumulated project rather than the latest profile change. The audit before
the service work counted about 21,400 lines. Roughly 6,600 were tests, 4,800 were
saved observations and research, and 1,900 were documentation. The Python runtime
was about 3,900 lines; tools and the C guard accounted for most of the remainder.
Tests and hardware evidence are retained. They are not installed as runtime
dependencies.
