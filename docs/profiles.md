# Multi-model profile catalogue and hardware identification

This document describes the configuration-driven model catalogue, hardware identification rules, and the procedure for adding new notebook models.

## Architecture

The catalogue records model identification, upstream provenance and trial artifacts as data. Adding a model to the catalogue does not require a new controller class. The standard-library loader validates the documented format; `profiles/schema.json` describes that format for editors. Exact DMI strings are matched against immutable records.

Registering a model does not enable its fan control. Hardware validation and a compatible write guard are separate requirements. Some models need guard features that the current maximum-only runner does not implement.

Hardware writes and active control remain strictly guarded. Only Acer Nitro AN515-54 on verified BIOS V1.33 is eligible for the guarded maximum-speed trial runner. All other models and unverified BIOS versions fail closed for trial writes. Read-only inspection remains available on any machine.

## Upstream provenance

Upstream profiles come from `nbfc-linux` 0.5.4 pinned at commit `9da010d6a2e8351bdf6900d6c3643f1e742af936`.

The pinned commit contains exact profiles for nine AN515 variants:

- Acer Nitro AN515-42 (`share/nbfc/configs/Acer Nitro AN515-42.json`, SHA256: `21b608e9a7bd2a1027b5e3d2061831bfdca55fca46d7b8a316e0b658fa141420`)
- Acer Nitro AN515-43 (`share/nbfc/configs/Acer Nitro AN515-43.json`, SHA256: `90b064cccc820fd47dde57a3c2f38a10ce3f1ddf442ff133fec37793f6c48817`)
- Acer Nitro AN515-44 (`share/nbfc/configs/Acer Nitro AN515-44.json`, SHA256: `85ecb193782206ec21402c0dab664da1aa38844a9c46d79615b8c4bfbbfb39c6`)
- Acer Nitro AN515-45 (`share/nbfc/configs/Acer Nitro AN515-45.json`, SHA256: `5eff0dd6f815f8dd5361f06060bf2223ad88d39ca46be7d812b5f4b9caebe78d`)
- Acer Nitro AN515-47 (`share/nbfc/configs/Acer Nitro AN515-47.json`, SHA256: `6e7a349c71fd946b25fcd97d1f186962b340a27e88b9b86d617d9ab5e81edac7`)
- Acer Nitro AN515-51 (`share/nbfc/configs/Acer Nitro AN515-51.json`, SHA256: `d17df1df577db9aaef4e34e64c4ef2ff63b07a09e76dd16b79115d0df401db3f`)
- Acer Nitro AN515-54 (`share/nbfc/configs/Acer Nitro AN515-54.json`, SHA256: `b04e2edc839169f7342dbbe8114f8ca66dee03dbd02d5685380b448d3161a568`)
- Acer Nitro AN515-57 (`share/nbfc/configs/Acer Nitro AN515-57.json`, SHA256: `9616731df305b40f11b1a5142d909b614684162aba88e99a8a489b14eb1cc260`)
- Acer Nitro AN515-58 (`share/nbfc/configs/Acer Nitro AN515-58.json`, SHA256: `7d2f3e1e734f5bed912ef0627c49746e38d2fa805c94ad6635038302622848ec`)

The pinned upstream commit does not include exact profiles for AN515-55 or AN515-56. The catalogue marks these models as `needs_profile`. Aliasing AN515-55 or AN515-56 to AN515-54 or guessing registers is forbidden.

## Support status lifecycle

Each model entry has a `support_status` field. It progresses through four tiers:

1. `needs_profile`:
   The hardware model exists, but no audited profile exists in the pinned upstream release. The tool reports this status explicitly and suggests researching upstream profile sources. No write actions are permitted.
2. `upstream_only`:
   An audited upstream profile exists with verified SHA256, but the project has not validated the profile on physical hardware. Read-only inspection and monitoring are available. All write operations remain disabled.
3. `guarded_max_trial_eligible`:
   The model has an allowlisted C guard policy, a dedicated maximum-only trial profile, and recorded physical evidence on a specific BIOS version. Only Acer Nitro AN515-54 on BIOS V1.33 currently holds this status.
4. Production control:
   No model is eligible for production control. The `production_control` capability flag is hardcoded to `false` across the entire codebase.

## Matching rules

Matching is strict and fail-closed:

- Vendor matching:
  The DMI `sys_vendor` string must equal the catalogue `vendor` field exactly.
- Product matching:
  The DMI `product_name` string must equal `product_name` or an item in `aliases` exactly.
- No partial matches:
  Zero prefix matching, substring matching, or regular expressions are permitted. Suffixes and near misses like `Nitro AN515-540` or `Nitro AN515-54A` fail closed.
- BIOS version checks:
  If a model has recorded evidence for specific BIOS versions, running under any other BIOS version downgrades the support tier to `upstream_only` and denies trial writes.
- No bypass flags:
  There is no `--force-model` or bypass flag.

## Developer inspection tool

Use `tools/profile_check.py` to inspect the catalogue and test DMI matching. This tool is a developer diagnostic. It never accesses `/dev/ec`, never loads kernel modules, and never writes files.

```sh
# List all models in the catalogue
python3 tools/profile_check.py --list

# List models in JSON format
python3 tools/profile_check.py --list --json

# Inspect a specific model
python3 tools/profile_check.py --model AN515-54

# Detect host DMI identity (read-only)
python3 tools/profile_check.py --detect

# Test detection against a synthetic sysfs root
python3 tools/profile_check.py --detect --dmi-root /path/to/fake/dmi/id --json
```

## Adding a new profile

To add a new model, update `profiles/catalog.json`. Do not write new Python controller logic.

Follow these steps:
1. Confirm the exact DMI `sys_vendor` and `product_name` from `/sys/class/dmi/id/`.
2. Confirm the exact upstream NBFC profile path under `share/nbfc/configs/` and compute its SHA256.
3. Add a new object to the `models` array in `profiles/catalog.json`.
4. Set `support_status` to `upstream_only` if the profile comes from upstream, or `needs_profile` if no profile exists.
5. Set `capabilities`:
   - `read_only_monitoring`: true
   - `guarded_max_trial`: false
   - `production_control`: false
6. Run the test suite:
   ```sh
   python3 -m unittest tests/test_models.py tests/test_profile_check.py -v
   ```

### Example catalogue entry

```json
{
  "model_id": "AN515-57",
  "vendor": "Acer",
  "product_name": "Nitro AN515-57",
  "aliases": [],
  "support_status": "upstream_only",
  "upstream_profile": {
    "path": "share/nbfc/configs/Acer Nitro AN515-57.json",
    "sha256": "9616731df305b40f11b1a5142d909b614684162aba88e99a8a489b14eb1cc260"
  },
  "capabilities": {
    "read_only_monitoring": true,
    "guarded_max_trial": false,
    "production_control": false
  },
  "next_steps": "Known upstream profile exists. Device-specific validation is required before trial writes can be authorized."
}
```

To record a reviewed maximum trial, also supply `local_artifacts`, BIOS-specific
`evidence` and relative `evidence.source_paths` pointing to its reports. Evidence
must include restoration and write-protection results. The loader rejects
incomplete trial records, ambiguous product identifiers, unknown fields and
non-boolean capability flags. Profile, policy and evidence paths must stay inside
the repository. Schema version 1 is currently supported.

The real trial preflight selects the matched entry's local policy and profile.
Explicit command-line paths must agree with that selection. The existing build
manifest then checks their exact hashes along with the binary and guard sources.
Real hardware identity always comes from `/sys/class/dmi/id`; an injected DMI root
is available only for diagnostics and synthetic checks.

Build preparation accepts explicit `--policy-path` and `--profile-path` arguments,
so future profiles do not have to use the -54 filenames. This does not relax the
runner's maximum-only structural checks or the C guard. In particular, the -57
and -58 upstream profiles initialize register 0x03, which this guard forbids.
See the [audited source differences](research/nbfc-model-register-differences-0.5.4.json).

## Checking an AN515-56

No AN515-56 profile exists in nbfc-linux, including upstream commit `2b8e31b`
(2026-10-26), and no `acer-wmi` fan quirk was found. The -54, -57 and -58 share
fan registers (tachometers at `0x13`/`0x15`, targets at `0x37`/`0x3a`, manual
mode at `0x21`/`0x22`). They differ in the unlock register: `0x10` on the -54,
`0x03` on the -57/-58. On the -57/-58, `0x03` also carries battery bits.

`tools/probe_model.py` is a single standard-library file. It checks a machine
read-only. Windows cannot read the EC without a third-party kernel driver, so
run it from an Ubuntu live USB:

```sh
sudo python3 probe_model.py > nitro-probe.json
```

It takes about 80 seconds, including 30 seconds of CPU load so the firmware
ramps the fans. A `matches_an515_54_layout` verdict means the tachometer words
behave like the fan RPM and the Auto control bytes match the -54. That is strong
evidence, not proof that writes work. The -56 then needs a catalogue entry, a
guardian identity entry, and one guarded write test using the -54 policy, which
never writes `0x03`.

## Remaining blockers for production fan control

The implementation gaps listed below describe the earlier readiness review.
The -54 now has a guarded NBFC adapter, independent recovery and systemd
packaging. Hardware checks for the new service remain pending. The current scope
and installation workflow are in [backend-service.md](backend-service.md).
Other-model validation is deferred until another model is needed.

Synthetic unit tests and short maximum-only runs do not prove hardware safety. The project remains in a bounded evaluation phase.

The following blockers must be resolved before production control can be considered:
1. Reduced-speed trial validation:
   Only maximum fan speed (100%) has been tested. Speeds below 100%, such as 30% and intermediate curve points, have not been evaluated on hardware.
2. Supervisor daemon design and auto handoff:
   The reviewed trial already isolates NBFC state under a private runtime directory. The production daemon must preserve that isolation and make firmware Auto at startup, mode switching and shutdown explicit. NBFC's own auto mode means a software curve. Recovery must also be owned outside the supervising process so its own death cannot prevent handoff.
3. Failure and power event handling:
   System behavior during suspend, resume, sensor disconnection, and emergency thermal cutoffs has not been tested on real hardware.
4. Physical fan assignments:
   CPU/GPU labels remain unverified. This blocks independently labelled fan control, but does not block controlling both fans together under neutral fan1/fan2 labels.
5. Other-model hardware validation:
   All other Nitro models in the catalogue remain unvalidated on physical hardware.
6. Service packaging:
   Systemd packaging, startup behavior and group-gated client access to the root service have not been implemented. Other-model testing and a panel interface need not delay the -54 backend.

The [backend readiness review](readiness-review-2026-11-01.md) separates these
implementation gaps from the remaining hardware checks.

## Verification

The reviewed local suite ran 401 tests, with 396 passing and five opt-in Linux
integration tests skipped. On basement-server, 63 targeted unit/diagnostic tests
and all five fake-EC integration tests passed. Real DMI detection selected the
AN515-54 on V1.33 and its existing maximum-only policy/profile. The existing
hardware-test manifest still matches all five attested guard/profile artifacts.
No hardware EC file was opened during these checks; write support remained N.

The integration tests use a separate user-owned build. They must not reuse the
real trial's private state, which can become root-owned after a sudo trial.
To recreate the dedicated build on basement-server:

```sh
python3 /home/luke/nbfc-evaluation/reviewed/tools/ec_guard/build_guarded_nbfc.py \
  --source-dir /home/luke/nbfc-evaluation/nbfc-linux-0.5.4 \
  --ec-guard-dir /home/luke/nbfc-evaluation/reviewed/tools/ec_guard \
  --output-dir /home/luke/nbfc-evaluation/profile-tests-runtime/bin \
  --private-root /home/luke/nbfc-evaluation/profile-tests-runtime \
  --manifest-path /home/luke/nbfc-evaluation/profile-tests-runtime/manifest.json
```

Then, from `/home/luke/nbfc-evaluation/reviewed`:

```sh
NITROFAN_GUARDED_MANIFEST=/home/luke/nbfc-evaluation/profile-tests-runtime/manifest.json \
  python3 -m unittest tests.test_nbfc_upstream_integration -v
```
