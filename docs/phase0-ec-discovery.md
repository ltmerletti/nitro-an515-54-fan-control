# Phase 0 EC discovery recipe and evidence boundaries

## Purpose

This document specifies the read-only Embedded Controller (EC) discovery procedure
for the Acer Nitro AN515-54 (BIOS V1.33, Ubuntu 26.04.1, kernel 7.0.0-42-generic).

The goal of this phase is to capture observations combining:
1. Raw EC register bytes via `nitrofan.ec.ECReader` and `tools/ec_discover.py`.
2. Live tachometer fan RPM (`fan1`, `fan2`) from `acer_wmi` (`predator_v4=1`).
3. Real CPU package temperature (`coretemp`) and awake-only NVIDIA GPU temperature.

Each sample records its acquisition start time and `sample_duration_s`. EC bytes,
fan RPM, and temperatures are read sequentially; a sample is not atomic. RPM is
read directly after the EC snapshot, before a GPU query can introduce a delay.
Individual EC bytes can also change while a full dump is being read.

No fan speed control is enabled. Phase 0 hardware control remains **incomplete** and
the candidate EC control map remains **unverified**.

---

## Evidence boundaries and safety limits

- **Candidate readings do not confirm control**:
  Observing changing values in candidate registers (e.g. `0x13`, `0x15`) under firmware
  control demonstrates correlation, not control authority.
- **Sibling map discrepancies**:
  The initial baseline dump (`docs/ec-baseline-2026-11-01.md`) observed:
  - Register `0x03` held `0x21` (not `0x11` as proposed by the sibling map).
  - Registers `0x13`/`0x14` and `0x15`/`0x16` held `d9 09` (2521) and `60 09` (2400)
    in little-endian format, suggesting multi-byte tachometer RPM readings rather than
    single-byte percentages.
  - Registers `0x21`, `0x22`, `0x37`, `0x3a` were all `0x00`.
- **Zero hardware writes**:
  - Do NOT load `ec_sys` with `write_support=1`.
  - Do NOT write unlock sequences (`0x11`) to register `0x03`.
  - Do NOT apply battery-bit arithmetic or guessed bitmasks to register `0x03`.
  - Do NOT write speed or mode registers.
- **Evidence prerequisite**:
  Passive idle, load, and return-to-idle observations (and Windows PredatorSense /
  NitroSense trace logs if obtainable) must corroborate register behavior before any
  staged write test is proposed.

---

## Environment preparation

### 1. Load ec_sys with write protection

Load `ec_sys` explicitly disabling write support:

```sh
sudo modprobe ec_sys write_support=0
```

Verify that write support is disabled:

```sh
cat /sys/module/ec_sys/parameters/write_support
```

The output **must** print `N`. If it prints anything else, do not proceed.

*(Fallback note: If `ec_sys` is unavailable in the kernel, manual setup of the
`acpi_ec` driver providing `/dev/ec` is the fallback. Do not automatically install
DKMS packages).*

### 2. Ensure acer_wmi tachometer telemetry is active

Verify `acer_wmi` is loaded with `predator_v4=1`:

```sh
ls /sys/class/hwmon/*/name | xargs -n1 grep -H "acer"
```

If not present, load it:

```sh
sudo modprobe -r acer_wmi && sudo modprobe acer_wmi predator_v4=1
```

### 3. File permissions and security hygiene

- **Do NOT** run recursive `chmod` or `chown` on `/sys/kernel/debug`.
- **Do NOT** configure persistent passwordless sudoers rules.
- Run the collector using `sudo` while letting the unprivileged user's shell redirect
  stdout to a user-owned evidence file.

---

## Multi-phase collection recipe

Run all collections from the project repository. Output is streamed as JSON lines.
Redirecting stdout via the user's shell ensures the generated files are owned by `luke`,
not `root`.

### First paired baseline on basement-server

The user has already loaded `ec_sys` with writes disabled and provided a complete
dump. See [baseline observations](ec-baseline-2026-11-01.md). To compare the
possible two-byte readbacks with tachometer RPM, capture all 256 bytes in each
sample. Candidate-only collection omits the adjacent high bytes.

After deployment to `/home/luke/nitrofan-phase0`, run as `luke`:

```sh
sudo python3 ~/nitrofan-phase0/tools/ec_discover.py --full-dump --samples 30 --interval 2 --label passive-baseline > ~/nitrofan-ec-baseline.jsonl
```

This records roughly a minute of normal activity without adding load. The shell
creates a user-owned file that can be fetched over SSH. Keep write support at `N`.

### Phase A: Baseline idle (unloaded system)

Record 15 samples at 2-second intervals during steady-state idle:

```sh
sudo python3 tools/ec_discover.py --label idle --samples 15 --interval 2 > ~/ec-idle.jsonl
```

### Phase B: External thermal load

Run an external workload in a separate terminal (e.g. compilation or benchmark).
The collector itself does **not** induce CPU or GPU load.

```sh
sudo python3 tools/ec_discover.py --label load --samples 15 --interval 2 > ~/ec-load.jsonl
```

### Phase C: Return to idle (cool-down)

Stop the workload and immediately monitor fan speed and temperature decline:

```sh
sudo python3 tools/ec_discover.py --label return-idle --samples 15 --interval 2 > ~/ec-return-idle.jsonl
```

### Full 256-byte snapshot dumps (optional)

To capture every EC register (0x00..0xFF) for byte-differential analysis:

```sh
sudo python3 tools/ec_discover.py --full-dump --label full-idle --samples 1 > ~/ec-full-idle.jsonl
sudo python3 tools/ec_discover.py --full-dump --label full-load --samples 1 > ~/ec-full-load.jsonl
```

---

## Output format

Each output line is an independent JSON object:

```json
{
  "timestamp": "2026-11-01T17:35:00.123456+00:00",
  "elapsed_s": 0.0,
  "label": "idle",
  "ec_path": "/sys/kernel/debug/ec/ec0/io",
  "registers": {
    "0x03": 33,
    "0x13": 217,
    "0x15": 96,
    "0x21": 0,
    "0x22": 0,
    "0x37": 0,
    "0x3a": 0
  },
  "candidate_roles_unverified": {
    "unlock": {
      "register": "0x03",
      "raw_value": 33,
      "status": "UNVERIFIED",
      "verified": false
    },
    "fans": {
      "cpu": {
        "mode_register": "0x22",
        "mode_raw_value": 0,
        "speed_register": "0x37",
        "speed_raw_value": 0,
        "readback_register": "0x13",
        "readback_raw_value": 217,
        "status": "UNVERIFIED",
        "verified": false
      },
      "gpu": {
        "mode_register": "0x21",
        "mode_raw_value": 0,
        "speed_register": "0x3a",
        "speed_raw_value": 0,
        "readback_register": "0x15",
        "readback_raw_value": 96,
        "status": "UNVERIFIED",
        "verified": false
      }
    }
  },
  "telemetry": {
    "temperatures_c": {
      "cpu": 45.0,
      "gpu": 42.0,
      "acer_temp1": 43.0,
      "acer_temp2": 40.0,
      "acer_temp3": 39.0
    },
    "fans_rpm": {
      "fan1": 2450,
      "fan2": 2300
    },
    "diagnostics": {}
  }
}
```

---

## Current milestone status

- **Collector implementation**: COMPLETE (`nitrofan/ec.py`, `tools/ec_discover.py`, tests).
- **Phase 0 hardware evidence**: INITIAL DUMP RECEIVED. The user verified read-only
  access and supplied the first full EC dump. Paired EC/RPM samples and multi-phase
  collection remain pending. Hardware fan control remains disabled.
