# First AN515-54 EC dump

Source: user-provided `od -Ax -tx1 -v -N256` output from basement-server,
November 1, 2026. Exact collection time and simultaneous temperatures/RPM
were not supplied. This is an initial baseline, not a verified idle sample.
The machine identifies as Nitro AN515-54, BIOS V1.33.

`ec_sys` was loaded with `write_support=0`; its parameter printed `N`.
The [unaltered dump](ec-baseline-2026-11-01.hex) contains all 256 byte offsets.
No EC writes were performed by the discovery command.

| Candidate location | Observed bytes | Implication |
| --- | --- | --- |
| `0x03` | `21` | Original value is `0x21`. Battery/control bit meanings are unknown; do not replace it with the sibling unlock value. |
| `0x13` | `d9` | Decimal 217 does not fit the proposed single-byte percent interpretation. |
| `0x15` | `60` | Decimal 96 is ambiguous without paired observations. |
| `0x21`, `0x22` | `00`, `00` | These differ from the sibling map's proposed firmware auto values (`0x10`, `0x04`). |
| `0x37`, `0x3a` | `00`, `00` | Zero targets under firmware control do not establish speed control semantics. |

A hypothesis for further read-only comparison: treating `0x13`/`0x14` as
a little-endian 16-bit integer gives 2521; `0x15`/`0x16` gives 2400. Those are
plausible RPM values for these fans. This does not establish their units,
CPU/GPU assignments, stability, or relationship to the driver tachometers.
Sequential byte reads can also span a firmware update of a multi-byte value.

The next collection should record raw EC snapshots, neutral `fan1`/`fan2`
tachometers, CPU/GPU temperatures, and timestamps together. Compare changes
across normal activity before considering the specification's load phase.
Record cooling afterwards as well. Do not enable writes or attempt the
sibling mode/unlock sequence on the strength of this dump.
