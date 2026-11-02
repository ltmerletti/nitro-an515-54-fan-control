# Linux-only control research

The user confirmed basement-server runs Linux only. Windows NitroSense dumps
are unavailable. Continue read-only evidence collection and model-specific
source review before preparing a staged control test.

## Exact-model NBFC profile

The [nbfc-linux AN515-54 profile](https://github.com/nbfc-linux/nbfc-linux/blob/2b8e31bf95b7f0c2e0d80adb6fd598be6edb8fc8/share/nbfc/configs/Acer%20Nitro%20AN515-54.json)
was added in [commit e00cd639](https://github.com/nbfc-linux/nbfc-linux/commit/e00cd6398c902212952174c4dd4a07469e3f8d24)
on February 13, 2026. The commit author reports testing with the AN515-54.
The BIOS revision is unspecified, so this does not establish write behavior
on basement-server's V1.33 firmware.

Its word-sized read registers at `0x13` and `0x15` agree with our measured
little-endian RPM pairs. The profile names them CPU and GPU respectively;
those physical assignments remain externally reported rather than independently
verified on our unit. Keep neutral `fan1` and `fan2` names in telemetry.

The profile lists target registers `0x37` and `0x3a`, CPU manual/auto values
`0x0c`/`0x04` at `0x22`, and GPU manual/auto values `0x30`/`0x10` at `0x21`.
These are source-backed candidates, not validated writable settings here.
Unlike the newer AN515-57 profile, it lists no `0x03` unlock write. Do not
overwrite our observed `0x21` value at `0x03` based on the sibling recipe.

## Differences that affect any future test

The pinned NBFC implementation sets both read and write widths from
`ReadWriteWords=true`, unless a fan-specific override is present. This model's
profile has no override. In `src/fan.c`, target writes therefore call
`ec->WriteWord`, while mode and CoolBoost entries in
`src/register_write_configuration_utils.c` call `ec->WriteByte`.

A target word starting at `0x37` or `0x3a` can also touch `0x38` or `0x3b`.
Neither adjacent byte is in NitroFan's original candidate write allowlist.
The profile additionally disables CoolBoost at `0x10` on initialization and
sets it to `1` on reset; `0x10` is also outside that allowlist. Its reset
targets are fixed at 50, and reset modes differ from our observed zero
baseline. None of these actions should be copied into an active test without
explicit configuration, original-value restoration, and a justified need.

The read scaling maximum of 6122 RPM and any zero-speed curve entries in the
profile are unverified on our unit. NitroFan's minimum manual speed remains
25 percent by default. RPM is not a duty-cycle percentage.

This profile gives a better candidate control map without requiring Windows.
It does not replace the required guarded 100 percent, 30 percent, and auto
hardware verification. Before that step, complete the read-only backend and
prepare a test that restricts every written byte to the active allowlist,
records originals, monitors CPU temperature and both tachometers, and restores
firmware control on failure or exit. No NBFC service was installed or run.

## Saved source evidence

[Sources and SHA-256 hashes](research/nbfc-an515-54/sources.json) pin the exact
profile and implementation used for this review. The saved files are research
data and were not executed.

- [Model profile](research/nbfc-an515-54/profile.json)
- [Fan read/write implementation](research/nbfc-an515-54/fan.c.txt)
- [Initialization/reset implementation](research/nbfc-an515-54/register_write_configuration_utils.c.txt)
