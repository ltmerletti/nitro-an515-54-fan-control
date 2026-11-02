# NitroFan

Fan control research for the Acer Nitro AN515-54 on Linux.

## Status

The basic backend now connects NitroFan's socket API to guarded upstream NBFC.
It provides firmware Auto, a shared Manual target for both fans (25..100%), and
Max. Startup always selects Auto. A separate guardian and systemd recovery path
restore firmware control after process failure. No application or panel UI is
included.

The active target is Acer Nitro AN515-54 on BIOS V1.33. The earlier three-minute
maximum trial confirmed both fans reached 6122 RPM and returned to firmware
control. The installed service has now passed on-laptop checks of 25%, 30%, Max,
crash recovery and firmware handoff. The backend is complete. A full 60%
observation and physical sleep/wake were not tested. See
[installation and validation](docs/backend-service.md).

The model catalogue remains an extension point. Support for -55, -56 and other
models is deferred until needed; their control registers are not assumed to
match the -54. Historical research and hardware evidence are retained.

## NitroFan package

Python 3.12+, standard library only. The `nitrofan/` package contains:

- **config.py**: loads and validates `/etc/nitrofan/config.toml` via `tomllib`
- **curve.py**: fan curve interpolation with hysteresis and rate limiting
- **protocol.py**: JSON-lines wire format helpers and frame limits
- **sensors.py**: real sensor provider for `coretemp`, awake-only targeted NVIDIA GPU, and `acer_wmi`
- **ec.py**: read-only `ECReader` for ACPI Embedded Controller pread at offsets 0..255
- **backends/**: abstract `Backend` base, in-memory `MockBackend`, read-only `HwmonBackend`, and guarded `NBFCBackend`
- **lock.py**: single-instance advisory lock (`fcntl.flock`) and stale socket cleanup
- **controller.py**: deterministic safety controller, capabilities tracking, and emergency latching
- **server.py**: non-blocking Unix domain socket server (permissions 0660)
- **daemon.py**: daemon service entry point (`python3 -m nitrofan.daemon`)
- **nbfc_guardian.py**: independent NBFC supervision and external recovery

### Running the daemon

Run the daemon with the read-only hwmon backend:

```sh
python3 -m nitrofan.daemon --backend hwmon --read-only --socket /tmp/nf.sock
```

Or run unprivileged development using the mock backend:

```sh
python3 -m nitrofan.daemon --backend mock --socket /tmp/nf.sock
```

Options:
- `--backend [mock|hwmon|ec|nbfc]`: backend selection (`hwmon` and `ec` require `--read-only`; `nbfc` requires its guarded build/config)
- `--read-only`: enable read-only monitoring mode (zero hardware writes; required for `hwmon`)
- `--sysfs-root PATH`: root directory to prepend for sysfs paths (for testing against fake sysfs trees)
- `--socket PATH`: path to Unix domain socket (default `/run/nitrofan/nitrofan.sock`)
- `--config PATH`: optional path to TOML configuration file
- `--lock PATH`: lock file (`<socket>.lock` for custom sockets; `/run/nitrofan/nitrofand.lock` for the production socket)
- `--debug`: enable debug logging

### Interacting via raw Unix socket

No CLI is implemented yet. Communicate with the running daemon directly over its
Unix socket using standard tools or Python.

Using netcat:

```sh
# Query status
echo '{"cmd":"status"}' | nc -U /tmp/nf.sock

# Set manual fixed speed to 50%
echo '{"cmd":"set_mode","mode":"fixed","percent":50}' | nc -U /tmp/nf.sock

# Return to automatic firmware mode
echo '{"cmd":"set_mode","mode":"auto"}' | nc -U /tmp/nf.sock
```

Using Python:

```python
import socket
from nitrofan.protocol import CMD_STATUS, decode_reply, encode_request
from nitrofan.protocol import MAX_FRAME_BYTES

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(5)
    client.connect("/tmp/nf.sock")
    client.sendall(encode_request(CMD_STATUS))
    with client.makefile("rb") as stream:
        reply = decode_reply(stream.readline(MAX_FRAME_BYTES + 1))
print("Daemon mode:", reply["mode"])
print("Temperatures:", reply["temps"])
print("Fan speeds:", reply["fans"])
```

Mock fixed mode accepts a single `percent` or a map containing both `cpu` and `gpu`.
The NBFC backend accepts a single target or equal `fan1`/`fan2` targets. It rejects software curves.
Curve updates remain in memory until restart. Combined curve data is limited to
32 KiB so status replies fit within the 64 KiB protocol frame limit. `save` returns
an explicit error at this checkpoint; persistence comes later.

Checkpoint 2 was verified with 228 tests on Python 3.12.16. Milestone 3 was verified
with 267 tests covering real hwmon discovery, awake-only targeted NVIDIA GPU queries,
read-only controller safeguards, and fake sysfs daemon socket integration.

### Config

The example uses `backend = "mock"` for development without hardware access.
Load it directly while working through the mock milestones.
See [examples/config.toml](examples/config.toml) for all fields and documentation.

Load config in Python:

```python
from nitrofan.config import load_config
cfg = load_config()                          # /etc/nitrofan/config.toml
cfg = load_config("examples/config.toml")    # or a specific path
```

Invalid config raises `ConfigError` with a descriptive message.

### Fan curves

```python
from nitrofan.config import CurvePoint
from nitrofan.curve import FanState, interpolate

curve = (CurvePoint(45, 25), CurvePoint(60, 40), CurvePoint(70, 60),
         CurvePoint(80, 85), CurvePoint(88, 100))

# Pure interpolation (no state)
pct = interpolate(curve, 65)  # 50

# With hysteresis and rate limiting
fan = FanState()
pct = fan.update(temp_c=70, raw_target=interpolate(curve, 70), min_percent=25)
```

### Protocol

```python
from nitrofan.protocol import encode_request, decode_reply

frame = encode_request("status")  # b'{"cmd":"status"}\n'
reply = decode_reply(response_bytes)
```


## Read-only monitor

[tools/monitor.py](tools/monitor.py) logs timestamps, temperatures, available fan
RPM, and optional microphone loudness to CSV. It uses Python 3.12+ and the standard
library. It never writes fan registers, loads modules, or accesses `/dev/ec`.

```sh
# Print one CSV row every 5 seconds. Stop with Ctrl+C.
python3 tools/monitor.py

# Save 12 samples, about a minute of observations.
python3 tools/monitor.py --samples 12 --csv fan-log.csv

# Include a one-second microphone sample during each interval.
python3 tools/monitor.py --mic --csv fan-log.csv

# List sensor paths and recording devices without recording audio.
python3 tools/monitor.py --list
```

`--interval SEC` changes the sampling interval. `--csv PATH` creates or overwrites
that file and flushes each row immediately. Without it, CSV goes to stdout.
Diagnostics go to stderr. Missing readings stay blank.

CSV includes UTC time, monotonic elapsed seconds, Celsius readings from hwmon,
RPM where exposed, and Nvidia GPU temperature when available. Column names include
the chip, hwmon ID, label, and sensor number to distinguish sensors. They stay fixed
during a run; hwmon IDs may change across boots.

Nvidia display devices are checked for `runtime_status=active` before every
`nvidia-smi` query. Each query targets that specific PCI device. GPU temperature
stays blank while suspended, then resumes when the device is active again.

## Microphone

Install the optional capture tool on basement-server.

```sh
sudo apt install alsa-utils
arecord -l
```

The discovered analog capture device on basement-server is the PCH card. If the
default device fails, select it explicitly.

```sh
python3 tools/monitor.py --mic --mic-device plughw:CARD=PCH,DEV=0 --csv fan-log.csv
```

A copy is installed at `~/nitrofan-monitor.py` on basement-server. Run it there with
`python3 ~/nitrofan-monitor.py --mic --mic-device plughw:CARD=PCH,DEV=0 --csv ~/fan-log.csv`.

The user must have permission to open `/dev/snd` capture devices. If capture reports
permission denied, run the monitor with sudo for a short test or arrange audio-group
access. The monitor itself does not change permissions or mixer settings.

To grant audio access, run `sudo usermod -aG audio luke`, then disconnect SSH and
reconnect. Use a fresh login before testing capture without sudo.

`mic_dbfs` is relative RMS loudness, not calibrated room decibels (dB SPL). Values
closer to zero are louder. Keep microphone position and gain fixed for comparisons;
automatic gain control can obscure fan noise changes. Audio is processed in memory
and discarded. `mic_clipping=1` means the capture hit full scale. Silence has a blank
dBFS value with clipping `0`; failed capture leaves both fields blank.

## Fan speed

On basement-server, loading `acer_wmi` with `predator_v4=1` exposes an `acer` hwmon
device with `fan1_input`, `fan2_input`, and three temperature readings. Confirmed on
November 1, 2026 with BIOS V1.33 and Ubuntu kernel 7.0.0-42-generic.

```sh
sudo modprobe -r acer_wmi && sudo modprobe acer_wmi predator_v4=1
```

The monitor detects these RPM readings at startup. Restart it after loading the
module to include them in a new CSV. The CPU/GPU assignments of fan1 and fan2, and
the three Acer temperatures, are not verified. No PWM files appeared, so fan speed
control through this interface is not confirmed. EC access is unnecessary for the
current monitoring task; the sibling EC register map remains unverified.

To retain the module option across boots, create a modprobe configuration file.
This affects subsequent module loads; it does not reload the running driver.

```sh
echo 'options acer_wmi predator_v4=1' | sudo tee /etc/modprobe.d/nitrofan-monitor.conf
```

## Phase 0 EC discovery collector

[tools/ec_discover.py](tools/ec_discover.py) records raw EC registers alongside
real CPU/GPU temperatures, and neutral `acer_wmi` fan tachometer RPM. It runs without
pip install or manual `PYTHONPATH` manipulation. It is strictly read-only: no writes,
no unlock sequences, no battery-bit changes, and no write-support module loading.
See [docs/phase0-ec-discovery.md](docs/phase0-ec-discovery.md) for the complete recipe.
Samples include their start time and acquisition duration. EC bytes, tachometers,
and temperatures are read sequentially. Use `--full-dump` when comparing possible
two-byte readbacks; candidate-only collection does not include adjacent bytes.

The first paired capture validates `0x13`/`0x14` and `0x15`/`0x16` as little-endian
16-bit RPM readbacks for neutral `fan1`/`fan2` on this unit. These are not
single-byte percentages. Fan control and CPU/GPU fan assignments remain
unverified. See the [capture analysis](docs/ec-passive-baseline-2026-11-01.md).

```sh
# 1. Load ec_sys with write-protection enabled
sudo modprobe ec_sys write_support=0
cat /sys/module/ec_sys/parameters/write_support  # must output 'N'

# 2. Collect 15 idle samples at 2-second intervals, redirecting to a user-owned file
sudo python3 tools/ec_discover.py --label idle --samples 15 --interval 2 > ~/ec-idle.jsonl

# 3. Optional full 256-byte dump
sudo python3 tools/ec_discover.py --full-dump --label full-idle --samples 1 > ~/ec-full-idle.jsonl
```

Options:
- `--samples N`: sample count (default 1, must be >= 1)
- `--interval SEC`: interval in seconds (default 2.0, positive finite)
- `--label TEXT`: phase label (e.g. `idle`, `load`, `return-idle`)
- `--full-dump`: read all 256 bytes (0x00..0xFF) instead of configured candidate registers only
- `--config PATH`: path to TOML configuration (default `examples/config.toml`)
- `--ec-path PATH`: custom EC interface path (for testing)
- `--sysfs-root PATH`: prepend root for fake sysfs testing

## NBFC read-only evaluation

A bounded read-only trial helper is available at [tools/nbfc_readonly_check.sh](tools/nbfc_readonly_check.sh)
and deployed to `/home/luke/nbfc-evaluation/read-only-check.sh` on `basement-server`.
It verifies DMI identifiers, requires `write_support=N`, enforces `--read-only` and
`--embedded-controller ec_sys`, runs a brief 3-second dummy profile validation,
and executes a 45-second bounded foreground trial without background forks or writes.
See [docs/nbfc-evaluation.md](docs/nbfc-evaluation.md) for full provenance and trial details.

```sh
# Deploy and run on server (redirected to a user-owned log file)
sudo /home/luke/nbfc-evaluation/read-only-check.sh > ~/nbfc-readonly.log 2>&1
```

## Multi-model profile catalogue

NitroFan uses a configuration-driven catalogue at [profiles/catalog.json](profiles/catalog.json)
for notebook model identification and profile provenance. Model profiles are data,
not code. The system does not use separate controller classes or fuzzy matching.

All models match exact DMI `sys_vendor` and `product_name` strings from sysfs.
Only Acer Nitro AN515-54 on verified BIOS V1.33 is eligible for the guarded
maximum trial runner. Models AN515-55 and AN515-56 report `needs_profile`
because no exact profile exists in pinned upstream nbfc-linux 0.5.4 commit
`9da010d6a2e8351bdf6900d6c3643f1e742af936`. Aliasing to other models is forbidden.
All other models report `upstream_only` pending hardware testing. Production
validation remains pending across all models. The experimental NBFC service targets only the -54 on V1.33. See [docs/profiles.md](docs/profiles.md)
for details and catalogue schema.

[tools/profile_check.py](tools/profile_check.py) is a read-only developer diagnostic
for checking the catalogue and testing local DMI detection. It never accesses `/dev/ec`
or modifies hardware state.

```sh
# List all catalogue entries
python3 tools/profile_check.py --list

# Inspect host DMI match
python3 tools/profile_check.py --detect

# Output machine-readable JSON
python3 tools/profile_check.py --detect --json
```

## Remaining on-laptop checks

Backend implementation and service packaging are installed. The latest
[bounded service validation](docs/backend-service.md#bounded-hardware-check)
completed 25%, 30%, Max, daemon SIGKILL recovery, a 20-second firmware Auto
observation and shutdown handoff. The 60% observation was deferred before it
was requested because the guardian sampled CPU at 92 C after the 30% stage.
The result is partial, with 60% still pending under cooler conditions.
The service finished stopped, baseline bytes restored and EC writes disabled.
The [staged suspend/resume check](docs/backend-service.md#suspend-and-resume-validation)
records firmware handoff before sleep and verifies a fresh daemon in Auto after
wake. Someone must be nearby to wake the laptop because sleep interrupts SSH.

The earlier crash test exposed native startup failure after SIGKILL. The installed
fix removes duplicate native PID/socket handling. The repeat recovered to a fresh,
healthy daemon in firmware Auto and passed the subsequent observation. Earlier
thermal interruptions and the failed crash test remain in the
[hardware record](docs/data/nbfc/lifecycle-validation-2026-11-01.json).
The 90 C backend emergency threshold is unchanged. Active and stopped service
sleep/wake scenarios still need physical validation before boot startup is enabled.
The first sleep attempt was blocked by a controller shutdown failure before
suspension. The operator restored firmware baseline/write protection and cancelled
the test. The shutdown fix is installed. A follow-up cancellation correction
skips resetting a service that is already inactive; it is staged for installation.
Physical sleep testing is deferred because the operator has no physical access.
Mock and fake-EC checks verify software behavior. Other-model support, independently
labelled fan control and a panel interface are deferred and do not block combined
fan control on this -54.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The suite covers monitoring, EC reading and discovery, profile validation,
NBFC guards, controller safety, local socket handling, service lifecycle and
installation. Native NBFC integration tests opt in to a separate Linux build and
use a regular 256-byte EC fixture. They never use root or hardware EC files.

`--sysfs-root PATH` is available on the monitor for testing against a directory
containing `sys/class/hwmon` and `sys/bus/pci/devices`.
