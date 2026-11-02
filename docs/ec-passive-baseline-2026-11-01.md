# Paired EC and tachometer capture

The read-only capture on basement-server contains 30 complete 256-byte samples
from November 1, 2026, 17:42:48 to 17:43:52 UTC. This covers about 64 seconds of
normal server activity. No artificial workload or fan-control writes were used.

The observations support two RPM readbacks on this AN515-54 with BIOS V1.33.

| EC bytes, low first | Driver tachometer | Exact matches | Mean absolute difference | Largest difference |
| --- | --- | --- | --- | --- |
| `0x13`, `0x14` | `fan1_input` | 27/30 | 4.37 RPM | 53 RPM |
| `0x15`, `0x16` | `fan2_input` | 29/30 | 0.83 RPM | 25 RPM |

Decode each pair as `low + 256 * high`. Both fields track the matching driver
tachometer over changing fan speeds. The small differences are consistent with
reading EC bytes and driver telemetry at different times. Each complete sample
took 0.173 to 0.300 seconds. Neither multi-byte reads nor the combined telemetry
are atomic. This capture validates the RPM interpretation for monitoring;
it does not establish which physical fan cools the CPU or GPU.

The proposed control locations did not change. Register `0x03` remained `0x21`;
`0x21`, `0x22`, `0x37`, and `0x3a` remained zero. These observations do not validate
the sibling unlock or manual-mode sequence. Keep hardware writes disabled.

CPU package temperature ranged from 49 to 95 C, with a final sample of 53 C.
GPU temperature ranged from 42 to 45 C. The fans rose during normal activity
and slowed as the system cooled, providing useful variation without a stress
test. A later direct temperature reading was 54 C. Because this workload
already caused high temperatures, further artificial load is deferred.

The next backend step is configurable, read-only 16-bit RPM support using these
neutral fan identifiers. Control discovery still needs separate evidence,
ideally EC comparisons of Auto, Max, and Custom settings in Windows NitroSense
if Windows is available. Do not infer writable offsets from RPM correlation.

Evidence files:

- [Raw JSON lines](data/ec-passive-baseline-2026-11-01.jsonl)
- [Analysis and source SHA-256](ec-passive-baseline-2026-11-01-analysis.json)
- [Interactive plot](ec-passive-baseline-2026-11-01.html)
- [First unpaired dump](ec-baseline-2026-11-01.md)
