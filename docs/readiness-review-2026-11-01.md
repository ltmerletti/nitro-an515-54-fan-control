# Backend readiness review

This review covers the state after the three-minute AN515-54 maximum test. It
separates missing implementation from behavior that still needs hardware checks.

This is the earlier review. The NBFC adapter, independent guardian, external
recovery, systemd units and installer have since been implemented. See
[the current service runbook](backend-service.md) for the remaining on-laptop
checks. Other-model work is deferred.

## Confirmed on basement-server

The AN515-54 on BIOS V1.33 exposes real RPM readings. The guarded NBFC build
drove both fans to 6122 RPM and maintained the request for 180 seconds. Its saved
EC bytes were restored and verified; the fans slowed afterward. NBFC stopped and
EC write support returned to N. The temperature and microphone observations
answered the functional question of whether the fans respond. Further cooling
comparisons are not a prerequisite for the backend.

## Work blocking everyday control on this laptop

| Area | Present state | What completes it |
| --- | --- | --- |
| Manual speeds | The active test policy permits 100% targets only. Lower speeds are deliberately rejected. | Add bounded reduced-speed trials, enforce the configured minimum, and verify 30% and 60% respond and remain stable. |
| Mode ownership | A bounded supervisor owns each test. NitroFan's normal daemon still offers mock control or read-only hardware backends. | Connect the daemon to supervised NBFC control and implement deliberate Auto, Manual and Max transitions. Auto must release control to Acer firmware. |
| Recovery | Child-death, sensor-loss and termination paths pass synthetic tests. Saved-byte restoration has worked on hardware after normal trial completion. | Validate the production path for sensor loss, high-temperature override, process failure, suspend/resume and firmware takeover. |
| Service lifecycle | No production startup service or installer is enabled. | Package the service with one controller owner, firmware Auto at startup, local socket permissions, shutdown handoff and bounded restart behavior. |

The existing supervisor cannot restore registers after its own SIGKILL. The
production design needs recovery owned outside that process before automatic
restart can be considered complete. Mock and fake-EC tests establish software
behavior; they do not replace the remaining hardware checks.

The original five-minute maximum, reduced-speed and firmware-auto observations
remain outstanding. The current helper caps each maximum request at 180 seconds;
extending that limit should be an explicit, reviewed trial change.

## What blocks other models

The pinned NBFC 0.5.4 source includes nine exact AN515 profiles. It has no exact
AN515-55 or AN515-56 profile. This describes the audited commit, not every NBFC
release or community configuration. The inventory and source checksums are in
[nbfc-models-0.5.4.json](research/nbfc-models-0.5.4.json).

An upstream profile is a starting point for evaluation. Each additional model
needs a matching profile, register policy, firmware-handoff behavior and evidence
from the actual hardware/BIOS. The -57 and -58 profiles initialize register 0x03,
which the current reviewed guard forbids. The -58 also uses a different RPM
scale. These differences are recorded in
[nbfc-model-register-differences-0.5.4.json](research/nbfc-model-register-differences-0.5.4.json).
They must not be enabled by treating all AN515 laptops as the same device.

Additional-model testing does not need to delay finishing the -54 backend. Fan 1
and fan 2 can remain neutral labels, with both controlled together, until their
physical CPU/GPU assignments are verified. A panel interface can follow the
working service; it does not block backend development.
