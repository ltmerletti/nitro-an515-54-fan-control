#!/usr/bin/env bash
set -euo pipefail

duration=30
use_mic=0
mic_device='plughw:CARD=PCH,DEV=0'
while (( $# )); do
    case "$1" in
        --duration)
            [[ $# -ge 2 ]] || { echo "--duration needs a value." >&2; exit 2; }
            duration="$2"
            shift 2
            ;;
        --mic) use_mic=1; shift ;;
        --mic-device)
            [[ $# -ge 2 ]] || { echo "--mic-device needs a value." >&2; exit 2; }
            mic_device="$2"
            shift 2
            ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ ! "$duration" =~ ^[1-9][0-9]{0,2}$ ]] || (( duration > 180 )); then
    echo "--duration must be an integer from 1 to 180 seconds." >&2
    exit 2
fi

if [[ "$EUID" != 0 ]]; then
    echo "Run this bounded maximum-only trial with sudo." >&2
    exit 1
fi
script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if (( use_mic )); then
    exec python3 "$script_directory/nbfc_noise_comparison.py" \
        --duration "$duration" --mic-device "$mic_device"
fi
supervisor="$script_directory/nbfc_guarded_trial.py"
child_pid=""
write_stage=0

cleanup() {
    local result="$?"
    trap - EXIT
    trap '' INT TERM HUP
    if [[ -n "$child_pid" ]]; then
        kill -TERM "$child_pid" 2>/dev/null || true
        wait "$child_pid" 2>/dev/null || true
    fi
    if (( write_stage )); then
        if ! modprobe -r ec_sys || ! modprobe ec_sys write_support=0; then
            echo "ERROR: Could not reload ec_sys with writes disabled." >&2
            result=1
        elif [[ "$(cat /sys/module/ec_sys/parameters/write_support)" != N ]]; then
            echo "ERROR: EC write protection could not be verified." >&2
            result=1
        else
            echo "EC write support is N again."
        fi
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if pgrep -f '/nitrofan-observations/[^[:space:]]*/monitor.py' >/dev/null; then
    echo "The Acer Auto baseline recording is still running. Retry after it finishes." >&2
    exit 1
else
    process_check="$?"
    [[ "$process_check" == 1 ]]
fi

# Validate the binary, sensors and saved baseline while the kernel still denies writes.
python3 "$supervisor" --duration "$duration" --check-only
echo "Enabling EC writes for one $duration-second, maximum-only NBFC trial."
write_stage=1
modprobe -r ec_sys
modprobe ec_sys write_support=1
[[ "$(cat /sys/module/ec_sys/parameters/write_support)" == Y ]]
python3 "$supervisor" --duration "$duration" &
child_pid="$!"
result=0
wait "$child_pid" || result="$?"
child_pid=""
if (( result )); then
    echo "Trial failed. Check the log for EC restoration errors; fan control is not verified." >&2
else
    echo "Bounded trial completed and saved bytes verified. Physical fan response still needs review."
fi
exit "$result"
